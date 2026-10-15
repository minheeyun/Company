"""명령행 진입점.

예)
  # 전체 파이프라인 (Claude API)
  python3 spec_verify.py --spec docs/spec.md --src src/ --env env.json -o out/

  # LLM 없이 정적 분석만
  python3 spec_verify.py --src src/ --env env.json --static-only -o out/

  # 사내 LLM 창구 사용: 프롬프트 파일을 내보내고, 응답 JSON을 저장한 뒤 같은 명령을 다시 실행
  python3 spec_verify.py --spec docs/spec.md --src src/ --manual out/llm -o out/
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

from . import __version__
from . import cfront as C
from . import report as R
from . import stages as ST
from .env import describe, load_env
from .llm import DEFAULT_MODEL, ClaudeLLM, FileLLM, LLMError, PendingResponse
from .spec import load_spec


def _rel(path: str) -> str:
    """보고서에 로컬 절대 경로가 남지 않도록 현재 폴더 기준 상대 경로로 표시"""
    try:
        return Path(path).resolve().relative_to(Path.cwd().resolve()).as_posix() or "."
    except ValueError:
        return Path(path).name


def _snippet(model: C.CodeModel, file: str, line: int, ctx: int = 2) -> str:
    f = model.resolve_file(file or "")
    if not f or not line:
        return ""
    return C.render_excerpt(model, f, int(line) - ctx, int(line) + ctx, {int(line)})


def assemble(args, model, env, cands, tool_notes, spec, pipe: dict | None, llm) -> dict:
    for c in cands:
        c.snippet = _snippet(model, c.file, c.line)
    result = {
        "meta": {
            "tool": f"specverify {__version__}",
            "generated_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "spec": spec.name if spec else None,
            "sources": args.src,
            "llm": llm.describe() if llm else "사용 안 함 (정적 분석만)",
            "stages": ["0 정적 분석"] + ([f"{i} {name}" for i, (_, name) in enumerate(ST.STAGES, 1) if i <= args.stop_after]
                                       if pipe else []),
        },
        "environment": {"source": env.source, "summary": describe(env), "assumptions": env.assumptions()},
        "code": {
            "root": _rel(model.root), "preprocessor": model.preprocessor, "tool_notes": tool_notes,
            "files": [{"path": sf.path, "lines": len(sf.lines), "preprocessed": sf.preprocessed} for sf in model.files.values()],
            "functions": len(model.functions),
            "issues": [{"file": i.file, "message": i.message, "severity": i.severity} for i in model.issues],
        },
        "candidates": [c.to_dict() for c in cands],
    }
    if pipe:
        for f in pipe["findings"]:
            for r in f.get("code_refs") or []:
                r["snippet"] = _snippet(model, r.get("file"), r.get("line"))
        result.update(pipe)
    result["llm_usage"] = getattr(llm, "usage", []) if llm else []
    return result


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="spec_verify",
        description="자연어 기능사양서와 Embedded C 코드 간 의미적 정합성 검증 및 사양 공백·잠재 결함 분석")
    ap.add_argument("--spec", help="기능사양서 (.md/.txt/.pdf/.docx)")
    ap.add_argument("--src", action="append", default=[], help="C 소스 파일/폴더 (여러 번 지정 가능)")
    ap.add_argument("--env", help="실행 환경 정보 JSON (자료형 크기, 태스크/ISR, 보호 함수, 컴파일 정의)")
    ap.add_argument("-D", "--define", action="append", default=[], help="컴파일 정의 추가 (예: -D BLV_USE_FILTER=0)")
    ap.add_argument("-I", "--include", action="append", default=[], help="헤더 검색 경로 추가")
    ap.add_argument("-o", "--out", default="specverify_out", help="결과 폴더 (기본: specverify_out)")
    ap.add_argument("--static-only", action="store_true", help="LLM 없이 정적 분석 결과만 보고")
    ap.add_argument("--manual", metavar="DIR", help="API 대신 프롬프트/응답 파일 교환 (사내 LLM 창구용)")
    ap.add_argument("--resume", action="store_true", help="결과 폴더의 단계별 결과(stages/*.json)를 재사용")
    ap.add_argument("--stop-after", type=int, default=5, choices=range(1, 6), help="이 단계까지만 수행 (1~5)")
    ap.add_argument("--model", default=DEFAULT_MODEL, help=f"Claude 모델 (기본 {DEFAULT_MODEL})")
    ap.add_argument("--effort", default="high", choices=["low", "medium", "high", "xhigh", "max"])
    ap.add_argument("--no-fallback", action="store_true", help="서버측 거절 대체(fallbacks) 비활성화")
    ap.add_argument("--batch-size", type=int, default=6, help="정합성 분석 1회 호출당 요구사항 수")
    ap.add_argument("--max-source-chars", type=int, default=400_000,
                    help="전체 소스를 문맥에 넣는 최대 크기 (초과 시 관련 함수만 발췌)")
    ap.add_argument("--no-gcc", action="store_true", help="gcc 경고 교차 확인 생략")
    ap.add_argument("--tool-cc", default="gcc", help="경고 수집에 쓸 컴파일러 (대상 교차 컴파일러 권장)")
    ap.add_argument("--gcc-analyzer", action="store_true", help="gcc -fanalyzer 경로 분석 추가 (느림)")
    ap.add_argument("--cppcheck", action="store_true", help="cppcheck 결과 추가 (설치 필요)")
    ap.add_argument("--report-only", metavar="REPORT_JSON", help="기존 report.json으로 보고서만 다시 생성")
    ap.add_argument("--prompt-dir", help="단계별 프롬프트 폴더 (기본: prompts/specverify)")
    args = ap.parse_args(argv)

    if args.report_only:
        result = json.loads(Path(args.report_only).read_text(encoding="utf-8"))
        paths = R.write_reports(result, Path(args.out))
        print(f"보고서 재생성 → {paths['html']}")
        return 0
    if not args.src:
        ap.error("--src 가 필요합니다.")
    if not args.static_only and not args.spec:
        ap.error("--spec 이 필요합니다 (정적 분석만 하려면 --static-only).")

    out = Path(args.out)
    try:
        env = load_env(args.env)
        for d in args.define:
            k, _, v = d.partition("=")
            env.defines[k] = v
        env.include_dirs += args.include
        spec = load_spec(args.spec) if args.spec else None
    except (OSError, ValueError, json.JSONDecodeError) as e:
        print(f"오류: {e}", file=sys.stderr)
        return 1

    print("[0/5] 정적 코드 분석 (전처리·AST·규칙 검사·도구 교차 확인)", file=sys.stderr)
    try:
        model, cands, tool_notes = ST.static_analysis(args.src, env, use_gcc=not args.no_gcc, gcc=args.tool_cc,
                                                      analyzer=args.gcc_analyzer, cppcheck=args.cppcheck)
    except (OSError, ValueError) as e:
        print(f"오류: {e}", file=sys.stderr)
        return 1
    print(f"  · 파일 {len(model.files)}개, 함수 {len(model.functions)}개, 정적 후보 {len(cands)}건"
          + (f", 분석 제한 {len(model.issues)}건" if model.issues else ""), file=sys.stderr)

    llm, pipe = None, None
    if not args.static_only:
        llm = FileLLM(args.manual) if args.manual else ClaudeLLM(args.model, args.effort, not args.no_fallback)
        opts = ST.Options(batch_size=args.batch_size, max_source_chars=args.max_source_chars,
                          resume=args.resume or bool(args.manual), stop_after=args.stop_after)
        if args.prompt_dir:
            opts.prompt_dir = Path(args.prompt_dir)
        run = ST.Run(spec, model, env, cands, tool_notes, out, llm, opts)
        try:
            pipe = ST.run_pipeline(run)
        except PendingResponse as p:
            print(f"\n프롬프트를 저장했습니다: {p.prompt_path}\n"
                  f"LLM 응답(JSON 객체)을 다음 파일로 저장한 뒤 같은 명령을 다시 실행하세요:\n  {p.response_path}",
                  file=sys.stderr)
            return 2
        except LLMError as e:
            print(f"오류: {e}", file=sys.stderr)
            print("  (완료된 단계는 결과 폴더의 stages/에 저장되어 있으며 --resume으로 이어서 실행할 수 있습니다.)",
                  file=sys.stderr)
            return 1

    result = assemble(args, model, env, cands, tool_notes, spec, pipe, llm)
    paths = R.write_reports(result, out)
    n = len(result.get("findings", []))
    print(f"완료 → {paths['html']}  (검출 결과 {n}건, 정적 후보 {len(cands)}건)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
