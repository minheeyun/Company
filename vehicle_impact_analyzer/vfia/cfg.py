"""함수별 CFG 생성, 후지배자(post-dominator), 제어 의존성(CDG).

* 분기·반복·switch·break/continue/goto·조기 return을 반영한다.
* 분기 조건의 &&, ||, ! 는 단락 평가 순서대로 조건 노드를 분해한다.
  (오른쪽 피연산자의 호출/부작용이 왼쪽 결과에 제어 의존하게 된다.)
* 종료하지 않는 루프(for(;;))는 루프 헤더에서 EXIT로 가는 가상 간선을 추가해
  후지배자 계산이 가능하게 한다.
* 제어 의존성은 Ferrante-Ottenstein-Warren 방식: 간선 A->B에서 B가 A를 후지배하지
  않으면, B부터 ipdom(A) 직전까지 후지배자 트리를 올라가며 A에 제어 의존한다고 표시.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Dict, List, Optional, Set, Tuple

from .model import walk_ast

_TRANSPARENT = ("ICast",)


def strip_casts(n: dict) -> dict:
    while n is not None and n.get("k") in _TRANSPARENT and n.get("ch"):
        n = n["ch"][0]
    return n


class CFGNode:
    __slots__ = ("id", "kind", "ast", "label", "line", "extra")

    def __init__(self, nid: str, kind: str, ast: Optional[dict] = None, label: str = "", line: int = 0):
        self.id = nid
        self.kind = kind
        self.ast = ast
        self.label = label
        self.line = line
        self.extra: dict = {}

    def __repr__(self) -> str:  # pragma: no cover
        return f"CFGNode({self.id},{self.kind})"


class CFG:
    def __init__(self, unit: str):
        self.unit = unit
        self.nodes: Dict[str, CFGNode] = {}
        self.succ: Dict[str, List[Tuple[str, str]]] = defaultdict(list)
        self.pred: Dict[str, List[Tuple[str, str]]] = defaultdict(list)
        self.entry = f"{unit}#entry"
        self.exit = f"{unit}#exit"
        self.fake_edges: Set[Tuple[str, str]] = set()
        self.unreachable: Set[str] = set()
        self.anchor_of_ast: Dict[int, str] = {}   # AST id -> CFG node id

    def add(self, node: CFGNode) -> str:
        self.nodes[node.id] = node
        return node.id

    def edge(self, a: str, b: str, label: str = "") -> None:
        if (b, label) not in self.succ[a]:
            self.succ[a].append((b, label))
            self.pred[b].append((a, label))

    def remove_edge(self, a: str, b: str) -> None:
        self.succ[a] = [(x, l) for x, l in self.succ[a] if x != b]
        self.pred[b] = [(x, l) for x, l in self.pred[b] if x != a]

    def succ_ids(self, n: str) -> List[str]:
        return [b for b, _ in self.succ.get(n, [])]

    def pred_ids(self, n: str) -> List[str]:
        return [a for a, _ in self.pred.get(n, [])]

    def rpo(self) -> List[str]:
        seen: Set[str] = set()
        order: List[str] = []
        stack = [(self.entry, iter(self.succ_ids(self.entry)))]
        seen.add(self.entry)
        while stack:
            n, it = stack[-1]
            nxt = next(it, None)
            if nxt is None:
                stack.pop()
                order.append(n)
                continue
            if nxt not in seen:
                seen.add(nxt)
                stack.append((nxt, iter(self.succ_ids(nxt))))
        order.reverse()
        rest = [n for n in self.nodes if n not in seen]
        return order + rest


class _Ctx:
    def __init__(self):
        self.brk: List[str] = []
        self.cont: List[str] = []
        self.switch_cases: List[List[Tuple[str, Optional[object]]]] = []


class CFGBuilder:
    """역방향(후속 노드를 먼저 아는) 재귀 구성. 루프는 자리표시자 노드로 되돌림 간선을 연결한다."""

    def __init__(self, unit: str, func_ast: dict):
        self.unit = unit
        self.f = func_ast
        self.cfg = CFG(unit)
        self.labels: Dict[str, str] = {}
        self.gotos: List[Tuple[str, str]] = []
        self._ph = 0
        self.ctx = _Ctx()

    def nid(self, ast: dict, suffix: str = "") -> str:
        return f"{self.unit}#{ast['id']}{suffix}"

    def _placeholder(self) -> str:
        self._ph += 1
        pid = f"{self.unit}#ph{self._ph}"
        self.cfg.add(CFGNode(pid, "placeholder"))
        return pid

    def _redirect(self, ph: str, real: str) -> None:
        for a, l in list(self.cfg.pred.get(ph, [])):
            self.cfg.remove_edge(a, ph)
            self.cfg.edge(a, real, l)
        self.cfg.nodes.pop(ph, None)
        self.cfg.succ.pop(ph, None)
        self.cfg.pred.pop(ph, None)

    def _node(self, kind: str, ast: dict, suffix: str = "") -> str:
        nid = self.nid(ast, suffix)
        n = CFGNode(nid, kind, ast, line=ast.get("l", 0))
        self.cfg.add(n)
        self.cfg.anchor_of_ast.setdefault(ast["id"], nid)
        return nid

    def build(self) -> CFG:
        cfg = self.cfg
        cfg.add(CFGNode(cfg.entry, "entry", self.f, "ENTRY", self.f.get("l", 0)))
        cfg.add(CFGNode(cfg.exit, "exit", None, "EXIT", self.f.get("el", 0)))
        body = self.f["ch"][0] if self.f.get("ch") else None
        first = self.visit(body, cfg.exit) if body else cfg.exit
        # formal-in 노드: 매개변수 정의 지점
        prev = cfg.entry
        for p in self.f.get("params", []):
            fid = f"{self.unit}#fi{p['index']}"
            n = CFGNode(fid, "formal_in", None, f"param {p['name']}", self.f.get("l", 0))
            n.extra["param"] = p
            cfg.add(n)
            cfg.edge(prev, fid)
            prev = fid
        cfg.edge(prev, first)
        for g, lab in self.gotos:
            tgt = self.labels.get(lab)
            if tgt:
                cfg.edge(g, tgt)
            else:
                cfg.edge(g, cfg.exit)
        self._finish()
        return cfg

    # --- 문장 ---
    def visit(self, s: Optional[dict], succ: str) -> str:
        if s is None:
            return succ
        k = s["k"]
        cfg = self.cfg
        if k == "Compound":
            for c in reversed(s.get("ch", [])):
                succ = self.visit(c, succ)
            return succ
        if k == "DeclStmt":
            for v in reversed(s.get("ch", [])):
                if v.get("static"):
                    continue
                n = self._node("decl", v)
                cfg.edge(n, succ)
                succ = n
            return succ
        if k in ("ExprStmt", "Null", "Asm", "UnknownStmt"):
            n = self._node("stmt" if k != "Asm" else "asm", s)
            cfg.edge(n, succ)
            return n
        if k == "Return":
            n = self._node("return", s)
            cfg.edge(n, cfg.exit)
            return n
        if k == "Break":
            n = self._node("break", s)
            cfg.edge(n, self.ctx.brk[-1] if self.ctx.brk else cfg.exit)
            return n
        if k == "Continue":
            n = self._node("continue", s)
            cfg.edge(n, self.ctx.cont[-1] if self.ctx.cont else cfg.exit)
            return n
        if k == "Goto":
            n = self._node("goto", s)
            self.gotos.append((n, s.get("v")))
            return n
        if k == "Label":
            n = self._node("label", s)
            self.labels[s.get("v")] = n
            sub = self.visit(s["ch"][0], succ) if s.get("ch") else succ
            cfg.edge(n, sub)
            return n
        if k == "If":
            ch = s["ch"]
            t = self.visit(ch[1], succ)
            e = self.visit(ch[2], succ) if len(ch) > 2 else succ
            return self.visit_cond(ch[0], t, e)
        if k == "While":
            ph = self._placeholder()
            self.ctx.brk.append(succ)
            self.ctx.cont.append(ph)
            body = self.visit(s["ch"][1], ph)
            self.ctx.brk.pop()
            self.ctx.cont.pop()
            head = self.visit_cond(s["ch"][0], body, succ, loop=s)
            self._redirect(ph, head)
            return head
        if k == "Do":
            ph = self._placeholder()
            self.ctx.brk.append(succ)
            self.ctx.cont.append(ph)
            body = self.visit(s["ch"][0], ph)
            self.ctx.brk.pop()
            self.ctx.cont.pop()
            head = self.visit_cond(s["ch"][1], body, succ, loop=s)
            self._redirect(ph, head)
            return body
        if k == "For":
            return self._for(s, succ)
        if k == "Switch":
            return self._switch(s, succ)
        if k in ("Case", "Default"):
            n = self._node("case", s)
            if self.ctx.switch_cases:
                self.ctx.switch_cases[-1].append((n, s.get("v") if k == "Case" else "default"))
            sub = self.visit(s["ch"][0], succ) if s.get("ch") else succ
            cfg.edge(n, sub)
            return n
        # 알 수 없는 문장은 단일 노드로 처리
        n = self._node("stmt", s)
        cfg.edge(n, succ)
        return n

    def _for(self, s: dict, succ: str) -> str:
        has = s.get("has", {})
        ch = list(s["ch"])
        init = ch.pop(0) if has.get("init") else None
        cond = ch.pop(0) if has.get("cond") else None
        inc = ch.pop(0) if has.get("inc") else None
        body = ch[0] if ch else None
        ph = self._placeholder()
        inc_first = self.visit(inc, ph) if inc else ph
        self.ctx.brk.append(succ)
        self.ctx.cont.append(inc_first)
        body_first = self.visit(body, inc_first)
        self.ctx.brk.pop()
        self.ctx.cont.pop()
        if cond is not None:
            head = self.visit_cond(cond, body_first, succ, loop=s)
        else:
            head = self._node("loophead", s, "lh")
            self.cfg.nodes[head].label = "for(;;)"
            self.cfg.edge(head, body_first, "T")
        self._redirect(ph, head)
        return self.visit(init, head) if init else head

    def _switch(self, s: dict, succ: str) -> str:
        self.ctx.brk.append(succ)
        self.ctx.switch_cases.append([])
        body_first = self.visit(s["ch"][1], succ)
        cases = self.ctx.switch_cases.pop()
        self.ctx.brk.pop()
        c = self._node("cond", s["ch"][0])
        self.cfg.nodes[c].extra["switch"] = True
        has_default = False
        for nid, v in cases:
            if v == "default":
                has_default = True
                self.cfg.edge(c, nid, "default")
            else:
                self.cfg.edge(c, nid, f"case {v}")
        if not has_default:
            self.cfg.edge(c, succ, "default")
        if not cases:
            self.cfg.edge(c, body_first, "")
        return c

    def visit_cond(self, e: dict, t: str, f: str, loop: Optional[dict] = None) -> str:
        """단락 평가 분해."""
        x = strip_casts(e)
        if x.get("k") == "Bin" and x.get("v") == "&&":
            r = self.visit_cond(x["ch"][1], t, f)
            return self.visit_cond(x["ch"][0], r, f)
        if x.get("k") == "Bin" and x.get("v") == "||":
            r = self.visit_cond(x["ch"][1], t, f)
            return self.visit_cond(x["ch"][0], t, r)
        if x.get("k") == "Un" and x.get("v") == "!" and x.get("ch"):
            inner = strip_casts(x["ch"][0])
            if inner.get("k") in ("Bin",) and inner.get("v") in ("&&", "||") or (
                    inner.get("k") == "Un" and inner.get("v") == "!"):
                return self.visit_cond(inner, f, t)
        n = self._node("cond", e)
        if loop is not None:
            self.cfg.nodes[n].extra["loop"] = loop["id"]
        self.cfg.edge(n, t, "T")
        self.cfg.edge(n, f, "F")
        return n

    def _finish(self) -> None:
        cfg = self.cfg
        # 도달 불가능 노드
        reach: Set[str] = set()
        stack = [cfg.entry]
        while stack:
            n = stack.pop()
            if n in reach:
                continue
            reach.add(n)
            stack.extend(cfg.succ_ids(n))
        cfg.unreachable = {n for n in cfg.nodes if n not in reach}
        # EXIT로 가지 못하는 노드(무한 루프)에 가상 간선 추가
        while True:
            to_exit: Set[str] = set()
            stack = [cfg.exit]
            while stack:
                n = stack.pop()
                if n in to_exit:
                    continue
                to_exit.add(n)
                stack.extend(cfg.pred_ids(n))
            missing = [n for n in cfg.rpo() if n not in to_exit]
            if not missing:
                break
            n = missing[0]
            cfg.edge(n, cfg.exit, "fake")
            cfg.fake_edges.add((n, cfg.exit))
        # CDG 계산을 위한 ENTRY->EXIT 가상 간선
        cfg.edge(cfg.entry, cfg.exit, "fake")
        cfg.fake_edges.add((cfg.entry, cfg.exit))


def build_cfg(unit: str, func_ast: dict) -> CFG:
    return CFGBuilder(unit, func_ast).build()


# ---------------------------------------------------------------------------
def postdominators(cfg: CFG) -> Dict[str, Optional[str]]:
    """Cooper-Harvey-Kennedy 반복 알고리즘(역 CFG)으로 즉시 후지배자 계산."""
    # 역 CFG의 RPO = EXIT에서 pred 방향 DFS 후위의 역순
    order: List[str] = []
    seen: Set[str] = {cfg.exit}
    stack = [(cfg.exit, iter(cfg.pred_ids(cfg.exit)))]
    while stack:
        n, it = stack[-1]
        nxt = next(it, None)
        if nxt is None:
            stack.pop()
            order.append(n)
            continue
        if nxt not in seen:
            seen.add(nxt)
            stack.append((nxt, iter(cfg.pred_ids(nxt))))
    order.reverse()
    index = {n: i for i, n in enumerate(order)}
    ipdom: Dict[str, Optional[str]] = {n: None for n in order}
    ipdom[cfg.exit] = cfg.exit

    def intersect(a: str, b: str) -> str:
        while a != b:
            while index[a] > index[b]:
                a = ipdom[a]  # type: ignore
            while index[b] > index[a]:
                b = ipdom[b]  # type: ignore
        return a

    changed = True
    while changed:
        changed = False
        for n in order:
            if n == cfg.exit:
                continue
            new = None
            for s in cfg.succ_ids(n):
                if s in index and ipdom.get(s) is not None:
                    new = s if new is None else intersect(s, new)
            if new is not None and ipdom[n] != new:
                ipdom[n] = new
                changed = True
    ipdom[cfg.exit] = None
    return ipdom


def control_dependence(cfg: CFG, ipdom: Optional[Dict[str, Optional[str]]] = None
                       ) -> Dict[str, Set[Tuple[str, str]]]:
    """node -> {(조건 노드, 분기 라벨)}"""
    ipdom = ipdom if ipdom is not None else postdominators(cfg)
    cd: Dict[str, Set[Tuple[str, str]]] = defaultdict(set)
    for a in list(cfg.nodes):
        succs = cfg.succ.get(a, [])
        if len(succs) < 2:
            continue
        stop = ipdom.get(a)
        for b, label in succs:
            runner: Optional[str] = b
            guard = 0
            while runner is not None and runner != stop and guard < 100000:
                if runner != cfg.exit:
                    cd[runner].add((a, label))
                runner = ipdom.get(runner)
                guard += 1
    return cd


def annotate_anchors(func_ast: dict, cfg: CFG) -> None:
    """AST 노드마다 그 노드를 포함하는 가장 안쪽 CFG 노드 ID('a')를 기록한다."""
    def rec(n: dict, cur: Optional[str]) -> None:
        a = cfg.anchor_of_ast.get(n["id"])
        if a is not None and n.get("k") not in ("Case", "Default", "Label"):
            cur = a
        if cur is not None:
            n["a"] = cur
        elif "a" in n:
            del n["a"]
        if n.get("k") in ("Case", "Default", "Label"):
            n["a"] = a or cur
            for c in n.get("ch", []) or []:
                rec(c, None)
            return
        for c in n.get("ch", []) or []:
            if c is not None:
                rec(c, cur if n.get("k") not in ("Compound", "If", "While", "Do", "For", "Switch") else None)
    for c in func_ast.get("ch", []):
        rec(c, None)
    # 구조 노드(If/While 등)는 조건 노드를 대표 앵커로 사용
    for n in walk_ast(func_ast):
        if n.get("k") in ("If", "While", "Do", "Switch") and "a" not in n:
            cond = n["ch"][0] if n["k"] != "Do" else n["ch"][1]
            a = _first_anchor(cond)
            if a:
                n["a"] = a


def _first_anchor(n: dict) -> Optional[str]:
    for x in walk_ast(n):
        if "a" in x:
            return x["a"]
    return None


def ntscd(cfg: CFG) -> Dict[str, Set[Tuple[str, str]]]:
    """비종료 민감 제어 의존(Non-Termination Sensitive Control Dependence, Ranganath et al. 2007).

    n이 m에 NTSCD 의존 ⇔ m의 후속 s1에서 시작하는 모든 최대 경로(무한 경로 포함)가 n을 지나고,
    다른 후속 s2에서는 n을 지나지 않는 최대 경로가 있다.
    후지배자 기반 정의는 종료점이 하나라는 전제가 있어, 'while(!ready){}' 같은 대기 루프나
    for(;;) 태스크 본체가 있으면 루프 뒤 문장의 제어 의존이 빠진다. 이 정의는 가상 종료 간선 없이
    실제 CFG에서 계산한다.
    """
    nodes = [n for n, x in cfg.nodes.items() if x.kind != "placeholder"]
    succ: Dict[str, List[Tuple[str, str]]] = {}
    for n in nodes:
        seen = {}
        for b, lab in cfg.succ.get(n, []):
            if (n, b) in cfg.fake_edges:
                continue
            seen.setdefault(b, lab)
        succ[n] = list(seen.items())
    preds: Dict[str, Set[str]] = defaultdict(set)
    for n, ss in succ.items():
        for b, _ in ss:
            preds[b].add(n)
    branches = [m for m in nodes if len(succ[m]) >= 2]
    cd: Dict[str, Set[Tuple[str, str]]] = defaultdict(set)
    if not branches:
        return cd
    for n in nodes:
        # MR(n): n을 반드시 지나는(must-reach) 노드 집합 — 최소 고정점
        remaining = {x: len(succ[x]) for x in nodes}
        mr = {n}
        stack = [n]
        while stack:
            x = stack.pop()
            for p in preds.get(x, ()):
                if p in mr:
                    continue
                remaining[p] -= 1
                if remaining[p] == 0:
                    mr.add(p)
                    stack.append(p)
        for m in branches:
            ins = [(b, lab) for b, lab in succ[m] if b in mr]
            if ins and len(ins) < len(succ[m]):
                for b, lab in ins:
                    cd[n].add((m, lab))
    return cd
