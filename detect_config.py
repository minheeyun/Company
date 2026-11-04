#!/usr/bin/env python3
"""프로젝트 폴더에서 분석용 빌드 설정(컴파일러·매크로·include·제외 폴더·메모리 클래스 헤더)을 자동으로 찾는다.

손으로 넣던 옵션( -D __ghs__ -D CPU_BOARD_REVA -I ...\\GHS --exclude *SAMPLECODE* --cpp "... -include cia_memclass.h" )을
다음 근거로 결정하고, 결정마다 근거와 확신도를 남긴다.

  1. 빌드 파일      compile_commands.json, *.gpj(GHS), *.ewp(IAR), .cproject(Eclipse·TASKING), Makefile·*.mk·*.mak,
                    CMakeLists.txt, *.bat·*.cmd 에서 컴파일러 실행 파일 이름과 -D / -I 옵션 (프로젝트 폴더와 그 상위 폴더)
  2. 컴파일러       빌드 파일에 근거가 없으면 후보 컴파일러 매크로마다 표본을 실제로 전처리·파싱해 오류가 가장 적은 것,
                    같으면 소스의 컴파일러 고유 구문(#pragma ghs, __no_init …)·링커 파일(*.lsl, *.icf …) 점수로 고른다.
                    어느 매크로로도 전처리 결과가 같으면 '분석에 영향 없음'으로 기록한다.
     Compiler.h     여러 개면 컴파일러 계열 이름(GHS·TASKING·IAR …)이 들어간 폴더
  3. 메모리 클래스   AUTOSAR VAR/FUNC/P2VAR 의 memclass 이름을 빈 매크로로 정의한 헤더 (memclass_shim)
  4. 반복 전처리·파싱  실제로 분석할 파일(--scope)과 표본을 분석과 같은 조건으로 전처리·파싱해 보고
                      - #error 가 나면 그 #if/#elif 사슬에서 고를 매크로를 찾아 -D 로 추가
                        (컴파일러 계열 이름 → 빌드 파일에 있는 이름 → 사슬의 첫 번째 순으로 선택, 나머지는 대안으로 기록)
                      - 헤더를 못 찾으면 프로젝트 안, 그다음 --search 위치에서 찾아 -I 로 추가 (없으면 '헤더 없음')
                      - 파서가 컴파일러 확장 키워드(__interrupt, __far …)에서 멈추면 -D 이름= 으로 지운다
                        (지워도 나아지지 않으면 되돌린다)
                    더 해결할 것이 없을 때까지 반복하고, 남은 실패는 원인별로 묶어 해결 방법과 함께 기록한다.
  5. 제외 폴더      SAMPLECODE·example·demo 처럼 예제 코드 폴더

사용 예)
  python detect_config.py "S:\\...\\26A10" "S:\\...\\26A22"            # 결과: cia_config.json, run_auto.ps1
  python folder_impact.py 변경전 변경후 -o result --auto-config        # 분석 전에 같은 탐지를 자동으로 수행

확신도가 '낮음'인 결정(예: 보드 리비전을 근거 없이 첫 번째로 고른 경우)은 반드시 실제 빌드 설정과 확인하세요.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import locale
import os
import random
import re
import subprocess
import sys
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from pathlib import Path

from pycparser import CParser

import auto_include
import folder_impact as fi
import memclass_shim
from c_impact import parsediag
from c_impact.frontend import BuildConfig, preprocess_command

HERE = Path(__file__).resolve().parent

# 컴파일러 계열: (실행 파일 이름 정규식, 정의할 매크로, Compiler.h 폴더 이름 단서)
FAMILIES = {
    "GHS": (r"\b(ccarm|ccppc|ccv850e?|ccrh850|cxarm|ccintarm|ccintppc)\b|green\s*hills|\.gpj\b", ["__ghs__"], ["ghs", "greenhills"]),
    "TASKING": (r"\b(ctc|cctc|carm)\b|tasking", ["__TASKING__"], ["tasking"]),
    "IAR": (r"\b(iccarm|iccrh850|iccrl78|iccrx)\b|iar\s*systems|\.ewp\b", ["__IAR_SYSTEMS_ICC__", "__ICCARM__"], ["iar"]),
    "ARMCC": (r"\b(armcc|armclang)\b", ["__CC_ARM", "__ARMCC_VERSION=5060000"], ["armcc", "keil", "arm_cc"]),
    "RENESAS": (r"\b(ccrl|ccrx|ccrh)\b", ["__CCRH__"], ["renesas", "ccrh", "ccrl"]),
    "WINDRIVER": (r"\b(dcc|dplus)\b|diab", ["__DCC__"], ["diab", "windriver"]),
    "GCC": (r"\b(arm-none-eabi-gcc|tricore-gcc|powerpc-eabi-gcc|gcc)\b", [], ["gcc", "gnu"]),
}
BUILD_GLOBS = ("*.gpj", "*.ewp", ".cproject", "*.mk", "*.mak", "Makefile*", "makefile*", "CMakeLists.txt", "*.cmake",
               "*.bat", "*.cmd", "*.opt", "*.bld", "*.ld_opts", "*.launch", "compile_commands.json")
SAMPLE_DIR = re.compile(r"(?i)(samplecode|sample_code|samples?|examples?|demo|testapp)$")
DEF_RE = re.compile(r"""(?:^|[\s"'=,;(])-D\s*([A-Za-z_]\w*(?:=[^\s"',;)]*)?)""")
INC_RE = re.compile(r"""(?:^|[\s"'=,;(])-I\s*("([^"]+)"|([^\s"',;)]+))""")
XML_DEF_RE = re.compile(r"<(?:state|listOptionValue[^>]*value)=?\"?([A-Za-z_]\w*(?:=[^<\"]*)?)\"?", re.I)
DEFINED_RE = re.compile(r"defined\s*\(?\s*([A-Za-z_]\w*)")
COND_RE = re.compile(r"^\s*#\s*(if|ifdef|ifndef|elif|else|endif)\b(.*)$")


def log(msg: str):
    print(msg, file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# 1. 빌드 파일
# ---------------------------------------------------------------------------


LINKER_EXT = {".lsl": "TASKING", ".icf": "IAR", ".sct": "ARMCC", ".dld": "WINDRIVER", ".gpj": "GHS", ".ewp": "IAR",
              ".eww": "IAR", ".dla": "GHS", ".dnm": "GHS", ".dbo": "GHS"}
BUILD_DIR_RE = re.compile(r"(?i)(build|make|project|prj|ide|tool|script|config|cfg|ghs|tasking|iar|compile)")


def _parent_roots(old_dir: Path, new_dir: Path) -> list[Path]:
    """프로젝트 폴더의 상위 폴더 (빌드 파일이 소스 폴더 밖에 있는 경우)."""
    out = []
    for d in (new_dir, old_dir):
        p = d.parent
        if p != d and p not in out and len(p.parts) > 1:
            out.append(p)
    return out


def _iter_build_candidates(root: Path, shallow: bool):
    if not shallow:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if not d.startswith(".") or d == ".settings"]
            for fn in filenames:
                yield Path(dirpath) / fn
        return
    # 상위 폴더: 바로 아래 파일 + 빌드 폴더처럼 보이는 하위 폴더 2단계까지
    try:
        entries = list(os.scandir(root))
    except OSError:
        return
    for e in entries:
        if e.is_file():
            yield Path(e.path)
        elif e.is_dir() and BUILD_DIR_RE.search(e.name) and not e.name.startswith("."):
            base = len(Path(e.path).parts)
            for dirpath, dirnames, filenames in os.walk(e.path):
                if len(Path(dirpath).parts) - base >= 2:
                    dirnames[:] = []
                for fn in filenames:
                    yield Path(dirpath) / fn


def scan_build_files(roots: list[Path], max_files: int = 400, shallow: list[Path] | None = None) -> dict:
    found, defines, incs, compilers = [], Counter(), Counter(), Counter()
    linker: Counter = Counter()
    cc_json = None
    seen: set[str] = set()
    for root, sh in [(r, False) for r in roots] + [(r, True) for r in (shallow or [])]:
        n = 0
        for p in _iter_build_candidates(root, sh):
            key = os.path.normcase(str(p))
            if key in seen:
                continue
            seen.add(key)
            fn = p.name
            fam = LINKER_EXT.get(p.suffix.lower())
            if fam:
                linker[fam] += 1
            if not any(p.match(g) for g in BUILD_GLOBS):
                continue
            try:
                if p.stat().st_size > 4_000_000:
                    continue
                text = p.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            n += 1
            try:
                rel = p.relative_to(root).as_posix()
            except ValueError:
                rel = str(p)
            found.append(rel if not sh else f"(상위) {p}")
            if fn == "compile_commands.json" and cc_json is None:
                cc_json = str(p)
            for m in DEF_RE.finditer(text):
                defines[m.group(1)] += 1
            if p.suffix.lower() in (".ewp", ".cproject") or fn == ".cproject":
                for blk in re.findall(r"(?is)<option[^>]*>(?:(?!</option>).)*?(?:CCDefines|defined|symbols|\.def)(?:(?!</option>).)*?</option>", text):
                    for v in XML_DEF_RE.findall(blk):
                        if not v.lower().startswith(("ccdefines", "true", "false")):
                            defines[v] += 1
            for m in INC_RE.finditer(text):
                incs[(m.group(2) or m.group(3)).strip()] += 1
            low = text.lower()
            for fam, (rx, _, _) in FAMILIES.items():
                k = len(re.findall(rx, low))
                if k and fam != "GCC":
                    compilers[fam] += k
                elif k and fam == "GCC" and re.search(r"gcc\b", low):
                    compilers[fam] += k
            if n >= max_files:
                break
    return {"files": found, "defines": defines, "includes": incs, "compilers": compilers, "compile_commands": cc_json,
            "linker_files": linker}


# 소스 안의 컴파일러 고유 구문과 가중치 (#if/#define 줄은 여러 컴파일러용 분기일 수 있어 세지 않는다)
# 한 컴파일러만 쓰는 구문(#pragma ghs 등)은 3, 여러 컴파일러가 함께 쓰는 키워드(__far 등)는 1
SIGNATURES = {
    "GHS": [(r"#\s*pragma\s+ghs\b", 3), (r"\b__ghs_\w+", 3), (r"\b(?:v800|arm|ppc|rh850)_ghs\.h\b", 3), (r"\bghs_\w+\.h\b", 2)],
    "TASKING": [(r"#\s*pragma\s+tasking\b", 3), (r"\b__(?:mfcr|mtcr|dsync|isync|svlcx|rslcx|bisr|syscall)\s*\(", 2),
                (r"#\s*pragma\s+section\s+(?:all|code|farbss|fardata|nearbss)\b", 2), (r"\b__(?:far|near|a0|a1|a8|a9)\b", 1)],
    "IAR": [(r"\b__no_init\b", 3), (r"\b__ramfunc\b", 3), (r"#\s*pragma\s+(?:location|vector|diag_suppress|required)\b", 2),
            (r"[<\"]intrinsics\.h[>\"]", 2), (r"\b__root\b", 1)],
    "ARMCC": [(r"\b__svc\s*\(", 2), (r"#\s*pragma\s+arm\s+section\b", 3), (r"\b__irq\b", 1), (r"\b__packed\b", 1)],
    "RENESAS": [(r"#\s*pragma\s+inline_asm\b", 3), (r"\b__evenaccess\b", 3), (r"#\s*pragma\s+stack_protector\b", 2)],
    "WINDRIVER": [(r"\b__diab\b", 2), (r"#\s*pragma\s+section\s+[A-Z_]+\s+\"[^\"]*\"\s+\"[^\"]*\"", 2)],
}
_SIG_RX = {f: [(re.compile(rx), w) for rx, w in v] for f, v in SIGNATURES.items()}
_SKIP_LINE = re.compile(r"^\s*#\s*(if|elif|ifdef|ifndef|define|undef|error|else|endif)\b")


def scan_signatures(files: dict[str, Path], linker: Counter | None = None, max_files: int = 4000) -> Counter:
    """컴파일러 고유 구문·링커 파일·폴더 이름에서 컴파일러 계열 단서를 센다 (.c 는 3배 가중)."""
    sig: Counter = Counter()
    for f, k in (linker or {}).items():
        sig[f] += 3 * k  # 링커·프로젝트 파일은 강한 단서
    dirs = set()
    for i, (rel, p) in enumerate(files.items()):
        dirs.update(x.lower() for x in Path(rel).parts[:-1])
        if i >= max_files:
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if not any(t in text for t in ("pragma", "__", "intrinsics")):
            continue
        w = 3 if rel.endswith(".c") else 1
        for line in text.splitlines():
            if _SKIP_LINE.match(line):
                continue
            for fam, rxs in _SIG_RX.items():
                for rx, pw in rxs:
                    k = len(rx.findall(line))
                    if k:
                        sig[fam] += w * pw * k
    for fam, (_, _, hints) in FAMILIES.items():
        if fam in SIGNATURES and any(d in hints or any(d == h or d.startswith(h + "_") or d.endswith("_" + h) for h in hints)
                                     for d in dirs):
            sig[fam] += 1
    return Counter({f: k for f, k in sig.items() if k > 0})


# ---------------------------------------------------------------------------
# 2. 컴파일러와 Compiler.h
# ---------------------------------------------------------------------------


def find_compiler_h(files: dict[str, Path]) -> list[str]:
    return sorted(rel for rel in files if Path(rel).name.lower() == "compiler.h")


def choose_family(build: dict, compiler_hs: list[str]) -> tuple[str | None, str, str]:
    """(계열, 근거, 확신도)."""
    if build["compilers"]:
        fam, k = build["compilers"].most_common(1)[0]
        return fam, f"빌드 파일에서 {fam} 컴파일러 이름 {k}회", "높음"
    fams = [fam for fam, (_, _, hints) in FAMILIES.items()
            if any(any(h in Path(rel).parent.as_posix().lower() for h in hints) for rel in compiler_hs)]
    if len(fams) == 1:
        return fams[0], f"Compiler.h 폴더 이름 ({fams[0]})", "중간"
    if fams:
        return fams[0], (f"빌드 파일이 없고 Compiler.h 가 여러 컴파일러용({', '.join(fams)})으로 있어 첫 번째를 선택 — "
                         "실제 컴파일러 확인 필요"), "낮음"
    return None, "빌드 파일·Compiler.h 폴더에서 컴파일러를 알 수 없음", "낮음"


def choose_compiler_h(compiler_hs: list[str], family: str | None) -> tuple[str | None, str]:
    if not compiler_hs:
        return None, "Compiler.h 없음"
    if len(compiler_hs) == 1:
        return Path(compiler_hs[0]).parent.as_posix(), "프로젝트에 하나뿐"
    hints = FAMILIES.get(family, (None, None, []))[2] if family else []
    for rel in compiler_hs:
        if any(h in rel.lower() for h in hints):
            return Path(rel).parent.as_posix(), f"{len(compiler_hs)}개 중 {family} 폴더"
    plain = [r for r in compiler_hs if not re.search(r"(?i)(template|sample|example|test)", r)]
    pick = (plain or compiler_hs)[0]
    return Path(pick).parent.as_posix(), f"{len(compiler_hs)}개 중 첫 번째 (계열 단서 없음)"


# ---------------------------------------------------------------------------
# 3. #error 사슬 분석
# ---------------------------------------------------------------------------


def error_chain(path: Path, line: int) -> list[str]:
    """#error 가 들어 있는 #if/#elif 사슬의 조건식 목록 (바깥 블록 제외)."""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    stack: list[list[str]] = []
    for i, text in enumerate(lines[: max(0, line - 1)], 1):
        m = COND_RE.match(text)
        if not m:
            continue
        kind, rest = m.group(1), m.group(2).strip()
        if kind in ("if", "ifdef", "ifndef"):
            stack.append([f"#{kind} {rest}"])
        elif kind in ("elif", "else") and stack:
            stack[-1].append(f"#{kind} {rest}".strip())
        elif kind == "endif" and stack:
            stack.pop()
    return stack[-1] if stack else []


def chain_candidates(chain: list[str]) -> list[str]:
    out = []
    for c in chain:
        if c.startswith("#ifndef") or c.startswith("#else"):
            continue
        if c.startswith("#ifdef"):
            names = [c.split()[1]] if len(c.split()) > 1 else []
        else:
            names = DEFINED_RE.findall(c)
            if not names:
                m = re.match(r"#(?:el)?if\s*\(?\s*([A-Za-z_]\w*)\s*(==\s*(\w+))?", c)
                if m:
                    names = [m.group(1) + (f"={m.group(3)}" if m.group(3) else "")]
        for n in names:
            if n not in out:
                out.append(n)
    return out


# ---------------------------------------------------------------------------
# 4. 시험 전처리·파싱
# ---------------------------------------------------------------------------


def _check(job) -> dict:
    """파일 하나를 실제 분석과 같게 전처리·파싱해 본다 (프로세스 풀 작업)."""
    root, rel, cfg, want_hash = job
    root = Path(root)
    try:
        r = subprocess.run(preprocess_command(root, rel, cfg), capture_output=True, cwd=str(root))
    except OSError as e:
        return {"rel": rel, "kind": "pp", "err": f"전처리기 실행 실패: {e}", "line": "", "hash": None}
    if r.returncode != 0:
        err = r.stderr.decode("utf-8", errors="replace")
        h = hashlib.sha1(r.stderr).hexdigest() if want_hash else None
        return {"rel": rel, "kind": "pp", "err": err, "line": "", "hash": h,
                "n_err": max(1, len(re.findall(r"\berror:", err)))}
    text = r.stdout.decode(locale.getpreferredencoding(False) or "utf-8", errors="replace")
    h = hashlib.sha1(r.stdout).hexdigest() if want_hash else None
    try:
        CParser().parse(text, str(root / rel))
    except Exception as e:  # pycparser.ParseError 등
        c = parsediag.classify(f"파싱 실패: {e}", text)
        return {"rel": rel, "kind": "parse", "err": f"파싱 실패: {e}", "class": c, "hash": h, "n_err": 1}
    return {"rel": rel, "kind": "", "err": "", "hash": h}


def _run_checks(jobs_list: list, n: int) -> list[dict]:
    if not jobs_list:
        return []
    if n > 1 and len(jobs_list) > 1:
        try:
            with ProcessPoolExecutor(max_workers=min(n, len(jobs_list))) as ex:
                return list(ex.map(_check, jobs_list, chunksize=max(1, len(jobs_list) // (n * 4))))
        except (OSError, BrokenProcessPool, PermissionError):
            pass
    return [_check(j) for j in jobs_list]


def _make_cfg(cpp, defines, includes, auto_inc) -> BuildConfig:
    cfg = BuildConfig(cpp=list(cpp))
    cfg.include_dirs = [i if Path(i).is_absolute() else "{ROOT}/" + i for i in includes] + \
        ["{ROOT}/" + d if d != "." else "{ROOT}" for d in auto_inc]
    cfg.defines = list(defines)
    return cfg


def trial_compilers(targets, cpp, includes, auto_inc, cands: list[str | None], jobs: int) -> dict:
    """후보 컴파일러 매크로마다 표본을 전처리·파싱해 실패 수와 전처리 결과가 달라지는지 본다."""
    out = {}
    for fam in cands:
        macros = FAMILIES[fam][1] if fam else []
        cfg = _make_cfg(cpp, macros, includes, auto_inc)
        res = _run_checks([(str(root), rel, cfg, True) for _, root, rel in targets], jobs)
        out[fam] = {"failed": sum(x.get("n_err", 0) for x in res if x["kind"]), "hashes": tuple(x["hash"] for x in res)}
    return out


def detect(old_dir: Path, new_dir: Path, exclude: list[str] | None = None, *, sample: int = 60, rounds: int = 8,
           jobs: int = 8, out_dir: Path | None = None, quiet: bool = False, scope: str = "changed",
           search: list[str] | None = None, max_scope: int = 300) -> dict:
    say = (lambda m: None) if quiet else log
    exclude = list(exclude or [])
    decisions: list[dict] = []
    jobs = max(1, jobs or 1)

    def decide(item, value, why, conf, alts=None):
        decisions.append({"item": item, "value": value, "evidence": why, "confidence": conf, "alternatives": alts or []})

    say("[1/6] 빌드 파일 찾는 중 (프로젝트 폴더와 그 상위 폴더)...")
    build = scan_build_files([new_dir, old_dir], shallow=_parent_roots(old_dir, new_dir))
    say(f"      빌드 파일 {len(build['files'])}개")

    # 예제 폴더 제외
    sample_dirs = set()
    for root in (new_dir, old_dir):
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            for d in dirnames:
                if SAMPLE_DIR.search(d):
                    sample_dirs.add(d)
    for d in sorted(sample_dirs):
        pat = f"*{d}*"
        if pat not in exclude:
            exclude.append(pat)
            decide("제외 폴더", pat, f"예제·시연 코드 폴더 이름 '{d}'", "중간")

    old_files, new_files = fi.list_c_files(old_dir, exclude), fi.list_c_files(new_dir, exclude)
    chs = find_compiler_h(new_files) or find_compiler_h(old_files)

    say("[2/6] 메모리 클래스 헤더 만드는 중...")
    out_dir = Path(out_dir or HERE)
    out_dir.mkdir(parents=True, exist_ok=True)
    names, guarded = memclass_shim.collect([old_dir, new_dir], exclude)
    use = sorted(n for n in names if n not in guarded and n not in memclass_shim.POSITIONS)
    mem = out_dir / "cia_memclass_auto.h"
    lines = ["/* detect_config.py 가 만든 파일: AUTOSAR 메모리 클래스 이름을 빈 값으로 정의 */",
             "#ifndef CIA_MEMCLASS_SHIM_H", "#define CIA_MEMCLASS_SHIM_H"]
    for n in use:
        lines += [f"#ifndef {n}", f"#define {n}", "#endif"]
    lines += ["#endif", ""]
    mem.write_text("\n".join(lines), encoding="utf-8")
    cpp = [sys.executable, str(HERE / "cpp_filter.py"), "-include", str(mem)]

    # 표본: 실제로 분석할 파일(--scope) 을 먼저, 모자라면 나머지에서 무작위
    changes = fi.compare_trees(old_files, new_files)
    scoped, _ = fi.analysis_scope(changes, old_files, new_files, scope)
    targets: list[tuple[str, Path, str]] = []
    for label, root, files in (("변경 후", new_dir, new_files), ("변경 전", old_dir, old_files)):
        if scoped is not None:
            mine = [f for f in scoped if f in files][:max_scope]
        else:
            ch = set(changes["modified"]) | set(changes["added"]) | set(changes["removed"])
            mine = [f for f in files if f.endswith(".c") and f in ch][:max_scope]
        rest = sorted(f for f in files if f.endswith(".c") and f not in mine)
        random.Random(1).shuffle(rest)
        if label == "변경 전":  # 변경 전은 분석 대상만 (무작위 표본은 변경 후에서)
            rest = []
        mine += rest[: max(0, sample - len(mine))]
        targets += [(label, root, f) for f in mine]
    n_scope = sum(1 for t in targets if scoped is None or t[2] in scoped)
    auto_inc = fi.header_dirs(old_files, new_files)
    defines: list[str] = []
    includes: list[str] = []

    say("[3/6] 컴파일러 판별 중...")
    family, why, conf = choose_family(build, chs)
    sig = scan_signatures(new_files, build["linker_files"])
    alts: list[str] = [f for f in build["compilers"] if f != family]
    if conf == "낮음":
        cands = [f for f, _ in sig.most_common()] + [f for f in FAMILIES if FAMILIES[f][1] and f not in sig]
        if family and family in cands:
            cands.remove(family)
            cands.insert(0, family)
        trial_targets = [t for t in targets if t[0] == "변경 후"][:24]
        say(f"      빌드 파일에 근거가 없어 표본 {len(trial_targets)}개로 후보 {len(cands)}개를 시험합니다...")
        ch_dir0, _ = choose_compiler_h(chs, family)
        tr = trial_compilers(trial_targets, cpp, [ch_dir0] if ch_dir0 else [], auto_inc, [None] + cands, jobs)
        sig_txt = "점수 " + ", ".join(f"{f} {k}" for f, k in sig.most_common(3))
        if len({v["hashes"] for v in tr.values()}) == 1:
            pick = sig.most_common(1)[0][0] if sig else None
            note = (f"표본 {len(trial_targets)}개 파일의 전처리 결과가 컴파일러 매크로({', '.join(cands[:4])}…)와 상관없이 같아 "
                    "분석 결과에 영향이 없음")
            if pick:
                family, why, conf = pick, f"{note} · 소스 단서 {sig_txt}", "참고"
            else:
                family, why, conf = None, note, "참고"
            alts = [f for f in cands if f != family][:4]
        else:
            best = min(v["failed"] for v in tr.values())
            tied = [f for f, v in tr.items() if v["failed"] == best]
            fam_tied = [f for f in tied if f]
            score = lambda f: (-sig.get(f, 0), cands.index(f))  # noqa: E731
            if fam_tied:
                fam_tied.sort(key=score)
                pick = fam_tied[0]
                fails = ", ".join(f"{f or '매크로 없음'} {v['failed']}" for f, v in list(tr.items())[:5])
                if len(fam_tied) == 1:
                    family, conf = pick, "중간"
                    why = f"시험 전처리·파싱 오류 수가 가장 적음 (표본 {len(trial_targets)}개, 오류 수: {fails})"
                elif sig.get(pick, 0) > (sig.get(fam_tied[1], 0) if len(fam_tied) > 1 else 0):
                    family, conf = pick, "중간"
                    why = f"시험 결과가 같은 후보({', '.join(fam_tied)}) 중 소스 단서가 가장 많음 ({sig_txt})"
                else:
                    family, conf = pick, "낮음"
                    why = (f"시험 결과가 같은 후보({', '.join(fam_tied)})를 구분할 근거가 없어 첫 번째를 선택"
                           + (f" (소스 단서: {sig_txt})" if sig else " (컴파일러 고유 구문·링커 파일 없음)") + " — 실제 컴파일러 확인 필요")
                alts = [f for f in fam_tied if f != pick] + [f for f in cands if f not in fam_tied][:3]
            else:
                family, conf = None, "낮음"
                why = "어떤 컴파일러 매크로도 시험 결과를 낫게 하지 않음"
                alts = cands[:4]
    if family:
        fam_macros = FAMILIES[family][1]
        defines += fam_macros
        if not alts:
            alts = [f for f, (_, _, h) in FAMILIES.items() if f != family and any(any(x in r.lower() for x in h) for r in chs)]
        decide("컴파일러", family, why, conf, alts)
        if fam_macros:
            decide("컴파일러 매크로", " ".join(f"-D {m}" for m in fam_macros), f"{family} 컴파일러가 미리 정의하는 매크로", conf)
    else:
        decide("컴파일러", "알 수 없음" if conf == "낮음" else "판별 불필요", why, conf, alts or list(FAMILIES))
    ch_dir, ch_why = choose_compiler_h(chs, family)
    if ch_dir:
        includes.append(ch_dir)
        decide("Compiler.h 경로", ch_dir, ch_why, "높음" if len(chs) == 1 or "폴더" in ch_why else "낮음",
               [Path(r).parent.as_posix() for r in chs if Path(r).parent.as_posix() != ch_dir])
    if build["compile_commands"]:
        decide("compile_commands.json", build["compile_commands"], "프로젝트에 있음 — 파일별 -D/-I 를 그대로 쓰는 것이 가장 정확", "높음")
    decide("메모리 클래스 헤더", str(mem), f"VAR·FUNC·P2VAR 등의 memclass 이름 {len(use)}개 (Compiler*.h 가 검사하는 {len(names) - len(use)}개 제외)", "높음")

    say(f"[4/6] 분석 대상 {n_scope}개 + 표본 {len(targets) - n_scope}개 파일을 전처리·파싱하며 오류 해결 중...")
    header_index = defaultdict(list)
    for rel in list(new_files) + list(old_files):
        if rel.endswith(".h"):
            header_index[Path(rel).name.lower()].append(Path(rel).parent.as_posix())
    ext_index = None
    build_defs = {d.split("=")[0]: d for d in build["defines"]}
    hints = FAMILIES.get(family, (None, None, []))[2] if family else []
    unresolved_err: dict[str, str] = {}
    missing_gen: Counter = Counter()
    keywords: dict[str, list] = {}
    rejected_kw: set[str] = set()
    history = []

    def run(tlist):
        cfg = _make_cfg(cpp, defines, includes, auto_inc)
        res = _run_checks([(str(root), rel, cfg, False) for _, root, rel in tlist], jobs)
        return {(t[0], t[2]): x for t, x in zip(tlist, res)}

    results = run(targets)
    for rnd in range(1, rounds + 1):
        failed = {k: v for k, v in results.items() if v["kind"]}
        history.append({"round": rnd, "files": len(targets), "failed": len(failed)})
        say(f"      {rnd}회차: {len(targets)}개 중 실패 {len(failed)}개")
        if not failed:
            break
        progress, new_kw = False, []
        n_dec = len(decisions)
        seen_err = set()
        for (label, rel), res in failed.items():
            root = new_dir if label == "변경 후" else old_dir
            if res["kind"] == "parse":
                c = res["class"]
                if c["cause"] == "컴파일러 확장 키워드" and c["fix"] and c["detail"] not in rejected_kw:
                    if c["fix"] not in defines:
                        defines.append(c["fix"])
                        new_kw.append(c["fix"])
                        keywords[c["fix"]] = [f"{c['where']}: {_short(c.get('line_text', ''))}"]
                        progress = True
                continue
            err = res["err"]
            for m in parsediag.ERROR_RE.finditer(err):
                fpath, line, msg = m.group(1), int(m.group(2)), m.group(3).strip().strip('"')
                key = (Path(fpath).name, line)
                if key in seen_err:
                    continue
                seen_err.add(key)
                p = Path(fpath)
                if not p.is_absolute():
                    p = root / fpath
                cands = chain_candidates(error_chain(p, line))
                if not cands:
                    unresolved_err[msg] = f"{Path(fpath).name}:{line}"
                    continue
                pick, reason, c2 = None, "", "낮음"
                for cnd in cands:
                    if any(h in cnd.lower() for h in hints):
                        pick, reason, c2 = cnd, f"{family} 컴파일러 계열 이름", "높음"
                        break
                if pick is None:
                    for cnd in cands:
                        if cnd.split("=")[0] in build_defs:
                            pick, reason, c2 = build_defs[cnd.split("=")[0]], "빌드 파일의 -D 옵션", "높음"
                            break
                if pick is None:
                    pick, reason = cands[0], "근거 없이 #if 사슬의 첫 번째 선택 — 실제 빌드 설정 확인 필요"
                if pick.split("=")[0] in {d.split("=")[0] for d in defines}:
                    unresolved_err[msg] = f"{Path(fpath).name}:{line} (이미 -D {pick} 인데도 #error)"
                    continue
                defines.append(pick)
                decide("매크로", f"-D {pick}", f"#error \"{msg}\" ({Path(fpath).name}:{line}) 를 피하려면 필요 · {reason}", c2,
                       [c for c in cands if c != pick])
                progress = True
            for m in parsediag.MISSING_RE.finditer(err):
                hdr = m.group(1).strip().strip('"<>')
                dirs = header_index.get(Path(hdr).name.lower(), [])
                if dirs:
                    d = sorted(dirs, key=lambda x: (bool(re.search(r"(?i)(sample|template|test)", x)), len(x)))[0]
                    if d not in includes:
                        includes.append(d)
                        decide("include 경로", d, f"'{hdr}' 를 찾기 위해", "중간", [x for x in dirs if x != d][:5])
                        progress = True
                    continue
                if search:
                    if ext_index is None:
                        say(f"      프로젝트 밖 헤더 색인 중: {', '.join(search)}")
                        ext_index = auto_include.HeaderIndex([Path(s) for s in search], [old_dir, new_dir])
                    cands = ext_index.candidates(hdr)
                    if cands:
                        d = auto_include.choose(hdr, cands, [i for i in includes if Path(i).is_absolute()], ext_index,
                                                set(), [], auto_include.DEFAULT_AVOID)
                        if d not in includes:
                            includes.append(d)
                            decide("include 경로 (프로젝트 밖)", d, f"'{hdr}' 를 --search 위치에서 찾음", "중간",
                                   [x for x in cands if x != d][:5])
                            progress = True
                        continue
                missing_gen[hdr] += 1
        if not progress:
            break
        other_progress = len(decisions) > n_dec
        before = len(failed)
        retry = [t for t in targets if (t[0], t[2]) in failed]
        again = run(retry)
        after = sum(1 for v in again.values() if v["kind"])
        if new_kw and not other_progress and after >= before:
            # 키워드를 지워도 나아지지 않으면 되돌린다
            for kw in new_kw:
                defines.remove(kw)
                keywords.pop(kw, None)
                rejected_kw.add(kw.split("(")[0].rstrip("="))
            again = run(retry)
        results.update(again)
    # 새 -D 가 원래 되던 파일을 깨지 않았는지 마지막에 전체를 다시 본다
    if len(history) > 1 or keywords:
        results = run(targets)
        history.append({"round": "최종", "files": len(targets), "failed": sum(1 for v in results.values() if v["kind"])})
    if keywords:
        decide("컴파일러 확장 키워드", " ".join(f"-D {k}" for k in keywords),
               "C 표준에 없는 키워드라 파서가 멈춤 → 빈 값으로 정의 (예: " + "; ".join(v[0] for v in list(keywords.values())[:3]) + ")",
               "중간")
    for hdr, k in missing_gen.most_common():
        decide("헤더 없음", hdr, f"표본 {k}개 파일이 include 하지만 프로젝트에 없음 — 빌드 때 생성되는 파일이거나 프로젝트 밖"
               "(MCAL·BSW·컴파일러 폴더)에 있는 파일. --search \"폴더\" 로 찾을 위치를 주세요", "참고")
    for msg, where in unresolved_err.items():
        decide("해결 못한 #error", msg, where, "참고")

    # 남은 실패의 원인
    remaining = _remaining(results, new_files, old_files)
    for g in remaining:
        if g["cause"] in ("헤더 없음", "#error"):
            continue  # 위에서 기록
        ex = f" — 예: {g['where']} `{_short(g.get('line_text', ''))}`" if g.get("where") else ""
        extra = f" · 정의 위치: {', '.join(g['defined_in'])}" if g.get("defined_in") else ""
        decide(f"해결 못한 파싱 실패 ({g['cause']})", g["detail"], f"{g['count']}개 파일{ex}{extra} · {g['advice']}", "참고")
    say("[5/6] 정리")
    final_fail = sum(1 for v in results.values() if v["kind"])
    say("[6/6] 완료")
    return {"compiler": family, "defines": defines, "include_dirs": includes, "exclude": exclude, "memclass_header": str(mem),
            "cpp": cpp, "compile_commands": build["compile_commands"], "decisions": decisions, "history": history,
            "sample_files": len(targets), "scope_files": n_scope, "sample_failed": final_fail, "remaining": remaining,
            "signatures": dict(sig), "build_files": build["files"][:50]}


def _short(s: str, n: int = 80) -> str:
    s = " ".join((s or "").split())
    return s if len(s) <= n else s[: n - 1] + "…"


def _remaining(results: dict, new_files: dict, old_files: dict) -> list[dict]:
    groups: dict[tuple, dict] = {}
    for (label, rel), v in results.items():
        if not v["kind"]:
            continue
        c = v.get("class") or parsediag.classify(v["err"])
        key = (c["cause"], c["detail"])
        g = groups.setdefault(key, {**c, "count": 0, "files": [], "advice": parsediag.ADVICE.get(c["cause"], "")})
        g["count"] += 1
        g["files"].append(f"({label}) {rel}")
    out = sorted(groups.values(), key=lambda g: -g["count"])
    for g in out:
        if g["cause"] == "정의되지 않은 타입":
            g["defined_in"] = parsediag.find_typedef(g["detail"], {**old_files, **new_files})
    return out


# ---------------------------------------------------------------------------
# 출력
# ---------------------------------------------------------------------------


def render(r: dict) -> str:
    lines = ["", "자동 탐지 결과", "=" * 60]
    for d in r["decisions"]:
        alt = f"  (대안: {', '.join(d['alternatives'][:4])})" if d["alternatives"] else ""
        lines.append(f"[{d['confidence']}] {d['item']}: {d['value']}")
        lines.append(f"        근거: {d['evidence']}{alt}")
    lines.append("")
    lines.append(f"시험한 파일 {r['sample_files']}개 (분석 대상 {r.get('scope_files', 0)}개 포함) 중 남은 실패 {r['sample_failed']}개")
    for g in r.get("remaining") or []:
        lines.append(f"  - {g['cause']} '{g['detail']}': {g['count']}개 파일 (예: {g['files'][0]})")
    low = [d for d in r["decisions"] if d["confidence"] == "낮음"]
    if low:
        lines.append(f"⚠ 확신도 '낮음' {len(low)}건은 실제 빌드 설정과 확인하세요.")
    return "\n".join(lines)


def ps1(r: dict, old_dir: Path, new_dir: Path, out: str) -> str:
    q = lambda s: '"' + str(s) + '"'  # noqa: E731
    parts = [f"python {q(HERE / 'folder_impact.py')} {q(old_dir)} {q(new_dir)} `", f"  -o {out} --scope changed `"]
    for e in r["exclude"]:
        parts.append(f"  --exclude {q(e)} `")
    if r["defines"]:
        parts.append("  " + " ".join(f"-D {q(d)}" for d in r["defines"]) + " `")
    for i in r["include_dirs"]:
        parts.append(f"  -I {q(i)} `")
    parts.append(f"  --cpp {q('python ' + str(HERE / 'cpp_filter.py') + ' -include ' + r['memclass_header'])}")
    return "\n".join(parts) + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="분석용 빌드 설정 자동 탐지")
    ap.add_argument("old_dir")
    ap.add_argument("new_dir")
    ap.add_argument("--exclude", action="append", default=[], metavar="GLOB")
    ap.add_argument("--sample", type=int, default=60, help="전처리해 볼 표본 .c 파일 수 (기본 60)")
    ap.add_argument("--rounds", type=int, default=8)
    ap.add_argument("--scope", choices=["changed", "all"], default="changed",
                    help="시험할 파일: changed = 실제 분석할 변경 파일과 그 헤더를 쓰는 파일 (기본), all = 무작위 표본")
    ap.add_argument("--search", action="append", default=[], metavar="DIR",
                    help="프로젝트 밖에서 헤더를 찾을 위치 (MCAL·BSW·컴파일러 include 폴더, 여러 번 가능)")
    ap.add_argument("-j", "--jobs", type=int, default=os.cpu_count() or 4)
    ap.add_argument("-o", "--out", default="cia_config.json", help="결과 JSON (기본 cia_config.json)")
    ap.add_argument("--ps1", default="run_auto.ps1", help="만들 실행 스크립트 (기본 run_auto.ps1)")
    ap.add_argument("--result", default="result_auto", help="실행 스크립트의 결과 폴더 (기본 result_auto)")
    args = ap.parse_args(argv)
    old_dir, new_dir = Path(args.old_dir).resolve(), Path(args.new_dir).resolve()
    for d in (old_dir, new_dir):
        if not d.is_dir():
            log(f"오류: 폴더가 아닙니다: {d}")
            return 1
    r = detect(old_dir, new_dir, args.exclude, sample=args.sample, rounds=args.rounds, jobs=args.jobs,
               out_dir=Path(args.out).resolve().parent, scope=args.scope, search=args.search)
    print(render(r))
    Path(args.out).write_text(json.dumps(r, ensure_ascii=False, indent=2), encoding="utf-8")
    Path(args.ps1).write_text(ps1(r, old_dir, new_dir, args.result), encoding="utf-8")
    print(f"\n설정: {Path(args.out).resolve()}")
    print(f"실행 스크립트: {Path(args.ps1).resolve()}  (powershell -ExecutionPolicy Bypass -File {args.ps1})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
