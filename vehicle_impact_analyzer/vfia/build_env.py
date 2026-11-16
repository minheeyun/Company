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
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

from . import portable as pt

# 컴파일러 프로필: 실행 파일 이름, 미리 정의 매크로, 대리 target, 옵션 형식
PROFILES: Dict[str, dict] = {
    "tasking": {
        "exe": ["ctc", "cctc", "ctc.exe", "cctc.exe", "carc", "ccarc", "cptc", "carm", "cc51", "c51", "cc166", "c166",
                "ccpcp", "cpcp", "ccmcs", "cmcs"],
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
    exe = _exe_name(argv[0])
    for name, p in PROFILES.items():
        if exe in {_exe_name(x) for x in p["exe"]}:
            return name
    if exe.endswith("gcc"):
        return "gcc"
    if exe.startswith("clang"):
        return "clang"
    return "gcc"


def _exe_name(p: str) -> str:
    """'C:\\Tools\\ctc.EXE' -> 'ctc' (경로·확장자·대소문자 무시)."""
    exe = pt.basename_any(p.strip('"')).lower()
    return exe[:-4] if exe.endswith(".exe") else exe


def _abs(directory: str, p: str) -> str:
    p = p.strip('"')
    return p if pt.is_abs_any(p) else os.path.normpath(os.path.join(directory, p))


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
_CD = re.compile(r"^\s*cd\s+(?:/d\s+)?(\"[^\"]+\"|\S+)\s*(?:&&|;|$)", re.I)


_SRC_EXT = (".c", ".cc", ".cpp", ".cxx")
_SKIP_DIRS = {"debug", "release", "build", "out", "output", "bin", "obj", "objs", "lst", "list", "doc", "docs", "test",
              "tests", "__pycache__"}


def _known_exes() -> Dict[str, str]:
    """알려진 컴파일러 실행 파일 이름 -> 프로필."""
    out: Dict[str, str] = {}
    for name, p in PROFILES.items():
        for e in p["exe"]:
            out.setdefault(_exe_name(e), name)
    return out


def _join_continuations(lines: List[str]) -> List[str]:
    """줄 끝의 '\\'(make·sh) 또는 '^'(cmd) 이어쓰기를 한 줄로."""
    out: List[str] = []
    buf = ""
    for raw in lines:
        line = raw.rstrip()
        if line.endswith((" \\", " ^")) or line in ("\\", "^"):
            buf += line[:-1] + " "
            continue
        out.append(buf + line)
        buf = ""
    if buf:
        out.append(buf)
    return out


def _candidate_dirs(base_dir: str, limit: int = 300) -> List[str]:
    """상대 경로 소스를 찾을 작업 디렉터리 후보: 기준 폴더와 그 아래 2단계까지(빌드 폴더 Debug 등 포함).

    IDE(이클립스 기반 TASKING 등)는 '<프로젝트>/Debug' 같은 빌드 폴더에서 '../src/a.c'로 컴파일하지만 로그에
    작업 디렉터리를 남기지 않는다. makefile이 있는 폴더를 먼저 본다.
    """
    cands, withmk = [base_dir], []
    try:
        for name in sorted(os.listdir(base_dir)):
            d1 = os.path.join(base_dir, name)
            if not os.path.isdir(d1) or name.startswith("."):
                continue
            sub = [d1]
            try:
                sub += [os.path.join(d1, n2) for n2 in sorted(os.listdir(d1))
                        if os.path.isdir(os.path.join(d1, n2)) and not n2.startswith(".")]
            except OSError:
                pass
            for d in sub:
                try:
                    has_mk = any(f.lower() in ("makefile", "subdir.mk") or f.lower().endswith(".mk") for f in os.listdir(d))
                except OSError:
                    has_mk = False
                (withmk if has_mk else cands).append(d)
                if len(cands) + len(withmk) > limit:
                    break
    except OSError:
        pass
    return [base_dir] + withmk + cands[1:]


def _find_file(rel: str, dirs: List[str]) -> Optional[str]:
    for d in dirs:
        p = _abs(d, rel)
        if os.path.isfile(p):
            return p
    return None


def _expand_option_files(toks: List[str], cwd: str, windows: bool, profile: str,
                         more_dirs: Optional[List[str]] = None) -> List[str]:
    """응답 파일(@file)과 TASKING 옵션 파일(-f file, -ffile, --option-file=file)을 펼친다.

    상대 경로면 작업 디렉터리, 그다음 빌드 폴더 후보(more_dirs)에서 찾는다.
    """
    dirs = [cwd] + list(more_dirs or [])
    out: List[str] = []
    i = 0
    while i < len(toks):
        t = toks[i]
        path = None
        tasking = profile in ("tasking", "auto")
        if t.startswith("@") and len(t) > 1:
            path = _find_file(t[1:], dirs)
        elif t == "-f" and i + 1 < len(toks) and tasking:
            path = _find_file(toks[i + 1], dirs)
            if path:
                i += 1
        elif t.startswith("--option-file="):
            path = _find_file(t.split("=", 1)[1], dirs)
        elif t.startswith("-f") and len(t) > 2 and tasking and re.search(r"[\\/.]", t[2:]):
            path = _find_file(t[2:], dirs)
        if path is not None:
            try:
                text = pt.read_text_any(path)
                out += pt.split_command(" ".join(text.splitlines()), windows or pt.looks_windows_command(text))
                i += 1
                continue
            except OSError:
                pass
        out.append(t)
        i += 1
    return out


def parse_build_log(log_text: str, base_dir: str, profile: str = "auto",
                    compiler_regex: Optional[str] = None) -> List[dict]:
    """빌드 로그에서 컴파일 명령을 추출해 compile DB 항목(arguments 형식)을 만든다.

    Bear처럼 컴파일러 호출을 가로챌 수 없는 환경(Windows IDE 빌드 등)용.
      * 'make: Entering directory', 'cd dir &&', 'cd /d dir &&'로 작업 디렉터리를 따라간다.
      * 줄 앞에 시각·로그 수준·'Executing:' 같은 글자가 있어도 컴파일러 이름(경로 포함, .exe 무관)을 찾아 그 뒤를 쓴다.
      * 응답 파일(@file)과 TASKING 옵션 파일(-f file)을 펼친다. 이어쓰기 줄(\\, ^)을 합친다.
      * 소스의 상대 경로가 작업 디렉터리 기준으로 없으면 base_dir 아래 빌드 폴더(Debug 등) 기준으로 찾는다.
    profile을 지정하면(auto가 아니면) 찾은 명령의 컴파일러 프로필을 그 값으로 둔다.
    """
    known = _known_exes()
    rx = re.compile(compiler_regex) if compiler_regex else None
    cwd = base_dir
    guess_dir: Optional[str] = None
    cand_dirs: Optional[List[str]] = None
    out = []
    for line in _join_continuations(log_text.splitlines()):
        line = line.strip()
        if not line:
            continue
        m = _ENTER_DIR.search(line)
        if m:
            cwd = m.group(1)
            continue
        low = line.lower()
        if not (any(e in low for e in _SRC_EXT) or "@" in line or " -f" in line or "--option-file" in low
                or re.match(r"\s*cd\s", low)):
            continue
        windows = pt.IS_WINDOWS or pt.looks_windows_command(line)
        # 한 줄에 여러 명령(cd dir && cc ... && cc ...)이면 나눠서 차례로
        segs: List[List[str]] = [[]]
        for t in pt.split_command(line, windows):
            if t in ("&&", "||", ";", "&", "|"):
                segs.append([])
            else:
                segs[-1].append(t)
        for toks in segs:
            if not toks:
                continue
            if toks[0].lower() == "cd" and len(toks) >= 2:
                target = toks[2] if toks[1].lower() == "/d" and len(toks) >= 3 else toks[1]
                cwd = _abs(cwd, target)
                continue
            start = None
            for i, t in enumerate(toks):
                if (rx and rx.search(t)) or _exe_name(t) in known:
                    start = i
                    break
            if start is None:
                continue
            toks = toks[start:]
            prof = profile if profile and profile != "auto" else known.get(_exe_name(toks[0]), "gcc")
            if cand_dirs is None and any(t.startswith(("@", "-f", "--option-file=")) for t in toks):
                cand_dirs = _candidate_dirs(base_dir)
            expanded = _expand_option_files(toks, cwd, windows, prof, ([guess_dir] if guess_dir else []) + (cand_dirs or []))
            srcs = [t for t in expanded[1:] if not t.startswith("-") and t.lower().endswith(_SRC_EXT)]
            for s_ in srcs:
                d = cwd
                f = _abs(d, s_)
                if not os.path.isfile(f) and not pt.is_abs_any(s_.strip('"')):
                    # 작업 디렉터리를 모르는 IDE 로그: 앞서 찾은 빌드 폴더 → 후보 폴더 순으로 상대 경로를 맞춰 본다
                    if guess_dir and os.path.isfile(_abs(guess_dir, s_)):
                        d = guess_dir
                    else:
                        if cand_dirs is None:
                            cand_dirs = _candidate_dirs(base_dir)
                        for c in cand_dirs:
                            if os.path.isfile(_abs(c, s_)):
                                d = guess_dir = c
                                break
                    f = _abs(d, s_)
                out.append({"directory": d, "file": f, "arguments": expanded, "compiler_profile": prof})
    return out


def diagnose_build_log(log_text: str, limit: int = 6) -> List[str]:
    """컴파일 명령을 못 찾았을 때 원인 파악용: 소스 파일(.c)이 나오는 줄과 그 줄의 앞쪽 단어."""
    lines = [l.strip() for l in _join_continuations(log_text.splitlines()) if l.strip()]
    hits = [l for l in lines if any(e in l.lower() for e in _SRC_EXT)]
    out = [f"로그 {len(lines)}줄 중 소스 파일(.c 등)이 나오는 줄 {len(hits)}개"]
    if not hits:
        out.append("→ 컴파일 명령이 로그에 찍히지 않은 것 같음: 빌드 도구의 상세 출력(verbose) 옵션을 켜고 다시 빌드해 로그를 받거나, "
                   "--scan(소스 폴더 검색)으로 대신 만드세요.")
        return out
    stop = {"compiling", "compile", "building", "build", "built", "info", "warning", "warn", "error", "note", "finished",
            "invoking", "file", "linking", "done", "make", "ninja", "cd", "echo", "debug", "release", "src", "source"}
    heads: Dict[str, int] = {}
    for l in hits:
        toks = pt.split_command(l, True)
        for t in toks[:6]:
            if t.startswith("-") or t.lower().endswith(_SRC_EXT):
                break
            n = _exe_name(t).strip(":[]()")
            if n and n not in stop and re.fullmatch(r"[\w.+-]{2,40}", n) and not re.fullmatch(r"[\d:.\-]+", n):
                heads[n] = heads.get(n, 0) + 1
    if heads:
        top = sorted(heads.items(), key=lambda x: -x[1])[:5]
        out.append("그 줄들 앞부분에 자주 나오는 단어: " + ", ".join(f"{k}({v})" for k, v in top)
                   + " → 이 중 컴파일러가 있으면 --compiler-regex로 지정(예: --compiler-regex \"" + top[0][0] + "\")")
    out.append("예:")
    out += ["  " + (l if len(l) <= 220 else l[:220] + " …") for l in hits[:limit]]
    return out


def scan_sources(root: str, profile: str = "auto", exclude_dirs: Optional[Set[str]] = None) -> List[dict]:
    """빌드 로그가 없을 때의 대체: root 아래 .c 파일마다, 헤더(.h)가 있는 모든 폴더를 -I로 주는 compile DB 항목.

    실제 빌드 옵션(매크로 정의, 헤더 우선순위)은 알 수 없으므로 근사다. 빌드 산출물 폴더(Debug, Release, out ...)와
    숨김 폴더는 건너뛴다.
    """
    skip = {x.lower() for x in (exclude_dirs or _SKIP_DIRS)}
    srcs, inc = [], []
    for d, dirs, files in os.walk(root):
        dirs[:] = sorted(x for x in dirs if not x.startswith(".") and x.lower() not in skip)
        if any(f.lower().endswith((".h", ".hpp")) for f in files):
            inc.append(d)
        srcs += [os.path.join(d, f) for f in sorted(files) if f.lower().endswith(_SRC_EXT)]
    inc.sort(key=lambda x: (x.count(os.sep), x))
    exe = (PROFILES.get(profile) or PROFILES["gcc"])["exe"][0] if profile != "auto" else "cc"
    args = [exe] + [f"-I{x}" for x in inc]
    return [{"directory": root, "file": f, "arguments": args + ["-c", f], "compiler_profile": profile if profile != "auto"
             else "gcc", "origin": "scan"} for f in srcs]


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
