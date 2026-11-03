"""기호 실행 (Symbolic Execution): 위험 연산까지의 실행 경로에서 안전 속성을 어기는 입력이 있는지 SMT(z3)로 찾는다.

대상은 위험 연산이 들어 있는 함수 하나다 (변경점·Taint 로 좁힌 후보만 검증한다).
  - 함수 인자·전역·static 변수는 자료형 범위 안의 임의 값(기호 변수)으로 시작한다.
  - CFG 를 따라 진입점부터 위험 연산까지 경로를 깊이 우선으로 펼치며, 대입은 기호식으로 갱신하고
    분기 조건(if·반복·switch)은 경로 제약으로 쌓는다. 불가능한 경로는 바로 잘라낸다.
  - 위험 연산에 도달하면 '안전 속성 위반'(인덱스 ≥ 배열 크기, 복사 길이 > 버퍼 크기, 분모 = 0,
    시프트 양 ≥ 비트 수, 대상 범위를 벗어난 값)을 함께 풀어 만족하는 입력(반례)을 찾는다.

결과
  violated  반례 입력이 있음 (경로 조건을 만족하면서 속성을 어김)
  holds     펼친 모든 경로에서 위반 불가 (반복문은 unroll 횟수까지만 펼침 → '제한된 범위에서 안전')
  unknown   경로 수·시간 한도 초과, z3 없음, 지원하지 않는 위험 종류

호출된 함수가 바꿀 수 있는 전역·포인터 대상은 호출 뒤 임의 값으로 본다(과대 근사).
그래서 반례는 '함수 인자·전역 값이 이렇다면' 이라는 뜻이며, 실제 호출자가 그런 값을 넘기는지는 Taint 결과와 함께 판단한다.
"""

from __future__ import annotations

import time

from pycparser import c_ast

from .cexpr import const_eval, gen, parse_int
from .program import Program, display_path

try:  # z3 는 선택 의존성이다: pip install z3-solver
    import z3  # type: ignore
except Exception:  # pragma: no cover - 설치 여부에 따라
    z3 = None

SUPPORTED = ("index", "length", "div", "shift", "narrow")


def available() -> bool:
    return z3 is not None


class SymResult(dict):
    """result, property, counterexample, paths, detail, engine"""


def property_text(kind: str, operand: str, sk) -> str:
    if kind == "index":
        return f"0 ≤ {operand} < {sk.dim}"
    if kind == "length":
        return f"{operand} ≤ {sk.dim} (목적지 버퍼 크기)"
    if kind == "div":
        return f"{operand} ≠ 0"
    if kind == "shift":
        return f"0 ≤ {operand} < {sk.width or 32}"
    if kind == "narrow":
        lo, hi = sk.target
        return f"{lo} ≤ {operand} ≤ {hi}"
    return ""


class _Unsupported(Exception):
    pass


class _State:
    __slots__ = ("env", "cons", "clob", "visits")

    def __init__(self, env, cons, clob, visits):
        self.env, self.cons, self.clob, self.visits = env, cons, clob, visits

    def copy(self):
        return _State(dict(self.env), list(self.cons), self.clob, dict(self.visits))


class _Exec:
    def __init__(self, prog: Program, func_key: str, timeout_ms: int):
        from .taint import _Env  # 순환 import 방지

        self.prog = prog
        self.f = prog.funcs[func_key]
        self.E = _Env(prog, prog.nodes[self.f.entry])
        self.init: dict[tuple, object] = {}
        self.base: list = []
        self.fresh_n = 0
        self.approx = False
        self.timeout_ms = timeout_ms
        self.names: dict[str, str] = {}

    # --- 기호 변수 ---
    def _range_of_path(self, path):
        t, env = self.prog.path_type(path)
        return self.E.type_range(t, env) if env is not None else None

    def _sym(self, name, rng):
        v = z3.Int(name)
        cons = []
        if rng:
            cons = [v >= int(rng[0]), v <= int(rng[1])]
        return v, cons

    def initial(self, path):
        if path not in self.init:
            name = display_path(self.prog, path)
            v, cons = self._sym(f"{name}#{len(self.init)}", self._range_of_path(path))
            self.names[str(v)] = name
            self.init[path] = v
            self.base += cons
        return self.init[path]

    def fresh(self, st: _State, rng=None, label="?"):
        self.fresh_n += 1
        v, cons = self._sym(f"~{label}#{self.fresh_n}", rng)
        self.names[str(v)] = label
        st.cons += cons
        return v

    def read(self, st: _State, path):
        if path in st.env:
            return st.env[path]
        v = self.prog.vars.get(path[0])
        kind = v.kind if v else ""
        if "[]" in path:
            return self.fresh(st, self._range_of_path(path), display_path(self.prog, path))
        if kind in ("local", "temp") and path[0] not in self.prog.use_index:
            val = self.fresh(st, self._range_of_path(path), display_path(self.prog, path))
            st.env[path] = val
            return val
        if st.clob and path[0] in self.prog.use_index and kind not in ("param",):
            val = self.fresh(st, self._range_of_path(path), display_path(self.prog, path))
            st.env[path] = val
            return val
        return self.initial(path)

    @staticmethod
    def wrap(val, rng):
        if not rng or rng[0] == -float("inf") or rng[1] == float("inf"):
            return val
        lo, hi = int(rng[0]), int(rng[1])
        span = hi - lo + 1
        if lo == 0:
            return val % span
        return ((val - lo) % span) + lo

    def write(self, st: _State, lv, val):
        try:
            paths = self.prog.lvals(lv)
        except Exception:
            paths = set()
        paths = {p for p in paths if not p[0].startswith("fn:")}
        if len(paths) == 1:
            (p,) = paths
            if "[]" not in p:
                st.env[p] = self.wrap(val, self._range_of_path(p))
                return
        for p in paths:  # 약한 갱신: 여러 대상이거나 배열 원소 → 이후 읽기는 임의 값
            st.env.pop(p, None)
            for k in list(st.env):
                if k[0] == p[0]:
                    st.env.pop(k, None)

    # --- 식 ---
    def truth(self, st, e):
        e2 = e
        while isinstance(e2, c_ast.Cast):
            e2 = e2.expr
        if isinstance(e2, c_ast.BinaryOp):
            op = e2.op
            if op in ("&&", "||"):
                a, b = self.truth(st, e2.left), self.truth(st, e2.right)
                return z3.And(a, b) if op == "&&" else z3.Or(a, b)
            if op in ("<", "<=", ">", ">=", "==", "!="):
                a, b = self.val(st, e2.left), self.val(st, e2.right)
                return {"<": a < b, "<=": a <= b, ">": a > b, ">=": a >= b, "==": a == b, "!=": a != b}[op]
        if isinstance(e2, c_ast.UnaryOp) and e2.op == "!":
            return z3.Not(self.truth(st, e2.expr))
        return self.val(st, e) != 0

    def val(self, st, e):
        c = const_eval(e, self.E.enums)
        if c is not None:
            return z3.IntVal(int(c))
        if isinstance(e, c_ast.Constant):
            self.approx = True
            return self.fresh(st, None, gen(e))
        if isinstance(e, c_ast.ID):
            key = self.prog.bind.get(id(e))
            if key and not key.startswith("fn:") and key in self.prog.vars:
                return self.read(st, (key,))
            self.approx = True
            return self.fresh(st, None, e.name)
        if isinstance(e, (c_ast.StructRef, c_ast.ArrayRef)) or (isinstance(e, c_ast.UnaryOp) and e.op == "*"):
            if isinstance(e, c_ast.ArrayRef):
                self.val(st, e.subscript)
            try:
                paths = {p for p in self.prog.lvals(e) if not p[0].startswith("fn:")}
            except Exception:
                paths = set()
            if len(paths) == 1:
                (p,) = paths
                return self.read(st, p)
            return self.fresh(st, self.E.lval_range(e), gen(e))
        if isinstance(e, c_ast.Cast):
            v = self.val(st, e.expr)
            rng = self.E.type_range(e.to_type)
            return self.wrap(v, rng) if rng else v
        if isinstance(e, c_ast.TernaryOp):
            return z3.If(self.truth(st, e.cond), self.val(st, e.iftrue), self.val(st, e.iffalse))
        if isinstance(e, c_ast.FuncCall):
            sid = self.prog.site_of.get(id(e))
            if sid is not None:
                return self.read(st, (f"{self.prog.sites[sid].caller}::$out{sid}",))
            return self.fresh(st, None, gen(e))
        if isinstance(e, c_ast.Assignment):
            v = self.val(st, e.rvalue)
            if e.op != "=":
                cur = self.val(st, e.lvalue)
                v = self._bin(st, e.op[:-1], cur, v, e.rvalue)
            self.write(st, e.lvalue, v)
            return v
        if isinstance(e, c_ast.UnaryOp):
            op = e.op
            if op in ("++", "--", "p++", "p--"):
                cur = self.val(st, e.expr)
                nv = cur + 1 if "+" in op else cur - 1
                self.write(st, e.expr, nv)
                return cur if op.startswith("p") else nv
            if op == "-":
                return -self.val(st, e.expr)
            if op == "+":
                return self.val(st, e.expr)
            if op == "!":
                return z3.If(self.truth(st, e.expr), 0, 1)
            if op == "~":
                return -self.val(st, e.expr) - 1
            if op in ("sizeof", "_Alignof"):
                from .taint import sizeof_expr
                s = sizeof_expr(self.E, e.expr)
                if s is not None:
                    return z3.IntVal(s)
            self.approx = True
            return self.fresh(st, None, gen(e))
        if isinstance(e, c_ast.BinaryOp):
            if e.op in ("&&", "||", "<", "<=", ">", ">=", "==", "!="):
                return z3.If(self.truth(st, e), 1, 0)
            return self._bin(st, e.op, self.val(st, e.left), self.val(st, e.right), e.right)
        if isinstance(e, c_ast.ExprList) and e.exprs:
            for x in e.exprs[:-1]:
                self.val(st, x)
            return self.val(st, e.exprs[-1])
        self.approx = True
        return self.fresh(st, None, type(e).__name__)

    def _bin(self, st, op, a, b, right_ast):
        rc = const_eval(right_ast, self.E.enums) if right_ast is not None else None
        if op == "+":
            return a + b
        if op == "-":
            return a - b
        if op == "*":
            return a * b
        if op in ("/", "%"):
            # C 는 0 쪽으로 자른다: 음수 피제수는 부호를 뒤집어 계산
            q = z3.If(a >= 0, a / b, -((-a) / b)) if rc is None or rc > 0 else a / b
            if op == "/":
                return q
            return a - q * b
        if op == "<<" and rc is not None and 0 <= rc < 64:
            return a * (2 ** int(rc))
        if op == ">>" and rc is not None and 0 <= rc < 64:
            return a / (2 ** int(rc))
        if op == "&" and rc is not None and rc >= 0 and (rc + 1) & rc == 0:
            return a % (rc + 1)
        self.approx = True
        return self.fresh(st, None, f"({op})")

    # --- 문장 ---
    def effect(self, st: _State, nid: int):
        n = self.prog.nodes[nid]
        e = n.ast
        if n.kind == "stmt":
            if isinstance(e, c_ast.Decl):
                if e.init is not None and not isinstance(e.init, c_ast.InitList):
                    v = self.val(st, e.init)
                    st.env[(n.var,)] = self.wrap(v, self._range_of_path((n.var,)))
                else:
                    st.env.pop((n.var,), None)
            else:
                self.val(st, e)
        elif n.kind in ("call", "extcall"):
            site = self.prog.sites[n.site]
            args = site.ast.args.exprs if site.ast.args else []
            for a in args:
                try:
                    objs = self.prog.pvals(a)
                except Exception:
                    objs = set()
                for o in objs:
                    for k in [k for k in st.env if k[0] == o[0]]:
                        st.env.pop(k, None)
            st.clob = True
            for k in [k for k in st.env if k[0] in self.prog.use_index]:
                st.env.pop(k, None)
            if n.kind == "extcall":
                out = (f"{site.caller}::$out{site.id}",)
                st.env[out] = self.fresh(st, self.E.ret_range(site.ast), f"{site.name}() 반환")
        elif n.kind == "actual_out":
            site = self.prog.sites[n.site]
            out = (f"{site.caller}::$out{site.id}",)
            st.env[out] = self.fresh(st, self.E.ret_range(site.ast), f"{site.name}() 반환")

    def branch(self, st: _State, nid: int, label):
        n = self.prog.nodes[nid]
        if n.kind != "cond" or label is None:
            return None
        if label in ("T", "F"):
            t = self.truth(st, n.ast)
            return t if label == "T" else z3.Not(t)
        v = self.val(st, n.ast)
        if label == "default":
            return z3.And(*[v != int(c) for c in (n.cases or [])]) if n.cases else None
        if isinstance(label, str) and label.startswith("case "):
            txt = label[5:].strip()
            k = parse_int(txt.rstrip("uUlL")) if txt else None
            if k is None:
                k = self.E.enums.get(txt)
            if k is None:
                try:
                    from pycparser import CParser
                    k = const_eval(CParser().parse(f"int _x = {txt};").ext[0].init, self.E.enums)
                except Exception:
                    k = None
            return (v == int(k)) if k is not None else None
        return None

    # --- 검사 ---
    def feasible(self, cons) -> str:
        s = z3.Solver()
        s.set("timeout", self.timeout_ms)
        s.add(*self.base)
        s.add(*cons)
        r = s.check()
        return "sat" if r == z3.sat else ("unsat" if r == z3.unsat else "unknown")

    def model(self, cons):
        s = z3.Solver()
        s.set("timeout", self.timeout_ms)
        s.add(*self.base)
        s.add(*cons)
        if s.check() != z3.sat:
            return None
        m = s.model()
        out = {}
        for d in m.decls():
            name = self.names.get(d.name(), d.name())
            if d.name().startswith("~") and "반환" not in name:
                continue
            try:
                out[name] = m[d].as_long()
            except Exception:
                out[name] = str(m[d])
        return out


def verify(prog: Program, nid: int, sk, *, max_paths: int = 64, unroll: int = 2, timeout_ms: int = 3000,
           budget_s: float = 10.0) -> SymResult:
    """위험 연산 노드 nid 의 sink(sk)에 대한 안전 속성을 기호 실행으로 검증한다."""
    n = prog.nodes[nid]
    prop = property_text(sk.kind, gen(sk.operand), sk)
    res = SymResult(result="unknown", property=prop, counterexample=None, paths=0, detail="", engine="z3")
    if z3 is None:
        res.update(engine="없음", detail="z3 가 설치되어 있지 않아 기호 실행을 건너뜀 (pip install z3-solver)")
        return res
    if sk.kind not in SUPPORTED or (sk.kind in ("index", "length") and sk.dim is None) or not n.func:
        res.update(detail="이 위험 종류·피연산자는 기호 실행 대상이 아님")
        return res
    ex = _Exec(prog, n.func, timeout_ms)
    f = ex.f
    t0 = time.time()
    stack = [(f.entry, _State({}, [], False, {}))]
    complete, bounded = True, False
    reached = 0
    while stack:
        if time.time() - t0 > budget_s or reached >= max_paths:
            complete = False
            break
        cur, st = stack.pop()
        st.visits[cur] = st.visits.get(cur, 0) + 1
        if cur == nid:
            reached += 1
            try:
                op = ex.val(st, sk.operand)
                viol = _violation(sk, op)
            except _Unsupported:
                complete = False
                continue
            except Exception as e:  # 기호화하지 못한 구문
                res.update(detail=f"기호화 실패: {type(e).__name__}")
                complete = False
                continue
            cons = st.cons + [viol]
            r = ex.feasible(cons)
            if r == "sat":
                cex = ex.model(cons) or {}
                res.update(result="violated", counterexample=cex, paths=reached,
                           detail=f"경로 {reached}개 탐색 중 위반 경로 발견" + (" (일부 값은 근사)" if ex.approx else ""))
                return res
            if r == "unknown":
                complete = False
            continue
        try:
            ex.effect(st, cur)
        except Exception:
            ex.approx = True
        succs = f.succ.get(cur, [])
        for nxt, label in reversed(succs):
            if prog.nodes[nxt].dead:
                continue
            if st.visits.get(nxt, 0) >= unroll:
                bounded = True
                continue
            st2 = st.copy() if len(succs) > 1 else st
            try:
                c = ex.branch(st2, cur, label)
            except Exception:
                c = None
                ex.approx = True
            if c is not None:
                st2.cons.append(c)
                if len(succs) > 1 and ex.feasible(st2.cons) == "unsat":
                    continue
            stack.append((nxt, st2))
    res.update(paths=reached)
    if not reached:
        res.update(detail="진입점에서 위험 연산까지 가능한 경로를 찾지 못함" + ("" if complete else " (탐색 한도 초과)"))
        res["result"] = "holds" if complete else "unknown"
        return res
    if complete:
        res.update(result="holds", detail=f"경로 {reached}개 모두 위반 불가" +
                   (f" (반복문은 {unroll}회까지 펼침)" if bounded else ""))
    else:
        res.update(detail=f"경로 {reached}개에서는 위반 없음, 탐색 한도(경로 {max_paths}개·{budget_s:.0f}초) 초과로 판정 보류")
    return res


def _violation(sk, op):
    if sk.kind == "index":
        return z3.Or(op < 0, op >= int(sk.dim))
    if sk.kind == "length":
        return op > int(sk.dim)
    if sk.kind == "div":
        return op == 0
    if sk.kind == "shift":
        return z3.Or(op < 0, op >= int(sk.width or 32))
    if sk.kind == "narrow":
        lo, hi = sk.target
        return z3.Or(op < int(lo), op > int(hi))
    raise _Unsupported()
