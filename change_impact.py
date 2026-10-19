#!/usr/bin/env python3
"""사양서 없이 C 코드 변경의 영향 범위를 분석하는 CLI (Change Impact Analysis).

  analyze  변경 전후 코드 비교 → 변수·조건·호출 의존성 추적 → 영향 경로·조건 보고서 (+ 선택: SIL 실행)
  sil      변경 전후 버전을 같은 시나리오로 실행해 변수·출력 차이 비교

예)
  python3 change_impact.py analyze --old v1/src --new v2/src -I include -D VARIANT_A=1 -o impact_out
  python3 change_impact.py analyze --git-repo . --old-rev main --new-rev HEAD --compile-commands build/compile_commands.json
  python3 change_impact.py sil --old v1/src --new v2/src --scenario impact_out/sil_scenario.json -o impact_out/sil
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

from c_impact import report
from c_impact.analysis import load_programs, run_analysis
from c_impact.frontend import BuildConfig, load_compile_commands
from c_impact.sil import SilError, load_scenario, run_sil


def _add_build_args(ap: argparse.ArgumentParser):
    g = ap.add_argument_group("입력 (디렉터리 또는 git 리비전)")
    g.add_argument("--old", help="변경 전 소스 루트 (--git-repo 사용 시 저장소 안의 하위 경로)")
    g.add_argument("--new", help="변경 후 소스 루트 (--git-repo 사용 시 저장소 안의 하위 경로)")
    g.add_argument("--git-repo", help="git 저장소 경로. --old-rev/--new-rev 를 꺼내 비교")
    g.add_argument("--old-rev", help="변경 전 리비전 (예: main, HEAD~1, 태그)")
    g.add_argument("--new-rev", help="변경 후 리비전 (생략하면 작업 트리)")
    b = ap.add_argument_group("빌드 조건")
    b.add_argument("--compile-commands", help="compile_commands.json (파일별 -I/-D 사용)")
    b.add_argument("--cc-root", help="compile_commands.json 이 만들어진 소스 루트 (기본: --new)")
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

    a = sub.add_parser("analyze", help="변경 영향 분석 보고서 생성")
    _add_build_args(a)
    a.add_argument("-o", "--out", default="impact_out", help="결과 폴더 (기본 impact_out)")
    a.add_argument("--extern-model", help="본문 없는 함수의 효과 정의 JSON")
    a.add_argument("--entry", action="append", default=[], metavar="FUNC", help="진입점(태스크·콜백) 함수, 여러 번 지정 가능")
    a.add_argument("--isr", action="append", default=[], metavar="FUNC", help="ISR 함수 (이름 패턴 외 추가 지정)")
    a.add_argument("--context-depth", type=int, default=2, help="호출 문맥 구분 길이 (0=구분 안 함, 기본 2)")
    a.add_argument("--format", default="md,json,dot", help="출력 형식 (md,json,dot 중 선택)")
    a.add_argument("--scenario", help="SIL 시나리오 JSON. 지정하면 분석 후 차등 실행 결과를 보고서에 합친다")
    a.add_argument("--sil-cycles", type=int, default=3, help="자동 생성 시나리오의 반복 주기 수 (기본 3)")
    a.add_argument("--sil-timeout", type=float, default=10.0, help="SIL 케이스당 제한 시간(초)")
    a.add_argument("--fail-on-impact", action="store_true", help="영향 항목이 있으면 종료 코드 3 (CI 게이트용)")
    a.add_argument("-j", "--jobs", type=int, default=os.cpu_count() or 1, help="병렬 파싱 프로세스 수 (기본: CPU 수)")
    a.add_argument("-q", "--quiet", action="store_true", help="터미널 요약 생략")
    a.set_defaults(func=cmd_analyze)

    s = sub.add_parser("sil", help="SIL 차등 실행")
    _add_build_args(s)
    s.add_argument("--scenario", required=True, help="시나리오 JSON")
    s.add_argument("-o", "--out", default="impact_out/sil", help="결과 폴더")
    s.add_argument("--sil-timeout", type=float, default=10.0, help="케이스당 제한 시간(초)")
    s.set_defaults(func=cmd_sil)

    args = ap.parse_args(argv)
    try:
        return args.func(args)
    except (RuntimeError, OSError) as e:
        print(f"오류: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
