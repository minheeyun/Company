"""C 식 출력·정규화·상수 계산."""

from __future__ import annotations

import re

from pycparser import c_ast, c_generator

_INT_RE = re.compile(r"^(0[xX][0-9a-fA-F]+|0[bB][01]+|0[0-7]*|[1-9][0-9]*)([uUlL]*)$")


def parse_int(text: str) -> int | None:
    m = _INT_RE.match(text.strip())
    if not m:
        return None
    body = m.group(1)
    if body[:2] in ("0x", "0X"):
        return int(body, 16)
    if body[:2] in ("0b", "0B"):
        return int(body[2:], 2)
    if len(body) > 1 and body[0] == "0":
        return int(body, 8)
    return int(body)


def parse_char(text: str) -> int | None:
    if len(text) >= 3 and text[0] == "'" and text[-1] == "'":
        body = text[1:-1]
        esc = {"\\n": 10, "\\r": 13, "\\t": 9, "\\0": 0, "\\\\": 92, "\\'": 39, '\\"': 34}
        if body in esc:
            return esc[body]
        if len(body) == 1:
            return ord(body)
        if body.startswith("\\x"):
            return int(body[2:], 16)
    return None


class _NormGen(c_generator.CGenerator):
    """변경 비교용: 정수 상수 표기(100U, 0x64)를 값으로 통일한다."""

    def visit_Constant(self, n):
        v = parse_int(n.value) if n.type != "char" else parse_char(n.value)
        return str(v) if v is not None else n.value


_gen = c_generator.CGenerator()
_norm = _NormGen()


def gen(node) -> str:
    if node is None:
        return ""
    try:
        return " ".join(_gen.visit(node).split())
    except Exception:
        return f"<{type(node).__name__}>"


def norm(node) -> str:
    if node is None:
        return ""
    try:
        return " ".join(_norm.visit(node).split())
    except Exception:
        return f"<{type(node).__name__}>"


def const_eval(e, enums: dict[str, int] | None = None) -> int | None:
    """컴파일 시간 정수 상수면 값을, 아니면 None 을 돌려준다."""
    enums = enums or {}
    if isinstance(e, c_ast.Constant):
        if e.type == "char":
            return parse_char(e.value)
        return parse_int(e.value)
    if isinstance(e, c_ast.ID):
        return enums.get(e.name)
    if isinstance(e, c_ast.Cast):
        return const_eval(e.expr, enums)
    if isinstance(e, c_ast.UnaryOp):
        v = const_eval(e.expr, enums) if e.op not in ("sizeof", "_Alignof") else None
        if v is None:
            return None
        return {"-": -v, "+": v, "~": ~v, "!": int(not v)}.get(e.op)
    if isinstance(e, c_ast.BinaryOp):
        a = const_eval(e.left, enums)
        if e.op == "&&" and a == 0:
            return 0
        if e.op == "||" and a not in (None, 0):
            return 1
        b = const_eval(e.right, enums)
        if a is None or b is None:
            return None
        try:
            return {
                "+": lambda: a + b, "-": lambda: a - b, "*": lambda: a * b,
                "/": lambda: int(a / b), "%": lambda: a - int(a / b) * b,
                "<<": lambda: a << b, ">>": lambda: a >> b, "&": lambda: a & b, "|": lambda: a | b, "^": lambda: a ^ b,
                "<": lambda: int(a < b), "<=": lambda: int(a <= b), ">": lambda: int(a > b), ">=": lambda: int(a >= b),
                "==": lambda: int(a == b), "!=": lambda: int(a != b), "&&": lambda: int(bool(a and b)),
                "||": lambda: int(bool(a or b)),
            }[e.op]()
        except (KeyError, ZeroDivisionError, ValueError):
            return None
    if isinstance(e, c_ast.TernaryOp):
        c = const_eval(e.cond, enums)
        if c is None:
            return None
        return const_eval(e.iftrue if c else e.iffalse, enums)
    return None


def walk(node):
    """전위 순회 (자식 순서 유지)."""
    stack = [node]
    while stack:
        n = stack.pop()
        if n is None:
            continue
        yield n
        stack.extend(reversed([c for _, c in n.children()]))
