#!/usr/bin/env python3
"""파일 하나가 왜 파싱에 실패하는지 실제 분석과 같은 조건으로 확인한다 (원인 추적용).

folder_impact.py 와 같은 include 경로(프로젝트 안 헤더 폴더 자동 등록 + -I 우선)와 -D 로 gcc 를 돌려서
  1. 실제로 include 된 Compiler.h / Compiler_Cfg.h / Platform_Types.h / Std_Types.h 의 경로 (gcc -H)
  2. AUTOSAR 컴파일러 추상화 매크로(P2VAR, VAR, FUNC, CONST, P2CONST, P2FUNC ...)가 정의됐는지 (gcc -dM)
  3. 정의되지 않았다면: 그 매크로를 정의하는 헤더가 어디 있는지, include 됐는지, include 됐는데도 없다면
     #define 을 막고 있는 #if 조건 (앞 15줄)
  4. 파서 오류와 그 원본 줄
을 출력한다.

사용 예)
  python why_parse.py "S:\\...\\26A22" Yura_Application/FunctionalLayer/Diag/LSS_Diag.c -D __ghs__ -D CPU_BOARD_REVA -I "Static_Code\\...\\GHS"
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

import folder_impact as fi
from c_impact.frontend import BuildConfig, preprocess_command

WATCH = ("compiler.h", "compiler_cfg.h", "platform_types.h", "std_types.h", "memmap.h")
MACROS = ("P2VAR", "P2CONST", "CONSTP2VAR", "CONSTP2CONST", "P2FUNC", "CONSTP2FUNC", "FUNC", "VAR", "CONST",
          "FUNC_P2VAR", "FUNC_P2CONST", "AUTOMATIC", "TYPEDEF", "INLINE", "LOCAL_INLINE")
DEFINE_RE = re.compile(rb"^\s*#\s*define\s+(" + b"|".join(m.encode() for m in MACROS) + rb")\b", re.M)


def run(cmd, cwd):
    return subprocess.run(cmd, capture_output=True, cwd=cwd)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="파일 하나의 파싱 실패 원인 추적")
    ap.add_argument("project", help="프로젝트 폴더 (예: 변경 후 폴더)")
    ap.add_argument("file", help="프로젝트 기준 .c 파일 경로")
    ap.add_argument("-I", dest="include_dirs", action="append", default=[])
    ap.add_argument("-D", dest="defines", action="append", default=[])
    ap.add_argument("-U", dest="undefines", action="append", default=[])
    ap.add_argument("--strip", action="append", default=[])
    ap.add_argument("--exclude", action="append", default=[])
    ap.add_argument("--no-auto-include", action="store_true")
    ap.add_argument("--gcc", default="gcc")
    args = ap.parse_args(argv)

    root = Path(args.project).resolve()
    rel = args.file.replace("\\", "/")
    if not (root / rel).is_file():
        print(f"파일이 없습니다: {root / rel}")
        return 1
    files = fi.list_c_files(root, args.exclude)
    cfg = BuildConfig(cpp=[args.gcc, "-E"])
    auto = [] if args.no_auto_include else fi.header_dirs(files)
    cfg.include_dirs = [d if Path(d).is_absolute() else "{ROOT}/" + d for d in args.include_dirs] + \
                       ["{ROOT}/" + d if d != "." else "{ROOT}" for d in auto]
    cfg.defines = list(args.defines) + [f"{k}=" for k in args.strip]
    cfg.undefines = list(args.undefines)
    base = preprocess_command(root, rel, cfg)
    print(f"[대상] {root / rel}")
    print(f"[include 경로] 직접 지정 {len(args.include_dirs)}개가 맨 앞, 자동 {len(auto)}개")
    for d in args.include_dirs:
        p = Path(d) if Path(d).is_absolute() else root / d
        print(f"    -I {p}  {'(있음)' if p.is_dir() else '(⚠ 폴더 없음)'}")

    # 1. 실제로 include 된 헤더
    r = run(base[:-1] + ["-H", base[-1]], str(root))
    err = r.stderr.decode("utf-8", errors="replace")
    used: dict[str, list[str]] = {}
    order: list[str] = []
    for line in err.splitlines():
        m = re.match(r"^(\.+) (.+)$", line)
        if not m:
            continue
        path = m.group(2).strip()
        order.append(path)
        name = os.path.basename(path.replace("\\", "/")).lower()
        if name in WATCH:
            used.setdefault(name, []).append(f"{'  ' * (len(m.group(1)) - 1)}{path}")
    print("\n[1] 실제로 include 된 주요 헤더 (gcc -H)")
    for name in WATCH:
        for p in used.get(name, []):
            print(f"    {name}: {p}")
        if name not in used:
            print(f"    {name}: (include 안 됨)")
    if r.returncode != 0:
        msg = [l for l in err.splitlines() if "error" in l][:3]
        print("    ⚠ 전처리 오류: " + " / ".join(msg))

    # 2. 매크로 정의 여부
    r = run(base[:-1] + ["-dM", base[-1]], str(root))
    defs = {}
    for line in r.stdout.decode("utf-8", errors="replace").splitlines():
        m = re.match(r"#define (\w+)(\(.*?\))?\s*(.*)$", line)
        if m and m.group(1) in MACROS:
            defs[m.group(1)] = f"{m.group(1)}{m.group(2) or ''} {m.group(3)}".strip()
    print("\n[2] AUTOSAR 컴파일러 추상화 매크로 (gcc -dM)")
    missing = []
    for mac in ("P2VAR", "P2CONST", "P2FUNC", "CONSTP2FUNC", "FUNC", "VAR", "CONST", "AUTOMATIC"):
        if mac in defs:
            print(f"    정의됨  {defs[mac][:110]}")
        else:
            print(f"    ⚠ 없음  {mac}")
            missing.append(mac)

    # 3. 없는 매크로를 정의하는 헤더 찾기
    if missing:
        print(f"\n[3] {', '.join(missing)} 를 정의하는 헤더 (프로젝트 안)")
        norm_used = {os.path.normcase(os.path.abspath(p if os.path.isabs(p) else root / p)) for p in order}
        hits = []
        for hrel, hp in files.items():
            if not hrel.lower().endswith(".h"):
                continue
            try:
                data = hp.read_bytes()
            except OSError:
                continue
            found = {m.decode() for m in DEFINE_RE.findall(data)}
            if found & set(missing):
                hits.append((hrel, hp, sorted(found & set(missing)), data))
        if not hits:
            print("    프로젝트 안에서 찾지 못했습니다. 프로젝트 밖(MCAL·컴파일러 폴더)에 있으면 auto_include.py --search 로 찾으세요.")
        for hrel, hp, macs, data in hits[:8]:
            inc = os.path.normcase(os.path.abspath(hp)) in norm_used
            print(f"    {hrel}  정의: {', '.join(macs)}  → {'include 됨' if inc else 'include 안 됨'}")
            if inc:
                lines = data.decode("utf-8", errors="replace").splitlines()
                idx = next((i for i, l in enumerate(lines) if re.match(r"\s*#\s*define\s+" + macs[0] + r"\b", l)), None)
                if idx is not None:
                    print("      ↳ include 됐는데도 정의가 없음 → 아래 #if 조건이 막고 있을 가능성:")
                    for j in range(max(0, idx - 15), idx + 1):
                        if re.match(r"\s*#\s*(if|ifdef|ifndef|elif|else|endif)", lines[j]) or j == idx:
                            print(f"        {j + 1}: {lines[j].strip()[:120]}")
        if len(hits) > 8:
            print(f"    … 외 {len(hits) - 8}개")
        print("    → 'include 안 됨' 이면 그 헤더의 폴더를 -I 로 맨 앞에 지정하거나, include 된 다른 이름이 같은 헤더(위 [1])를 확인하세요.")

    # 4. 파서 오류
    r = run(base, str(root))
    print("\n[4] 파서 결과")
    if r.returncode != 0:
        print("    전처리 실패 (위 [1] 의 오류 참고)")
        return 0
    try:
        import cpp_filter
        text = cpp_filter.clean("".join(cpp_filter._decode_line(l) for l in r.stdout.splitlines(keepends=True)))
        from pycparser import c_parser
        c_parser.CParser().parse(text, str(root / rel))
        print("    파싱 성공")
    except Exception as e:  # pycparser.ParseError 등
        msg = str(e)
        print(f"    파싱 실패: {msg[:200]}")
        m = re.match(r"(.+?):(\d+):(\d+): ", msg)
        if m:
            path = m.group(1).replace("\\\\", "\\")
            try:
                src = Path(path).read_text(encoding="utf-8", errors="replace").splitlines()[int(m.group(2)) - 1]
                print(f"    원본 줄: {src.strip()[:160]}")
            except (OSError, IndexError):
                pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
