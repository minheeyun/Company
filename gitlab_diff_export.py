#!/usr/bin/env python3
"""GitLab의 Changes(diffs) 항목을 파일별 HTML로 추출한다.

대상:
  * Merge Request   : --mr-url 또는 --project + --mr
  * 단일 커밋        : --project + --commit
  * 두 ref 비교      : --project + --compare FROM..TO
  * 오프라인 JSON    : --input (GitLab API 응답을 저장한 파일)

결과물:
  OUT_DIR/index.html            변경 파일 목록과 통계
  OUT_DIR/files/<경로>.html     파일 하나당 HTML 하나 (CSS 내장, 단독으로 열람 가능)

표준 라이브러리만 사용한다.
"""

from __future__ import annotations

import argparse
import difflib
import html
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

# ---------------------------------------------------------------------------
# GitLab API
# ---------------------------------------------------------------------------


class GitLabError(RuntimeError):
    pass


class GitLabClient:
    def __init__(self, base_url: str, token: str | None = None, timeout: int = 60):
        self.api = base_url.rstrip("/") + "/api/v4"
        self.token = token
        self.timeout = timeout

    def _get(self, path: str, params: dict | None = None):
        query = urllib.parse.urlencode(params or {})
        url = f"{self.api}{path}" + (f"?{query}" if query else "")
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        if self.token:
            req.add_header("PRIVATE-TOKEN", self.token)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.load(resp), resp.headers
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")[:500]
            raise GitLabError(f"GET {url} -> HTTP {e.code}: {body}") from e

    def _get_paged(self, path: str, params: dict | None = None) -> list:
        items: list = []
        page = "1"
        while page:
            data, headers = self._get(path, {**(params or {}), "per_page": 100, "page": page})
            items.extend(data)
            page = headers.get("X-Next-Page", "")
        return items

    @staticmethod
    def _project(project: str) -> str:
        return urllib.parse.quote(str(project), safe="")

    def mr_diffs(self, project: str, iid: int) -> list[dict]:
        p = self._project(project)
        try:
            # GitLab 15.7+
            return self._get_paged(f"/projects/{p}/merge_requests/{iid}/diffs")
        except GitLabError as e:
            if "HTTP 404" not in str(e):
                raise
            # 구버전 GitLab: deprecated /changes 엔드포인트로 대체
            data, _ = self._get(
                f"/projects/{p}/merge_requests/{iid}/changes", {"access_raw_diffs": "true"}
            )
            return data.get("changes", [])

    def commit_diffs(self, project: str, sha: str) -> list[dict]:
        p = self._project(project)
        return self._get_paged(f"/projects/{p}/repository/commits/{urllib.parse.quote(sha, safe='')}/diff")

    def compare_diffs(self, project: str, ref_from: str, ref_to: str) -> list[dict]:
        p = self._project(project)
        data, _ = self._get(f"/projects/{p}/repository/compare", {"from": ref_from, "to": ref_to})
        return data.get("diffs", [])


def parse_mr_url(url: str) -> tuple[str, str, int]:
    """https://gitlab.example.com/group/sub/proj/-/merge_requests/12 -> (base, project, iid)"""
    parsed = urllib.parse.urlparse(url)
    m = re.match(r"^/(?P<project>.+?)/-/merge_requests/(?P<iid>\d+)", parsed.path)
    if not parsed.scheme or not parsed.netloc or not m:
        raise ValueError(f"MR URL 형식이 아닙니다: {url}")
    return f"{parsed.scheme}://{parsed.netloc}", m.group("project"), int(m.group("iid"))


# ---------------------------------------------------------------------------
# Diff 파싱
# ---------------------------------------------------------------------------

HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$")


@dataclass
class Line:
    kind: str  # "ctx" | "add" | "del" | "meta"
    text: str
    old_no: int | None = None
    new_no: int | None = None
    inline: list[tuple[str, bool]] | None = None  # (조각, 강조여부)


@dataclass
class Hunk:
    header: str
    lines: list[Line] = field(default_factory=list)


@dataclass
class FileDiff:
    old_path: str
    new_path: str
    status: str  # added | deleted | renamed | modified
    hunks: list[Hunk]
    a_mode: str | None = None
    b_mode: str | None = None
    note: str | None = None  # 바이너리/너무 큼 등
    raw: str = ""  # GitLab이 준 원본 diff 텍스트

    @property
    def path(self) -> str:
        return self.new_path or self.old_path

    @property
    def additions(self) -> int:
        return sum(l.kind == "add" for h in self.hunks for l in h.lines)

    @property
    def deletions(self) -> int:
        return sum(l.kind == "del" for h in self.hunks for l in h.lines)


def parse_hunks(diff: str) -> list[Hunk]:
    hunks: list[Hunk] = []
    cur: Hunk | None = None
    old_no = new_no = 0
    lines = diff.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    for raw in lines:
        m = HUNK_RE.match(raw)
        if m:
            old_no, new_no = int(m.group(1)), int(m.group(3))
            cur = Hunk(header=raw)
            hunks.append(cur)
            continue
        if cur is None:
            # "diff --git", "---", "+++" 같은 헤더 (unidiff=true 일 때 등) 무시
            continue
        tag, text = raw[:1], raw[1:]
        if tag == "+":
            cur.lines.append(Line("add", text, None, new_no))
            new_no += 1
        elif tag == "-":
            cur.lines.append(Line("del", text, old_no, None))
            old_no += 1
        elif tag == "\\":
            cur.lines.append(Line("meta", raw))
        else:  # " " 또는 빈 줄
            cur.lines.append(Line("ctx", text, old_no, new_no))
            old_no += 1
            new_no += 1
    for h in hunks:
        _mark_inline_changes(h.lines)
    return hunks


def _mark_inline_changes(lines: list[Line]) -> None:
    """연속된 삭제/추가 블록을 짝지어 줄 안에서 바뀐 부분을 표시한다."""
    i = 0
    while i < len(lines):
        if lines[i].kind != "del":
            i += 1
            continue
        j = i
        while j < len(lines) and lines[j].kind == "del":
            j += 1
        k = j
        while k < len(lines) and lines[k].kind == "add":
            k += 1
        for d, a in zip(lines[i:j], lines[j:k]):
            sm = difflib.SequenceMatcher(None, d.text, a.text, autojunk=False)
            if sm.ratio() < 0.4:
                continue
            d.inline, a.inline = [], []
            for op, i1, i2, j1, j2 in sm.get_opcodes():
                if i2 > i1:
                    d.inline.append((d.text[i1:i2], op != "equal"))
                if j2 > j1:
                    a.inline.append((a.text[j1:j2], op != "equal"))
        i = k


def to_file_diff(item: dict) -> FileDiff:
    if item.get("new_file"):
        status = "added"
    elif item.get("deleted_file"):
        status = "deleted"
    elif item.get("renamed_file"):
        status = "renamed"
    else:
        status = "modified"
    diff = item.get("diff") or ""
    note = None
    if item.get("too_large"):
        note = "diff가 너무 커서 GitLab이 내용을 제공하지 않았습니다."
    elif item.get("collapsed") and not diff:
        note = "GitLab에서 접힌(collapsed) diff라 내용이 제공되지 않았습니다."
    elif diff.startswith("Binary files") or (not diff and status != "renamed"):
        note = "바이너리 파일이거나 표시할 내용이 없습니다."
    a_mode, b_mode = item.get("a_mode"), item.get("b_mode")
    return FileDiff(
        old_path=item.get("old_path") or "",
        new_path=item.get("new_path") or "",
        status=status,
        hunks=parse_hunks(diff),
        a_mode=a_mode,
        b_mode=b_mode,
        note=note,
        raw=diff,
    )


# ---------------------------------------------------------------------------
# HTML 렌더링
# ---------------------------------------------------------------------------

CSS = """
:root{--bg:#fff;--fg:#1f2328;--muted:#656d76;--border:#d0d7de;--hdr:#f6f8fa;
--add-bg:#e6ffec;--add-no:#ccffd8;--add-hl:#abf2bc;--del-bg:#ffebe9;--del-no:#ffd7d5;--del-hl:#ff818266;
--hunk-bg:#ddf4ff;--link:#0969da}
@media (prefers-color-scheme:dark){:root{--bg:#0d1117;--fg:#e6edf3;--muted:#8d96a0;--border:#30363d;--hdr:#161b22;
--add-bg:#12261e;--add-no:#1b4721;--add-hl:#2ea04366;--del-bg:#25171c;--del-no:#542426;--del-hl:#f8514966;
--hunk-bg:#121d2f;--link:#4493f8}}
*{box-sizing:border-box}
body{margin:0;padding:16px;background:var(--bg);color:var(--fg);
font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI","Noto Sans KR",Helvetica,Arial,sans-serif}
a{color:var(--link);text-decoration:none}a:hover{text-decoration:underline}
h1{font-size:20px;margin:0 0 4px}.sub{color:var(--muted);margin:0 0 16px}
.file{border:1px solid var(--border);border-radius:6px;overflow:hidden}
.fhdr{background:var(--hdr);border-bottom:1px solid var(--border);padding:8px 12px;
font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:13px;word-break:break-all}
.badge{display:inline-block;font:600 11px/18px sans-serif;padding:0 6px;border-radius:9px;margin-right:6px;
color:#fff;vertical-align:middle;text-transform:uppercase}
.b-added{background:#1a7f37}.b-deleted{background:#cf222e}.b-renamed{background:#8250df}.b-modified{background:#9a6700}
.stat-a{color:#1a7f37}.stat-d{color:#cf222e}
.scroll{overflow-x:auto}
table.diff{border-collapse:collapse;width:100%;font:12px/20px ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
table.diff td{padding:0 8px;vertical-align:top;white-space:pre}
td.no{width:1%;min-width:50px;text-align:right;color:var(--muted);user-select:none}
td.sign{width:1%;user-select:none;padding:0 2px 0 4px}
tr.add td{background:var(--add-bg)}tr.add td.no{background:var(--add-no)}
tr.del td{background:var(--del-bg)}tr.del td.no{background:var(--del-no)}
td.add{background:var(--add-bg)}td.no.add{background:var(--add-no)}
td.del{background:var(--del-bg)}td.no.del{background:var(--del-no)}
td.empty{background:var(--hdr)}
tr.add .hl,td.add .hl{background:var(--add-hl);border-radius:2px}
tr.del .hl,td.del .hl{background:var(--del-hl);border-radius:2px}
tr.hunk td{background:var(--hunk-bg);color:var(--muted)}
tr.meta td{color:var(--muted);font-style:italic}
.note{padding:16px;color:var(--muted)}
.nav{margin-bottom:12px}
table.idx{border-collapse:collapse;width:100%}
table.idx th,table.idx td{border-bottom:1px solid var(--border);padding:6px 8px;text-align:left}
table.idx .n{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
table.idx td.p{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:13px;word-break:break-all}
"""

E = html.escape


def _code(line: Line) -> str:
    if line.inline:
        return "".join(f'<span class="hl">{E(t)}</span>' if hl else E(t) for t, hl in line.inline)
    return E(line.text)


def _num(n: int | None) -> str:
    return "" if n is None else str(n)


def render_unified(fd: FileDiff) -> str:
    rows = []
    sign = {"add": "+", "del": "-", "ctx": " "}
    for h in fd.hunks:
        rows.append(f'<tr class="hunk"><td class="no">…</td><td class="no">…</td><td colspan="2">{E(h.header)}</td></tr>')
        for l in h.lines:
            if l.kind == "meta":
                rows.append(f'<tr class="meta"><td class="no"></td><td class="no"></td><td colspan="2">{E(l.text)}</td></tr>')
                continue
            rows.append(
                f'<tr class="{l.kind}"><td class="no">{_num(l.old_no)}</td><td class="no">{_num(l.new_no)}</td>'
                f'<td class="sign">{sign[l.kind]}</td><td>{_code(l)}</td></tr>'
            )
    return f'<table class="diff">{"".join(rows)}</table>'


def _side_by_side_rows(h: Hunk) -> Iterable[tuple[Line | None, Line | None]]:
    lines = h.lines
    i = 0
    while i < len(lines):
        l = lines[i]
        if l.kind in ("ctx", "meta"):
            yield l, l
            i += 1
            continue
        dels, adds = [], []
        while i < len(lines) and lines[i].kind == "del":
            dels.append(lines[i]); i += 1
        while i < len(lines) and lines[i].kind == "add":
            adds.append(lines[i]); i += 1
        for n in range(max(len(dels), len(adds))):
            yield (dels[n] if n < len(dels) else None), (adds[n] if n < len(adds) else None)


def render_side_by_side(fd: FileDiff) -> str:
    def cells(l: Line | None, side: str) -> str:
        if l is None:
            return '<td class="no empty"></td><td class="empty"></td>'
        if l.kind == "meta":
            return f'<td class="no"></td><td class="meta">{E(l.text)}</td>'
        no = l.old_no if side == "old" else l.new_no
        cls = l.kind if l.kind != "ctx" else ""
        return f'<td class="no {cls}">{_num(no)}</td><td class="{cls}">{_code(l)}</td>'

    rows = []
    for h in fd.hunks:
        rows.append(f'<tr class="hunk"><td class="no">…</td><td colspan="3">{E(h.header)}</td></tr>')
        for left, right in _side_by_side_rows(h):
            rows.append(f"<tr>{cells(left, 'old')}{cells(right, 'new')}</tr>")
    return f'<table class="diff sbs">{"".join(rows)}</table>'


def _title_html(fd: FileDiff) -> str:
    if fd.status == "renamed" and fd.old_path != fd.new_path:
        name = f"{E(fd.old_path)} → {E(fd.new_path)}"
    else:
        name = E(fd.path)
    mode = ""
    if fd.a_mode and fd.b_mode and fd.a_mode != fd.b_mode and fd.status == "modified":
        mode = f' <span class="sub">(mode {E(fd.a_mode)} → {E(fd.b_mode)})</span>'
    return (
        f'<span class="badge b-{fd.status}">{fd.status}</span>{name}{mode} '
        f'<span class="stat-a">+{fd.additions}</span> <span class="stat-d">−{fd.deletions}</span>'
    )


def _page(title: str, body: str) -> str:
    return (
        "<!doctype html>\n<html lang=\"ko\"><head><meta charset=\"utf-8\">"
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{E(title)}</title><style>{CSS}</style></head><body>{body}</body></html>\n"
    )


def render_file_page(fd: FileDiff, view: str, source: str, index_href: str) -> str:
    if fd.note and not fd.hunks:
        content = f'<div class="note">{E(fd.note)}</div>'
    else:
        table = render_side_by_side(fd) if view == "side-by-side" else render_unified(fd)
        content = f'<div class="scroll">{table}</div>'
    body = (
        f'<div class="nav"><a href="{E(index_href)}">← 전체 목록</a> · <span class="sub">{E(source)}</span></div>'
        f'<div class="file"><div class="fhdr">{_title_html(fd)}</div>{content}</div>'
    )
    return _page(fd.path, body + _data_script(fd, source))


DATA_SCRIPT_ID = "gitlab-diff-data"


def _data_script(fd: FileDiff, source: str) -> str:
    """리뷰 도구 등이 HTML을 다시 읽을 수 있도록 원본 diff를 JSON으로 함께 넣는다."""
    data = {
        "source": source,
        "old_path": fd.old_path,
        "new_path": fd.new_path,
        "status": fd.status,
        "a_mode": fd.a_mode,
        "b_mode": fd.b_mode,
        "note": fd.note,
        "diff": fd.raw,
    }
    payload = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    return f'<script type="application/json" id="{DATA_SCRIPT_ID}">{payload}</script>'


def render_index(files: list[tuple[FileDiff, str]], source: str) -> str:
    total_a = sum(fd.additions for fd, _ in files)
    total_d = sum(fd.deletions for fd, _ in files)
    rows = "".join(
        f'<tr><td><span class="badge b-{fd.status}">{fd.status}</span></td>'
        f'<td class="p"><a href="{E(href)}">{E(fd.path)}</a>'
        + (f'<br><span class="sub">← {E(fd.old_path)}</span>' if fd.status == "renamed" and fd.old_path != fd.new_path else "")
        + f'</td><td class="n stat-a">+{fd.additions}</td><td class="n stat-d">−{fd.deletions}</td></tr>'
        for fd, href in files
    )
    body = (
        f"<h1>Changes</h1><p class=\"sub\">{E(source)} · 파일 {len(files)}개 · "
        f'<span class="stat-a">+{total_a}</span> <span class="stat-d">−{total_d}</span></p>'
        f'<table class="idx"><thead><tr><th>상태</th><th>파일</th><th class="n">추가</th><th class="n">삭제</th></tr></thead>'
        f"<tbody>{rows}</tbody></table>"
    )
    return _page(f"Changes - {source}", body)


# ---------------------------------------------------------------------------
# 출력
# ---------------------------------------------------------------------------

_UNSAFE = re.compile(r'[<>:"|?*\x00-\x1f]')


def safe_rel_path(path: str) -> Path:
    """저장소 경로를 출력 디렉터리 밖으로 벗어나지 않는 상대 경로로 변환한다."""
    parts = [p for p in path.replace("\\", "/").split("/") if p not in ("", ".", "..")]
    parts = [_UNSAFE.sub("_", p) for p in parts] or ["_"]
    return Path(*parts[:-1], parts[-1] + ".html")


def export(items: list[dict], out_dir: Path, view: str = "unified", source: str = "") -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    files_dir = out_dir / "files"
    written: list[Path] = []
    index_entries: list[tuple[FileDiff, str]] = []
    used: set[str] = set()
    for item in items:
        fd = to_file_diff(item)
        rel = safe_rel_path(fd.path)
        # 대소문자만 다른 경로 등 충돌 방지
        base, n = rel, 2
        while str(rel).lower() in used:
            rel = base.with_name(f"{base.name[:-len('.html')]}~{n}.html")
            n += 1
        used.add(str(rel).lower())
        target = files_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        index_href = os.path.relpath(out_dir / "index.html", target.parent).replace(os.sep, "/")
        target.write_text(render_file_page(fd, view, source, index_href), encoding="utf-8")
        written.append(target)
        index_entries.append((fd, ("files" / rel).as_posix()))
    (out_dir / "index.html").write_text(render_index(index_entries, source), encoding="utf-8")
    return written


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def load_input(path: str) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, dict):  # /changes 또는 /compare 응답 형태
        data = data.get("changes") or data.get("diffs") or []
    if not isinstance(data, list):
        raise ValueError("입력 JSON은 diff 객체 배열이어야 합니다.")
    return data


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="GitLab Changes(diffs)를 파일별 HTML로 추출")
    src = ap.add_argument_group("대상")
    src.add_argument("--mr-url", help="MR URL (예: https://gitlab.example.com/group/proj/-/merge_requests/12)")
    src.add_argument("--gitlab-url", default=os.environ.get("GITLAB_URL", "https://gitlab.com"),
                     help="GitLab 주소 (기본: $GITLAB_URL 또는 https://gitlab.com)")
    src.add_argument("--project", help="프로젝트 ID 또는 경로 (예: group/proj)")
    tgt = src.add_mutually_exclusive_group()
    tgt.add_argument("--mr", type=int, help="Merge Request IID")
    tgt.add_argument("--commit", help="커밋 SHA")
    tgt.add_argument("--compare", metavar="FROM..TO", help="두 ref 비교")
    src.add_argument("--input", help="GitLab API diff 응답을 저장한 JSON 파일 (오프라인)")
    ap.add_argument("--token", default=os.environ.get("GITLAB_TOKEN"),
                    help="Personal/Project Access Token (기본: $GITLAB_TOKEN, read_api 권한)")
    ap.add_argument("-o", "--out", default="diff_html", help="출력 디렉터리 (기본: diff_html)")
    ap.add_argument("--view", choices=["unified", "side-by-side"], default="unified", help="diff 표시 방식")
    args = ap.parse_args(argv)

    try:
        if args.input:
            items, source = load_input(args.input), os.path.basename(args.input)
        else:
            base, project = args.gitlab_url, args.project
            if args.mr_url:
                base, project, args.mr = parse_mr_url(args.mr_url)
            if not project:
                ap.error("--mr-url, --project 또는 --input 중 하나가 필요합니다.")
            client = GitLabClient(base, args.token)
            if args.mr is not None:
                items, source = client.mr_diffs(project, args.mr), f"{project} !{args.mr}"
            elif args.commit:
                items, source = client.commit_diffs(project, args.commit), f"{project} @ {args.commit[:12]}"
            elif args.compare:
                if ".." not in args.compare:
                    ap.error("--compare 는 FROM..TO 형식이어야 합니다.")
                ref_from, ref_to = args.compare.split("..", 1)
                items, source = client.compare_diffs(project, ref_from, ref_to), f"{project} {ref_from}..{ref_to}"
            else:
                ap.error("--mr, --commit, --compare 중 하나가 필요합니다.")
    except (GitLabError, ValueError, OSError) as e:
        print(f"오류: {e}", file=sys.stderr)
        return 1

    out = Path(args.out)
    written = export(items, out, args.view, source)
    print(f"{len(written)}개 파일 HTML 생성 → {out / 'index.html'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
