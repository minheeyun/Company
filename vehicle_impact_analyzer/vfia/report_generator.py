"""보고서: 기계 처리용 JSON + 검토용 HTML.

변경 영향 보고서는 검증자가 다음 순서로 읽도록 구성한다.
  1. 무엇이 변경되었는가?            변경 전후 코드, 변경 유형, 관련 함수·변수
  2. 분석기가 무엇을 확인했는가?     빌드 설정, 데이터·제어 경로 종류, 해석하지 못한 영역, 검증 범위
  3. 어떤 영향이 발견되었는가?       관련 차량 기능, 영향 전달 경로, 발생 조건, 출력·상태 차이
  4. 검증자는 무엇을 추가로 확인?    경계값·시퀀스 시험, 타이밍, 미해결 호출·외부 모델 검토
"""
from __future__ import annotations

import datetime as _dt
import html
import json
import os
from typing import Dict, List, Optional

from . import __version__
from .model import Status

_STATUS_CLASS = {
    Status.EXEC_DIFF: "s-exec", Status.MODEL_DIFF: "s-model", Status.STATIC_CANDIDATE: "s-static",
    Status.INCONCLUSIVE: "s-inc", Status.MODEL_NO_DIFF: "s-nodiff", Status.EXEC_NO_DIFF: "s-nodiff",
    Status.NO_PATH: "s-none",
}
_ROLE = {"direct": "직접 구현", "support": "지원", "shared": "공용", "candidate": "후보(구조 근거 없음)"}
_EDGE = {"data": "데이터", "control": "제어", "call": "호출", "param_in": "인자 전달", "param_out": "반환/부작용",
         "summary": "호출 요약", "global_write": "전역 쓰기", "global_read": "전역 읽기", "signal_write": "신호 송신",
         "signal_read": "신호 수신", "calibrates": "캘리브레이션", "": ""}

CSS = """
:root{--bg:#fbfbfa;--fg:#1d1d1b;--mut:#6b6b66;--card:#fff;--bd:#e3e2dd;--code:#f4f3ef;--acc:#2f5d8a;
--exec:#8a1c1c;--model:#b4471b;--static:#9a6a00;--inc:#5b4b8a;--nodiff:#2e6b3f;--none:#6b6b66}
@media (prefers-color-scheme:dark){:root{--bg:#161615;--fg:#e9e8e3;--mut:#a3a29c;--card:#1f1f1d;--bd:#34332f;
--code:#262623;--acc:#8fb8e0;--exec:#ff8a80;--model:#ffab70;--static:#e8c35a;--inc:#b9a8ff;--nodiff:#86d19a;--none:#a3a29c}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.55 -apple-system,"Segoe UI",
"Apple SD Gothic Neo","Malgun Gothic",sans-serif}main{max-width:1180px;margin:0 auto;padding:24px 16px 64px}
h1{font-size:22px;margin:0 0 4px}h2{font-size:18px;margin:36px 0 10px;padding-bottom:6px;border-bottom:1px solid var(--bd)}
h3{font-size:15px;margin:22px 0 8px}h4{font-size:14px;margin:14px 0 6px}.mut{color:var(--mut)}
.card{background:var(--card);border:1px solid var(--bd);border-radius:8px;padding:12px 14px;margin:10px 0}
table{border-collapse:collapse;width:100%;margin:6px 0;font-size:13px}th,td{border-bottom:1px solid var(--bd);padding:6px 8px;
text-align:left;vertical-align:top}th{color:var(--mut);font-weight:600;white-space:nowrap}
code,pre{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:12.5px}
pre{background:var(--code);padding:8px 10px;border-radius:6px;overflow-x:auto;margin:4px 0;white-space:pre-wrap}
.badge{display:inline-block;padding:1px 8px;border-radius:999px;font-size:12px;font-weight:600;border:1px solid currentColor;white-space:nowrap}
.s-exec{color:var(--exec)}.s-model{color:var(--model)}.s-static{color:var(--static)}.s-inc{color:var(--inc)}
.s-nodiff{color:var(--nodiff)}.s-none{color:var(--none)}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:10px}@media(max-width:760px){.grid2{grid-template-columns:1fr}}
details>summary{cursor:pointer;font-weight:600;margin:4px 0}.path{margin:4px 0 8px;padding-left:0;list-style:none}
.path li{padding:2px 0 2px 14px;border-left:2px solid var(--bd);margin-left:6px}.path .via{color:var(--acc);font-size:12px;margin-right:6px}
.kv{display:grid;grid-template-columns:max-content 1fr;gap:2px 14px}.kv div:nth-child(odd){color:var(--mut)}
.pill{display:inline-block;background:var(--code);border-radius:4px;padding:0 6px;margin:1px 2px;font-size:12px}
.summary-cards{display:flex;flex-wrap:wrap;gap:10px}.summary-cards .card{flex:1 1 220px;margin:0}
.big{font-size:22px;font-weight:700}ul{margin:4px 0;padding-left:20px}
"""


def _e(x) -> str:
    return html.escape("" if x is None else str(x))


def badge(status: Optional[str]) -> str:
    if not status:
        return ""
    return f'<span class="badge {_STATUS_CLASS.get(status, "s-none")}">{_e(status)}</span>'


def _page(title: str, body: str) -> str:
    return (f"<!doctype html><html lang='ko'><head><meta charset='utf-8'><meta name='viewport' "
            f"content='width=device-width,initial-scale=1'><title>{_e(title)}</title><style>{CSS}</style></head>"
            f"<body><main>{body}</main></body></html>")


def write_json(path: str, data: dict) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, default=str)


def _build_box(build: dict, version_meta: Dict[str, dict]) -> str:
    rows = [f"<div>빌드 설정</div><div><b>{_e(build.get('name'))}</b></div>",
            f"<div>정의 매크로(-D)</div><div>{_e(', '.join(build.get('defines') or []) or '(없음)')}</div>",
            f"<div>compile DB</div><div>{_e(', '.join(build.get('compile_db_origin') or []))}</div>"]
    for side, m in version_meta.items():
        s = m.get("summary", {})
        rows.append(f"<div>{_e(side)} 소스</div><div><code>{_e(m.get('root'))}</code> — 번역 단위 {s.get('translation_units')}개, "
                    f"파싱 실패 {len(s.get('parse_failed_tus') or [])}개</div>")
    return f"<div class='kv'>{''.join(rows)}</div>"


# ---------------------------------------------------------------------------
def feature_report_html(res: dict, path: str) -> None:
    b = [f"<h1>기능 분류 보고서</h1><p class='mut'>버전 <b>{_e(res.get('version'))}</b> · VFIA {__version__} · "
         f"{_dt.datetime.now().strftime('%Y-%m-%d %H:%M')}</p>",
         f"<div class='card'>{_build_box(res.get('build') or {}, {})}</div>",
         "<div class='card mut'>" + "".join(f"<div>• {_e(n)}</div>" for n in res.get("notes", [])) + "</div>"]
    b.append("<h2>기능별 코드 요소</h2>")
    for f in res["features"]:
        counts = {}
        for x in f["elements"]:
            counts[x["role"]] = counts.get(x["role"], 0) + 1
        b.append(f"<h3>{_e(f['id'])} · {_e(f['name'])}</h3>")
        if f.get("requirements"):
            b.append("<div class='mut'>" + "<br>".join(f"{_e(r['id'])}: {_e(r.get('text'))}" for r in f["requirements"]) + "</div>")
        b.append("<div>" + " ".join(f"<span class='pill'>{_e(_ROLE.get(k, k))} {v}</span>" for k, v in counts.items())
                 + " · 관찰 대상: " + ", ".join(f"<span class='pill'>{_e(o['id'])} ({_e(o['kind'])})</span>" for o in f["observations"]) + "</div>")
        b.append("<table><tr><th>역할</th><th>요소</th><th>종류</th><th>위치</th><th>점수</th><th>근거</th><th>검토 상태</th></tr>")
        for x in f["elements"]:
            ev = "<br>".join(f"<span class='mut'>[{_e(e['type'])}]</span> {_e(e['detail'])}" for e in x["evidence"])
            b.append(f"<tr><td>{_e(_ROLE.get(x['role'], x['role']))}</td><td><code>{_e(x['name'])}</code></td>"
                     f"<td>{_e(x['kind'])}</td><td>{_e(x.get('file'))}:{_e(x.get('line'))}</td>"
                     f"<td>{x['rank_score']:.2f}</td><td>{ev}</td><td>{_e(x['status'])}</td></tr>")
        b.append("</table>")
        if f.get("rejected"):
            b.append("<div class='mut'>검토 기각: " + ", ".join(_e(x["name"]) for x in f["rejected"]) + "</div>")
    b.append("<h2>파일별 집계</h2><p class='mut'>함수·변수 단위 분류를 파일 단위로 합산. 한 파일에 여러 기능이 있을 수 있다.</p>")
    b.append("<table><tr><th>파일</th><th>기능(역할별 개수)</th><th>복수 기능</th></tr>")
    for fl in res["files"]:
        fs = "<br>".join(f"<b>{_e(x['feature'])}</b> " + ", ".join(f"{_e(_ROLE.get(k, k))} {v}" for k, v in x["roles"].items())
                         for x in fl["features"])
        b.append(f"<tr><td><code>{_e(fl['file'])}</code></td><td>{fs}</td><td>{'예' if fl['multi_feature'] else ''}</td></tr>")
    b.append("</table>")
    b.append("<h2>구조 군집(Louvain)</h2><p class='mut'>호출·데이터 공유가 밀접한 묶음. 묶음이 곧 차량 기능은 아니다.</p><table>"
             "<tr><th>군집</th><th>구성 요소</th><th>기능 연결(구조 근거 개수)</th></tr>")
    for c in res["communities"]:
        b.append(f"<tr><td>C{c['id']}</td><td>{', '.join('<code>' + _e(m) + '</code>' for m in c['members'])}</td>"
                 f"<td>{_e(c['feature_votes'] or '기능 미연결')}</td></tr>")
    b.append("</table>")
    if res.get("unmapped"):
        b.append("<h2>기능에 연결되지 않은 요소</h2><p>" + ", ".join(f"<code>{_e(u['name'])}</code>" for u in res["unmapped"]) + "</p>")
    m = res.get("method", {})
    b.append("<h2>방법</h2><div class='kv'>" + "".join(f"<div>{_e(k)}</div><div>{_e(v)}</div>" for k, v in m.items()) + "</div>")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(_page("기능 분류 보고서", "".join(b)))


# ---------------------------------------------------------------------------
def _path_html(p: dict) -> str:
    items = []
    for s in p["steps"]:
        via = _EDGE.get(s.get("via") or "", s.get("via") or "")
        loc = f"{s.get('func') or ''} {s.get('file') or ''}:{s.get('line') or ''}".strip() if s.get("file") else ""
        items.append(f"<li>{('<span class=via>' + _e(via) + (' ' + _e(s.get('label')) if s.get('label') else '') + ' →</span>') if via else ''}"
                     f"<code>{_e(s.get('text'))}</code> <span class='mut'>{_e(loc)}</span></li>")
    return f"<div class='mut'>관찰 대상 {_e(', '.join(p['observation']))} · 경로 길이 {p['length']}</div><ul class='path'>{''.join(items)}</ul>"


def _witness_table(ws: List[dict], title: str) -> str:
    if not ws:
        return ""
    keys = []
    for w in ws:
        for k in w.get("values", {}):
            if k not in keys:
                keys.append(k)
    head = "".join(f"<th>{_e(k)}</th>" for k in keys)
    rows = []
    for w in ws:
        inp = "<br>".join(", ".join(f"{_e(k)}={_e(v)}" for k, v in st.items()) or "—" for st in w["inputs"])
        if len(w["inputs"]) > 1:
            inp = "<br>".join(f"단계{i}: " + (", ".join(f"{_e(k)}={_e(v)}" for k, v in st.items()) or "—")
                              for i, st in enumerate(w["inputs"]))
        env = w.get("environment")
        if env:
            inp += "<br><span class='mut'>환경 입력: " + ", ".join(f"{_e(k)}={_e(v)}" for k, v in list(env.items())[:8]) + "</span>"
        vals = "".join(f"<td>{_e(', '.join(str(x) for x in w['values'].get(k, [])))}</td>" for k in keys)
        rows.append(f"<tr><td>{inp}</td>{vals}</tr>")
    return f"<h4>{_e(title)}</h4><table><tr><th>입력</th>{head}</tr>{''.join(rows)}</table>"


def _region_text(reg: Optional[dict]) -> str:
    if not reg:
        return ""
    return ", ".join(f"{_e(lo)} ≤ <code>{_e(k)}</code> ≤ {_e(hi)}" if lo != hi else f"<code>{_e(k)}</code> = {_e(lo)}"
                     for k, (lo, hi) in reg.items())


def _behavior_html(checks: List[dict]) -> str:
    out = []
    for c in checks:
        if c.get("scope") == "out":
            out.append(f"<div class='mut'>동작 모델 {_e(c['observation'])}({_e(c['function'])}): {_e(c.get('note'))}</div>")
            continue
        out.append(f"<div class='card'><b>동작 모델 {_e(c['observation'])}</b> ({_e(c['function'])}) {badge(c.get('status'))}")
        if c.get("error"):
            out.append(f"<div class='mut'>판정 유보 사유: {_e(c['error'])}</div>")
        if c.get("note"):
            out.append(f"<div class='mut'>{_e(c['note'])}</div>")
        for st in c.get("sentences") or []:
            out.append(f"<div class='card'><b>{_e(st)}</b></div>")
        if c.get("divergence") == "sat":
            dc = c.get("divergence_condition") or {}
            out.append(f"<div>분기·값 선택이 달라지는 조건{' (정확·검증됨)' if dc.get('exact') else ''}: "
                       f"<code>{_e(dc.get('text') or _region_text(c.get('divergence_region')) or '증거 입력 참고')}</code></div>")
            if c.get("divergence_formula"):
                out.append(f"<details><summary>결합 프로그램의 분기 차이 식(도달 조건 포함, 정규화)</summary>"
                           f"<pre>{_e(c['divergence_formula'])}</pre></details>")
            wd = c.get("with_output_diff")
            out.append(f"<div>그 입력에서 관찰값 차이: <b>{'발생' if wd == 'sat' else ('발생하지 않음' if wd == 'unsat' else '판정 불가')}</b></div>")
        elif c.get("divergence") == "unsat":
            out.append("<div>변경된 조건/값이 같은 입력에서 달라지는 경우 없음</div>")
        iso = c.get("isolated")
        if iso:
            unit = "이 변경" if iso.get("granularity") == "change" else f"이 함수({iso.get('function')}) 변경"
            txt = {"sat": f"{unit}만 적용해도 관찰값이 달라짐", "unsat": f"{unit}만 적용하면 관찰값 동일(검증 범위 내 동치)",
                   "unknown": "판정 불가", "unsupported": f"지원하지 않는 구문: {iso.get('error')}"}.get(iso["status"], iso["status"])
            cond = (iso.get("condition") or {})
            out.append(f"<div>변경 분리 검증: <b>{_e(txt)}</b>"
                       + (f" — 차이 조건{' (정확·검증됨)' if cond.get('exact') else ''}: <code>{_e(cond.get('text'))}</code>"
                          if cond.get("text") else "") + "</div>")
            if iso.get("note"):
                out.append(f"<div class='mut'>{_e(iso['note'])}</div>")
        for st in c.get("combined_sentences") or []:
            out.append(f"<div class='mut'>모든 변경을 함께 적용했을 때 이 변경의 분기 차이 지점에서: {_e(st)}</div>")
        if c.get("branch_tests"):
            rows = "".join(
                f"<tr><td>{_e(t['class'])}</td><td>{_e('; '.join(', '.join(f'{k}={v}' for k, v in st.items()) for st in t['inputs']))}"
                f"{('<br><span class=mut>' + _e(', '.join(f'{k}={v}' for k, v in list((t.get('environment') or {}).items())[:6])) + '</span>') if t.get('environment') else ''}</td>"
                f"<td>{_e(t['values'].get('old'))} → {_e(t['values'].get('new'))}</td><td>{'가능' if t['output_can_differ'] else '없음'}</td></tr>"
                for t in c["branch_tests"])
            out.append("<h4>분기 차이별 시험 입력(변경 전/후 실행이 갈리는 지점마다)</h4><table><tr><th>분기 차이</th><th>입력</th>"
                       f"<th>관찰값(전→후)</th><th>관찰값 차이</th></tr>{rows}</table>")
        for side, lst in (c.get("static_ranges") or {}).items():
            for x in lst:
                out.append(f"<div class='mut'>정적 구간 분석({side}) <code>{_e(x['condition'])}</code>: "
                           f"참 {'가능' if x.get('can_true') else '불가'}/거짓 {'가능' if x.get('can_false') else '불가'}, 변수 범위 {_e(x.get('vars'))}</div>")
        out.append(_witness_table(c.get("examples_diff") or [], "관찰값이 달라지는 예"))
        out.append(_witness_table(c.get("examples_same_output") or [], "중간 값은 달라지지만 관찰값은 같은 예"))
        if c.get("exec_examples"):
            rows = "".join(f"<tr><td>{_e(d['inputs'])}</td><td>{_e(' '.join(d['old']))}</td><td>{_e(' '.join(d['new']))}</td></tr>"
                           for d in c["exec_examples"])
            out.append(f"<h4>실행 비교로 확인한 차이</h4><table><tr><th>입력</th><th>변경 전 출력</th><th>변경 후 출력</th></tr>{rows}</table>")
        out.append("</div>")
    return "".join(out)


def impact_report_html(data: dict, path: str) -> None:
    meta = data["meta"]
    changes = data["changes"]
    b = [f"<h1>변경 영향 분석 보고서</h1><p class='mut'>프로젝트 <b>{_e(meta.get('project'))}</b> · VFIA {__version__} · "
         f"{_e(meta.get('generated'))}</p>"]
    feats = data.get("features", [])
    cards = [f"<div class='card'><div class='mut'>변경점</div><div class='big'>{len(changes)}</div></div>"]
    for f in feats:
        cards.append(f"<div class='card'><div class='mut'>{_e(f['feature'])}</div><div><b>{_e(f['name'])}</b></div>"
                     f"<div>{badge(f['overall'])}</div><div class='mut'>관련 변경 {len(f['changes'])}개</div></div>")
    b.append(f"<div class='summary-cards'>{''.join(cards)}</div>")
    concl = []
    for r in data.get("behavior", []):
        for st in ((r.get("condition") or {}).get("sentences") or [])[:2]:
            concl.append((r["observation"], "전체 변경", st, (r.get("condition") or {}).get("exact")))
        for cid, pc in (r.get("per_change") or {}).items():
            if pc.get("status") in (Status.MODEL_DIFF, Status.EXEC_DIFF):
                for st in (pc.get("sentences") or [])[:1]:
                    concl.append((r["observation"], cid, st, ((pc.get("isolated") or {}).get("condition") or {}).get("exact")))
    if concl:
        b.append("<h2>어떤 조건에서 차량 기능이 어떻게 달라지는가</h2><table><tr><th>관찰</th><th>변경</th><th>결론</th><th>조건</th></tr>"
                 + "".join(f"<tr><td>{_e(o)}</td><td>{_e(c_)}</td><td>{_e(st)}</td><td>{'정확·검증됨' if ex else '부분/근사'}</td></tr>"
                           for o, c_, st, ex in concl) + "</table>")
    b.append("<div class='card mut'>상태 구분: " + " ".join(badge(s) for s in (
        Status.EXEC_DIFF, Status.MODEL_DIFF, Status.STATIC_CANDIDATE, Status.INCONCLUSIVE, Status.MODEL_NO_DIFF,
        Status.EXEC_NO_DIFF, Status.NO_PATH)) + "<br>경로를 찾지 못한 경우 '영향 없음'이 아니라 "
             "'현재 분석 범위에서 영향 경로 미발견'으로 기록한다.</div>")

    # 1
    b.append("<h2>1. 무엇이 변경되었는가?</h2><table><tr><th>ID</th><th>변경 유형</th><th>대상</th><th>요약</th><th>위치(전 → 후)</th></tr>")
    for c in changes:
        lo = f"{c['old'].get('file') or ''}:{c['old'].get('line') or ''}" if c["old"].get("line") else "—"
        ln = f"{c['new'].get('file') or ''}:{c['new'].get('line') or ''}" if c["new"].get("line") else "—"
        b.append(f"<tr><td><a href='#{c['id']}'>{c['id']}</a></td><td>{_e(c['type_label'])}</td><td><code>{_e(c.get('unit_name'))}</code></td>"
                 f"<td>{_e(c['summary'])}</td><td>{_e(lo)} → {_e(ln)}</td></tr>")
    b.append("</table>")
    for c in changes:
        b.append(f"<div class='card' id='{c['id']}'><b>{c['id']} · {_e(c['type_label'])}</b> <code>{_e(c.get('unit_name'))}</code>"
                 f"<div>{_e(c['summary'])}</div><div class='grid2'>"
                 f"<div><div class='mut'>변경 전 {_e(c['old'].get('file'))}:{_e(c['old'].get('line'))}</div><pre>{_e(c['old'].get('code') or '(없음)')}</pre></div>"
                 f"<div><div class='mut'>변경 후 {_e(c['new'].get('file'))}:{_e(c['new'].get('line'))}</div><pre>{_e(c['new'].get('code') or '(없음)')}</pre></div></div>")
        extra = []
        if c.get("related_variables"):
            extra.append(f"<div>관련 변수: {', '.join('<code>' + _e(v) + '</code>' for v in c['related_variables'])}</div>")
        if c.get("side_effect"):
            extra.append(f"<div><b>부작용</b>: {_e(', '.join(c['side_effect']))} — 사용처가 없어도 영향이 없다고 보지 않음</div>")
        if c.get("affected_tus"):
            extra.append(f"<div>영향받는 번역 단위: {', '.join('<code>' + _e(t) + '</code>' for t in c['affected_tus'])}</div>")
        if c.get("expansion_sites"):
            extra.append("<div>매크로 전개 지점: " + ", ".join(
                f"{_e(s['function'])}:{_e(s['new_line'])} ({_e(s['old'])}→{_e(s['new'])})" for s in c["expansion_sites"]) + "</div>")
        if c.get("link_risk"):
            extra.append(f"<div><b>링크 위험</b>: {_e(c['link_risk'])}</div>")
        if c.get("review_note"):
            extra.append(f"<div class='mut'>{_e(c['review_note'])}</div>")
        if c.get("note"):
            extra.append(f"<div class='mut'>{_e(c['note'])}</div>")
        b.append("".join(extra) + "</div>")

    cov = data.get("text_coverage") or []
    if cov:
        cnt: Dict[str, int] = {}
        for h in cov:
            cnt[h["class"]] = cnt.get(h["class"], 0) + 1
        names = {"mapped": "AST 변경으로 설명됨", "comment_or_whitespace": "주석·공백", "inactive_in_this_config": "비활성 #if 구간",
                 "preprocessor": "전처리 지시문", "not_compiled": "컴파일 대상 아님", "unmapped": "설명되지 않음(누락 가능성)"}
        b.append("<h3>전처리 전 텍스트 변경과 AST 변경의 대응</h3><div>" + " ".join(
            f"<span class='pill'>{_e(names.get(k, k))} {v}</span>" for k, v in cnt.items()) + "</div>")
        odd = [h for h in cov if h["class"] in ("unmapped", "not_compiled", "inactive_in_this_config")]
        if odd:
            b.append("<table><tr><th>분류</th><th>파일</th><th>줄(전→후)</th><th>변경 후 내용</th></tr>" + "".join(
                f"<tr><td>{_e(names.get(h['class']))}</td><td>{_e(h['file_new'] or h['file_old'])}</td><td>{_e(h['old'])} → {_e(h['new'])}</td>"
                f"<td><pre>{_e(h['text_new'] or h['text_old'])}</pre></td></tr>" for h in odd) + "</table>")
    res_ = data.get("resources") or {}
    if res_.get("functions") or res_.get("tasks") or res_.get("ram_old") != res_.get("ram_new"):
        b.append("<h3>비기능 영향(추정)</h3><div class='mut'>" + _e(res_.get("note")) + "</div><table><tr><th>대상</th><th>스택(전→후)</th><th>코드 크기(전→후)</th></tr>"
                 + "".join(f"<tr><td>{_e(x['function'])}</td><td>{_e(x['stack_old'])} → {_e(x['stack_new'])}</td>"
                           f"<td>{_e(x['code_nodes_old'])} → {_e(x['code_nodes_new'])}</td></tr>" for x in res_.get("functions", []))
                 + "".join(f"<tr><td>태스크 {_e(x['context'])} 최악 호출 체인</td><td>{_e(x['stack_old'])} → {_e(x['stack_new'])}</td><td></td></tr>"
                           for x in res_.get("tasks", []))
                 + f"<tr><td>RAM(전역 합)</td><td colspan=2>{_e(res_.get('ram_old'))} → {_e(res_.get('ram_new'))} 바이트</td></tr></table>")

    # 2
    b.append("<h2>2. 분석기가 무엇을 확인했는가?</h2>")
    if data.get("assumptions"):
        b.append("<h3>분석 가정</h3><table>" + "".join(f"<tr><th>{_e(a['topic'])}</th><td>{_e(a['text'])}</td></tr>"
                                                        for a in data["assumptions"]) + "</table>")
    ph = data.get("parse_health") or {}
    if ph:
        b.append("<h3>빌드 환경 재현·파싱 건전성(단계 2 완료 기준)</h3><table><tr><th>버전</th><th>컴파일러 프로필</th><th>파싱 성공률</th>"
                 "<th>오류 번역 단위</th><th>누락 의심 함수</th><th>부분 AST 함수</th><th>제거된 원래 옵션</th></tr>")
        for side, v in ph.items():
            if not v:
                continue
            b.append(f"<tr><td>{_e(side)}</td><td>{_e(', '.join(v.get('compiler_profiles') or []))}</td>"
                     f"<td>{_e(v.get('success_rate'))}</td><td>{_e(v.get('errors_by_tu') or '-')}</td>"
                     f"<td>{_e([m['function'] for m in v.get('missing_function_candidates') or []] or '-')}</td>"
                     f"<td>{_e([m['function'] for m in v.get('partial_ast_functions') or []] or '-')}</td>"
                     f"<td>{_e(' '.join(v.get('dropped_options') or []) or '-')}</td></tr>")
        b.append("</table>")
    si = (data.get("symbol_integration") or {})
    if si:
        b.append("<h3>번역 단위 간 심벌 통합</h3><ul>")
        for side, v in si.items():
            if v:
                b.append(f"<li>{_e(side)}: 외부 링크 {v.get('extern_symbols')}개(이름으로 통합), static {v.get('static_symbols')}개(파일별 분리), "
                         f"선언·정의 자료형 불일치 {len(v.get('type_mismatches') or [])}건, 같은 이름 static {len(v.get('same_name_statics') or [])}건, "
                         f"정의 없는 참조 {_e(', '.join(v.get('undefined_referenced') or []) or '-')}</li>")
        b.append("</ul>")
    itf = data.get("interference") or {}
    if any(itf.values()):
        b.append("<h3>실행 문맥 간 간섭 의존</h3><ul>" + "".join(
            f"<li>{_e(side)}: 간섭 의존 간선 {v.get('edges')}개, 보호 없는 비원자 접근 {v.get('unprotected_non_atomic')}건, "
            f"init 순서로 제거한 흐름 {len(v.get('filtered_flows') or [])}건</li>" for side, v in itf.items() if v) + "</ul>")
    if data.get("static_branches"):
        b.append("<h3>정적으로 결정되는 분기(구간 추상 해석)</h3><ul>" + "".join(
            f"<li><code>{_e(x['text'])}</code> {_e(x['file'])}:{_e(x['line'])} — 항상 {_e(x['always'])}</li>"
            for x in data["static_branches"]) + "</ul>")
    if data.get("mvp"):
        b.append("<h3>2.5단계 함수 단위 간이 분석(비교용)</h3><table><tr><th>변경</th><th>MVP 영향 기능</th><th>호출 그래프만</th></tr>" + "".join(
            f"<tr><td>{_e(m['change'])} {_e(m['unit'])}</td><td>{_e(', '.join(m['features']) or '-')}</td>"
            f"<td>{_e(', '.join(m['callgraph_features']) or '-')}</td></tr>" for m in data["mvp"]) + "</table>")
    b.append(f"<div class='card'>{_build_box(meta.get('build') or {}, meta.get('versions') or {})}</div>")
    scope = data.get("scope", {})
    b.append("<div class='card'><div class='kv'>" + "".join(f"<div>{_e(k)}</div><div>{_e(v)}</div>" for k, v in scope.get("method", {}).items()) + "</div></div>")
    inact = scope.get("inactive_regions") or {}
    if inact:
        b.append("<h3>이 빌드 설정에서 비활성인 조건부 컴파일 구간</h3><table><tr><th>파일</th><th>#if 조건 매크로</th><th>비활성 줄</th></tr>")
        for f, v in inact.items():
            b.append(f"<tr><td><code>{_e(f)}</code></td><td>{_e(', '.join(v['macros']))}</td><td>{_e(v['ranges'])}</td></tr>")
        b.append("</table>")
    unres = scope.get("unresolved") or {}
    b.append("<h3>해석하지 못했거나 보수적으로 모델링한 영역</h3><ul>")
    for k, v in unres.items():
        if v:
            b.append(f"<li><b>{_e(k)}</b>: {_e(', '.join(v) if isinstance(v, list) else v)}</li>")
    b.append("</ul>")
    if data.get("behavior"):
        b.append("<h3>동작 차이 검증 범위</h3><table><tr><th>기능/관찰</th><th>모델 함수</th><th>입력 정의역</th><th>범위</th><th>결과</th><th>실행 비교</th></tr>")
        for r in data["behavior"]:
            bd = r.get("bounds", {})
            ex = r.get("exec") or {}
            exs = (ex.get("status") or "") + (f" ({ex.get('vectors')}개 입력, 차이 {ex.get('diff_count')}개)" if ex.get("vectors") else "") \
                + (f" — {ex.get('reason')}" if ex.get("reason") else "")
            b.append(f"<tr><td>{_e(r['feature'])}/{_e(r['observation'])}</td><td><code>{_e(r['function'])}</code></td>"
                     f"<td>{_e(r.get('domain'))}</td><td>반복 {bd.get('loop_bound')}회, 인라인 {bd.get('inline_depth')}단계, "
                     f"시퀀스 {bd.get('sequence')}단계, 초기 상태 {_e(bd.get('initial_state'))}</td>"
                     f"<td>{badge(r.get('status'))}{('<br><span class=mut>' + _e(r.get('error')) + '</span>') if r.get('error') else ''}"
                     f"{('<br><span class=mut>' + _e('; '.join(r.get('notes') or [])) + '</span>') if r.get('notes') else ''}</td>"
                     f"<td>{_e(exs)}</td></tr>")
        b.append("</table>")

    # 3
    b.append("<h2>3. 어떤 영향이 발견되었는가?</h2><p class='mut'>변경은 순위 점수(의존 거리·경로 수) 순으로 정렬</p>")
    for f in feats:
        b.append(f"<h3>{_e(f['feature'])} · {_e(f['name'])} {badge(f['overall'])}</h3>")
        b.append("<table><tr><th>변경</th><th>유형</th><th>대상</th><th>순위 점수</th><th>상태</th></tr>" + "".join(
            f"<tr><td><a href='#{_e(x['change'])}-{_e(f['feature'])}'>{_e(x['change'])}</a></td><td>{_e(x['type'])}</td>"
            f"<td><code>{_e(x['unit'])}</code></td><td>{_e(x.get('rank_score'))}</td><td>{badge(x['status'])}</td></tr>"
            for x in f["changes"]) + "</table>")
        for bh in [r for r in data.get("behavior", []) if r["feature"] == f["feature"] and r.get("status") == Status.MODEL_DIFF]:
            cond = bh.get("condition") or {}
            b.append(f"<div class='card'><b>전체 변경 적용 시 관찰값 차이 ({_e(bh['observation'])}, {_e(bh.get('method'))})</b>"
                     + (f"<div>차이 조건{' (정확·검증됨)' if cond.get('exact') else ''}: <code>{_e(cond.get('text'))}</code></div>"
                        if cond.get("text") else "")
                     + "".join(f"<div><b>{_e(st)}</b></div>" for st in cond.get("sentences") or [])
                     + _witness_table(bh.get("witnesses") or [], "증거 입력") + "</div>")
    for c in changes:
        im = c["impact"]
        for fi in im["features"]:
            b.append(f"<div class='card' id='{c['id']}-{_e(fi['feature'])}'><b>{c['id']} → {_e(fi['feature'])} {_e(fi['feature_name'])}</b> "
                     f"{badge(fi['status'])} <span class='mut'>({_e(c['type_label'])}, {_e(c.get('unit_name'))})</span>")
            if fi.get("status_note"):
                b.append(f"<div class='mut'>{_e(fi['status_note'])}</div>")
            for side in ("new", "old"):
                sd = fi["sides"].get(side)
                if sd:
                    b.append(f"<div class='mut'>순위 점수 {fi.get('rank_score')} · 의존 거리 {sd.get('dependence_distance')} · "
                             f"최단 경로 수 {sd.get('path_count')} · 문맥 민감 chop {sd.get('chop_size')}노드"
                             f"(단순 교집합 {sd.get('naive_intersection_size')}노드, 실현 불가 경로로 제외 {sd.get('overapprox_removed')})</div>")
                    for it in sd.get("interference") or []:
                        b.append(f"<div class='mut'>간섭 의존: {_e(it.get('note'))}</div>")
                    break
            for side, label in (("old", "변경 전 그래프"), ("new", "변경 후 그래프")):
                sd = fi["sides"].get(side)
                if not sd:
                    continue
                b.append(f"<details {'open' if side == 'new' or 'new' not in fi['sides'] else ''}><summary>{label}: "
                         f"관찰 대상 {_e(', '.join(sd['reached_observations']))} 도달, 경로 함수 {len(sd['functions'])}개</summary>")
                for p in sd["paths"]:
                    b.append(_path_html(p))
                if sd.get("contexts"):
                    b.append(f"<div>실행 문맥: {', '.join('<span class=pill>' + _e(x) + '</span>' for x in sd['contexts'])}</div>")
                for sh in sd.get("shared_state") or []:
                    b.append(f"<div class='mut'>공유 상태 <code>{_e(sh['location'])}</code>: 쓰기 {_e(sh['writers'])} / 읽기 {_e(sh['readers'])}"
                             f"{' (volatile)' if sh.get('volatile') else ''}</div>")
                b.append("</details>")
            if fi.get("timing"):
                t = fi["timing"]
                b.append(f"<div>타이밍 영향: 경로상 함수 {', '.join('<code>' + _e(x) + '</code>' for x in t['functions'])}, "
                         f"갱신 상태 {_e(', '.join(t['updated_state']))}<br><span class='mut'>{_e(t['note'])}</span></div>")
            if fi.get("unit_roles"):
                b.append("<div class='mut'>기능 분류상 변경 대상의 역할: " + ", ".join(
                    f"{_e(r['side'])}:{_e(_ROLE.get(r['role'], r['role']))}" for r in fi["unit_roles"]) + "</div>")
            if fi.get("behavior"):
                b.append(_behavior_html(fi["behavior"]))
            b.append("</div>")
    none = [c for c in changes if not c["impact"]["features"]]
    if none:
        b.append("<h3>기능 관찰 대상까지 경로를 찾지 못한 변경</h3><table><tr><th>변경</th><th>상태</th><th>영향 범위(전방 슬라이스)</th></tr>")
        for c in none:
            sl = c["impact"]["slice"].get("new") or c["impact"]["slice"].get("old") or {}
            desc = ""
            if sl:
                desc = f"함수 {', '.join(sl.get('functions') or []) or '-'} / 전역 {', '.join(sl.get('globals') or []) or '-'}"
            b.append(f"<tr><td>{c['id']} {_e(c['type_label'])} <code>{_e(c.get('unit_name'))}</code></td>"
                     f"<td>{badge(Status.NO_PATH)}</td><td>{_e(desc) or '데이터 흐름 시작점 없음(검토 항목 참고)'}</td></tr>")
        b.append("</table>")

    # 4
    b.append("<h2>4. 검증자는 무엇을 추가로 확인해야 하는가?</h2>")
    for sec in data.get("review", []):
        b.append(f"<h3>{_e(sec['title'])}</h3><ul>" + "".join(f"<li>{_e(x)}</li>" for x in sec["items"]) + "</ul>")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(_page("변경 영향 분석 보고서", "".join(b)))


# ---------------------------------------------------------------------------
def _seq_text(ex: dict) -> str:
    steps = []
    env = ex.get("environment") or {}
    for i, st in enumerate(ex.get("inputs", [])):
        parts = [f"{k}={v}" for k, v in st.items()]
        parts += [f"{k.split('[')[0]}={v}" for k, v in env.items() if f"[단계{i}," in k]
        steps.append(f"단계{i}(" + ", ".join(parts) + ")")
    return " → ".join(steps)


def review_items(changes: List[dict], behavior: List[dict], meta: dict, scope: dict, data: Optional[dict] = None) -> List[dict]:
    """보고서 4장: 검증자가 추가로 확인할 항목 생성."""
    tests, timing, ext, scope_items, iface, config = [], [], [], [], [], []
    static_pairs = {(c["id"], f["feature"]) for c in changes for f in c["impact"]["features"]
                    if f["status"] == Status.STATIC_CANDIDATE}
    for r in behavior or []:
        dom = r.get("domain") or {}
        out_scope = []
        for cid, pc in (r.get("per_change") or {}).items():
            reg = pc.get("divergence_region") or (pc.get("isolated") or {}).get("region")
            if reg and pc.get("status") in (Status.MODEL_DIFF, Status.EXEC_DIFF, Status.MODEL_NO_DIFF):
                parts = []
                for k, (lo, hi) in reg.items():
                    if lo is None:
                        continue
                    d = dom.get(k)
                    if isinstance(d, list) and [lo, hi] == list(d):
                        continue          # 정의역 전체 -> 경계 시험 의미 없음
                    parts.append(f"{k}={lo}" if lo == hi else f"{k}: {lo - 1}, {lo}, {hi}, {hi + 1}")
                if parts:
                    tests.append(f"[{cid}] {r['feature']}/{r['observation']}: 경계값 시험 — " + "; ".join(parts))
            if r["bounds"]["sequence"] > 1 and pc.get("status") in (Status.MODEL_DIFF, Status.EXEC_DIFF):
                ex = (pc.get("examples_diff") or [{}])[0]
                tests.append(f"[{cid}] {r['feature']}: 입력 시퀀스·상태 전이 시험(최소 {r['bounds']['sequence']}단계) "
                             f"예: {_seq_text(ex)}")
            if pc.get("scope") == "out" and (cid, r["feature"]) in static_pairs:
                out_scope.append(cid)
        if out_scope:
            scope_items.append(f"{r['feature']}/{r['observation']}: 정적 경로는 있으나 동작 모델({r['function']}) 밖에서 "
                               f"변경된 항목 {', '.join(out_scope)} — 상위 함수·태스크 단위 통합 시나리오로 확인")
        if r.get("unwind_possible"):
            tests.append(f"{r['feature']}/{r['observation']}: 반복 전개 한도({r['bounds']['loop_bound']}) 밖 실행 존재 — 한도를 늘리거나 시험으로 확인")
    seen_shared = set()
    for c in changes:
        if c["category"] == "timing":
            timing.append(f"[{c['id']}] {c['summary']} — 데이터 갱신 주기, 실행 순서, 공유 상태 접근 시점 확인 "
                          f"(문맥 내 함수 {len(c.get('context_functions') or [])}개)")
        if c["type"] == "type_layout_change":
            iface.append(f"[{c['id']}] {c['summary']} — {c.get('review_note')} (번역 단위: {', '.join(c.get('affected_tus') or [])})")
        if c.get("link_risk"):
            iface.append(f"[{c['id']}] {c['link_risk']}")
        if c["type"] == "signature_change":
            iface.append(f"[{c['id']}] {c['summary']} — 모든 호출부의 형변환·값 범위 확인")
        if c.get("side_effect") and not any(f["status"] != Status.NO_PATH for f in c["impact"]["features"]):
            sl = c["impact"]["slice"].get("new") or {}
            iface.append(f"[{c['id']}] {c['summary']} — 기능 관찰 대상까지 경로는 없지만 부작용이 있음"
                         f"(영향 전역: {', '.join(sl.get('globals') or []) or '-'})")
        for f in c["impact"]["features"]:
            for sd in f["sides"].values():
                for sh in sd.get("shared_state") or []:
                    key = sh["location"]
                    if key in seen_shared:
                        continue
                    seen_shared.add(key)
                    timing.append(f"공유 상태 {sh['location']}: 쓰기 {', '.join(sh['writers'])} / 읽기 {', '.join(sh['readers'])}"
                                  f"{' (volatile)' if sh.get('volatile') else ''} — 동기화·원자성·갱신 시점 확인")
        hits = c["impact"].get("incomplete_in_slice") or []
        for h in hits:
            if h.get("may_hide"):
                for r in h["reasons"]:
                    ext.append(f"[{c['id']}] 영향 범위 안: {r}")
    stub_notes = set()
    for e in behavior or []:
        for n in (e.get("exec") or {}).get("notes", []):
            stub_notes.add(n.split(": ", 1)[1] if ": " in n else n)
    for n in sorted(stub_notes):
        ext.append(f"전후 비교 실행: {n}")
    ext_fns = (scope.get("unresolved") or {}).get("본문 없는 외부 함수(보수적 요약)") or []
    if ext_fns:
        ext.append(f"본문 없는 외부 함수 {', '.join(ext_fns)}: 인자→결과·포인터 인자 쓰기로 보수적 요약. 실제 동작(RTE/BSW 모델) 검토")
    for f, v in (scope.get("inactive_regions") or {}).items():
        others = [m for m in v["macros"] if not m.endswith("_H") and not m.endswith("_H_")]
        if others:
            config.append(f"{f}: 이 빌드 설정에서 비활성인 구간이 있음(조건 매크로 {', '.join(others)}). 다른 차종·빌드 설정으로 재분석 필요")
    for u in (scope.get("unresolved") or {}).get("간접 호출 미해결", []) or []:
        ext.append(f"간접 호출 미해결: {u}")
    for c in changes:
        if c["type"] == "inactive_code_change":
            config.append(f"[{c['id']}] {c['summary']}")
    for r in behavior or []:
        cond = r.get("condition") or {}
        if any(v.startswith("Cal_") or "cal" in v.lower() for v in cond.get("variables") or []):
            tests.append(f"{r['feature']}/{r['observation']}: 차이가 캘리브레이션 값에 따라 달라짐 — 데이터셋(A2L/HEX)의 실제 값으로 확인: {cond.get('text')}")
    data = data or {}
    build = []
    for h in data.get("text_coverage") or []:
        if h["class"] == "unmapped":
            build.append(f"{h['file_new'] or h['file_old']} {h['old']}→{h['new']}: 텍스트 변경이 AST 변경으로 설명되지 않음 — "
                         "매크로·전처리·파싱 누락 여부 확인")
        elif h["class"] == "not_compiled":
            build.append(f"{h['file_new'] or h['file_old']}: 어느 번역 단위에도 포함되지 않은 파일의 변경 — 분석 대상 밖")
    for side, ph in (data.get("parse_health") or {}).items():
        if not ph:
            continue
        for m in ph.get("missing_function_candidates") or []:
            build.append(f"[{side}] {m['file']}:{m['line']} {m['function']}: 소스에는 있으나 AST에 없음(컴파일러 확장·옵션 변환 확인)")
        for m in ph.get("partial_ast_functions") or []:
            build.append(f"[{side}] {m['file']}:{m['line']} {m['function']}: 파싱 오류로 부분 AST — {m['error']}")
    for t in (data.get("resources") or {}).get("tasks") or []:
        if (t.get("stack_new") or 0) > (t.get("stack_old") or 0):
            iface.append(f"태스크 {t['context']} 최악 호출 체인 스택 추정 {t['stack_old']} -> {t['stack_new']} 바이트: 스택 여유 확인")
    for side, v in (data.get("interference") or {}).items():
        if v and v.get("unprotected_non_atomic"):
            timing.append(f"[{side}] 보호 없는 비원자 공유 접근 {v['unprotected_non_atomic']}건: 임계 구역·원자 접근 검토")
    out = []
    for title, items in (("빌드 환경·파싱·전처리 대응", build), ("경계값·시퀀스 시험", tests), ("타이밍·동시성 확인", timing),
                         ("모델 범위 밖 변경(통합 시나리오 필요)", scope_items),
                         ("미해결 호출·외부 모델 검토", ext), ("인터페이스·메모리 배치·부작용·링크", iface),
                         ("빌드 설정·차종 변형", config)):
        if items:
            out.append({"title": title, "items": sorted(set(items), key=items.index)})
    return out
