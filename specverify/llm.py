"""LLM 호출 계층.

- ClaudeLLM: Anthropic API (structured outputs로 단계별 JSON 스키마 강제, 적응형 사고, 스트리밍)
- FileLLM:   프롬프트를 파일로 내보내고 응답 JSON 파일을 읽는다.
             사내 승인 LLM 창구에 수동으로 붙여넣거나, 응답을 재사용(재현)할 때 쓴다.

두 구현 모두 call(label, system, blocks, schema) -> dict 형태로 같은 인터페이스를 가진다.
blocks는 Messages API의 content 블록 목록이며, 공통 문맥 블록에 cache_control을 달아
단계 2~5가 같은 사양서·코드 색인을 반복 전송할 때 프롬프트 캐시를 재사용한다.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

DEFAULT_MODEL = "claude-opus-5-5"


class LLMError(RuntimeError):
    pass


class PendingResponse(LLMError):
    """FileLLM: 응답 파일이 아직 없음 (프롬프트만 기록됨)."""

    def __init__(self, prompt_path: Path, response_path: Path):
        super().__init__(f"응답 대기: {response_path}")
        self.prompt_path = prompt_path
        self.response_path = response_path


def _parse_json(text: str, label: str) -> dict:
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else t
        t = t.rsplit("```", 1)[0]
    try:
        data = json.loads(t)
    except json.JSONDecodeError as e:
        raise LLMError(f"[{label}] 응답이 올바른 JSON이 아닙니다: {e}") from e
    if not isinstance(data, dict):
        raise LLMError(f"[{label}] 응답 최상위가 JSON 객체가 아닙니다.")
    return data


class ClaudeLLM:
    def __init__(self, model: str = DEFAULT_MODEL, effort: str = "high", fallback: bool = True,
                 max_tokens: int = 64000, client=None):
        self.model = model
        self.effort = effort
        self.fallback = fallback
        self.max_tokens = max_tokens
        self._client = client
        self.usage: list[dict] = []

    @property
    def client(self):
        if self._client is None:
            try:
                import anthropic
            except ImportError as e:
                raise LLMError("anthropic 패키지가 필요합니다 (pip install -r requirements.txt).") from e
            self._client = anthropic.Anthropic()
        return self._client

    def describe(self) -> str:
        return f"{self.model} (effort={self.effort})"

    def call(self, label: str, system: str, blocks: list[dict], schema: dict) -> dict:
        try:
            import anthropic
            api_errors = anthropic
        except ImportError:
            api_errors = None
        extra = {"betas": ["server-side-fallback-2026-07-01"], "fallbacks": "default"} if self.fallback else {}
        started = time.time()
        try:
            with self.client.beta.messages.stream(
                model=self.model,
                max_tokens=self.max_tokens,
                system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": "user", "content": blocks}],
                thinking={"type": "adaptive"},
                output_config={"effort": self.effort, "format": {"type": "json_schema", "schema": schema}},
                **extra,
            ) as stream:
                msg = stream.get_final_message()
        except Exception as e:  # SDK 오류를 사용자용 메시지로 변환
            if api_errors is not None:
                if isinstance(e, api_errors.AuthenticationError):
                    raise LLMError("인증 실패: ANTHROPIC_API_KEY(또는 ant auth login)를 확인하세요.") from e
                if isinstance(e, api_errors.BadRequestError):
                    raise LLMError(f"[{label}] 요청 오류: {e.message}") from e
                if isinstance(e, api_errors.RateLimitError):
                    raise LLMError("요청 한도 초과: 잠시 후 --resume 으로 이어서 실행하세요.") from e
                if isinstance(e, api_errors.APIStatusError):
                    raise LLMError(f"[{label}] API 오류 ({e.status_code}): {e.message}") from e
                if isinstance(e, api_errors.APIConnectionError):
                    raise LLMError("API 서버에 연결할 수 없습니다. 네트워크/프록시를 확인하세요.") from e
            raise
        u = getattr(msg, "usage", None)
        self.usage.append({
            "label": label, "model": getattr(msg, "model", self.model), "seconds": round(time.time() - started, 1),
            "input_tokens": getattr(u, "input_tokens", None), "output_tokens": getattr(u, "output_tokens", None),
            "cache_read_input_tokens": getattr(u, "cache_read_input_tokens", None),
            "cache_creation_input_tokens": getattr(u, "cache_creation_input_tokens", None),
            "stop_reason": msg.stop_reason,
        })
        if msg.stop_reason == "refusal":
            raise LLMError(f"[{label}] 모델이 요청을 거절했습니다.")
        if msg.stop_reason == "max_tokens":
            raise LLMError(f"[{label}] 출력 길이 한도에 도달해 JSON이 잘렸습니다. --batch-size를 줄여 다시 실행하세요.")
        text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
        if not text.strip():
            raise LLMError(f"[{label}] 응답에 본문이 없습니다 (stop_reason={msg.stop_reason}).")
        return _parse_json(text, label)


def render_prompt(system: str, blocks: list[dict], schema: dict) -> str:
    parts = ["# SYSTEM\n", system, "\n\n# USER\n"]
    for b in blocks:
        if b.get("type") == "text":
            parts.append(b["text"])
            parts.append("\n")
    parts.append("\n\n# 응답 형식\n아래 JSON 스키마를 만족하는 JSON 객체 하나만 출력하라 (설명·코드펜스 없이).\n```json\n")
    parts.append(json.dumps(schema, ensure_ascii=False, indent=1))
    parts.append("\n```\n")
    return "".join(parts)


class FileLLM:
    """프롬프트를 <dir>/<label>.prompt.md로 쓰고 <dir>/<label>.response.json을 읽는다."""

    def __init__(self, directory: str | Path):
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.usage: list[dict] = []

    def describe(self) -> str:
        return f"파일 교환 모드 ({self.dir})"

    def call(self, label: str, system: str, blocks: list[dict], schema: dict) -> dict:
        prompt = self.dir / f"{label}.prompt.md"
        resp = self.dir / f"{label}.response.json"
        prompt.write_text(render_prompt(system, blocks, schema), encoding="utf-8")
        if not resp.is_file():
            raise PendingResponse(prompt, resp)
        self.usage.append({"label": label, "model": "file", "source": str(resp)})
        return _parse_json(resp.read_text(encoding="utf-8"), label)
