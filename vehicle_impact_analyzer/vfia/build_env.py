"""차량 ECU 빌드 환경 재현 계층.

양산 ECU는 TASKING, Green Hills, Wind River Diab, HighTec 같은 Clang이 아닌 컴파일러를 많이
쓰고 compile_commands.json이 기본으로 나오지 않는다. 이 모듈은
  * 빌드 로그(make/ninja/IDE 출력)에서 컴파일 명령을 추출해 compile DB를 만들고
  * 원래 컴파일러 옵션을 Clang 옵션으로 변환하며(include/define/표준만 유지, 나머지는 기록 후 제거)
  * 원래 컴파일러가 미리 정의하는 매크로(__TASKING__ 등)를 -D로 넣고
  * __interrupt, __far, intrinsic 같은 확장을 '호환 헤더'로 치환(-include)하고
  * 같은 데이터 모델(ILP32)의 대리 target(--target)과 -ffreestanding을 지정한다.
변환 결과와 제거한 옵션은 분석 결과에 남겨 재현성을 확인할 수 있게 한다.
"""
from __future__ import annotations

import json
import os
import re
import shlex
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

# 컴파일러 프로필: 실행 파일 이름, 미리 정의 매크로, 대리 target, 옵션 형식
PROFILES: Dict[str, dict] = {
    "tasking": {
        "exe": ["ctc", "cctc", "ctc.exe", "cctc.exe", "carc", "cptc"],
        "predefined": {"__TASKING__": "1", "__CTC__": "1", "__TRICORE__": "1", "__CORE_TC162__": "1"},
        "target": "arm-none-eabi", "std_map": {"--iso=99": "-std=c99", "--iso=90": "-std=c89", "--iso=11": "-std=c11"},
        "include": ["-I", "--include-directory="], "define": ["-D", "--define="], "undef": ["-U", "--undefine="],
        "preinclude": ["-H", "--include-file="], "flag_map": {"--uchar": "-funsigned-char", "-u": "-funsigned-char"},
        "compat": """
#define __interrupt(x)
#define __interrupt_fast(x)
#define __vector_table(x)
#define __bisr_(x)
#define __far
#define __near
#define __a0
#define __a1
#define __a8
#define __a9
#define __align(x)
#define __at(x)
#define __private0
#define __private1
#define __share
#define __clone
#define __noinline
#define __unaligned
""",
    },
    "ghs": {
        "exe": ["ccrh850", "ccv850", "ccppc", "ccarm", "cxrh850", "ccrh850.exe", "ccv850.exe", "ccppc.exe"],
        "predefined": {"__ghs__": "1", "__GHS_VERSION_NUMBER": "201754", "__EDG__": "1"},
        "target": "arm-none-eabi", "std_map": {"-c99": "-std=c99", "-C99": "-std=c99", "--c99": "-std=c99"},
        "include": ["-I"], "define": ["-D"], "undef": ["-U"], "preinclude": ["-include", "-preinclude="],
        "flag_map": {"--unsigned_chars": "-funsigned-char", "--signed_chars": "-fsigned-char"},
        "compat": """
#define __interrupt
#define __fast_interrupt
#define __packed
#define __ghs_noprofile
#define __ghs_zda
#define __ghs_tda
#define __ghs_sda
#define __attribute__(x)
""",
    },
    "diab": {
        "exe": ["dcc", "dplus", "dcc.exe", "dplus.exe"],
        "predefined": {"__DCC__": "1", "__DIAB__": "1", "__ppc": "1", "__EABI__": "1"},
        "target": "powerpc-none-eabi", "std_map": {"-Xdialect-c99": "-std=c99", "-Xdialect-c89": "-std=c89"},
        "include": ["-I"], "define": ["-D"], "undef": ["-U"], "preinclude": ["-i="],
        "compat": """
#define __interrupt__
#define interrupt
#define __extended__
#define __packed__
#define __inline__ inline
""",
    },
    "hightec": {
        "exe": ["tricore-gcc", "tricore-elf-gcc", "tricore-gcc.exe", "tricore-elf-gcc.exe"],
        "predefined": {"__HIGHTEC__": "1", "__TRICORE__": "1", "__tricore__": "1"},
        "target": "arm-none-eabi", "std_map": {}, "include": ["-I", "-isystem", "-iquote"],
        "define": ["-D"], "undef": ["-U"], "preinclude": ["-include"], "gcc_like": True,
        "compat": """
#define __interrupt(x)
#define __far
#define __near
""",
    },
    "gcc": {"exe": ["gcc", "cc", "arm-none-eabi-gcc", "powerpc-eabi-gcc", "x86_64-linux-gnu-gcc"], "predefined": {},
            "target": None, "std_map": {}, "include": ["-I", "-isystem", "-iquote"], "define": ["-D"],
            "undef": ["-U"], "preinclude": ["-include"], "gcc_like": True, "compat": ""},
    "clang": {"exe": ["clang", "clang-18", "clang-17", "clang-16"], "predefined": {}, "target": None, "std_map": {},
              "include": ["-I", "-isystem", "-iquote"], "define": ["-D"], "undef": ["-U"], "preinclude": ["-include"],
              "gcc_like": True, "compat": ""},
}

# 자주 쓰는 intrinsic: 매크로로 지우지 않고 함수로 선언해 '외부 호출(스텁)'로 남긴다.
COMMON_INTRINSICS = """
void __enable(void); void __disable(void); void __nop(void); void __isync(void); void __dsync(void);
void __debug(void); unsigned int __mfcr(int reg); void __mtcr(int reg, unsigned int val);
unsigned int __clz(unsigned int x); void __syscall(int n); unsigned int __disable_and_save(void);
void __restore(unsigned int s); void __svlcx(void); void __rslcx(void);
"""


# 자료형 크기·부호 등 분석 의미에 영향을 주는 옵션은 유지
_KEEP_FLAGS = {"-m32", "-m64", "-mx32", "-funsigned-char", "-fsigned-char", "-fshort-enums", "-fno-short-enums",
               "-fshort-wchar", "-ansi", "-fms-extensions"}


@dataclass
class TranslatedArgs:
    args: List[str]
    dropped: List[str] = field(default_factory=list)
    profile: str = "gcc"
    notes: List[str] = field(default_factory=list)


def detect_profile(argv: List[str], configured: Optional[str] = None) -> str:
    if configured and configured != "auto":
        return configured
    if not argv:
        return "gcc"
    exe = os.path.basename(argv[0]).lower()
    for name, p in PROFILES.items():
        if exe in p["exe"]:
            return name
    if exe.endswith(("gcc", "gcc.exe")):
        return "gcc"
    if exe.startswith("clang"):
        return "clang"
    return "gcc"


def _abs(directory: str, p: str) -> str:
    p = p.strip('"')
    return p if os.path.isabs(p) else os.path.normpath(os.path.join(directory, p))


def translate(argv: List[str], directory: str, src: str, profile_name: str) -> TranslatedArgs:
    """원래 컴파일러 인자 -> Clang 인자. include·define·undef·사전 포함·언어 표준만 옮긴다."""
    prof = PROFILES.get(profile_name, PROFILES["gcc"])
    out: List[str] = []
    dropped: List[str] = []
    i = 1 if argv and not argv[0].startswith("-") else 0
    srcn = os.path.normpath(src)

    def match(a: str, prefixes: List[str]) -> Optional[Tuple[str, Optional[str]]]:
        for pf in prefixes:
            if a == pf.rstrip("="):
                return pf, None                 # 값이 다음 인자
            if a.startswith(pf) and len(a) > len(pf):
                return pf, a[len(pf):]
        return None

    while i < len(argv):
        a = argv[i]
        nxt = argv[i + 1] if i + 1 < len(argv) else None
        if a.startswith(("@",)):
            dropped.append(a)                   # 응답 파일: 빌드 로그 파서가 미리 풀어야 함
            i += 1
            continue
        if _abs(directory, a) == srcn:
            i += 1
            continue
        if a in prof["std_map"]:
            out.append(prof["std_map"][a])
            i += 1
            continue
        if a.startswith("-std=") or a in _KEEP_FLAGS or a.startswith("--target=") or a in prof.get("flag_map", {}):
            out.append(prof.get("flag_map", {}).get(a, a))
            i += 1
            continue
        if a == "-target" and nxt:
            out.append(f"--target={nxt}")
            i += 2
            continue
        handled = False
        for kind, pfs in (("I", prof["include"]), ("D", prof["define"]), ("U", prof["undef"]),
                          ("include", prof["preinclude"])):
            m = match(a, pfs)
            if not m:
                continue
            val = m[1]
            if val is None:
                if nxt is None:
                    break
                val = nxt
                i += 1
            if kind == "I":
                flag = "-isystem" if m[0] == "-isystem" else "-I"
                out += [flag, _abs(directory, val)] if flag == "-isystem" else [f"-I{_abs(directory, val)}"]
            elif kind == "D":
                out.append(f"-D{val}")
            elif kind == "U":
                out.append(f"-U{val}")
            else:
                out += ["-include", _abs(directory, val)]
            handled = True
            break
        if handled:
            i += 1
            continue
        if prof.get("gcc_like") and a in ("-o", "-MF", "-MT", "-MQ", "-x"):
            i += 2
            continue
        if a in ("-c", "-S", "-E", "-MD", "-MMD", "-MP", "-M", "-MM", "-pipe", "-o"):
            i += 2 if a == "-o" else 1
            continue
        if prof.get("gcc_like") and (a.startswith("-f") or a.startswith("-m") or a.startswith("-W") or a.startswith("-O")
                                     or a.startswith("-g")):
            # 코드 생성 옵션은 분석 의미에 영향이 없거나 Clang이 모를 수 있어 제거(기록)
            dropped.append(a)
            i += 1
            continue
        dropped.append(a)
        i += 1
    return TranslatedArgs(out, dropped, profile_name)


def compat_header(profile_name: str, cache_dir: str, extra_defines: Dict[str, str] = None,
                  extra_lines: str = "") -> str:
    """컴파일러 확장 키워드·intrinsic 치환 헤더를 만들어 경로를 돌려준다(-include 대상)."""
    prof = PROFILES.get(profile_name, PROFILES["gcc"])
    os.makedirs(cache_dir, exist_ok=True)
    path = os.path.join(cache_dir, f"vfia_compat_{profile_name}.h")
    lines = ["/* VFIA 분석용 호환 헤더(자동 생성). 실제 빌드에는 사용하지 않는다. */",
             f"/* 컴파일러 프로필: {profile_name} */", "#ifndef VFIA_COMPAT_H", "#define VFIA_COMPAT_H",
             prof.get("compat", "")]
    if profile_name not in ("gcc", "clang"):
        lines.append(COMMON_INTRINSICS)
    for k, v in (extra_defines or {}).items():
        lines.append(f"#define {k} {v}")
    if extra_lines:
        lines.append(extra_lines)
    lines.append("#endif")
    text = "\n".join(lines) + "\n"
    old = None
    if os.path.isfile(path):
        with open(path, "r", encoding="utf-8") as f:
            old = f.read()
    if old != text:
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
    return path


def clang_args_for(profile_name: str, translated: List[str], build, cache_dir: Optional[str]) -> Tuple[List[str], List[str]]:
    """변환된 인자에 target·freestanding·사전 정의 매크로·호환 헤더를 더한다."""
    prof = PROFILES.get(profile_name, PROFILES["gcc"])
    notes: List[str] = []
    args = list(translated)
    target = getattr(build, "target", None) or prof.get("target")
    if target and getattr(build, "target", "auto") != "host":
        args.insert(0, f"--target={target}")
        if profile_name not in ("gcc", "clang"):
            notes.append(f"{profile_name}: 데이터 모델이 같은 대리 target '{target}'로 파싱(아키텍처 고유 의미는 근사)")
    if profile_name not in ("gcc", "clang") or getattr(build, "freestanding", False):
        args.append("-ffreestanding")
    sys_inc = list(getattr(build, "system_includes", []) or [])
    if sys_inc:
        args.append("-nostdinc")
        for d in sys_inc:
            args += ["-isystem", d]
        notes.append("툴체인 시스템 헤더를 -nostdinc + -isystem으로 사용")
    elif profile_name not in ("gcc", "clang"):
        notes.append("툴체인 시스템 헤더 경로 미지정: clang 내장 헤더(stdint.h 등)로 대체")
    for k, v in prof.get("predefined", {}).items():
        args.append(f"-D{k}={v}")
    for k, v in (getattr(build, "predefined", {}) or {}).items():
        args.append(f"-D{k}={v}")
    if cache_dir and getattr(build, "compat", True) and (prof.get("compat") or profile_name not in ("gcc", "clang")
                                                         or getattr(build, "compat_defines", None)):
        h = compat_header(profile_name, cache_dir, getattr(build, "compat_defines", None),
                          getattr(build, "compat_lines", "") or "")
        args += ["-include", h]
    # 확장 문법으로 인한 연쇄 오류를 줄이되, 경고로 남긴다
    args += ["-Wno-error=implicit-function-declaration", "-Wno-unknown-pragmas", "-Wno-unknown-attributes",
             "-Wno-error=int-conversion", "-Wno-error=incompatible-function-pointer-types"]
    return args, notes


# ---------------------------------------------------------------------------
_ENTER_DIR = re.compile(r"(?:make|ninja)(?:\[\d+\])?: Entering directory [`'\"](.+?)['\"`]")
_CD = re.compile(r"^\s*cd\s+(?:/d\s+)?(\"[^\"]+\"|\S+)\s*(?:&&|;|$)")


def parse_build_log(log_text: str, base_dir: str, profile: str = "auto",
                    compiler_regex: Optional[str] = None) -> List[dict]:
    """빌드 로그에서 컴파일 명령을 추출해 compile DB 항목(arguments 형식)을 만든다.

    Bear처럼 컴파일러 호출을 가로챌 수 없는 환경(Windows IDE 빌드 등)용. 'make: Entering directory'와
    'cd dir &&' 형식의 작업 디렉터리 변경을 따라간다. 응답 파일(@file)은 펼친다.
    """
    exes = set()
    for name, p in PROFILES.items():
        if profile in ("auto", name):
            exes.update(e.lower() for e in p["exe"])
    rx = re.compile(compiler_regex) if compiler_regex else None
    cwd = base_dir
    out = []
    for raw in log_text.splitlines():
        line = raw.strip()
        if not line:
            continue
        m = _ENTER_DIR.search(line)
        if m:
            cwd = m.group(1)
            continue
        m = _CD.match(line)
        if m:
            cwd = _abs(cwd, m.group(1).strip('"'))
            line = line[m.end():].strip()
            if not line:
                continue
        try:
            toks = shlex.split(line, posix=os.name != "nt")
        except ValueError:
            continue
        if not toks:
            continue
        exe = os.path.basename(toks[0].strip('"')).lower()
        if not ((rx and rx.search(toks[0])) or exe in exes):
            continue
        expanded: List[str] = []
        for t in toks:
            if t.startswith("@"):
                rp = _abs(cwd, t[1:])
                try:
                    with open(rp, "r", encoding="utf-8", errors="replace") as f:
                        expanded += shlex.split(f.read())
                    continue
                except OSError:
                    pass
            expanded.append(t)
        srcs = [t for t in expanded[1:] if t.lower().endswith((".c", ".cc", ".cpp"))]
        if not srcs:
            continue
        for s in srcs:
            out.append({"directory": cwd, "file": _abs(cwd, s), "arguments": expanded,
                        "compiler_profile": detect_profile(expanded, profile)})
    return out


def write_compile_db(entries: List[dict], path: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(entries, f, indent=2, ensure_ascii=False)


# ---------------------------------------------------------------------------
_FUNC_DEF = re.compile(
    r"^[ \t]*(?!(?:if|while|for|switch|return|else|do|sizeof|case)\b)"
    r"[A-Za-z_][\w \t\*]*?\b([A-Za-z_]\w*)[ \t]*\(([^;{}()]|\([^;{}()]*\))*\)[ \t\r\n]*\{",
    re.M)


def function_definitions_in_text(text: str, inactive: List[Tuple[int, int]]) -> Dict[str, int]:
    """소스 텍스트에서 함수 정의로 보이는 이름 -> 줄 번호(비활성 #if 구간·매크로 줄 제외)."""
    out: Dict[str, int] = {}
    # 주석 제거(줄 수 유지)
    def blank(m):
        return re.sub(r"[^\n]", " ", m.group(0))
    t = re.sub(r"/\*.*?\*/", blank, text, flags=re.S)
    t = re.sub(r"//[^\n]*", blank, t)
    for m in _FUNC_DEF.finditer(t):
        line = t.count("\n", 0, m.start(1)) + 1
        line_text = t.split("\n")[line - 1].lstrip()
        if line_text.startswith("#"):
            continue
        if any(a <= line <= b for a, b in inactive):
            continue
        name = m.group(1)
        if name in ("if", "while", "for", "switch"):
            continue
        out.setdefault(name, line)
    return out
