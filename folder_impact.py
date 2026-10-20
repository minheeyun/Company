#!/usr/bin/env python3
"""변경 전 프로젝트 폴더와 변경 후 프로젝트 폴더를 받아 *.c 변경 영향 분석을 한 번에 수행한다.

git 이 없어도 되는 폴더 비교 전용 실행기다. 내부 분석은 c_impact 패키지(change_impact.py 와 같은 엔진)를 쓴다.

  1. 두 폴더의 *.c / *.h 를 파일 단위로 비교 (추가·삭제·수정, 줄바꿈 CRLF/LF 차이는 무시)
  2. 헤더가 있는 폴더를 모두 include 경로로 자동 등록 (-I 직접 지정, compile_commands.json 도 가능)
  3. 분석 범위: 전체 *.c (기본) 또는 변경된 .c + 변경된 헤더를 (간접) include 하는 .c 만 (--scope changed)
  4. 변경 영향 분석 (전처리 후 AST 비교 → 의존성 → 전방 슬라이싱 → 영향 함수·변수·경로·조건)
  5. 함수별 읽기·쓰기·호출 관계가 변경 전후로 어떻게 달라졌는지 비교
  6. (선택) --explore: 결과가 달라지는 입력·상태 자동 탐색, --scenario: 지정한 SIL 시나리오 실행

사용 예)
  python3 folder_impact.py before/ after/
  python3 folder_impact.py before/ after/ -o result --explore --max-cases 300
  python3 folder_impact.py before/ after/ --scope changed -D VARIANT_A=1 --exclude 'test/*'
  python3 folder_impact.py before/ after/ --compile-commands after/build/compile_commands.json

결과 (-o, 기본 impact_out/folder):
  summary.md / summary.json     폴더 비교 요약 (파일 변경, 영향 요약, 의존성 변화, 실행 확인, 분석 범위)
  impact/impact_report.md       변경 영향 분석 보고서 (변경 위치, 영향 함수·변수 표, 경로, 진입점, 테스트 후보, 제외 구성)
  impact/impact.json, impact.dot, sil_scenario.json
  deps/deps_new.md, deps_old.md, deps_access_new.csv   함수·변수 읽기/쓰기 표
  explore/explore_report.md     (--explore 시) 차이를 내는 최소 입력
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import re
import sys
import time
from collections import defaultdict
from pathlib import Path

from c_impact import deps as deps_mod
from c_impact import report
from c_impact.analysis import load_programs, run_analysis
from c_impact.frontend import BuildConfig, load_compile_commands
from c_impact.sil import SilError, load_scenario, run_sil

SOURCE_EXT = (".c",)
HEADER_EXT = (".h",)
_INCLUDE_RE = re.compile(rb'^\s*#\s*include\s*[<"]([^>"]+)[>"]', re.M)


def log(msg: str):
    print(msg, file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# 폴더 비교
# ---------------------------------------------------------------------------


def _excluded(rel: str, exclude: list[str]) -> bool:
    return any(fnmatch.fnmatch(rel, pat) or fnmatch.fnmatch(Path(rel).name, pat) for pat in exclude)


def list_c_files(root: Path, exclude: list[str]) -> dict[str, Path]:
    """root 아래 *.c / *.h (숨김 폴더 제외) → 상대 경로: 절대 경로."""
    out = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        for name in sorted(filenames):
            if not name.endswith(SOURCE_EXT + HEADER_EXT):
                continue
            p = Path(dirpath) / name
            rel = p.relative_to(root).as_posix()
            if not _excluded(rel, exclude):
                out[rel] = p
    return out


def _digest(p: Path) -> str:
    data = p.read_bytes().replace(b"\r\n", b"\n")
    return hashlib.sha1(data).hexdigest()


def compare_trees(old_files: dict[str, Path], new_files: dict[str, Path]) -> dict[str, list[str]]:
    added = sorted(set(new_files) - set(old_files))
    removed = sorted(set(old_files) - set(new_files))
    modified, unchanged = [], []
    for rel in sorted(set(old_files) & set(new_files)):
        (modified if _digest(old_files[rel]) != _digest(new_files[rel]) else unchanged).append(rel)
    return {"added": added, "removed": removed, "modified": modified, "unchanged": unchanged}


def header_dirs(*trees: dict[str, Path]) -> list[str]:
    """헤더가 있는 폴더(상대 경로). 루트(.)를 맨 앞에 둔다."""
    dirs = set()
    for files in trees:
        for rel in files:
            if rel.endswith(HEADER_EXT):
                dirs.add(Path(rel).parent.as_posix())
    return ["."] + sorted(d for d in dirs if d != ".")


def duplicate_headers(*trees: dict[str, Path]) -> dict[str, list[str]]:
    by_name = defaultdict(set)
    for files in trees:
        for rel in files:
            if rel.endswith(HEADER_EXT):
                by_name[Path(rel).name].add(rel)
    return {k: sorted(v) for k, v in by_name.items() if len(v) > 1}


def includers_of(files: dict[str, Path], headers: set[str]) -> set[str]:
    """headers 를 (간접적으로) include 하는 파일. include 이름은 경로 끝부분으로 맞춘다."""
    hdrs = [rel for rel in files if rel.endswith(HEADER_EXT)]
    rev: dict[str, set[str]] = defaultdict(set)
    for rel, p in files.items():
        try:
            data = p.read_bytes()
        except OSError:
            continue
        for inc in _INCLUDE_RE.findall(data):
            name = inc.decode(errors="replace").replace("\\", "/").lstrip("./")
            for h in hdrs:
                if h == name or h.endswith("/" + name):
                    rev[h].add(rel)
    seen, stack = set(), [h for h in headers]
    while stack:
        h = stack.pop()
        for user in rev.get(h, ()):
            if user not in seen:
                seen.add(user)
                stack.append(user)
    return seen


def analysis_scope(changes: dict, old_files: dict, new_files: dict, scope: str) -> tuple[list[str] | None, list[str]]:
    """분석할 .c 목록(None = 전체)과 그 근거."""
    if scope == "all":
        return None, ["전체 *.c"]
    changed = set(changes["added"]) | set(changes["removed"]) | set(changes["modified"])
    c_files = {r for r in changed if r.endswith(SOURCE_EXT)}
    hdrs = {r for r in changed if r.endswith(HEADER_EXT)}
    via = set()
    if hdrs:
        via = {r for r in includers_of(old_files, hdrs) | includers_of(new_files, hdrs) if r.endswith(SOURCE_EXT)}
    files = sorted(c_files | via)
    why = [f"변경된 .c {len(c_files)}개"]
    if hdrs:
        why.append(f"변경된 헤더 {len(hdrs)}개를 include 하는 .c {len(via - c_files)}개")
    return files, why


def _glob_escape(rel: str) -> str:
    return re.sub(r"([\[\]*?])", r"[\1]", rel)


# ---------------------------------------------------------------------------
# 의존성 변화
# ---------------------------------------------------------------------------


def deps_diff(t_old: dict, t_new: dict) -> dict:
    fo = {f["func"]: f for f in t_old["functions"]}
    fn = {f["func"]: f for f in t_new["functions"]}
    rows = []
    for name in sorted(set(fo) | set(fn)):
        a, b = fo.get(name), fn.get(name)
        if a is None or b is None:
            f = b or a
            rows.append({"func": name, "status": "추가" if a is None else "삭제", "file": f["file"],
                         "reads": sorted(f["reads"]), "writes": sorted(f["writes"]),
                         "calls": sorted(f["calls"] + f["extern_calls"])})
            continue
        row = {"func": name, "status": "변경", "file": b["file"]}
        changed = False
        for key, sel in (("reads", lambda f: set(f["reads"])), ("writes", lambda f: set(f["writes"])),
                         ("calls", lambda f: set(f["calls"]) | set(f["extern_calls"]))):
            plus, minus = sorted(sel(b) - sel(a)), sorted(sel(a) - sel(b))
            row[key + "_added"], row[key + "_removed"] = plus, minus
            changed |= bool(plus or minus)
        if changed:
            rows.append(row)
    vo = {v["var"] for v in t_old["variables"]}
    vn = {v["var"] for v in t_new["variables"]}
    return {"functions": rows, "vars_added": sorted(vn - vo), "vars_removed": sorted(vo - vn)}


# ---------------------------------------------------------------------------
# 요약
# ---------------------------------------------------------------------------


def _c(s) -> str:
    return str(s).replace("|", "\\|").replace("\n", " ")


def _list(xs, limit=8) -> str:
    xs = list(xs)
    if not xs:
        return "-"
    s = ", ".join(f"`{_c(x)}`" for x in xs[:limit])
    return s + (f" 외 {len(xs) - limit}개" if len(xs) > limit else "")


def _collapse_changes(changes: list[dict]) -> list[dict]:
    """같은 줄·함수·종류의 호출 세부 항목(인자·호출·반환값 수신)을 하나로 합친다 (요약 표시용)."""
    groups: dict[tuple, list[dict]] = {}
    for c in changes:
        groups.setdefault((c["file"], c["line"], c["func"], c["kind"]), []).append(c)
    out = []
    for items in groups.values():
        pick = next((c for c in items if (c["new"] or c["old"]).endswith(" 호출")), items[0])
        if pick is not items[0] or len(items) > 1:
            pick = dict(pick)
            for k in ("old", "new"):
                if pick[k].endswith(" 호출"):
                    pick[k] = pick[k][: -len(" 호출")]
        out.append(pick)
    return out


def render_summary(s: dict) -> str:
    ch = s["file_changes"]
    out = ["# 폴더 비교 변경 영향 분석", "",
           f"- 변경 전: `{s['old_dir']}`",
           f"- 변경 후: `{s['new_dir']}`",
           f"- 실행: {s['time']} · 소요 {s['elapsed']:.1f}초",
           f"- 분석 범위: {', '.join(s['scope_reason'])} → 변경 전 {s['analyzed']['old']}개 / 변경 후 {s['analyzed']['new']}개 파일",
           f"- include 경로: {'자동 ' + str(len(s['auto_include'])) + '개' if s['auto_include'] else '자동 등록 안 함'}"
           + (f" + 지정 {len(s['user_include'])}개" if s["user_include"] else "")
           + (" · compile_commands.json 사용" if s["compile_commands"] else ""),
           ""]
    out += ["## 1. 파일 변경 (*.c, *.h)", "",
            f"추가 {len(ch['added'])} · 삭제 {len(ch['removed'])} · 수정 {len(ch['modified'])} · 동일 {len(ch['unchanged'])}", ""]
    if ch["added"] or ch["removed"] or ch["modified"]:
        out += ["| 상태 | 파일 |", "|---|---|"]
        for st, key in (("수정", "modified"), ("추가", "added"), ("삭제", "removed")):
            for rel in ch[key]:
                out.append(f"| {st} | `{_c(rel)}` |")
    if s["duplicate_headers"]:
        out += ["", "⚠ 같은 이름의 헤더가 여러 폴더에 있습니다. 자동 include 순서(루트 → 폴더 이름순)에 따라 다른 헤더가 쓰일 수 있으니 "
                "필요하면 `-I` 나 `--compile-commands` 로 지정하세요: "
                + "; ".join(f"`{k}`: {', '.join(v)}" for k, v in list(s["duplicate_headers"].items())[:10])]

    r = s.get("analysis")
    out += ["", "## 2. 변경 영향 요약", ""]
    if r is None:
        out.append("C 소스·헤더 변경이 없어 분석하지 않았습니다.")
    else:
        st = r["stats"]
        out.append(f"변경 지점 **{len(r['changes'])}개** · 영향 함수 **{st['affected_functions']}개** · "
                   f"영향 변수·출력 **{len(r['items'])}개** — 상세: [`impact/impact_report.md`](impact/impact_report.md)")
        out.append("")
        changes = _collapse_changes(r["changes"])
        if changes:
            out += ["| 위치 | 함수 | 종류 | 변경 전 → 변경 후 | 비고 |", "|---|---|---|---|---|"]
            for c in changes[:40]:
                body = f"`{_c(c['old'])}` → `{_c(c['new'])}`" if c["old"] and c["new"] else f"`{_c(c['new'] or c['old'])}`"
                out.append(f"| {c['file']}:{c['line']} | `{_c(c['func'])}` | {report.KIND_KO.get(c['kind'], c['kind'])} | "
                           f"{body} | {_c(c['detail'])} |")
            if len(changes) > 40:
                out.append(f"| … | | | 외 {len(changes) - 40}개 (상세 보고서 참고) | |")
            out.append("")
        if r["items"]:
            out += ["| 영향받는 함수 | 변수·출력 | 어떻게 | 차이가 발생하는 조건 | 상태 |", "|---|---|---|---|---|"]
            for it in r["items"][:60]:
                cond = "<br>".join(_c(x) for x in it["conditions"][:4]) or "모든 실행"
                out.append(f"| `{_c(it['func_display'])}` | `{_c(it['name'])}` | {it['how_kind']}: {_c(it['how'])} | {cond} | "
                           f"{_c(it.get('status', report.STATUS_STATIC))} |")
            if len(r["items"]) > 60:
                out.append(f"| … | 외 {len(r['items']) - 60}개 | | | |")

    d = s.get("deps_diff")
    if d is not None:
        out += ["", "## 3. 함수·변수 의존성 변화 (읽기·쓰기·호출)", "",
                "변경 전후 버전 각각의 함수별 읽기·쓰기 변수와 호출 대상을 비교한 결과입니다. "
                "전체 표: [`deps/deps_new.md`](deps/deps_new.md), [`deps/deps_old.md`](deps/deps_old.md)", ""]
        if d["functions"]:
            out += ["| 함수 | 상태 | 읽기 +/− | 쓰기 +/− | 호출 +/− |", "|---|---|---|---|---|"]
            for row in d["functions"]:
                if row["status"] != "변경":
                    out.append(f"| `{_c(row['func'])}` | {row['status']} | {_list(row['reads'])} | {_list(row['writes'])} | "
                               f"{_list(row['calls'])} |")
                    continue

                def pm(k):
                    a, m = row[k + "_added"], row[k + "_removed"]
                    if not a and not m:
                        return "-"
                    return " ".join(x for x in (("+ " + _list(a)) if a else "", ("− " + _list(m)) if m else "") if x)
                out.append(f"| `{_c(row['func'])}` | 변경 | {pm('reads')} | {pm('writes')} | {pm('calls')} |")
        else:
            out.append("함수별 읽기·쓰기·호출 관계는 바뀌지 않았습니다 (값이나 조건만 바뀐 변경).")
        if d["vars_added"] or d["vars_removed"]:
            out += ["", f"- 새 상태 변수: {_list(d['vars_added'], 20)}", f"- 사라진 상태 변수: {_list(d['vars_removed'], 20)}"]

    out += ["", "## 4. 실행 확인", ""]
    ex = s.get("explore")
    if ex:
        out.append(f"- 자동 탐색: 케이스 {ex['cases']}개 중 차이 {ex['diff_cases']}개 — [`explore/explore_report.md`](explore/explore_report.md)")
        for lab, w in sorted(ex["witnesses_input_only"].items()):
            b = ", ".join(f"{k}={json.dumps(v)}" for k, v in w["bindings"].items() if v != ex["defaults"].get(k)) or "기본값"
            out.append(f"  - `{_c(lab)}` (입력만): {_c(b)} → 단계 #{w['step']} `{_c(w['old'])}` → `{_c(w['new'])}`")
    sil = s.get("sil")
    if sil:
        out.append(f"- 지정 시나리오: 케이스 {sil['cases']}개 중 차이 {sil['diff_cases']}개 — [`sil/sil_report.md`](sil/sil_report.md)")
    if not ex and not sil:
        out.append("실행하지 않았습니다. `--explore` 로 차이를 내는 입력을 자동 탐색하거나 `--scenario` 로 시나리오를 실행할 수 있습니다. "
                   "초안 시나리오는 `impact/sil_scenario.json` 입니다.")

    out += ["", "## 5. 분석 범위와 주의", ""]
    for w in s["warnings"]:
        out.append(f"- ⚠ {_c(w)}")
    if r is not None:
        pe = r["excluded"]["parse_errors"]
        if pe:
            out.append(f"- 파싱하지 못해 분석에서 빠진 파일 {len(pe)}개: {_list(pe, 20)} (상세 보고서 7절)")
        if r["excluded"]["extern_functions"]:
            out.append(f"- 본문 없는 외부 함수 {len(r['excluded']['extern_functions'])}개는 보수적으로 가정했습니다 (상세 보고서 7절).")
    out.append("- 결과는 정적 분석상 영향 후보입니다. 실행 확인은 지정·탐색한 입력 범위 안에서만 의미가 있습니다. "
               "동작 차이가 의도된 변경인지 결함인지는 별도로 판단해야 합니다.")
    out += ["", "## 결과 파일", ""] + [f"- `{p}`" for p in s["outputs"]] + [""]
    return "\n".join(out)


def terminal(s: dict) -> str:
    ch = s["file_changes"]
    lines = [f"[파일] 추가 {len(ch['added'])} · 삭제 {len(ch['removed'])} · 수정 {len(ch['modified'])} · 동일 {len(ch['unchanged'])}"]
    for st, key in (("M", "modified"), ("A", "added"), ("D", "removed")):
        for rel in ch[key][:30]:
            lines.append(f"  {st} {rel}")
    if s.get("analysis"):
        lines += ["", report.terminal_summary(s["analysis"])]
    d = s.get("deps_diff")
    if d and d["functions"]:
        lines += ["", "[의존성 변화]"]
        for row in d["functions"][:20]:
            if row["status"] != "변경":
                lines.append(f"  {row['func']}: {row['status']}")
            else:
                parts = []
                for k, ko in (("reads", "읽기"), ("writes", "쓰기"), ("calls", "호출")):
                    if row[k + "_added"]:
                        parts.append(f"{ko}+ {','.join(row[k + '_added'])}")
                    if row[k + "_removed"]:
                        parts.append(f"{ko}- {','.join(row[k + '_removed'])}")
                lines.append(f"  {row['func']}: " + "; ".join(parts))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 실행
# ---------------------------------------------------------------------------


def run(args) -> int:
    t0 = time.time()
    old_dir, new_dir = Path(args.old_dir).resolve(), Path(args.new_dir).resolve()
    for p in (old_dir, new_dir):
        if not p.is_dir():
            log(f"오류: 폴더가 아닙니다: {p}")
            return 1
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    exclude = list(args.exclude or [])
    warnings: list[str] = []

    log("1/5 파일 비교 중...")
    old_files, new_files = list_c_files(old_dir, exclude), list_c_files(new_dir, exclude)
    if not old_files and not new_files:
        log("오류: 두 폴더 모두 *.c / *.h 파일이 없습니다.")
        return 1
    changes = compare_trees(old_files, new_files)
    auto_inc = [] if args.no_auto_include else header_dirs(old_files, new_files)
    summary: dict = {
        "old_dir": str(old_dir), "new_dir": str(new_dir),
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "file_changes": changes,
        "auto_include": auto_inc, "user_include": args.include_dirs,
        "compile_commands": args.compile_commands,
        "duplicate_headers": duplicate_headers(old_files, new_files) if auto_inc else {},
        "warnings": warnings, "outputs": [],
    }
    any_change = changes["added"] or changes["removed"] or changes["modified"]
    files, why = analysis_scope(changes, old_files, new_files, args.scope)
    summary["scope_reason"] = why
    if args.scope == "changed":
        warnings.append("--scope changed: 변경 파일과 그 헤더를 쓰는 파일만 분석했습니다. 다른 .c 파일이 같은 전역 변수를 읽거나 "
                        "변경된 함수를 호출하면 그 영향은 빠집니다.")

    analysis = None
    if not any_change and not args.force:
        log("C 소스·헤더 변경이 없습니다. (--force 로 강제 분석 가능)")
        summary["analyzed"] = {"old": 0, "new": 0}
    elif files is not None and not files:
        log("분석할 .c 파일이 없습니다 (헤더만 바뀌었고 그 헤더를 쓰는 .c 가 없음).")
        summary["analyzed"] = {"old": 0, "new": 0}
    else:
        cfg = BuildConfig(cpp=args.cpp.split(), use_fake_libc=not args.system_headers)
        cfg.include_dirs = ["{ROOT}/" + d if d != "." else "{ROOT}" for d in auto_inc]
        for d in args.include_dirs:
            cfg.include_dirs.insert(0, d if Path(d).is_absolute() else "{ROOT}/" + d)  # 직접 지정한 경로가 우선
        cfg.defines = list(args.defines) + [f"{k}=" for k in args.strip]
        cfg.undefines = list(args.undefines)
        if args.compile_commands:
            cfg.per_file = load_compile_commands(args.compile_commands, args.cc_root or new_dir)
        globs = [_glob_escape(f) for f in files] if files is not None else None

        log("2/5 전처리·파싱·의존성 분석 중...")
        old, new = load_programs(old_dir, new_dir, cfg, globs, exclude or None, args.extern_model, args.jobs)
        summary["analyzed"] = {"old": len(old.tus), "new": len(new.tus)}
        log("3/5 변경 영향 분석(전방 슬라이싱) 중...")
        analysis = run_analysis(old, new, cfg, k=args.context_depth, entries=args.entry, isr=args.isr,
                                sil_cycles=args.cycles)

        log("4/5 의존성 비교 중...")
        t_old = deps_mod.dependency_table(old, args.entry, args.isr)
        t_new = deps_mod.dependency_table(new, args.entry, args.isr)
        summary["deps_diff"] = deps_diff(t_old, t_new)
        dd = out / "deps"
        dd.mkdir(exist_ok=True)
        (dd / "deps_new.md").write_text(deps_mod.render_md(t_new, transitive=True), encoding="utf-8")
        (dd / "deps_old.md").write_text(deps_mod.render_md(t_old, transitive=True), encoding="utf-8")
        (dd / "deps_new.json").write_text(json.dumps(t_new, ensure_ascii=False, indent=2), encoding="utf-8")
        (dd / "deps_access_new.csv").write_text(deps_mod.render_csv(t_new), encoding="utf-8-sig")
        summary["outputs"] += [str(dd / n) for n in ("deps_new.md", "deps_old.md", "deps_new.json", "deps_access_new.csv")]

        if args.scenario or args.explore:
            log("5/5 SIL 실행 중...")
        if args.scenario:
            try:
                sil = run_sil(old_dir, new_dir, load_scenario(args.scenario), out / "sil", cfg, args.sil_timeout)
                report.merge_sil(analysis, sil)
                summary["sil"] = {"cases": len(sil["cases"]), "diff_cases": sil["diff_cases"]}
                summary["outputs"].append(str(out / "sil" / "sil_report.md"))
            except (SilError, OSError, ValueError) as e:
                warnings.append(f"SIL 실행 실패: {str(e).splitlines()[0]}")
        if args.explore:
            from c_impact.explore import explore
            try:
                ex = explore(old, new, analysis, cfg, out / "explore", cycles=args.cycles, budget=args.max_cases,
                             seed=args.seed, timeout=args.sil_timeout)
                if not args.scenario:
                    report.merge_sil(analysis, ex["sil"])
                summary["explore"] = {k: ex[k] for k in ("cases", "diff_cases", "witnesses_input_only", "defaults")}
                summary["outputs"].append(str(out / "explore" / "explore_report.md"))
            except (SilError, OSError, ValueError) as e:
                warnings.append(f"자동 탐색 실패: {str(e).splitlines()[0]}")
        written = report.write_outputs(analysis, out / "impact", set(args.format.split(",")))
        summary["outputs"] = [str(p) for p in written] + summary["outputs"]
        summary["analysis"] = analysis

    summary["elapsed"] = time.time() - t0
    summary["outputs"] = [str(out / "summary.md"), str(out / "summary.json")] + summary["outputs"]
    (out / "summary.md").write_text(render_summary(summary), encoding="utf-8")
    (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    if not args.quiet:
        print(terminal(summary))
        print()
    print(f"요약: {out / 'summary.md'}")
    if analysis is not None:
        print(f"상세: {out / 'impact' / 'impact_report.md'}")
        if analysis["excluded"]["parse_errors"]:
            log(f"경고: 파싱하지 못한 파일 {len(analysis['excluded']['parse_errors'])}개 (summary.md 5절)")
        if args.fail_on_impact and analysis["items"]:
            return 3
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="변경 전/후 프로젝트 폴더를 비교해 *.c 변경 영향 분석을 수행한다",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="예) python3 folder_impact.py before/ after/ --explore")
    ap.add_argument("old_dir", help="변경 전 프로젝트 폴더")
    ap.add_argument("new_dir", help="변경 후 프로젝트 폴더")
    ap.add_argument("-o", "--out", default="impact_out/folder", help="결과 폴더 (기본 impact_out/folder)")
    ap.add_argument("--scope", choices=["all", "changed"], default="all",
                    help="all: 전체 *.c 분석 (기본, 파일 간 영향 포함) / changed: 변경된 .c 와 변경 헤더를 쓰는 .c 만 (빠름)")
    ap.add_argument("--exclude", action="append", metavar="GLOB", help="제외할 파일·폴더 패턴 (예: 'test/*', '*_stub.c')")
    ap.add_argument("--force", action="store_true", help="C 파일 변경이 없어도 분석")

    b = ap.add_argument_group("빌드 조건")
    b.add_argument("-I", dest="include_dirs", action="append", default=[], metavar="DIR",
                   help="include 경로 (폴더 기준 상대 또는 절대). 자동 등록 경로보다 먼저 검색")
    b.add_argument("--no-auto-include", action="store_true", help="헤더가 있는 폴더를 include 경로로 자동 등록하지 않음")
    b.add_argument("-D", dest="defines", action="append", default=[], metavar="NAME[=VAL]", help="매크로 정의")
    b.add_argument("-U", dest="undefines", action="append", default=[], metavar="NAME", help="매크로 해제")
    b.add_argument("--strip", action="append", default=[], metavar="KEYWORD", help="지울 컴파일러 확장 키워드 (예: __interrupt)")
    b.add_argument("--compile-commands", help="compile_commands.json (파일별 -I/-D)")
    b.add_argument("--cc-root", help="compile_commands.json 기준 소스 루트 (기본: 변경 후 폴더)")
    b.add_argument("--cpp", default="gcc -E", help="전처리 명령 (기본 'gcc -E')")
    b.add_argument("--system-headers", action="store_true", help="내장 최소 표준 헤더 대신 시스템 헤더 사용")

    a = ap.add_argument_group("분석")
    a.add_argument("--extern-model", help="본문 없는 함수의 효과 정의 JSON")
    a.add_argument("--entry", action="append", default=[], metavar="FUNC", help="진입점(태스크·콜백) 함수")
    a.add_argument("--isr", action="append", default=[], metavar="FUNC", help="ISR 함수")
    a.add_argument("--context-depth", type=int, default=2, help="호출 문맥 구분 길이 (기본 2)")
    a.add_argument("--format", default="md,json,dot", help="상세 보고서 형식 (md,json,dot)")
    a.add_argument("-j", "--jobs", type=int, default=os.cpu_count() or 1, help="병렬 파싱 프로세스 수")
    a.add_argument("--fail-on-impact", action="store_true", help="영향 항목이 있으면 종료 코드 3")
    a.add_argument("-q", "--quiet", action="store_true", help="터미널 요약 생략")

    x = ap.add_argument_group("실행 확인 (SIL, gcc 필요)")
    x.add_argument("--explore", action="store_true", help="결과가 달라지는 입력·상태 자동 탐색")
    x.add_argument("--scenario", help="SIL 시나리오 JSON 실행")
    x.add_argument("--max-cases", type=int, default=300, help="자동 탐색 최대 케이스 수 (기본 300)")
    x.add_argument("--cycles", type=int, default=6, help="반복 주기 수 (기본 6)")
    x.add_argument("--seed", type=int, default=1, help="자동 탐색 seed")
    x.add_argument("--sil-timeout", type=float, default=10.0, help="케이스당 제한 시간(초)")

    args = ap.parse_args(argv)
    try:
        return run(args)
    except (RuntimeError, OSError) as e:
        log(f"오류: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
