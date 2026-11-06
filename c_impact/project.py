"""프로젝트별 보정 설정 (cia_project.yaml).

실제 프로젝트 결과를 검증자가 대조한 뒤(calibrate.py), 그 결과를 다음 실행에 반영하는 곳이다.
모든 키는 선택이며, 없으면 기본값을 쓴다.

  review_patterns:      이름 규칙 (정규식, 대소문자 무시). 키: error_call, counter, timer, error_state, flag,
                        init_func, critical, feature_switch
  pattern_mode:         add (기본: 기본 패턴에 덧붙임) | replace (기본 패턴 대신 씀)
  call_verbs:           호출 이름 조각 → 처리 이름 (예: {Adj: 조정, Diag: 진단}) — 변경 제목·설명에 쓰임
  priority_rules:       우선순위 규칙 목록 (위에서부터 처음 맞는 규칙). 조건 키:
                          judgment: [판정...]  comparison: [신규|기존|해결|판정 보류]
                          external_output: true|false  cross_function: true|false  review_flags: true|false
                          files: [glob...]  funcs: [정규식...]
                        priority: 높음|중간|낮음, reason: 화면에 보일 근거
  suppress:             review_items: [검토 항목 번호]   risk_rules: [자동 분류 규칙 이름]
                        (발견 사항에서 빼되, 근거 상세에 '보정으로 제외'로 남긴다)
  tests:                regression / transition / external / boundary / questions : true|false (검증 항목 생성 여부)

찾는 위치: --project-config 로 준 파일 → 변경 후 폴더의 상위 폴더/cia_project.yaml → 변경 후 폴더/cia_project.yaml
"""

from __future__ import annotations

import fnmatch
import json
import re
from pathlib import Path

DEFAULT_PRIORITY_RULES = [
    {"priority": "높음", "judgment": ["실행 재현 확인", "분석 모델 내 위반 확인"], "reason": "분석 모델 내 위반 또는 실행 재현"},
    {"priority": "높음", "judgment": ["위험 후보"], "comparison": ["신규"], "reason": "변경으로 새로 생긴 위험 후보"},
    {"priority": "높음", "judgment": ["위험 후보"], "external_output": True, "reason": "위험 후보의 영향이 외부 출력까지 이어짐"},
    {"priority": "중간", "judgment": ["위험 후보"], "reason": "위험 후보"},
    {"priority": "중간", "cross_function": True, "reason": "다른 함수로 영향이 이어짐"},
    {"priority": "중간", "review_flags": True, "reason": "자동 검토 항목 지적 있음"},
    {"priority": "낮음", "reason": "변경 사실만 확인 (다른 함수 영향·지적 없음)"},
]
DEFAULT_TESTS = {"regression": True, "transition": True, "external": True, "boundary": True, "questions": True}
NAMES = ("cia_project.yaml", "cia_project.yml", "cia_project.json")


def find(new_dir: Path, explicit: str | None) -> Path | None:
    if explicit:
        p = Path(explicit)
        if not p.is_file():
            raise ValueError(f"프로젝트 설정 파일이 없습니다: {p}")
        return p
    new_dir = Path(new_dir).resolve()
    for base in (new_dir.parent, new_dir):
        for n in NAMES:
            if (base / n).is_file():
                return base / n
    return None


def load(path: Path | None) -> dict:
    if path is None:
        return {}
    text = Path(path).read_text(encoding="utf-8")
    if Path(path).suffix.lower() == ".json":
        data = json.loads(text)
    else:
        try:
            import yaml  # type: ignore
        except ImportError as e:
            raise ValueError("cia_project.yaml 을 읽으려면 PyYAML 이 필요합니다: pip install pyyaml (또는 .json 으로 저장)") from e
        data = yaml.safe_load(text) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path}: 최상위는 키-값 형식이어야 합니다")
    for k, v in (data.get("review_patterns") or {}).items():
        try:
            re.compile(v)
        except re.error as e:
            raise ValueError(f"{path}: review_patterns.{k} 정규식 오류: {e}") from e
    data["_path"] = str(path)
    return data


def merge_patterns(defaults: dict, rules_patterns: dict | None, project: dict) -> dict:
    """기본 패턴 ← security_rules.yaml 의 review_patterns ← 프로젝트 설정 (add 면 | 로 덧붙임)."""
    out = dict(defaults)
    out.update({k: v for k, v in (rules_patterns or {}).items() if v})
    mode = (project.get("pattern_mode") or "add").lower()
    for k, v in (project.get("review_patterns") or {}).items():
        if not v:
            continue
        out[k] = v if mode == "replace" or k not in out else f"(?:{out[k]})|(?:{v})"
    return out


def priority_of(rules: list[dict] | None, facts: dict) -> tuple[str, str]:
    """facts: judgment, comparison, external_output, cross_function, review_flags, file, func → (우선순위, 근거)."""
    for r in rules or DEFAULT_PRIORITY_RULES:
        if r.get("judgment") and facts["judgment"] not in r["judgment"]:
            continue
        if r.get("comparison") and facts["comparison"] not in r["comparison"]:
            continue
        ok = True
        for k in ("external_output", "cross_function", "review_flags"):
            if k in r and bool(facts.get(k)) != bool(r[k]):
                ok = False
        if r.get("files") and not any(fnmatch.fnmatch(facts.get("file", ""), g) for g in r["files"]):
            ok = False
        if r.get("funcs") and not any(re.search(g, facts.get("func", "")) for g in r["funcs"]):
            ok = False
        if ok:
            return r.get("priority", "중간"), r.get("reason", "")
    return "낮음", ""


def tests_enabled(project: dict) -> dict:
    return {**DEFAULT_TESTS, **{k: bool(v) for k, v in (project.get("tests") or {}).items()}}


def suppressed(project: dict) -> tuple[set[str], set[str]]:
    s = project.get("suppress") or {}
    return {str(x) for x in s.get("review_items") or []}, {str(x) for x in s.get("risk_rules") or []}


def to_yaml(data: dict) -> str:
    """PyYAML 없이도 읽을 수 있는 단순 YAML (문자열·숫자·불리언·목록·사전)."""
    def q(v):
        if isinstance(v, bool):
            return "true" if v else "false"
        if isinstance(v, (int, float)):
            return str(v)
        s = str(v)
        return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'

    def emit(v, ind):
        pad = "  " * ind
        out = []
        if isinstance(v, dict):
            for k, x in v.items():
                if isinstance(x, (dict, list)) and x:
                    out.append(f"{pad}{k}:")
                    out += emit(x, ind + 1)
                elif isinstance(x, (dict, list)):
                    out.append(f"{pad}{k}: {'{}' if isinstance(x, dict) else '[]'}")
                else:
                    out.append(f"{pad}{k}: {q(x)}")
        elif isinstance(v, list):
            for x in v:
                if isinstance(x, dict):
                    items = list(x.items())
                    first = True
                    for k, y in items:
                        lead = f"{pad}- " if first else f"{pad}  "
                        first = False
                        if isinstance(y, list):
                            out.append(f"{lead}{k}: [{', '.join(q(z) for z in y)}]")
                        else:
                            out.append(f"{lead}{k}: {q(y)}")
                else:
                    out.append(f"{pad}- {q(x)}")
        return out
    return "\n".join(emit(data, 0)) + "\n"
