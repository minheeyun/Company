"""사람이 읽는 SW 검증 리뷰 보고서(HTML, 단일 파일)."""

from __future__ import annotations

import html
import json

E = html.escape

CSS = """
:root{--bg:#f7f8fa;--card:#fff;--fg:#1d232b;--muted:#5d6876;--line:#dfe3e8;--code:#f2f4f7;
--direct:#d93636;--indirect:#d07a10;--potential:#7e57c2;--none:#2f8f55;--add:#e6f6ea;--del:#fdecec;--accent:#2563eb}
@media (prefers-color-scheme: dark){:root:not([data-theme="light"]){--bg:#14171c;--card:#1c2027;--fg:#e4e8ee;--muted:#9aa5b4;
--line:#2e3540;--code:#232933;--add:#183222;--del:#3a1d1f;--accent:#7aa2ff}}
:root[data-theme="dark"]{--bg:#14171c;--card:#1c2027;--fg:#e4e8ee;--muted:#9aa5b4;--line:#2e3540;--code:#232933;--add:#183222;
--del:#3a1d1f;--accent:#7aa2ff}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.55 -apple-system,"Segoe UI",
"Noto Sans KR","Malgun Gothic",sans-serif}
header{padding:20px 24px;border-bottom:1px solid var(--line);background:var(--card)}
h1{font-size:20px;margin:0 0 6px}h2{font-size:17px;margin:28px 0 10px;padding-bottom:6px;border-bottom:2px solid var(--line)}
h3{font-size:15px;margin:18px 0 8px}main{padding:8px 24px 60px;max-width:1500px;margin:auto}
nav{display:flex;flex-wrap:wrap;gap:6px;margin-top:10px}nav a{font-size:12px;color:var(--accent);text-decoration:none;
border:1px solid var(--line);border-radius:12px;padding:2px 10px}
.cards{display:flex;flex-wrap:wrap;gap:10px;margin:14px 0}.card{background:var(--card);border:1px solid var(--line);
border-radius:8px;padding:10px 14px;min-width:130px}.card b{display:block;font-size:22px}.card span{color:var(--muted);font-size:12px}
table{border-collapse:collapse;width:100%;background:var(--card);font-size:13px;margin:6px 0 14px}
th,td{border:1px solid var(--line);padding:5px 8px;vertical-align:top;text-align:left}th{background:var(--code);white-space:nowrap}
code,pre{font-family:ui-monospace,Consolas,monospace;font-size:12px}pre{background:var(--code);padding:6px 8px;margin:0;
white-space:pre-wrap;word-break:break-word;border-radius:4px}
.pre-del{background:var(--del)}.pre-add{background:var(--add)}
.lv{display:inline-block;padding:0 7px;border-radius:9px;color:#fff;font-size:11px;font-weight:600;white-space:nowrap}
.lv-DIRECT{background:var(--direct)}.lv-INDIRECT{background:var(--indirect)}.lv-POTENTIAL{background:var(--potential)}
.lv-NO_IMPACT_CONFIRMED{background:var(--none)}.p-High{color:var(--direct);font-weight:700}.p-Medium{color:var(--indirect);
font-weight:600}.p-Low{color:var(--muted)}.muted{color:var(--muted)}.warn{border-left:4px solid var(--indirect);
background:var(--card);padding:8px 12px;margin:10px 0}.tag{display:inline-block;border:1px solid var(--line);border-radius:4px;
padding:0 5px;margin:1px;font-size:11px}.ok{color:var(--none);font-weight:600}
details{margin:4px 0}summary{cursor:pointer}.path{font-size:12px}.path .hop{white-space:nowrap}
.path .pot{color:var(--potential);font-style:italic}
.graphwrap{background:var(--card);border:1px solid var(--line);border-radius:8px;overflow:auto;max-height:720px}
.impact-graph text{font-size:11px;fill:var(--fg)}.impact-graph .elabel{font-size:9px;fill:var(--muted)}
.impact-graph .node rect,.impact-graph .node polygon,.impact-graph .node ellipse{fill:var(--card);stroke-width:1.8}
.impact-graph .node rect.inner{fill:none;stroke-width:1}
.impact-graph .lv-DIRECT>rect,.impact-graph .lv-DIRECT>polygon,.impact-graph .lv-DIRECT>ellipse{stroke:var(--direct)}
.impact-graph .lv-INDIRECT>rect,.impact-graph .lv-INDIRECT>polygon,.impact-graph .lv-INDIRECT>ellipse{stroke:var(--indirect)}
.impact-graph .lv-POTENTIAL>rect,.impact-graph .lv-POTENTIAL>polygon,.impact-graph .lv-POTENTIAL>ellipse{stroke:var(--potential)}
.impact-graph .lv-NONE>rect,.impact-graph .lv-NONE>polygon{stroke:var(--muted)}
.impact-graph .k-change polygon{fill:var(--del)}.impact-graph .callctx rect{stroke-dasharray:4 3}
.impact-graph .edge path{stroke:var(--muted);stroke-width:1.3}.impact-graph .e-potential path{stroke:#e8710a;stroke-dasharray:6 4}
.impact-graph .dim{opacity:.12}.legend span{margin-right:14px;font-size:12px}
.tw{overflow-x:auto;margin:6px 0 14px}.tw table{margin:0;min-width:860px}
td pre{min-width:240px}td pre+pre{margin-top:3px}.tag{white-space:nowrap}
select{font:inherit;max-width:100%;padding:2px 6px;background:var(--card);color:var(--fg);border:1px solid var(--line);border-radius:4px}
"""

JS = """
function showGraph(v){document.querySelectorAll('.gview').forEach(function(el){
 el.style.display=(el.getAttribute('data-change')===v)?'block':'none';});}
"""


def lv(level):
    return f'<span class="lv lv-{E(level or "")}">{E(level or "-")}</span>'


def pre(code, cls=""):
    if code is None:
        return '<span class="muted">—</span>'
    return f'<pre class="{cls}">{E(str(code))}</pre>'


def path_html(path):
    hops = []
    for p in path:
        loc = f"{p.get('name') or p.get('function')}" + (f":L{p['line']}" if p.get("line") else "")
        cls = "hop" + ("" if p.get("certain", True) else " pot")
        hops.append(f'<span class="{cls}" title="{E(p.get("reason") or "")}">[{E(p.get("edge_label", ""))}] '
                    f"<b>{E(loc)}</b></span>")
    return '<div class="path">' + " → ".join(hops) + "</div>"


def table(headers, rows):
    h = "".join(f"<th>{E(x)}</th>" for x in headers)
    body = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in rows)
    return (f'<div class="tw"><table><thead><tr>{h}</tr></thead>'
            f"<tbody>{body or '<tr><td colspan=99 class=muted>없음</td></tr>'}</tbody></table></div>")


def render_html(r: dict, svg: str) -> str:
    from .graph_render import to_svg
    s = r["summary"]
    lvl = s["functions_by_level"]
    funcs = r["impact"]["functions"]
    sec = []
    title = f"변경 영향성 분석 보고서 — {r.get('label') or ''}"
    nav = ["요약", "1. 변경 내용", "2. 직접 영향", "3. 간접 영향", "4. 영향 경로", "5. 동작 변화", "6. 잠재 결함",
           "7. 근거·불확실성", "8. 테스트", "9. 수동 리뷰", "영향성 표"]
    anchors = ["sum", "s1", "s2", "s3", "s4", "s5", "s6", "s7", "s8", "s9", "tbl"]

    # ---------------------------------------------------------------- 요약
    comp = r["completeness"]
    warn = "".join(f'<div class="warn">⚠ {E(n)}</div>' for n in comp["notes"])
    cards = [("변경 파일", s["changed_files"]), ("의미 있는 변경", s["semantic_changes"]),
             ("포맷/주석만 변경", s["non_semantic_changes"]), ("DIRECT 함수", lvl["DIRECT"]),
             ("INDIRECT 함수", lvl["INDIRECT"]), ("POTENTIAL 함수", lvl["POTENTIAL"]),
             ("영향 없음 확인", lvl["NO_IMPACT_CONFIRMED"]),
             ("잠재 결함(High)", f"{s['defects']['total']} ({s['defects']['high']})"),
             ("정적 확정 결함", s["defects"]["confirmed"]), ("회귀 테스트(High)", f"{s['tests']['total']} ({s['tests']['high']})")]
    inp = r["inputs"]
    sec.append(f'<h2 id="sum">분석 요약</h2>{warn}<div class="cards">' +
               "".join(f"<div class=card><b>{E(str(v))}</b><span>{E(k)}</span></div>" for k, v in cards) + "</div>" +
               table(["항목", "값"], [
                   ["Baseline", f"<code>{E(inp['baseline'])}</code>"], ["Current", f"<code>{E(inp['current'])}</code>"],
                   ["입력 모드", E(str(inp.get("mode", "dirs")))],
                   ["분석 모드", E(r["analysis_mode"]) + ("" if r["analysis_mode"] == "compdb" else
                                                       " <span class=muted>(compile_commands.json 없음: 제한 모드)</span>")],
                   ["빌드 설정", E(r["build_config"])],
                   ["파싱", f"Current {r['projects']['current']['files']}개 .c / 함수 {r['projects']['current']['functions']}개, "
                          f"오류 {len(r['projects']['current']['failures'])}건"],
                   ["생성 시각", E(r["generated_at"])], ["도구", E(f"{r['tool']['name']} {r['tool']['version']}")]]))

    # ------------------------------------------------------------ 1. 변경
    cs = r["change_summary"]
    files_rows = [[f"<code>{E(f['file'])}</code>", E(f["status"]), f["hunks"], f"+{f['added_lines']} / -{f['removed_lines']}",
                   f["format_only_hunks"]] for f in cs["files"]]
    ch_rows = []
    for c in r["changes"]:
        b, a = c.get("base") or {}, c.get("curr") or {}
        ch_rows.append([f"<b>{E(c['id'])}</b>", E(c["category"]),
                        "".join(f'<span class="tag">{E(x)}</span>' for x in c["labels"]),
                        f"<code>{E((a or b).get('file') or '')}</code><br>{E(c.get('function') or c.get('variable') or c.get('macro_name') or c.get('type_name') or '')}",
                        f"L{b.get('line', '-')} → L{a.get('line', '-')}",
                        pre(b.get("code"), "pre-del") + pre(a.get("code"), "pre-add"), E(c["description"])])
    ns_rows = [[E(n["type"]), E(n.get("function") or n.get("file") or ""), E(str(n.get("line") or n.get("from") or "")),
                E(n.get("note", ""))] for n in r["non_semantic"]]
    sec.append('<h2 id="s1">1. 변경 내용 요약</h2><h3>변경 파일</h3>' +
               table(["파일", "상태", "hunk", "줄 수", "포맷만 변경 hunk"], files_rows) +
               "<h3>의미 있는 변경점</h3>" +
               table(["ID", "분류", "변경 유형", "위치", "라인", "코드 (위: 변경 전 / 아래: 변경 후)", "설명"], ch_rows) +
               "<h3>변경된 조건</h3>" +
               table(["ID", "함수", "변경 전 조건", "변경 후 조건"],
                     [[E(x["id"]), E(x["function"] or ""), pre(x["from"], "pre-del"), pre(x["to"], "pre-add")]
                      for x in cs["changed_conditions"]]) +
               f"<p>변경 함수: {', '.join(f'<code>{E(x)}</code>' for x in cs['changed_functions']) or '-'} / "
               f"변경 변수: {', '.join(f'<code>{E(x)}</code>' for x in cs['changed_variables']) or '-'} / "
               f"변경 매크로: {', '.join(f'<code>{E(x)}</code>' for x in cs['changed_macros']) or '-'}</p>" +
               "<details><summary>의미 변경이 아닌 항목(주석/공백/포맷, 위치 이동)</summary>" +
               table(["유형", "대상", "위치", "비고"], ns_rows) + "</details>")

    # --------------------------------------------------- 2/3. 직접·간접
    def func_rows(level):
        rows = []
        for f in funcs:
            if f["level"] != level:
                continue
            nodes = "".join(f"<div><code>L{n['line']}</code> {E(n['code'][:90])} <span class=muted>[{E(n['edge'])}]</span></div>"
                            for n in f["nodes"][:6])
            if f["call_context_only"]:
                nodes = f"<span class=muted>호출 체인상 상위 함수 — 관측 가능한 효과가 바뀐 " \
                        f"{', '.join(E(v) for v in f['via_callee'])} 를 호출</span>"
            rows.append([f"<b>{E(f['name'])}</b>", f"<code>{E(f['file'])}</code>", lv(f["level"]),
                         f'<span class="p-{f["priority"]}">{f["priority"]}</span>',
                         "".join(f'<span class="tag">{E(x)}</span>' for x in f["edge_labels"]),
                         E(", ".join(f["changes"])), E(", ".join(f["roles"])), nodes,
                         path_html(f["paths"][0]) if f["paths"] else ""])
        return rows

    hdr = ["함수", "파일", "수준", "우선순위", "전파", "관련 변경", "역할", "영향 받는 문장", "대표 전파 경로"]
    sec.append('<h2 id="s2">2. 직접 영향 함수 (DIRECT)</h2>'
               '<p class="muted">변경된 문장 자체, 변경된 정의(매크로·전역·자료형·시그니처)의 사용 위치, 같은 함수 안 1단계 의존 문장.</p>' +
               table(hdr, func_rows("DIRECT")))
    sec.append('<h2 id="s3">3. 간접 영향 함수 (INDIRECT / POTENTIAL)</h2>'
               '<p class="muted">INDIRECT: 확정 간선(호출·데이터·제어)만으로 도달. POTENTIAL: 함수 포인터·포인터 별칭·비활성 코드·'
               '분석 한도 등 불확실 간선 포함 — 영향 가능성을 배제할 수 없음.</p>' +
               table(hdr, func_rows("INDIRECT")) + "<h3>POTENTIAL</h3>" + table(hdr, func_rows("POTENTIAL")) +
               "<h3>NO_IMPACT_CONFIRMED (설정 범위·가정 하 비영향)</h3>" +
               table(["함수", "파일", "근거"], [[E(n["name"]), f"<code>{E(n['file'])}</code>", E(n["reason"])]
                                             for n in r["impact"]["no_impact"]]) +
               "<h3>영향 받는 변수</h3>" +
               table(["변수", "수준", "역할", "쓰기(영향 받은 위치)", "읽기"],
                     [[f"<code>{E(v['name'])}</code>", lv(v["level"]), E(", ".join(v["roles"])),
                       E(", ".join(v["writers"][:8])), E(", ".join(v["readers"][:8]))] for v in r["impact"]["variables"]]) +
               "<h3>영향 받는 출력/외부 호출</h3>" +
               table(["호출 위치", "외부 함수", "호출", "수준", "출력 함수"],
                     [[E(f"{o['function']}:L{o['line']}"), E(o["callee"]), f"<code>{E(o['call'])}</code>", lv(o["level"]),
                       "✔" if o["is_output"] else ""] for o in r["impact"]["outputs"]]))

    # ------------------------------------------------------- 4. 경로/그래프
    pcs = [pc for pc in r["impact"]["per_change"] if pc.get("graph", {}).get("nodes")]
    desc = {c["id"]: c["description"] for c in r["changes"]}
    first = pcs[0]["change"] if pcs else "*"
    opts = "".join(f'<option value="{E(pc["change"])}">{E(pc["change"])} — {E(desc[pc["change"]][:70])}</option>'
                   for pc in pcs) + '<option value="*">전체 변경 통합 그래프</option>'
    views = "".join(f'<div class="gview graphwrap" data-change="{E(pc["change"])}" '
                    f'style="display:{"block" if pc["change"] == first else "none"}">{to_svg(pc["graph"])}</div>'
                    for pc in pcs)
    views += f'<div class="gview graphwrap" data-change="*" style="display:{"block" if first == "*" else "none"}">{svg}</div>'
    pc_html = []
    for pc in r["impact"]["per_change"]:
        rows = [[E(fid), lv(e["level"]), E(", ".join(e["edges"])), path_html(e["path"])]
                for fid, e in sorted(pc["functions"].items(), key=lambda kv: ["DIRECT", "INDIRECT", "POTENTIAL"].index(kv[1]["level"]))]
        outs = ", ".join(sorted({f"{o['callee']}({o['function']}:L{o['line']})" for o in pc["external_outputs"]}))
        pc_html.append(f"<details><summary><b>{E(pc['change'])}</b> {E(', '.join(pc['types'][:3]))} — "
                       f"영향 함수 {len(rows)}개</summary>" + table(["함수", "수준", "전파 간선", "전파 경로(근거)"], rows) +
                       (f"<p>도달하는 외부 출력: {E(outs)}</p>" if outs else "") + "</details>")
    chains = []
    for fid, cl in r["call_chains"].items():
        items = "".join(f"<li>{' → '.join(E(x) for x in c['chain'])}{' <i class=muted>(간접 호출 포함)</i>' if c['potential'] else ''}"
                        f" <span class=muted>[{E(c['entry_reason'])}]</span></li>" for c in cl)
        chains.append(f"<details><summary><code>{E(fid)}</code> → 진입점 체인 {len(cl)}개</summary><ul>{items}</ul></details>")
    sec.append('<h2 id="s4">4. 영향 전파 경로</h2><p class="legend"><span>━ 확정 의존</span>'
               '<span style="color:#e8710a">┅ 불확실 의존(POTENTIAL)</span><span>육각형: 변경</span><span>마름모: 조건</span>'
               '<span>사각형: 함수(점선=호출 체인 상위)</span><span>타원: 공유 변수</span><span>이중 사각: 외부 출력</span></p>'
               f'<p>그래프 선택: <select onchange="showGraph(this.value)">{opts}</select></p>'
               f'{views}<h3>변경점별 영향 경로</h3>{"".join(pc_html)}'
               f'<h3>변경 함수의 상위 호출 체인(진입점까지)</h3>{"".join(chains)}')

    # ---------------------------------------------------- 5. 동작 변화
    beh = []
    for fid, c in r["cfg_comparison"].items():
        rc_rows = [[f"L{x['line']}", f"<code>{E(x['text'][:70])}</code>",
                    "<br>".join(E(" ∧ ".join(p)) for p in (x["base_conditions"] or [])) or "<i>새 문장</i>",
                    "<br>".join(E(" ∧ ".join(p)) for p in x["curr_conditions"]) or "<i>도달 경로 없음</i>"]
                   for x in c["reach_condition_changes"]]
        pa = "".join(f"<li>{E(' ∧ '.join(p['decisions']) or '(무조건)')} ⇒ {E('; '.join(p['actions'][:5]))}"
                     f"{' <b>[경로 조건 모순: 실행 불가 추정]</b>' if p['feasible'] is False else ''}</li>"
                     for p in c["paths_added"])
        pr = "".join(f"<li>{E(' ∧ '.join(p['decisions']) or '(무조건)')} ⇒ {E('; '.join(p['actions'][:5]))}</li>"
                     for p in c["paths_removed"])
        dead = "".join(f"<li>L{x['line']} <code>{E(x['text'])}</code> 항상 {'참' if x['always'] else '거짓'}</li>"
                       for x in c["static_dead_branches_curr"])
        unr = "".join(f"<li>L{x['line']} <code>{E(x['text'])}</code></li>" for x in c["unreachable_curr"])
        if not (rc_rows or pa or pr or dead or unr):
            beh.append(f"<p><code>{E(fid)}</code>: <span class=muted>CFG 경로/도달 조건 변화 없음 "
                       f"(경로 {c['curr']['paths']}개) — 값/계산만 변경</span></p>")
            continue
        beh.append(f"<h3><code>{E(fid)}</code>{' <span class=muted>(' + E(c['reason']) + ')</span>' if c.get('reason') else ''}"
                   f"</h3><p class=muted>CFG 노드 {c['base']['nodes']}→{c['curr']['nodes']}, "
                   f"경로 {c['base']['paths']}→{c['curr']['paths']}{' (한도 도달)' if c['curr']['truncated'] else ''}, "
                   f"추가 경로 {c['n_paths_added']}, 삭제 경로 {c['n_paths_removed']}</p>" +
                   (table(["라인", "문장", "변경 전 도달 조건", "변경 후 도달 조건"], rc_rows) if rc_rows else "") +
                   (f"<p>추가된 실행 경로</p><ul>{pa}</ul>" if pa else "") +
                   (f"<p>삭제된 실행 경로</p><ul>{pr}</ul>" if pr else "") +
                   (f"<p>새로 생긴 고정 결과 분기(도달 불가)</p><ul>{dead}</ul>" if dead else "") +
                   (f"<p>도달 불가 문장</p><ul>{unr}</ul>" if unr else ""))
    bnd = []
    for t in r["tests"]:
        if t["kind"] == "BOUNDARY" and isinstance(t.get("code_behavior"), list):
            rows = [[E(str(x["value"])), "참" if x["before"] else "거짓", "참" if x["after"] else "거짓",
                     "<b>변경</b>" if x["changed"] else ""] for x in t["code_behavior"]]
            bnd.append(f"<h3>{E(t['purpose'])}</h3>" + table(["입력값", "변경 전 조건", "변경 후 조건", ""], rows))
    st_rows = []
    for c in r["changes"]:
        d = c.get("detail") or {}
        if c.get("summary_only") and (d.get("transitions_added") or d.get("transitions_removed")):
            for a, b in d.get("transitions_removed", []):
                st_rows.append([E(c["id"]), E(c.get("function") or ""), E(d.get("variable", "")), E(a), E(b), "", "삭제"])
            for a, b in d.get("transitions_added", []):
                st_rows.append([E(c["id"]), E(c.get("function") or ""), E(d.get("variable", "")), E(a), "", E(b), "추가"])
    st_html = ("<h3>상태 전이 변화</h3>" + table(["변경", "함수", "상태 변수", "현재 상태(case)", "변경 전 다음 상태",
                                                "변경 후 다음 상태", "구분"], st_rows)) if st_rows else ""
    sec.append('<h2 id="s5">5. 변경으로 달라질 수 있는 동작</h2>'
               '<p class=muted>CFG 경로/도달 조건 비교와 코드에서 계산되는 조건 결과입니다. 의도된 동작인지는 사양으로 확인해야 합니다.</p>' +
               "".join(bnd) + st_html + "".join(beh))

    # ------------------------------------------------------ 6. 결함
    d_rows = [[E(d["id"]), f'<span class="p-{d["severity"]}">{d["severity"]}</span>',
               ('<span class="ok">정적 확정</span>' if d["status"] == "CONFIRMED_BY_STATIC_ANALYSIS" else "추가 검증 필요"),
               E(d["category"]), E(d["type"]), E(f"{d.get('function') or ''}" + (f":L{d['line']}" if d.get("line") else "")),
               E(d["description"]), pre(d.get("code")) if d.get("code") else "", E(d.get("evidence") or ""),
               E(d.get("change") or ""), E(d.get("recommendation") or "")] for d in r["defects"]]
    sec.append('<h2 id="s6">6. 잠재 결함 및 위험</h2>' +
               table(["ID", "심각도", "판정", "분류", "유형", "위치", "설명", "코드", "근거", "변경", "권고"], d_rows))

    # ------------------------------------------- 7. 근거와 불확실성
    u = r["uncertainty"]
    sec.append('<h2 id="s7">7. 분석 근거 및 불확실성</h2><h3>가정</h3><ul>' +
               "".join(f"<li>{E(a)}</li>" for a in u["assumptions"]) + "</ul><h3>한계/누락</h3><ul>" +
               "".join(f"<li>{E(a)}</li>" for a in u["limitations"]) + "</ul>" +
               "<h3>간접 호출(함수 포인터) 해석</h3>" +
               table(["호출 위치", "식", "후보 대상", "해석 근거"],
                     [[E(f"{e['caller']}:L{e['line']}"), f"<code>{E(e.get('expr') or '')}</code>", E(e["callee"]),
                       E(e["reason"])] for e in u["potential_indirect_edges"]]) +
               "<h3>파싱 실패 / 분석 누락</h3>" +
               table(["버전", "파일", "진단"], [[v, E(x["file"]), E("; ".join(d["message"] for d in x["diagnostics"][:3]))]
                                              for v in ("baseline", "current") for x in u["parse_failures"][v]]) +
               f"<p>분석되지 않은 헤더: {E(', '.join(u['unparsed_headers']) or '없음')}</p>" +
               f"<p>비활성(#if) 영역: {E(', '.join(f'{a}:{b}-{c}' for a, b, c in u['inactive_regions']) or '없음')}</p>" +
               f"<p>탐색 중단(한도): {len(u['truncated'])}건</p>" +
               f"<p>외부(정의 없는) 함수: {E(', '.join(u['external_functions']) or '없음')}</p>")

    # ---------------------------------------------------- 8. 테스트
    t_rows = []
    for t in r["tests"]:
        cb = ""
        if t.get("code_behavior") is not None:
            cb = f"<details><summary>코드 기준 동작</summary><pre>{E(json.dumps(t['code_behavior'], ensure_ascii=False, indent=1)[:1500])}</pre></details>"
        exp = E(t["expected"]) if t["expected_source"] == "requirement" else f'<span class="muted">{E(t["expected"])}</span>'
        t_rows.append([E(t["id"]), f'<span class="p-{t["priority"]}">{t["priority"]}</span>', E(t["kind"]),
                       E(t["purpose"]), E(", ".join(t["target_functions"])),
                       "<br>".join(E(x) for x in t["drive_from"][:2]),
                       "<br>".join(E(x) for x in t["inputs"][:6]),
                       "<br>".join(E(x) for x in t["preconditions"]),
                       "<ol>" + "".join(f"<li>{E(x)}</li>" for x in t["scenario"][:8]) + "</ol>",
                       exp + (f"<br><i>{E(t['code_expected_hint'])}</i>" if t.get("code_expected_hint") else "") + cb,
                       E(t["reason"][:300]), E(", ".join(t["related_changes"])),
                       E(" → ".join(t["impact_path"][:6])),
                       E(", ".join(x["id"] for x in t["requirements"])),
                       E(", ".join(x["id"] for x in t["existing_tests"]))])
    req = r["requirements_impacted"]
    sec.append('<h2 id="s8">8. 검증이 필요한 함수와 테스트 케이스</h2>' +
               (("<h3>영향 받는 요구사항</h3>" + table(["요구사항", "제목", "수준", "관련 함수", "사양상 기대 동작"],
                                                    [[E(x["id"]), E(x["title"]), lv(x["level"]), E(", ".join(x["functions"])),
                                                      E(x.get("expected") or "")] for x in req])) if req else
                "<p class=muted>요구사항 매핑 정보가 제공되지 않아 예상 결과를 확정하지 않았습니다.</p>") +
               table(["ID", "우선", "종류", "목적", "대상", "구동 경로", "입력", "사전 조건", "시나리오", "예상 결과",
                      "필요 이유", "관련 변경", "영향 경로", "요구사항", "기존 테스트"], t_rows))

    # ----------------------------------------------- 9. 수동 리뷰
    sec.append('<h2 id="s9">9. 추가 수동 리뷰가 필요한 항목</h2>' +
               table(["항목", "사유", "참조"], [[E(m["item"]), E(m["reason"]), E(str(m["ref"]))] for m in r["manual_review"]]))

    # ------------------------------------------------- 영향성 표
    sec.append('<h2 id="tbl">영향성 분석 결과 표</h2>' +
               table(["변경 ID", "변경 위치", "변경 유형", "영향 함수", "영향 파일", "영향 경로", "의존", "영향 수준",
                      "영향 근거", "우선순위"],
                     [[E(t["change"]), f"<code>{E(t['location'])}</code>",
                       E(t["change_category"]) + "<br><span class=muted>" + E(", ".join(t["change_types"][:3])) + "</span>",
                       f"<b>{E(t['function_name'])}</b>", f"<code>{E(t['file'] or '')}</code>",
                       E(", ".join(t["path_type"])), E(" / ".join(t["dependency"])), lv(t["level"]),
                       f'<div class="path">{E(t["evidence"])}</div>', f'<span class="p-{t["priority"]}">{t["priority"]}</span>']
                      for t in r["impact_table"]]))

    nav_html = "".join(f'<a href="#{a}">{E(n)}</a>' for a, n in zip(anchors, nav))
    return (f'<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" '
            f'content="width=device-width, initial-scale=1"><title>변경 영향성 분석</title><style>{CSS}</style>'
            f"<script>{JS}</script></head><body><header><h1>{E(title)}</h1>"
            f'<div class="muted">AST · CFG · Call Graph · Def-Use 기반 Embedded C 변경 영향성 분석 / SW 검증 리뷰용</div>'
            f"<nav>{nav_html}</nav></header><main>{''.join(sec)}</main></body></html>")
