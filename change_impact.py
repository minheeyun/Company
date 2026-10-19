#!/usr/bin/env python3
"""사양서 없이 C 코드 변경의 영향 범위를 분석하는 CLI (Change Impact Analysis).

  deps     프로젝트 전체에서 어떤 함수가 어떤 변수를 읽고 쓰는가 (함수 간 의존성)
  slice    한 버전에서 "이 줄(조건식·변수)을 바꾸면" 확인해야 할 함수·변수 (전방 슬라이스, --what-if)
  analyze  변경 전후 코드 비교 → 변수·조건·호출 의존성 추적 → 영향 경로·조건 보고서 (+ 선택: SIL 실행)
  sil      변경 전후 버전을 같은 시나리오로 실행해 변수·출력 차이 비교
  explore  변경 전후 결과가 달라지는 입력·상태를 자동으로 찾아 최소 재현 입력 보고

예)
  python3 change_impact.py analyze --old v1/src --new v2/src -I include -D VARIANT_A=1 -o impact_out
  python3 change_impact.py analyze --git-repo . --old-rev main --new-rev HEAD --compile-commands build/compile_commands.json
  python3 change_impact.py sil --old v1/src --new v2/src --scenario impact_out/sil_scenario.json -o impact_out/sil
  python3 change_impact.py deps --src v2/src --var g_timeout
  python3 change_impact.py slice --src v1/src --at timeout.c:9 --what-if "tick >= 200U"
  python3 change_impact.py explore --old v1/src --new v2/src --max-cases 300 --cycles 6
"""

from __future__ import annotations

import argparse
import contextlib
import os
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

import json

from c_impact import deps as deps_mod
from c_impact import report
from c_impact.analysis import load_program, load_programs, run_analysis, run_slice
from c_impact.explore import explore
from c_impact.frontend import BuildConfig, load_compile_commands
from c_impact.sil import SilError, load_scenario, run_sil


def _add_build_args(ap: argparse.ArgumentParser):
    _add_pair_input(ap)
    _add_cfg_args(ap)


def _add_pair_input(ap: argparse.ArgumentParser):
    g = ap.add_argument_group("입력 (디렉터리 또는 git 리비전)")
    g.add_argument("--old", help="변경 전 소스 루트 (--git-repo 사용 시 저장소 안의 하위 경로)")
    g.add_argument("--new", help="변경 후 소스 루트 (--git-repo 사용 시 저장소 안의 하위 경로)")
    g.add_argument("--git-repo", help="git 저장소 경로. --old-rev/--new-rev 를 꺼내 비교")
    g.add_argument("--old-rev", help="변경 전 리비전 (예: main, HEAD~1, 태그)")
    g.add_argument("--new-rev", help="변경 후 리비전 (생략하면 작업 트리)")


def _add_single_input(ap: argparse.ArgumentParser):
    g = ap.add_argument_group("입력 (디렉터리 또는 git 리비전 하나)")
    g.add_argument("--src", help="소스 루트 (--git-repo 사용 시 저장소 안의 하위 경로)")
    g.add_argument("--git-repo", help="git 저장소 경로")
    g.add_argument("--rev", help="분석할 리비전 (생략하면 작업 트리)")


def _add_analysis_opts(ap: argparse.ArgumentParser, depth: bool = True):
    ap.add_argument("--extern-model", help="본문 없는 함수의 효과 정의 JSON")
    ap.add_argument("--entry", action="append", default=[], metavar="FUNC", help="진입점(태스크·콜백) 함수, 여러 번 지정 가능")
    ap.add_argument("--isr", action="append", default=[], metavar="FUNC", help="ISR 함수 (이름 패턴 외 추가 지정)")
    if depth:
        ap.add_argument("--context-depth", type=int, default=2, help="호출 문맥 구분 길이 (0=구분 안 함, 기본 2)")
    ap.add_argument("-j", "--jobs", type=int, default=os.cpu_count() or 1, help="병렬 파싱 프로세스 수 (기본: CPU 수)")


def _add_cfg_args(ap: argparse.ArgumentParser):
    b = ap.add_argument_group("빌드 조건")
    b.add_argument("--compile-commands", help="compile_commands.json (파일별 -I/-D 사용)")
    b.add_argument("--cc-root", help="compile_commands.json 이 만들어진 소스 루트 (기본: --new 또는 --src)")
    b.add_argument("-I", dest="include_dirs", action="append", default=[], metavar="DIR",
                   help="include 경로 (소스 루트 기준 상대 경로 또는 절대 경로), 여러 번 지정 가능")
    b.add_argument("-D", dest="defines", action="append", default=[], metavar="NAME[=VAL]", help="매크로 정의")
    b.add_argument("-U", dest="undefines", action="append", default=[], metavar="NAME", help="매크로 해제")
    b.add_argument("--force-include", action="append", default=[], metavar="HEADER", help="모든 파일 앞에 포함할 헤더")
    b.add_argument("--strip", action="append", default=[], metavar="KEYWORD",
                   help="지울 컴파일러 확장 키워드 (예: __interrupt, __far, @near 는 불가)")
    b.add_argument("--cpp", default="gcc -E", help="전처리 명령 (기본 'gcc -E', 예: 'clang -E')")
    b.add_argument("--system-headers", action="store_true", help="내장 최소 표준 헤더 대신 시스템 헤더 사용")
    b.add_argument("--include", dest="globs", action="append", metavar="GLOB", help="분석할 파일 패턴 (기본 *.c)")
    b.add_argument("--exclude", action="append", metavar="GLOB", help="제외할 파일 패턴 (예: 'test/*')")


def _build_config(args, new_root: Path) -> BuildConfig:
    cfg = BuildConfig(cpp=args.cpp.split(), use_fake_libc=not args.system_headers)
    for d in args.include_dirs:
        p = Path(d)
        cfg.include_dirs.append(d if p.is_absolute() else "{ROOT}/" + d)
    cfg.defines = list(args.defines) + [f"{k}=" for k in args.strip]
    cfg.undefines = list(args.undefines)
    cfg.forced_includes = [f if Path(f).is_absolute() else "{ROOT}/" + f for f in args.force_include]
    if args.compile_commands:
        cfg.per_file = load_compile_commands(args.compile_commands, args.cc_root or new_root)
    return cfg


def _export_rev(repo: Path, rev: str, dest: Path):
    dest.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen(["git", "-C", str(repo), "archive", "--format=tar", rev], stdout=subprocess.PIPE)
    with tarfile.open(fileobj=proc.stdout, mode="r|") as tar:
        try:
            tar.extractall(dest, filter="data")
        except TypeError:  # Python < 3.12
            tar.extractall(dest)
    proc.stdout.close()
    if proc.wait() != 0:
        raise RuntimeError(f"git archive {rev} 실패")


@contextlib.contextmanager
def _roots(args):
    if not args.git_repo:
        if not args.old or not args.new:
            raise SystemExit("오류: --old 와 --new 를 지정하거나 --git-repo 와 --old-rev 를 지정하세요.")
        yield Path(args.old).resolve(), Path(args.new).resolve()
        return
    if not args.old_rev:
        raise SystemExit("오류: --git-repo 사용 시 --old-rev 가 필요합니다.")
    repo = Path(args.git_repo).resolve()
    with tempfile.TemporaryDirectory(prefix="cia_git_") as tmp:
        old = Path(tmp) / "old"
        _export_rev(repo, args.old_rev, old)
        if args.new_rev:
            new = Path(tmp) / "new"
            _export_rev(repo, args.new_rev, new)
        else:
            new = repo
        sub_old, sub_new = args.old or ".", args.new or args.old or "."
        yield (old / sub_old).resolve(), (new / sub_new).resolve()


@contextlib.contextmanager
def _single_root(args):
    if not args.git_repo:
        if not args.src:
            raise SystemExit("오류: --src 를 지정하거나 --git-repo [--rev] 를 지정하세요.")
        yield Path(args.src).resolve()
        return
    repo = Path(args.git_repo).resolve()
    if not args.rev:
        yield (repo / (args.src or ".")).resolve()
        return
    with tempfile.TemporaryDirectory(prefix="cia_git_") as tmp:
        _export_rev(repo, args.rev, Path(tmp))
        yield (Path(tmp) / (args.src or ".")).resolve()


def cmd_deps(args) -> int:
    with _single_root(args) as root:
        cfg = _build_config(args, root)
        prog = load_program(root, cfg, args.globs, args.exclude, args.extern_model, args.jobs)
        if not prog.tus:
            print("오류: 분석할 소스 파일이 없습니다 (--include 패턴 확인).", file=sys.stderr)
            return 1
        t = deps_mod.dependency_table(prog, args.entry, args.isr)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "deps.json").write_text(json.dumps(t, ensure_ascii=False, indent=2), encoding="utf-8")
    (out / "deps.md").write_text(deps_mod.render_md(t, args.transitive), encoding="utf-8")
    (out / "deps_access.csv").write_text(deps_mod.render_csv(t), encoding="utf-8-sig")
    if args.query_func or args.var:
        print(deps_mod.query(t, args.query_func, args.var))
        print()
    else:
        print(f"함수 {len(t['functions'])}개 · 상태 변수 {len(t['variables'])}개")
    for name in ("deps.md", "deps.json", "deps_access.csv"):
        print(f"생성: {out / name}")
    if t["parse_errors"]:
        print(f"경고: 파싱하지 못한 파일 {len(t['parse_errors'])}개", file=sys.stderr)
    return 0


def cmd_slice(args) -> int:
    if not args.at and not args.var:
        print("오류: --at 파일:줄 또는 --var 변수 중 하나 이상이 필요합니다.", file=sys.stderr)
        return 1
    with _single_root(args) as root:
        cfg = _build_config(args, root)
        prog = load_program(root, cfg, args.globs, args.exclude, args.extern_model, args.jobs)
        try:
            r = run_slice(prog, cfg, at=args.at, var=args.var, what_if=args.what_if, k=args.context_depth,
                          entries=args.entry, isr=args.isr)
        except ValueError as e:
            print(f"오류: {e}", file=sys.stderr)
            return 1
        r["parse_errors"] = prog.parse_errors
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "slice.json").write_text(json.dumps(r, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    (out / "slice_report.md").write_text(report.render_slice_md(r), encoding="utf-8")
    for w in r["warnings"]:
        print(f"경고: {w}", file=sys.stderr)
    if r.get("what_if") and r["what_if"]["detail"]:
        print(f"가정: {r['what_if']['current']} → {r['what_if']['assumed']}  ({r['what_if']['detail']})")
    print(f"영향 함수 {len(r['affected_functions'])}개 · 확인할 변수·출력 {len(r['items'])}개")
    for it in r["items"]:
        cond = "; ".join(it["conditions"]) or "모든 실행"
        print(f"  {it['func_display']}  {it['name']}  {it['how_kind']}: {it['how']}  | 조건: {cond}")
    print(f"생성: {out / 'slice_report.md'}")
    return 0


def cmd_explore(args) -> int:
    with _roots(args) as (old_root, new_root):
        cfg = _build_config(args, new_root)
        old, new = load_programs(old_root, new_root, cfg, args.globs, args.exclude, args.extern_model, args.jobs)
        result = run_analysis(old, new, cfg, k=args.context_depth, entries=args.entry, isr=args.isr,
                              sil_cycles=args.cycles)
        out = Path(args.out)
        base = load_scenario(args.scenario) if args.scenario else None
        try:
            s = explore(old, new, result, cfg, out, base=base, cycles=args.cycles, budget=args.max_cases,
                        seed=args.seed, timeout=args.sil_timeout)
        except (SilError, ValueError) as e:
            print(f"오류: {e}", file=sys.stderr)
            return 1
        report.merge_sil(result, s["sil"])
        report.write_outputs(result, out, {"md", "json"})
    print(f"케이스 {s['cases']}개 실행 · 차이 발생 {s['diff_cases']}개")
    for lab, w in sorted(s["witnesses"].items()):
        b = ", ".join(f"{k}={json.dumps(v)}" for k, v in w["bindings"].items() if v != s["defaults"].get(k)) or "기본값"
        print(f"  {lab}: {b}  → 단계 #{w['step']} {w['step_label']}: {w['old']} → {w['new']}")
    print(f"생성: {out / 'explore_report.md'}")
    print(f"생성: {out / 'impact_report.md'} (실행 확인 상태 반영)")
    return 0


def cmd_analyze(args) -> int:
    with _roots(args) as (old_root, new_root):
        cfg = _build_config(args, new_root)
        old, new = load_programs(old_root, new_root, cfg, args.globs, args.exclude, args.extern_model, args.jobs)
        if not new.tus and not old.tus:
            print("오류: 분석할 소스 파일이 없습니다 (--include 패턴 확인).", file=sys.stderr)
            return 1
        result = run_analysis(old, new, cfg, k=args.context_depth, entries=args.entry, isr=args.isr,
                              sil_cycles=args.sil_cycles)
        out_dir = Path(args.out)
        if args.scenario:
            try:
                sil = run_sil(old_root, new_root, load_scenario(args.scenario), out_dir / "sil", cfg, args.sil_timeout)
                report.merge_sil(result, sil)
            except SilError as e:
                print(f"SIL 실행 실패: {e}", file=sys.stderr)
                result["excluded"]["warnings"].append(f"SIL 실행 실패: {str(e).splitlines()[0]}")
        files = report.write_outputs(result, out_dir, set(args.format.split(",")))
    if not args.quiet:
        print(report.terminal_summary(result))
        print()
    for f in files:
        print(f"생성: {f}")
    errs = result["excluded"]["parse_errors"]
    if errs:
        print(f"경고: 파싱하지 못한 파일 {len(errs)}개 (보고서 7절 참고)", file=sys.stderr)
    if args.fail_on_impact and result["items"]:
        return 3
    return 0


def cmd_sil(args) -> int:
    with _roots(args) as (old_root, new_root):
        cfg = _build_config(args, new_root)
        try:
            s = run_sil(old_root, new_root, load_scenario(args.scenario), Path(args.out), cfg, args.sil_timeout)
        except (SilError, OSError, ValueError) as e:
            print(f"오류: {e}", file=sys.stderr)
            return 1
    print(f"케이스 {len(s['cases'])}개 중 차이 발생 {s['diff_cases']}개")
    for lab, ob in sorted(s["observables"].items()):
        if ob["diff_cases"]:
            ex = ob["example"]
            print(f"  {lab}: {ob['diff_cases']}개 케이스 (예: {ex['case']}, 단계 #{ex['step']} {ex['step_label']}: "
                  f"{ex['old']} → {ex['new']})")
    print(f"생성: {Path(args.out) / 'sil_report.md'}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="C 코드 변경 영향 분석 (전방 슬라이싱 + SIL 차등 실행)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("deps", help="함수·변수 읽기/쓰기 의존성 표")
    _add_single_input(d)
    _add_cfg_args(d)
    _add_analysis_opts(d, depth=False)
    d.add_argument("-o", "--out", default="impact_out/deps", help="결과 폴더")
    d.add_argument("--func", dest="query_func", help="이 함수의 읽기·쓰기·호출 관계를 터미널에 출력")
    d.add_argument("--var", help="이 변수(구조체면 하위 필드 포함)를 읽고 쓰는 함수를 터미널에 출력")
    d.add_argument("--transitive", action="store_true", help="보고서에 '호출을 통해 추가로 읽고 쓰는 변수' 열 추가")
    d.set_defaults(func=cmd_deps)

    sl = sub.add_parser("slice", help="한 버전에서 줄·변수를 바꾸면 영향받는 범위 (what-if)")
    _add_single_input(sl)
    _add_cfg_args(sl)
    _add_analysis_opts(sl)
    sl.add_argument("--at", action="append", default=[], metavar="FILE:LINE", help="시작 위치 (예: timeout.c:9), 여러 번 지정 가능")
    sl.add_argument("--var", help="시작 변수. --at 와 함께면 그 줄에서 이 변수를 쓰는 문장만, 단독이면 이 변수에 쓰는 모든 문장")
    sl.add_argument("--what-if", help="--at 줄의 조건식·대입식을 이 식으로 바꾼다고 가정 (판정 차이 구간 계산)")
    sl.add_argument("-o", "--out", default="impact_out/slice", help="결과 폴더")
    sl.set_defaults(func=cmd_slice)

    a = sub.add_parser("analyze", help="변경 영향 분석 보고서 생성")
    _add_build_args(a)
    _add_analysis_opts(a)
    a.add_argument("-o", "--out", default="impact_out", help="결과 폴더 (기본 impact_out)")
    a.add_argument("--format", default="md,json,dot", help="출력 형식 (md,json,dot 중 선택)")
    a.add_argument("--scenario", help="SIL 시나리오 JSON. 지정하면 분석 후 차등 실행 결과를 보고서에 합친다")
    a.add_argument("--sil-cycles", type=int, default=3, help="자동 생성 시나리오의 반복 주기 수 (기본 3)")
    a.add_argument("--sil-timeout", type=float, default=10.0, help="SIL 케이스당 제한 시간(초)")
    a.add_argument("--fail-on-impact", action="store_true", help="영향 항목이 있으면 종료 코드 3 (CI 게이트용)")
    a.add_argument("-q", "--quiet", action="store_true", help="터미널 요약 생략")
    a.set_defaults(func=cmd_analyze)

    s = sub.add_parser("sil", help="SIL 차등 실행")
    _add_build_args(s)
    s.add_argument("--scenario", required=True, help="시나리오 JSON")
    s.add_argument("-o", "--out", default="impact_out/sil", help="결과 폴더")
    s.add_argument("--sil-timeout", type=float, default=10.0, help="케이스당 제한 시간(초)")
    s.set_defaults(func=cmd_sil)

    e = sub.add_parser("explore", help="변경 전후 결과가 달라지는 입력·상태 자동 탐색")
    _add_build_args(e)
    _add_analysis_opts(e)
    e.add_argument("--scenario", help="기준 시나리오 (생략하면 analyze 의 초안 시나리오 사용). ${이름} 자리표시자가 탐색 대상")
    e.add_argument("--max-cases", type=int, default=300, help="실행할 최대 케이스 수 (기본 300)")
    e.add_argument("--cycles", type=int, default=6, help="반복 주기 수 (기준 시나리오가 없을 때, 기본 6)")
    e.add_argument("--seed", type=int, default=1, help="무작위 케이스 seed (같은 seed 면 같은 케이스)")
    e.add_argument("--sil-timeout", type=float, default=10.0, help="케이스당 제한 시간(초)")
    e.add_argument("-o", "--out", default="impact_out/explore", help="결과 폴더")
    e.set_defaults(func=cmd_explore)

    args = ap.parse_args(argv)
    try:
        return args.func(args)
    except (RuntimeError, OSError) as e:
        print(f"오류: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
