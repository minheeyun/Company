"""구간(interval) 추상 해석: 함수 내부 정수 변수의 값 범위와 분기 실행 가능성.

  * 추상 도메인: 변수별 [lo, hi] (자료형 범위가 top), 합류 지점은 hull(합집합 근사)
  * 반복문: 되돌림 간선이 들어오는 노드에서 2회 방문 뒤 widening(늘어나는 경계를 자료형 끝으로)
    으로 계산 종료를 보장하고, 이후 narrowing 패스로 다시 좁힌다
  * 조건 간선(T/F)에서 'x op 상수' 형태로 범위를 정제
결과는 조건 노드별 '참/거짓 가능 여부'와 관련 변수 범위다. 정적으로 항상 참/거짓인 조건(죽은 분기)을
보고하고, 변경된 조건에 정적 범위 정보를 덧붙인다. 전역·포인터·호출 결과는 자료형 범위(top)로 둔다.
"""
from __future__ import annotations

from typing import Dict, Optional, Tuple

from .cfg import build_cfg, strip_casts
from .model import NK, AnalysisGraph

Itv = Tuple[int, int]


def type_range(ty: Optional[dict]) -> Itv:
    if not ty or ty.get("k") not in ("i", "b"):
        return (-(1 << 63), (1 << 63) - 1)
    if ty.get("k") == "b":
        return (0, 1)
    b = int(ty.get("b", 32))
    return (-(1 << (b - 1)), (1 << (b - 1)) - 1) if ty.get("s") else (0, (1 << b) - 1)


def _clamp(i: Itv, ty: Optional[dict]) -> Itv:
    lo, hi = type_range(ty)
    if i[0] < lo or i[1] > hi:
        return (lo, hi)          # 넘침 가능 -> wrap 결과는 자료형 전체
    return i


def _hull(a: Optional[Itv], b: Optional[Itv]) -> Optional[Itv]:
    if a is None:
        return b
    if b is None:
        return a
    return (min(a[0], b[0]), max(a[1], b[1]))


State = Dict[str, Itv]


class IntervalAnalysis:
    def __init__(self, fkey: str, ast: dict, param_domains: Optional[Dict[str, Itv]] = None):
        self.fkey = fkey
        self.ast = ast
        self.cfg = build_cfg(fkey, ast)
        self.types: Dict[str, dict] = {}
        self.param_domains = param_domains or {}
        for p in ast.get("params", []):
            self.types[p["key"]] = p.get("ty") or {}
        self.cond_info: Dict[str, dict] = {}

    # ---- 식 ----
    def ev(self, n: dict, st: State) -> Itv:
        k = n.get("k")
        ty = n.get("ty") or {}
        if k == "Lit" and isinstance(n.get("v"), int):
            return (n["v"], n["v"])
        if k == "Ref":
            if n.get("rk") == "e" and n.get("val") is not None:
                return (n["val"], n["val"])
            if n.get("rk") in ("l", "p") and n.get("ref") in st:
                return st[n["ref"]]
            return type_range(ty)
        if k in ("ICast", "Cast"):
            c = n["ch"][0] if n.get("ch") else None
            if c is None:
                return type_range(ty)
            return _clamp(self.ev(c, st), ty) if ty.get("k") in ("i", "b") else type_range(ty)
        if k == "Bin":
            op = n.get("v")
            a = self.ev(n["ch"][0], st)
            b = self.ev(n["ch"][1], st)
            if op in ("<", "<=", ">", ">=", "==", "!=", "&&", "||"):
                t, f = self._cmp(op, a, b)
                return (0 if f else 1, 1 if t else 0)
            if op == "+":
                return _clamp((a[0] + b[0], a[1] + b[1]), ty)
            if op == "-":
                return _clamp((a[0] - b[1], a[1] - b[0]), ty)
            if op == "*":
                xs = [a[0] * b[0], a[0] * b[1], a[1] * b[0], a[1] * b[1]]
                return _clamp((min(xs), max(xs)), ty)
            if op == "/" and b[0] == b[1] and b[0] > 0:
                return _clamp((int(a[0] / b[0]), int(a[1] / b[0])), ty)
            if op == "&" and b[0] == b[1] and b[0] >= 0 and a[0] >= 0:
                return (0, min(a[1], b[0]))
            if op == ">>" and b[0] == b[1] and b[0] >= 0 and a[0] >= 0:
                return (a[0] >> b[0], a[1] >> b[0])
            if op == "%" and b[0] == b[1] and b[0] > 0 and a[0] >= 0:
                return (0, min(a[1], b[0] - 1))
            return type_range(ty)
        if k == "Un":
            op = n.get("v")
            c = n["ch"][0] if n.get("ch") else None
            if op == "!":
                return (0, 1)
            if op == "-" and c is not None:
                a = self.ev(c, st)
                return _clamp((-a[1], -a[0]), ty)
            if op in ("pre++", "pre--", "post++", "post--") and c is not None:
                s = strip_casts(c)
                a = self.ev(c, st)
                d = 1 if "++" in op else -1
                na = _clamp((a[0] + d, a[1] + d), s.get("ty"))
                if s.get("k") == "Ref" and s.get("rk") in ("l", "p"):
                    st[s["ref"]] = na
                return na if op.startswith("pre") else a
            return type_range(ty)
        if k == "Assign":
            v = self.ev(n["ch"][1], st)
            s = strip_casts(n["ch"][0])
            if s.get("k") == "Ref" and s.get("rk") in ("l", "p"):
                v = _clamp(v, s.get("ty"))
                st[s["ref"]] = v
            return v
        if k == "CAssign":
            s = strip_casts(n["ch"][0])
            self.ev(n["ch"][1], st)
            if s.get("k") == "Ref" and s.get("rk") in ("l", "p"):
                st[s["ref"]] = type_range(s.get("ty"))
            return type_range(ty)
        if k == "Cond":
            self.ev(n["ch"][0], st)
            return _hull(self.ev(n["ch"][1], st), self.ev(n["ch"][2], st)) or type_range(ty)
        for c in n.get("ch", []) or []:
            if c is not None and isinstance(c, dict):
                self.ev(c, st)
        return type_range(ty)

    @staticmethod
    def _cmp(op: str, a: Itv, b: Itv) -> Tuple[bool, bool]:
        """(참 가능, 거짓 가능)"""
        if op == "<":
            return a[0] < b[1], a[1] >= b[0]
        if op == "<=":
            return a[0] <= b[1], a[1] > b[0]
        if op == ">":
            return a[1] > b[0], a[0] <= b[1]
        if op == ">=":
            return a[1] >= b[0], a[0] < b[1]
        if op == "==":
            return not (a[1] < b[0] or b[1] < a[0]), not (a[0] == a[1] == b[0] == b[1])
        if op == "!=":
            return not (a[0] == a[1] == b[0] == b[1]), not (a[1] < b[0] or b[1] < a[0])
        return True, True

    def _refine(self, e: dict, st: State, branch: str) -> Optional[State]:
        """조건 간선에서 'x op 상수' 범위 정제. 불가능한 간선이면 None."""
        x = strip_casts(e)
        if x.get("k") == "Un" and x.get("v") == "!" and x.get("ch"):
            return self._refine(x["ch"][0], st, "F" if branch == "T" else "T")
        if x.get("k") != "Bin" or x.get("v") not in ("<", "<=", ">", ">=", "==", "!="):
            if x.get("k") == "Ref" and x.get("rk") in ("l", "p"):
                cur = st.get(x["ref"], type_range(x.get("ty")))
                if branch == "F":
                    if cur[0] > 0 or cur[1] < 0:
                        return None
                    out = dict(st)
                    out[x["ref"]] = (0, 0)
                    return out
                if cur == (0, 0):
                    return None
            return st
        op = x["v"]
        l = strip_casts(x["ch"][0])
        a, b = self.ev(x["ch"][0], dict(st)), self.ev(x["ch"][1], dict(st))
        t, f = self._cmp(op, a, b)
        if (branch == "T" and not t) or (branch == "F" and not f):
            return None
        if branch == "F":
            op = {"<": ">=", "<=": ">", ">": "<=", ">=": "<", "==": "!=", "!=": "=="}[op]
        out = dict(st)
        if l.get("k") == "Ref" and l.get("rk") in ("l", "p") and b[0] == b[1]:
            c = b[0]
            lo, hi = out.get(l["ref"], type_range(l.get("ty")))
            nlo, nhi = {"<": (lo, min(hi, c - 1)), "<=": (lo, min(hi, c)), ">": (max(lo, c + 1), hi),
                        ">=": (max(lo, c), hi), "==": (max(lo, c), min(hi, c)), "!=": (lo, hi)}[op]
            if nlo > nhi:
                return None
            out[l["ref"]] = (nlo, nhi)
        return out

    # ---- 고정점 ----
    def _transfer(self, nid: str, st: State) -> State:
        node = self.cfg.nodes[nid]
        st = dict(st)
        a = node.ast
        if node.kind == "formal_in":
            p = node.extra["param"]
            st[p["key"]] = self.param_domains.get(p["name"], type_range(p.get("ty")))
        elif node.kind == "decl" and a is not None:
            ty = a.get("ty") or {}
            if a.get("ch") and ty.get("k") in ("i", "b"):
                st[a["ref"]] = _clamp(self.ev(a["ch"][0], st), ty)
            elif ty.get("k") in ("i", "b"):
                st[a["ref"]] = type_range(ty)
        elif node.kind in ("stmt", "return") and a is not None:
            for c in a.get("ch", []) or []:
                self.ev(c, st)
        elif node.kind == "cond" and a is not None:
            self.ev(a, st)
        return st

    def run(self) -> Dict[str, dict]:
        cfg = self.cfg
        order = cfg.rpo()
        pos = {n: i for i, n in enumerate(order)}
        heads = {b for a in cfg.nodes for b, _ in cfg.succ.get(a, []) if pos.get(b, 0) <= pos.get(a, 0)
                 and (a, b) not in cfg.fake_edges}
        out: Dict[str, Optional[State]] = {}
        inn: Dict[str, Optional[State]] = {cfg.entry: {}}
        visits: Dict[str, int] = {}

        def edge_state(a: str, b: str, lab: str) -> Optional[State]:
            st = out.get(a)
            if st is None:
                return None
            node = cfg.nodes[a]
            if node.kind == "cond" and lab in ("T", "F") and node.ast is not None and not node.extra.get("switch"):
                return self._refine(node.ast, st, lab)
            return st

        def join(sts):
            res: Optional[State] = None
            for s in sts:
                if s is None:
                    continue
                if res is None:
                    res = dict(s)
                else:
                    for k in set(res) | set(s):
                        if k in res and k in s:
                            res[k] = _hull(res[k], s[k])
                        else:
                            res.pop(k, None)     # 한쪽 경로에서만 알려진 값 -> top
            return res

        for phase in ("widen", "narrow"):
            changed = True
            rounds = 0
            while changed and rounds < 200:
                changed = False
                rounds += 1
                for n in order:
                    if n == cfg.entry:
                        new_in = {}
                    else:
                        new_in = join(edge_state(p, n, lab) for p, lab in cfg.pred.get(n, []) if (p, n) not in cfg.fake_edges)
                    if new_in is None:
                        continue
                    if phase == "widen" and n in heads and inn.get(n) is not None:
                        visits[n] = visits.get(n, 0) + 1
                        if visits[n] > 2:
                            old = inn[n]
                            w = {}
                            for k, v in new_in.items():
                                if k in old:
                                    tr = type_range(self.types.get(k))
                                    lo = old[k][0] if v[0] >= old[k][0] else tr[0]
                                    hi = old[k][1] if v[1] <= old[k][1] else tr[1]
                                    w[k] = (lo, hi)
                            new_in = w
                    if new_in != inn.get(n):
                        inn[n] = new_in
                        changed = True
                    out[n] = self._transfer(n, new_in)
                if phase == "narrow" and rounds >= 2:
                    break
        res = {}
        for n, node in cfg.nodes.items():
            if node.kind != "cond" or node.extra.get("switch"):
                continue
            st = out.get(n)
            if st is None:
                res[n] = {"reachable": False}
                continue
            t = self._refine(node.ast, st, "T") is not None
            f = self._refine(node.ast, st, "F") is not None
            vars_ = {}
            for x in _refs(node.ast):
                if x.get("rk") in ("l", "p") and x.get("ref") in st:
                    vars_[x.get("v")] = list(st[x["ref"]])
            res[n] = {"reachable": True, "can_true": t, "can_false": f, "vars": vars_}
        return res


def _refs(n: dict):
    stack = [n]
    while stack:
        x = stack.pop()
        if x.get("k") == "Ref":
            yield x
        stack.extend(c for c in (x.get("ch") or []) if c is not None)


def interval_report(g: AnalysisGraph) -> dict:
    """모든 함수의 조건 노드 범위 정보와 정적으로 결정되는(죽은) 분기."""
    conds: Dict[str, dict] = {}
    for f in g.nodes_of(NK.FUNCTION):
        ast = g.asts.get(f)
        if not ast or not ast.get("ch"):
            continue
        try:
            conds.update(IntervalAnalysis(f, ast).run())
        except (RecursionError, KeyError, IndexError, TypeError):
            continue
    dead = []
    for n, info in conds.items():
        if info.get("reachable") and (not info["can_true"] or not info["can_false"]) and g.has_node(n):
            d = g.node(n)
            if d.get("loop") is not None:
                continue
            dead.append({"node": n, "text": d.get("text"), "file": d.get("file"), "line": d.get("line"),
                         "always": "참" if not info["can_false"] else "거짓", "vars": info["vars"]})
    return {"conditions": conds, "static_branches": dead}
