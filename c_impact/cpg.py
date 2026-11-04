"""CPG (Code Property Graph): AST · CFG · 제어 의존(CDG) · 데이터 의존(DDG) · 호출 관계를 한 그래프로 묶은 보기.

program.Program 이 이미 계산한 정보(문장 단위 AST 노드, 함수별 CFG, 후위 지배자 기반 제어 의존, 도달 정의·포인터 분석
기반 데이터 의존, 호출 site)를 Joern 의 CPG 와 같은 노드·간선 종류로 노출하고, 그 위에서 질의를 제공한다.

  cpg.methods("Dcm_*")                    함수 (METHOD)
  cpg.calls("memcpy")                      호출 지점 (CALL)
  cpg.taint(rules)                         외부 입력(Source)에서 시작하는 데이터 흐름 (Taint)
  cpg.reachable_by_flows(sink, taint)      Sink 피연산자까지 이어지는 Source → Sink 경로
  cpg.export_json(path, funcs)             노드·간선을 JSON 으로 저장 (Joern 없이도 그래프를 볼 수 있게)

Taint 전파는 데이터 흐름만 따른다(값 의존, 메모리, 인자, 반환). 제어 의존은 '변경 영향' 쪽(slicer)에서 다룬다.
포인터 인자로 들어온 외부 입력은 포인터 값이 아니라 '가리키는 데이터'를 오염원으로 본다.
"""

from __future__ import annotations

import fnmatch
import json
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

from pycparser import c_ast

from .program import Program, display_path, overlaps
from .slicer import Hit

NODE_LABEL = {
    "entry": "METHOD", "exit": "METHOD_RETURN", "formal_in": "METHOD_PARAMETER_IN", "stmt": "STATEMENT",
    "cond": "CONTROL_STRUCTURE", "return": "RETURN", "call": "CALL", "extcall": "CALL", "actual_in": "ARGUMENT",
    "actual_out": "CALL_RESULT", "ginit": "GLOBAL_INIT", "nop": "JUMP_TARGET",
}


@dataclass
class TaintResult:
    hits: dict[int, Hit] = field(default_factory=dict)          # 오염된 노드 → 처음 도달한 경로 정보
    tdefs: dict[int, set] = field(default_factory=dict)         # 노드가 오염시키는 메모리 위치
    sources: dict[int, str] = field(default_factory=dict)       # 출발 노드 → Source 이름
    labels: dict[int, str] = field(default_factory=dict)        # 출발 노드 → 설명 (함수 인자·호출)

    _by_base: dict | None = None

    def by_base(self) -> dict:
        if self._by_base is None:
            idx: dict[str, list] = {}
            for d, tset in self.tdefs.items():
                for t in tset:
                    idx.setdefault(t[0], []).append((d, t))
            self._by_base = idx
        return self._by_base

    def source_of(self, nid: int) -> int:
        cur = self.hits.get(nid)
        while cur is not None and cur.pred is not None:
            cur = self.hits.get(cur.pred)
        return cur.node if cur is not None else nid


class CPG:
    def __init__(self, prog: Program):
        self.prog = prog

    # ------------------------------------------------------------------
    # 질의
    # ------------------------------------------------------------------

    def methods(self, pattern: str = "*"):
        return [f for f in self.prog.funcs.values() if fnmatch.fnmatchcase(f.name, pattern)]

    def calls(self, pattern: str = "*") -> list[int]:
        """이름이 pattern 과 맞는 호출 지점의 노드 (외부 함수는 extcall, 내부 함수는 call 노드)."""
        return [s.call_node for s in self.prog.sites if fnmatch.fnmatchcase(s.name, pattern) and s.call_node >= 0]

    def argument(self, call_node: int, index: int):
        """호출의 index 번째 인자 (1부터, -1 = 마지막). (노드, 인자 AST)"""
        n = self.prog.nodes[call_node]
        site = self.prog.sites[n.site]
        args = site.ast.args.exprs if site.ast.args else []
        if not args:
            return None
        i = len(args) + index if index < 0 else index - 1
        if not 0 <= i < len(args):
            return None
        if site.actual_in and i < len(site.actual_in):
            return site.actual_in[i], args[i]
        return call_node, args[i]

    def controlled_by(self, nid: int) -> list[tuple[int, str]]:
        return [(p, lab) for p, lab in self.prog.cd_parents.get(nid, ()) if self.prog.nodes[p].kind == "cond"]

    # ------------------------------------------------------------------
    # Taint (Source → 데이터 흐름)
    # ------------------------------------------------------------------

    def _pointee(self, path: tuple) -> set:
        objs = set(self.prog.pts.get(path, set()))
        return {o for o in objs if not o[0].startswith("fn:")}

    def _source_seeds(self, rules) -> list[tuple[int, set, str, str]]:
        p = self.prog
        seeds = []
        for f in p.funcs.values():
            for sname, args in rules.param_sources(f.name):
                for i, key in enumerate(f.params):
                    v = p.vars[key]
                    if args and v.name not in args and (i + 1) not in args and (i - len(f.params)) not in args:
                        continue
                    if i >= len(f.formal_in):
                        continue
                    if v.env is not None and (v.env.is_pointer(v.type) or v.env.is_array(v.type)):
                        objs = self._pointee((key,))
                        if not objs:
                            # 호출자가 분석 범위 밖인 포인터 인자: 대상을 모르는 객체(sym:*이름)로 본다
                            sk = f"sym:*{v.name}"
                            objs = {(sk,)} if sk in p.vars else set()
                        if not objs:
                            continue
                        paths, what = objs, f"*{v.name}"
                    else:
                        paths, what = {(key,)}, v.name
                    seeds.append((f.formal_in[i], paths, sname, f"{f.name}() 인자 {what}"))
        for s in p.sites:
            if s.call_node < 0:
                continue
            args = s.ast.args.exprs if s.ast.args else []
            for sname, idxs in rules.out_arg_sources(s.name):
                paths = set()
                for ix in idxs:
                    j = len(args) + ix if ix < 0 else ix - 1
                    if 0 <= j < len(args):
                        try:
                            paths |= {o for o in p.pvals(args[j]) if not o[0].startswith("fn:")}
                        except Exception:
                            pass
                if paths:
                    seeds.append((s.call_node, paths, sname, f"{s.name}() 출력 인자"))
            for sname in rules.return_sources(s.name):
                out_node = s.call_node if s.actual_out is None else s.actual_out
                seeds.append((out_node, {(f"{s.caller}::$out{s.id}",)}, sname, f"{s.name}() 반환값"))
        return seeds

    def taint(self, rules) -> TaintResult:
        p = self.prog
        res = TaintResult()
        q: deque = deque()
        for nid, paths, sname, label in self._source_seeds(rules):
            if p.nodes[nid].dead:
                continue
            if nid not in res.hits:
                res.hits[nid] = Hit(nid, None, ("source", sname), len(res.hits))
                res.sources[nid] = sname
                res.labels[nid] = label
                res.tdefs[nid] = set()
                q.append(nid)
            res.tdefs[nid] |= paths

        def reach(m, d, edge, defs=None):
            n = p.nodes[m]
            if n.dead:
                return
            new = set(defs if defs is not None else n.defs)
            if m in res.hits:
                if new - res.tdefs[m]:
                    res.tdefs[m] |= new
                    q.append(m)
                return
            res.hits[m] = Hit(m, d, edge, len(res.hits))
            res.tdefs[m] = new
            q.append(m)

        while q:
            d = q.popleft()
            n = p.nodes[d]
            for path in list(res.tdefs[d]):
                if path[0] in p.use_index:  # 전역·static·포인터 대상: 흐름 비민감
                    for m, u in p.use_index[path[0]]:
                        if m != d and overlaps(path, u):
                            reach(m, d, ("memory", display_path(p, u)))
                else:
                    label = display_path(p, path)
                    for m, lab in p.dd.get(d, ()):
                        if lab == label:
                            reach(m, d, ("data", lab))
            if n.kind == "actual_in":
                site = p.sites[n.site]
                for c in sorted(site.callees):
                    f = p.funcs[c]
                    if n.arg < len(f.formal_in):
                        pk = f.params[n.arg]
                        reach(f.formal_in[n.arg], d, ("param", p.vars[pk].name), {(pk,)})
            elif n.kind == "return":
                for sid in p.callers.get(n.func, ()):
                    site = p.sites[sid]
                    if site.actual_out is not None:
                        reach(site.actual_out, d, ("return", n.func))
        return res

    def reachable_by_flows(self, sink_node: int, reads: set, taint: TaintResult):
        """sink 피연산자가 읽는 위치(reads) 중 오염된 것을 공급한 노드: (공급 노드, 위치, 간선 종류) 또는 None."""
        p = self.prog
        best = None
        idx = taint.by_base()
        for u in reads:
            cands = []
            for d, t in idx.get(u[0], ()):
                if not overlaps(t, u):
                    continue
                if u[0] in p.use_index:
                    if d != sink_node:
                        cands.append((d, "memory"))
                elif any(m == sink_node for m, _ in p.dd.get(d, ())):
                    cands.append((d, "data"))
            for d, kind in cands:
                o = taint.hits[d].order
                if best is None or o < best[0]:
                    best = (o, d, u, kind)
        return None if best is None else best[1:]

    # ------------------------------------------------------------------
    # 내보내기
    # ------------------------------------------------------------------

    def export_json(self, path: Path, funcs: set[str] | None = None, max_edges: int = 300000) -> dict:
        p = self.prog
        keys = set(p.funcs) if funcs is None else {k for k in funcs if k in p.funcs}
        nodes, edges = [], []
        ids: set[int] = set()
        for k in sorted(keys):
            f = p.funcs[k]
            for nid in f.nodes:
                n = p.nodes[nid]
                if n.dead:
                    continue
                ids.add(nid)
                nodes.append({"id": nid, "label": NODE_LABEL.get(n.kind, n.kind.upper()), "method": f.name,
                              "code": n.text, "file": _rel(p, n.file), "line": n.line})
        for k in sorted(keys):
            f = p.funcs[k]
            for a, succs in f.succ.items():
                for b, lab in succs:
                    if a in ids and b in ids:
                        edges.append([a, b, "CFG", lab or ""])
        for a in ids:
            for b, lab in p.cd_children.get(a, ()):
                if b in ids:
                    edges.append([a, b, "CDG", lab or ""])
            for b, lab in p.dd.get(a, ()):
                if b in ids:
                    edges.append([a, b, "REACHING_DEF", lab])
        for a in ids:
            for d in p.nodes[a].defs:
                if d[0] in p.use_index:
                    for b, u in p.use_index[d[0]]:
                        if b in ids and b != a and overlaps(d, u):
                            edges.append([a, b, "REACHING_DEF", display_path(p, u)])
                if len(edges) > max_edges:
                    break
        for s in p.sites:
            if s.call_node in ids:
                for c in sorted(s.callees):
                    f = p.funcs[c]
                    if f.entry in ids:
                        edges.append([s.call_node, f.entry, "CALL", s.name])
                    for i, a in enumerate(s.actual_in):
                        if i < len(f.formal_in) and f.formal_in[i] in ids:
                            edges.append([a, f.formal_in[i], "ARGUMENT", str(i + 1)])
                    for r in f.returns:
                        if s.actual_out is not None and r in ids:
                            edges.append([r, s.actual_out, "RETURN", f.name])
        data = {
            "format": "c_impact CPG 1 (노드 label·간선 type 은 Joern CPG 이름을 따름)",
            "methods": [{"name": p.funcs[k].name, "key": k, "file": _rel(p, p.funcs[k].file), "line": p.funcs[k].line}
                        for k in sorted(keys)],
            "nodes": nodes, "edges": edges[:max_edges], "truncated": len(edges) > max_edges,
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return {"nodes": len(nodes), "edges": min(len(edges), max_edges), "methods": len(keys)}


def _rel(prog: Program, path: str) -> str:
    try:
        return Path(path).resolve().relative_to(prog.root.resolve()).as_posix()
    except (ValueError, OSError):
        return path


def is_lvalue_write(stmt_ast, target) -> bool:
    """target(ArrayRef 등)이 문장에서 쓰이는 쪽(대입 왼쪽·증감)인가."""
    for n in _iter(stmt_ast):
        if isinstance(n, c_ast.Assignment) and n.lvalue is target:
            return True
        if isinstance(n, c_ast.UnaryOp) and n.op in ("++", "--", "p++", "p--") and n.expr is target:
            return True
    return False


def _iter(e):
    stack = [e]
    while stack:
        n = stack.pop()
        if n is None:
            continue
        yield n
        stack.extend(c for _, c in n.children())
