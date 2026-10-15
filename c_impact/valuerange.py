"""정수 값 범위(구간) 추론 — 경로 조건 실현 가능성, 상수 조건, 자료형 범위 초과 판정용.

지원 범위(한계 포함):
  - '변수 op 상수' 형태의 관계/동등 비교, !, &&, || 의 조합
  - 변수 = 상수, 변수 = 변수 ± 상수, ++/-- 대입의 구간 갱신
  - 그 외 식은 '알 수 없음'(자료형 전체 범위)으로 처리 — 실행 가능성을 확정하지 않는다.
"""

from __future__ import annotations

from .roles import lvalue_key

INF = None


def type_range(t: dict | None, platform=None):
    """자료형 정보 → (최솟값, 최댓값). 모르면 None."""
    if not t:
        return None
    if t.get("kind") in ("int", "enum"):
        bits = t.get("bits")
        if not bits and platform is not None:
            bits = platform.int_bits
        if not bits:
            return None
        if t.get("signed", True) and t.get("kind") == "int":
            return (-(1 << (bits - 1)), (1 << (bits - 1)) - 1)
        if t.get("kind") == "enum":
            return None
        return (0, (1 << bits) - 1)
    return None


class Env:
    def __init__(self, ranges=None):
        self.r = dict(ranges or {})

    def copy(self):
        return Env(self.r)

    def get(self, key, t=None, platform=None):
        if key in self.r:
            return self.r[key]
        return type_range(t, platform)


def _intersect(a, b):
    if a is None:
        return b
    if b is None:
        return a
    lo = max(a[0], b[0])
    hi = min(a[1], b[1])
    return (lo, hi) if lo <= hi else False


def _op_range(op, c):
    big = 1 << 70
    return {"<": (-big, c - 1), "<=": (-big, c), ">": (c + 1, big), ">=": (c, big), "==": (c, c)}.get(op)


_NEG = {"<": ">=", "<=": ">", ">": "<=", ">=": "<", "==": "!=", "!=": "=="}
_FLIP = {"<": ">", ">": "<", "<=": ">=", ">=": "<=", "==": "==", "!=": "!="}


def _strip(e):
    while e and e.get("k") == "cast":
        e = e["e"]
    return e


def _atom(e):
    """(키, 연산자, 상수, 자료형) 또는 None."""
    e = _strip(e)
    if not e or e.get("k") != "bin" or e["op"] not in _NEG:
        return None
    l, r = _strip(e["l"]), _strip(e["r"])
    if r.get("k") == "const" and r.get("v") is not None and l.get("k") != "const":
        k = lvalue_key(l)
        return (k, e["op"], r["v"], l.get("t")) if k else None
    if l.get("k") == "const" and l.get("v") is not None and r.get("k") != "const":
        k = lvalue_key(r)
        return (k, _FLIP[e["op"]], l["v"], r.get("t")) if k else None
    return None


def refine(env: Env, e, outcome: bool, platform=None):
    """조건 e 가 outcome 일 때의 환경. 모순이면 None. 해석 불가하면 env 그대로."""
    e = _strip(e)
    if e is None:
        return env
    k = e.get("k")
    if k == "const" and e.get("v") is not None:
        return env if bool(e["v"]) == outcome else None
    if k == "un" and e["op"] == "!":
        return refine(env, e["e"], not outcome, platform)
    if k == "bin" and e["op"] == "&&":
        if outcome:
            env2 = refine(env, e["l"], True, platform)
            return refine(env2, e["r"], True, platform) if env2 is not None else None
        # !(a && b): 둘 중 하나 거짓 — 각각 시도해 둘 다 모순일 때만 모순
        a = refine(env, e["l"], False, platform)
        b = refine(env, e["r"], False, platform)
        if a is None and b is None:
            return None
        return env
    if k == "bin" and e["op"] == "||":
        if not outcome:
            env2 = refine(env, e["l"], False, platform)
            return refine(env2, e["r"], False, platform) if env2 is not None else None
        a = refine(env, e["l"], True, platform)
        b = refine(env, e["r"], True, platform)
        if a is None and b is None:
            return None
        return env
    at = _atom(e)
    if at:
        key, op, c, t = at
        if not outcome:
            op = _NEG[op]
        cur = env.get(key, t, platform)
        if op == "!=":
            if cur and cur is not False and cur[0] == cur[1] == c:
                return None
            if cur and cur[0] == c and cur[1] > c:
                env2 = env.copy()
                env2.r[key] = (c + 1, cur[1])
                return env2
            if cur and cur[1] == c and cur[0] < c:
                env2 = env.copy()
                env2.r[key] = (cur[0], c - 1)
                return env2
            return env
        new = _intersect(cur, _op_range(op, c))
        if new is False:
            return None
        env2 = env.copy()
        env2.r[key] = new
        return env2
    # 단일 변수의 참/거짓
    lk = lvalue_key(e) if k in ("var", "member") else None
    if lk:
        cur = env.get(lk, e.get("t"), platform)
        if outcome:
            if cur and cur[0] == cur[1] == 0:
                return None
            return env
        new = _intersect(cur, (0, 0))
        if new is False:
            return None
        env2 = env.copy()
        env2.r[lk] = new
        return env2
    return env


def eval_cond(env: Env, e, platform=None):
    """True / False / None(판단 불가)."""
    t = refine(env, e, True, platform) is not None
    f = refine(env, e, False, platform) is not None
    if t and not f:
        return True
    if f and not t:
        return False
    if not t and not f:
        return "contradiction"
    return None


def apply_stmt(env: Env, st, platform=None, types=None) -> Env:
    """문장 실행 후 환경 갱신(대입·증감)."""
    defs = st.get("defs", [])
    if not defs:
        return env
    env = env.copy()
    e = st.get("expr")
    handled = set()
    if e is not None:
        for x in _walk(e):
            if x.get("k") == "assign":
                lk = lvalue_key(x["l"])
                if not lk:
                    continue
                t = x["l"].get("t")
                tr = type_range(t, platform)
                r = _value(env, x["r"], platform)
                if x["op"] == "=" and r is not None:
                    env.r[lk] = _clamp(r, tr)
                elif x["op"] in ("+=", "-=") and r is not None and r[0] == r[1]:
                    cur = env.get(lk, t, platform)
                    if cur:
                        d = r[0] if x["op"] == "+=" else -r[0]
                        env.r[lk] = _clamp((cur[0] + d, cur[1] + d), tr)
                    else:
                        env.r.pop(lk, None)
                else:
                    env.r.pop(lk, None)
                handled.add(lk)
            elif x.get("k") == "un" and x["op"] in ("++", "--"):
                lk = lvalue_key(x["e"])
                if not lk:
                    continue
                t = x["e"].get("t")
                cur = env.get(lk, t, platform)
                tr = type_range(t, platform)
                if cur:
                    d = 1 if x["op"] == "++" else -1
                    env.r[lk] = _clamp((cur[0] + d, cur[1] + d), tr)
                handled.add(lk)
    if st.get("kind") == "decl" and st.get("decl") and st.get("expr") is not None:
        r = _value(env, st["expr"], platform)
        if r is not None:
            env.r[st["decl"]] = r
            handled.add(st["decl"])
    for d in defs:
        if d not in handled:
            env.r.pop(d, None)
    return env


def _clamp(r, tr):
    """자료형 범위를 넘으면(순환 가능) 전체 범위로."""
    if tr is None or r is None:
        return r
    if r[0] < tr[0] or r[1] > tr[1]:
        return tr
    return r


def _value(env, e, platform):
    e = _strip(e)
    if e is None:
        return None
    if e.get("k") == "const" and e.get("v") is not None:
        return (e["v"], e["v"])
    lk = lvalue_key(e) if e.get("k") in ("var", "member") else None
    if lk:
        return env.get(lk, e.get("t"), platform)
    if e.get("k") == "bin" and e["op"] in ("+", "-"):
        a, b = _value(env, e["l"], platform), _value(env, e["r"], platform)
        if a and b:
            return (a[0] + b[0], a[1] + b[1]) if e["op"] == "+" else (a[0] - b[1], a[1] - b[0])
    return None


def _walk(e):
    from .roles import iter_expr
    return iter_expr(e)


def expr_interval(e, platform=None, env: Env | None = None):
    """식의 값 구간(보수적). 변수는 자료형 범위로 가정. 모르면 None."""
    env = env or Env()
    e = _strip_implicit(e)
    if e is None:
        return None
    k = e.get("k")
    if k == "const":
        v = e.get("v")
        return (v, v) if v is not None else None
    if k in ("var", "member", "index"):
        lk = lvalue_key(e)
        if lk and lk in env.r:
            return env.r[lk]
        return type_range(e.get("t"), platform)
    if k == "cast":
        inner = expr_interval(e["e"], platform, env)
        tr = type_range(e.get("t"), platform)
        if inner and tr and tr[0] <= inner[0] and inner[1] <= tr[1]:
            return inner
        return tr
    if k == "un" and e["op"] in ("+", "-"):
        a = expr_interval(e["e"], platform, env)
        if a is None:
            return None
        return a if e["op"] == "+" else (-a[1], -a[0])
    if k == "un" and e["op"] == "!":
        return (0, 1)
    if k == "bin":
        op = e["op"]
        if op in ("<", ">", "<=", ">=", "==", "!=", "&&", "||"):
            return (0, 1)
        a = expr_interval(e["l"], platform, env)
        b = expr_interval(e["r"], platform, env)
        if a is None or b is None:
            return None
        if op == "+":
            return (a[0] + b[0], a[1] + b[1])
        if op == "-":
            return (a[0] - b[1], a[1] - b[0])
        if op == "*":
            c = [a[0] * b[0], a[0] * b[1], a[1] * b[0], a[1] * b[1]]
            return (min(c), max(c))
        if op in ("/", "%"):
            if b[0] <= 0 <= b[1] or a[0] < 0 or b[0] < 0:
                return None
            if op == "/":
                return (a[0] // b[1], a[1] // b[0])
            return (0, min(a[1], b[1] - 1))
        if op in ("&",) and a[0] >= 0 and b[0] >= 0:
            return (0, min(a[1], b[1]))
        if op in ("|", "^") and a[0] >= 0 and b[0] >= 0:
            m = max(a[1], b[1])
            return (0, (1 << m.bit_length()) - 1)
        if op == ">>" and a[0] >= 0 and b[0] >= 0:
            return (a[0] >> b[1], a[1] >> b[0])
        if op == "<<" and a[0] >= 0 and b[0] >= 0 and b[1] < 64:
            return (a[0] << b[0], a[1] << b[1])
        return None
    if k == "cond":
        a = expr_interval(e["a"], platform, env)
        b = expr_interval(e["b"], platform, env)
        if a and b:
            return (min(a[0], b[0]), max(a[1], b[1]))
    return None


def _strip_implicit(e):
    while e and e.get("k") == "cast" and not e.get("explicit"):
        e = e["e"]
    return e
