"""차이 조건 도출과 출력 변화 설명(결합 프로그램 위에서).

결합 프로그램(product program): 변경 전/후 프로그램에 같은 입력·초기 상태·환경 입력을 넣고
  D(x) = (관찰값_old(x) != 관찰값_new(x)) ∧ 정의역 ∧ (두 버전 모두 전개 범위 안에서 종료)
를 만든다. 조건식 XOR은 '조건에 들어가는 값이 전후 같을 때'만 맞는 특수한 경우이므로 쓰지 않고,
분기 차이도 각 버전에서 실제로 계산된 조건값·도달 조건으로 비교한다.

이 모듈은 D를 사람이 읽을 수 있는 '입력 상자들의 합집합'으로 바꾼다.
  1. D를 만족하는 점을 하나 찾고
  2. 변수마다 이분 탐색으로 상자를 최대한 넓힌다(상자 ⊆ D 를 SMT로 매번 확인 → 건전)
  3. 찾은 상자를 제외하고 반복
  4. 남은 D가 공집합이면 '정확(검증됨)', 상자 개수 한도에 걸리면 '부분(일부 상자만)'
각 상자 안에서 출력 변화를 설명한다: 변경 전/후 관찰값을 상수·입력 변수 후보 중에서 찾고
(상자 전체에서 성립함을 SMT로 확인), 변화량(new-old)의 최소·최대를 구한다.
"""
from __future__ import annotations

from typing import Dict, List, Optional, Sequence, Tuple

import z3


class InputVar:
    __slots__ = ("term", "name", "signed", "lo", "hi", "width")

    def __init__(self, term, name: str, signed: bool, lo: int, hi: int):
        self.term = term
        self.name = name
        self.signed = signed
        self.lo = lo
        self.hi = hi
        self.width = term.size()

    def le(self, v: int):
        c = z3.BitVecVal(v % (1 << self.width), self.width)
        return self.term <= c if self.signed else z3.ULE(self.term, c)

    def ge(self, v: int):
        c = z3.BitVecVal(v % (1 << self.width), self.width)
        return self.term >= c if self.signed else z3.UGE(self.term, c)

    def value(self, m: z3.ModelRef) -> int:
        x = m.eval(self.term, model_completion=True).as_long()
        if self.signed and x >= 1 << (self.width - 1):
            x -= 1 << self.width
        return x


def _box_formula(box: Dict[str, Tuple[int, int]], vars_: Sequence[InputVar]):
    cs = []
    for v in vars_:
        lo, hi = box[v.name]
        cs += [v.ge(lo), v.le(hi)]
    return z3.And(*cs) if cs else z3.BoolVal(True)


def derive_boxes(base: List, region, vars_: Sequence[InputVar], max_boxes: int = 8,
                 timeout_ms: int = 10000, budget_s: float = 15.0) -> dict:
    """region(입력에 대한 z3 식)을 입력 상자들의 합집합으로 표현한다.

    상자 ⊆ region 확인은 '¬region ∧ 상자'의 충족 불가능성으로 하며, 같은 솔버에 push/pop으로
    상자 제약만 바꿔 넣어(증분 풀이) 학습 결과를 재사용한다. 시간 예산을 넘기면 그때까지의 상자만
    돌려주고 '부분'으로 표시한다.
    """
    import time
    t0 = time.time()
    boxes: List[Dict[str, Tuple[int, int]]] = []
    if not vars_:
        return {"boxes": [], "exact": False, "reason": "입력 변수 없음"}
    finder = z3.Solver()
    finder.set("timeout", timeout_ms)
    finder.add(*base)
    finder.add(region)
    neg = z3.Solver()
    neg.set("timeout", timeout_ms)
    neg.add(*base)
    neg.add(z3.Not(region))
    state = {"unknown": False}

    def subset(box) -> bool:
        neg.push()
        neg.add(_box_formula(box, vars_))
        r = neg.check()
        neg.pop()
        if r == z3.unknown:
            state["unknown"] = True
        return r == z3.unsat

    exact = False
    over_budget = False
    while len(boxes) < max_boxes:
        if time.time() - t0 > budget_s:
            over_budget = True
            break
        r = finder.check()
        if r == z3.unsat:
            exact = True
            break
        if r != z3.sat:
            state["unknown"] = True
            break
        m = finder.model()
        box = {v.name: (v.value(m), v.value(m)) for v in vars_}
        for v in vars_:
            lo_ok, lo_bad = box[v.name][0], v.lo - 1
            while lo_ok - lo_bad > 1 and time.time() - t0 <= budget_s:
                mid = (lo_ok + lo_bad) // 2
                trial = dict(box)
                trial[v.name] = (mid, box[v.name][1])
                if subset(trial):
                    lo_ok = mid
                else:
                    lo_bad = mid
            box[v.name] = (lo_ok, box[v.name][1])
            hi_ok, hi_bad = box[v.name][1], v.hi + 1
            while hi_bad - hi_ok > 1 and time.time() - t0 <= budget_s:
                mid = (hi_ok + hi_bad) // 2
                trial = dict(box)
                trial[v.name] = (box[v.name][0], mid)
                if subset(trial):
                    hi_ok = mid
                else:
                    hi_bad = mid
            box[v.name] = (box[v.name][0], hi_ok)
        boxes.append(box)
        finder.add(z3.Not(_box_formula(box, vars_)))
    return {"boxes": [{k: list(v) for k, v in b.items()} for b in boxes], "exact": exact and not state["unknown"],
            "complete": exact, "limit_reached": not exact and not over_budget and not state["unknown"],
            "over_budget": over_budget, "solver_unknown": state["unknown"],
            "seconds": round(time.time() - t0, 2), "domains": {v.name: [v.lo, v.hi] for v in vars_}}


def box_text(box: Dict[str, List[int]], domains: Dict[str, List[int]]) -> str:
    parts = []
    for k, (lo, hi) in box.items():
        dlo, dhi = domains.get(k, [None, None])
        if lo == dlo and hi == dhi:
            continue
        if lo == hi:
            parts.append(f"{k} = {lo}")
        elif lo == dlo:
            parts.append(f"{k} ≤ {hi}")
        elif hi == dhi:
            parts.append(f"{k} ≥ {lo}")
        else:
            parts.append(f"{lo} ≤ {k} ≤ {hi}")
    return " ∧ ".join(parts) if parts else "(정의역 전체)"


def condition_text(res: dict) -> str:
    if not res.get("boxes"):
        return "(없음)" if res.get("complete") else "(도출 실패)"
    txt = " ∨ ".join(f"({box_text(b, res['domains'])})" if len(res["boxes"]) > 1 else box_text(b, res["domains"])
                     for b in res["boxes"])
    if res.get("exact"):
        return txt
    why = "시간 예산 초과" if res.get("over_budget") else ("상자 개수 한도" if res.get("limit_reached") else "솔버 판정 불가")
    return txt + f"  [부분: {why}로 차이 영역의 일부만 표현]"


def _const_or_var(term, cands: List[Tuple[str, object]], base: List, timeout_ms: int) -> Optional[str]:
    """상자 전체에서 term == 후보(상수/입력 변수)가 항상 성립하는 후보 이름."""
    for name, c in cands:
        s = z3.Solver()
        s.set("timeout", timeout_ms)
        s.add(*base)
        try:
            s.add(term != c)
        except z3.Z3Exception:
            continue
        if s.check() == z3.unsat:
            return name
    return None


def explain_box(base: List, region, box: Dict[str, List[int]], vars_: Sequence[InputVar], old_terms: List,
                new_terms: List, out_signed: bool, timeout_ms: int = 10000) -> Optional[dict]:
    """상자 안에서 출력이 어떻게 바뀌는지: 변경 전/후 값의 형태와 변화량 범위."""
    inbox = list(base) + [_box_formula({k: tuple(v) for k, v in box.items()}, vars_), region]
    s = z3.Solver()
    s.set("timeout", timeout_ms)
    s.add(*inbox)
    if s.check() != z3.sat:
        return None
    m = s.model()
    step = None
    for k, (a, b) in enumerate(zip(old_terms, new_terms)):
        if a is None or b is None:
            continue
        w = max(a.size(), b.size())
        a2, b2 = z3.ZeroExt(w - a.size(), a) if a.size() < w else a, z3.ZeroExt(w - b.size(), b) if b.size() < w else b
        if z3.is_true(m.eval(a2 != b2, model_completion=True)):
            step = k
            break
    if step is None:
        return None
    o, n = old_terms[step], new_terms[step]

    def sval(t):
        x = m.eval(t, model_completion=True).as_long()
        return x - (1 << t.size()) if out_signed and x >= 1 << (t.size() - 1) else x

    def cands(t):
        out = [(str(sval(t)), z3.BitVecVal(m.eval(t, model_completion=True).as_long(), t.size()))]
        for v in vars_:
            vt = v.term
            if vt.size() == t.size():
                out.append((v.name, vt))
            elif vt.size() < t.size():
                out.append((v.name, z3.SignExt(t.size() - vt.size(), vt) if v.signed
                            else z3.ZeroExt(t.size() - vt.size(), vt)))
        return out
    inbox_step = inbox
    old_form = _const_or_var(o, cands(o), inbox_step, timeout_ms)
    new_form = _const_or_var(n, cands(n), inbox_step, timeout_ms)
    # 변화량(new - old) 범위
    w = max(o.size(), n.size()) + 1
    ext = (lambda t: z3.SignExt(w - t.size(), t)) if out_signed else (lambda t: z3.ZeroExt(w - t.size(), t))
    delta = z3.BV2Int(ext(n) - ext(o), is_signed=True)
    rng = []
    for minimize in (True, False):
        op = z3.Optimize()
        op.set("timeout", timeout_ms)
        op.add(*inbox_step)
        op.minimize(delta) if minimize else op.maximize(delta)
        if op.check() != z3.sat:
            rng = []
            break
        v = op.model().eval(delta, model_completion=True)
        rng.append(v.as_long() if z3.is_int_value(v) else None)
    direction = None
    if len(rng) == 2 and None not in rng:
        if rng[0] >= 0:
            direction = "증가" if rng[0] > 0 else "증가(일부 입력은 이 주기에 같음)"
        elif rng[1] <= 0:
            direction = "감소" if rng[1] < 0 else "감소(일부 입력은 이 주기에 같음)"
        else:
            direction = "증가 또는 감소"
    return {"step": step, "old": old_form, "new": new_form, "example_old": sval(o), "example_new": sval(n),
            "delta": rng, "direction": direction}


def _ga(word: str) -> str:
    """주격 조사: 마지막 글자에 받침이 있으면 '이', 없으면 '가', 한글이 아니면 '이(가)'."""
    w = (word or "").rstrip()
    if not w:
        return "가"
    ch = w[-1]
    if "가" <= ch <= "힣":
        return "이" if (ord(ch) - 0xAC00) % 28 else "가"
    return "이(가)"


def feature_sentence(feature_name: str, obs_desc: str, cond: str, expl: Optional[dict], seq: bool) -> str:
    if not expl:
        return f"[{feature_name}] {cond} 일 때 {obs_desc} 값이 달라진다."
    old = expl.get("old") or f"{expl['example_old']}(예)"
    new = expl.get("new") or f"{expl['example_new']}(예)"
    when = f"{expl['step'] + 1}번째 주기에 " if seq else ""
    d = expl.get("delta") or []
    mag = ""
    if len(d) == 2 and None not in d:
        mag = f" — 변화량 {d[0]:+d}" + (f" ~ {d[1]:+d}" if d[0] != d[1] else "")
    dirn = f", 출력 {expl['direction']}" if expl.get("direction") else ""
    return f"[{feature_name}] {cond} 일 때 {when}{obs_desc}{_ga(obs_desc)} 변경 전 {old} → 변경 후 {new}{dirn}{mag}"
