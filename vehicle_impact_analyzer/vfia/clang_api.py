"""libclang(clang.cindex) 보조 기능.

Python 바인딩이 직접 노출하지 않는 C API(연산자 종류, 상수식 평가, 전처리기로
제외된 구간)를 ctypes로 등록한다. 정밀 분석 단계에서는 이 계층을 LibTooling 기반
추출기 출력(JSON)으로 대체할 수 있도록 project_indexer가 이 모듈만 의존한다.
"""
from __future__ import annotations

import ctypes
import os
import shutil
import subprocess
from functools import lru_cache
from typing import List, Optional, Tuple

import clang.cindex as ci

_LIB_READY = False


class _CXSourceRangeList(ctypes.Structure):
    _fields_ = [("count", ctypes.c_uint), ("ranges", ctypes.POINTER(ci.SourceRange))]


UNARY_KIND = {
    1: "post++", 2: "post--", 3: "pre++", 4: "pre--", 5: "&", 6: "*", 7: "+", 8: "-",
    9: "~", 10: "!", 11: "__real", 12: "__imag", 13: "__extension__", 14: "co_await",
}


def init_libclang() -> None:
    global _LIB_READY
    if _LIB_READY:
        return
    libfile = os.environ.get("VFIA_LIBCLANG")
    if libfile and not ci.Config.loaded:
        ci.Config.set_library_file(libfile)
    lib = ci.conf.lib
    lib.clang_getCursorBinaryOperatorKind.argtypes = [ci.Cursor]
    lib.clang_getCursorBinaryOperatorKind.restype = ctypes.c_int
    lib.clang_getBinaryOperatorKindSpelling.argtypes = [ctypes.c_int]
    lib.clang_getBinaryOperatorKindSpelling.restype = ci._CXString
    lib.clang_getCursorUnaryOperatorKind.argtypes = [ci.Cursor]
    lib.clang_getCursorUnaryOperatorKind.restype = ctypes.c_int
    lib.clang_Cursor_Evaluate.argtypes = [ci.Cursor]
    lib.clang_Cursor_Evaluate.restype = ctypes.c_void_p
    lib.clang_EvalResult_getKind.argtypes = [ctypes.c_void_p]
    lib.clang_EvalResult_getKind.restype = ctypes.c_int
    lib.clang_EvalResult_isUnsignedInt.argtypes = [ctypes.c_void_p]
    lib.clang_EvalResult_isUnsignedInt.restype = ctypes.c_uint
    lib.clang_EvalResult_getAsUnsigned.argtypes = [ctypes.c_void_p]
    lib.clang_EvalResult_getAsUnsigned.restype = ctypes.c_ulonglong
    lib.clang_EvalResult_getAsLongLong.argtypes = [ctypes.c_void_p]
    lib.clang_EvalResult_getAsLongLong.restype = ctypes.c_longlong
    lib.clang_EvalResult_getAsDouble.argtypes = [ctypes.c_void_p]
    lib.clang_EvalResult_getAsDouble.restype = ctypes.c_double
    lib.clang_EvalResult_dispose.argtypes = [ctypes.c_void_p]
    lib.clang_getSkippedRanges.argtypes = [ci.TranslationUnit, ci.File]
    lib.clang_getSkippedRanges.restype = ctypes.POINTER(_CXSourceRangeList)
    lib.clang_disposeSourceRangeList.argtypes = [ctypes.POINTER(_CXSourceRangeList)]
    lib.clang_Cursor_isMacroFunctionLike.argtypes = [ci.Cursor]
    lib.clang_Cursor_isMacroFunctionLike.restype = ctypes.c_uint
    _LIB_READY = True


def binary_op(cursor: ci.Cursor) -> str:
    lib = ci.conf.lib
    k = lib.clang_getCursorBinaryOperatorKind(cursor)
    s = ci._CXString.from_result(lib.clang_getBinaryOperatorKindSpelling(k))
    if s:
        return s
    return _op_from_tokens(cursor) or "?"


def unary_op(cursor: ci.Cursor) -> str:
    k = ci.conf.lib.clang_getCursorUnaryOperatorKind(cursor)
    return UNARY_KIND.get(k, "?")


def _op_from_tokens(cursor: ci.Cursor) -> Optional[str]:
    ch = list(cursor.get_children())
    if not ch:
        return None
    end = ch[0].extent.end.offset
    for t in cursor.get_tokens():
        if t.extent.start.offset >= end and t.kind == ci.TokenKind.PUNCTUATION:
            return t.spelling
    return None


def evaluate(cursor: ci.Cursor):
    """상수식을 평가한다. 정수면 int, 실수면 float, 실패하면 None."""
    lib = ci.conf.lib
    r = lib.clang_Cursor_Evaluate(cursor)
    if not r:
        return None
    try:
        kind = lib.clang_EvalResult_getKind(r)
        if kind == 1:  # CXEval_Int
            if lib.clang_EvalResult_isUnsignedInt(r):
                return int(lib.clang_EvalResult_getAsUnsigned(r))
            return int(lib.clang_EvalResult_getAsLongLong(r))
        if kind == 2:  # CXEval_Float
            return float(lib.clang_EvalResult_getAsDouble(r))
        return None
    finally:
        lib.clang_EvalResult_dispose(r)


def skipped_ranges(tu: ci.TranslationUnit, filename: str) -> List[Tuple[int, int]]:
    """해당 빌드 설정에서 전처리기가 제외한(#if 비활성) 줄 범위."""
    lib = ci.conf.lib
    try:
        f = ci.File.from_name(tu, filename)
    except Exception:
        return []
    rl = lib.clang_getSkippedRanges(tu, f)
    if not rl:
        return []
    out = []
    try:
        for i in range(rl.contents.count):
            r = rl.contents.ranges[i]
            out.append((r.start.line, r.end.line))
    finally:
        lib.clang_disposeSourceRangeList(rl)
    return out


def is_macro_function_like(cursor: ci.Cursor) -> bool:
    return bool(ci.conf.lib.clang_Cursor_isMacroFunctionLike(cursor))


BUILTIN_FALLBACK = os.path.join(os.path.dirname(os.path.abspath(__file__)), "builtin_include")


def _clang_major(text: str) -> Optional[int]:
    import re
    m = re.search(r"clang version (\d+)", text or "")
    return int(m.group(1)) if m else None


def libclang_version() -> str:
    """예: 'clang version 18.1.1'. 알 수 없으면 빈 문자열."""
    try:
        init_libclang()
        f = ci.conf.lib.clang_getClangVersion
        f.argtypes = []
        f.restype = ci._CXString
        return ci._CXString.from_result(f())
    except Exception:
        return ""


def _libclang_path() -> Optional[str]:
    p = os.environ.get("VFIA_LIBCLANG")
    if p:
        return p
    try:
        return getattr(ci.conf.lib, "_name", None)
    except Exception:
        return None


def _resource_candidates() -> List[Tuple[str, str]]:
    """(include 디렉터리, 출처) 후보. 앞쪽이 우선."""
    import glob
    out: List[Tuple[str, str]] = []
    env = os.environ.get("VFIA_CLANG_RESOURCE_DIR")
    if env:
        out.append((os.path.join(env, "include"), "VFIA_CLANG_RESOURCE_DIR"))
    # 1) libclang 라이브러리 위치 기준(LLVM 배포판: bin/libclang.dll, lib/libclang.so -> lib/clang/<ver>/include)
    lib = _libclang_path()
    if lib and os.path.isabs(lib):
        d = os.path.dirname(lib)
        for base in (d, os.path.dirname(d)):
            for inc in sorted(glob.glob(os.path.join(base, "lib", "clang", "*", "include")), reverse=True):
                out.append((inc, "libclang 위치"))
            for inc in sorted(glob.glob(os.path.join(base, "clang", "*", "include")), reverse=True):
                out.append((inc, "libclang 위치"))
    # 2) PATH의 clang 실행 파일
    for exe in ("clang", "clang-20", "clang-19", "clang-18", "clang-17", "clang-16", "clang-15"):
        path = shutil.which(exe)
        if not path:
            continue
        try:
            r = subprocess.run([path, "-print-resource-dir"], capture_output=True, timeout=10)
            res = r.stdout.decode("utf-8", errors="replace").strip()
        except Exception:
            continue
        if res:
            out.append((os.path.join(res, "include"), f"{exe} -print-resource-dir"))
    # 3) 흔한 설치 위치(Windows LLVM 설치본, Visual Studio 동봉 Clang, Linux 배포판)
    pats: List[str] = []
    if os.name == "nt":
        for var in ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432", "LOCALAPPDATA"):
            base = os.environ.get(var)
            if base:
                pats.append(os.path.join(base, "LLVM", "lib", "clang", "*", "include"))
                pats.append(os.path.join(base, "Programs", "LLVM", "lib", "clang", "*", "include"))
                pats.append(os.path.join(base, "Microsoft Visual Studio", "*", "*", "VC", "Tools", "Llvm", "x64",
                                         "lib", "clang", "*", "include"))
                pats.append(os.path.join(base, "Microsoft Visual Studio", "*", "*", "VC", "Tools", "Llvm",
                                         "lib", "clang", "*", "include"))
        for drive_root in ("C:\\msys64", "C:\\tools\\msys64"):
            pats.append(os.path.join(drive_root, "*", "lib", "clang", "*", "include"))
        scoop = os.path.join(os.path.expanduser("~"), "scoop", "apps", "llvm", "current", "lib", "clang", "*",
                             "include")
        pats.append(scoop)
    else:
        pats += ["/usr/lib/llvm-*/lib/clang/*/include", "/usr/lib/clang/*/include", "/usr/local/lib/clang/*/include",
                 "/opt/homebrew/opt/llvm/lib/clang/*/include", "/usr/local/opt/llvm/lib/clang/*/include"]

    def ver_key(p: str):
        import re
        m = re.search(r"[\\/]clang[\\/](\d+)", p)
        return int(m.group(1)) if m else 0
    found = []
    for pat in pats:
        found += glob.glob(pat)
    for inc in sorted(set(found), key=ver_key, reverse=True):
        out.append((inc, "설치 위치 탐색"))
    return out


@lru_cache(maxsize=1)
def resource_include_info() -> dict:
    """컴파일러 내장 헤더(stddef.h, stdbool.h, stdint.h 등) 위치와 출처.

    pip libclang 휠에는 내장 헤더가 없으므로 시스템 Clang/LLVM의 resource-dir을 찾는다. libclang과
    주 버전이 같은 것을 우선하고, 하나도 없으면 vfia/builtin_include(최소 freestanding 헤더)를 쓴다.
    """
    if os.environ.get("VFIA_CLANG_RESOURCE_DIR", "").lower() == "builtin":
        return {"path": BUILTIN_FALLBACK, "source": "vfia 대체 헤더(VFIA_CLANG_RESOURCE_DIR=builtin)", "fallback": True}
    want = _clang_major(libclang_version())
    cands = [(inc, src) for inc, src in _resource_candidates() if os.path.isfile(os.path.join(inc, "stdbool.h"))]
    if want:
        same = [c for c in cands if f"{os.sep}{want}{os.sep}" in c[0] + os.sep or f"/{want}/" in c[0] + "/"]
        if same:
            cands = same + [c for c in cands if c not in same]
    if cands:
        inc, src = cands[0]
        return {"path": inc, "source": src, "fallback": False}
    return {"path": BUILTIN_FALLBACK, "source": "vfia 대체 헤더(시스템 Clang 내장 헤더를 찾지 못함)",
            "fallback": True}


def detect_resource_include() -> Optional[str]:
    return resource_include_info()["path"]


SIGNED_INT = {ci.TypeKind.CHAR_S, ci.TypeKind.SCHAR, ci.TypeKind.SHORT, ci.TypeKind.INT,
              ci.TypeKind.LONG, ci.TypeKind.LONGLONG, ci.TypeKind.INT128, ci.TypeKind.WCHAR}
UNSIGNED_INT = {ci.TypeKind.CHAR_U, ci.TypeKind.UCHAR, ci.TypeKind.USHORT, ci.TypeKind.UINT,
                ci.TypeKind.ULONG, ci.TypeKind.ULONGLONG, ci.TypeKind.UINT128,
                ci.TypeKind.CHAR16, ci.TypeKind.CHAR32}
FLOAT_KINDS = {ci.TypeKind.FLOAT, ci.TypeKind.DOUBLE, ci.TypeKind.LONGDOUBLE, ci.TypeKind.HALF,
               ci.TypeKind.FLOAT128}


def record_name(decl: ci.Cursor, fallback_type: Optional[ci.Type] = None) -> str:
    sp = decl.spelling or ""
    if sp and "(unnamed" not in sp and "(anonymous" not in sp:
        return sp
    if fallback_type is not None:
        s = fallback_type.spelling
        if s and "(unnamed" not in s and "(anonymous" not in s:
            return s.replace("const ", "").replace("volatile ", "").strip()
    t = decl.type.spelling if decl.type else ""
    if t and "(unnamed" not in t and "(anonymous" not in t:
        return t
    loc = decl.location
    return f"anon@{os.path.basename(loc.file.name) if loc.file else '?'}:{loc.line}"


def type_desc(t: ci.Type, depth: int = 0) -> dict:
    """심벌릭 실행·영향 분석에 필요한 최소한의 자료형 정보."""
    can = t.get_canonical()
    k = can.kind
    d: dict = {"n": t.spelling, "cn": can.spelling}
    try:
        if t.is_volatile_qualified() or can.is_volatile_qualified():
            d["vol"] = True
        if t.is_const_qualified() or can.is_const_qualified():
            d["const"] = True
    except Exception:
        pass
    if k == ci.TypeKind.BOOL:
        d.update(k="b", b=8, s=False)
    elif k in SIGNED_INT or k in UNSIGNED_INT:
        d.update(k="i", b=max(8, can.get_size() * 8), s=k in SIGNED_INT)
    elif k == ci.TypeKind.ENUM:
        decl = can.get_declaration()
        try:
            et = decl.enum_type.get_canonical()
            d.update(k="i", b=max(8, et.get_size() * 8), s=et.kind in SIGNED_INT, enum=record_name(decl, t))
        except Exception:
            d.update(k="i", b=32, s=True)
    elif k in FLOAT_KINDS:
        d.update(k="fl", b=max(16, can.get_size() * 8))
    elif k == ci.TypeKind.POINTER:
        pt = can.get_pointee()
        try:
            psz = can.get_size() * 8
        except Exception:
            psz = 64
        d.update(k="p", b=psz if psz > 0 else 64)
        if depth < 2:
            d["pt"] = type_desc(pt, depth + 1)
    elif k in (ci.TypeKind.CONSTANTARRAY, ci.TypeKind.INCOMPLETEARRAY, ci.TypeKind.VARIABLEARRAY,
               ci.TypeKind.DEPENDENTSIZEDARRAY):
        d.update(k="a")
        try:
            d["cnt"] = can.get_array_size()
        except Exception:
            d["cnt"] = -1
        if depth < 3:
            d["el"] = type_desc(can.get_array_element_type(), depth + 1)
    elif k == ci.TypeKind.RECORD:
        decl = can.get_declaration()
        d.update(k="r", rec=record_name(decl, t))
        try:
            d["size"] = can.get_size()
        except Exception:
            pass
    elif k in (ci.TypeKind.FUNCTIONPROTO, ci.TypeKind.FUNCTIONNOPROTO):
        d.update(k="f")
    elif k == ci.TypeKind.VOID:
        d.update(k="v")
    else:
        d.update(k="o")
    return d


def is_ptrish(td: Optional[dict]) -> bool:
    """points-to 제약을 만들 필요가 있는 자료형인가(포인터·포인터 배열·구조체)."""
    if not td:
        return False
    k = td.get("k")
    if k in ("p", "r"):
        return True
    if k == "a":
        return is_ptrish(td.get("el"))
    return False
