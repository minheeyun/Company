"""분석 설정 로딩.

설정 파일(YAML 또는 JSON) 예시는 examples/torque_ecu/analysis.yaml 참고.
경로는 설정 파일 위치 기준 상대 경로로 해석한다.
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


def load_structured(path: str) -> Any:
    with open(path, "r", encoding="utf-8") as f:
        text = f.read()
    if path.endswith((".yaml", ".yml")):
        try:
            import yaml  # type: ignore
        except ImportError as e:  # pragma: no cover
            raise RuntimeError("YAML 설정을 읽으려면 PyYAML이 필요합니다 (pip install pyyaml)") from e
        return yaml.safe_load(text) or {}
    return json.loads(text)


@dataclass
class BuildConfig:
    name: str = "default"
    extra_args: List[str] = field(default_factory=list)
    variant_filter: Optional[str] = None
    clang_resource_dir: Optional[str] = "auto"
    parse_all_comments: bool = True

    def describe(self) -> dict:
        return {"name": self.name, "extra_args": self.extra_args, "variant_filter": self.variant_filter}


@dataclass
class VersionSpec:
    name: str
    root: str
    compile_commands: Optional[str] = None
    sources: List[str] = field(default_factory=list)
    args: List[str] = field(default_factory=list)
    vehicle_model: Optional[List[str]] = None
    exclude: List[str] = field(default_factory=list)


@dataclass
class AnalysisOptions:
    loop_bound: int = 8
    inline_depth: int = 8
    sequence_length: int = 4
    solver_timeout_ms: int = 20000
    initial_state: str = "initializers"      # initializers | symbolic
    max_witnesses: int = 3
    exec_compare: str = "auto"               # auto | on | off
    exec_max_vectors: int = 2000
    ppr_alpha: float = 0.25                  # 시드 전파 재시작 확률
    feature_score_threshold: float = 0.02
    shared_feature_count: int = 2            # 이 개수 이상의 기능에 연결되면 공용 역할
    max_paths_per_feature: int = 3
    pointsto_max_iter: int = 60


@dataclass
class AnalysisConfig:
    path: str
    project: str
    build: BuildConfig
    versions: Dict[str, VersionSpec]
    vehicle_model: Optional[List[str]]
    output_dir: str
    options: AnalysisOptions
    review_file: Optional[str] = None
    traces: Dict[str, str] = field(default_factory=dict)

    @property
    def base_dir(self) -> str:
        return os.path.dirname(os.path.abspath(self.path))

    def version(self, name: str) -> VersionSpec:
        if name not in self.versions:
            raise KeyError(f"설정에 '{name}' 버전이 없습니다. 사용 가능: {list(self.versions)}")
        return self.versions[name]

    def vehicle_model_for(self, version: str) -> Optional[List[str]]:
        v = self.versions.get(version)
        if v and v.vehicle_model:
            return v.vehicle_model
        return self.vehicle_model

    def fingerprint(self) -> str:
        d = {
            "build": self.build.describe(),
            "versions": {k: vars(v) for k, v in self.versions.items()},
            "vehicle_model": self.vehicle_model,
        }
        return hashlib.sha256(json.dumps(d, sort_keys=True).encode()).hexdigest()[:16]


def _abs(base: str, p: Optional[str]) -> Optional[str]:
    if p is None:
        return None
    return p if os.path.isabs(p) else os.path.normpath(os.path.join(base, p))


def _abs_list(base: str, p) -> Optional[List[str]]:
    """문자열 또는 문자열 목록(여러 차량 모델 파일 병합)을 절대 경로 목록으로."""
    if p is None:
        return None
    if isinstance(p, str):
        p = [p]
    return [_abs(base, x) for x in p]


def load_config(path: str) -> AnalysisConfig:
    raw = load_structured(path)
    base = os.path.dirname(os.path.abspath(path))
    b = raw.get("build", {}) or {}
    build = BuildConfig(
        name=b.get("name", "default"),
        extra_args=list(b.get("extra_args", []) or []),
        variant_filter=b.get("variant_filter"),
        clang_resource_dir=b.get("clang_resource_dir", "auto"),
        parse_all_comments=b.get("parse_all_comments", True),
    )
    versions: Dict[str, VersionSpec] = {}
    for name, v in (raw.get("versions") or {}).items():
        root = _abs(base, v["root"])
        versions[name] = VersionSpec(
            name=name,
            root=root,
            compile_commands=_abs(base, v.get("compile_commands")),
            sources=list(v.get("sources", []) or []),
            args=list(v.get("args", []) or []),
            vehicle_model=_abs_list(base, v.get("vehicle_model")),
            exclude=list(v.get("exclude", []) or []),
        )
    opts = AnalysisOptions(**(raw.get("analysis") or {}))
    traces = {k: _abs(base, p) for k, p in (raw.get("traces") or {}).items()}
    return AnalysisConfig(
        path=os.path.abspath(path),
        project=raw.get("project", "project"),
        build=build,
        versions=versions,
        vehicle_model=_abs_list(base, raw.get("vehicle_model")),
        output_dir=_abs(base, raw.get("output_dir", "out")),
        options=opts,
        review_file=_abs(base, raw.get("review_file")),
        traces=traces,
    )
