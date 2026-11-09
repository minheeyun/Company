"""비기능 영향 추정: 스택·RAM·코드 크기 대리 지표.

출력이 같아도 스택 오버플로·RAM 초과·주기 초과가 생길 수 있으므로 변경 전후 차이를 함께 보고한다.
  * 함수 스택 프레임 ≈ 지역 변수 크기 합(레지스터 할당·패딩·호출 규약 미반영)
  * 태스크별 최악 스택 ≈ 실행 문맥 진입 함수에서 호출 그래프를 따라간 프레임 합의 최댓값
    (재귀가 있으면 '무한(재귀)'으로 표시)
  * RAM ≈ 정의된 전역/정적 변수 크기 합, 코드 크기 ≈ 함수 AST 노드 수(실행시간 대리 지표 아님)
정확한 값은 링커 맵·스택 분석 도구(예: 컴파일러 스택 사용 보고서)로 확인해야 한다.
"""
from __future__ import annotations

from typing import Dict, Optional

from .model import EK, NK, AnalysisGraph, walk_ast


def type_size(ty: Optional[dict], records: Dict[str, dict] = None) -> int:
    if not ty:
        return 0
    k = ty.get("k")
    if k in ("i", "b", "fl"):
        return max(1, int(ty.get("b", 32)) // 8)
    if k == "p":
        return max(1, int(ty.get("b", 32)) // 8)
    if k == "r":
        if ty.get("size") and ty["size"] > 0:
            return int(ty["size"])
        rec = (records or {}).get(ty.get("rec"))
        return int(rec.get("size") or 0) if rec else 0
    if k == "a":
        cnt = ty.get("cnt") or 0
        return max(0, int(cnt)) * type_size(ty.get("el"), records)
    return 0


def _records(g: AnalysisGraph) -> Dict[str, dict]:
    return {g.node(r).get("name"): g.node(r) for r in g.nodes_of(NK.RECORD)}


def function_frames(g: AnalysisGraph) -> Dict[str, dict]:
    recs = _records(g)
    out = {}
    for f in g.nodes_of(NK.FUNCTION):
        ast = g.asts.get(f)
        if not ast:
            continue
        stack = 0
        nodes = 0
        for n in walk_ast(ast):
            nodes += 1
            if n.get("k") == "Var" and not n.get("static"):
                stack += type_size(n.get("ty"), recs)
        stack += sum(type_size(p.get("ty"), recs) for p in ast.get("params", []))
        out[f] = {"stack": stack, "code_nodes": nodes}
    return out


def task_stacks(g: AnalysisGraph, frames: Dict[str, dict]) -> Dict[str, Optional[int]]:
    """실행 문맥별 최악 호출 체인 스택(None = 재귀로 상한 없음)."""
    memo: Dict[str, Optional[int]] = {}
    onstack = set()

    def worst(f: str) -> Optional[int]:
        if f in memo:
            return memo[f]
        if f in onstack:
            return None
        onstack.add(f)
        best = 0
        for c in set(g.succ(f, [EK.CALLS])):
            w = worst(c)
            if w is None:
                onstack.discard(f)
                memo[f] = None
                return None
            best = max(best, w)
        onstack.discard(f)
        memo[f] = frames.get(f, {}).get("stack", 0) + best
        return memo[f]

    out = {}
    for t in g.nodes_of(NK.TASK):
        vals = [worst(e) for e in g.succ(t, [EK.ACTIVATES])]
        out[g.node(t).get("name")] = None if any(v is None for v in vals) else max(vals, default=0)
    return out


def ram_total(g: AnalysisGraph) -> int:
    recs = _records(g)
    total = 0
    for k in g.nodes_of(NK.GLOBAL):
        if g.node(k).get("external"):
            continue
        ast = g.asts.get(k) or {}
        total += type_size(ast.get("ty"), recs)
    return total


def resource_report(g_old: AnalysisGraph, g_new: AnalysisGraph) -> dict:
    fo, fn = function_frames(g_old), function_frames(g_new)
    to, tn = task_stacks(g_old, fo), task_stacks(g_new, fn)
    funcs = []
    for f in sorted(set(fo) | set(fn)):
        a, b = fo.get(f, {}), fn.get(f, {})
        if a.get("stack") != b.get("stack") or a.get("code_nodes") != b.get("code_nodes"):
            funcs.append({"function": f.split(":")[-1], "stack_old": a.get("stack"), "stack_new": b.get("stack"),
                          "code_nodes_old": a.get("code_nodes"), "code_nodes_new": b.get("code_nodes")})
    tasks = [{"context": t, "stack_old": to.get(t), "stack_new": tn.get(t)}
             for t in sorted(set(to) | set(tn)) if to.get(t) != tn.get(t)]
    return {"functions": funcs, "tasks": tasks, "ram_old": ram_total(g_old), "ram_new": ram_total(g_new),
            "note": "스택=지역변수 합(추정), 태스크 스택=최악 호출 체인 합, RAM=전역/정적 변수 합, 코드=AST 노드 수(대리 지표)"}
