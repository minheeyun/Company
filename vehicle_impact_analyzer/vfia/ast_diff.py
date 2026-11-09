"""GumTree 계열 AST 매칭과 편집 스크립트.

Falleri et al., "Fine-grained and Accurate Source Code Differencing"(ASE 2014)의 구조를 따른다.
  1. Top-down: 높이 큰 순으로 동형(같은 해시) 부분 트리를 대응. 후보가 여럿이면 부모 유사도로 선택
  2. Bottom-up: 대응된 자손 비율(dice)이 높은 같은 종류 컨테이너를 대응, 이후 자식 복구(LCS)
  3. 편집 스크립트: update / insert / delete / move(부모 변경, 형제 순서 변경)

줄 번호가 아니라 문법 구조로 대응하므로 코드 삽입 이후의 위치 어긋남에 영향받지 않는다.
주의: 대응 결과는 문법상의 변경이며, 변경된 코드의 동작 동일성을 증명하지 않는다.
"""
from __future__ import annotations

import hashlib
import heapq
from collections import Counter
from typing import Dict, List, Optional, Set, Tuple


def node_label(n: dict) -> str:
    k = n.get("k")
    v = n.get("v")
    if k in ("ICast", "Cast"):
        return (n.get("ty") or {}).get("cn", "")
    if k == "Var":
        return f"{v}:{(n.get('ty') or {}).get('cn', '')}"
    if k == "Field":
        return f"{v}:{(n.get('ty') or {}).get('cn', '')}"
    if k == "Typedef":
        return f"{v}:{(n.get('ty') or {}).get('cn', '')}"
    if k == "EnumConst":
        return f"{v}={n.get('val')}"
    if k == "Macro":
        return f"{v}={n.get('body')}"
    if k == "GlobalVar":
        return f"{v}:{(n.get('ty') or {}).get('cn', '')}"
    if v is None:
        return ""
    return str(v)


class Tree:
    def __init__(self, root: dict):
        self.root = root
        self.nodes: Dict[int, dict] = {}
        self.parent: Dict[int, Optional[int]] = {}
        self.children: Dict[int, List[int]] = {}
        self.height: Dict[int, int] = {}
        self.size: Dict[int, int] = {}
        self.hash: Dict[int, str] = {}
        self.desc: Dict[int, Set[int]] = {}
        self.depth: Dict[int, int] = {}
        self.order: List[int] = []      # 후위 순회
        self._build(root, None, 0)

    def _build(self, n: dict, parent: Optional[int], depth: int) -> None:
        stack = [(n, parent, depth, False)]
        while stack:
            x, p, d, done = stack.pop()
            i = x["id"]
            if not done:
                self.nodes[i] = x
                self.parent[i] = p
                self.depth[i] = d
                ch = [c for c in (x.get("ch") or []) if c is not None]
                self.children[i] = [c["id"] for c in ch]
                stack.append((x, p, d, True))
                for c in reversed(ch):
                    stack.append((c, i, d + 1, False))
            else:
                ch = self.children[i]
                self.height[i] = 1 + max((self.height[c] for c in ch), default=0)
                self.size[i] = 1 + sum(self.size[c] for c in ch)
                h = hashlib.md5()
                h.update(f"{x.get('k')}|{node_label(x)}|".encode())
                for c in ch:
                    h.update(self.hash[c].encode())
                self.hash[i] = h.hexdigest()
                ds: Set[int] = set()
                for c in ch:
                    ds.add(c)
                    ds |= self.desc[c]
                self.desc[i] = ds
                self.order.append(i)

    def kind(self, i: int) -> str:
        return self.nodes[i].get("k")

    def bag(self, i: int) -> Counter:
        """부분 트리의 (종류:라벨) 다중집합 — 위치와 무관한 내용 유사도 계산용."""
        cache = self.__dict__.setdefault("_bag", {})
        if i not in cache:
            c = Counter()
            for x in [i] + list(self.desc[i]):
                c[f"{self.kind(x)}:{self.label(x)}"] += 1
            cache[i] = c
        return cache[i]

    def label(self, i: int) -> str:
        return node_label(self.nodes[i])


class Mapping:
    def __init__(self):
        self.a2b: Dict[int, int] = {}
        self.b2a: Dict[int, int] = {}

    def add(self, a: int, b: int) -> None:
        self.a2b[a] = b
        self.b2a[b] = a

    def has_a(self, a: int) -> bool:
        return a in self.a2b

    def has_b(self, b: int) -> bool:
        return b in self.b2a


def _dice(t1: Tree, t2: Tree, m: Mapping, a: int, b: int) -> float:
    da, db = t1.desc[a], t2.desc[b]
    if not da and not db:
        return 1.0 if t1.label(a) == t2.label(b) else 0.5
    common = sum(1 for x in da if m.a2b.get(x) in db)
    return 2.0 * common / (len(da) + len(db)) if (da or db) else 0.0


def bag_sim(t1: Tree, t2: Tree, a: int, b: int) -> float:
    x, y = t1.bag(a), t2.bag(b)
    inter = sum((x & y).values())
    union = sum((x | y).values())
    return inter / union if union else 0.0


def _map_iso(t1: Tree, t2: Tree, m: Mapping, a: int, b: int) -> None:
    stack = [(a, b)]
    while stack:
        x, y = stack.pop()
        if m.has_a(x) or m.has_b(y):
            continue
        m.add(x, y)
        for cx, cy in zip(t1.children[x], t2.children[y]):
            stack.append((cx, cy))


def match(t1: Tree, t2: Tree, min_height: int = 2, sim: float = 0.5) -> Mapping:
    m = Mapping()
    # ---- 1. top-down ----
    l1: List[Tuple[int, int, int]] = []
    l2: List[Tuple[int, int, int]] = []
    heapq.heappush(l1, (-t1.height[t1.root["id"]], 0, t1.root["id"]))
    heapq.heappush(l2, (-t2.height[t2.root["id"]], 0, t2.root["id"]))
    cnt = [1]
    candidates: List[Tuple[int, int]] = []

    def push(l, t, i):
        cnt[0] += 1
        heapq.heappush(l, (-t.height[i], cnt[0], i))

    def pop_height(l, t) -> List[int]:
        h = -l[0][0]
        out = []
        while l and -l[0][0] == h:
            out.append(heapq.heappop(l)[2])
        return out

    while l1 and l2:
        h1, h2 = -l1[0][0], -l2[0][0]
        if max(h1, h2) < min_height:
            break
        if h1 != h2:
            if h1 > h2:
                for i in pop_height(l1, t1):
                    for c in t1.children[i]:
                        push(l1, t1, c)
            else:
                for i in pop_height(l2, t2):
                    for c in t2.children[i]:
                        push(l2, t2, c)
            continue
        H1 = pop_height(l1, t1)
        H2 = pop_height(l2, t2)
        by_hash2: Dict[str, List[int]] = {}
        for b in H2:
            by_hash2.setdefault(t2.hash[b], []).append(b)
        by_hash1: Dict[str, List[int]] = {}
        for a in H1:
            by_hash1.setdefault(t1.hash[a], []).append(a)
        matched1, matched2 = set(), set()
        for hsh, as_ in by_hash1.items():
            bs = by_hash2.get(hsh, [])
            if not bs:
                continue
            if len(as_) == 1 and len(bs) == 1:
                _map_iso(t1, t2, m, as_[0], bs[0])
            else:
                for a in as_:
                    for b in bs:
                        candidates.append((a, b))
            matched1 |= set(as_)
            matched2 |= set(bs)
        for a in H1:
            if a not in matched1:
                for c in t1.children[a]:
                    push(l1, t1, c)
        for b in H2:
            if b not in matched2:
                for c in t2.children[b]:
                    push(l2, t2, c)

    def parent_sim(a: int, b: int) -> float:
        pa, pb = t1.parent.get(a), t2.parent.get(b)
        if pa is None or pb is None:
            return 0.0
        return _dice(t1, t2, m, pa, pb)

    def pos_sim(a: int, b: int) -> float:
        pa, pb = t1.parent.get(a), t2.parent.get(b)
        ia = t1.children[pa].index(a) if pa is not None else 0
        ib = t2.children[pb].index(b) if pb is not None else 0
        return -abs(ia - ib) - 0.01 * abs(t1.depth[a] - t2.depth[b])

    candidates.sort(key=lambda ab: (parent_sim(*ab), pos_sim(*ab)), reverse=True)
    for a, b in candidates:
        if not m.has_a(a) and not m.has_b(b):
            _map_iso(t1, t2, m, a, b)

    # ---- 2. bottom-up ----
    r1, r2 = t1.root["id"], t2.root["id"]
    for a in t1.order:
        if m.has_a(a):
            continue
        if a == r1:
            if not m.has_b(r2) and t1.kind(a) == t2.kind(r2):
                m.add(a, r2)
                _recover(t1, t2, m, a, r2)
            continue
        if not any(m.has_a(d) for d in t1.desc[a]):
            continue
        cands = set()
        for d in t1.desc[a]:
            b = m.a2b.get(d)
            while b is not None:
                b = t2.parent.get(b)
                if b is not None and not m.has_b(b) and t2.kind(b) == t1.kind(a):
                    cands.add(b)
        best, best_s = None, -1.0
        for b in cands:
            s = max(_dice(t1, t2, m, a, b), 0.9 * bag_sim(t1, t2, a, b))
            if s > best_s:
                best, best_s = b, s
        if best is not None and best_s >= sim:
            m.add(a, best)
            _recover(t1, t2, m, a, best)
    # 남은 루트 수준 대응
    if not m.has_a(r1) and not m.has_b(r2):
        m.add(r1, r2)
        _recover(t1, t2, m, r1, r2)
    return m


def _lcs(xs: List[int], ys: List[int], eq) -> List[Tuple[int, int]]:
    n, k = len(xs), len(ys)
    dp = [[0] * (k + 1) for _ in range(n + 1)]
    for i in range(n - 1, -1, -1):
        for j in range(k - 1, -1, -1):
            dp[i][j] = dp[i + 1][j + 1] + 1 if eq(xs[i], ys[j]) else max(dp[i + 1][j], dp[i][j + 1])
    out, i, j = [], 0, 0
    while i < n and j < k:
        if eq(xs[i], ys[j]):
            out.append((xs[i], ys[j]))
            i += 1
            j += 1
        elif dp[i + 1][j] >= dp[i][j + 1]:
            i += 1
        else:
            j += 1
    return out


def _recover(t1: Tree, t2: Tree, m: Mapping, a: int, b: int) -> None:
    """대응된 컨테이너의 남은 자식 복구: 동형 -> (종류,라벨) LCS -> 같은 종류 중 내용 유사도 최대."""
    ca = [x for x in t1.children[a] if not m.has_a(x)]
    cb = [y for y in t2.children[b] if not m.has_b(y)]
    if not ca or not cb:
        return
    for eq in (lambda x, y: t1.hash[x] == t2.hash[y],
               lambda x, y: t1.kind(x) == t2.kind(y) and t1.label(x) == t2.label(y)):
        ca = [x for x in ca if not m.has_a(x)]
        cb = [y for y in cb if not m.has_b(y)]
        for x, y in _lcs(ca, cb, eq):
            if not m.has_a(x) and not m.has_b(y):
                if t1.hash[x] == t2.hash[y]:
                    _map_iso(t1, t2, m, x, y)
                else:
                    m.add(x, y)
                    _recover(t1, t2, m, x, y)
    ca = [x for x in ca if not m.has_a(x)]
    cb = [y for y in cb if not m.has_b(y)]
    if not ca or not cb:
        return
    na, nb = max(1, len(t1.children[a])), max(1, len(t2.children[b]))
    scored = []
    for x in ca:
        ix = t1.children[a].index(x) / na
        for y in cb:
            if t1.kind(x) != t2.kind(y):
                continue
            iy = t2.children[b].index(y) / nb
            sc = bag_sim(t1, t2, x, y) + 0.1 * (1 - abs(ix - iy))
            scored.append((sc, x, y))
    scored.sort(reverse=True)
    for sc, x, y in scored:
        if sc < 0.3 and not (len(ca) == 1 and len(cb) == 1):
            break
        if not m.has_a(x) and not m.has_b(y):
            m.add(x, y)
            _recover(t1, t2, m, x, y)


class Action:
    __slots__ = ("op", "a", "b", "detail")

    def __init__(self, op: str, a: Optional[int], b: Optional[int], detail: str = ""):
        self.op = op          # update | insert | delete | move
        self.a = a            # 변경 전 노드 id
        self.b = b            # 변경 후 노드 id
        self.detail = detail

    def __repr__(self) -> str:  # pragma: no cover
        return f"{self.op}({self.a}->{self.b} {self.detail})"


def edit_script(t1: Tree, t2: Tree, m: Mapping) -> List[Action]:
    acts: List[Action] = []
    for a, b in m.a2b.items():
        if t1.kind(a) == t2.kind(b) and t1.label(a) != t2.label(b):
            acts.append(Action("update", a, b, f"{t1.label(a)} -> {t2.label(b)}"))
        elif t1.kind(a) != t2.kind(b):
            acts.append(Action("update", a, b, f"{t1.kind(a)} -> {t2.kind(b)}"))
        pa, pb = t1.parent.get(a), t2.parent.get(b)
        if pa is not None and pb is not None and m.a2b.get(pa) != pb and m.has_a(pa):
            acts.append(Action("move", a, b, "부모 변경"))
    # 형제 순서 변경
    for a, b in m.a2b.items():
        ca = [x for x in t1.children[a] if x in m.a2b and t2.parent.get(m.a2b[x]) == b]
        cb = [y for y in t2.children[b] if y in m.b2a and t1.parent.get(m.b2a[y]) == a]
        if len(ca) < 2:
            continue
        common = _lcs(ca, cb, lambda x, y: m.a2b.get(x) == y)
        in_lcs = {x for x, _ in common}
        for x in ca:
            if x not in in_lcs:
                acts.append(Action("move", x, m.a2b[x], "순서 변경"))
    for b in t2.nodes:
        if not m.has_b(b):
            p = t2.parent.get(b)
            if p is None or m.has_b(p):
                acts.append(Action("insert", None, b, t2.kind(b)))
    for a in t1.nodes:
        if not m.has_a(a):
            p = t1.parent.get(a)
            if p is None or m.has_a(p):
                acts.append(Action("delete", a, None, t1.kind(a)))
    return acts


def diff(root1: dict, root2: dict) -> Tuple[Tree, Tree, Mapping, List[Action]]:
    t1, t2 = Tree(root1), Tree(root2)
    m = match(t1, t2)
    return t1, t2, m, edit_script(t1, t2, m)


def similarity(root1: dict, root2: dict) -> float:
    """두 AST의 구조 유사도(대응 노드 비율). 함수 이름 변경·파일 이동 탐지에 사용."""
    t1, t2 = Tree(root1), Tree(root2)
    m = match(t1, t2)
    return 2.0 * len(m.a2b) / (len(t1.nodes) + len(t2.nodes))
