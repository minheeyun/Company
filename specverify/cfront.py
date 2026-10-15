"""Embedded C 소스 → 분석용 사실(fact) 모델.

처리 순서
  1. 원본 스캔: 주석 제거, #define(매크로 값), #include, 조건부 컴파일 구간
  2. 전처리: gcc -E (없으면 cpp). 누락된 헤더는 빈 스텁으로, 컴파일러 확장 키워드는 prelude로 무력화
     - 사용자가 준 defines/include_dirs 를 그대로 적용 → 실제 빌드 구성 기준의 활성 코드만 분석
     - linemarker로 '전처리 후 살아남은 원본 라인'을 계산 → 비활성(#if 0 등) 라인 표시
  3. pycparser AST 순회: 함수 범위, 호출, 전역/정적 변수 접근(read/write/rmw), 조건식,
     대입문, 상태 전이(switch-case + 상태 변수 대입), 그리고 checks.py가 쓸 이벤트(가드 포함)
  4. 자료형은 typedef 체인을 끝까지 풀고 env.Target 기준 실제 비트 폭으로 환산

파싱에 실패한 파일은 보고서의 '분석 제한'에 남기고 정규식 기반 함수 위치만 추출한다.
"""

from __future__ import annotations

import ast as pyast
import os
import re
import shutil
import subprocess
import tempfile
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from .env import Environment

try:  # pycparser는 선택 의존성: 없으면 정규식 기반 축소 분석
    from pycparser import c_ast, c_generator, c_parser
except ImportError:  # pragma: no cover - 환경에 따라
    c_ast = c_generator = c_parser = None

C_EXT = {".c"}
H_EXT = {".h"}

# ---------------------------------------------------------------------------
# 자료형
# ---------------------------------------------------------------------------


@dataclass
class TypeInfo:
    text: str                       # 선언 표기 (예: "uint8", "uint16 *")
    kind: str                       # int | bool | enum | float | struct | union | pointer | array | void | func | unknown
    bits: int | None = None
    signed: bool | None = None
    volatile: bool = False
    const: bool = False
    enum: str | None = None
    struct: str | None = None
    elem: "TypeInfo | None" = None
    dims: list = field(default_factory=list)
    base: str = ""                  # typedef를 푼 기본형 (예: "unsigned char")

    @property
    def is_integer(self) -> bool:
        return self.kind in ("int", "bool", "enum") and self.bits is not None

    def int_range(self) -> tuple[int, int] | None:
        if self.kind == "bool":
            return (0, 1)
        if not self.is_integer:
            return None
        if self.signed:
            return (-(1 << (self.bits - 1)), (1 << (self.bits - 1)) - 1)
        return (0, (1 << self.bits) - 1)

    def describe(self) -> str:
        parts = [self.text]
        extra = []
        if self.base and self.base != self.text:
            extra.append(self.base)
        if self.kind == "array" and self.elem is not None:
            extra.append(f"{self.elem.text}[{']['.join('?' if d is None else str(d) for d in self.dims)}]")
        r = self.int_range()
        if r and self.kind != "enum":
            extra.append(f"{self.bits}bit {'signed' if self.signed else 'unsigned'} {r[0]}..{r[1]}")
        if self.volatile:
            extra.append("volatile")
        if self.const:
            extra.append("const")
        return parts[0] + (f" ({', '.join(extra)})" if extra else "")

    def to_dict(self) -> dict:
        d = {"text": self.text, "kind": self.kind, "bits": self.bits, "signed": self.signed,
             "volatile": self.volatile, "const": self.const, "base": self.base}
        if self.enum:
            d["enum"] = self.enum
        if self.struct:
            d["struct"] = self.struct
        if self.dims:
            d["dims"] = self.dims
        if self.elem:
            d["elem"] = self.elem.to_dict()
        return d


UNKNOWN = TypeInfo(text="?", kind="unknown")

# ---------------------------------------------------------------------------
# 사실 레코드
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Guard:
    """해당 문장을 실행하기 위해 참이어야 하는 조건 (negated면 조건식의 부정)."""
    text: str
    line: int
    negated: bool
    kind: str               # if | loop | case | early-exit | ternary
    node: object = field(default=None, compare=False, hash=False, repr=False)

    def render(self) -> str:
        return f"!({self.text})" if self.negated else self.text


@dataclass
class VarInfo:
    key: str                # 전역 고유 키 (정적 전역은 "file::name")
    name: str
    type: TypeInfo
    file: str
    line: int
    scope: str              # global | static | extern | local | static_local | param
    function: str | None = None
    init: str | None = None  # 초기화식 (전처리 후)

    @property
    def persistent(self) -> bool:
        return self.scope in ("global", "static", "extern", "static_local")


@dataclass
class Access:
    var: str                # VarInfo.key
    name: str
    mode: str               # read | write | rmw | addr
    file: str
    line: int
    col: int
    function: str
    guards: tuple = ()


@dataclass
class CallSite:
    caller: str
    callee: str
    file: str
    line: int
    args: str
    guards: tuple = ()


@dataclass
class Condition:
    kind: str               # if | while | for | do | switch | ternary
    file: str
    line: int
    function: str
    text: str               # 전처리(매크로 확장) 후 조건식
    raw: str                # 원본 라인 (주석 제거)
    vars: list = field(default_factory=list)
    macros: list = field(default_factory=list)


@dataclass
class Assign:
    file: str
    line: int
    function: str
    target: str
    target_var: str | None
    op: str
    value: str
    raw: str
    guards: tuple = ()


@dataclass
class Transition:
    state_var: str
    from_states: list
    to_state: str
    file: str
    line: int
    function: str
    guards: tuple = ()


@dataclass
class Event:
    """checks.py용 원시 이벤트 (AST 노드 포함)."""
    kind: str               # incdec | compound | assign | binop | index | deref | compare | cast
    node: object
    file: str
    line: int
    function: str
    guards: tuple
    fn_key: str = ""
    tu: object = None       # _TU (상수·자료형 해석)
    names: dict = field(default_factory=dict)  # 이벤트 시점의 식별자 → VarInfo


@dataclass
class SwitchInfo:
    file: str
    line: int
    function: str
    var: str | None         # switch 대상 변수 키
    expr: str
    labels: list
    has_default: bool


@dataclass
class Function:
    name: str
    file: str
    start: int
    end: int
    signature: str
    static: bool
    return_type: TypeInfo = field(default_factory=lambda: UNKNOWN)
    params: list = field(default_factory=list)     # [VarInfo]
    locals: list = field(default_factory=list)     # [VarInfo]
    calls: list = field(default_factory=list)      # [CallSite]
    accesses: list = field(default_factory=list)   # [Access]
    conditions: list = field(default_factory=list)
    assigns: list = field(default_factory=list)
    parsed: bool = True

    @property
    def key(self) -> str:
        return f"{self.file}::{self.name}" if self.static else self.name


@dataclass
class Macro:
    name: str
    value: str
    file: str
    line: int
    params: str | None = None
    numeric: float | int | None = None


@dataclass
class CondRegion:
    file: str
    start: int
    end: int
    directive: str
    active: bool | None


@dataclass
class EnumInfo:
    name: str
    members: dict
    file: str
    line: int


@dataclass
class SourceFile:
    path: str               # 표시용 상대 경로
    abspath: str
    lines: list             # 원본
    code: list              # 주석 제거본 (라인 수 동일)
    active: set = field(default_factory=set)
    preprocessed: bool = False

    def line(self, n: int) -> str:
        return self.lines[n - 1] if 1 <= n <= len(self.lines) else ""

    def is_inactive(self, n: int) -> bool:
        """전처리 결과에서 사라진 코드 라인 (조건부 컴파일로 제외)."""
        if not self.preprocessed or not (1 <= n <= len(self.code)):
            return False
        s = self.code[n - 1].strip()
        if not s or s.startswith("#") or s in ("{", "}", "};"):
            return False
        return n not in self.active


@dataclass
class ParseIssue:
    file: str
    message: str
    severity: str = "warning"


@dataclass
class CodeModel:
    root: str
    env: Environment
    files: dict = field(default_factory=dict)        # path -> SourceFile
    functions: dict = field(default_factory=dict)    # key -> Function
    variables: dict = field(default_factory=dict)    # key -> VarInfo (전역/정적만)
    macros: dict = field(default_factory=dict)       # name -> Macro
    enums: dict = field(default_factory=dict)        # name -> EnumInfo
    enum_values: dict = field(default_factory=dict)  # member -> int
    typedefs: dict = field(default_factory=dict)     # name -> TypeInfo
    regions: list = field(default_factory=list)
    transitions: list = field(default_factory=list)
    switches: list = field(default_factory=list)
    prototypes: set = field(default_factory=set)     # 선언(프로토타입)만 있는 함수 이름
    events: list = field(default_factory=list)
    issues: list = field(default_factory=list)
    preprocessor: str = ""

    # --- 조회 ---------------------------------------------------------------
    def find_function(self, name: str, file: str | None = None) -> Function | None:
        if file:
            f = self.functions.get(f"{file}::{name}")
            if f:
                return f
        f = self.functions.get(name)
        if f:
            return f
        for fn in self.functions.values():
            if fn.name == name and (file is None or fn.file == file):
                return fn
        return None

    def function_at(self, file: str, line: int) -> Function | None:
        for fn in self.functions.values():
            if fn.file == file and fn.start <= line <= fn.end:
                return fn
        return None

    def resolve_file(self, name: str) -> str | None:
        if not name:
            return None
        name = name.replace("\\", "/").lstrip("./")
        if name in self.files:
            return name
        cands = [p for p in self.files if p.endswith("/" + name) or name.endswith("/" + p) or Path(p).name == name]
        return cands[0] if len(cands) == 1 else None

    def callees(self, fn: Function) -> list[Function]:
        out, seen = [], set()
        for c in fn.calls:
            g = self.find_function(c.callee, fn.file)
            if g and g.key not in seen:
                seen.add(g.key)
                out.append(g)
        return out

    def callers(self, fn: Function) -> list[Function]:
        out = []
        for g in self.functions.values():
            if any(c.callee == fn.name and self.find_function(c.callee, g.file) is fn for c in g.calls):
                out.append(g)
        return out

    def reachable(self, entry: str) -> list[Function]:
        start = self.find_function(entry)
        if not start:
            return []
        seen, stack, out = {start.key}, [start], []
        while stack:
            fn = stack.pop()
            out.append(fn)
            for g in self.callees(fn):
                if g.key not in seen:
                    seen.add(g.key)
                    stack.append(g)
        return out

    def context_map(self) -> dict:
        """함수 키 -> 실행 컨텍스트 이름 목록 (env.contexts의 진입 함수에서 호출 그래프로 도달 가능한 함수)"""
        cache = getattr(self, "_ctx_cache", None)
        if cache is None:
            cache = defaultdict(list)
            for ctx in self.env.contexts:
                for entry in ctx.entries:
                    for fn in self.reachable(entry):
                        if ctx.name not in cache[fn.key]:
                            cache[fn.key].append(ctx.name)
            self._ctx_cache = cache
        return cache

    def accesses_of(self, var_key: str) -> list[Access]:
        return [a for fn in self.functions.values() for a in fn.accesses if a.var == var_key]


# ---------------------------------------------------------------------------
# 원본 스캔
# ---------------------------------------------------------------------------


def strip_comments(text: str, blank_strings: bool = False) -> str:
    """주석을 공백으로 치환 (줄 수 유지). blank_strings면 문자열/문자 리터럴 내용도 공백 처리."""
    out = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c == "/" and i + 1 < n and text[i + 1] == "/":
            j = text.find("\n", i)
            j = n if j < 0 else j
            out.append(" " * (j - i))
            i = j
        elif c == "/" and i + 1 < n and text[i + 1] == "*":
            j = text.find("*/", i + 2)
            j = n if j < 0 else j + 2
            out.append("".join("\n" if ch == "\n" else " " for ch in text[i:j]))
            i = j
        elif c in "\"'":
            j = i + 1
            while j < n and text[j] != c and text[j] != "\n":
                j += 2 if text[j] == "\\" else 1
            j = min(j + 1, n)
            if blank_strings:
                out.append(c + " " * max(0, j - i - 2) + (c if j - i >= 2 else ""))
            else:
                out.append(text[i:j])
            i = j
        else:
            out.append(c)
            i += 1
    return "".join(out)


_INT_SUFFIX = re.compile(r"(?<=[0-9a-fA-F])([uU][lL]{0,2}|[lL]{1,2}[uU]?)\b")
_CAST = re.compile(r"\(\s*(?:const\s+)?(?:unsigned|signed|u?int\d*(?:_t)?|uint\d+|sint\d+|[US]\d+|char|short|int|long|float\d*|boolean)\s*\)")


def eval_const_text(expr: str, macros: dict | None = None, depth: int = 0) -> int | float | None:
    """매크로 본문 같은 C 상수식 문자열을 평가 (실패 시 None)."""
    if depth > 16:
        return None
    s = expr.strip()
    if not s:
        return None
    if macros:
        def sub(m):
            name = m.group(0)
            mac = macros.get(name)
            if mac is None or mac.params is not None:
                return name
            v = eval_const_text(mac.value, macros, depth + 1)
            return name if v is None else f"({v})"
        s = re.sub(r"\b[A-Za-z_]\w*\b", sub, s)
    s = _CAST.sub("", s)
    s = re.sub(r"'(\\?.)'", lambda m: str(ord(m.group(1)[-1])), s)
    s = _INT_SUFFIX.sub("", s)
    s = re.sub(r"(?<![\w.])0([0-7]+)\b", r"0o\1", s)
    s = re.sub(r"(\d+\.?\d*)[fF]\b", r"\1", s)
    s = s.replace("&&", " and ").replace("||", " or ")
    s = re.sub(r"!(?!=)", " not ", s)
    try:
        tree = pyast.parse(s, mode="eval")
    except SyntaxError:
        return None
    return _eval_py(tree.body)


def _eval_py(n):
    if isinstance(n, pyast.Constant) and isinstance(n.value, (int, float)) and not isinstance(n.value, bool):
        return n.value
    if isinstance(n, pyast.UnaryOp):
        v = _eval_py(n.operand)
        if v is None:
            return None
        if isinstance(n.op, pyast.USub):
            return -v
        if isinstance(n.op, pyast.UAdd):
            return v
        if isinstance(n.op, pyast.Invert) and isinstance(v, int):
            return ~v
        if isinstance(n.op, pyast.Not):
            return int(not v)
        return None
    if isinstance(n, pyast.BinOp):
        a, b = _eval_py(n.left), _eval_py(n.right)
        if a is None or b is None:
            return None
        try:
            if isinstance(n.op, pyast.Add):
                return a + b
            if isinstance(n.op, pyast.Sub):
                return a - b
            if isinstance(n.op, pyast.Mult):
                return a * b
            if isinstance(n.op, pyast.Div):
                if isinstance(a, int) and isinstance(b, int):
                    return int(a / b) if b else None
                return a / b if b else None
            if isinstance(n.op, pyast.Mod) and isinstance(a, int) and isinstance(b, int):
                return (abs(a) % abs(b)) * (1 if a >= 0 else -1) if b else None
            if isinstance(a, int) and isinstance(b, int):
                if isinstance(n.op, pyast.LShift):
                    return a << b if 0 <= b < 128 else None
                if isinstance(n.op, pyast.RShift):
                    return a >> b if b >= 0 else None
                if isinstance(n.op, pyast.BitOr):
                    return a | b
                if isinstance(n.op, pyast.BitAnd):
                    return a & b
                if isinstance(n.op, pyast.BitXor):
                    return a ^ b
        except (OverflowError, ZeroDivisionError):
            return None
        return None
    if isinstance(n, pyast.Compare) and len(n.ops) == 1:
        a, b = _eval_py(n.left), _eval_py(n.comparators[0])
        if a is None or b is None:
            return None
        op = n.ops[0]
        table = {pyast.Lt: a < b, pyast.LtE: a <= b, pyast.Gt: a > b, pyast.GtE: a >= b,
                 pyast.Eq: a == b, pyast.NotEq: a != b}
        for k, v in table.items():
            if isinstance(op, k):
                return int(v)
    if isinstance(n, pyast.BoolOp):
        vals = [_eval_py(v) for v in n.values]
        if any(v is None for v in vals):
            return None
        return int(all(vals)) if isinstance(n.op, pyast.And) else int(any(vals))
    if isinstance(n, pyast.IfExp):
        c = _eval_py(n.test)
        if c is None:
            return None
        return _eval_py(n.body if c else n.orelse)
    return None


_DIRECTIVE = re.compile(r"^\s*#\s*(\w+)\s*(.*)$")


def scan_directives(sf: SourceFile):
    """(#define 목록, #include 목록, 조건부 컴파일 구간)"""
    defines, includes, regions = [], [], []
    stack: list[list] = []  # [start_line, directive_text]
    i = 0
    code = sf.code
    while i < len(code):
        start = i
        line = code[i]
        while line.rstrip().endswith("\\") and i + 1 < len(code):
            i += 1
            line = line.rstrip()[:-1] + " " + code[i]
        i += 1
        m = _DIRECTIVE.match(line)
        if not m:
            continue
        d, rest = m.group(1), m.group(2).strip()
        lineno = start + 1
        if d == "define":
            mm = re.match(r"([A-Za-z_]\w*)(\([^)]*\))?\s*(.*)$", rest)
            if mm:
                defines.append(Macro(name=mm.group(1), value=mm.group(3).strip(), file=sf.path, line=lineno,
                                     params=mm.group(2)))
        elif d == "include":
            mm = re.match(r'["<]([^">]+)[">]', rest)
            if mm:
                includes.append((mm.group(1), lineno, rest.startswith("<")))
        elif d in ("if", "ifdef", "ifndef"):
            stack.append([lineno, f"#{d} {rest}"])
        elif d in ("elif", "else"):
            if stack:
                s, txt = stack[-1]
                regions.append(CondRegion(sf.path, s, lineno, txt, None))
                stack[-1] = [lineno, f"#{d} {rest}".strip() + f"  (← {txt})"]
        elif d == "endif":
            if stack:
                s, txt = stack.pop()
                regions.append(CondRegion(sf.path, s, lineno, txt, None))
    return defines, includes, regions


# ---------------------------------------------------------------------------
# 전처리
# ---------------------------------------------------------------------------

STD_HEADERS = {
    "stdint.h": """typedef signed char int8_t; typedef unsigned char uint8_t;
typedef short int16_t; typedef unsigned short uint16_t;
typedef int int32_t; typedef unsigned int uint32_t;
typedef long long int64_t; typedef unsigned long long uint64_t;
typedef unsigned long uintptr_t; typedef long intptr_t;
#define UINT8_MAX 255U
#define UINT16_MAX 65535U
#define UINT32_MAX 4294967295UL
#define INT8_MAX 127
#define INT16_MAX 32767
#define INT32_MAX 2147483647L
""",
    "stdbool.h": "#define bool _Bool\n#define true 1\n#define false 0\n",
    "stddef.h": "typedef unsigned long size_t; typedef long ptrdiff_t;\n#define NULL ((void*)0)\n#define offsetof(t, m) ((size_t)&((t*)0)->m)\n",
    "string.h": "typedef unsigned long size_t;\nvoid *memcpy(void *d, const void *s, size_t n);\nvoid *memset(void *d, int c, size_t n);\nint memcmp(const void *a, const void *b, size_t n);\n",
    "limits.h": "#define CHAR_BIT 8\n#define UCHAR_MAX 255\n#define USHRT_MAX 65535\n#define INT_MAX 2147483647\n#define UINT_MAX 4294967295U\n",
    "stdarg.h": "typedef char *va_list;\n",
    "stdio.h": "typedef struct _FILE FILE;\nint printf(const char *f, ...);\n",
    "stdlib.h": "typedef unsigned long size_t;\n#define NULL ((void*)0)\n",
}

# 소스에 정의가 없을 때만 주입하는 흔한 임베디드/AUTOSAR 자료형
BUILTIN_TYPEDEFS = {
    "uint8": "unsigned char", "uint16": "unsigned short", "uint32": "unsigned long",
    "uint64": "unsigned long long", "sint8": "signed char", "sint16": "signed short",
    "sint32": "signed long", "sint64": "signed long long", "boolean": "unsigned char",
    "float32": "float", "float64": "double", "Std_ReturnType": "unsigned char",
    "U8": "unsigned char", "U16": "unsigned short", "U32": "unsigned long",
    "S8": "signed char", "S16": "signed short", "S32": "signed long",
    "u8": "unsigned char", "u16": "unsigned short", "u32": "unsigned long",
    "s8": "signed char", "s16": "signed short", "s32": "signed long",
    "uint8_t": "unsigned char", "uint16_t": "unsigned short", "uint32_t": "unsigned int",
    "int8_t": "signed char", "int16_t": "short", "int32_t": "int", "bool_t": "unsigned char",
}

PRELUDE = r"""
#define __attribute__(x)
#define __attribute(x)
#define __declspec(x)
#define __asm__(...)
#define __asm(...)
#define asm(...)
#define __interrupt
#define __irq
#define __near
#define __far
#define _near
#define _far
#define __inline inline
#define __inline__ inline
#define __forceinline inline
#define __restrict
#define __restrict__
#define __extension__
#define __volatile__ volatile
#define __const const
#define __signed__ signed
#define _Pragma(x)
#define __builtin_va_list char*
#define __int64 long long
#define _Noreturn
#define _Static_assert(...)
"""

AUTOSAR_ABSTRACTION = r"""
#ifndef FUNC
#define FUNC(rettype, memclass) rettype
#endif
#ifndef FUNC_P2VAR
#define FUNC_P2VAR(rettype, ptrclass, memclass) rettype *
#endif
#ifndef FUNC_P2CONST
#define FUNC_P2CONST(rettype, ptrclass, memclass) const rettype *
#endif
#ifndef P2VAR
#define P2VAR(ptrtype, memclass, ptrclass) ptrtype *
#endif
#ifndef P2CONST
#define P2CONST(ptrtype, memclass, ptrclass) const ptrtype *
#endif
#ifndef CONSTP2VAR
#define CONSTP2VAR(ptrtype, memclass, ptrclass) ptrtype * const
#endif
#ifndef CONSTP2CONST
#define CONSTP2CONST(ptrtype, memclass, ptrclass) const ptrtype * const
#endif
#ifndef P2FUNC
#define P2FUNC(rettype, ptrclass, fctname) rettype (*fctname)
#endif
#ifndef VAR
#define VAR(vartype, memclass) vartype
#endif
#ifndef CONST
#define CONST(consttype, memclass) const consttype
#endif
#ifndef STATIC
#define STATIC static
#endif
#ifndef INLINE
#define INLINE inline
#endif
#ifndef LOCAL_INLINE
#define LOCAL_INLINE static inline
#endif
"""

_TYPEDEF_NAME = re.compile(r"\btypedef\b[^;{]*?(?:\{[^}]*\}\s*)?\**\s*\(?\s*\*?\s*([A-Za-z_]\w*)\s*\)?\s*(?:\[[^\]]*\]\s*)*(?:\([^)]*\)\s*)?;", re.S)


def _find_preprocessor() -> list[str] | None:
    for cand in (os.environ.get("SPECVERIFY_CPP"), "gcc", "clang", "cpp"):
        if not cand:
            continue
        exe = shutil.which(cand.split()[0])
        if exe:
            args = [exe] + cand.split()[1:]
            return args + (["-E"] if Path(exe).name != "cpp" else [])
    return None


_LINEMARK = re.compile(r'^#\s*(?:line\s+)?(\d+)\s+"((?:[^"\\]|\\.)*)"')


def _active_lines(pp_text: str) -> dict[str, set[int]]:
    """전처리 출력의 linemarker를 따라 (원본 파일 → 출력에 남은 라인 집합)을 만든다."""
    active: dict[str, set[int]] = defaultdict(set)
    cur, ln = None, 0
    for line in pp_text.split("\n"):
        m = _LINEMARK.match(line)
        if m:
            ln = int(m.group(1))
            cur = os.path.normpath(m.group(2))
            active[cur]  # 등록만
            continue
        if cur is not None and line.strip():
            active[cur].add(ln)
        ln += 1
    return active


# ---------------------------------------------------------------------------
# AST → 모델
# ---------------------------------------------------------------------------

class _TU:
    """번역 단위 하나의 typedef/struct/enum 정보와 자료형 해석기."""

    def __init__(self, model: CodeModel):
        self.model = model
        self.t = model.env.target
        self.typedef_nodes: dict = {}
        self.structs: dict = {}         # key -> {member: type node}
        self.enum_values: dict = {}
        self.enum_ranges: dict = {}     # enum name -> (min, max)
        self.anon_names: dict = {}      # id(익명 enum/struct 노드) -> typedef 이름
        self._memo: dict = {}

    def builtin(self, names: list[str]) -> TypeInfo:
        ns = [n for n in names if n not in ("const", "volatile", "register", "static", "extern", "auto", "inline")]
        txt = " ".join(ns)
        s = set(ns)
        t = self.t
        if "void" in s:
            return TypeInfo(txt, "void", base=txt)
        if "_Bool" in s:
            return TypeInfo(txt, "bool", bits=8, signed=False, base=txt)
        if "float" in s:
            return TypeInfo(txt, "float", bits=32, base=txt)
        if "double" in s:
            return TypeInfo(txt, "float", bits=64, base=txt)
        unsigned = "unsigned" in s
        if "char" in s:
            signed = (not unsigned) if ("signed" in s or "unsigned" in s) else t.char_signed
            return TypeInfo(txt, "int", bits=8, signed=signed, base=txt)
        if "short" in s:
            return TypeInfo(txt, "int", bits=t.short_bits, signed=not unsigned, base=txt)
        if ns.count("long") >= 2:
            return TypeInfo(txt, "int", bits=t.long_long_bits, signed=not unsigned, base=txt)
        if "long" in s:
            return TypeInfo(txt, "int", bits=t.long_bits, signed=not unsigned, base=txt)
        if s & {"int", "signed", "unsigned"}:
            return TypeInfo(txt, "int", bits=t.int_bits, signed=not unsigned, base=txt)
        return TypeInfo(txt or "?", "unknown", base=txt)

    def resolve(self, node, quals=()) -> TypeInfo:
        """pycparser 타입 노드 → TypeInfo"""
        if node is None:
            return UNKNOWN
        if isinstance(node, c_ast.TypeDecl):
            ti = self.resolve(node.type)
            q = set(node.quals or []) | set(quals)
            if q & {"volatile", "const"}:
                ti = TypeInfo(**{**ti.__dict__})
                ti.volatile = ti.volatile or "volatile" in q
                ti.const = ti.const or "const" in q
            return ti
        if isinstance(node, c_ast.Typename) or isinstance(node, c_ast.Decl):
            return self.resolve(node.type, tuple(getattr(node, "quals", None) or ()))
        if isinstance(node, c_ast.IdentifierType):
            names = list(node.names)
            if len(names) == 1 and names[0] in self.typedef_nodes:
                name = names[0]
                if name not in self._memo:
                    self._memo[name] = UNKNOWN  # 순환 방지
                    base = self.resolve(self.typedef_nodes[name])
                    ti = TypeInfo(**{**base.__dict__})
                    ti.text = name
                    ti.base = base.base or base.text
                    if ti.kind == "struct" and not ti.struct:
                        ti.struct = name
                    self._memo[name] = ti
                return self._memo[name]
            return self.builtin(names)
        if isinstance(node, c_ast.PtrDecl):
            inner = self.resolve(node.type)
            ti = TypeInfo(f"{inner.text} *", "pointer", bits=self.t.pointer_bits, signed=False, elem=inner,
                          base=f"{inner.base or inner.text} *")
            q = set(node.quals or [])
            ti.volatile, ti.const = "volatile" in q, "const" in q
            return ti
        if isinstance(node, c_ast.ArrayDecl):
            inner = self.resolve(node.type)
            dim = self.const_value(node.dim) if node.dim is not None else None
            dims = [dim] + (inner.dims if inner.kind == "array" else [])
            elem = inner.elem if inner.kind == "array" else inner
            return TypeInfo(f"{elem.text}[{dim if dim is not None else ''}]", "array", elem=elem, dims=dims,
                            base=elem.base, volatile=elem.volatile, const=elem.const)
        if isinstance(node, c_ast.FuncDecl):
            return TypeInfo("func", "func")
        if isinstance(node, (c_ast.Struct, c_ast.Union)):
            kind = "struct" if isinstance(node, c_ast.Struct) else "union"
            key = node.name or self.anon_names.get(id(node))
            if node.decls is not None:
                key = key or f"<anon@{node.coord.line if node.coord else 0}>"
                self.structs[key] = {d.name: d.type for d in node.decls if d.name}
            return TypeInfo(f"{kind} {node.name or ''}".strip(), kind, struct=key, base=f"{kind} {node.name or ''}".strip())
        if isinstance(node, c_ast.Enum):
            name = node.name or self.anon_names.get(id(node)) or f"<anon-enum@{node.coord.line if node.coord else 0}>"
            if node.values is not None:
                self.register_enum(node, name)
            rng = self.enum_ranges.get(name)
            return TypeInfo(f"enum {node.name or ''}".strip(), "enum", bits=self.t.int_bits,
                            signed=not (rng and rng[0] >= 0), enum=name, base="enum")
        return UNKNOWN

    def register_enum(self, node, name: str):
        val = -1
        members = {}
        for e in node.values.enumerators:
            if e.value is not None:
                v = self.const_value(e.value)
                val = v if isinstance(v, int) else val + 1
            else:
                val += 1
            members[e.name] = val
            self.enum_values[e.name] = val
            self.model.enum_values[e.name] = val
        if members:
            self.enum_ranges[name] = (min(members.values()), max(members.values()))
        if name not in self.model.enums:
            f = self.model._display(node.coord.file) if node.coord else "?"
            self.model.enums[name] = EnumInfo(name, members, f, node.coord.line if node.coord else 0)

    def member_type(self, struct_key: str | None, member: str) -> TypeInfo:
        if not struct_key:
            return UNKNOWN
        members = self.structs.get(struct_key)
        if members is None:
            # typedef된 struct는 내부 태그로도 찾아본다
            td = self.typedef_nodes.get(struct_key)
            if td is not None:
                self.resolve(td)
                inner = getattr(td, "type", None)
                tag = (getattr(inner, "name", None) or self.anon_names.get(id(inner))
                       or f"<anon@{inner.coord.line if inner is not None and inner.coord else 0}>")
                members = self.structs.get(tag)
        if not members or member not in members:
            return UNKNOWN
        return self.resolve(members[member])

    # --- 상수 ----------------------------------------------------------------
    def const_value(self, node):
        if node is None:
            return None
        if isinstance(node, c_ast.Constant):
            return parse_c_constant(node.value)
        if isinstance(node, c_ast.ID):
            return self.enum_values.get(node.name)
        if isinstance(node, c_ast.UnaryOp):
            if node.op == "sizeof":
                return None
            v = self.const_value(node.expr)
            if v is None:
                return None
            if node.op == "-":
                return -v
            if node.op == "+":
                return v
            if node.op == "~" and isinstance(v, int):
                return ~v
            if node.op == "!":
                return int(not v)
            return None
        if isinstance(node, c_ast.Cast):
            v = self.const_value(node.expr)
            if v is None:
                return None
            ti = self.resolve(node.to_type)
            r = ti.int_range()
            if r and isinstance(v, int):
                span = r[1] - r[0] + 1
                v = (v - r[0]) % span + r[0]
            return v
        if isinstance(node, c_ast.BinaryOp):
            a, b = self.const_value(node.left), self.const_value(node.right)
            if a is None or b is None:
                return None
            return _apply_binop(node.op, a, b)
        if isinstance(node, c_ast.TernaryOp):
            c = self.const_value(node.cond)
            if c is None:
                return None
            return self.const_value(node.iftrue if c else node.iffalse)
        return None


def parse_c_constant(text: str):
    s = text.strip()
    if s.startswith("'"):
        body = s[1:-1]
        esc = {"n": 10, "t": 9, "r": 13, "0": 0, "\\": 92, "'": 39, '"': 34}
        if body.startswith("\\"):
            if body[1:2] == "x":
                return int(body[2:], 16)
            if body[1:].isdigit():
                return int(body[1:], 8)
            return esc.get(body[1:2])
        return ord(body) if body else None
    if s.startswith('"'):
        return None
    t = re.sub(r"[uUlL]+$", "", s)
    try:
        if t.lower().startswith("0x"):
            return int(t, 16)
        if t.lower().startswith("0b"):
            return int(t, 2)
        if re.fullmatch(r"0[0-7]+", t):
            return int(t, 8)
        if re.fullmatch(r"\d+", t):
            return int(t)
        return float(re.sub(r"[fF]$", "", t))
    except ValueError:
        return None


def _apply_binop(op, a, b):
    try:
        if op == "+":
            return a + b
        if op == "-":
            return a - b
        if op == "*":
            return a * b
        if op == "/":
            if not b:
                return None
            return int(a / b) if isinstance(a, int) and isinstance(b, int) else a / b
        if op == "%":
            return (abs(a) % abs(b)) * (1 if a >= 0 else -1) if b and isinstance(a, int) and isinstance(b, int) else None
        if isinstance(a, int) and isinstance(b, int):
            if op == "<<":
                return a << b if 0 <= b < 128 else None
            if op == ">>":
                return a >> b if b >= 0 else None
            if op == "&":
                return a & b
            if op == "|":
                return a | b
            if op == "^":
                return a ^ b
        cmp = {"<": a < b, "<=": a <= b, ">": a > b, ">=": a >= b, "==": a == b, "!=": a != b,
               "&&": bool(a) and bool(b), "||": bool(a) or bool(b)}
        if op in cmp:
            return int(cmp[op])
    except (OverflowError, ZeroDivisionError):
        return None
    return None


_GEN = None


def expr_text(node) -> str:
    global _GEN
    if node is None:
        return ""
    if _GEN is None:
        _GEN = c_generator.CGenerator()
    try:
        return _GEN.visit(node)
    except Exception:  # noqa: BLE001 - 표시용
        return "<expr>"


@dataclass
class _FuncScope:
    """함수 내부의 이름 해석 스코프."""
    fn: Function
    tu: _TU
    names: list = field(default_factory=lambda: [{}])  # 블록 스택: name -> VarInfo

    def lookup(self, name: str) -> VarInfo | None:
        for d in reversed(self.names):
            if name in d:
                return d[name]
        return None


def _terminates(stmt) -> bool:
    if stmt is None:
        return False
    if isinstance(stmt, (c_ast.Return, c_ast.Break, c_ast.Continue, c_ast.Goto)):
        return True
    if isinstance(stmt, c_ast.Compound) and stmt.block_items:
        return _terminates(stmt.block_items[-1])
    if isinstance(stmt, c_ast.If) and stmt.iffalse is not None:
        return _terminates(stmt.iftrue) and _terminates(stmt.iffalse)
    return False


STATE_NAME = re.compile(r"(state|mode|sts|status|stat|phase|step|seq)", re.I)


class _Builder:
    def __init__(self, model: CodeModel, tu: _TU, file_globals: dict):
        self.m = model
        self.tu = tu
        self.globals = file_globals   # name -> VarInfo (이 번역 단위에서 보이는 전역)
        self.scope: _FuncScope | None = None
        self.guards: list[Guard] = []
        self.switch_stack: list = []  # [(switch_var_key, labels list)]

    # --- 위치 ---------------------------------------------------------------
    def loc(self, node) -> tuple[str, int, int]:
        c = getattr(node, "coord", None)
        if c is None or c.file is None:
            return ("?", 0, 0)
        return (self.m._display(c.file), c.line or 0, c.column or 0)

    def raw(self, file: str, line: int) -> str:
        sf = self.m.files.get(file)
        return sf.code[line - 1].strip() if sf and 1 <= line <= len(sf.code) else ""

    # --- 최상위 --------------------------------------------------------------
    def top(self, ext):
        for node in ext:
            if isinstance(node, c_ast.Typedef):
                self.tu.typedef_nodes[node.name] = node.type
                inner = getattr(node.type, "type", None)
                if isinstance(inner, (c_ast.Enum, c_ast.Struct, c_ast.Union)) and inner.name is None:
                    self.tu.anon_names[id(inner)] = node.name
                ti = self.tu.resolve(node.type)  # 내부 struct/enum 등록
                from_prelude = bool(node.coord and node.coord.file and "specverify_prelude" in node.coord.file)
                if node.name not in self.m.typedefs and not from_prelude:
                    named = TypeInfo(**{**ti.__dict__})
                    named.text = node.name
                    named.base = ti.base or ti.text
                    self.m.typedefs[node.name] = named
            elif isinstance(node, c_ast.Decl):
                self.global_decl(node)
            elif isinstance(node, c_ast.FuncDef):
                self.funcdef(node)

    def global_decl(self, node):
        file, line, _ = self.loc(node)
        if isinstance(node.type, c_ast.FuncDecl):
            if node.name:
                self.m.prototypes.add(node.name)
            return
        ti = self.tu.resolve(node.type)
        if not node.name:
            return  # struct/enum 선언만
        storage = set(node.storage or [])
        scope = "static" if "static" in storage else ("extern" if "extern" in storage else "global")
        key = f"{file}::{node.name}" if scope == "static" else node.name
        init = expr_text(node.init) if node.init is not None else None
        existing = self.m.variables.get(key)
        vi = VarInfo(key, node.name, ti, file, line, scope, init=init)
        if existing is None or (existing.scope == "extern" and scope != "extern"):
            self.m.variables[key] = vi
        self.globals[node.name] = self.m.variables[key]

    def funcdef(self, node):
        decl = node.decl
        file, line, _ = self.loc(decl)
        ftype = decl.type
        static = "static" in (decl.storage or [])
        params = []
        if isinstance(ftype, c_ast.FuncDecl) and ftype.args is not None:
            for p in ftype.args.params:
                if isinstance(p, c_ast.Decl) and p.name:
                    pf, pl, _ = self.loc(p)
                    params.append(VarInfo(f"{decl.name}::{p.name}", p.name, self.tu.resolve(p.type), pf, pl, "param",
                                          function=decl.name))
        ret = self.tu.resolve(ftype.type) if isinstance(ftype, c_ast.FuncDecl) else UNKNOWN
        start = line
        body_file, body_line, body_col = self.loc(node.body)
        end = self.m._match_brace(body_file, body_line, body_col) or self._max_line(node.body) or start
        sig = self.raw(file, line)
        fn = Function(name=decl.name, file=file, start=start, end=end, signature=sig or decl.name,
                      static=static, return_type=ret, params=params)
        if fn.key in self.m.functions and self.m.functions[fn.key].parsed:
            return  # 헤더의 static inline 등 중복
        self.m.functions[fn.key] = fn
        self.scope = _FuncScope(fn, self.tu)
        for p in params:
            self.scope.names[0][p.name] = p
        self.guards = []
        self.stmt(node.body)
        self.scope = None

    def _max_line(self, node) -> int:
        best = 0
        for _, ch in node.children():
            c = getattr(ch, "coord", None)
            if c is not None and c.line:
                best = max(best, c.line)
            best = max(best, self._max_line(ch))
        return best

    # --- 문장 ----------------------------------------------------------------
    def push(self, cond, negated, kind):
        file, line, _ = self.loc(cond)
        self.guards.append(Guard(expr_text(cond), line, negated, kind, cond))

    def stmt(self, s):
        if s is None:
            return
        if isinstance(s, c_ast.Compound):
            self.scope.names.append({})
            pushed = 0
            for item in s.block_items or []:
                self.stmt(item)
                # if (cond) { ...; return; } 이후 문장은 !(cond) 조건에서만 실행
                if isinstance(item, c_ast.If) and item.iffalse is None and _terminates(item.iftrue):
                    self.push(item.cond, True, "early-exit")
                    pushed += 1
            for _ in range(pushed):
                self.guards.pop()
            self.scope.names.pop()
        elif isinstance(s, c_ast.Decl):
            self.local_decl(s)
        elif isinstance(s, c_ast.DeclList):
            for d in s.decls:
                self.local_decl(d)
        elif isinstance(s, c_ast.If):
            self.cond(s.cond, "if")
            self.push(s.cond, False, "if")
            self.stmt(s.iftrue)
            self.guards.pop()
            if s.iffalse is not None:
                self.push(s.cond, True, "if")
                self.stmt(s.iffalse)
                self.guards.pop()
        elif isinstance(s, c_ast.For):
            self.scope.names.append({})
            if isinstance(s.init, c_ast.DeclList):
                for d in s.init.decls:
                    self.local_decl(d)
            elif s.init is not None:
                self.expr(s.init)
            if s.cond is not None:
                self.cond(s.cond, "for")
                self.push(s.cond, False, "loop")
            self.stmt(s.stmt)
            if s.next is not None:
                self.expr(s.next)
            if s.cond is not None:
                self.guards.pop()
            self.scope.names.pop()
        elif isinstance(s, c_ast.While):
            self.cond(s.cond, "while")
            self.push(s.cond, False, "loop")
            self.stmt(s.stmt)
            self.guards.pop()
        elif isinstance(s, c_ast.DoWhile):
            self.stmt(s.stmt)
            self.cond(s.cond, "do")
        elif isinstance(s, c_ast.Switch):
            self.cond(s.cond, "switch")
            key = self.lvalue_key(s.cond)
            self.switch_stack.append([key, s.cond, []])
            body = s.stmt.block_items if isinstance(s.stmt, c_ast.Compound) else [s.stmt]
            prev_terminated = True
            for item in body or []:
                if isinstance(item, (c_ast.Case, c_ast.Default)):
                    label = expr_text(item.expr) if isinstance(item, c_ast.Case) else "default"
                    labels = self.switch_stack[-1][2]
                    if prev_terminated:
                        labels.clear()
                    labels.append(label)
                    case_cond = (c_ast.BinaryOp("==", s.cond, item.expr, item.coord)
                                 if isinstance(item, c_ast.Case) else None)
                    if case_cond is not None:
                        self.push(case_cond, False, "case")
                    for st in item.stmts or []:
                        self.stmt(st)
                    if case_cond is not None:
                        self.guards.pop()
                    prev_terminated = bool(item.stmts) and _terminates(item.stmts[-1])
                else:
                    self.stmt(item)
            self.switch_stack.pop()
            file, line, _ = self.loc(s)
            labels = [expr_text(it.expr) for it in body or [] if isinstance(it, c_ast.Case)]
            self.m.switches.append(SwitchInfo(file, line, self.scope.fn.name, key, expr_text(s.cond), labels,
                                              any(isinstance(it, c_ast.Default) for it in body or [])))
        elif isinstance(s, (c_ast.Case, c_ast.Default)):
            for st in s.stmts or []:
                self.stmt(st)
        elif isinstance(s, c_ast.Label):
            self.stmt(s.stmt)
        elif isinstance(s, c_ast.Return):
            if s.expr is not None:
                self.expr(s.expr)
        elif isinstance(s, (c_ast.Break, c_ast.Continue, c_ast.Goto, c_ast.EmptyStatement, c_ast.Pragma)):
            pass
        else:
            self.expr(s)

    def local_decl(self, d):
        if not d.name or isinstance(d.type, c_ast.FuncDecl):
            return
        file, line, _ = self.loc(d)
        storage = set(d.storage or [])
        scope = "static_local" if "static" in storage else "local"
        fn = self.scope.fn
        vi = VarInfo(f"{fn.key}::{d.name}@{line}", d.name, self.tu.resolve(d.type), file, line, scope,
                     function=fn.name, init=expr_text(d.init) if d.init is not None else None)
        if scope == "static_local":
            self.m.variables[vi.key] = vi
        fn.locals.append(vi)
        self.scope.names[-1][d.name] = vi
        if d.init is not None:
            self.expr(d.init)
            self.record_access(vi, "write", d)
            self.ev("assign", d)

    # --- 식 -----------------------------------------------------------------
    def cond(self, node, kind):
        file, line, _ = self.loc(node)
        raw = self.raw(file, line)
        ids = sorted({n.name for n in _walk(node) if isinstance(n, c_ast.ID) and self.resolve_name(n.name)})
        macros = [t for t in re.findall(r"\b[A-Za-z_]\w*\b", raw) if t in self.m.macros]
        self.scope.fn.conditions.append(Condition(kind, file, line, self.scope.fn.name, expr_text(node), raw,
                                                  ids, sorted(set(macros))))
        self.expr(node)

    def resolve_name(self, name: str) -> VarInfo | None:
        v = self.scope.lookup(name) if self.scope else None
        return v or self.globals.get(name)

    def record_access(self, vi: VarInfo, mode: str, node):
        file, line, col = self.loc(node)
        self.scope.fn.accesses.append(Access(vi.key, vi.name, mode, file, line, col, self.scope.fn.name,
                                             tuple(self.guards)))

    def lvalue_key(self, node) -> str | None:
        base = node
        while isinstance(base, (c_ast.StructRef, c_ast.ArrayRef, c_ast.Cast)):
            base = base.name if not isinstance(base, c_ast.Cast) else base.expr
        if isinstance(base, c_ast.UnaryOp) and base.op == "*":
            base = base.expr
        if isinstance(base, c_ast.ID):
            vi = self.resolve_name(base.name)
            return vi.key if vi else None
        return None

    def ev(self, kind, node):
        file, line, _ = self.loc(node)
        names = {}
        for root in [node] + [g.node for g in self.guards]:
            for x in _walk(root):
                if isinstance(x, c_ast.ID) and x.name not in names:
                    vi = self.resolve_name(x.name)
                    if vi:
                        names[x.name] = vi
        self.m.events.append(Event(kind, node, file, line, self.scope.fn.name, tuple(self.guards),
                                   self.scope.fn.key, self.tu, names))

    def expr(self, n, mode="read"):
        if n is None:
            return
        if isinstance(n, c_ast.ID):
            vi = self.resolve_name(n.name)
            if vi:
                self.record_access(vi, mode, n)
        elif isinstance(n, c_ast.Constant):
            pass
        elif isinstance(n, c_ast.Assignment):
            file, line, _ = self.loc(n)
            target_key = self.lvalue_key(n.lvalue)
            if n.op == "=":
                rmw = target_key is not None and any(
                    isinstance(x, c_ast.ID) and (self.resolve_name(x.name) or UNKNOWN_VAR).key == target_key
                    for x in _walk(n.rvalue))
                self.expr(n.rvalue)
                self.expr(n.lvalue, "rmw" if rmw else "write")
                self.ev("assign", n)
            else:
                self.expr(n.rvalue)
                self.expr(n.lvalue, "rmw")
                self.ev("compound", n)
            self.scope.fn.assigns.append(Assign(file, line, self.scope.fn.name, expr_text(n.lvalue), target_key,
                                                n.op, expr_text(n.rvalue), self.raw(file, line), tuple(self.guards)))
            self.transition(n, target_key)
        elif isinstance(n, c_ast.UnaryOp):
            if n.op in ("p++", "p--", "++", "--"):
                self.expr(n.expr, "rmw")
                self.ev("incdec", n)
                file, line, _ = self.loc(n)
                self.scope.fn.assigns.append(Assign(file, line, self.scope.fn.name, expr_text(n.expr),
                                                    self.lvalue_key(n.expr), n.op.replace("p", ""), "",
                                                    self.raw(file, line), tuple(self.guards)))
            elif n.op == "&":
                self.expr(n.expr, "addr")
            elif n.op == "*":
                self.expr(n.expr)
                self.ev("deref", n)
            elif n.op == "sizeof":
                pass
            else:
                self.expr(n.expr)
        elif isinstance(n, c_ast.BinaryOp):
            self.expr(n.left)
            self.expr(n.right)
            self.ev("binop", n)
        elif isinstance(n, c_ast.TernaryOp):
            self.cond(n.cond, "ternary")
            self.push(n.cond, False, "ternary")
            self.expr(n.iftrue)
            self.guards.pop()
            self.push(n.cond, True, "ternary")
            self.expr(n.iffalse)
            self.guards.pop()
        elif isinstance(n, c_ast.ArrayRef):
            self.expr(n.name, mode)
            self.expr(n.subscript)
            self.ev("index", n)
        elif isinstance(n, c_ast.StructRef):
            self.expr(n.name, mode if n.type == "." else "read")
            if n.type == "->":
                self.ev("deref", n)
        elif isinstance(n, c_ast.FuncCall):
            file, line, _ = self.loc(n)
            callee = n.name.name if isinstance(n.name, c_ast.ID) else expr_text(n.name)
            if not isinstance(n.name, c_ast.ID):
                self.expr(n.name)
            args = n.args.exprs if n.args is not None else []
            for a in args:
                self.expr(a)
            self.scope.fn.calls.append(CallSite(self.scope.fn.name, callee, file, line,
                                                ", ".join(expr_text(a) for a in args), tuple(self.guards)))
        elif isinstance(n, c_ast.Cast):
            self.expr(n.expr, mode)
            self.ev("cast", n)
        elif isinstance(n, c_ast.ExprList):
            for e in n.exprs:
                self.expr(e)
        elif isinstance(n, c_ast.InitList):
            for e in n.exprs:
                self.expr(e)
        elif isinstance(n, c_ast.NamedInitializer):
            self.expr(n.expr)
        elif isinstance(n, c_ast.CompoundLiteral):
            self.expr(n.init)
        else:
            for _, ch in n.children():
                self.expr(ch)

    def transition(self, n, target_key):
        """상태 변수 대입 → 상태 전이 레코드"""
        if n.op != "=" or target_key is None:
            return
        vi = self.m.variables.get(target_key) or (self.scope.lookup(expr_text(n.lvalue)) if self.scope else None)
        if vi is None:
            return
        is_state = vi.type.kind == "enum" or STATE_NAME.search(vi.name)
        rv = n.rvalue
        if not is_state or not (isinstance(rv, c_ast.ID) and rv.name in self.m.enum_values
                                or isinstance(rv, c_ast.Constant)):
            return
        to_state = expr_text(rv)
        frm: list[str] = []
        for key, cond_node, labels in reversed(self.switch_stack):
            if key == target_key:
                frm = list(labels)
                break
        if not frm:
            for g in self.guards:
                if (not g.negated and isinstance(g.node, c_ast.BinaryOp) and g.node.op == "=="
                        and self.lvalue_key(g.node.left) == target_key):
                    frm.append(expr_text(g.node.right))
        file, line, _ = self.loc(n)
        self.m.transitions.append(Transition(vi.key, frm or ["*"], to_state, file, line, self.scope.fn.name,
                                             tuple(self.guards)))


UNKNOWN_VAR = VarInfo("?", "?", UNKNOWN, "?", 0, "local")


def _walk(node):
    yield node
    for _, ch in node.children():
        yield from _walk(ch)


# ---------------------------------------------------------------------------
# 진입점
# ---------------------------------------------------------------------------


def collect_sources(inputs: list[str]) -> tuple[Path, list[Path]]:
    paths: list[Path] = []
    for raw in inputs:
        p = Path(raw).resolve()
        if p.is_dir():
            paths.extend(sorted(x for x in p.rglob("*") if x.is_file() and x.suffix.lower() in C_EXT | H_EXT))
        elif p.is_file():
            paths.append(p)
        else:
            raise FileNotFoundError(raw)
    if not paths:
        raise ValueError("분석할 .c/.h 파일이 없습니다.")
    roots = [Path(r).resolve() for r in inputs]
    root = Path(os.path.commonpath([str(r if r.is_dir() else r.parent) for r in roots]))
    return root, paths


def build_model(inputs: list[str], env: Environment) -> CodeModel:
    root, paths = collect_sources(inputs)
    model = CodeModel(root=str(root), env=env)

    def display(path: str) -> str:
        ap = os.path.normpath(os.path.abspath(path))
        try:
            return Path(ap).relative_to(root).as_posix()
        except ValueError:
            return Path(ap).as_posix()

    model._display = display  # type: ignore[attr-defined]
    model._match_brace = lambda f, l, c: _match_brace(model, f, l, c)  # type: ignore[attr-defined]

    all_includes = []
    for p in paths:
        text = p.read_text(encoding="utf-8", errors="replace")
        lines = text.split("\n")
        if lines and lines[-1] == "":
            lines.pop()
        code = strip_comments(text).split("\n")[: len(lines)]
        while len(code) < len(lines):
            code.append("")
        sf = SourceFile(display(str(p)), str(p), lines, code)
        model.files[sf.path] = sf
        defines, includes, regions = scan_directives(sf)
        for mac in defines:
            model.macros.setdefault(mac.name, mac)
        model.regions.extend(regions)
        all_includes.extend((p, inc, ln, sysinc) for inc, ln, sysinc in includes)
    for mac in model.macros.values():
        if mac.params is None:
            mac.numeric = eval_const_text(mac.value, model.macros)

    if c_parser is None:
        model.issues.append(ParseIssue("*", "pycparser 미설치 → AST 분석 생략, 정규식 기반 함수 위치만 추출 "
                                            "(pip install pycparser)", "error"))
        _regex_functions(model)
        return model

    model._paths = paths  # type: ignore[attr-defined]
    model._includes = all_includes  # type: ignore[attr-defined]
    pp = _find_preprocessor()
    model.preprocessor = " ".join(pp) if pp else "(없음)"
    with tempfile.TemporaryDirectory(prefix="specverify_") as tmp:
        flags, prelude = compile_flags(model, Path(tmp))
        for p in paths:
            if p.suffix.lower() not in C_EXT:
                continue
            sf = model.files[display(str(p))]
            text = None
            if pp:
                cmd = pp + flags + [str(p)]
                r = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
                if r.returncode != 0:
                    model.issues.append(ParseIssue(sf.path, "전처리 실패: " + _first_lines(r.stderr), "error"))
                else:
                    text = r.stdout
            else:
                model.issues.append(ParseIssue(sf.path, "C 전처리기(gcc/clang/cpp) 없음 → 매크로 미확장 상태로 파싱 시도"))
                text = _naive_preprocess(sf, prelude)
            if text is None:
                continue
            for f, lines in _active_lines(text).items():
                d = model.files.get(display(f))
                if d is not None:
                    d.active |= lines
                    d.preprocessed = True
            text = re.sub(r"^#\s*pragma.*$", "", text, flags=re.M)
            try:
                ast = c_parser.CParser().parse(text, str(p))
            except Exception as e:  # pycparser.ParseError 포함
                model.issues.append(ParseIssue(sf.path, f"구문 분석 실패: {_shorten_parse_error(e, display)}", "error"))
                continue
            tu = _TU(model)
            _Builder(model, tu, {}).top(ast.ext)

    for reg in model.regions:
        sf = model.files.get(reg.file)
        if sf and sf.preprocessed:
            code_lines = [n for n in range(reg.start + 1, reg.end)
                          if sf.code[n - 1].strip() and not sf.code[n - 1].strip().startswith("#")]
            if code_lines:
                reg.active = any(n in sf.active for n in code_lines)
    _regex_functions(model, only_missing=True)
    return model


def compile_flags(model: CodeModel, workdir: Path) -> tuple[list[str], Path]:
    """전처리/컴파일 공통 플래그 (스텁 헤더·prelude를 workdir에 생성). 외부 도구(gcc 경고 등)도 같은 설정을 쓴다."""
    env = model.env
    paths = model._paths  # type: ignore[attr-defined]
    fake = workdir / "fake_include"
    fake.mkdir(parents=True, exist_ok=True)
    inc_dirs = sorted({str(p.parent) for p in paths if p.suffix.lower() in H_EXT} | {model.root})
    inc_dirs += [str(Path(d).resolve()) for d in env.include_dirs]
    _make_stub_headers(fake, model._includes, inc_dirs)  # type: ignore[attr-defined]
    source_text = "\n".join("\n".join(sf.code) for sf in model.files.values())
    defined_types = set(_TYPEDEF_NAME.findall(source_text))
    prelude = workdir / "specverify_prelude.h"
    typedefs = {k: v for k, v in BUILTIN_TYPEDEFS.items() if k not in defined_types and k not in env.typedefs}
    typedefs.update(env.typedefs)
    prelude.write_text(PRELUDE + AUTOSAR_ABSTRACTION
                       + "".join(f"typedef {v} {k};\n" for k, v in typedefs.items()), encoding="utf-8")
    flags = ["-nostdinc", "-undef", "-std=gnu99", "-x", "c", "-include", str(prelude)]
    flags += [f"-I{d}" for d in inc_dirs] + [f"-I{fake}"]
    flags += [f"-D{k}" if v == "" else f"-D{k}={v}" for k, v in env.defines.items()]
    return flags, prelude


def _first_lines(s: str, n: int = 3) -> str:
    return " / ".join(x for x in s.strip().splitlines()[:n])


def _shorten_parse_error(e: Exception, display) -> str:
    msg = str(e)
    m = re.match(r"(.+?):(\d+):(\d+): (.*)", msg)
    if m:
        return f"{display(m.group(1))}:{m.group(2)}: {m.group(4)}"
    return msg


def _naive_preprocess(sf: SourceFile, prelude: Path) -> str:
    out = [prelude.read_text(encoding="utf-8"), f'# 1 "{sf.abspath}"']
    out += ["" if l.strip().startswith("#") else l for l in sf.code]
    return "\n".join(out)


def _make_stub_headers(fake: Path, includes, inc_dirs: list[str]):
    for src, inc, _, sysinc in includes:
        candidates = ([] if sysinc else [src.parent / inc]) + [Path(d) / inc for d in inc_dirs]
        if any(c.is_file() for c in candidates):
            continue
        target = fake / inc
        if target.exists():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(STD_HEADERS.get(Path(inc).name, f"/* specverify: '{inc}' 스텁 (원본 없음) */\n"),
                          encoding="utf-8")


def _match_brace(model: CodeModel, file: str, line: int, col: int) -> int | None:
    sf = model.files.get(file)
    if not sf or not line:
        return None
    text = strip_comments("\n".join(sf.lines), blank_strings=True).split("\n")
    depth = 0
    started = False
    for ln in range(line, len(text) + 1):
        s = text[ln - 1]
        start_col = (col - 1) if ln == line and col else 0
        for ch in s[start_col:]:
            if ch == "{":
                depth += 1
                started = True
            elif ch == "}":
                depth -= 1
                if started and depth == 0:
                    return ln
    return None


_FUNC_RE = re.compile(r"^[ \t]*(?:static\s+|inline\s+|extern\s+|FUNC\s*\([^)]*\)\s*)*[\w\s\*\(\),]*?\b([A-Za-z_]\w*)\s*\(([^;{}]*)\)\s*\{", re.M)


def _regex_functions(model: CodeModel, only_missing: bool = False):
    """AST가 없는 파일에서 함수 위치만이라도 추출 (축소 분석)."""
    for sf in model.files.values():
        if not sf.path.endswith(".c"):
            continue
        if only_missing and any(fn.file == sf.path for fn in model.functions.values()):
            continue
        text = "\n".join(sf.code)
        for m in _FUNC_RE.finditer(text):
            name = m.group(1)
            if name in ("if", "while", "for", "switch", "return", "sizeof"):
                continue
            line = text.count("\n", 0, m.start(1)) + 1
            brace_line = text.count("\n", 0, m.end() - 1) + 1
            col = m.end() - 1 - (text.rfind("\n", 0, m.end() - 1) + 1) + 1
            end = _match_brace(model, sf.path, brace_line, col) or line
            key = f"{sf.path}::{name}"
            if key not in model.functions and name not in model.functions:
                model.functions[key] = Function(name, sf.path, line, end, sf.code[line - 1].strip(),
                                                static=True, parsed=False)


# ---------------------------------------------------------------------------
# LLM 입력용 렌더링
# ---------------------------------------------------------------------------


def render_excerpt(model: CodeModel, file: str, start: int, end: int, mark: set | None = None) -> str:
    sf = model.files.get(file)
    if not sf:
        return f"(파일 없음: {file})"
    out = []
    for n in range(max(1, start), min(len(sf.lines), end) + 1):
        flag = ">" if mark and n in mark else " "
        inactive = "⊘" if sf.is_inactive(n) else " "
        out.append(f"{n:5d}{flag}{inactive}| {sf.lines[n - 1]}")
    return "\n".join(out)


def render_function(model: CodeModel, fn: Function, mark: set | None = None) -> str:
    head = f"--- {fn.file} :: {fn.name}() [L{fn.start}-{fn.end}]"
    return head + "\n" + render_excerpt(model, fn.file, fn.start, fn.end, mark)


def _guard_text(guards, compact: bool = False) -> str:
    if not compact:
        return " && ".join(f"L{g.line}:{g.render()}" for g in guards)
    early = [g for g in guards if g.kind == "early-exit"]
    rest = [g for g in guards if g.kind != "early-exit"]
    txt = " && ".join(f"L{g.line}:{g.render()}" for g in rest)
    if early:
        txt += (" | " if txt else "") + "앞선 조기 반환 통과: " + ", ".join(f"L{g.line}" for g in early)
    return txt


def render_index(model: CodeModel) -> str:
    """요구사항 추적에 쓰는 코드 색인 (전처리 후 의미 + 원본 표기)."""
    out: list[str] = []
    out.append("## 파일")
    for sf in model.files.values():
        state = "전처리·파싱됨" if sf.preprocessed else ("헤더" if sf.path.endswith(".h") else "AST 없음")
        out.append(f"- {sf.path} ({len(sf.lines)} lines, {state})")
    if model.regions:
        out.append("\n## 조건부 컴파일 구간")
        for r in model.regions:
            st = {True: "활성", False: "비활성(현재 빌드 설정에서 제외)", None: "판단 불가"}[r.active]
            out.append(f"- {r.file}:{r.start}-{r.end} {r.directive} → {st}")
    if model.macros:
        out.append("\n## 매크로 (원본 정의 → 평가값)")
        for m in model.macros.values():
            if m.name.endswith("_H") and not m.value:
                continue
            val = f" = {m.numeric}" if m.numeric is not None and str(m.numeric) != m.value.strip("() ") else ""
            out.append(f"- {m.name}{m.params or ''} → {m.value or '(빈 값)'}{val}  @{m.file}:{m.line}")
    if model.typedefs:
        out.append("\n## typedef (실제 크기)")
        for name, ti in model.typedefs.items():
            out.append(f"- {ti.describe()}")
    if model.enums:
        out.append("\n## enum")
        for e in model.enums.values():
            out.append(f"- {e.name} @{e.file}:{e.line}: " + ", ".join(f"{k}={v}" for k, v in e.members.items()))
    if model.variables:
        out.append("\n## 전역/정적 변수")
        for v in model.variables.values():
            init = f" = {v.init}" if v.init is not None else ""
            fn = f" (함수 {v.function} 내부 static)" if v.function else ""
            out.append(f"- [{v.scope}] {v.name}: {v.type.describe()}{init}  @{v.file}:{v.line}{fn}")
    out.append("\n## 함수")
    for fn in model.functions.values():
        callers = ", ".join(sorted({c.name for c in model.callers(fn)})) or "-"
        ctxs = model.context_map().get(fn.key)
        out.append(f"\n### {fn.name}  @{fn.file}:{fn.start}-{fn.end}" + ("" if fn.parsed else "  (AST 없음)"))
        out.append(f"- 선언: {fn.signature}")
        out.append(f"- 호출자: {callers}" + (f" / 실행 컨텍스트: {', '.join(ctxs)}" if ctxs else ""))
        if fn.calls:
            out.append("- 호출: " + ", ".join(f"{c.callee}(L{c.line})" for c in fn.calls))
        gl = defaultdict(set)
        for a in fn.accesses:
            if a.var in model.variables:
                gl[a.name].add(a.mode)
        if gl:
            out.append("- 전역 접근: " + ", ".join(f"{k}[{'/'.join(sorted(v))}]" for k, v in sorted(gl.items())))
        for c in fn.conditions:
            exp = f"  ⇒ {c.text}" if _norm(c.text) not in _norm(c.raw) else ""
            out.append(f"  - L{c.line} {c.kind}: {c.raw}{exp}")
        seen = set()
        for a in fn.assigns:
            if (a.line, a.raw) in seen:
                continue
            seen.add((a.line, a.raw))
            g = f"  [조건 {_guard_text(a.guards, True)}]" if a.guards else ""
            out.append(f"  - L{a.line} 대입: {a.raw}{g}")
    if model.transitions:
        out.append("\n## 상태 전이 (상태 변수 대입)")
        for t in model.transitions:
            g = f" [조건 {_guard_text(t.guards, True)}]" if t.guards else ""
            out.append(f"- {t.state_var}: {', '.join(t.from_states)} → {t.to_state}  @{t.file}:{t.line} "
                       f"{t.function}(){g}")
    if model.issues:
        out.append("\n## 분석 제한")
        for i in model.issues:
            out.append(f"- [{i.severity}] {i.file}: {i.message}")
    return "\n".join(out)


def _norm(s: str) -> str:
    return re.sub(r"\s+|[()]", "", s)
