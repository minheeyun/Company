"""2.5단계 MVP: 함수 단위 간이 영향 분석과 비교 기준선(baseline).

정밀 분석(CFG·PDG·SDG)은 구현 비용이 크므로, 먼저 실제 데이터에서 전체 흐름이 쓸 만한 결과를
내는지 이 간이 분석으로 확인한다. 정밀 분석이 이 간이 분석보다 얼마나 나은지가 평가 결과가 된다.

  mvp        호출 그래프 + 전역변수 읽기·쓰기 + 함수 요약(반환값·포인터 부작용 유무)
             변경 함수 -> (반환값/부작용이 있으면) 호출자 -> ... , 쓰는 전역 -> 그 전역을 읽는 함수 -> ...
             변경 함수가 인자를 넘기는 직접 피호출 함수까지 포함
  callgraph  호출 그래프만: 변경 함수의 모든 (간접) 호출자와 피호출자
기능 영향: 관찰 대상(함수 반환값·전역·신호·레지스터)을 소유/쓰는 함수가 영향 집합에 들어오면 영향 있음.
"""
from __future__ import annotations

from collections import deque
from typing import Dict, List, Optional, Set

from .model import EK, NK, AnalysisGraph


def _changed_functions(g: AnalysisGraph, ch: dict, side: str) -> Set[str]:
    out = set()
    unit = ch.get("unit_new" if side == "new" else "unit_old")
    if unit and g.has_node(unit) and g.kind(unit) == NK.FUNCTION:
        out.add(unit)
    for a in (ch.get(side) or {}).get("anchors", []):
        f = g.func_of(a) if g.has_node(a) else None
        if f:
            out.add(f)
        if a.startswith("loc:") and g.has_node(a):
            for w in g.pred(a, [EK.GLOBAL_WRITE]):
                if g.func_of(w):
                    out.add(g.func_of(w))
            for r in g.succ(a, [EK.GLOBAL_READ]):
                if g.func_of(r):
                    out.add(g.func_of(r))
    return out


def _has_output(g: AnalysisGraph, f: str) -> bool:
    """함수 요약: 반환값이 있거나 포인터를 통한 부작용(fo_mem로 들어오는 간선)이 있는가."""
    if g.has_node(f"{f}#fo_ret"):
        return True
    return bool(g.pred(f"{f}#fo_mem", [EK.DATA])) if g.has_node(f"{f}#fo_mem") else False


def mvp_affected(g: AnalysisGraph, changed: Set[str]) -> Dict[str, Optional[str]]:
    """영향 함수 -> 그 함수에 도달한 이전 함수(근거 사슬)."""
    parent: Dict[str, Optional[str]] = {f: None for f in changed}
    q = deque(changed)
    readers: Dict[str, Set[str]] = {}
    for f in g.nodes_of(NK.FUNCTION):
        for gv in g.succ(f, [EK.READS]):
            readers.setdefault(gv, set()).add(f)
    while q:
        f = q.popleft()
        nxt = []
        if _has_output(g, f):
            nxt += [c for c in g.pred(f, [EK.CALLS]) if g.kind(c) == NK.FUNCTION]
        for gv in g.succ(f, [EK.WRITES]):
            nxt += list(readers.get(gv, ()))
        if f in changed:
            nxt += [c for c in g.succ(f, [EK.CALLS]) if not g.node(c).get("external")
                    and (g.node(c).get("params") or [])]
        for x in nxt:
            if x not in parent:
                parent[x] = f
                q.append(x)
    return parent


def callgraph_affected(g: AnalysisGraph, changed: Set[str]) -> Set[str]:
    out = set(changed)
    for direction in ("up", "down"):
        q = deque(changed)
        seen = set(changed)
        while q:
            f = q.popleft()
            nb = g.pred(f, [EK.CALLS]) if direction == "up" else g.succ(f, [EK.CALLS])
            for x in nb:
                if x not in seen and g.kind(x) == NK.FUNCTION:
                    seen.add(x)
                    q.append(x)
        out |= seen
    return out


def feature_owners(g: AnalysisGraph) -> Dict[str, Set[str]]:
    """기능 -> 관찰 대상을 만들어 내는 함수들."""
    out: Dict[str, Set[str]] = {}
    for feat in g.nodes_of(NK.FEATURE):
        fid = g.node(feat).get("fid")
        owners: Set[str] = set()
        for o in g.succ(feat, [EK.HAS_OBSERVATION]):
            for a in g.succ(o, [EK.OBSERVES]):
                if g.func_of(a):
                    owners.add(g.func_of(a))
                k = g.kind(a)
                if k == NK.LOCATION:
                    owners |= {g.func_of(w) for w in g.pred(a, [EK.GLOBAL_WRITE]) if g.func_of(w)}
                if k == NK.SIGNAL:
                    for w in g.pred(a, [EK.SIGNAL_WRITE]):
                        if g.func_of(w):
                            owners.add(g.func_of(w))
                        if g.kind(w) == NK.LOCATION:
                            owners |= {g.func_of(x) for x in g.pred(w, [EK.GLOBAL_WRITE]) if g.func_of(x)}
        out[fid] = owners
    return out


def _chain(parent: Dict[str, Optional[str]], f: str) -> List[str]:
    out = []
    x: Optional[str] = f
    guard = 0
    while x is not None and guard < 1000:
        guard += 1
        out.append(x.split(":")[-1])
        x = parent.get(x)
    return list(reversed(out))


def mvp_analyze(go: AnalysisGraph, gn: AnalysisGraph, changes: List[dict]) -> List[dict]:
    own_o, own_n = feature_owners(go), feature_owners(gn)
    out = []
    for ch in changes:
        entry = {"change": ch["id"], "type": ch["type"], "unit": ch.get("unit_name"), "features": {}, "functions": set(),
                 "callgraph_functions": set(), "callgraph_features": set()}
        for side, g, owners in (("old", go, own_o), ("new", gn, own_n)):
            changed = _changed_functions(g, ch, side)
            if not changed:
                continue
            par = mvp_affected(g, changed)
            entry["functions"] |= set(par)
            cg = callgraph_affected(g, changed)
            entry["callgraph_functions"] |= cg
            for fid, own in owners.items():
                hit = sorted(own & set(par))
                if hit and fid not in entry["features"]:
                    entry["features"][fid] = " -> ".join(_chain(par, hit[0]))
                if own & cg:
                    entry["callgraph_features"].add(fid)
        entry["functions"] = sorted(f.split(":")[-1] for f in entry["functions"])
        entry["callgraph_functions"] = sorted(f.split(":")[-1] for f in entry["callgraph_functions"])
        entry["callgraph_features"] = sorted(entry["callgraph_features"])
        out.append(entry)
    return out
