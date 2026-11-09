#!/usr/bin/env python3
"""변경 영향 분석기: 변경 전후 코드를 비교하고, 변경점에서 어떤 데이터·실행 경로를 거쳐
어떤 차량 기능까지 영향이 전달되는지, 어떤 조건에서 동작이 어떻게 달라지는지 분석한다.

  python analyze_impact.py --config examples/torque_ecu/analysis.yaml

산출물(<output_dir>/)
  impact.json   기계 처리용 전체 결과(변경점, 영향 경로, 기능별 상태, 동작 검증, 검토 항목)
  impact.html   검토용 보고서(무엇이 변경 → 무엇을 확인 → 어떤 영향 → 무엇을 추가 확인)
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from vfia.behavior_checker import BehaviorChecker          # noqa: E402
from vfia.change_detector import detect_changes             # noqa: E402
from vfia.cochange import load_cochange  # noqa: E402
from vfia.config import load_config                         # noqa: E402
from vfia.feature_mapper import FeatureMapper               # noqa: E402
from vfia.impact_analyzer import ImpactAnalyzer, feature_summary  # noqa: E402
from vfia.model import DEPENDENCE_EDGES, AnalysisGraph  # noqa: E402
from vfia.pipeline import get_graph                         # noqa: E402
from vfia.report_generator import impact_report_html, review_items, write_json  # noqa: E402


def _scope(go: AnalysisGraph, gn: AnalysisGraph) -> dict:
    inactive = {}
    for side, g in (("변경 후", gn),):
        for f, info in (g.meta.get("files") or {}).items():
            if info.get("inactive"):
                inactive[f] = {"macros": info.get("cond_macros", []), "ranges": info.get("inactive")}
    unresolved = {}
    for side, g in (("변경 전", go), ("변경 후", gn)):
        s = g.meta.get("summary", {})
        unresolved.setdefault("본문 없는 외부 함수(보수적 요약)", [])
        unresolved["본문 없는 외부 함수(보수적 요약)"] = sorted(set(unresolved["본문 없는 외부 함수(보수적 요약)"]) |
                                                         {x.split(":")[-1] for x in s.get("external_functions", [])})
        unresolved.setdefault("간접 호출 미해결", [])
        unresolved["간접 호출 미해결"] += [f"{side} {d.get('file')}:{d.get('line')}" for d in g.diagnostics
                                         if d.get("code") == "unresolved_indirect_call"]
        unresolved.setdefault("파싱 오류 번역 단위", [])
        unresolved["파싱 오류 번역 단위"] += [f"{side} {t}" for t in s.get("parse_failed_tus", [])]
        unresolved.setdefault("헤더 전개 차이·중복 정의", [])
        unresolved["헤더 전개 차이·중복 정의"] += [f"{side} {d['message']}" for d in g.diagnostics
                                             if d.get("code") in ("header_variant_conflict", "multiple_definition",
                                                                  "macro_context_variant")]
        unresolved.setdefault("정의를 찾지 못한 전역 변수", [])
        unresolved["정의를 찾지 못한 전역 변수"] += [f"{side} {x}" for x in s.get("unresolved_globals", [])]
        unresolved.setdefault("부분 해석(포인터·식·어셈블리)", [])
        unresolved["부분 해석(포인터·식·어셈블리)"] += sorted({f"{side} {d.get('file')}:{d.get('line')} {d['message']}"
                                                         for d in g.diagnostics if d.get("code") == "incomplete"})
        unresolved.setdefault("차량 모델 연결 메모", [])
        unresolved["차량 모델 연결 메모"] += [f"{side} {n}" for n in (g.meta.get("vehicle_model") or {}).get("notes", [])]
        for n in s.get("notes", []):
            unresolved.setdefault("빌드 입력 메모", []).append(f"{side} {n}")
    pts = gn.meta.get("pointsto", {})
    method = {
        "그래프": "변경 전/후 그래프를 각각 생성·분석(간선 비병합), 노드 대응표로 결과만 연결",
        "AST 변경 분석": "GumTree 계열(top-down 동형 매칭 + bottom-up dice/내용 유사도 + LCS 복구)",
        "함수 내부": "CFG, 후지배자 기반 제어 의존성, worklist Reaching Definitions(Def-Use)",
        "함수 간": "SDG(actual/formal in·out, 호출 정점), HRB 요약 간선, 2단계 슬라이싱(호출 문맥 비혼합)",
        "포인터": f"Andersen points-to(흐름·문맥 비민감, 필드 구분) + 포인터 매개변수 부작용의 호출 지점별 해석 "
                  f"(제약 {pts.get('constraints')}개, 반복 {pts.get('iterations')}회, 수렴 {pts.get('converged')})",
        "공유 상태": "전역/정적 변수 위치 노드(쓰기→위치→읽기), 태스크·ISR 문맥과 연결",
        "슬라이스 간선": ", ".join(sorted(DEPENDENCE_EDGES)),
        "기능 영향 판정": "변경점 전방 슬라이스 ∩ 기능 관찰 대상 후방 슬라이스(양 버전 각각)",
    }
    return {"method": method, "inactive_regions": inactive,
            "unresolved": {k: sorted(set(v)) if isinstance(v, list) else v for k, v in unresolved.items()}}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="차량 기능 변경 영향 분석기 (공용 코드 분석 그래프 기반)")
    ap.add_argument("--config", required=True)
    ap.add_argument("--before", default="before", help="변경 전 버전 이름")
    ap.add_argument("--after", default="after", help="변경 후 버전 이름")
    ap.add_argument("--rebuild", action="store_true", help="그래프 캐시 무시")
    ap.add_argument("--no-behavior", action="store_true", help="동작 차이 검증(기호 실행·실행 비교) 생략")
    ap.add_argument("--exec", dest="exec_mode", default=None, choices=["auto", "on", "off"],
                    help="호스트 컴파일러로 전후 비교 실행(기본: 설정값)")
    ap.add_argument("--out", default=None)
    ap.add_argument("--level", default="full", choices=["full", "function"],
                    help="function: 2.5단계 MVP(호출 그래프+전역 읽기/쓰기+함수 요약)만 빠르게 실행")
    ap.add_argument("--strict", action="store_true",
                    help="파싱 성공률이 기준(analysis.min_parse_success_rate) 미만이면 종료 코드 2")
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args(argv)
    log = (lambda *x: None) if a.quiet else print
    cfg = load_config(a.config)
    out = a.out or cfg.output_dir
    os.makedirs(out, exist_ok=True)
    t0 = time.time()
    go = get_graph(cfg, a.before, rebuild=a.rebuild, log=log)
    gn = get_graph(cfg, a.after, rebuild=a.rebuild, log=log)

    rate_ok = True
    for name, g in ((a.before, go), (a.after, gn)):
        ph = (g.meta.get("summary") or {}).get("parse_health") or {}
        rate = ph.get("success_rate", 1.0)
        miss = len(ph.get("missing_function_candidates") or [])
        part = len(ph.get("partial_ast_functions") or [])
        ok = rate >= cfg.options.min_parse_success_rate and not miss
        rate_ok &= ok
        log(f"[{name}] 파싱 성공률 {rate:.0%}(기준 {cfg.options.min_parse_success_rate:.0%}), 누락 의심 함수 {miss}개, "
            f"부분 AST 함수 {part}개 -> {'단계 2 완료 기준 충족' if ok else '기준 미달: 빌드 환경 재현 확인 필요'}")
    log("변경점 추출(AST 대응)...")
    cres, cd = detect_changes(go, gn)
    log(f"  변경점 {len(cres['changes'])}개")
    from vfia.mvp_impact import mvp_analyze
    mvp = mvp_analyze(go, gn, cres["changes"])
    if a.level == "function":
        jp = os.path.join(out, "impact_mvp.json")
        write_json(jp, {"kind": "change_impact_mvp", "changes": cres["changes"], "mvp": mvp,
                        "method": "호출 그래프 + 전역 읽기/쓰기 + 함수 요약(2.5단계 MVP)"})
        log(f"\n함수 단위 간이 영향 분석: {jp}")
        for m in mvp:
            log(f"  {m['change']} {m['unit']}: 영향 함수 {len(m['functions'])}개, 기능 {', '.join(m['features']) or '-'}")
        return 0 if (rate_ok or not a.strict) else 2
    log("기능 분류(양 버전, 같은 그래프 사용)...")
    cochange = load_cochange(cfg.history)
    fmaps = {"old": FeatureMapper(go, cfg.options, cfg.review_file, cfg.traces, log, cochange).run(),
             "new": FeatureMapper(gn, cfg.options, cfg.review_file, cfg.traces, log, cochange).run()}
    log("영향 추적(전방 ∩ 후방 슬라이스)...")
    results = ImpactAnalyzer(go, gn, cd, cres["changes"], fmaps, cfg.options.max_paths_per_feature).run()
    behavior = []
    if not a.no_behavior:
        log("동작 차이 검증(기호 실행, 전후 비교 실행)...")
        bc = BehaviorChecker(go, gn, cd, cfg.options, a.exec_mode or cfg.options.exec_compare,
                             workdir=os.path.join(out, "exec"), log=log)
        behavior = bc.run(results)
    feats = feature_summary(results)
    scope = _scope(go, gn)
    meta = {
        "project": cfg.project, "generated": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "config": cfg.path, "build": gn.meta.get("build"),
        "versions": {a.before: {"root": go.meta.get("root"), "summary": go.meta.get("summary"),
                                "fingerprint": go.meta.get("input_fingerprint")},
                     a.after: {"root": gn.meta.get("root"), "summary": gn.meta.get("summary"),
                               "fingerprint": gn.meta.get("input_fingerprint")}},
        "vehicle_model": {a.before: go.meta.get("vehicle_model"), a.after: gn.meta.get("vehicle_model")},
        "seconds": None,
    }
    from vfia.assumptions import analysis_assumptions
    from vfia.intervals import interval_report
    data = {"kind": "change_impact", "meta": meta, "changes": results, "features": feats, "behavior": behavior,
            "scope": scope, "change_notes": cres.get("notes"), "file_moves": cres.get("file_moves"),
            "text_coverage": cres.get("text_coverage"), "resources": cres.get("resources"), "mvp": mvp,
            "assumptions": analysis_assumptions(go, gn, cfg.options),
            "static_branches": interval_report(gn)["static_branches"],
            "parse_health": {a.before: (go.meta.get("summary") or {}).get("parse_health"),
                             a.after: (gn.meta.get("summary") or {}).get("parse_health")},
            "symbol_integration": {a.before: (go.meta.get("summary") or {}).get("symbol_integration"),
                                   a.after: (gn.meta.get("summary") or {}).get("symbol_integration")},
            "interference": {a.before: go.meta.get("interference"), a.after: gn.meta.get("interference")},
            "external_models": gn.meta.get("external_models"),
            "cochange": None}
    if cochange:
        data["cochange"] = {"summary": cochange.summary(), "per_change": {
            c["id"]: cochange.rules_from([x for x in {(c.get("old") or {}).get("file"), (c.get("new") or {}).get("file")} if x])
            for c in results}}
    data["review"] = review_items(results, behavior, meta, scope)
    meta["seconds"] = round(time.time() - t0, 2)
    jp, hp = os.path.join(out, "impact.json"), os.path.join(out, "impact.html")
    write_json(jp, data)
    impact_report_html(data, hp)
    log(f"\n영향 분석 결과: {jp}\n검토 보고서:   {hp}  ({meta['seconds']}s)")
    if a.strict and not rate_ok:
        log("파싱 기준 미달(--strict): 종료 코드 2")
        return 2
    log("\n기능별 판정:")
    for f in feats:
        log(f"  {f['feature']:<16} {f['overall']:<28} 관련 변경 {len(f['changes'])}개")
    nofeat = [c["id"] for c in results if not c["impact"]["features"]]
    if nofeat:
        log(f"  (기능 관찰 대상까지 경로 미발견: {', '.join(nofeat)})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
