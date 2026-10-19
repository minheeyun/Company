"""소스 수집, 빌드 조건(compile_commands.json, -I/-D), 전처리, 파싱, 타입 모델.

같은 파일이라도 빌드 조건(매크로, include 경로)이 다르면 다른 프로그램이므로
분석은 항상 '실제 빌드 조건으로 전처리한 결과'를 대상으로 한다.
매크로 값·자료형 변경도 전처리 후 AST 비교에서 그대로 드러난다.
"""

from __future__ import annotations

import fnmatch
import json
import os
import shlex
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from pycparser import c_ast, c_parser

ROOT_TOKEN = "{ROOT}"

# 파서가 모르는 컴파일러 확장을 지우는 기본 매크로 (임베디드 컴파일러 키워드는 -D/--strip 으로 추가)
DEFAULT_DEFINES = [
    "__attribute__(x)=",
    "__asm__(...)=",
    "__asm(...)=",
    "__extension__=",
    "__inline__=inline",
    "__inline=inline",
    "__volatile__=volatile",
    "__const=const",
    "__restrict=",
    "__restrict__=",
    "__builtin_expect(x,y)=(x)",
    "__declspec(x)=",
    "__CIA__=1",
]

# 표준 헤더 대신 쓰는 최소 선언 (pycparser는 GNU 확장이 섞인 시스템 헤더를 해석하지 못한다)
_FAKE_LIBC = r"""
#ifndef CIA_FAKE_LIBC_H
#define CIA_FAKE_LIBC_H
typedef signed char int8_t; typedef unsigned char uint8_t;
typedef short int16_t; typedef unsigned short uint16_t;
typedef int int32_t; typedef unsigned int uint32_t;
typedef long long int64_t; typedef unsigned long long uint64_t;
typedef int8_t int_least8_t; typedef uint8_t uint_least8_t; typedef int16_t int_least16_t; typedef uint16_t uint_least16_t;
typedef int32_t int_least32_t; typedef uint32_t uint_least32_t; typedef int64_t int_least64_t; typedef uint64_t uint_least64_t;
typedef int8_t int_fast8_t; typedef uint8_t uint_fast8_t; typedef int16_t int_fast16_t; typedef uint16_t uint_fast16_t;
typedef int32_t int_fast32_t; typedef uint32_t uint_fast32_t; typedef int64_t int_fast64_t; typedef uint64_t uint_fast64_t;
typedef long intptr_t; typedef unsigned long uintptr_t; typedef long long intmax_t; typedef unsigned long long uintmax_t;
typedef unsigned long size_t; typedef long ssize_t; typedef long ptrdiff_t; typedef int wchar_t;
typedef void *va_list; typedef struct _cia_FILE FILE; typedef long time_t; typedef long clock_t;
typedef int sig_atomic_t; typedef struct { int _x[16]; } jmp_buf[1]; typedef struct { long q, r; } ldiv_t; typedef struct { int q, r; } div_t;
#define bool _Bool
#define true 1
#define false 0
#define __bool_true_false_are_defined 1
#define NULL ((void*)0)
#define offsetof(t, m) ((size_t)&(((t*)0)->m))
#define va_start(a, b) ((void)(a))
#define va_end(a) ((void)(a))
#define va_arg(a, t) (*(t*)(a))
#define va_copy(d, s) ((d) = (s))
#define assert(x) ((void)(x))
#define static_assert _Static_assert
#define CHAR_BIT 8
#define SCHAR_MIN (-128)
#define SCHAR_MAX 127
#define UCHAR_MAX 255
#define CHAR_MIN (-128)
#define CHAR_MAX 127
#define SHRT_MIN (-32768)
#define SHRT_MAX 32767
#define USHRT_MAX 65535
#define INT_MIN (-2147483647-1)
#define INT_MAX 2147483647
#define UINT_MAX 4294967295U
#define LONG_MIN (-2147483647L-1)
#define LONG_MAX 2147483647L
#define ULONG_MAX 4294967295UL
#define LLONG_MAX 9223372036854775807LL
#define LLONG_MIN (-9223372036854775807LL-1)
#define ULLONG_MAX 18446744073709551615ULL
#define INT8_MIN (-128)
#define INT8_MAX 127
#define UINT8_MAX 255
#define INT16_MIN (-32768)
#define INT16_MAX 32767
#define UINT16_MAX 65535
#define INT32_MIN (-2147483647-1)
#define INT32_MAX 2147483647
#define UINT32_MAX 4294967295U
#define INT64_MAX 9223372036854775807LL
#define INT64_MIN (-9223372036854775807LL-1)
#define UINT64_MAX 18446744073709551615ULL
#define SIZE_MAX 4294967295U
#define INT8_C(x) (x)
#define UINT8_C(x) (x)
#define INT16_C(x) (x)
#define UINT16_C(x) (x)
#define INT32_C(x) (x)
#define UINT32_C(x) (x ## U)
#define INT64_C(x) (x ## LL)
#define UINT64_C(x) (x ## ULL)
#define FLT_EPSILON 1.19209290e-07F
#define DBL_EPSILON 2.2204460492503131e-16
#define FLT_MAX 3.40282347e+38F
#define DBL_MAX 1.7976931348623157e+308
#define FLT_MIN 1.17549435e-38F
#define DBL_MIN 2.2250738585072014e-308
#define NAN (0.0f/0.0f)
#define INFINITY (1.0f/0.0f)
#define HUGE_VAL (1.0/0.0)
#define EOF (-1)
#define EXIT_SUCCESS 0
#define EXIT_FAILURE 1
#define RAND_MAX 32767
#endif
"""

_FAKE_HEADERS = [
    "assert.h", "ctype.h", "errno.h", "float.h", "inttypes.h", "iso646.h", "limits.h", "math.h",
    "setjmp.h", "signal.h", "stdarg.h", "stdbool.h", "stddef.h", "stdint.h", "stdio.h", "stdlib.h",
    "string.h", "time.h", "stdalign.h", "stdnoreturn.h",
]

_fake_dir: Path | None = None


def fake_include_dir() -> Path:
    global _fake_dir
    if _fake_dir is None:
        d = Path(tempfile.mkdtemp(prefix="cia_fake_libc_"))
        (d / "_cia_fake_libc.h").write_text(_FAKE_LIBC, encoding="utf-8")
        for h in _FAKE_HEADERS:
            (d / h).write_text('#include "_cia_fake_libc.h"\n', encoding="utf-8")
        _fake_dir = d
    return _fake_dir


# ---------------------------------------------------------------------------
# 빌드 조건
# ---------------------------------------------------------------------------


@dataclass
class BuildConfig:
    cpp: list[str] = field(default_factory=lambda: ["gcc", "-E"])
    include_dirs: list[str] = field(default_factory=list)   # ROOT_TOKEN 포함 가능
    defines: list[str] = field(default_factory=list)
    undefines: list[str] = field(default_factory=list)
    forced_includes: list[str] = field(default_factory=list)
    per_file: dict[str, list[str]] = field(default_factory=dict)  # 상대 경로 → 전처리 플래그 (compile_commands)
    use_fake_libc: bool = True

    def describe(self) -> dict:
        return {
            "cpp": " ".join(self.cpp),
            "include_dirs": self.include_dirs,
            "defines": self.defines,
            "undefines": self.undefines,
            "forced_includes": self.forced_includes,
            "compile_commands_files": len(self.per_file),
            "fake_libc": self.use_fake_libc,
        }


def _rootify(path: str, base: Path, cc_root: Path) -> str:
    p = Path(path)
    if not p.is_absolute():
        p = base / p
    p = Path(os.path.normpath(p))
    try:
        rel = p.relative_to(cc_root)
        return f"{ROOT_TOKEN}/{rel.as_posix()}" if rel.as_posix() != "." else ROOT_TOKEN
    except ValueError:
        return str(p)


def load_compile_commands(path: str | Path, cc_root: str | Path) -> dict[str, list[str]]:
    """compile_commands.json 에서 파일별 전처리 플래그(-I -D -U -include -isystem -iquote -std)만 뽑는다.

    cc_root 아래 경로는 ROOT_TOKEN 으로 바꿔 두어 변경 전/후 트리 어느 쪽에도 적용할 수 있게 한다.
    같은 파일이 여러 번 나오면(빌드 구성이 여러 개) 첫 항목을 쓰고, 나머지는 구성별로 따로 실행하라고 안내한다.
    """
    cc_root = Path(cc_root).resolve()
    entries = json.loads(Path(path).read_text(encoding="utf-8"))
    out: dict[str, list[str]] = {}
    for e in entries:
        base = Path(e.get("directory", "."))
        args = e.get("arguments") or shlex.split(e.get("command", ""))
        fpath = Path(e["file"])
        if not fpath.is_absolute():
            fpath = base / fpath
        try:
            rel = Path(os.path.normpath(fpath)).relative_to(cc_root).as_posix()
        except ValueError:
            continue
        if rel in out:
            continue
        flags: list[str] = []
        i = 0
        while i < len(args):
            a = args[i]
            nxt = args[i + 1] if i + 1 < len(args) else None
            for opt in ("-I", "-isystem", "-iquote", "-include"):
                if a == opt and nxt is not None:
                    flags += [opt, _rootify(nxt, base, cc_root)]
                    i += 1
                    break
                if a.startswith(opt) and len(a) > len(opt) and opt != "-include":
                    flags += [opt, _rootify(a[len(opt):], base, cc_root)]
                    break
            else:
                if a in ("-D", "-U") and nxt is not None:
                    flags += [a, nxt]
                    i += 1
                elif a.startswith(("-D", "-U", "-std=")):
                    flags.append(a)
            i += 1
        out[rel] = flags
    return out


def discover_sources(root: Path, include: list[str] | None, exclude: list[str] | None) -> list[str]:
    include = include or ["*.c"]
    exclude = exclude or []
    files = []
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(root).as_posix()
        if any(part.startswith(".") for part in Path(rel).parts):
            continue
        if any(fnmatch.fnmatch(rel, pat) or fnmatch.fnmatch(p.name, pat) for pat in include) and not any(
            fnmatch.fnmatch(rel, pat) for pat in exclude
        ):
            files.append(rel)
    return files


def preprocess_command(root: Path, rel: str, cfg: BuildConfig) -> list[str]:
    def sub(s: str) -> str:
        return s.replace(ROOT_TOKEN, str(root))

    cmd = list(cfg.cpp)
    if cfg.use_fake_libc:
        cmd += ["-nostdinc", "-I", str(fake_include_dir())]
    for d in DEFAULT_DEFINES:
        cmd.append("-D" + d)
    cmd += [sub(x) for x in cfg.per_file.get(rel, [])]
    for inc in cfg.include_dirs:
        cmd += ["-I", sub(inc)]
    for d in cfg.defines:
        cmd.append("-D" + d)
    for u in cfg.undefines:
        cmd.append("-U" + u)
    for f in cfg.forced_includes:
        cmd += ["-include", sub(f)]
    cmd.append(str(root / rel))
    return cmd


def preprocess(root: Path, rel: str, cfg: BuildConfig) -> str:
    cmd = preprocess_command(root, rel, cfg)
    r = subprocess.run(cmd, capture_output=True, text=True, cwd=str(root))
    if r.returncode != 0:
        raise RuntimeError(f"전처리 실패 ({' '.join(cmd[:2])} ... {rel}):\n{r.stderr.strip()[:2000]}")
    return r.stdout


# ---------------------------------------------------------------------------
# 타입 모델
# ---------------------------------------------------------------------------

_INT_RANGES = {
    "_Bool": (0, 1),
    "char": (-128, 127), "signed char": (-128, 127), "unsigned char": (0, 255),
    "short": (-32768, 32767), "unsigned short": (0, 65535),
    "int": (-2**31, 2**31 - 1), "unsigned int": (0, 2**32 - 1),
    "long": (-2**31, 2**31 - 1), "unsigned long": (0, 2**32 - 1),
    "long long": (-2**63, 2**63 - 1), "unsigned long long": (0, 2**64 - 1),
}


class TypeEnv:
    """번역 단위 하나의 typedef / struct·union 태그 정보."""

    def __init__(self):
        self.typedefs: dict[str, c_ast.Node] = {}
        self.tags: dict[tuple[str, str], c_ast.Node] = {}

    def collect(self, node: c_ast.Node):
        """typedef 와 태그 정의를 모은다 (함수 안에 있는 것도 포함)."""
        for n in _walk(node):
            if isinstance(n, c_ast.Typedef):
                self.typedefs[n.name] = n.type
            elif isinstance(n, (c_ast.Struct, c_ast.Union)) and n.name and n.decls is not None:
                self.tags[(type(n).__name__, n.name)] = n

    def core(self, t, depth: int = 0):
        """TypeDecl·typedef 를 벗겨 PtrDecl/ArrayDecl/FuncDecl/Struct/Union/Enum/IdentifierType 중 하나를 돌려준다."""
        while t is not None and depth < 64:
            depth += 1
            if isinstance(t, (c_ast.TypeDecl, c_ast.Typename)):
                t = t.type
            elif isinstance(t, c_ast.IdentifierType):
                if len(t.names) == 1 and t.names[0] in self.typedefs:
                    t = self.typedefs[t.names[0]]
                else:
                    return t
            elif isinstance(t, (c_ast.Struct, c_ast.Union)) and t.decls is None:
                return self.tags.get((type(t).__name__, t.name), t)
            else:
                return t
        return t

    def is_array(self, t) -> bool:
        return isinstance(self.core(t), c_ast.ArrayDecl)

    def is_pointer(self, t) -> bool:
        return isinstance(self.core(t), c_ast.PtrDecl)

    def is_aggregate(self, t) -> bool:
        return isinstance(self.core(t), (c_ast.Struct, c_ast.Union, c_ast.ArrayDecl))

    def is_union(self, t) -> bool:
        return isinstance(self.core(t), c_ast.Union)

    def is_func(self, t) -> bool:
        return isinstance(self.core(t), c_ast.FuncDecl)

    def is_void(self, t) -> bool:
        c = self.core(t)
        return isinstance(c, c_ast.IdentifierType) and c.names == ["void"]

    def is_float(self, t) -> bool:
        c = self.core(t)
        return isinstance(c, c_ast.IdentifierType) and any(n in ("float", "double") for n in c.names)

    def is_scalar(self, t) -> bool:
        c = self.core(t)
        return isinstance(c, (c_ast.IdentifierType, c_ast.Enum)) and not self.is_void(t)

    def element(self, t):
        c = self.core(t)
        if isinstance(c, (c_ast.ArrayDecl, c_ast.PtrDecl)):
            return c.type
        return None

    def field(self, t, name: str):
        c = self.core(t)
        if not isinstance(c, (c_ast.Struct, c_ast.Union)) or not c.decls:
            return None
        for d in c.decls:
            if d.name == name:
                return d.type
            if d.name is None:  # 익명 struct/union 멤버
                sub = self.field(d.type, name)
                if sub is not None:
                    return sub
        return None

    def fields(self, t) -> list[str]:
        c = self.core(t)
        if not isinstance(c, (c_ast.Struct, c_ast.Union)) or not c.decls:
            return []
        return [d.name for d in c.decls if d.name]

    def int_range(self, t) -> tuple[int, int] | None:
        c = self.core(t)
        if isinstance(c, c_ast.Enum):
            return _INT_RANGES["int"]
        if not isinstance(c, c_ast.IdentifierType):
            return None
        names = [n for n in c.names if n not in ("signed",)]
        if any(n in ("float", "double", "void") for n in names):
            return None
        unsigned = "unsigned" in names
        names = [n for n in names if n != "unsigned"]
        if names == ["int"] or not names:
            base = "int"
        elif names.count("long") == 2:
            base = "long long"
        elif "long" in names:
            base = "long"
        elif "short" in names:
            base = "short"
        elif "char" in names:
            return _INT_RANGES["unsigned char" if unsigned else "signed char"]
        elif "_Bool" in names:
            return _INT_RANGES["_Bool"]
        else:
            return None
        key = f"unsigned {base}" if unsigned else base
        return _INT_RANGES.get(key, _INT_RANGES.get(base))

    def signature(self, t, depth: int = 0, seen: frozenset = frozenset()) -> str:
        """typedef 를 끝까지 풀어 쓴 타입 문자열 (typedef/구조체 정의 변경 감지용)."""
        if t is None or depth > 32:
            return "?"
        if isinstance(t, (c_ast.TypeDecl, c_ast.Typename)):
            q = " ".join(t.quals or [])
            return (q + " " if q else "") + self.signature(t.type, depth + 1, seen)
        if isinstance(t, c_ast.IdentifierType):
            if len(t.names) == 1 and t.names[0] in self.typedefs:
                return self.signature(self.typedefs[t.names[0]], depth + 1, seen)
            return " ".join(t.names)
        if isinstance(t, c_ast.PtrDecl):
            q = " ".join(t.quals or [])
            inner = self.signature(t.type, depth + 1, seen)
            return f"ptr{('<' + q + '>') if q else ''}({inner})"
        if isinstance(t, c_ast.ArrayDecl):
            from pycparser import c_generator
            dim = c_generator.CGenerator().visit(t.dim) if t.dim is not None else ""
            return f"array[{dim}]({self.signature(t.type, depth + 1, seen)})"
        if isinstance(t, c_ast.FuncDecl):
            params = []
            if t.args:
                for p in t.args.params:
                    params.append("..." if isinstance(p, c_ast.EllipsisParam) else self.signature(p.type, depth + 1, seen))
            return f"func({','.join(params)})->{self.signature(t.type, depth + 1, seen)}"
        if isinstance(t, (c_ast.Struct, c_ast.Union)):
            kind = type(t).__name__.lower()
            full = self.core(t)
            key = (kind, t.name)
            if t.name and key in seen:
                return f"{kind} {t.name}"
            decls = getattr(full, "decls", None)
            if not decls:
                return f"{kind} {t.name or ''}"
            seen = seen | {key}
            members = []
            for d in decls:
                bits = ""
                if d.bitsize is not None:
                    from pycparser import c_generator
                    bits = ":" + c_generator.CGenerator().visit(d.bitsize)
                members.append(f"{d.name}:{self.signature(d.type, depth + 1, seen)}{bits}")
            return f"{kind} {t.name or ''}{{{';'.join(members)}}}"
        if isinstance(t, c_ast.Enum):
            vals = []
            if t.values:
                from pycparser import c_generator
                for e in t.values.enumerators:
                    vals.append(e.name + ("=" + c_generator.CGenerator().visit(e.value) if e.value is not None else ""))
            return f"enum {t.name or ''}{{{','.join(vals)}}}"
        return type(t).__name__


def _walk(node):
    stack = [node]
    while stack:
        n = stack.pop()
        if n is None:
            continue
        yield n
        stack.extend(c for _, c in n.children())


# ---------------------------------------------------------------------------
# 번역 단위
# ---------------------------------------------------------------------------


@dataclass
class TranslationUnit:
    rel: str
    ast: c_ast.FileAST | None
    env: TypeEnv
    error: str | None = None
    lines: dict[str, list[str]] = field(default_factory=dict)


def parse_tu(root: Path, rel: str, cfg: BuildConfig) -> TranslationUnit:
    root = Path(root).resolve()
    env = TypeEnv()
    try:
        text = preprocess(root, rel, cfg)
    except (RuntimeError, OSError) as e:
        return TranslationUnit(rel, None, env, str(e))
    try:
        ast = c_parser.CParser().parse(text, str(root / rel))
    except Exception as e:  # pycparser.ParseError 등
        return TranslationUnit(rel, None, env, f"파싱 실패: {e}")
    env.collect(ast)
    return TranslationUnit(rel, ast, env)


def _parse_job(args):
    return parse_tu(*args)


def load_version(root: Path, files: list[str], cfg: BuildConfig, jobs: int = 1) -> list[TranslationUnit]:
    """파일별 전처리·파싱. jobs > 1 이면 프로세스 병렬로 처리한다."""
    if jobs > 1 and len(files) > 1:
        import concurrent.futures as cf
        try:
            with cf.ProcessPoolExecutor(max_workers=min(jobs, len(files))) as ex:
                return list(ex.map(_parse_job, [(root, rel, cfg) for rel in files], chunksize=1))
        except (OSError, cf.process.BrokenProcessPool):
            pass  # 병렬 실행이 막힌 환경이면 순차 처리
    return [parse_tu(root, rel, cfg) for rel in files]


class SourceLines:
    """보고서에 원본 소스 줄을 보여 주기 위한 캐시."""

    def __init__(self):
        self._cache: dict[str, list[str]] = {}

    def get(self, path: str, line: int) -> str:
        if path not in self._cache:
            try:
                self._cache[path] = Path(path).read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                self._cache[path] = []
        lines = self._cache[path]
        return lines[line - 1].strip() if 0 < line <= len(lines) else ""
