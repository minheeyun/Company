"""Andersen 방식 points-to 분석(흐름·문맥 비민감, 필드 경로 구분, 배열 원소 통합).

추상 lvalue(LV)와 추상 값(VAL)을 튜플로 표현하고, 제약을 고정점까지 반복 적용한다.

  LV : ('var', key) | ('field', LV, name) | ('deref', VAL) | ('unk',)
  VAL: ('addr', LV) | ('load', LV) | ('fn', key) | ('ret', callsite) | ('union', (VAL...)) | ('none',)

함수 포인터(콜백 테이블 포함)도 같은 방식으로 추적해 간접 호출 대상을 복원한다.
대규모 코드나 높은 정밀도가 필요하면 LLVM IR 기반 SVF로 대체하는 것을 권장한다.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Dict, Iterable, List, Optional, Set, Tuple

from .model import FIELD_SEP, loc_root

NONE = ("none",)
UNK = ("unk",)


def vunion(vals: Iterable[tuple]) -> tuple:
    vs = tuple(v for v in vals if v and v != NONE)
    if not vs:
        return NONE
    if len(vs) == 1:
        return vs[0]
    return ("union", vs)


class PointsTo:
    def __init__(self, max_iter: int = 60):
        self.pts: Dict[str, Set[str]] = defaultdict(set)
        self.assigns: List[Tuple[tuple, tuple]] = []
        self.copyrec: List[Tuple[tuple, tuple]] = []
        self.calls: Dict[str, dict] = {}      # cs_id -> {callee: key|None, callee_val, args:[VAL], caller}
        self.returns: List[Tuple[str, tuple]] = []
        self.params: Dict[str, List[str]] = {}   # fn key -> [param key]
        self.callees: Dict[str, Set[str]] = defaultdict(set)
        self.max_iter = max_iter
        self.converged = True
        self.iterations = 0

    # --- 제약 추가 ---
    def add_assign(self, lv: tuple, val: tuple) -> None:
        if val != NONE:
            self.assigns.append((lv, val))

    def add_copyrec(self, lv: tuple, src_lv: tuple) -> None:
        self.copyrec.append((lv, src_lv))

    def add_call(self, cs: str, caller: str, callee: Optional[str], callee_val: tuple, args: List[tuple]) -> None:
        self.calls[cs] = {"callee": callee, "callee_val": callee_val, "args": args, "caller": caller}

    def add_return(self, fn: str, val: tuple) -> None:
        if val != NONE:
            self.returns.append((fn, val))

    # --- 해석 ---
    def resolve_lv(self, lv: tuple) -> Set[str]:
        t = lv[0]
        if t == "var":
            return {lv[1]}
        if t == "field":
            return {f"{b}{FIELD_SEP}{lv[2]}" for b in self.resolve_lv(lv[1]) if not b.startswith("fn:")}
        if t == "deref":
            return {x for x in self.eval_val(lv[1]) if not x.startswith("fn:")}
        if t == "elem":
            return self.resolve_lv(lv[1])
        return set()

    def load(self, loc: str) -> Set[str]:
        out = set(self.pts.get(loc, ()))
        # 필드 비민감 초기화(배열·구조체 전체에 대한 기록)를 상위 위치에서 상속
        cur = loc
        while FIELD_SEP in cur:
            cur = cur.rsplit(FIELD_SEP, 1)[0]
            out |= self.pts.get(cur, set())
        return out

    def eval_val(self, val: tuple) -> Set[str]:
        t = val[0]
        if t == "addr":
            return self.resolve_lv(val[1])
        if t == "load":
            out: Set[str] = set()
            for l in self.resolve_lv(val[1]):
                out |= self.load(l)
            return out
        if t == "fn":
            return {val[1]}
        if t == "ret":
            out = set()
            for c in self.callees.get(val[1], ()):
                out |= self.pts.get(f"ret:{c}", set())
            return out
        if t == "union":
            out = set()
            for v in val[1]:
                out |= self.eval_val(v)
            return out
        return set()

    def _add(self, loc: str, targets: Set[str]) -> bool:
        cur = self.pts[loc]
        before = len(cur)
        cur |= targets
        return len(cur) != before

    def solve(self, known_functions: Set[str]) -> None:
        for it in range(self.max_iter):
            changed = False
            for lv, val in self.assigns:
                tg = self.eval_val(val)
                if not tg:
                    continue
                for l in self.resolve_lv(lv):
                    changed |= self._add(l, tg)
            for lv, src in self.copyrec:
                srcs = self.resolve_lv(src)
                dsts = self.resolve_lv(lv)
                for s in srcs:
                    for k in [k for k in list(self.pts) if k == s or k.startswith(s + FIELD_SEP)]:
                        suffix = k[len(s):]
                        for d in dsts:
                            changed |= self._add(d + suffix, set(self.pts[k]))
            for fn, val in self.returns:
                tg = self.eval_val(val)
                if tg:
                    changed |= self._add(f"ret:{fn}", tg)
            for cs, info in self.calls.items():
                if info["callee"]:
                    targets = {info["callee"]}
                else:
                    targets = {x for x in self.eval_val(info["callee_val"]) if x.startswith("fn:")}
                new = targets - self.callees[cs]
                if new:
                    self.callees[cs] |= new
                    changed = True
                for c in self.callees[cs]:
                    params = self.params.get(c)
                    if not params:
                        continue
                    for i, a in enumerate(info["args"]):
                        if i < len(params):
                            tg = self.eval_val(a)
                            if tg:
                                changed |= self._add(params[i], tg)
            self.iterations = it + 1
            if not changed:
                break
        else:
            self.converged = False
        for cs in self.calls:
            self.callees[cs] = {c for c in self.callees[cs] if c in known_functions or self.calls[cs]["callee"]}

    def targets_of(self, val: tuple) -> Set[str]:
        return self.eval_val(val)

    def summary(self) -> dict:
        return {
            "constraints": len(self.assigns) + len(self.copyrec) + len(self.returns),
            "call_sites": len(self.calls),
            "iterations": self.iterations,
            "converged": self.converged,
            "pointer_locations": sum(1 for k, v in self.pts.items() if v and not k.startswith("ret:")),
        }


def roots(locs: Iterable[str]) -> Set[str]:
    return {loc_root(l) for l in locs}
