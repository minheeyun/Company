"""결정적(규칙 기반) 잠재 결함 후보 검출.

AI가 '실행 경로/오버플로 여부'를 추측하지 않도록, 자료형·상수·가드 조건으로 판단 가능한 부분을
먼저 기계적으로 계산해 후보와 근거를 만든다. 각 후보의 확실성(certainty):

  CERTAIN   자료형 크기와 상수만으로 성립하는 사실 (예: uint8 변수 < 300 은 항상 참)
  PROBABLE  구조적 근거가 강함 (예: 가드가 자료형 범위를 넘어 포화 방지 효과 없음, ISR과의 읽기-판단-쓰기)
  POSSIBLE  런타임 값/지속 시간에 따라 발생 (예: 포화 처리 없는 8bit 카운터)
  REVIEW    판단에 필요한 정보가 부족 (예: 실행 컨텍스트 정보 없음)

후보는 '결함 확정'이 아니다. 단계 5(코드 결함 분석)에서 LLM이 사양·호출 맥락을 보고
판정(확정/가능성/오탐 추정)을 내리며, 그 판정도 보고서 가드레일을 거친다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from . import cfront as C

try:
    from pycparser import c_ast
except ImportError:  # pragma: no cover
    c_ast = None


@dataclass
class Candidate:
    id: str
    rule: str
    category: str           # RUNTIME_ERROR | LOGIC_ERROR | CONCURRENCY
    title: str
    file: str
    function: str
    line: int
    certainty: str          # CERTAIN | PROBABLE | POSSIBLE | REVIEW
    severity: str           # HIGH | MEDIUM | LOW | INFO
    message: str
    evidence: list = field(default_factory=list)
    needs: list = field(default_factory=list)
    symbols: list = field(default_factory=list)
    tool_evidence: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return dict(self.__dict__)


RULES = {
    "COUNTER_WRAP": ("RUNTIME_ERROR", "카운터/타이머 오버플로·언더플로(순환)"),
    "INEFFECTIVE_GUARD": ("LOGIC_ERROR", "자료형 범위를 넘는 가드로 포화 방지 실패"),
    "CONST_COMPARE_RANGE": ("LOGIC_ERROR", "자료형 범위 밖 상수와 비교 (항상 참/거짓)"),
    "CONST_TRUNCATION": ("RUNTIME_ERROR", "상수 대입/형변환 시 값 손실"),
    "IMPLICIT_NARROWING": ("RUNTIME_ERROR", "암시적 축소 변환에 따른 값 손실 가능"),
    "ARITH_OVERFLOW": ("RUNTIME_ERROR", "산술 연산 중간 결과 오버플로 가능"),
    "DIV_BY_ZERO": ("RUNTIME_ERROR", "0으로 나누기 가능"),
    "INDEX_OOB": ("RUNTIME_ERROR", "배열 인덱스 범위 초과 가능"),
    "NULL_DEREF": ("RUNTIME_ERROR", "포인터 NULL 검사 없이 역참조"),
    "UNINIT_LOCAL": ("RUNTIME_ERROR", "초기화되지 않은 지역 변수 사용"),
    "SHARED_RMW": ("CONCURRENCY", "공유 변수의 비원자적 읽기-수정-쓰기"),
    "SHARED_RCW": ("CONCURRENCY", "공유 변수의 비원자적 읽기-판단-쓰기"),
    "TORN_ACCESS": ("CONCURRENCY", "원자적 접근 폭을 넘는 공유 변수 접근"),
    "MISSING_VOLATILE": ("CONCURRENCY", "ISR 공유 변수에 volatile 없음"),
    "SHARED_UNVERIFIED": ("CONCURRENCY", "공유 변수 경쟁 가능성 (실행 환경 정보 부족)"),
    "STATE_NEVER_ENTERED": ("LOGIC_ERROR", "진입 전이가 없는 상태 (도달 불가 가능)"),
    "SWITCH_MISSING_CASE": ("LOGIC_ERROR", "상태 switch에서 처리되지 않는 값"),
    "TOOL_WARNING": ("RUNTIME_ERROR", "외부 정적 분석 도구 경고"),
}

REL_OPS = {"<", "<=", ">", ">=", "==", "!="}
FLIP = {"<": ">", "<=": ">=", ">": "<", ">=": "<=", "==": "==", "!=": "!="}
NEG = {"<": ">=", "<=": ">", ">": "<=", ">=": "<", "==": "!=", "!=": "=="}

# ---------------------------------------------------------------------------
# 값 범위 (구간 연산 + C 정수 승격)
# ---------------------------------------------------------------------------


@dataclass
class P:
    lo: int
    hi: int
    bits: int
    signed: bool
    const: bool = False

    @property
    def tmin(self) -> int:
        return -(1 << (self.bits - 1)) if self.signed else 0

    @property
    def tmax(self) -> int:
        return (1 << (self.bits - 1)) - 1 if self.signed else (1 << self.bits) - 1

    def type_name(self) -> str:
        return f"{'signed' if self.signed else 'unsigned'} {self.bits}bit"


def _type_p(ti: C.TypeInfo) -> P | None:
    r = ti.int_range() if ti else None
    if not r:
        return None
    return P(r[0], r[1], ti.bits or 8, bool(ti.signed))


def const_type(text: str, v: int, t) -> tuple[int, bool]:
    suf = re.search(r"[uUlL]+$", text or "")
    s = suf.group(0).lower() if suf else ""
    is_hex = (text or "").lower().startswith(("0x", "0b")) or bool(re.fullmatch(r"0[0-7]+[uUlL]*", text or ""))
    ranks = [t.int_bits, t.long_bits, t.long_long_bits]
    start = s.count("l")
    for bits in ranks[min(start, 2):]:
        opts = [False] if "u" in s else ([True, False] if is_hex else [True])
        for signed in opts:
            lo, hi = (-(1 << (bits - 1)), (1 << (bits - 1)) - 1) if signed else (0, (1 << bits) - 1)
            if lo <= v <= hi:
                return bits, signed
    return t.long_long_bits, "u" in s


class Ranges:
    def __init__(self, model: C.CodeModel):
        self.m = model
        self.t = model.env.target

    def promote(self, p: P) -> P:
        ib = self.t.int_bits
        if p.bits < ib:
            return P(p.lo, p.hi, ib, True, p.const)
        return p

    def common(self, a: P, b: P) -> tuple[int, bool]:
        a, b = self.promote(a), self.promote(b)
        if a.signed == b.signed:
            return max(a.bits, b.bits), a.signed
        u, s = (a, b) if not a.signed else (b, a)
        if u.bits >= s.bits:
            return u.bits, False
        return s.bits, True

    def of(self, n, ev: C.Event) -> P | None:
        tu = ev.tu
        if n is None or c_ast is None:
            return None
        if isinstance(n, c_ast.Constant):
            v = C.parse_c_constant(n.value)
            if isinstance(v, int):
                bits, signed = const_type(n.value, v, self.t)
                return P(v, v, bits, signed, True)
            return None
        if isinstance(n, c_ast.ID):
            if n.name in self.m.enum_values and n.name not in ev.names:
                v = self.m.enum_values[n.name]
                return P(v, v, self.t.int_bits, True, True)
            vi = ev.names.get(n.name)
            return _type_p(vi.type) if vi else None
        if isinstance(n, c_ast.Cast):
            ti = tu.resolve(n.to_type)
            tp = _type_p(ti)
            if tp is None:
                return None
            inner = self.of(n.expr, ev)
            if inner and tp.tmin <= inner.lo and inner.hi <= tp.tmax:
                return P(inner.lo, inner.hi, tp.bits, tp.signed, inner.const)
            if inner and inner.const:
                span = tp.tmax - tp.tmin + 1
                v = (inner.lo - tp.tmin) % span + tp.tmin
                return P(v, v, tp.bits, tp.signed, True)
            return tp
        if isinstance(n, (c_ast.ArrayRef, c_ast.StructRef)):
            return _type_p(self.type_of(n, ev))
        if isinstance(n, c_ast.UnaryOp):
            if n.op == "!":
                return P(0, 1, self.t.int_bits, True)
            inner = self.of(n.expr, ev)
            if inner is None:
                return None
            if n.op == "-":
                q = self.promote(inner)
                return P(-inner.hi, -inner.lo, q.bits, q.signed, inner.const)
            if n.op == "+":
                return self.promote(inner)
            if n.op in ("p++", "p--", "++", "--"):
                return inner
            return None
        if isinstance(n, c_ast.BinaryOp):
            if n.op in REL_OPS or n.op in ("&&", "||"):
                return P(0, 1, self.t.int_bits, True)
            a, b = self.of(n.left, ev), self.of(n.right, ev)
            if a is None or b is None:
                return None
            res, _exact, _ovf = self.arith(n.op, a, b)
            return res
        if isinstance(n, c_ast.TernaryOp):
            a, b = self.of(n.iftrue, ev), self.of(n.iffalse, ev)
            if a is None or b is None:
                return None
            bits, signed = self.common(a, b)
            return P(min(a.lo, b.lo), max(a.hi, b.hi), bits, signed)
        if isinstance(n, c_ast.FuncCall) and isinstance(n.name, c_ast.ID):
            fn = self.m.find_function(n.name.name, ev.file)
            return _type_p(fn.return_type) if fn else None
        return None

    def arith(self, op: str, a: P, b: P):
        """(결과 범위, 정확한 수학적 구간, 오버플로 가능 여부)"""
        bits, signed = self.common(a, b) if op not in ("<<", ">>") else (self.promote(a).bits, self.promote(a).signed)
        tmin = -(1 << (bits - 1)) if signed else 0
        tmax = (1 << (bits - 1)) - 1 if signed else (1 << bits) - 1
        const = a.const and b.const
        lo = hi = None
        if op == "+":
            lo, hi = a.lo + b.lo, a.hi + b.hi
        elif op == "-":
            lo, hi = a.lo - b.hi, a.hi - b.lo
        elif op == "*":
            prods = [a.lo * b.lo, a.lo * b.hi, a.hi * b.lo, a.hi * b.hi]
            lo, hi = min(prods), max(prods)
        elif op == "<<" and b.lo == b.hi and 0 <= b.lo < 64:
            lo, hi = a.lo << b.lo, a.hi << b.lo
        elif op == ">>" and 0 <= b.lo <= b.hi < 64:
            lo, hi = a.lo >> b.hi, a.hi >> b.lo
        elif op == "/":
            if b.lo > 0:
                lo, hi = min(0, a.lo), max(0, a.hi // b.lo)
            else:
                lo, hi = min(0, a.lo, -a.hi), max(0, a.hi, -a.lo)
        elif op == "%":
            m = max(abs(b.lo), abs(b.hi))
            lo, hi = (-(m - 1) if a.lo < 0 else 0), max(0, m - 1)
        elif op == "&":
            if b.const and b.lo >= 0:
                lo, hi = 0, b.lo
            elif a.const and a.lo >= 0:
                lo, hi = 0, a.lo
        if lo is None:
            return P(tmin, tmax, bits, signed, False), None, False
        overflow = lo < tmin or hi > tmax
        if overflow:
            return P(tmin, tmax, bits, signed, False), (lo, hi), True
        return P(lo, hi, bits, signed, const), (lo, hi), False

    def type_of(self, n, ev: C.Event) -> C.TypeInfo:
        tu = ev.tu
        if isinstance(n, c_ast.ID):
            vi = ev.names.get(n.name)
            return vi.type if vi else C.UNKNOWN
        if isinstance(n, c_ast.ArrayRef):
            base = self.type_of(n.name, ev)
            if base.kind == "array":
                if len(base.dims) > 1:
                    return C.TypeInfo(base.text, "array", elem=base.elem, dims=base.dims[1:])
                return base.elem or C.UNKNOWN
            if base.kind == "pointer":
                return base.elem or C.UNKNOWN
            return C.UNKNOWN
        if isinstance(n, c_ast.StructRef):
            base = self.type_of(n.name, ev)
            if n.type == "->" and base.kind == "pointer":
                base = base.elem or C.UNKNOWN
            return tu.member_type(base.struct, n.field.name)
        if isinstance(n, c_ast.Cast):
            return tu.resolve(n.to_type)
        if isinstance(n, c_ast.UnaryOp) and n.op == "*":
            base = self.type_of(n.expr, ev)
            return base.elem if base.kind == "pointer" and base.elem else C.UNKNOWN
        return C.UNKNOWN


# ---------------------------------------------------------------------------
# 가드 해석
# ---------------------------------------------------------------------------


def _atoms(node, negated: bool):
    """가드식에서 (비교 노드, 부정 여부) 원자 조건을 뽑는다 (&&, ||의 드모르간 처리 포함)."""
    if isinstance(node, c_ast.UnaryOp) and node.op == "!":
        yield from _atoms(node.expr, not negated)
    elif isinstance(node, c_ast.BinaryOp) and node.op == "&&" and not negated:
        yield from _atoms(node.left, False)
        yield from _atoms(node.right, False)
    elif isinstance(node, c_ast.BinaryOp) and node.op == "||" and negated:
        yield from _atoms(node.left, True)
        yield from _atoms(node.right, True)
    elif isinstance(node, c_ast.BinaryOp) and node.op in REL_OPS:
        yield node, negated


@dataclass
class Bounds:
    upper: int | None = None
    lower: int | None = None
    upper_src: object = None
    lower_src: object = None
    nonconst: list = field(default_factory=list)   # 상수 아닌 비교 가드
    nonconst_upper: list = field(default_factory=list)  # (상한 식 노드, 포함 여부)
    ne: list = field(default_factory=list)          # != 상수


def bounds_for(target_text: str, guards, ev: C.Event, rng: Ranges) -> Bounds:
    b = Bounds()
    for g in guards:
        if g.node is None:
            continue
        for cmp, neg in _atoms(g.node, g.negated):
            op = cmp.op
            if C.expr_text(cmp.left) == target_text:
                other, op2 = cmp.right, op
            elif C.expr_text(cmp.right) == target_text:
                other, op2 = cmp.left, FLIP[op]
            else:
                continue
            if neg:
                op2 = NEG[op2]
            v = ev.tu.const_value(other)
            if not isinstance(v, int):
                b.nonconst.append(g)
                if op2 in ("<", "<="):
                    b.nonconst_upper.append((other, op2 == "<="))
                continue
            if op2 == "<":
                ub = v - 1
            elif op2 == "<=":
                ub = v
            elif op2 == "==":
                ub = v
            else:
                ub = None
            if ub is not None and (b.upper is None or ub < b.upper):
                b.upper, b.upper_src = ub, g
            if op2 == ">":
                lb = v + 1
            elif op2 == ">=":
                lb = v
            elif op2 == "==":
                lb = v
            else:
                lb = None
            if lb is not None and (b.lower is None or lb > b.lower):
                b.lower, b.lower_src = lb, g
            if op2 == "!=":
                b.ne.append((v, g))
    return b


# ---------------------------------------------------------------------------
# 검사기
# ---------------------------------------------------------------------------


class Checker:
    def __init__(self, model: C.CodeModel):
        self.m = model
        self.env = model.env
        self.rng = Ranges(model)
        self.out: list[Candidate] = []
        self._seen: set = set()
        self._var_max_cache: dict = {}

    def add(self, rule, ev_or_loc, certainty, severity, message, evidence=(), needs=(), symbols=(), title=None):
        if isinstance(ev_or_loc, C.Event):
            file, line, func = ev_or_loc.file, ev_or_loc.line, ev_or_loc.function
        else:
            file, line, func = ev_or_loc
        key = (rule, file, line, tuple(symbols))
        if key in self._seen:
            return
        self._seen.add(key)
        cat, default_title = RULES[rule]
        self.out.append(Candidate(f"SC-{len(self.out) + 1:03d}", rule, cat, title or default_title, file, func, line,
                                  certainty, severity, message, list(evidence), list(needs), list(symbols)))

    def run(self) -> list[Candidate]:
        if c_ast is None:
            return []
        for ev in self.m.events:
            try:
                self.check_event(ev)
            except Exception as e:  # noqa: BLE001 - 한 이벤트의 실패가 전체를 막지 않도록
                self.m.issues.append(C.ParseIssue(ev.file, f"검사 중 예외 (L{ev.line}): {e!r}"))
        self.check_uninit()
        self.check_concurrency()
        self.check_states()
        self.out.sort(key=lambda c: (c.file, c.line, c.rule))
        for i, c in enumerate(self.out, 1):
            c.id = f"SC-{i:03d}"
        return self.out

    # --- 이벤트별 -------------------------------------------------------------
    def check_event(self, ev: C.Event):
        n = ev.node
        if ev.kind in ("incdec", "compound") or (ev.kind == "assign" and isinstance(n, c_ast.Assignment)):
            self.check_counter(ev)
        if ev.kind == "binop":
            if n.op in REL_OPS:
                self.check_compare(ev)
            elif n.op in ("/", "%"):
                self.check_div(ev)
            elif n.op in ("*", "<<", "+"):
                self.check_overflow(ev)
        if ev.kind == "assign":
            self.check_truncation(ev)
        if ev.kind == "cast":
            self.check_cast(ev)
        if ev.kind == "index":
            self.check_index(ev)
            self.check_deref(ev, n.name)
        if ev.kind == "deref":
            self.check_deref(ev, n.expr if isinstance(n, c_ast.UnaryOp) else n.name)

    def _base_var(self, node, ev) -> C.VarInfo | None:
        while isinstance(node, (c_ast.ArrayRef, c_ast.StructRef)):
            node = node.name
        if isinstance(node, c_ast.ID):
            return ev.names.get(node.name)
        return None

    def _decision_uses(self, var: C.VarInfo, text: str) -> list[str]:
        uses = []
        pat = re.compile(r"(?<![\w.>])" + re.escape(text) + r"(?![\w\[])")
        for fn in self.m.functions.values():
            for c in fn.conditions:
                if pat.search(c.text) and re.search(r"[<>]=?|==|!=", c.text):
                    if var.scope in ("static", "static_local") and fn.file != var.file:
                        continue
                    uses.append(f"{c.file}:{c.line} {fn.name}(): {c.raw}")
        return uses

    def _post_reset(self, ev, target_text: str, vtype: P) -> str | None:
        """증가 이후 같은 함수에서 '상한 도달 시 재설정' 처리가 있는지 (예: if (idx >= N) idx = 0;)"""
        fn = self.m.functions.get(ev.fn_key)
        if not fn:
            return None
        for a in fn.assigns:
            if a.line <= ev.line or a.op != "=" or a.target != target_text:
                continue
            for g in a.guards:
                if g.node is None:
                    continue
                for cmp, neg in _atoms(g.node, g.negated):
                    sides = (C.expr_text(cmp.left), C.expr_text(cmp.right))
                    if target_text in sides:
                        other = cmp.right if sides[0] == target_text else cmp.left
                        v = ev.tu.const_value(other)
                        if isinstance(v, int) and v <= vtype.tmax:
                            return f"L{a.line}: {a.raw} (조건 L{g.line}: {g.render()})"
        return None

    def check_counter(self, ev: C.Event):
        n = ev.node
        if ev.kind == "incdec":
            target, inc, step = n.expr, n.op in ("++", "p++"), 1
        elif ev.kind == "compound":
            if n.op not in ("+=", "-="):
                return
            target, inc = n.lvalue, n.op == "+="
            sp = self.rng.of(n.rvalue, ev)
            if sp is None or sp.hi <= 0:
                return
            step = sp.hi
            if not inc and sp.lo < 0:
                return
        else:
            rv = n.rvalue
            if n.op != "=" or not isinstance(rv, c_ast.BinaryOp) or rv.op not in ("+", "-"):
                return
            lt = C.expr_text(n.lvalue)
            if C.expr_text(rv.left) == lt:
                other = rv.right
            elif rv.op == "+" and C.expr_text(rv.right) == lt:
                other = rv.left
            else:
                return
            sv = ev.tu.const_value(other)
            if not isinstance(sv, int) or sv <= 0:
                return
            target, inc, step = n.lvalue, rv.op == "+", sv
        var = self._base_var(target, ev)
        if var is None or not var.persistent:
            return
        tp = _type_p(self.rng.type_of(target, ev))
        if tp is None or tp.bits >= 32:
            # 32bit 이상 카운터는 순환까지 시간이 매우 길어 후보에서 제외 (주기가 짧으면 단계 5에서 판단)
            return
        text = C.expr_text(target)
        b = bounds_for(text, ev.guards, ev, self.rng)
        tname = f"{self.rng.type_of(target, ev).text}({tp.type_name()}, {tp.tmin}..{tp.tmax})"
        uses = self._decision_uses(var, text)
        if inc:
            if b.upper is not None:
                if b.upper + step <= tp.tmax:
                    return
                g = b.upper_src
                self.add("INEFFECTIVE_GUARD", ev, "PROBABLE", "HIGH",
                         f"`{text}` 증가 가드 `{g.render()}`(L{g.line})는 {tname}에서 항상 참이므로 포화 처리가 동작하지 않는다. "
                         f"{tp.tmax} 다음 증가 시 {tp.tmin}으로 순환한다.",
                         [f"가드 상한 {b.upper} + 증가량 {step} > 자료형 최대 {tp.tmax}"] + [f"판정에 사용: {u}" for u in uses[:4]],
                         ["증가가 연속으로 몇 주기까지 지속될 수 있는지(사양상 상한)", "매크로 값이 빌드 구성별로 다른지"],
                         [text], title="가드 상수가 자료형 범위를 넘어 순환 방지 실패")
                return
            if b.ne and any(v <= tp.tmax for v, _ in b.ne) and step == 1:
                return  # x != MAX 가드
            reset = self._post_reset(ev, text, tp)
            if reset:
                return
            if b.nonconst:
                self.add("COUNTER_WRAP", ev, "REVIEW", "LOW",
                         f"`{text}`({tname}) 증가가 상수가 아닌 조건으로만 제한된다: "
                         + ", ".join(f"L{g.line}:{g.render()}" for g in b.nonconst),
                         [f"판정에 사용: {u}" for u in uses[:4]], ["비교 대상 변수의 최대값이 자료형 범위 이내인지"], [text])
                return
            sev = "HIGH" if uses else "MEDIUM"
            self.add("COUNTER_WRAP", ev, "POSSIBLE", sev,
                     f"`{text}`({tname})가 포화(상한) 처리 없이 증가한다. 증가 조건이 {tp.tmax - tp.tmin + 1}회 이상 "
                     f"연속 성립하면 {tp.tmin}으로 순환하여, 이 값을 쓰는 판정 결과가 뒤집힐 수 있다.",
                     (["증가 위치의 가드: " + " && ".join(f"L{g.line}:{g.render()}" for g in ev.guards)] if ev.guards
                      else ["가드 없음"]) + [f"판정에 사용: {u}" for u in uses[:4]],
                     ["증가 조건이 연속 성립할 수 있는 최대 횟수(지속 고장·장시간 운행)", "카운터를 0으로 되돌리는 다른 경로"],
                     [text])
        else:
            if not tp.signed:
                if b.lower is not None and b.lower - step >= tp.tmin:
                    return
                if any(v == 0 for v, _ in b.ne) and step == 1:
                    return
                if b.nonconst:
                    return
                self.add("COUNTER_WRAP", ev, "POSSIBLE", "MEDIUM",
                         f"`{text}`({tname})가 하한 검사 없이 감소한다. 0에서 감소하면 {tp.tmax}으로 순환한다.",
                         [f"판정에 사용: {u}" for u in uses[:4]], ["0인 상태에서 이 문장이 실행될 수 있는지"], [text],
                         title="부호 없는 카운터/타이머 언더플로")

    def check_compare(self, ev: C.Event):
        n = ev.node
        lv = (c_ast.ID, c_ast.ArrayRef, c_ast.StructRef)
        lval, other, op = None, None, n.op
        if isinstance(n.left, lv) and isinstance(ev.tu.const_value(n.right), int):
            lval, other = n.left, n.right
        elif isinstance(n.right, lv) and isinstance(ev.tu.const_value(n.left), int):
            lval, other, op = n.right, n.left, FLIP[n.op]
        if lval is None:
            return
        if isinstance(lval, c_ast.ID) and lval.name in self.m.enum_values and lval.name not in ev.names:
            return
        ti = self.rng.type_of(lval, ev)
        if ti.kind not in ("int",):
            return
        tp = _type_p(ti)
        c = ev.tu.const_value(other)
        lo, hi = tp.tmin, tp.tmax
        fns = {"<": lambda x: x < c, "<=": lambda x: x <= c, ">": lambda x: x > c, ">=": lambda x: x >= c,
               "==": lambda x: x == c, "!=": lambda x: x != c}
        f = fns[op]
        if op in ("==", "!="):
            always = (op == "!=") if not (lo <= c <= hi) else None
        else:
            a, z = f(lo), f(hi)
            always = a if a == z else None
        if always is None:
            return
        text = C.expr_text(lval)
        trivial = (op in (">=",) and c == lo == 0 and always) or (op == "<" and c == 0 and lo == 0 and not always)
        sev = "LOW" if trivial else "HIGH"
        cert = "CERTAIN"
        raw = self.m.files[ev.file].code[ev.line - 1].strip() if ev.file in self.m.files else ""
        self.add("CONST_COMPARE_RANGE", ev, cert, sev,
                 f"`{C.expr_text(n)}`은(는) `{text}`의 자료형 {ti.text}({tp.type_name()}, {lo}..{hi})에서 "
                 f"항상 {'참' if always else '거짓'}이다." + (" (부호 없는 변수의 0 이상 비교: 방어 코드일 수 있음)" if trivial else ""),
                 [f"원본: {raw}", f"상수 {c}는 자료형 범위 {lo}..{hi} " + ("밖" if not (lo <= c <= hi) else "경계")],
                 ([] if trivial else ["의도한 임계값과 변수 자료형 중 어느 쪽이 사양과 맞는지"]), [text, C.expr_text(n)])

    def check_div(self, ev: C.Event):
        n = ev.node
        v = ev.tu.const_value(n.right)
        if isinstance(v, (int, float)):
            if v == 0:
                self.add("DIV_BY_ZERO", ev, "CERTAIN", "HIGH", f"`{C.expr_text(n)}`: 상수 0으로 나눈다.", symbols=[C.expr_text(n.right)])
            return
        text = C.expr_text(n.right)
        b = bounds_for(text, ev.guards, ev, self.rng)
        if (b.lower is not None and b.lower > 0) or (b.upper is not None and b.upper < 0) or any(x == 0 for x, _ in b.ne):
            return
        rp = self.rng.of(n.right, ev)
        if rp is not None and (rp.lo > 0 or rp.hi < 0):
            return
        var = self._base_var(n.right, ev)
        if var is not None and var.persistent:
            mx = self.var_range(var)
            if mx is not None and mx[0] > 0:
                return
        self.add("DIV_BY_ZERO", ev, "REVIEW" if b.nonconst else "POSSIBLE", "MEDIUM",
                 f"`{C.expr_text(n)}`의 제수 `{text}`가 0이 아님을 보장하는 조건이 이 경로에 없다.",
                 ["경로 조건: " + (" && ".join(f"L{g.line}:{g.render()}" for g in ev.guards) or "없음")],
                 [f"`{text}`가 0이 될 수 있는 입력/초기 상태(NvM 미기록, 보정값 오류 등)"], [text])

    def check_overflow(self, ev: C.Event):
        n = ev.node
        a, b = self.rng.of(n.left, ev), self.rng.of(n.right, ev)
        if a is None or b is None or (a.const and b.const):
            return
        res, exact, ovf = self.rng.arith(n.op, a, b)
        if not ovf:
            return
        if n.op == "+" and res.bits > 16:
            return
        ib = self.env.target.int_bits
        self.add("ARITH_OVERFLOW", ev, "POSSIBLE", "MEDIUM" if res.bits <= 16 else "LOW",
                 f"`{C.expr_text(n)}`의 연산 자료형은 C 정수 승격 결과 {res.type_name()}(int={ib}bit 기준)이며 "
                 f"피연산자 자료형 범위상 결과가 {exact[0]}..{exact[1]}까지 가능해 {res.tmin}..{res.tmax}를 넘는다"
                 + (" (부호 없는 연산: 모듈러 순환)" if not res.signed else " (부호 있는 연산: 정의되지 않은 동작)") + ".",
                 [f"좌변 범위 {a.lo}..{a.hi}, 우변 범위 {b.lo}..{b.hi}"],
                 ["실제 입력 값 범위(사양/하드웨어, 예: ADC 분해능)가 오버플로 구간에 들어가는지", "대상 컴파일러의 int 크기"],
                 [C.expr_text(n)])

    def _assign_parts(self, ev):
        n = ev.node
        if isinstance(n, c_ast.Decl):
            vi = ev.names.get(n.name)
            if vi is None:
                fn = self.m.functions.get(ev.fn_key)
                vi = next((v for v in (fn.locals if fn else []) if v.name == n.name and v.line == ev.line), None)
            return (vi.type if vi else C.UNKNOWN), n.name, n.init
        if isinstance(n, c_ast.Assignment) and n.op == "=":
            return self.rng.type_of(n.lvalue, ev), C.expr_text(n.lvalue), n.rvalue
        return None, None, None

    def check_truncation(self, ev: C.Event):
        ti, text, rv = self._assign_parts(ev)
        if ti is None or rv is None or not ti.is_integer or ti.kind == "enum":
            return
        tp = _type_p(ti)
        v = ev.tu.const_value(rv)
        if isinstance(v, int):
            if not (tp.tmin <= v <= tp.tmax):
                span = tp.tmax - tp.tmin + 1
                stored = (v - tp.tmin) % span + tp.tmin
                self.add("CONST_TRUNCATION", ev, "CERTAIN", "HIGH",
                         f"`{text}`({ti.text}, {tp.tmin}..{tp.tmax})에 상수 {v}를 대입하면 {stored}로 저장된다.",
                         [f"대입식: {text} = {C.expr_text(rv)}"], ["사양상 의도한 값과 변수 자료형"], [text])
            return
        if isinstance(rv, c_ast.Cast):
            return
        rp = self.rng.of(rv, ev)
        if rp is None or (tp.tmin <= rp.lo and rp.hi <= tp.tmax):
            return
        if rp.bits <= tp.bits and rp.signed == tp.signed:
            return
        self.add("IMPLICIT_NARROWING", ev, "POSSIBLE", "LOW",
                 f"`{text}`({ti.text}, {tp.tmin}..{tp.tmax})에 범위 {rp.lo}..{rp.hi}({rp.type_name()})인 식 "
                 f"`{C.expr_text(rv)}`을 명시적 형변환 없이 대입한다.",
                 [], ["우변의 실제 값 범위가 좌변 자료형에 들어가는지"], [text])

    def check_cast(self, ev: C.Event):
        n = ev.node
        v = ev.tu.const_value(n.expr)
        tp = _type_p(ev.tu.resolve(n.to_type))
        if not isinstance(v, int) or tp is None:
            return
        if not (tp.tmin <= v <= tp.tmax):
            self.add("CONST_TRUNCATION", ev, "CERTAIN", "MEDIUM",
                     f"`{C.expr_text(n)}`: 상수 {v}가 {tp.type_name()} 범위({tp.tmin}..{tp.tmax})를 넘어 잘린다.",
                     symbols=[C.expr_text(n)])

    def var_range(self, var: C.VarInfo) -> tuple[int, int] | None:
        """영속 정수 변수의 가능한 값 범위를 모든 쓰기 지점에서 추정 (모르면 None)."""
        if var.key in self._var_max_cache:
            return self._var_max_cache[var.key]
        self._var_max_cache[var.key] = None
        tp = _type_p(var.type)
        if tp is None:
            return None
        vals = [0]  # 정적 저장기간 변수는 0으로 시작
        if var.init:
            iv = C.eval_const_text(var.init, None)
            if not isinstance(iv, int):
                return None
            vals = [iv]
        for ev in self.m.events:
            n = ev.node
            if ev.kind == "incdec" and C.expr_text(n.expr) == var.name and ev.names.get(var.name) is var:
                if n.op in ("++", "p++"):
                    b = bounds_for(var.name, ev.guards, ev, self.rng)
                    reset = self._post_reset(ev, var.name, tp)
                    if b.upper is not None:
                        vals.append(b.upper + 1)
                    elif reset:
                        m = re.search(r"\b(\d+)U?\b", reset.split("(조건", 1)[1]) if "(조건" in reset else None
                        if not m:
                            return None
                        vals.append(int(m.group(1)) - 1)
                    else:
                        return None
                else:
                    vals.append(tp.tmin)
            elif ev.kind in ("assign", "compound") and isinstance(n, c_ast.Assignment) \
                    and C.expr_text(n.lvalue) == var.name and ev.names.get(var.name) is var:
                if ev.kind == "compound":
                    return None
                v = ev.tu.const_value(n.rvalue)
                if not isinstance(v, int):
                    return None
                vals.append(v)
        r = (min(vals), max(vals))
        self._var_max_cache[var.key] = r
        return r

    def check_index(self, ev: C.Event):
        n = ev.node
        at = self.rng.type_of(n.name, ev)
        if at.kind != "array" or not at.dims or at.dims[0] is None:
            return
        dim = at.dims[0]
        sub = n.subscript
        v = ev.tu.const_value(sub)
        text = C.expr_text(sub)
        arr = C.expr_text(n.name)
        if isinstance(v, int):
            if v < 0 or v >= dim:
                self.add("INDEX_OOB", ev, "CERTAIN", "HIGH", f"`{arr}[{text}]`: 상수 인덱스 {v}가 크기 {dim}을 벗어난다.",
                         symbols=[arr])
            return
        b = bounds_for(text, ev.guards, ev, self.rng)
        ub = b.upper
        src = f"가드 L{b.upper_src.line}: {b.upper_src.render()}" if b.upper_src else None
        if ub is None and b.nonconst_upper:
            for other, incl in b.nonconst_upper:
                ov = self._base_var(other, ev)
                r = self.var_range(ov) if ov is not None and ov.persistent and isinstance(other, c_ast.ID) else None
                if r is not None:
                    cand = r[1] if incl else r[1] - 1
                    if ub is None or cand < ub:
                        ub, src = cand, f"상한 변수 `{C.expr_text(other)}`의 추정 범위 {r[0]}..{r[1]}"
        var = self._base_var(sub, ev)
        if ub is None and var is not None and var.persistent and isinstance(sub, c_ast.ID):
            r = self.var_range(var)
            if r is not None:
                ub, src = r[1], f"`{text}`의 모든 쓰기 지점 기준 추정 범위 {r[0]}..{r[1]}"
        if ub is None:
            sp = self.rng.of(sub, ev)
            if sp is not None and sp.lo >= 0 and sp.hi <= dim - 1:
                return
            self.add("INDEX_OOB", ev, "REVIEW" if b.nonconst else "POSSIBLE", "MEDIUM",
                     f"`{arr}[{text}]`(크기 {dim})의 인덱스 상한을 보장하는 조건을 찾지 못했다.",
                     ["경로 조건: " + (" && ".join(f"L{g.line}:{g.render()}" for g in ev.guards) or "없음")],
                     [f"`{text}`의 최대값이 {dim - 1} 이하로 유지되는지"], [arr, text])
            return
        if ub <= dim - 1:
            return
        self.add("INDEX_OOB", ev, "PROBABLE" if b.upper is not None else "POSSIBLE", "HIGH",
                 f"`{arr}[{text}]`(크기 {dim}): 인덱스 상한이 {ub}로 {dim - 1}을 넘는다 ({src}).",
                 [src or ""], ["상한 조건의 경계(<, <=) 의도"], [arr, text])

    def check_deref(self, ev: C.Event, ptr):
        if not isinstance(ptr, c_ast.ID):
            return
        vi = ev.names.get(ptr.name)
        if vi is None or vi.scope != "param" or vi.type.kind != "pointer":
            return
        if ev.kind == "index" and self.rng.type_of(ptr, ev).kind != "pointer":
            return
        for g in ev.guards:
            if any(isinstance(x, c_ast.ID) and x.name == ptr.name for x in C._walk(g.node)):
                return
        fn = self.m.functions.get(ev.fn_key)
        key = ("NULL_DEREF", ev.fn_key, ptr.name)
        if key in self._seen:
            return
        self._seen.add(key)
        callers = [c.name for c in self.m.callers(fn)] if fn else []
        self.add("NULL_DEREF", ev, "REVIEW", "LOW",
                 f"포인터 매개변수 `{ptr.name}`를 NULL 검사 없이 역참조한다 (호출자 보장 여부 확인 필요).",
                 [f"호출자: {', '.join(callers) or '정적 분석 범위에 없음'}"],
                 ["호출자가 NULL이 아님을 보장하는지(설계 규약, DET 검사)"], [ptr.name])

    # --- 함수 단위 -------------------------------------------------------------
    def check_uninit(self):
        for fn in self.m.functions.values():
            for v in fn.locals:
                if v.scope != "local" or v.init is not None or v.type.kind in ("array", "struct", "union"):
                    continue
                accs = sorted((a for a in fn.accesses if a.var == v.key), key=lambda a: (a.line, a.col))
                if not accs:
                    continue
                first = accs[0]
                if first.mode in ("read", "rmw"):
                    self.add("UNINIT_LOCAL", (fn.file, first.line, fn.name), "PROBABLE", "HIGH",
                             f"지역 변수 `{v.name}`(L{v.line} 선언)이 값을 쓰기 전에 읽힌다 (소스 순서 기준).",
                             [], ["반복문/goto 등으로 실제 실행 순서가 다른지"], [v.name])
                    continue
                writes = [a for a in accs if a.mode in ("write", "addr")]
                for r in (a for a in accs if a.mode in ("read", "rmw")):
                    before = [w for w in writes if (w.line, w.col) < (r.line, r.col)]
                    rs = {(g.line, g.negated) for g in r.guards}
                    sets = [{(g.line, g.negated) for g in w.guards} - rs for w in before]
                    if any(not s for s in sets):
                        continue
                    pairs = any(len(s1) == 1 and len(s2) == 1 and next(iter(s1))[0] == next(iter(s2))[0]
                                and next(iter(s1))[1] != next(iter(s2))[1] for s1 in sets for s2 in sets)
                    if pairs:
                        continue
                    self.add("UNINIT_LOCAL", (fn.file, r.line, fn.name), "POSSIBLE", "MEDIUM",
                             f"지역 변수 `{v.name}`이 일부 조건 경로에서만 초기화된 뒤 L{r.line}에서 읽힌다.",
                             ["쓰기: " + ", ".join(f"L{w.line}" for w in before)],
                             ["초기화되지 않는 경로가 실제로 실행 가능한지"], [v.name])
                    break

    def _protected(self, fn: C.Function, line: int) -> bool:
        enters = [c.line for c in fn.calls if self.env.is_protect_enter(c.callee) and c.line <= line]
        if not enters:
            return False
        last = max(enters)
        exits = [c.line for c in fn.calls if self.env.is_protect_exit(c.callee) and last <= c.line < line]
        return not exits

    def check_concurrency(self):
        env = self.env
        cmap = self.m.context_map()
        ctx_by_name = {c.name: c for c in env.contexts}
        if not env.contexts:
            self._concurrency_unverified()
            return
        for var in self.m.variables.values():
            if var.scope == "extern":
                continue
            by_ctx: dict = {}
            for fn in self.m.functions.values():
                for a in fn.accesses:
                    if a.var != var.key:
                        continue
                    for cn in cmap.get(fn.key, []):
                        ctx = ctx_by_name[cn]
                        if ctx.concurrent:
                            by_ctx.setdefault(cn, []).append((fn, a))
            if len(by_ctx) < 2:
                continue
            writers = {cn for cn, lst in by_ctx.items() if any(a.mode in ("write", "rmw", "addr") for _, a in lst)}
            if not writers:
                continue
            isr_ctxs = [cn for cn in by_ctx if ctx_by_name[cn].kind == "isr"]
            if isr_ctxs and not var.type.volatile:
                fn0, a0 = by_ctx[isr_ctxs[0]][0]
                self.add("MISSING_VOLATILE", (var.file, var.line, "-"), "POSSIBLE", "LOW",
                         f"`{var.name}`는 ISR({', '.join(isr_ctxs)})과 다른 컨텍스트가 공유하지만 volatile이 아니다. "
                         f"컴파일러 최적화로 반복 읽기가 캐시될 수 있다.",
                         [f"접근 컨텍스트: {', '.join(sorted(by_ctx))}"], ["컴파일러 최적화 수준, 반복문 내 폴링 여부"], [var.name])
            for a_name, lst in by_ctx.items():
                A = ctx_by_name[a_name]
                preempters = [cn for cn in by_ctx if cn != a_name and cn in writers and self._can_preempt(A, ctx_by_name[cn])]
                readers_pre = [cn for cn in by_ctx if cn != a_name and self._can_preempt(A, ctx_by_name[cn])]
                for fn, a in lst:
                    if self._protected(fn, a.line):
                        continue
                    other = ", ".join(preempters)
                    if a.mode == "rmw" and preempters:
                        self.add("SHARED_RMW", (a.file, a.line, fn.name), "PROBABLE", "HIGH",
                                 f"{a_name}의 `{var.name}` 읽기-수정-쓰기(L{a.line})가 보호 없이 수행되고, 그 사이 {other}가 "
                                 f"같은 변수를 갱신할 수 있어 갱신이 유실될 수 있다.",
                                 [f"{cn}의 쓰기: " + ", ".join(f"{f.name}():L{x.line}" for f, x in by_ctx[cn]
                                                             if x.mode in ("write", "rmw")) for cn in preempters],
                                 ["해당 연산이 대상 MCU에서 단일 명령(원자적)으로 컴파일되는지"], [var.name])
                    if a.mode == "read" and preempters:
                        later_w = [x for x in fn.accesses if x.var == var.key and x.mode in ("write", "rmw")
                                   and x.line > a.line and not self._protected(fn, x.line)]
                        if later_w:
                            w = later_w[0]
                            self.add("SHARED_RCW", (a.file, a.line, fn.name), "PROBABLE", "HIGH",
                                     f"{a_name}가 `{var.name}`를 L{a.line}에서 읽고 L{w.line}에서 쓰는 사이에 {other}가 값을 "
                                     f"바꾸면, 그 변경이 L{w.line}의 쓰기로 덮여 유실된다.",
                                     [f"{fn.name}(): L{a.line} 읽기 → L{w.line} 쓰기 (보호 구간 없음)"] +
                                     [f"{cn}의 쓰기: " + ", ".join(f"{f.name}():L{x.line}" for f, x in by_ctx[cn]
                                                                 if x.mode in ("write", "rmw")) for cn in preempters],
                                     ["두 접근 사이에서 인터럽트가 발생 가능한지(실행 시간, 인터럽트 금지 여부)"], [var.name])
                    bits = var.type.bits if var.type.kind != "array" else None
                    ab = env.target.atomic_access_bits
                    if ab and bits and bits > ab and ((a.mode == "read" and preempters) or
                                                       (a.mode in ("write", "rmw") and readers_pre)):
                        self.add("TORN_ACCESS", (a.file, a.line, fn.name), "PROBABLE", "MEDIUM",
                                 f"`{var.name}`({bits}bit)는 원자적 접근 폭({ab}bit)보다 커서 {a_name}의 접근(L{a.line}) 도중 "
                                 f"{', '.join(preempters or readers_pre)}가 끼어들면 상·하위가 다른 시점의 값이 섞일 수 있다.",
                                 [], ["대상 MCU의 해당 변수 접근 명령(LDD/LDW 등)이 원자적인지"], [var.name])

    def _can_preempt(self, a, b) -> bool:
        if a.name == b.name:
            return False
        if b.kind == "isr":
            return not (a.kind == "isr" and a.priority is not None and b.priority is not None and a.priority >= b.priority)
        if a.kind == "isr":
            return False
        if a.priority is None or b.priority is None:
            return True
        return a.preemptive and b.priority > a.priority

    def _concurrency_unverified(self):
        isr_like = [fn for fn in self.m.functions.values()
                    if re.search(r"(?i)(isr|irq|interrupt|_handler$|^ISR_)", fn.name) and not self.m.callers(fn)]
        if not isr_like:
            return
        for fn in isr_like:
            for a in fn.accesses:
                var = self.m.variables.get(a.var)
                if var is None or a.mode not in ("write", "rmw"):
                    continue
                others = sorted({f.name for f in self.m.functions.values() if f is not fn
                                 and any(x.var == var.key for x in f.accesses)})
                if others:
                    self.add("SHARED_UNVERIFIED", (a.file, a.line, fn.name), "REVIEW", "MEDIUM",
                             f"`{fn.name}`(이름상 ISR로 추정)에서 갱신하는 `{var.name}`를 {', '.join(others)}에서도 접근한다. "
                             f"실행 컨텍스트 정보가 없어 경쟁 여부를 확정하지 않는다.",
                             [], ["env.json의 contexts(태스크 주기·우선순위·ISR) 및 protection 정보"], [var.name])

    def check_states(self):
        for var in self.m.variables.values():
            if var.type.kind != "enum" or not var.type.enum:
                continue
            enum = self.m.enums.get(var.type.enum)
            if not enum:
                continue
            assigned = {t.to_state for t in self.m.transitions if t.state_var == var.key}
            if var.init:
                assigned.add(var.init)
            else:
                assigned |= {k for k, v in enum.members.items() if v == 0}  # 정적 0 초기화
            never = [k for k in enum.members if k not in assigned]
            for k in never:
                handled = [s for s in self.m.switches if s.var == var.key and k in s.labels]
                self.add("STATE_NEVER_ENTERED", (enum.file, enum.line, "-"), "POSSIBLE", "MEDIUM",
                         f"`{var.name}`({var.type.enum})를 `{k}`로 바꾸는 대입이 분석 범위에 없다"
                         + (f"; 그럼에도 switch에서 처리함 ({', '.join(f'{s.file}:{s.line}' for s in handled)})" if handled else "")
                         + ".",
                         ["관측된 전이: " + ", ".join(sorted({f'{"/".join(t.from_states)}→{t.to_state}' for t in self.m.transitions if t.state_var == var.key}))],
                         ["포인터·NvM·다른 모듈에서 이 상태로 설정하는지", "사양상 이 상태가 필요한지"], [var.name, k])
            for s in self.m.switches:
                if s.var != var.key:
                    continue
                missing = [k for k in enum.members if k not in s.labels]
                if missing and not s.has_default:
                    self.add("SWITCH_MISSING_CASE", (s.file, s.line, s.function), "POSSIBLE", "MEDIUM",
                             f"`switch ({s.expr})`가 {', '.join(missing)}를 처리하지 않고 default도 없다.",
                             [], ["해당 상태에서 아무 동작도 하지 않는 것이 사양상 맞는지"], [var.name])


def run_checks(model: C.CodeModel) -> list[Candidate]:
    return Checker(model).run()


def render_candidates(cands: list[Candidate]) -> str:
    out = []
    for c in cands:
        out.append(f"- [{c.id}] {c.rule} / {c.certainty} / {c.severity} @ {c.file}:{c.line} {c.function}() — {c.message}")
        for e in c.evidence:
            if e:
                out.append(f"    · 근거: {e}")
        for t in c.tool_evidence:
            out.append(f"    · 도구: {t}")
        if c.needs:
            out.append(f"    · 확정에 필요한 정보: {'; '.join(c.needs)}")
    return "\n".join(out) if out else "(검출된 후보 없음)"
