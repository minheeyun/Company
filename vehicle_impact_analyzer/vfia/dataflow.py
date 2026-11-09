"""데이터흐름 분석: worklist 기반 Reaching Definitions와 Def-Use 연결.

정의 집합은 파이썬 정수 비트셋으로 표현한다.
  IN[n]  = ∪ OUT[p]  (p ∈ pred(n))
  OUT[n] = GEN[n] ∪ (IN[n] − KILL[n])
반복문이 있으면 변경이 없어질 때까지(고정점) worklist에서 재계산한다.

강한 갱신(strong update): 단일 위치에 대한 직접 대입만 이전 정의를 제거(kill)한다.
포인터를 통한 쓰기(대상이 여러 개), 배열 원소, 호출 부작용은 약한 갱신이다.
"""
from __future__ import annotations

from collections import defaultdict, deque
from typing import Dict, Iterable, List, Set, Tuple

from .cfg import CFG
from .model import loc_may_alias


class ReachingDefs:
    def __init__(self, cfg: CFG, defs: Dict[str, List[Tuple[str, bool]]]):
        """defs: node -> [(location, strong)]"""
        self.cfg = cfg
        self.def_list: List[Tuple[str, str]] = []          # index -> (node, loc)
        self.by_loc: Dict[str, List[int]] = defaultdict(list)
        gen: Dict[str, int] = defaultdict(int)
        strong_locs: Dict[str, Set[str]] = defaultdict(set)
        for n, ds in defs.items():
            for loc, strong in ds:
                i = len(self.def_list)
                self.def_list.append((n, loc))
                self.by_loc[loc].append(i)
                gen[n] |= 1 << i
                if strong:
                    strong_locs[n].add(loc)
        kill: Dict[str, int] = defaultdict(int)
        for n, locs in strong_locs.items():
            m = 0
            for loc in locs:
                for i in self.by_loc[loc]:
                    m |= 1 << i
            kill[n] = m & ~gen[n]
        self.IN: Dict[str, int] = defaultdict(int)
        self.OUT: Dict[str, int] = defaultdict(int)
        order = cfg.rpo()
        work = deque(order)
        inq = set(order)
        iters = 0
        while work:
            n = work.popleft()
            inq.discard(n)
            iters += 1
            i = 0
            for p in cfg.pred_ids(n):
                i |= self.OUT[p]
            self.IN[n] = i
            o = gen[n] | (i & ~kill[n])
            if o != self.OUT[n]:
                self.OUT[n] = o
                for s in cfg.succ_ids(n):
                    if s not in inq:
                        work.append(s)
                        inq.add(s)
        self.iterations = iters

    def reaching(self, node: str, loc: str) -> List[str]:
        """node 직전에 loc(별칭 포함)에 도달하는 정의 노드들."""
        bits = self.IN.get(node, 0)
        out: List[str] = []
        if not bits:
            return out
        for dloc, idxs in self.by_loc.items():
            if not loc_may_alias(dloc, loc):
                continue
            for i in idxs:
                if bits >> i & 1:
                    out.append(self.def_list[i][0])
        return out


def def_use_edges(rd: ReachingDefs, uses: Dict[str, Iterable[str]],
                  at: Dict[str, str] = None) -> List[Tuple[str, str, str]]:
    """(정의 노드, 사용 노드, 위치). at: 사용 노드 -> 도달 정의를 볼 CFG 지점(actual-in용)."""
    edges: List[Tuple[str, str, str]] = []
    at = at or {}
    for user, locs in uses.items():
        point = at.get(user, user)
        for loc in locs:
            for d in rd.reaching(point, loc):
                if d != user:
                    edges.append((d, user, loc))
    return edges
