"""위험 연산 도달 분석 (Taint Analysis): 변경 지점에서 퍼진 영향이 위험 연산의 '위험한 자리'에 닿는지.

출발점(source)  변경된 문장 (diff 가 정한 시작점)
전파            전방 슬라이스와 같은 의존 그래프 (데이터·메모리·인자·반환·제어)
도착점(sink)    배열 인덱스, 포인터 역참조, 나눗셈·나머지의 분모, 시프트 양, 메모리 복사·초기화 길이,
                좁아지는 변환(대입·형변환), 반복 조건, 함수 포인터 호출

문장 전체가 아니라 '위험한 피연산자'를 기준으로 판정한다. 예를 들어 `x = a[idx] / div` 에서
idx 가 영향받으면 인덱스 위험, div 가 영향받으면 0 나누기 위험이고, a 의 원소 값만 영향받으면 위험이 아니다.

판정 구분
  값 전달          영향받은 값이 위험한 피연산자로 들어감
  변경된 연산      변경된 문장에서 위험한 피연산자 자체가 바뀜 (변경 전 같은 자리와 다름)
  추가된 연산      변경으로 새로 생긴 문장·함수의 위험 연산
  실행 조건 변경   위험 연산을 직접 감싸는 조건이 바뀌었거나, 문장이 다른 조건 아래로 옮겨짐

확실한 위험 (상수·조건 구간으로 위반이 증명됨)
  - 상수 인덱스 < 0 또는 ≥ 배열 크기, 상수 분모 0, 상수 시프트 양 < 0 또는 ≥ 비트 수, 상수 NULL 역참조,
    자료형 범위를 벗어나는 상수의 대입·형변환
  - 바뀐 조건이 허용하는 구간(예: i <= 10)이 배열 크기(10)를 넘거나 분모 0·과도한 시프트를 허용
위반이 증명되지 않고, 자료형 범위나 감싸는 조건으로 안전이 증명되면(예: if (d != 0U) 안의 x / d) 보고하지 않는다.
분석은 변경 후 코드 기준이다.
"""

from __future__ import annotations

import re
from collections import defaultdict

from pycparser import c_ast

from .cexpr import const_eval, gen, norm
from .diff import INF, _complement, _intersect, fmt_set, predicate
from .program import Node, Program, display_path, overlaps
from .slicer import Hit, path_to

KIND_LABEL = {
    "index": "배열 인덱스 범위",
    "deref": "포인터 역참조",
    "div": "0으로 나누기",
    "shift": "시프트 양",
    "length": "복사·초기화 길이",
    "narrow": "값 잘림 (좁아지는 변환)",
    "loop": "반복 조건",
    "fptr": "함수 포인터 호출",
}
KIND_RISK = {
    "index": "범위를 벗어난 읽기·쓰기",
    "deref": "잘못된 주소 접근",
    "div": "0으로 나누기 (예외·정의되지 않은 동작)",
    "shift": "비트 수 이상·음수 시프트 (정의되지 않은 동작)",
    "length": "버퍼 넘침",
    "narrow": "상위 비트 잘림·부호 오류",
    "loop": "반복 횟수·종료 여부 변화 (무한 루프·WDG 리셋)",
    "fptr": "엉뚱한 함수 실행",
}
_KIND_ORDER = ["index", "div", "length", "deref", "fptr", "shift", "narrow", "loop"]
HOW_ORDER = ["값 전달", "변경된 연산", "실행 조건 변경", "추가된 연산", "영향 범위 안의 연산"]
CERTAIN, CANDIDATE = "확실한 위험", "위험 후보"

# 메모리 복사·초기화 함수: 마지막 인자를 길이로 본다
LENGTH_CALL = re.compile(r"(mem(set|cpy|copy|move|clear)|strncpy|strncat|snprintf)", re.I)
_LOOP_SIGS = ("while|", "for|", "dowhile|")


class _Sink:
    __slots__ = ("kind", "expr", "operand", "target", "dim", "width", "write")

    def __init__(self, kind, expr, operand, target=None, dim=None, width=None):
        self.kind, self.expr, self.operand = kind, expr, operand
        self.target, self.dim, self.width = target, dim, width  # dim: 배열 크기 또는 복사 목적지 바이트 크기
        self.write = False

    def key(self) -> tuple:
        return self.kind, norm(self.operand), self.target, self.dim


def _strip(e):
    while isinstance(e, c_ast.Cast):
        e = e.expr
    return e


class _Env:
    """노드 하나를 볼 때 필요한 타입·열거 상수 정보."""

    def __init__(self, prog: Program, n: Node):
        self.prog = prog
        f = prog.funcs.get(n.func) if n.func else None
        self.env = f.env if f else None
        self.enums = prog.enums.get(f.tu, {}) if f else {}

    def const(self, e):
        return const_eval(e, self.enums)

    def type_range(self, t, env=None):
        env = env or self.env
        if env is None or t is None:
            return None
        try:
            r = env.int_range(t)
        except Exception:
            return None
        return (r[0], r[1]) if r else None

    def lval_range(self, e):
        rs = []
        try:
            ls = self.prog.lvals(e)
        except Exception:
            return None
        for l in ls:
            t, env = self.prog.path_type(l)
            r = self.type_range(t, env)
            if r is None:
                return None
            rs.append(r)
        if not rs:
            return None
        return min(r[0] for r in rs), max(r[1] for r in rs)

    def ret_range(self, call: c_ast.FuncCall):
        if not isinstance(call.name, c_ast.ID):
            return None
        key = self.prog.bind.get(id(call.name))
        if key and key.startswith("fn:") and key[3:] in self.prog.funcs:
            f = self.prog.funcs[key[3:]]
            return self.type_range(f.ret_type, f.env)
        proto = self.prog.protos.get(call.name.name)
        if proto:
            return self.type_range(proto[0].type, proto[1])
        return None

    def rng(self, e, depth: int = 0):
        """식이 가질 수 있는 정수 값 범위 (수학적 값, 넘침 전). 모르면 None."""
        if e is None or depth > 24:
            return None
        v = self.const(e)
        if v is not None:
            return v, v
        if isinstance(e, c_ast.Cast):
            tr = self.type_range(e.to_type)
            inner = self.rng(e.expr, depth + 1)
            if tr is None:
                return None
            if inner is not None and tr[0] <= inner[0] and inner[1] <= tr[1]:
                return inner
            return tr
        if isinstance(e, (c_ast.ID, c_ast.StructRef, c_ast.ArrayRef)) or (isinstance(e, c_ast.UnaryOp) and e.op == "*"):
            return self.lval_range(e)
        if isinstance(e, c_ast.FuncCall):
            return self.ret_range(e)
        if isinstance(e, c_ast.TernaryOp):
            a, b = self.rng(e.iftrue, depth + 1), self.rng(e.iffalse, depth + 1)
            return (min(a[0], b[0]), max(a[1], b[1])) if a and b else None
        if isinstance(e, c_ast.UnaryOp):
            if e.op == "!":
                return 0, 1
            if e.op == "-":
                a = self.rng(e.expr, depth + 1)
                return (-a[1], -a[0]) if a else None
            if e.op == "+":
                return self.rng(e.expr, depth + 1)
            if e.op in ("sizeof", "_Alignof"):
                return 0, 2 ** 31 - 1
            return None
        if isinstance(e, c_ast.BinaryOp):
            if e.op in ("<", "<=", ">", ">=", "==", "!=", "&&", "||"):
                return 0, 1
            a, b = self.rng(e.left, depth + 1), self.rng(e.right, depth + 1)
            if e.op == "&":
                cs = [x for x in (a, b) if x and x[0] == x[1] and x[0] >= 0]
                if cs:
                    return 0, min(c[0] for c in cs)
                if a and b and a[0] >= 0 and b[0] >= 0:
                    return 0, min(a[1], b[1])
                return None
            if a is None or b is None:
                return None
            if e.op in ("|", "^"):
                if a[0] < 0 or b[0] < 0:
                    return None
                hi = max(a[1], b[1])
                return 0, (1 << int(hi).bit_length()) - 1
            if e.op == "+":
                return a[0] + b[0], a[1] + b[1]
            if e.op == "-":
                return a[0] - b[1], a[1] - b[0]
            if e.op == "*":
                xs = [a[0] * b[0], a[0] * b[1], a[1] * b[0], a[1] * b[1]]
                return min(xs), max(xs)
            if e.op == "/":
                if b[0] > 0:
                    xs = [int(a[0] / b[0]), int(a[0] / b[1]), int(a[1] / b[0]), int(a[1] / b[1])]
                    return min(xs), max(xs)
                return None
            if e.op == "%":
                m = max(abs(b[0]), abs(b[1]))
                if m == 0:
                    return None
                return (0 if a[0] >= 0 else -(m - 1)), m - 1
            if e.op == ">>" and b[0] >= 0 and a[0] >= 0:
                return a[0] >> int(min(b[1], 64)), a[1] >> int(b[0])
            if e.op == "<<" and b[0] >= 0 and a[0] >= 0 and b[1] <= 64:
                return a[0] << int(b[0]), a[1] << int(b[1])
        return None


# ---------------------------------------------------------------------------
# 위험 연산 찾기
# ---------------------------------------------------------------------------


def _array_dim(E: _Env, arr):
    try:
        ls = E.prog.lvals(arr)
    except Exception:
        return None
    dims = set()
    for l in ls:
        t, env = E.prog.path_type(l)
        if env is None:
            return None
        c = env.core(t)
        if not isinstance(c, c_ast.ArrayDecl) or c.dim is None:
            return None
        d = E.const(c.dim)
        if d is None:
            return None
        dims.add(d)
    return min(dims) if dims else None


def _walk(E: _Env, e, out: list):
    if e is None:
        return
    if isinstance(e, c_ast.FuncCall):
        return  # 호출 인자는 인자 노드에서 따로 본다
    if isinstance(e, c_ast.UnaryOp) and e.op in ("sizeof", "_Alignof"):
        return
    if isinstance(e, (c_ast.Typename, c_ast.TypeDecl, c_ast.PtrDecl, c_ast.ArrayDecl, c_ast.FuncDecl)):
        return
    if isinstance(e, c_ast.ArrayRef):
        out.append(_Sink("index", e, e.subscript, dim=_array_dim(E, e.name)))
    elif isinstance(e, c_ast.UnaryOp) and e.op == "*":
        out.append(_Sink("deref", e, e.expr))
    elif isinstance(e, c_ast.StructRef) and e.type == "->":
        out.append(_Sink("deref", e, e.name))
    elif isinstance(e, c_ast.BinaryOp) and e.op in ("/", "%"):
        out.append(_Sink("div", e, e.right))
    elif isinstance(e, c_ast.BinaryOp) and e.op in ("<<", ">>"):
        lr = E.rng(e.left)
        width = 64 if lr and (lr[1] > 2 ** 32 - 1 or lr[0] < -(2 ** 31)) else 32
        out.append(_Sink("shift", e, e.right, width=width))
    elif isinstance(e, c_ast.Cast):
        tr = E.type_range(e.to_type)
        if tr is not None:
            out.append(_Sink("narrow", e, e.expr, target=tr))
    elif isinstance(e, c_ast.Assignment) and e.op == "=":
        tr = E.lval_range(e.lvalue)
        if tr is not None:
            out.append(_Sink("narrow", e, e.rvalue, target=tr))
    for _, c in e.children():
        _walk(E, c, out)


def _length_spec(prog: Program, name: str, nargs: int, rules=None):
    """복사·초기화 함수면 (길이 인자 위치, 목적지 인자 위치) — 0부터. 아니면 None."""
    if rules is not None:
        spec = rules.length_rule(name)
        if spec is None:
            return None
        _, la, da = spec
    elif LENGTH_CALL.search(name):
        la, da = -1, 1
    else:
        return None
    li = nargs + la if la < 0 else la - 1
    di = nargs + da if da < 0 else da - 1
    if nargs < 2 or not 0 <= li < nargs:
        return None
    return li, (di if 0 <= di < nargs else None)


def _dest_size(E: "_Env", prog: Program, dest):
    """복사 목적지 식이 가리키는 버퍼의 바이트 크기 (모르면 None)."""
    if dest is None:
        return None
    d = _strip(dest)
    off = 0
    if isinstance(d, c_ast.BinaryOp) and d.op == "+":
        c = E.const(d.right)
        if c is None:
            return None
        base = _strip(d.left)
        es = None
        try:
            for l in prog.lvals(base) if isinstance(base, (c_ast.ID, c_ast.StructRef)) else ():
                t, env = prog.path_type(l)
                et = env.element(t) if env else None
                es = sizeof_type(env, et) if et is not None else None
        except Exception:
            es = None
        if es is None:
            return None
        off, d = c * es, base
    if isinstance(d, c_ast.UnaryOp) and d.op == "&":
        size = sizeof_expr(E, d.expr)
    elif isinstance(d, (c_ast.ID, c_ast.StructRef, c_ast.ArrayRef)) and _is_array_expr(prog, d):
        size = sizeof_expr(E, d)
    else:
        try:
            objs = {o for o in prog.pvals(d) if not o[0].startswith(("fn:", "sym:", "hw:"))}
        except Exception:
            objs = set()
        sizes = []
        for o in objs:
            t, env = prog.path_type(o[:-1] if o[-1] == "[]" else o)
            sizes.append(sizeof_type(env, t) if env is not None else None)
        size = min(sizes) if sizes and None not in sizes else None
    return None if size is None else size - off


def _is_array_expr(prog: Program, e) -> bool:
    try:
        return any(prog._is_array_path(l) for l in prog.lvals(e))
    except Exception:
        return False


_SCALAR_SIZE = {"char": 1, "_Bool": 1, "short": 2, "int": 4, "long": 4, "float": 4, "double": 8, "void": 1}


def sizeof_type(env, t, depth: int = 0):
    """32비트 MCU 기준 sizeof (char 1, short 2, int·long·포인터·enum 4, long long·double 8). 모르면 None."""
    if env is None or t is None or depth > 32:
        return None
    c = env.core(t)
    if isinstance(c, c_ast.IdentifierType):
        names = [x for x in c.names if x not in ("signed", "unsigned", "const", "volatile")]
        if names.count("long") == 2:
            return 8
        for k in ("char", "_Bool", "short", "double", "float", "long", "int", "void"):
            if k in names:
                return _SCALAR_SIZE[k]
        return 4 if not names else None
    if isinstance(c, (c_ast.PtrDecl, c_ast.Enum)):
        return 4
    if isinstance(c, c_ast.ArrayDecl):
        dim = const_eval(c.dim) if c.dim is not None else None
        es = sizeof_type(env, c.type, depth + 1)
        return dim * es if dim is not None and es is not None else None
    if isinstance(c, (c_ast.Struct, c_ast.Union)) and c.decls:
        size, align = 0, 1
        for d in c.decls:
            s = sizeof_type(env, d.type, depth + 1)
            a = _alignof(env, d.type, depth + 1)
            if s is None or a is None:
                return None
            align = max(align, a)
            if isinstance(c, c_ast.Union):
                size = max(size, s)
            else:
                size = (size + a - 1) // a * a + s
        return (size + align - 1) // align * align
    return None


def _alignof(env, t, depth=0):
    c = env.core(t)
    if isinstance(c, c_ast.ArrayDecl):
        return _alignof(env, c.type, depth + 1)
    if isinstance(c, (c_ast.Struct, c_ast.Union)) and c.decls:
        al = [_alignof(env, d.type, depth + 1) for d in c.decls]
        return None if None in al else max(al or [1])
    return sizeof_type(env, t, depth)


def sizeof_expr(E: "_Env", e):
    """sizeof(식 또는 자료형)."""
    if isinstance(e, c_ast.Typename):
        return sizeof_type(E.env, e.type)
    try:
        ls = E.prog.lvals(e)
    except Exception:
        return None
    sizes = set()
    for l in ls:
        t, env = E.prog.path_type(l)
        sizes.add(sizeof_type(env, t) if env is not None else None)
    return sizes.pop() if len(sizes) == 1 else None


def sinks_of(prog: Program, n: Node, rules=None) -> list[_Sink]:
    E = _Env(prog, n)
    out: list[_Sink] = []
    e = n.ast
    if n.kind == "stmt":
        if isinstance(e, c_ast.Decl):
            if e.init is not None and not isinstance(e.init, c_ast.InitList):
                tr = E.type_range(e.type)
                if tr is not None:
                    out.append(_Sink("narrow", e, e.init, target=tr))
            _walk(E, e.init, out)
        else:
            _walk(E, e, out)
    elif n.kind in ("cond", "return"):
        _walk(E, e, out)
        if n.kind == "cond" and n.sig.startswith(_LOOP_SIGS) and E.const(e) is None:
            out.append(_Sink("loop", e, e))
    elif n.kind == "actual_in":
        _walk(E, e, out)
        site = prog.sites[n.site]
        args = site.ast.args.exprs if site.ast.args else []
        spec = _length_spec(prog, site.name, len(args), rules)
        if spec and n.arg == spec[0]:
            dest = args[spec[1]] if spec[1] is not None else None
            out.append(_Sink("length", site.ast, e, dim=_dest_size(E, prog, dest)))
    elif n.kind == "extcall":
        args = e.args.exprs if e.args else []
        for a in args:
            _walk(E, a, out)
        spec = _length_spec(prog, prog.sites[n.site].name, len(args), rules)
        if spec:
            dest = args[spec[1]] if spec[1] is not None else None
            out.append(_Sink("length", e, args[spec[0]], dim=_dest_size(E, prog, dest)))
    elif n.kind == "call":
        site = prog.sites[n.site]
        if site.direct is None:
            out.append(_Sink("fptr", site.ast, site.ast.name))
    if rules is not None:
        out = [sk for sk in out if sk.kind in rules.enabled_kinds]
    for sk in out:
        sk.write = sk.kind == "index" and _is_write(e, sk.expr)
    return out


def _is_write(stmt, target) -> bool:
    stack = [stmt]
    while stack:
        x = stack.pop()
        if x is None:
            continue
        if isinstance(x, c_ast.Assignment) and x.lvalue is target:
            return True
        if isinstance(x, c_ast.UnaryOp) and x.op in ("++", "--", "p++", "p--") and x.expr is target:
            return True
        stack.extend(c for _, c in x.children())
    return False


# ---------------------------------------------------------------------------
# 안전 증명·위반 증명
# ---------------------------------------------------------------------------


def _facts(cond, label):
    """조건 cond 가 label(T/F) 일 때 성립하는 단일 변수 비교식 목록 (식, 참/거짓)."""
    c = _strip(cond)
    if isinstance(c, c_ast.BinaryOp):
        if c.op == "&&" and label == "T":
            return _facts(c.left, "T") + _facts(c.right, "T")
        if c.op == "||" and label == "F":
            return _facts(c.left, "F") + _facts(c.right, "F")
    if label in ("T", "F"):
        return [(cond, label)]
    return []


def _allowed(prog: Program, guards, operand, dom, seeds: set[int]):
    """감싸는 조건들이 operand 에 허용하는 값 구간. (구간 또는 None, 바뀐 조건이 관여했는지, 근거 조건들)"""
    key = norm(_strip(operand))
    s = [dom]
    used, changed, texts = False, False, []
    for pn, label in guards:
        for f, lab in _facts(pn.ast, label):
            pr = predicate(f, prog)
            if not pr or pr[0] != key:
                continue
            ss = pr[1] if lab == "T" else _complement(pr[1], pr[2])
            s = _intersect(s, ss)
            used = True
            changed = changed or pn.id in seeds
            texts.append(f"`{gen(f)}` {'참' if lab == 'T' else '거짓'}" + (" (바뀐 조건)" if pn.id in seeds else ""))
    return (s if used else None), changed, texts


def _judge(E: _Env, prog: Program, sk: _Sink, guards, seeds: set[int]):
    """(판정, 근거). 판정: 'certain' | 'safe' | 'unknown'."""
    op = sk.operand
    c = E.const(op)
    var = gen(_strip(op))
    if sk.kind == "index":
        if c is not None:
            if c < 0 or (sk.dim is not None and c >= sk.dim):
                return "certain", f"상수 인덱스 {c} — 배열 크기 {sk.dim}" if sk.dim is not None else f"음수 인덱스 {c}"
            return "safe", ""
        if sk.dim is None:
            return "unknown", ""
        r = E.rng(op)
        dom = r if r else (-INF, INF)
        ok = [(0, sk.dim - 1)]
        allowed, changed, texts = _allowed(prog, guards, op, dom, seeds)
        if allowed is None:
            if r and r[0] >= 0 and r[1] <= sk.dim - 1:
                return "safe", ""
            return "unknown", f"배열 크기 {sk.dim}" + (f", `{var}` 자료형 범위 {r[0]}..{r[1]}" if r else "")
        bad = _complement(ok, dom) if dom != (-INF, INF) else [(-INF, -1), (sk.dim, INF)]
        viol = _intersect(allowed, bad)
        if not viol:
            return "safe", ""
        ev = f"조건상 {fmt_set(var, allowed, dom)} → 배열 크기 {sk.dim} 을 벗어날 수 있음 ({'; '.join(texts)})"
        return ("certain" if changed else "unknown"), ev
    if sk.kind == "div":
        if c is not None:
            return ("certain", "상수 분모 0") if c == 0 else ("safe", "")
        r = E.rng(op)
        dom = r if r else (-INF, INF)
        if r and (r[0] > 0 or r[1] < 0):
            return "safe", ""
        allowed, changed, texts = _allowed(prog, guards, op, dom, seeds)
        if allowed is None:
            return "unknown", ""
        if not _intersect(allowed, [(0, 0)]):
            return "safe", ""
        return ("certain" if changed else "unknown"), f"조건상 {fmt_set(var, allowed, dom)} → 분모 `{var}` 가 0일 수 있음 ({'; '.join(texts)})"
    if sk.kind == "shift":
        w = sk.width or 32
        if c is not None:
            return ("certain", f"시프트 양 {c} — {w}비트 값") if c < 0 or c >= w else ("safe", "")
        r = E.rng(op)
        dom = r if r else (-INF, INF)
        if r and r[0] >= 0 and r[1] < w:
            return "safe", ""
        allowed, changed, texts = _allowed(prog, guards, op, dom, seeds)
        if allowed is None:
            return "unknown", ""
        viol = _intersect(allowed, [(-INF, -1), (w, INF)])
        if not viol:
            return "safe", ""
        return ("certain" if changed else "unknown"), f"조건상 {fmt_set(var, allowed, dom)} → {w}비트 이상 시프트 가능 ({'; '.join(texts)})"
    if sk.kind == "deref":
        if c is not None:
            return ("certain", "NULL(0) 주소 역참조") if c == 0 else ("safe", "")  # 상수 주소는 레지스터로 따로 본다
        try:
            targets = prog.pvals(op)
        except Exception:
            targets = set()
        if targets and all(not t[0].startswith(("sym:", "hw:", "ret:")) for t in targets):
            return "safe", ""  # 포인터 분석상 항상 이름 있는 변수를 가리킴 (대상만 달라질 수 있음)
        return "unknown", "포인터가 가리키는 대상을 분석으로 확정하지 못함"
    if sk.kind == "narrow":
        lo, hi = sk.target
        if c is not None:
            if c < lo or c > hi:
                if c == -1 and lo == 0 and isinstance(sk.expr, c_ast.Cast):
                    return "safe", ""  # (U16)-1 같은 '모든 비트 1' 관용 표기
                return "certain", f"상수 {c} 가 대상 범위 {lo}..{hi} 를 벗어남"
            return "safe", ""
        r = E.rng(op)
        if r is None:
            return "safe", ""  # 값 범위를 모르면(실수·포인터 등) 보고하지 않는다
        if lo <= r[0] and r[1] <= hi:
            return "safe", ""
        return "unknown", f"값 범위 {_fmt_num(r[0])}..{_fmt_num(r[1])} → 대상 범위 {lo}..{hi}"
    if sk.kind == "length":
        size = sk.dim
        if c is not None:
            if c < 0:
                return "certain", f"음수 길이 {c}"
            if size is not None and c > size:
                return "certain", f"상수 길이 {c} — 목적지 버퍼 {size}바이트"
            return "safe", ""
        so = _strip(op)
        if isinstance(so, c_ast.UnaryOp) and so.op == "sizeof":
            s2 = sizeof_expr(E, so.expr)
            if size is not None and s2 is not None and s2 > size:
                return "certain", f"sizeof 길이 {s2} — 목적지 버퍼 {size}바이트"
            return "safe", ""
        if size is None:
            return "unknown", "목적지 버퍼 크기를 확정하지 못함"
        r = E.rng(op)
        dom = r if r else (0, INF)
        if r and r[1] <= size:
            return "safe", ""
        allowed, changed, texts = _allowed(prog, guards, op, dom, seeds)
        if allowed is None:
            return "unknown", f"목적지 버퍼 {size}바이트, 복사 길이 `{var}` 범위 " + \
                (f"{_fmt_num(r[0])}..{_fmt_num(r[1])}" if r else "모름")
        viol = _intersect(allowed, [(size + 1, INF)])
        if not viol:
            return "safe", ""
        return ("certain" if changed else "unknown"), \
            f"조건상 {fmt_set(var, allowed, dom)} → 목적지 버퍼 {size}바이트를 넘는 복사 가능 ({'; '.join(texts)})"
    if sk.kind == "loop":
        return "unknown", ""
    if sk.kind == "fptr":
        return "unknown", ""
    return "unknown", ""


def _fmt_num(x) -> str:
    if x in (INF, -INF):
        return "∞" if x > 0 else "-∞"
    return str(int(x))


# ---------------------------------------------------------------------------
# 영향 전달 판정
# ---------------------------------------------------------------------------


def _reads(prog: Program, e) -> set:
    D: set = set()
    U: set = set()
    try:
        prog._rv(e, D, U)
    except Exception:
        return set()
    return U


class _Taint:
    def __init__(self, prog: Program, hits: dict[int, Hit]):
        self.prog, self.hits = prog, hits
        self.rdd: dict[int, list[int]] = defaultdict(list)
        for d, succs in prog.dd.items():
            if d in hits:
                for m, _ in succs:
                    self.rdd[m].append(d)
        self.defs_by_base: dict[str, list[tuple[int, tuple]]] = defaultdict(list)
        for d in hits:
            for p in prog.nodes[d].defs:
                self.defs_by_base[p[0]].append((d, p))

    def source(self, m: int, reads: set):
        """reads 중 영향받은 값을 하나 골라 (공급 노드, 경로, 간선 종류)로 돌려준다."""
        best = None
        for u in reads:
            if u[0] in self.prog.use_index:  # 흐름 비민감 메모리 (전역·static·포인터 대상)
                cands = [(d, p) for d, p in self.defs_by_base.get(u[0], ()) if d != m and overlaps(p, u)]
                kind = "memory"
            else:
                cands = [(d, p) for d in self.rdd.get(m, ()) for p in self.prog.nodes[d].defs
                         if p[0] == u[0] and overlaps(p, u)]
                kind = "data"
            for d, p in cands:
                o = self.hits[d].order
                if best is None or o < best[0]:
                    best = (o, d, u, kind)
        return None if best is None else best[1:]


# ---------------------------------------------------------------------------
# 실행
# ---------------------------------------------------------------------------


def find_risks(ctx, old: Program, rules=None) -> list[dict]:
    """ctx: analysis._Ctx (분석할 버전), old: 비교 상대 버전. 위험 연산 도달 항목 목록 (중요한 것부터).
    각 항목의 '_node'·'_sink'·'_verdict' 는 검증 단계용 내부 값이다 (JSON 으로 내보내기 전에 지운다)."""
    from .analysis import _conditions, _guards, _rel, _step

    prog, hits, cs = ctx.prog, ctx.hits, ctx.cs
    seeds = cs.seeds_new if ctx.version == "new" else cs.seeds_old
    taint = _Taint(prog, hits)
    out: dict[tuple, dict] = {}
    for m in sorted(hits, key=lambda x: hits[x].order):
        n = prog.nodes[m]
        if n.dead or not n.visible or n.kind in ("formal_in", "ginit", "actual_out"):
            continue
        sinks = sinks_of(prog, n, rules)
        if not sinks:
            continue
        E = _Env(prog, n)
        guards = _guards(prog, m)
        ch = ctx.change_by_node.get(m)
        old_keys = None
        if ch is not None and ch.kind == "changed" and ch.other_node is not None:
            old_keys = {sk.key() for sk in sinks_of(old, old.nodes[ch.other_node], rules)}
        ctrl = [(p, lab) for p, lab in prog.cd_parents.get(m, ()) if p in seeds and prog.nodes[p].kind == "cond"]
        for sk in sinks:
            verdict, evidence = _judge(E, prog, sk, guards, seeds)
            if verdict == "safe":
                continue
            reads = _reads(prog, sk.operand) if sk.kind != "loop" else _reads(prog, sk.expr)
            src = taint.source(m, reads) if reads else None
            how, path, values = None, None, []
            if src is not None:
                d, u, ekind = src
                how = "값 전달"
                values = [display_path(prog, u)]
                path = path_to(hits, d) + [Hit(m, d, (ekind, display_path(prog, u)), hits[m].order)]
            elif m in seeds and ch is not None and ch.kind == "changed" and old_keys is not None \
                    and sk.key() not in old_keys:
                how, path = "변경된 연산", [hits[m]]
            elif m in seeds and ch is not None and ch.kind == "moved":
                how, path = "실행 조건 변경", [hits[m]]
                evidence = evidence or "문장이 다른 조건 아래로 옮겨짐"
            elif ctrl:
                p, lab = ctrl[0]
                how = "실행 조건 변경"
                path = [Hit(p, None, ("seed", ""), hits[p].order if p in hits else 0),
                        Hit(m, p, ("control", lab), hits[m].order)]
                evidence = evidence or f"바뀐 조건 `{prog.nodes[p].text}` 바로 아래의 연산"
            elif m in seeds and (ch is None or ch.kind in ("added", "func_added")):
                how, path = "추가된 연산", [hits[m]]
            elif verdict == "certain":
                how, path = "영향 범위 안의 연산", path_to(hits, m)
            else:
                continue
            if how not in ("값 전달", "영향 범위 안의 연산") and verdict != "certain":
                # 피연산자 값은 그대로인 연산: 위반 가능성을 보여 주는 근거가 있을 때만 보고한다
                if sk.kind in ("deref", "fptr", "loop") or (sk.kind == "index" and not evidence):
                    continue
            key = (n.file, n.line, sk.kind, norm(sk.operand))
            row = {
                "severity": CERTAIN if verdict == "certain" else CANDIDATE,
                "kind": sk.kind,
                "kind_label": KIND_LABEL[sk.kind],
                "risk": KIND_RISK[sk.kind],
                "how": how,
                "func": prog.func_display(n.func),
                "file": _rel(prog, n.file),
                "line": n.line,
                "src": ctx.lines.get(n.file, n.line),
                "expr": gen(sk.expr),
                "operand": gen(sk.operand),
                "values": values,
                "evidence": evidence,
                "conditions": _conditions(ctx, path, m),
                "path": [_step(ctx, h) for h in path],
            }
            first = path[0].node
            fn = prog.nodes[first]
            row["origin"] = {"func": prog.func_display(fn.func), "file": _rel(prog, fn.file), "line": fn.line}
            row["_node"], row["_sink"], row["_verdict"], row["_origin_node"] = m, sk, verdict, first
            prev = out.get(key)
            if prev is None or _rank(row) < _rank(prev):
                out[key] = row
    return sorted(out.values(), key=_rank)


def _rank(r: dict) -> tuple:
    return (0 if r["severity"] == CERTAIN else 1, HOW_ORDER.index(r["how"]), _KIND_ORDER.index(r["kind"]),
            len(r["path"]), r["file"], r["line"])

