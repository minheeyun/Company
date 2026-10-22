#!/usr/bin/env python3
"""프로젝트 밖에 있는 헤더(MCAL, 컴파일러, 공용 include 등)의 폴더를 자동으로 찾아 include 경로로 등록한다.

헤더 폴더가 많을 때 -I 를 하나씩 지정하지 않도록 다음을 반복한다.
  1. 변경 전/후 폴더의 *.c 를 실제 조건(-D, 프로젝트 내부 헤더 폴더)으로 전처리해 본다.
  2. "fatal error: Xxx.h: No such file or directory" 에서 빠진 헤더 이름을 모은다.
  3. --search 로 지정한 위치(여러 개 가능)에서 그 헤더가 있는 폴더를 찾는다.
     `#include "Mcal/Port.h"` 처럼 하위 경로가 있으면 그 경로가 맞는 폴더만 후보로 본다.
     후보가 여럿이면: 이미 고른 폴더 > test/stub/sample 등이 아닌 폴더 > --prefer 일치
     > 빠진 다른 헤더도 함께 있는 폴더 > 얕은 경로 순으로 고르고, 다른 후보는 보고서에 남긴다.
  4. 찾은 폴더를 넣고 아직 실패한 파일만 다시 전처리한다 (헤더 안에서 또 다른 헤더를 찾는 경우까지 반복).

찾은 폴더는 명령줄 대신 gcc 의 CPATH 환경 변수로 넘긴다(-I 와 같은 효과, 프로젝트 안 헤더보다 나중에 검색).
Windows 명령줄 길이 제한(32,767자)에 걸리지 않고, --explore 의 SIL 빌드에도 그대로 적용된다.
끝나면 folder_impact.py 를 같은 조건으로 실행한다 (--no-run 이면 탐색만).

사용 예)
  python auto_include.py "S:\\...\\26A10" "S:\\...\\26A22" --search "S:\\Tdrive\\MCAL" --search "C:\\TASKING\\include" -o result
  python auto_include.py before after --search D:\\ext --prefer "*TC3xx*" -D __TASKING__ -o result --explore

결과 (-o 폴더 아래):
  include_search/auto_include_report.md   추가한 폴더, 여러 후보 중 고른 헤더, 찾지 못한 헤더, 헤더 외 전처리 오류
  include_search/include_dirs.txt         찾은 include 폴더 목록
  include_search/run_folder_impact.ps1    같은 조건으로 다시 실행하는 PowerShell 스크립트 (CPATH 설정 포함)
  summary.md 등                           folder_impact.py 결과
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import subprocess
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import folder_impact as fi
from c_impact.frontend import BuildConfig, preprocess_command

# 소스 파일 자체가 없을 때의 "cc1: fatal error" 는 줄:열이 없으므로 제외된다
MISSING_RE = re.compile(r":\d+:\d+: fatal error: (.+?): No such file or directory")
DEFAULT_AVOID = ["*test*", "*stub*", "*sample*", "*example*", "*backup*", "*bak*", "*template*"]


def log(msg: str):
    print(msg, file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# 헤더 색인
# ---------------------------------------------------------------------------


class HeaderIndex:
    """검색 위치 아래 모든 파일: 소문자 파일 이름 → 경로, 폴더 → 파일 이름(소문자)."""

    def __init__(self, roots: list[Path], skip: list[Path]):
        self.by_name: dict[str, list[Path]] = defaultdict(list)
        self.by_dir: dict[str, set[str]] = defaultdict(set)
        self.files = 0
        skip_norm = [os.path.normcase(str(s.resolve())) for s in skip]
        t0 = time.time()
        for root in roots:
            for dirpath, dirnames, filenames in os.walk(root):
                nd = os.path.normcase(os.path.abspath(dirpath))
                if any(nd == s or nd.startswith(s + os.sep) for s in skip_norm):
                    dirnames[:] = []
                    continue
                dirnames[:] = [d for d in dirnames if not d.startswith(".")]
                for name in filenames:
                    low = name.lower()
                    self.by_name[low].append(Path(dirpath) / name)
                    self.by_dir[nd].add(low)
                    self.files += 1
                    if self.files % 50000 == 0:
                        log(f"  색인 {self.files}개 파일 ({time.time() - t0:.0f}초)...")

    def candidates(self, include_name: str) -> list[str]:
        """include 이름("a/b.h" 가능)을 만족하는 -I 폴더 후보."""
        parts = [p for p in include_name.replace("\\", "/").split("/") if p not in ("", ".")]
        if ".." in parts:
            parts = parts[len(parts) - parts[::-1].index("..") :]
        if not parts:
            return []
        out = []
        for full in self.by_name.get(parts[-1].lower(), []):
            fp = full.parts
            if len(fp) <= len(parts):
                continue
            if [x.lower() for x in fp[-len(parts):]] != [x.lower() for x in parts]:
                continue
            out.append(str(Path(*fp[: -len(parts)])))
        return sorted(set(out))


def choose(name: str, cands: list[str], selected: list[str], index: HeaderIndex, missing: set[str],
           prefer: list[str], avoid: list[str]) -> str:
    sel = {os.path.normcase(s) for s in selected}

    def match(c, pats):
        low = c.replace("\\", "/").lower()
        return any(fnmatch.fnmatch(low, p.lower()) or fnmatch.fnmatch(low, "*" + p.lower().strip("*") + "*")
                   for p in pats)

    sel_parts = [[x.lower() for x in Path(s).parts] for s in selected]

    def affinity(c):
        # 이미 고른 폴더와 공유하는 상위 경로 길이 (같은 MCAL 패키지·파생 칩을 계속 쓰도록)
        cp = [x.lower() for x in Path(c).parts]
        best = 0
        for sp in sel_parts:
            n = 0
            for a, b in zip(cp, sp):
                if a != b:
                    break
                n += 1
            best = max(best, n)
        return best

    def score(c):
        here = index.by_dir.get(os.path.normcase(os.path.abspath(c)), set())
        together = sum(1 for m in missing if m.replace("\\", "/").split("/")[-1].lower() in here)
        return (os.path.normcase(c) not in sel, match(c, avoid), not match(c, prefer) if prefer else False,
                -affinity(c), -together, len(Path(c).parts), c)

    return min(cands, key=score)


# ---------------------------------------------------------------------------
# 전처리 확인
# ---------------------------------------------------------------------------


def check_files(items: list[tuple[str, Path, str]], cfg: BuildConfig, env: dict, jobs: int) -> dict:
    """(버전, 루트, 상대 경로) → ("ok"|"missing"|"other", 헤더 이름 또는 메시지)."""

    def one(item):
        ver, root, rel = item
        try:
            r = subprocess.run(preprocess_command(root, rel, cfg), stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                               text=True, errors="replace", env=env, cwd=str(root))
        except OSError as e:
            return item, ("other", f"gcc 실행 실패: {e}")
        if r.returncode == 0:
            return item, ("ok", "")
        m = MISSING_RE.search(r.stderr)
        if m:
            return item, ("missing", m.group(1).strip())
        lines = [l for l in r.stderr.splitlines() if "error" in l] or r.stderr.splitlines()[:1]
        return item, ("other", lines[0] if lines else f"종료 코드 {r.returncode}")

    with ThreadPoolExecutor(max_workers=max(1, jobs)) as ex:
        return dict(ex.map(one, items))


def _strip_path(msg: str) -> str:
    """오류 메시지에서 파일 위치를 지워 같은 원인끼리 묶는다."""
    return re.sub(r"^.*?:\d+:\d+:\s*", "", msg).strip()


# ---------------------------------------------------------------------------
# 실행
# ---------------------------------------------------------------------------


def search(args, passthrough: list[str]) -> tuple[dict, list[str]]:
    old_dir, new_dir = Path(args.old_dir).resolve(), Path(args.new_dir).resolve()
    roots = [Path(s).resolve() for s in args.search]
    for p in [old_dir, new_dir] + roots:
        if not p.is_dir():
            raise SystemExit(f"오류: 폴더가 아닙니다: {p}")
    exclude = list(args.exclude or [])
    old_files, new_files = fi.list_c_files(old_dir, exclude), fi.list_c_files(new_dir, exclude)
    items = [("old", old_dir, r) for r in old_files if r.endswith(".c")] + \
            [("new", new_dir, r) for r in new_files if r.endswith(".c")]
    if not items:
        raise SystemExit("오류: 두 폴더에 *.c 파일이 없습니다.")

    cfg = BuildConfig(cpp=args.cpp.split(), use_fake_libc=not args.system_headers)
    auto = [] if args.no_auto_include else fi.header_dirs(old_files, new_files)
    cfg.include_dirs = [d if Path(d).is_absolute() else "{ROOT}/" + d for d in args.include_dirs] + \
                       ["{ROOT}/" + d if d != "." else "{ROOT}" for d in auto]
    cfg.defines = list(args.defines) + [f"{k}=" for k in args.strip]
    cfg.undefines = list(args.undefines)

    log(f"1/3 검색 위치 색인 중: {', '.join(map(str, roots))}")
    index = HeaderIndex(roots, skip=[old_dir, new_dir] + [Path(s) for s in args.skip])
    log(f"  파일 {index.files}개")

    selected: list[str] = []
    found: dict[str, dict] = {}          # 헤더 → {"dir", "alternatives", "files"}
    unresolved: dict[str, list[str]] = defaultdict(list)
    base_cpath = os.environ.get("CPATH", "")
    pending = items
    status: dict = {}
    rounds = 0
    log(f"2/3 전처리로 빠진 헤더 찾기 (파일 {len(items)}개)...")
    while pending and rounds < args.max_rounds:
        rounds += 1
        env = dict(os.environ)
        env["CPATH"] = os.pathsep.join([x for x in [base_cpath] + selected if x])
        res = check_files(pending, cfg, env, args.jobs)
        status.update(res)
        missing: dict[str, list] = defaultdict(list)
        for item, (kind, val) in res.items():
            if kind == "missing":
                missing[val].append(item)
        n_ok = sum(1 for k, _ in res.values() if k == "ok")
        log(f"  {rounds}회차: 확인 {len(pending)}개 → 성공 {n_ok}, 빠진 헤더 {len(missing)}종")
        retry = []
        added = 0
        names = set(missing)
        # 후보가 하나뿐인 헤더부터 정해야, 여러 후보 중 고를 때 이미 고른 폴더와 가까운 쪽을 택할 수 있다
        cand_map = {n: index.candidates(n) for n in missing if n not in found}
        for name, files in sorted(missing.items(), key=lambda x: (len(cand_map.get(x[0], [])) or 10**6, -len(x[1]), x[0])):
            rels = [f"({v}) {r}" for v, _, r in files]
            if name in found:  # 폴더를 넣었는데도 같은 헤더가 없다고 나옴
                unresolved[name] += rels
                found[name]["note"] = "폴더를 추가했지만 여전히 찾지 못함 (include 이름·대소문자 확인)"
                continue
            cands = cand_map[name]
            if not cands:
                unresolved[name] += rels
                continue
            pick = choose(name, cands, selected, index, names, args.prefer, args.avoid)
            found[name] = {"dir": pick, "alternatives": [c for c in cands if c != pick], "files": len(files)}
            if pick not in selected:
                selected.append(pick)
                added += 1
            retry += files
        if not added and not retry:
            break
        pending = retry

    # 재조정: 여러 후보 중 고른 헤더를, 다 찾은 뒤의 폴더 구성을 기준으로 다시 고른다
    # (gcc 는 파일마다 첫 번째로 빠진 헤더에서 멈추므로 처음 고를 때는 나머지 폴더를 모른다)
    rebalanced = []
    for name, info in found.items():
        if not info["alternatives"]:
            continue
        cur = info["dir"]
        users = [h for h, i in found.items() if i["dir"] == cur and h != name]
        others = [d for d in selected if d != cur]
        best = choose(name, [cur] + info["alternatives"], others, index, set(), args.prefer, args.avoid)
        if best == cur:
            continue
        if users:
            selected.insert(selected.index(cur), best)  # 같은 이름 헤더가 먼저 검색되도록 앞에 넣는다
        else:
            selected[selected.index(cur)] = best
        info["alternatives"] = sorted([c for c in [cur] + info["alternatives"] if c != best])
        info["dir"] = best
        rebalanced.append((name, cur, best))
    if rebalanced:
        log(f"  재조정: {len(rebalanced)}개 헤더의 폴더를 다른 후보로 바꿔 전체를 다시 확인합니다")
        env = dict(os.environ)
        env["CPATH"] = os.pathsep.join([x for x in [base_cpath] + selected if x])
        status = check_files(items, cfg, env, args.jobs)

    kinds = defaultdict(int)
    for kind, _ in status.values():
        kinds[kind] += 1
    other = defaultdict(list)
    for (v, _, r), (kind, msg) in status.items():
        if kind == "other":
            other[_strip_path(msg)].append(f"({v}) {r}")
    result = {
        "old_dir": str(old_dir), "new_dir": str(new_dir),
        "search": [str(r) for r in roots], "indexed_files": index.files, "rounds": rounds,
        "files": len(items), "ok": kinds["ok"], "missing": kinds["missing"], "other": kinds["other"],
        "include_dirs": selected,
        "rebalanced": [{"header": h, "from": f, "to": t} for h, f, t in rebalanced],
        "found": found,
        "unresolved": {k: sorted(set(v)) for k, v in unresolved.items()},
        "other_errors": {k: sorted(v) for k, v in other.items()},
        "defines": cfg.defines, "user_include": args.include_dirs,
    }
    return result, passthrough


def _ps_quote(s: str) -> str:
    return "'" + str(s).replace("'", "''") + "'"


def write_outputs(r: dict, out: Path, fi_args: list[str]) -> Path:
    out.mkdir(parents=True, exist_ok=True)
    (out / "include_dirs.txt").write_text("\n".join(r["include_dirs"]) + "\n", encoding="utf-8")
    (out / "auto_include.json").write_text(json.dumps(r, ensure_ascii=False, indent=2), encoding="utf-8")
    script = Path(__file__).resolve().parent / "folder_impact.py"
    ps = ["# auto_include.py 가 만든 실행 스크립트: 찾은 include 폴더를 CPATH 로 넘겨 folder_impact.py 를 실행한다",
          "$env:CPATH = " + _ps_quote(os.pathsep.join(r["include_dirs"])),
          "& " + " ".join(_ps_quote(x) for x in [sys.executable, str(script)] + fi_args)]
    (out / "run_folder_impact.ps1").write_text("\r\n".join(ps) + "\r\n", encoding="utf-8-sig")
    cpath_len = len(os.pathsep.join(r["include_dirs"]))

    lines = ["# include 경로 자동 탐색 결과", "",
             f"- 검색 위치: {', '.join('`' + s + '`' for s in r['search'])} (파일 {r['indexed_files']}개 색인, 반복 {r['rounds']}회)",
             f"- 전처리 결과 (*.c {r['files']}개, 변경 전+후): **성공 {r['ok']}** · 헤더를 못 찾음 {r['missing']} · 그 밖의 오류 {r['other']}",
             f"- 추가한 include 폴더 **{len(r['include_dirs'])}개** → CPATH 로 전달 (길이 {cpath_len}자)", ""]
    if cpath_len > 30000:
        lines += ["⚠ CPATH 가 Windows 환경 변수 한도(32,767자)에 가깝습니다. `--search` 범위를 좁히거나 공통 상위 폴더를 `-I` 로 직접 지정하세요.", ""]
    lines += ["## 1. 추가한 include 폴더 (검색 순서)", "", "| # | 폴더 | 이 폴더에서 찾은 헤더 |", "|---|---|---|"]
    by_dir = defaultdict(list)
    for h, info in r["found"].items():
        by_dir[info["dir"]].append(h)
    for i, d in enumerate(r["include_dirs"], 1):
        hs = sorted(by_dir.get(d, []))
        lines.append(f"| {i} | `{d}` | {', '.join('`' + h + '`' for h in hs[:8])}{' 외 ' + str(len(hs) - 8) + '개' if len(hs) > 8 else ''} |")
    amb = {h: i for h, i in r["found"].items() if i["alternatives"]}
    lines += ["", "## 2. 후보가 여러 개라 하나를 고른 헤더 (확인 필요)", ""]
    if amb:
        lines += ["다른 폴더가 맞다면 `--prefer \"*폴더이름*\"` 이나 `--avoid`, `--skip` 으로 조정하세요.", "",
                  "| 헤더 | 고른 폴더 | 다른 후보 |", "|---|---|---|"]
        for h, i in sorted(amb.items()):
            alts = i["alternatives"]
            lines.append(f"| `{h}` | `{i['dir']}` | {'<br>'.join('`' + a + '`' for a in alts[:5])}"
                         f"{'<br>외 ' + str(len(alts) - 5) + '개' if len(alts) > 5 else ''} |")
    else:
        lines.append("없음")
    lines += ["", "## 3. 찾지 못한 헤더", ""]
    if r["unresolved"]:
        lines += ["검색 위치에 없는 헤더입니다. 그 헤더가 있는 위치를 `--search` 로 추가하거나, 빌드 중에 생성되는 헤더라면 "
                  "생성된 폴더를 지정하세요.", "", "| 헤더 | 필요한 파일 수 | 예시 파일 | 비고 |", "|---|---|---|---|"]
        for h, files in sorted(r["unresolved"].items(), key=lambda x: -len(x[1])):
            note = r["found"].get(h, {}).get("note", "")
            lines.append(f"| `{h}` | {len(files)} | {files[0]} | {note} |")
    else:
        lines.append("없음")
    lines += ["", "## 4. 헤더 외 전처리 오류", ""]
    if r["other_errors"]:
        lines += ["`#error` 는 보통 컴파일러 판별 매크로가 없어서 납니다 (예: `-D __TASKING__`). 전용 키워드는 `--strip` 이나 "
                  "`-D \"__interrupt(x)=\"` 로 지웁니다.", "", "| 오류 | 파일 수 | 예시 파일 |", "|---|---|---|"]
        for msg, files in sorted(r["other_errors"].items(), key=lambda x: -len(x[1]))[:30]:
            cell = msg.replace("|", "\\|")[:150]
            lines.append(f"| {cell} | {len(files)} | {files[0]} |")
    else:
        lines.append("없음")
    lines += ["", "## 5. 다시 실행", "",
              "같은 PowerShell 창에서:", "", "```powershell", f"powershell -ExecutionPolicy Bypass -File {_ps_quote(out / 'run_folder_impact.ps1')}", "```",
              "", "또는 직접:", "", "```powershell",
              f"$env:CPATH = Get-Content {_ps_quote(out / 'include_dirs.txt')} | Where-Object {{ $_ }} | Join-String -Separator ';'",
              "python folder_impact.py <변경 전> <변경 후> -o result", "```",
              "(Windows PowerShell 5.1 에는 Join-String 이 없으므로 `run_folder_impact.ps1` 사용을 권합니다.)", ""]
    (out / "auto_include_report.md").write_text("\n".join(lines), encoding="utf-8")
    return out / "auto_include_report.md"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="프로젝트 밖 헤더 폴더를 자동으로 찾아 include 경로로 등록한 뒤 folder_impact.py 를 실행한다",
        epilog="여기에 없는 옵션(--explore, --scope, --entry 등)은 그대로 folder_impact.py 로 전달된다.")
    ap.add_argument("old_dir", help="변경 전 프로젝트 폴더")
    ap.add_argument("new_dir", help="변경 후 프로젝트 폴더")
    ap.add_argument("--search", action="append", required=True, metavar="DIR",
                    help="헤더를 찾을 위치 (MCAL·컴파일러·공용 include 상위 폴더), 여러 번 지정 가능")
    ap.add_argument("--skip", action="append", default=[], metavar="DIR", help="검색에서 뺄 폴더 (예: 다른 버전 프로젝트 폴더)")
    ap.add_argument("--prefer", action="append", default=[], metavar="PATTERN",
                    help="후보가 여러 개일 때 우선할 폴더 패턴 (예: '*TC3xx*')")
    ap.add_argument("--avoid", action="append", default=list(DEFAULT_AVOID), metavar="PATTERN",
                    help="후보가 여러 개일 때 피할 폴더 패턴 (기본: test, stub, sample, example, backup ...)")
    ap.add_argument("--max-rounds", type=int, default=12, help="반복 횟수 한도 (기본 12)")
    ap.add_argument("--no-run", action="store_true", help="include 폴더만 찾고 folder_impact.py 는 실행하지 않음")
    ap.add_argument("-o", "--out", default="impact_out/folder", help="결과 폴더 (folder_impact.py 의 -o 와 같음)")
    ap.add_argument("--exclude", action="append", metavar="GLOB", help="제외할 파일·폴더 패턴")
    ap.add_argument("-I", dest="include_dirs", action="append", default=[], metavar="DIR", help="직접 지정할 include 경로")
    ap.add_argument("--no-auto-include", action="store_true", help="프로젝트 안 헤더 폴더를 자동 등록하지 않음")
    ap.add_argument("-D", dest="defines", action="append", default=[], metavar="NAME[=VAL]", help="매크로 정의")
    ap.add_argument("-U", dest="undefines", action="append", default=[], metavar="NAME", help="매크로 해제")
    ap.add_argument("--strip", action="append", default=[], metavar="KEYWORD", help="지울 컴파일러 확장 키워드")
    ap.add_argument("--cpp", default="gcc -E", help="전처리 명령 (기본 'gcc -E')")
    ap.add_argument("--system-headers", action="store_true", help="내장 최소 표준 헤더 대신 시스템 헤더 사용")
    ap.add_argument("-j", "--jobs", type=int, default=os.cpu_count() or 1, help="동시에 실행할 전처리 수")
    args, rest = ap.parse_known_args(argv)

    t0 = time.time()
    r, passthrough = search(args, rest)
    fi_args = [args.old_dir, args.new_dir, "-o", args.out, "-j", str(args.jobs), "--cpp", args.cpp]
    for d in args.include_dirs:
        fi_args += ["-I", d]
    for d in args.defines:
        fi_args += ["-D", d]
    for d in args.undefines:
        fi_args += ["-U", d]
    for d in args.strip:
        fi_args += ["--strip", d]
    for d in args.exclude or []:
        fi_args += ["--exclude", d]
    if args.no_auto_include:
        fi_args.append("--no-auto-include")
    if args.system_headers:
        fi_args.append("--system-headers")
    fi_args += passthrough
    report_path = write_outputs(r, Path(args.out) / "include_search", fi_args)

    print(f"include 폴더 {len(r['include_dirs'])}개 추가 · 전처리 성공 {r['ok']}/{r['files']} "
          f"(헤더 못 찾음 {r['missing']}, 그 밖의 오류 {r['other']}) · {time.time() - t0:.0f}초")
    for h, files in sorted(r["unresolved"].items(), key=lambda x: -len(x[1]))[:10]:
        print(f"  찾지 못한 헤더: {h} ({len(files)}개 파일)")
    for msg, files in sorted(r["other_errors"].items(), key=lambda x: -len(x[1]))[:5]:
        print(f"  그 밖의 오류: {msg[:120]} ({len(files)}개 파일)")
    print(f"보고서: {report_path}")
    if args.no_run:
        print(f"실행 스크립트: {Path(args.out) / 'include_search' / 'run_folder_impact.ps1'}")
        return 0
    print("3/3 folder_impact.py 실행...", flush=True)
    os.environ["CPATH"] = os.pathsep.join([x for x in [os.environ.get("CPATH", "")] + r["include_dirs"] if x])
    return fi.main(fi_args)


if __name__ == "__main__":
    sys.exit(main())
