"""두 스크립트가 공유하는 그래프 생성/캐시 파이프라인.

  get_graph(config, 'before') -> AnalysisGraph (SQLite 캐시: <output_dir>/graphs/<version>.db)

입력 지문(소스·헤더 해시, compile DB, 빌드 설정, 차량 모델 파일)이 같으면 저장된
그래프를 재사용한다. 번역 단위 파싱은 project_indexer의 TU 캐시로 증분 처리한다
(헤더가 바뀌면 그 헤더를 포함한 번역 단위만 다시 파싱). points-to·함수 요약 등
전역 분석은 정확성을 위해 매번 전체를 다시 계산한다.
"""
from __future__ import annotations

import glob
import hashlib
import json
import os
import time
from . import __version__
from .config import AnalysisConfig
from .graph_builder import build_graph
from .model import AnalysisGraph
from .project_indexer import ProjectIndexer, file_hash
from .vehicle_context import apply_vehicle_model, load_vehicle_model


def _input_fingerprint(cfg: AnalysisConfig, version: str) -> str:
    v = cfg.version(version)
    h = hashlib.sha256()
    h.update(__version__.encode())
    # 분석기 코드가 바뀌면 그래프를 다시 만든다
    for f in sorted(glob.glob(os.path.join(os.path.dirname(__file__), "*.py"))):
        h.update((file_hash(f) or "").encode())
    h.update(json.dumps(cfg.build.describe(), sort_keys=True).encode())
    h.update(json.dumps(vars(cfg.options), sort_keys=True).encode())
    h.update(json.dumps({"sources": v.sources, "args": v.args, "exclude": v.exclude}, sort_keys=True).encode())
    for pat in ("**/*.c", "**/*.h"):
        for f in sorted(glob.glob(os.path.join(v.root, pat), recursive=True)):
            h.update(os.path.relpath(f, v.root).encode())
            h.update((file_hash(f) or "").encode())
    paths = []
    if v.compile_commands:
        paths.append(v.compile_commands)
    vm = cfg.vehicle_model_for(version)
    if vm:
        paths.extend(vm if isinstance(vm, list) else [vm])
    for p in paths:
        h.update(p.encode())
        h.update((file_hash(p) or "missing").encode())
    # 차량 모델에서 참조한 ARXML도 지문에 포함
    for p in glob.glob(os.path.join(os.path.dirname(cfg.path), "**/*.arxml"), recursive=True):
        h.update((file_hash(p) or "").encode())
    return h.hexdigest()[:24]


def get_graph(cfg: AnalysisConfig, version: str, rebuild: bool = False, log=print) -> AnalysisGraph:
    os.makedirs(os.path.join(cfg.output_dir, "graphs"), exist_ok=True)
    dbpath = os.path.join(cfg.output_dir, "graphs", f"{version}.db")
    fp = _input_fingerprint(cfg, version)
    if not rebuild and os.path.isfile(dbpath):
        try:
            g = AnalysisGraph.load(dbpath)
            if g.meta.get("input_fingerprint") == fp:
                log(f"[{version}] 저장된 분석 그래프 재사용: {dbpath}")
                return g
        except Exception as e:  # pragma: no cover
            log(f"[{version}] 그래프 캐시 로딩 실패({e}), 다시 생성")
    t0 = time.time()
    v = cfg.version(version)
    log(f"[{version}] 인덱싱: {v.root} (빌드 설정: {cfg.build.name})")
    idx = ProjectIndexer(v, cfg.build, cache_dir=os.path.join(cfg.output_dir, "cache", version), log=log).run()
    s = idx.summary()
    log(f"[{version}] 번역 단위 {s['translation_units']}개, 심벌 {s['units']}, 파싱 실패 {len(s['parse_failed_tus'])}개")
    g = build_graph(idx, cfg.options, log)
    vm = load_vehicle_model(cfg.vehicle_model_for(version))
    notes = apply_vehicle_model(g, vm)
    g.meta["input_fingerprint"] = fp
    g.meta["config_path"] = cfg.path
    g.meta["project"] = cfg.project
    g.meta["build_seconds"] = round(time.time() - t0, 2)
    for n in notes:
        g.diag("info", "vehicle_model", n)
    g.save(dbpath)
    log(f"[{version}] 그래프 저장: {dbpath} ({g.g.number_of_nodes()} 노드, {g.g.number_of_edges()} 간선, "
        f"{g.meta['build_seconds']}s)")
    return g
