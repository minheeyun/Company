"""명령행 인터페이스.

  python3 -m c_impact analyze   ... 전체 분석(1~6단계) → JSON/CSV/DOT/SVG/HTML
  python3 -m c_impact ast       ... 1단계: 한 트리의 AST 모델(함수/변수/호출/문장) 덤프
  python3 -m c_impact changes   ... 2단계: 변경점 탐지 결과
  python3 -m c_impact callgraph ... 3단계: Call Graph(DOT/JSON)
  python3 -m c_impact cfg       ... 4단계: 함수 CFG(DOT) 및 경로
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import sys

from .config import AnalysisConfig


def _add_build(p):
    g = p.add_argument_group("빌드/파싱 옵션")
    g.add_argument("--compdb", help="compile_commands.json 경로 (없으면 제한 모드)")
    g.add_argument("--compdb-root", help="compile_commands.json 이 가리키는 소스 트리 루트 (기본: Current 루트/저장소)")
    g.add_argument("-I", "--include", dest="include_dirs", action="append", default=[], help="include 디렉터리")
    g.add_argument("-D", "--define", dest="defines", action="append", default=[], help="전처리 매크로 (NAME 또는 NAME=VAL)")
    g.add_argument("--std", default="c99", help="C 표준 (기본 c99)")
    g.add_argument("--target", help="clang 타깃 트리플 (예: arm-none-eabi, msp430) — 자료형 크기에 반영")
    g.add_argument("--extra-arg", dest="extra_args", action="append", default=[], help="clang 에 그대로 넘길 인자")
    g.add_argument("--jobs", type=int, default=1, help="병렬 파싱 수")


def _add_sources(p):
    g = p.add_argument_group("입력 (하나의 모드 선택)")
    g.add_argument("--base-dir", help="변경 전(Baseline) 프로젝트 루트")
    g.add_argument("--curr-dir", help="변경 후(Current) 프로젝트 루트")
    g.add_argument("--patch", help="Baseline 에 적용할 unified diff(git diff) 파일 — 결과를 Current 로 사용")
    g.add_argument("--repo", help="Git 저장소(또는 하위 프로젝트 디렉터리)")
    g.add_argument("--base-rev", help="기준 리비전 (기본 HEAD)")
    g.add_argument("--head-rev", help="비교 리비전 (생략 시 작업 트리)")
    g.add_argument("--working-tree", action="store_true", help="기준 리비전 대비 작업 트리 변경 분석")
    g.add_argument("--files", nargs="*", help="변경점 분석 대상 파일(프로젝트 루트 기준 상대경로). 영향 분석은 전체 프로젝트")


def build_parser():
    ap = argparse.ArgumentParser(prog="c_impact", description="AST/CFG 기반 Embedded C 변경 영향성 분석")
    sub = ap.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("analyze", help="전체 영향성 분석")
    _add_sources(a)
    _add_build(a)
    g = a.add_argument_group("분석 설정")
    g.add_argument("--config", help="분석 설정 JSON (플랫폼, 태스크/ISR, 패턴, 요구사항/기존 테스트 매핑 등)")
    g.add_argument("--build-config", help="빌드 설정 이름(보고서 표기)")
    g.add_argument("--variant", action="append", default=[],
                   help='추가 빌드 설정 "이름:-DA -DB=1" — 설정별로 별도 결과 디렉터리 생성')
    g.add_argument("--max-depth", type=int)
    g.add_argument("--max-paths", type=int)
    g.add_argument("--loop-unroll", type=int)
    g.add_argument("--no-per-change", action="store_true", help="변경점별 개별 영향 분석 생략(대규모 변경 시)")
    o = a.add_argument_group("출력")
    o.add_argument("-o", "--out", default="impact_out", help="결과 디렉터리")
    o.add_argument("--format", default="json,csv,dot,html", help="출력 형식 (json,csv,dot,html)")
    o.add_argument("--cache-dir", help="파싱 캐시 디렉터리 (기본: <out>/.cache)")
    o.add_argument("--no-cache", action="store_true")
    o.add_argument("-q", "--quiet", action="store_true")

    s1 = sub.add_parser("ast", help="1단계: AST 모델 덤프")
    s1.add_argument("root")
    s1.add_argument("--function", help="이 함수만 출력")
    s1.add_argument("-o", "--out", help="JSON 출력 파일 (기본 stdout 요약)")
    _add_build(s1)

    s2 = sub.add_parser("changes", help="2단계: 변경점 탐지")
    _add_sources(s2)
    _add_build(s2)
    s2.add_argument("--config")
    s2.add_argument("-o", "--out", help="JSON 출력 파일")

    s3 = sub.add_parser("callgraph", help="3단계: Call Graph")
    s3.add_argument("root")
    s3.add_argument("--config")
    s3.add_argument("--dot", help="DOT 출력 파일")
    s3.add_argument("--from", dest="from_fn", help="이 함수의 상위 호출 체인 출력")
    _add_build(s3)

    s4 = sub.add_parser("cfg", help="4단계: 함수 CFG")
    s4.add_argument("root")
    s4.add_argument("function")
    s4.add_argument("--dot", help="DOT 출력 파일")
    s4.add_argument("--max-paths", type=int, default=50)
    _add_build(s4)
    return ap


def _load_cfg(args):
    cfg = AnalysisConfig.load(getattr(args, "config", None))
    for k in ("max_depth", "max_paths", "loop_unroll"):
        v = getattr(args, k, None)
        if v is not None:
            setattr(cfg, k, v)
    if getattr(args, "build_config", None):
        cfg.build_config_name = args.build_config
    return cfg


def _load(root, args, cfg=None):
    from .compdb import CompileDB
    from .project import load_project
    cfg = cfg or AnalysisConfig()
    compdb = CompileDB(args.compdb, args.compdb_root or root) if args.compdb else None
    return load_project(root, os.path.basename(os.path.abspath(root)), compdb=compdb, include_dirs=args.include_dirs,
                        defines=args.defines, std=args.std, target=args.target, extra_args=args.extra_args,
                        external_globs=cfg.external_globs, exclude_globs=cfg.exclude_globs, jobs=args.jobs)


def cmd_analyze(args):
    from .pipeline import Options, analyze
    from .reports import write_all
    from .sources import prepare
    cfg = _load_cfg(args)
    log = (lambda m: None) if args.quiet else (lambda m: print(m, file=sys.stderr))
    variants = [(cfg.build_config_name, [])]
    for v in args.variant:
        name, _, flags = v.partition(":")
        variants.append((name.strip(), [x[2:] for x in flags.split() if x.startswith("-D")]))
    with prepare(args) as src:
        targets = src.target_files
        outputs = []
        for name, defs in variants:
            cfg_v = copy.deepcopy(cfg)
            cfg_v.build_config_name = name
            out = args.out if name == variants[0][0] and not defs else os.path.join(args.out, f"variant_{name}")
            cache = None if args.no_cache else (args.cache_dir or os.path.join(args.out, ".cache"))
            opt = Options(compdb=args.compdb, compdb_root=args.compdb_root or (src.meta.get("repo") or src.curr_root),
                          include_dirs=args.include_dirs, defines=list(args.defines) + defs, std=args.std,
                          target=args.target, extra_args=args.extra_args, cache_dir=cache, jobs=args.jobs,
                          target_files=targets, per_change=not args.no_per_change, label=src.label,
                          source_meta=src.meta)
            log(f"== 빌드 설정 '{name}' 분석: {src.label}")
            result = analyze(src.base_root, src.curr_root, cfg_v, opt, progress=log)
            files = write_all(result, out, tuple(x.strip() for x in args.format.split(",")))
            outputs.append((name, result, files))
    for name, result, files in outputs:
        s = result["summary"]
        lv = s["functions_by_level"]
        print(f"[{name}] 변경 {s['changes']}건 (포맷만 {s['non_semantic_changes']}건) | "
              f"DIRECT {lv['DIRECT']} / INDIRECT {lv['INDIRECT']} / POTENTIAL {lv['POTENTIAL']} / "
              f"NO_IMPACT {lv['NO_IMPACT_CONFIRMED']} 함수 | 결함 후보 {s['defects']['total']}(확정 {s['defects']['confirmed']}) | "
              f"테스트 {s['tests']['total']}")
        if not s["complete"]:
            print("  ⚠ " + " / ".join(result["completeness"]["notes"]))
        for k, p in files.items():
            print(f"  {k:14} {p}")
    return 0


def cmd_ast(args):
    from .ast_extract import iter_stmts
    p = _load(args.root, args)
    if args.out:
        data = {"summary": p.to_summary(), "functions": p.functions, "globals": p.globals, "macros": p.macros,
                "typedefs": p.typedefs, "records": p.records, "enums": p.enums, "func_decls": p.func_decls}
        if args.function:
            data["functions"] = {k: v for k, v in p.functions.items() if v["name"] == args.function or k == args.function}
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        print(f"저장: {args.out}")
        return 0
    print(json.dumps(p.to_summary(), ensure_ascii=False, indent=1))
    for fid, fn in sorted(p.functions.items()):
        if args.function and args.function not in (fid, fn["name"]):
            continue
        print(f"\n# {fid}  [{fn['file']}:{fn['line']}]  {fn['sig']}")
        if args.function:
            for st in iter_stmts(fn["body"]):
                print(f"  S{st['id']:<3} L{st['line']:<4} {st['kind']:<7} {st['text'][:70]}")
                if st.get("defs") or st.get("uses"):
                    print(f"        def={st.get('defs')} use={st.get('uses')}")
        calls = ", ".join(sorted({c['name'] + ('(*)' if c.get('indirect') else '') for c in fn["calls"]}))
        print(f"  calls: {calls}")
    return 0


def cmd_changes(args):
    from .change_detect import detect_changes
    from .roles import Roles
    from .sources import prepare
    cfg = _load_cfg(args)
    with prepare(args) as src:
        b = _load(src.base_root, args, cfg)
        c = _load(src.curr_root, args, cfg)
        cs = detect_changes(b, c, Roles(b, cfg), Roles(c, cfg), cfg, only_files=src.target_files)
    data = cs.to_dict()
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1, default=str)
        print(f"저장: {args.out}")
    for ch in cs.changes:
        loc = ch.get("curr") or ch.get("base") or {}
        print(f"{ch['id']} [{ch['category']}] {loc.get('file')}:{loc.get('line')} {', '.join(ch['labels'])}\n    {ch['description']}")
    for n in cs.non_semantic:
        print(f"  (의미 변경 아님) {n['type']} {n.get('function') or n.get('file')} — {n.get('note')}")
    return 0


def cmd_callgraph(args):
    from .callgraph import CallGraph
    cfg = _load_cfg(args)
    p = _load(args.root, args, cfg)
    cg = CallGraph(p, cfg)
    if args.dot:
        lines = ["digraph callgraph {", "  rankdir=LR; node [shape=box, fontname=Helvetica];"]
        for e in cg.edges:
            st = "solid" if e["certainty"] == "confirmed" else "dashed"
            lines.append(f'  "{e["caller"]}" -> "{e["callee"]}" [style={st}];')
        lines.append("}")
        with open(args.dot, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        print(f"저장: {args.dot}")
    if args.from_fn:
        for c in cg.chains_to_entries(args.from_fn):
            print(" → ".join(c["chain"]), "(간접 포함)" if c["potential"] else "", f"[{c['entry_reason']}]")
        return 0
    for e in cg.edges:
        print(f"{e['caller']} -> {e['callee']}  L{e['line']} {e['kind']}/{e['certainty']}  {e['reason']}")
    print("진입점:", ", ".join(sorted(cg.entry_points)))
    if cg.cycles():
        print("순환 호출:", cg.cycles())
    return 0


def cmd_cfg(args):
    from .cfg import CFG
    p = _load(args.root, args)
    fid = next((k for k, v in p.functions.items() if args.function in (k, v["name"])), None)
    if fid is None:
        print(f"함수 없음: {args.function}", file=sys.stderr)
        return 1
    g = CFG(p.functions[fid])
    if args.dot:
        with open(args.dot, "w", encoding="utf-8") as f:
            f.write(g.to_dot(fid))
        print(f"저장: {args.dot}")
    paths, trunc = g.paths(args.max_paths)
    print(f"{fid}: 노드 {len(g.nodes)}, 경로 {len(paths)}{' (한도)' if trunc else ''}")
    for i, pth in enumerate(paths, 1):
        dec = [f"{t}={l}" for _, t, l in g.path_decisions(pth)]
        print(f"  P{i}: {' ∧ '.join(dec) or '(무조건)'}  feasible={g.feasible(pth)}")
    for x in g.static_branch_outcomes():
        print(f"  고정 분기: L{x['line']} {x['text']} 항상 {x['always']}")
    for n in g.unreachable_nodes():
        print(f"  도달 불가: L{g.nodes[n]['line']} {g.nodes[n]['text']}")
    return 0


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        return {"analyze": cmd_analyze, "ast": cmd_ast, "changes": cmd_changes, "callgraph": cmd_callgraph,
                "cfg": cmd_cfg}[args.cmd](args)
    except Exception as e:  # 사용자에게 원인 표시
        from .clang_frontend import ClangUnavailable
        from .sources import SourceError
        if isinstance(e, (ClangUnavailable, SourceError, ValueError, FileNotFoundError)):
            print(f"오류: {e}", file=sys.stderr)
            return 2
        raise


if __name__ == "__main__":
    sys.exit(main())
