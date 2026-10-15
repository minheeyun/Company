"""libclang 로딩과 python 바인딩에 없는 C API(ctypes) 보조 함수."""

from __future__ import annotations

import ctypes
import os
import shutil
import subprocess
from bisect import bisect_left
from functools import lru_cache

try:
    import clang.cindex as ci
except ImportError:  # pragma: no cover - 설치 안내용
    ci = None


class ClangUnavailable(RuntimeError):
    pass


_configured = False


def require_clang():
    """libclang 을 준비한다. LIBCLANG_PATH 환경변수로 라이브러리 경로를 지정할 수 있다."""
    global _configured
    if ci is None:
        raise ClangUnavailable("python 패키지 'libclang' 이 필요합니다: pip install libclang")
    if not _configured:
        path = os.environ.get("LIBCLANG_PATH")
        if path and not ci.Config.loaded:
            if os.path.isdir(path):
                ci.Config.set_library_path(path)
            else:
                ci.Config.set_library_file(path)
        try:
            ci.conf.lib  # noqa: B018 - 로딩 확인
        except Exception as e:  # pragma: no cover
            raise ClangUnavailable(f"libclang 로딩 실패: {e}") from e
        _setup_ctypes()
        _configured = True
    return ci


_HAS = {}


def _setup_ctypes():
    lib = ci.conf.lib
    Cursor = ci.Cursor

    def bind(name, argtypes, restype):
        try:
            fn = getattr(lib, name)
        except AttributeError:
            _HAS[name] = False
            return
        fn.argtypes = argtypes
        fn.restype = restype
        _HAS[name] = True

    bind("clang_getCursorBinaryOperatorKind", [Cursor], ctypes.c_int)
    bind("clang_getBinaryOperatorKindSpelling", [ctypes.c_int], ci._CXString)
    bind("clang_getCursorUnaryOperatorKind", [Cursor], ctypes.c_int)
    bind("clang_getUnaryOperatorKindSpelling", [ctypes.c_int], ci._CXString)
    bind("clang_Cursor_Evaluate", [Cursor], ctypes.c_void_p)
    bind("clang_EvalResult_getKind", [ctypes.c_void_p], ctypes.c_int)
    bind("clang_EvalResult_isUnsignedInt", [ctypes.c_void_p], ctypes.c_uint)
    bind("clang_EvalResult_getAsLongLong", [ctypes.c_void_p], ctypes.c_longlong)
    bind("clang_EvalResult_getAsUnsigned", [ctypes.c_void_p], ctypes.c_ulonglong)
    bind("clang_EvalResult_dispose", [ctypes.c_void_p], None)
    bind("clang_getAllSkippedRanges", [ctypes.c_void_p], ctypes.POINTER(_CXSourceRangeList))
    bind("clang_disposeSourceRangeList", [ctypes.POINTER(_CXSourceRangeList)], None)


class _CXSourceRangeList(ctypes.Structure):
    pass


if ci is not None:
    _CXSourceRangeList._fields_ = [("count", ctypes.c_uint), ("ranges", ctypes.POINTER(ci.SourceRange))]


# 구버전 libclang 대비용: BinaryOperatorKind 번호 → 철자 (LLVM 17+ 순서)
_BINOPS = ["", ".*", "->*", "*", "/", "%", "+", "-", "<<", ">>", "<=>", "<", ">", "<=", ">=", "==", "!=",
           "&", "^", "|", "&&", "||", "=", "*=", "/=", "%=", "+=", "-=", "<<=", ">>=", "&=", "^=", "|=", ","]
_UNOPS = ["", "++", "--", "++", "--", "&", "*", "+", "-", "~", "!", "__real", "__imag", "__extension__", "co_await"]


def binary_operator(cur) -> str | None:
    if _HAS.get("clang_getCursorBinaryOperatorKind"):
        k = ci.conf.lib.clang_getCursorBinaryOperatorKind(cur)
        if 0 < k < len(_BINOPS):
            return _BINOPS[k]
    return None


def unary_operator(cur) -> tuple[str | None, bool]:
    """(연산자, postfix 여부)"""
    if _HAS.get("clang_getCursorUnaryOperatorKind"):
        k = ci.conf.lib.clang_getCursorUnaryOperatorKind(cur)
        if 0 < k < len(_UNOPS):
            return _UNOPS[k], k in (1, 2)
    return None, False


def evaluate(cur):
    """정수 상수식이면 값을 돌려준다(매크로 전개 결과 포함). 아니면 None."""
    if not _HAS.get("clang_Cursor_Evaluate"):
        return None
    lib = ci.conf.lib
    res = lib.clang_Cursor_Evaluate(cur)
    if not res:
        return None
    try:
        if lib.clang_EvalResult_getKind(res) != 1:  # CXEval_Int
            return None
        if lib.clang_EvalResult_isUnsignedInt(res):
            return int(lib.clang_EvalResult_getAsUnsigned(res))
        return int(lib.clang_EvalResult_getAsLongLong(res))
    finally:
        lib.clang_EvalResult_dispose(res)


def skipped_ranges(tu) -> list[tuple[str, int, int]]:
    """현재 빌드 설정에서 전처리기로 제외된(#if 0 등) 구간 (파일, 시작줄, 끝줄)."""
    if not _HAS.get("clang_getAllSkippedRanges"):
        return []
    lib = ci.conf.lib
    lst = lib.clang_getAllSkippedRanges(tu)
    out = []
    if not lst:
        return out
    try:
        for i in range(lst.contents.count):
            r = lst.contents.ranges[i]
            f = r.start.file
            out.append((f.name if f else "", r.start.line, r.end.line))
    finally:
        lib.clang_disposeSourceRangeList(lst)
    return out


@lru_cache(maxsize=1)
def resource_dir() -> str | None:
    """pip libclang 은 내장 헤더(stddef.h 등)가 없으므로 시스템 clang 의 resource-dir 를 사용한다."""
    env = os.environ.get("CLANG_RESOURCE_DIR")
    if env:
        return env
    exe = shutil.which("clang")
    if not exe:
        return None
    try:
        out = subprocess.run([exe, "-print-resource-dir"], capture_output=True, text=True, timeout=10)
        d = out.stdout.strip()
        return d if d and os.path.isdir(os.path.join(d, "include")) else None
    except Exception:
        return None


class TokenIndex:
    """주 파일 토큰을 한 번만 구해 두고 오프셋 범위로 잘라 쓴다.
    주석, 전처리 지시문 줄, 현재 빌드 설정에서 비활성인 구간의 토큰은 제외한다(의미 비교용)."""

    def __init__(self, tu, path: str, skipped_lines=None):
        f = tu.get_file(path)
        size = os.path.getsize(path)
        ext = ci.SourceRange.from_locations(tu.get_location(path, 0), tu.get_location(path, size))
        toks = []
        for t in tu.get_tokens(extent=ext):
            if t.kind == ci.TokenKind.COMMENT:
                continue
            loc = t.location
            if loc.file is None or loc.file.name != f.name:
                continue
            toks.append((t.extent.start.offset, t.extent.end.offset, t.spelling, loc.line))
        directive = set()
        prev_line = None
        for o, e, sp, ln in toks:
            if ln != prev_line and sp == "#":
                directive.add(ln)
            prev_line = ln
        skip = set(skipped_lines or ())
        toks = [t for t in toks if t[3] not in directive and t[3] not in skip]
        self.toks = toks
        self.starts = [t[0] for t in toks]

    def slice(self, start_off: int, end_off: int):
        i = bisect_left(self.starts, start_off)
        j = bisect_left(self.starts, end_off)
        return self.toks[i:j]

    def text(self, start_off: int, end_off: int) -> str:
        return " ".join(t[2] for t in self.slice(start_off, end_off))
