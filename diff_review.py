#!/usr/bin/env python3
"""gitlab_diff_export.py 로 만든 HTML을 읽어 Claude로 검증 리뷰를 생성한다.

검토 기준(시스템 프롬프트)은 prompts/autosar_verifier.md 에 있으며 --prompt 로 바꿀 수 있다.
개선 설명, 결함 티켓, 사양, 참고 패턴, 테스트 결과는 옵션으로 함께 넘긴다.

결과물 (--out 디렉터리):
  review.md            모델 응답 전체
  review_record.md     1. 변경 배경 + 2. 리뷰 결과   (리뷰 기록용)
  verifier_memo.md     3. 검증자 메모               (리뷰 기록에는 넣지 않음)
  review.html          위 내용을 보기 좋게 렌더링한 페이지
"""

from __future__ import annotations

import argparse
import base64
import fnmatch
import html
import json
import re
import sys
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path

import gitlab_diff_export as gde

DEFAULT_MODEL = "claude-opus-5-5"
DEFAULT_PROMPT = Path(__file__).resolve().parent / "prompts" / "autosar_verifier.md"

# ---------------------------------------------------------------------------
# HTML → diff 복원
# ---------------------------------------------------------------------------


class _DataScriptParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self._inside = False
        self.chunks: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "script" and dict(attrs).get("id") == gde.DATA_SCRIPT_ID:
            self._inside = True

    def handle_endtag(self, tag):
        if tag == "script":
            self._inside = False

    def handle_data(self, data):
        if self._inside:
            self.chunks.append(data)


def read_diff_html(path: Path) -> dict:
    parser = _DataScriptParser()
    parser.feed(path.read_text(encoding="utf-8"))
    if not parser.chunks:
        raise ValueError(f"{path}: diff 데이터가 없습니다. 최신 gitlab_diff_export.py로 다시 추출하세요.")
    return json.loads("".join(parser.chunks))


def collect_html(inputs: list[str]) -> list[Path]:
    """디렉터리(추출 결과 폴더)나 개별 HTML 파일 목록을 받아 파일별 HTML 경로를 돌려준다."""
    paths: list[Path] = []
    for raw in inputs:
        p = Path(raw)
        if p.is_dir():
            root = p / "files" if (p / "files").is_dir() else p
            paths.extend(sorted(x for x in root.rglob("*.html") if x.name != "index.html"))
        elif p.is_file():
            paths.append(p)
        else:
            raise FileNotFoundError(raw)
    return paths


def load_items(inputs: list[str], include: list[str] | None = None) -> list[dict]:
    items = [read_diff_html(p) for p in collect_html(inputs)]
    if include:
        items = [
            it for it in items
            if any(fnmatch.fnmatch(it.get("new_path") or it.get("old_path") or "", pat) for pat in include)
        ]
    return items


# ---------------------------------------------------------------------------
# 프롬프트 구성
# ---------------------------------------------------------------------------


def format_diff(items: list[dict]) -> str:
    """파일명·hunk 헤더(함수명)·이전/이후 라인 번호가 보이는 텍스트로 변환한다."""
    out: list[str] = []
    for it in items:
        fd = gde.to_file_diff(it)
        name = f"{fd.old_path} → {fd.new_path}" if fd.status == "renamed" and fd.old_path != fd.new_path else fd.path
        out.append(f"### 파일: {name}  [{fd.status}, +{fd.additions} -{fd.deletions}]")
        if fd.note and not fd.hunks:
            out.append(f"(diff 없음: {fd.note})")
        out.append("  이전   이후")
        sign = {"add": "+", "del": "-", "ctx": " "}
        for h in fd.hunks:
            out.append(h.header)
            for l in h.lines:
                if l.kind == "meta":
                    out.append(f"{'':>6} {'':>6}   {l.text}")
                else:
                    old = "" if l.old_no is None else l.old_no
                    new = "" if l.new_no is None else l.new_no
                    out.append(f"{old:>6} {new:>6} {sign[l.kind]} {l.text}")
        out.append("")
    return "\n".join(out)


@dataclass
class Attachment:
    label: str  # 예: "관련 사양"
    name: str
    text: str | None = None
    pdf: bytes | None = None


def read_attachment(label: str, value: str) -> Attachment:
    """경로가 존재하면 파일을 읽고(.pdf는 문서 블록으로), 아니면 값 자체를 텍스트로 쓴다."""
    p = Path(value)
    if p.is_file():
        if p.suffix.lower() == ".pdf":
            return Attachment(label, p.name, pdf=p.read_bytes())
        return Attachment(label, p.name, text=p.read_text(encoding="utf-8", errors="replace"))
    return Attachment(label, "(직접 입력)", text=value)


INPUT_FIELDS = [
    ("description", "개선 설명(개발자 작성)"),
    ("ticket", "결함 티켓 번호"),
    ("spec", "관련 사양"),
    ("reference", "참고한 기존 패턴"),
    ("test_result", "테스트 결과"),
]

OUTPUT_INSTRUCTION = """위 자료로 [검토 관점]과 [규칙]에 따라 [출력 형식]대로 작성하라.
"제공되지 않음"으로 표시된 자료는 없는 것으로 보고, 필요한 판단은 "확인 필요"로 적는다.
Markdown으로 쓰고, 세 절은 각각 정확히 다음 헤딩으로 시작한다:
## 1. 변경 배경
## 2. 리뷰 결과
## 3. 검증자 메모
테스트 케이스 표는 Markdown 표(| No | 사전 조건 | 조작 | 기대 결과 | 관찰 수단 |)로 쓴다."""


def build_user_content(diff_text: str, attachments: list[Attachment]) -> list[dict]:
    blocks: list[dict] = []
    # PDF는 문서 블록으로 먼저 넣는다
    for a in attachments:
        if a.pdf is not None:
            blocks.append({
                "type": "document",
                "source": {"type": "base64", "media_type": "application/pdf",
                           "data": base64.standard_b64encode(a.pdf).decode("ascii")},
                "title": f"{a.label}: {a.name}",
            })
    parts: list[str] = ["[입력]"]
    for _, label in INPUT_FIELDS:
        mine = [a for a in attachments if a.label == label]
        if not mine:
            parts.append(f'<자료 항목="{label}">제공되지 않음</자료>')
            continue
        for a in mine:
            body = a.text if a.text is not None else f"(첨부 PDF 문서 '{a.label}: {a.name}' 참조)"
            parts.append(f'<자료 항목="{label}" 출처="{html.escape(a.name)}">\n{body}\n</자료>')
    parts.append(f"<코드_diff>\n{diff_text}\n</코드_diff>")
    parts.append(OUTPUT_INSTRUCTION)
    blocks.append({"type": "text", "text": "\n\n".join(parts)})
    return blocks


# ---------------------------------------------------------------------------
# Claude 호출
# ---------------------------------------------------------------------------


class ReviewError(RuntimeError):
    pass


def run_review(system_prompt: str, content: list[dict], *, model: str = DEFAULT_MODEL,
               effort: str = "high", fallback: bool = True, client=None) -> str:
    import anthropic

    client = client or anthropic.Anthropic()
    extra = {"betas": ["server-side-fallback-2026-07-01"], "fallbacks": "default"} if fallback else {}
    try:
        with client.beta.messages.stream(
            model=model,
            max_tokens=64000,
            system=[{"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": content}],
            thinking={"type": "adaptive"},
            output_config={"effort": effort},
            **extra,
        ) as stream:
            msg = stream.get_final_message()
    except anthropic.AuthenticationError as e:
        raise ReviewError("인증 실패: ANTHROPIC_API_KEY를 확인하세요.") from e
    except anthropic.BadRequestError as e:
        raise ReviewError(f"요청 오류: {e.message}") from e
    except anthropic.RateLimitError as e:
        raise ReviewError("요청 한도 초과: 잠시 후 다시 시도하세요.") from e
    except anthropic.APIStatusError as e:
        raise ReviewError(f"API 오류 ({e.status_code}): {e.message}") from e
    except anthropic.APIConnectionError as e:
        raise ReviewError("API 서버에 연결할 수 없습니다. 네트워크/프록시를 확인하세요.") from e

    if msg.stop_reason == "refusal":
        raise ReviewError("모델이 요청을 거절했습니다.")
    text = "".join(b.text for b in msg.content if b.type == "text").strip()
    if msg.stop_reason == "max_tokens":
        text += "\n\n> ⚠ 출력 길이 한도에 도달해 응답이 잘렸습니다."
    if not text:
        raise ReviewError(f"응답에 본문이 없습니다 (stop_reason={msg.stop_reason}).")
    return text


# ---------------------------------------------------------------------------
# 결과 저장
# ---------------------------------------------------------------------------

MEMO_HEADING = re.compile(r"^#{1,6}\s*3\s*[.)]", re.M)


def split_review(md: str) -> tuple[str, str]:
    """(리뷰 기록용 1~2절, 검증자 메모 3절)로 나눈다. 3절을 못 찾으면 전체를 기록으로 본다."""
    m = MEMO_HEADING.search(md)
    if not m:
        return md.strip(), ""
    return md[: m.start()].strip(), md[m.start():].strip()


def _inline(s: str) -> str:
    s = html.escape(s)
    s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
    s = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s)
    return s


def md_to_html(md: str) -> str:
    """리뷰 출력에 쓰이는 범위(헤딩·목록·표·인용·코드블록·문단)만 처리하는 간단한 변환기."""
    out: list[str] = []
    lines = md.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        s = line.strip()
        if not s:
            i += 1
        elif s.startswith("```"):
            buf = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith("```"):
                buf.append(lines[i]); i += 1
            i += 1
            out.append(f"<pre><code>{html.escape(chr(10).join(buf))}</code></pre>")
        elif m := re.match(r"^(#{1,6})\s+(.*)$", s):
            n = min(len(m.group(1)) + 1, 6)
            out.append(f"<h{n}>{_inline(m.group(2))}</h{n}>")
            i += 1
        elif s.startswith("|"):
            rows = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                rows.append(lines[i].strip()); i += 1
            cells = [[c.strip() for c in r.strip("|").split("|")] for r in rows]
            cells = [r for r in cells if not all(re.fullmatch(r":?-{2,}:?", c) for c in r)]
            if cells:
                head, *body = cells
                out.append("<table><thead><tr>" + "".join(f"<th>{_inline(c)}</th>" for c in head) + "</tr></thead><tbody>"
                           + "".join("<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in r) + "</tr>" for r in body)
                           + "</tbody></table>")
        elif re.match(r"^([-*]|\d+[.)])\s+", s):
            ordered = bool(re.match(r"^\d", s))
            items = []
            while i < len(lines) and re.match(r"^\s*([-*]|\d+[.)])\s+", lines[i]):
                items.append(re.sub(r"^\s*([-*]|\d+[.)])\s+", "", lines[i])); i += 1
            tag = "ol" if ordered else "ul"
            out.append(f"<{tag}>" + "".join(f"<li>{_inline(x)}</li>" for x in items) + f"</{tag}>")
        elif s.startswith(">"):
            buf = []
            while i < len(lines) and lines[i].strip().startswith(">"):
                buf.append(lines[i].strip()[1:].strip()); i += 1
            out.append(f"<blockquote>{_inline(' '.join(buf))}</blockquote>")
        else:
            buf = []
            while i < len(lines) and lines[i].strip() and not re.match(r"^\s*(#|\||[-*]\s|\d+[.)]\s|>|```)", lines[i]):
                buf.append(lines[i].strip()); i += 1
            if not buf:  # 위 분기 어디에도 안 걸린 한 줄
                buf.append(s); i += 1
            out.append(f"<p>{_inline(' '.join(buf))}</p>")
    return "\n".join(out)


REVIEW_CSS = gde.CSS + """
.wrap{max-width:1100px;margin:0 auto}
section{border:1px solid var(--border);border-radius:6px;padding:4px 20px 16px;margin-bottom:20px}
section.memo{border-style:dashed}
.tag{display:inline-block;font-size:12px;color:var(--muted);border:1px solid var(--border);border-radius:9px;padding:0 8px;margin-top:12px}
section table{border-collapse:collapse;width:100%;margin:8px 0}
section th,section td{border:1px solid var(--border);padding:6px 8px;text-align:left;vertical-align:top}
section th{background:var(--hdr)}
blockquote{margin:8px 0;padding:4px 12px;border-left:3px solid var(--border);color:var(--muted)}
pre{background:var(--hdr);padding:8px;overflow-x:auto}
code{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:90%}
"""


def render_review_html(record: str, memo: str, meta: str) -> str:
    body = (
        f'<div class="wrap"><h1>검증 리뷰</h1><p class="sub">{html.escape(meta)}</p>'
        f'<section class="record"><span class="tag">리뷰 기록</span>{md_to_html(record)}</section>'
    )
    if memo:
        body += f'<section class="memo"><span class="tag">검증자 메모 · 리뷰 기록에는 넣지 않음</span>{md_to_html(memo)}</section>'
    body += "</div>"
    return (
        '<!doctype html>\n<html lang="ko"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>검증 리뷰</title><style>{REVIEW_CSS}</style></head><body>{body}</body></html>\n"
    )


def save_outputs(md: str, out_dir: Path, meta: str) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    record, memo = split_review(md)
    (out_dir / "review.md").write_text(md + "\n", encoding="utf-8")
    (out_dir / "review_record.md").write_text(record + "\n", encoding="utf-8")
    (out_dir / "verifier_memo.md").write_text(memo + "\n", encoding="utf-8")
    page = out_dir / "review.html"
    page.write_text(render_review_html(record, memo, meta), encoding="utf-8")
    return page


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="추출된 diff HTML을 AUTOSAR 검증 관점으로 리뷰")
    ap.add_argument("inputs", nargs="+", help="gitlab_diff_export.py 출력 폴더 또는 파일별 HTML")
    ap.add_argument("--include", action="append", metavar="GLOB", help="리뷰할 파일 경로 패턴 (예: '*.c'), 여러 번 지정 가능")
    ap.add_argument("-d", "--description", action="append", default=[], help="개선 설명 (텍스트 또는 파일 경로)")
    ap.add_argument("-t", "--ticket", action="append", default=[], help="결함 티켓 번호/내용 (텍스트 또는 파일)")
    ap.add_argument("--spec", action="append", default=[], help="관련 사양 파일 (.txt/.md/.pdf 등)")
    ap.add_argument("--reference", action="append", default=[], help="참고한 기존 패턴 (파일 또는 텍스트)")
    ap.add_argument("--test-result", action="append", default=[], help="테스트 결과 (파일 또는 텍스트)")
    ap.add_argument("--prompt", default=str(DEFAULT_PROMPT), help="검토 기준 프롬프트 파일")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--effort", default="high", choices=["low", "medium", "high", "xhigh", "max"])
    ap.add_argument("--no-fallback", action="store_true", help="서버측 거절 대체 모델(fallbacks) 비활성화")
    ap.add_argument("-o", "--out", help="결과 폴더 (기본: 첫 입력 폴더/review)")
    ap.add_argument("--dry-run", action="store_true", help="API를 호출하지 않고 보낼 프롬프트만 저장")
    args = ap.parse_args(argv)

    try:
        items = load_items(args.inputs, args.include)
        if not items:
            raise ValueError("리뷰할 파일이 없습니다 (--include 패턴을 확인하세요).")
        system_prompt = Path(args.prompt).read_text(encoding="utf-8")
        attachments = [
            read_attachment(label, v)
            for key, label in INPUT_FIELDS
            for v in getattr(args, key)
        ]
    except (OSError, ValueError) as e:
        print(f"오류: {e}", file=sys.stderr)
        return 1

    diff_text = format_diff(items)
    content = build_user_content(diff_text, attachments)
    first = Path(args.inputs[0])
    out_dir = Path(args.out) if args.out else (first if first.is_dir() else first.parent) / "review"
    source = items[0].get("source") or ""
    meta = f"{source} · 파일 {len(items)}개 · 모델 {args.model}"

    if args.dry_run:
        out_dir.mkdir(parents=True, exist_ok=True)
        text = "\n\n".join(b["text"] for b in content if b["type"] == "text")
        (out_dir / "prompt_system.md").write_text(system_prompt, encoding="utf-8")
        (out_dir / "prompt_user.md").write_text(text, encoding="utf-8")
        print(f"프롬프트 저장 → {out_dir / 'prompt_system.md'}, {out_dir / 'prompt_user.md'}")
        return 0

    print(f"파일 {len(items)}개 리뷰 중 ({args.model})...", file=sys.stderr)
    try:
        md = run_review(system_prompt, content, model=args.model, effort=args.effort,
                        fallback=not args.no_fallback)
    except ImportError:
        print("오류: anthropic 패키지가 필요합니다 (pip install anthropic).", file=sys.stderr)
        return 1
    except ReviewError as e:
        print(f"오류: {e}", file=sys.stderr)
        return 1
    page = save_outputs(md, out_dir, meta)
    print(f"리뷰 완료 → {page}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
