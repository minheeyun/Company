"""SDG 기반 2단계(Horwitz-Reps-Binkley) 슬라이싱.

전방 슬라이스(변경점 -> 영향)
  1단계: 피호출 함수로 '내려가지' 않는다(CALL, PARAM_IN 제외). SUMMARY로 호출을 건너뛰고
         PARAM_OUT으로 호출자에게 '올라간다'.
  2단계: 올라가지 않는다(PARAM_OUT 제외). CALL/PARAM_IN으로 내려간다.
  -> 서로 다른 호출의 진입·복귀 경로가 섞인 '실현 불가능한 경로'를 배제한다.
후방 슬라이스(기능 출력 -> 기여 코드)는 방향만 반대로 동일하게 적용한다.

전역 상태·신호 간선(GLOBAL_*, SIGNAL_*)으로 도착한 노드는 임의의 실행 문맥이므로
1단계 상태로 다시 시작한다(태스크·ISR 사이 공유 상태 전달).

결과에는 각 노드에 처음 도달한 간선(부모)을 기록해 근거 경로를 재구성할 수 있다.
"""
from __future__ import annotations

from collections import deque
from typing import Dict, FrozenSet, Iterable, List, Optional, Set, Tuple

from .model import ASCEND_EDGES, DEPENDENCE_EDGES, DESCEND_EDGES, EK, AnalysisGraph

# 전역 상태·신호·간섭 간선으로 도착하면 임의의 실행 문맥(1단계)에서 다시 시작
_FWD_RESET = frozenset({EK.GLOBAL_READ, EK.SIGNAL_READ, EK.CALIBRATES, "interference"})
_BWD_RESET = frozenset({EK.GLOBAL_WRITE, EK.SIGNAL_WRITE, EK.CALIBRATES, "interference"})

Parent = Tuple[str, str, str, int]   # (이전 노드, 간선 종류, 라벨, 이전 단계)


class SliceResult:
    def __init__(self, direction: str, seeds: List[str]):
        self.direction = direction
        self.seeds = seeds
        self.phase: Dict[str, int] = {}
        self.parent: Dict[Tuple[str, int], Optional[Parent]] = {}

    @property
    def nodes(self) -> Set[str]:
        return set(self.phase)

    def __contains__(self, n: str) -> bool:
        return n in self.phase

    def __len__(self) -> int:
        return len(self.phase)

    def path_to(self, node: str) -> List[Tuple[str, str, str]]:
        """실행 방향 근거 경로 [(노드, 이전 노드에서 들어온 간선 종류, 라벨)].
        전방: seed -> node,  후방: node -> 기준점(기능 출력)."""
        if node not in self.phase:
            return []
        chain: List[Tuple[str, str, str]] = []
        st: Optional[Tuple[str, int]] = (node, self.phase[node])
        guard = 0
        while st is not None and guard < 10000:
            guard += 1
            p = self.parent.get(st)
            if p is None:
                chain.append((st[0], "", ""))
                break
            chain.append((st[0], p[1], p[2]))
            st = (p[0], p[3])
        if self.direction == "forward":
            return list(reversed(chain))
        out = [(chain[0][0], "", "")]
        for i in range(1, len(chain)):
            out.append((chain[i][0], chain[i - 1][1], chain[i - 1][2]))
        return out


def _run(g: AnalysisGraph, seeds: Iterable[str], forward: bool, kinds: FrozenSet[str],
         restrict: Optional[Set[str]] = None, max_nodes: int = 2_000_000) -> SliceResult:
    res = SliceResult("forward" if forward else "backward", list(seeds))
    q: deque = deque()

    def push(n: str, ph: int, parent: Optional[Parent]) -> None:
        if restrict is not None and n not in restrict:
            return
        cur = res.phase.get(n)
        if cur is not None and cur <= ph:
            return
        res.phase[n] = ph
        res.parent[(n, ph)] = parent
        q.append((n, ph))

    for s in seeds:
        if g.has_node(s):
            push(s, 1, None)
    descend, ascend = DESCEND_EDGES, ASCEND_EDGES
    reset = _FWD_RESET if forward else _BWD_RESET
    while q and len(res.phase) < max_nodes:
        n, ph = q.popleft()
        if res.phase.get(n) != ph:
            continue
        edges = g.out_edges(n, kinds) if forward else g.in_edges(n, kinds)
        for s, d, data in edges:
            k = data["kind"]
            other = d if forward else s
            par = (n, k, data.get("label", ""), ph)
            if forward:
                if ph == 1:
                    nph = 2 if k in descend else 1
                else:
                    if k in ascend:
                        continue
                    nph = 1 if k in reset else 2
            else:
                if ph == 1:
                    nph = 2 if k in ascend else 1
                else:
                    if k in descend:
                        continue
                    nph = 1 if k in reset else 2
            push(other, nph, par)
    return res


def forward_slice(g: AnalysisGraph, seeds: Iterable[str], kinds: FrozenSet[str] = DEPENDENCE_EDGES,
                  restrict: Optional[Set[str]] = None) -> SliceResult:
    return _run(g, seeds, True, kinds, restrict)


def backward_slice(g: AnalysisGraph, criteria: Iterable[str], kinds: FrozenSet[str] = DEPENDENCE_EDGES,
                   restrict: Optional[Set[str]] = None) -> SliceResult:
    return _run(g, criteria, False, kinds, restrict)


# ---------------------------------------------------------------------------
# 문맥 민감 chopping (Reps & Rosay, "Precise Interprocedural Chopping", FSE'95 방식)
#
# 전방∩후방 슬라이스(단순 교집합)는 함수 경계를 넘으면 "호출 A로 들어가 호출 B로 나오는"
# 실현 불가능한 경로의 노드까지 포함한다. 여기서는
#   (1) 실현 가능 경로 = '복귀(올라가기)* 다음 호출(내려가기)*' 이고, 짝이 맞는 호출은
#       SUMMARY 간선으로 건너뛰는 2단계 자동자(product automaton)로 표현하고
#   (2) 시작점에서의 전방 도달 상태 ∩ 목표점으로의 역방향 도달 상태 = chop 상태
#   (3) chop 안의 SUMMARY 간선(짝이 맞는 호출)은 피호출 함수 안의 same-level chop
#       (formal-in -> formal-out)으로 재귀 확장(메모이즈)
# 전역 상태·신호 간선으로 넘어가면 임의 문맥에서 다시 시작(1단계)한다.
# ---------------------------------------------------------------------------
EDGE_COST = {
    EK.DATA: 1.0, EK.PARAM_IN: 0.5, EK.PARAM_OUT: 0.5, EK.SUMMARY: 1.0, EK.CALL: 1.5, EK.CONTROL: 1.5,
    EK.GLOBAL_WRITE: 0.5, EK.GLOBAL_READ: 1.5, EK.SIGNAL_WRITE: 0.5, EK.SIGNAL_READ: 1.5,
    EK.CALIBRATES: 1.0, "interference": 2.0,
}
_INTRA = frozenset({EK.DATA, EK.CONTROL, EK.SUMMARY})


def _fwd_next(ph: int, k: str) -> Optional[int]:
    if ph == 1:
        if k in _FWD_RESET:
            return 1
        return 2 if k in DESCEND_EDGES else 1
    if k in ASCEND_EDGES:
        return None
    return 1 if k in _FWD_RESET else 2


def _bwd_prev(ph_dst: int, k: str) -> List[int]:
    """전방 전이 (s,ph)->(d,ph_dst)가 가능한 ph 목록."""
    out = []
    if ph_dst == 2:
        if k in DESCEND_EDGES:
            out.append(1)
        if k not in ASCEND_EDGES and k not in _FWD_RESET:
            out.append(2)
    else:
        if k not in DESCEND_EDGES:
            out.append(1)
        if k in _FWD_RESET:
            out.append(2)
    return out


class ChopResult:
    def __init__(self, sources: List[str], targets: List[str]):
        self.sources = sources
        self.targets = targets
        self.states: Set[Tuple[str, int]] = set()
        self.interior: Set[str] = set()
        self.dist: Dict[Tuple[str, int], float] = {}
        self.parent: Dict[Tuple[str, int], Optional[Tuple[Tuple[str, int], str, str]]] = {}
        self.hops: Dict[Tuple[str, int], int] = {}
        self.npaths: Dict[Tuple[str, int], int] = {}

    @property
    def nodes(self) -> Set[str]:
        return {n for n, _ in self.states} | self.interior

    def reached_targets(self) -> List[str]:
        return sorted({n for n, _ in self.states if n in set(self.targets)})

    def best_state(self, node: str) -> Optional[Tuple[str, int]]:
        cands = [(self.dist[s], s) for s in ((node, 1), (node, 2)) if s in self.dist]
        return min(cands)[1] if cands else None

    def path_to(self, node: str) -> List[Tuple[str, str, str]]:
        st = self.best_state(node)
        if st is None:
            return []
        chain = []
        guard = 0
        while st is not None and guard < 10000:
            guard += 1
            p = self.parent.get(st)
            if p is None:
                chain.append((st[0], "", ""))
                break
            chain.append((st[0], p[1], p[2]))
            st = p[0]
        return list(reversed(chain))

    def distance(self, node: str) -> Optional[float]:
        st = self.best_state(node)
        return self.dist.get(st) if st else None

    def path_count(self, node: str) -> int:
        """홉 수 기준 최단 경로 개수(상한 10^6)."""
        best, total = None, 0
        for st in ((node, 1), (node, 2)):
            if st in self.hops:
                h = self.hops[st]
                if best is None or h < best:
                    best, total = h, self.npaths.get(st, 0)
                elif h == best:
                    total += self.npaths.get(st, 0)
        return min(total, 10 ** 6)


def _same_level_chop(g: AnalysisGraph, func: str, src: str, dst: str, memo: dict, depth: int = 0) -> Set[str]:
    key = (src, dst)
    if key in memo:
        return memo[key]
    memo[key] = set()
    if depth > 50 or not g.has_node(src) or not g.has_node(dst):
        return set()
    fw = {src}
    q = deque([src])
    while q:
        n = q.popleft()
        for _, d, _ in g.out_edges(n, _INTRA):
            if d not in fw and g.func_of(d) == func:
                fw.add(d)
                q.append(d)
    if dst not in fw:
        return set()
    bw = {dst}
    q = deque([dst])
    while q:
        n = q.popleft()
        for s, _, _ in g.in_edges(n, _INTRA):
            if s not in bw and g.func_of(s) == func:
                bw.add(s)
                q.append(s)
    res = fw & bw
    for n in list(res):
        for _, d, data in g.out_edges(n, [EK.SUMMARY]):
            if d in res:
                res |= _expand_summary(g, n, d, data.get("label", ""), memo, depth + 1)
    memo[key] = res
    return res


def _expand_summary(g: AnalysisGraph, ai: str, ao: str, label: str, memo: dict, depth: int) -> Set[str]:
    """짝이 맞는 호출(ai -> ao 요약 간선)의 피호출 함수 내부 노드."""
    if label == "external" or ":in" not in ai:
        return set()
    cs, idx = ai.rsplit(":in", 1)
    out: Set[str] = set()
    if not g.has_node(cs):
        return out
    for c in g.node(cs).get("callees") or []:
        fi = f"{c}#fi{idx}"
        fos = [f"{c}#{label}"] if label in ("fo_ret", "fo_mem") else [f"{c}#fo_ret", f"{c}#fo_mem"]
        for fo in fos:
            inner = _same_level_chop(g, c, fi, fo, memo, depth)
            if inner:
                out |= inner
    return out


def chop(g: AnalysisGraph, sources: Iterable[str], targets: Iterable[str],
         kinds: FrozenSet[str] = DEPENDENCE_EDGES, extra_reset: FrozenSet[str] = frozenset()) -> ChopResult:
    import heapq
    srcs = [s for s in sources if g.has_node(s)]
    tgts = [t for t in targets if g.has_node(t)]
    res = ChopResult(srcs, tgts)
    if not srcs or not tgts:
        return res
    kinds = frozenset(kinds) | extra_reset
    # (1) 전방 도달 상태
    fwd: Set[Tuple[str, int]] = set()
    q = deque((s, 1) for s in srcs)
    fwd.update(q)
    while q:
        n, ph = q.popleft()
        for _, d, data in g.out_edges(n, kinds):
            k = data["kind"]
            nph = 1 if k in extra_reset else _fwd_next(ph, k)
            if nph is not None and (d, nph) not in fwd:
                fwd.add((d, nph))
                q.append((d, nph))
    # (2) 목표 상태로의 역방향 도달
    bwd: Set[Tuple[str, int]] = set()
    q = deque()
    for t in tgts:
        for ph in (1, 2):
            if (t, ph) in fwd:
                bwd.add((t, ph))
                q.append((t, ph))
    while q:
        d, ph = q.popleft()
        for s, _, data in g.in_edges(d, kinds):
            k = data["kind"]
            prevs = [1, 2] if (k in extra_reset and ph == 1) else ([] if k in extra_reset else _bwd_prev(ph, k))
            for pp in prevs:
                st = (s, pp)
                if st in fwd and st not in bwd:
                    bwd.add(st)
                    q.append(st)
    res.states = bwd
    # (3) 짝이 맞는 호출 내부 확장
    memo: dict = {}
    for n, ph in list(bwd):
        for _, d, data in g.out_edges(n, [EK.SUMMARY]):
            if (d, ph) in bwd:
                res.interior |= _expand_summary(g, n, d, data.get("label", ""), memo, 0)
    # (4) chop 상태 안에서 가중 최단 경로(의존 거리)와 홉 기준 최단 경로 수
    dist: Dict[Tuple[str, int], float] = {}
    heap = []
    for s in srcs:
        for ph in (1,):
            if (s, ph) in bwd:
                dist[(s, ph)] = 0.0
                res.parent[(s, ph)] = None
                heapq.heappush(heap, (0.0, s, ph))
    while heap:
        dd, n, ph = heapq.heappop(heap)
        if dd > dist.get((n, ph), float("inf")):
            continue
        for _, d, data in g.out_edges(n, kinds):
            k = data["kind"]
            nph = 1 if k in extra_reset else _fwd_next(ph, k)
            if nph is None or (d, nph) not in bwd:
                continue
            nd = dd + EDGE_COST.get(k, 1.0)
            if nd < dist.get((d, nph), float("inf")):
                dist[(d, nph)] = nd
                res.parent[(d, nph)] = ((n, ph), k, data.get("label", ""))
                heapq.heappush(heap, (nd, d, nph))
    res.dist = dist
    hops: Dict[Tuple[str, int], int] = {}
    cnt: Dict[Tuple[str, int], int] = {}
    q = deque()
    for s in srcs:
        if (s, 1) in bwd:
            hops[(s, 1)] = 0
            cnt[(s, 1)] = 1
            q.append((s, 1))
    while q:
        st = q.popleft()
        n, ph = st
        for _, d, data in g.out_edges(n, kinds):
            k = data["kind"]
            nph = 1 if k in extra_reset else _fwd_next(ph, k)
            if nph is None or (d, nph) not in bwd:
                continue
            ns = (d, nph)
            if ns not in hops:
                hops[ns] = hops[st] + 1
                cnt[ns] = cnt[st]
                q.append(ns)
            elif hops[ns] == hops[st] + 1:
                cnt[ns] = min(10 ** 6, cnt[ns] + cnt[st])
    res.hops = hops
    res.npaths = cnt
    return res


def naive_chop(g: AnalysisGraph, sources: Iterable[str], targets: Iterable[str],
               kinds: FrozenSet[str] = DEPENDENCE_EDGES) -> Set[str]:
    """비교용: 전방 슬라이스 ∩ 후방 슬라이스(함수 경계에서 과대근사)."""
    fs = forward_slice(g, sources, kinds)
    bs = backward_slice(g, targets, kinds)
    return fs.nodes & bs.nodes


def chop_path(g: AnalysisGraph, seeds: Iterable[str], targets: Set[str], allowed: Set[str] = None,
              kinds: FrozenSet[str] = DEPENDENCE_EDGES) -> List[Tuple[str, str, str]]:
    c = chop(g, seeds, targets, kinds)
    best = None
    for t in c.reached_targets():
        p = c.path_to(t)
        if p and (best is None or len(p) < len(best)):
            best = p
    return best or []


def functions_in(g: AnalysisGraph, nodes: Iterable[str]) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for n in nodes:
        f = g.func_of(n)
        if f:
            out[f] = out.get(f, 0) + 1
    return out
