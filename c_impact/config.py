"""분석 설정. 모든 항목은 JSON 설정 파일(--config)로 덮어쓸 수 있다."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, fields
from pathlib import Path


@dataclass
class Platform:
    """대상 MCU 자료형 환경. 값 범위·오버플로·원자성 판단에 쓰인다."""

    name: str = "generic-32bit"
    char_bits: int = 8
    short_bits: int = 16
    int_bits: int = 32
    long_bits: int = 32
    long_long_bits: int = 64
    pointer_bits: int = 32
    # 한 번의 load/store 로 원자적으로 접근 가능한 최대 비트 수
    atomic_access_bits: int = 32
    char_signed: bool = True


@dataclass
class TaskInfo:
    name: str
    kind: str = "task"  # task | isr | background
    priority: int = 0
    period_ms: float | None = None


@dataclass
class AnalysisConfig:
    # 탐색 한도
    max_depth: int = 8  # 함수 경계를 넘는 전파 횟수 한도
    max_facts: int = 20000
    max_paths: int = 200  # 함수당 CFG 경로 열거 한도
    loop_unroll: int = 1  # 경로 열거 시 반복문 본문 최대 반복 횟수
    max_call_chains: int = 20  # 진입점까지 보고할 호출 체인 수

    # 이름 기반 의미 태깅 (심볼 참조 분석 결과 위에 '역할'만 붙이는 용도)
    output_func_patterns: list[str] = field(default_factory=lambda: [
        r"^(Dio|Port|Pwm|Can|Lin|Spi|Uart|Gpio|Hal|Io)_(Write|Set|Transmit|Send|Output)",
        r"(Output|_Out$|_Write$|_Set[A-Z]?\w*$|Lamp|Relay|Buzzer|Led|Motor|Actuator)",
    ])
    output_var_patterns: list[str] = field(default_factory=lambda: [
        r"(^|_)(out|Out|OUT)(_|$|[A-Z])", r"(Output|Cmd|Command|Req|Request|Lamp|Relay|Led|Buzzer)",
    ])
    state_var_patterns: list[str] = field(default_factory=lambda: [r"(?i)(state|mode|status|phase|sts)"])
    counter_var_patterns: list[str] = field(default_factory=lambda: [
        r"(?i)(cnt|count|counter|timer|tmr|tick|debounce|delay|timeout|elapsed)"])
    fault_var_patterns: list[str] = field(default_factory=lambda: [r"(?i)(fault|fail|err|error|diag|dtc|alarm|warn)"])
    recovery_patterns: list[str] = field(default_factory=lambda: [r"(?i)(recover|reset|clear|heal|restart|safe)"])

    # 외부 라이브러리로 볼 경로 (프로젝트 루트 기준 glob)
    external_globs: list[str] = field(default_factory=lambda: ["**/external/**", "**/third_party/**", "**/vendor/**"])
    exclude_globs: list[str] = field(default_factory=lambda: ["**/.git/**", "**/build/**"])

    platform: Platform = field(default_factory=Platform)
    tasks: list[TaskInfo] = field(default_factory=list)

    # 요구사항 / 기존 테스트 매핑 (선택)
    requirements: dict = field(default_factory=dict)
    existing_tests: dict = field(default_factory=dict)

    build_config_name: str = "default"

    # ------------------------------------------------------------------
    def matches(self, kind: str, name: str) -> bool:
        pats = getattr(self, f"{kind}_patterns")
        return any(re.search(p, name or "") for p in pats)

    @classmethod
    def load(cls, path: str | Path | None) -> "AnalysisConfig":
        cfg = cls()
        if not path:
            return cfg
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        base_dir = Path(path).resolve().parent
        cfg.apply(data, base_dir)
        return cfg

    def apply(self, data: dict, base_dir: Path | None = None) -> None:
        names = {f.name for f in fields(self)}
        for k, v in data.items():
            if k == "platform":
                self.platform = Platform(**{**self.platform.__dict__, **v})
            elif k == "tasks":
                self.tasks = [TaskInfo(**t) for t in v]
            elif k in ("requirements", "existing_tests") and isinstance(v, str):
                p = Path(v)
                if base_dir and not p.is_absolute():
                    p = base_dir / p
                setattr(self, k, json.loads(p.read_text(encoding="utf-8")))
            elif k in names:
                setattr(self, k, v)
            elif not k.startswith("_"):
                raise ValueError(f"알 수 없는 설정 항목: {k}")

    def to_dict(self) -> dict:
        d = {f.name: getattr(self, f.name) for f in fields(self)}
        d["platform"] = dict(self.platform.__dict__)
        d["tasks"] = [dict(t.__dict__) for t in self.tasks]
        d["requirements"] = f"{len(self.requirements)} items"
        d["existing_tests"] = f"{len(self.existing_tests)} items"
        return d
