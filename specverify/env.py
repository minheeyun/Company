"""분석 대상의 실행 환경 정보 (env.json).

Embedded C의 결함 판단은 자료형 크기, 태스크/ISR 구성, 동기화 방식에 따라 달라진다.
이 정보가 없으면 기본값을 가정하고, 가정한 항목은 보고서의 '분석 전제'에 그대로 드러낸다.
동시성 결함은 실행 컨텍스트 정보가 없으면 확정하지 않고 '검토 필요'로만 보고한다.

예시 (모든 항목 선택):
{
  "target": {"name": "16-bit MCU", "int_bits": 16, "long_bits": 32, "char_signed": false,
             "atomic_access_bits": 8, "pointer_bits": 16},
  "typedefs": {"U8": "unsigned char"},
  "defines": {"BLV_USE_FILTER": "1"},
  "include_dirs": ["include"],
  "contexts": [
    {"name": "OsTask_10ms", "kind": "task", "entries": ["BlvDiag_MainFunction"],
     "period_ms": 10, "priority": 5, "preemptive": true},
    {"name": "ADC_ISR", "kind": "isr", "entries": ["Adc_ConvEndIsr"], "priority": 100},
    {"name": "Init", "kind": "init", "entries": ["BlvDiag_Init"], "concurrent": false}
  ],
  "protection": {"enter": ["SuspendAllInterrupts", "SchM_Enter_*"],
                 "exit": ["ResumeAllInterrupts", "SchM_Exit_*"]},
  "notes": ["ADC 결과는 10bit (0..1023)"]
}
"""

from __future__ import annotations

import fnmatch
import json
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Target:
    name: str = "미지정"
    int_bits: int = 32
    long_bits: int = 32
    long_long_bits: int = 64
    short_bits: int = 16
    pointer_bits: int = 32
    char_signed: bool = False
    atomic_access_bits: int | None = None  # None = 미확인


@dataclass
class Context:
    name: str
    kind: str  # task | isr | init | callback | other
    entries: list[str]
    period_ms: float | None = None
    priority: int | None = None
    preemptive: bool = True
    concurrent: bool = True  # False면 다른 컨텍스트와 동시에 실행되지 않음 (예: 인터럽트 허용 전 초기화)
    notes: str = ""


@dataclass
class Environment:
    target: Target = field(default_factory=Target)
    typedefs: dict[str, str] = field(default_factory=dict)
    defines: dict[str, str] = field(default_factory=dict)
    include_dirs: list[str] = field(default_factory=list)
    contexts: list[Context] = field(default_factory=list)
    protect_enter: list[str] = field(default_factory=list)
    protect_exit: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    source: str = "(기본값)"
    provided: set[str] = field(default_factory=set)  # 사용자가 실제로 제공한 키

    # --- 판단 보조 -----------------------------------------------------------
    def is_protect_enter(self, func: str) -> bool:
        return any(fnmatch.fnmatchcase(func, p) for p in self.protect_enter)

    def is_protect_exit(self, func: str) -> bool:
        return any(fnmatch.fnmatchcase(func, p) for p in self.protect_exit)

    @property
    def has_contexts(self) -> bool:
        return bool(self.contexts)

    def assumptions(self) -> list[str]:
        """보고서에 표시할 분석 전제(가정)와 누락 정보."""
        out: list[str] = []
        t = self.target
        if "target" not in self.provided:
            out.append(f"대상 MCU/컴파일러 정보 없음 → int {t.int_bits}bit, long {t.long_bits}bit, "
                       f"char {'signed' if t.char_signed else 'unsigned'}로 가정")
        else:
            out.append(f"대상: {t.name} (int {t.int_bits}bit, long {t.long_bits}bit, "
                       f"char {'signed' if t.char_signed else 'unsigned'})")
        if t.atomic_access_bits is None:
            out.append("원자적 메모리 접근 폭 미확인 → 다중 바이트 변수의 분할 읽기(torn read) 여부는 판단하지 않음")
        if not self.contexts:
            out.append("태스크/ISR 구성 정보 없음 → 공유변수 경쟁은 확정하지 않고 '검토 필요'로만 보고")
        if not self.protect_enter:
            out.append("보호 메커니즘(인터럽트 금지·SchM 등) 함수 목록 없음 → 보호 구간 인식 불가")
        return out


def load_env(path: str | None) -> Environment:
    if not path:
        return Environment()
    p = Path(path)
    data = json.loads(p.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path}: 최상위가 JSON 객체여야 합니다.")
    env = Environment(source=str(p), provided=set(data))
    t = data.get("target") or {}
    for key in ("name", "int_bits", "long_bits", "long_long_bits", "short_bits", "pointer_bits",
                "char_signed", "atomic_access_bits"):
        if key in t:
            setattr(env.target, key, t[key])
    env.typedefs = {str(k): str(v) for k, v in (data.get("typedefs") or {}).items()}
    env.defines = {str(k): "" if v is None else str(v) for k, v in (data.get("defines") or {}).items()}
    env.include_dirs = [str(x) for x in data.get("include_dirs") or []]
    for c in data.get("contexts") or []:
        entries = c.get("entries") or ([c["entry"]] if c.get("entry") else [])
        if not c.get("name") or not entries:
            raise ValueError(f"{path}: contexts 항목에는 name과 entries가 필요합니다: {c}")
        env.contexts.append(Context(
            name=c["name"], kind=c.get("kind", "task"), entries=list(entries),
            period_ms=c.get("period_ms"), priority=c.get("priority"),
            preemptive=c.get("preemptive", True),
            concurrent=c.get("concurrent", c.get("kind") != "init"),
            notes=c.get("notes", ""),
        ))
    prot = data.get("protection") or {}
    env.protect_enter = list(prot.get("enter") or [])
    env.protect_exit = list(prot.get("exit") or [])
    notes = data.get("notes") or []
    env.notes = [notes] if isinstance(notes, str) else [str(n) for n in notes]
    return env


def describe(env: Environment) -> str:
    """LLM 프롬프트에 넣을 환경 요약."""
    t = env.target
    lines = [f"- 대상: {t.name}",
             f"- 자료형 크기: char 8bit({'signed' if t.char_signed else 'unsigned'}), short {t.short_bits}bit, "
             f"int {t.int_bits}bit, long {t.long_bits}bit, long long {t.long_long_bits}bit, pointer {t.pointer_bits}bit",
             f"- 원자적 접근 폭: {t.atomic_access_bits if t.atomic_access_bits else '미확인'}"
             + ("bit" if t.atomic_access_bits else "")]
    if env.contexts:
        lines.append("- 실행 컨텍스트:")
        for c in env.contexts:
            attrs = [c.kind]
            if c.period_ms is not None:
                attrs.append(f"주기 {c.period_ms}ms")
            if c.priority is not None:
                attrs.append(f"우선순위 {c.priority}")
            attrs.append("선점 가능" if c.preemptive else "비선점")
            if not c.concurrent:
                attrs.append("다른 컨텍스트와 동시 실행 안 됨")
            lines.append(f"  - {c.name} ({', '.join(attrs)}): 진입 함수 {', '.join(c.entries)}"
                         + (f" — {c.notes}" if c.notes else ""))
    else:
        lines.append("- 실행 컨텍스트: 제공되지 않음")
    if env.protect_enter:
        lines.append(f"- 보호 구간 함수: 진입 {', '.join(env.protect_enter)} / 해제 {', '.join(env.protect_exit) or '-'}")
    else:
        lines.append("- 보호 구간 함수: 제공되지 않음")
    if env.defines:
        lines.append("- 컴파일 정의: " + ", ".join(f"{k}={v}" for k, v in env.defines.items()))
    for n in env.notes:
        lines.append(f"- 참고: {n}")
    return "\n".join(lines)
