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

_FWD_RESET = frozenset({EK.GLOBAL_READ, EK.SIGNAL_READ, EK.CALIBRATES})
_BWD_RESET = frozenset({EK.GLOBAL_WRITE, EK.SIGNAL_WRITE, EK.CALIBRATES})

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


def chop_path(g: AnalysisGraph, seeds: Iterable[str], targets: Set[str], allowed: Set[str],
              kinds: FrozenSet[str] = DEPENDENCE_EDGES) -> List[Tuple[str, str, str]]:
    """seed에서 target까지, 허용 노드(전방∩후방) 안에서 실현 가능한 최단 경로."""
    res = _run(g, seeds, True, kinds, restrict=allowed | set(seeds))
    best = None
    for t in targets:
        if t in res:
            p = res.path_to(t)
            if best is None or len(p) < len(best):
                best = p
    return best or []


def functions_in(g: AnalysisGraph, nodes: Iterable[str]) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for n in nodes:
        f = g.func_of(n)
        if f:
            out[f] = out.get(f, 0) + 1
    return out
