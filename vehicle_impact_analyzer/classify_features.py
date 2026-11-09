#!/usr/bin/env python3
"""기능 분류기: C 코드의 파일·함수·변수를 차량 기능과 연결한다.

  python classify_features.py --config examples/torque_ecu/analysis.yaml --version after

산출물(<output_dir>/)
  features_<version>.json   기계 처리용(기능 ID, 요소, 역할, 근거, 검토 상태, 파일 집계, 군집)
  features_<version>.html   검토용 보고서
  graphs/<version>.db       공용 분석 그래프(변경 영향 분석기와 공유)
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from vfia.cochange import load_cochange  # noqa: E402
from vfia.config import load_config                      # noqa: E402
from vfia.feature_mapper import FeatureMapper, export_llm_prompts  # noqa: E402
from vfia.pipeline import get_graph                      # noqa: E402
from vfia.report_generator import feature_report_html, write_json  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="차량 기능 분류기 (공용 코드 분석 그래프 기반)")
    ap.add_argument("--config", required=True, help="분석 설정 파일(YAML/JSON)")
    ap.add_argument("--version", default=None, help="분석할 버전 이름(기본: 설정의 마지막 버전)")
    ap.add_argument("--rebuild", action="store_true", help="그래프 캐시를 무시하고 다시 생성")
    ap.add_argument("--out", default=None, help="출력 디렉터리(기본: 설정의 output_dir)")
    ap.add_argument("--graphml", action="store_true", help="함수·전역 수준 그래프를 GraphML로 내보내기")
    ap.add_argument("--llm-prompts", action="store_true",
                    help="기능 미연결 군집에 대한 기능명 후보 생성용 LLM 프롬프트(JSONL) 내보내기")
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args(argv)
    log = (lambda *x: None) if a.quiet else print
    cfg = load_config(a.config)
    version = a.version or list(cfg.versions)[-1]
    out = a.out or cfg.output_dir
    os.makedirs(out, exist_ok=True)
    g = get_graph(cfg, version, rebuild=a.rebuild, log=log)
    res = FeatureMapper(g, cfg.options, cfg.review_file, cfg.traces, log, load_cochange(cfg.history)).run()
    jp = os.path.join(out, f"features_{version}.json")
    hp = os.path.join(out, f"features_{version}.html")
    write_json(jp, res)
    feature_report_html(res, hp)
    log(f"기능 분류 결과: {jp}")
    log(f"검토 보고서:   {hp}")
    for f in res["features"]:
        roles = {}
        for x in f["elements"]:
            roles[x["role"]] = roles.get(x["role"], 0) + 1
        log(f"  {f['id']:<16} {f['name']}: " + ", ".join(f"{k} {v}" for k, v in roles.items()))
    if a.graphml:
        from vfia.model import NK
        gp = os.path.join(out, f"graph_{version}.graphml")
        g.export_graphml(gp, {NK.FUNCTION, NK.GLOBAL, NK.FILE, NK.TASK, NK.SIGNAL, NK.FEATURE, NK.OBSERVATION})
        log(f"GraphML: {gp}")
    if a.llm_prompts:
        lp = os.path.join(out, f"llm_prompts_{version}.jsonl")
        n = export_llm_prompts(res, lp)
        log(f"LLM 프롬프트 {n}건: {lp} (경로 존재 여부는 그래프로만 판단)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
