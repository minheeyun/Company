"""폴더 비교 결과를 검증자용 하나의 HTML 파일(report.html)로 만든다.

화면 구성
  상단 요약     비교 버전, 수정 파일 수, 분석한 C 파일 수, 분석 범위, 주요 동작 변경, 미완료 검증
  변경 검토     변경 목록(기능 중심 제목 · 관련 함수 · 우선순위 · 판정 · 검토 상태)과
                변경마다 같은 ① 변경 내용 → ② 스크립트가 확인한 것 → ③ 발견 사항 → ④ 추가 검증
  발견 사항     같은 원인끼리 묶은 문제 목록 (판정 = 근거 수준, 비교 상태 = 신규·기존·해결·판정 보류)
  검증 기록     시험·판단 항목마다 담당자 · 결과 · 증거 · 판정 근거 (결과만 고르면 완료가 되지 않음)
  근거 상세     함수 전체 코드 차이, 전체 영향 경로, 자동 분류 규칙 원문, 분석 설정·도구 실행 기록
변경(C) · 검사(K) · 발견(V/F) · 시험(T) 은 ID 로 연결되어 서로 이동할 수 있다.

외부 파일·인터넷 연결 없이 열리도록 데이터(JSON)와 화면 코드(CSS·JS)를 모두 한 파일에 넣는다.
검증 기록은 브라우저(localStorage)에 저장되고 CSV 로 내보낼 수 있다.
"""

from __future__ import annotations

import json


def render(data: dict) -> str:
    payload = json.dumps(data, ensure_ascii=False, default=str)
    # </script> 등이 데이터 안에 있어도 스크립트 블록이 끝나지 않게 한다
    payload = payload.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    return _TEMPLATE.replace("__DATA__", payload)


_TEMPLATE = r"""<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>변경 검토 보고서</title>
<style>
:root {
  --bg: #f6f7f9; --panel: #ffffff; --text: #1d2330; --muted: #5d6677; --line: #dfe3ea;
  --accent: #2f5bd3; --accent-bg: #e8eefc; --code-bg: #f1f3f7; --row-hover: #f3f6fc;
  --p0: #b42318; --p0-bg: #fdecea; --p1: #9a5b00; --p1-bg: #fff3df; --p2: #6b3fb3; --p2-bg: #f1eafb;
  --p3: #1f6f4a; --p3-bg: #e5f5ec; --p4: #4a5363; --p4-bg: #eef0f3;
  --add: #1f6f4a; --add-bg: #e6f6ec; --del: #b42318; --del-bg: #fdecea; --chg-bg: #fff6dc;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --bg: #14171c; --panel: #1c2027; --text: #e4e7ec; --muted: #9aa3b2; --line: #2e343e;
    --accent: #8aa8ff; --accent-bg: #23304d; --code-bg: #252a33; --row-hover: #232935;
    --p0: #ff8a7a; --p0-bg: #3d1f1c; --p1: #f3b75a; --p1-bg: #3a2c14; --p2: #c4a3ff; --p2-bg: #2e2440;
    --p3: #6fd3a0; --p3-bg: #183326; --p4: #b3bac6; --p4-bg: #2a2f37;
    --add: #6fd3a0; --add-bg: #17301f; --del: #ff8a7a; --del-bg: #3a1d1b; --chg-bg: #33301a;
  }
}
:root[data-theme="dark"] {
  --bg: #14171c; --panel: #1c2027; --text: #e4e7ec; --muted: #9aa3b2; --line: #2e343e;
  --accent: #8aa8ff; --accent-bg: #23304d; --code-bg: #252a33; --row-hover: #232935;
  --p0: #ff8a7a; --p0-bg: #3d1f1c; --p1: #f3b75a; --p1-bg: #3a2c14; --p2: #c4a3ff; --p2-bg: #2e2440;
  --p3: #6fd3a0; --p3-bg: #183326; --p4: #b3bac6; --p4-bg: #2a2f37;
  --add: #6fd3a0; --add-bg: #17301f; --del: #ff8a7a; --del-bg: #3a1d1b; --chg-bg: #33301a;
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--text);
  font: 14px/1.55 "Malgun Gothic", "Apple SD Gothic Neo", "Noto Sans KR", system-ui, sans-serif; }
header { padding: 16px 24px 14px; background: var(--panel); border-bottom: 1px solid var(--line); }
header h1 { margin: 0; font-size: 20px; }
header .sub { color: var(--muted); font-size: 13px; margin-top: 2px; }
.ver { font-size: 22px; font-weight: 700; letter-spacing: .3px; }
.ver .arr { color: var(--accent); margin: 0 6px; }
.stats { display: grid; grid-template-columns: repeat(auto-fill, minmax(150px, 1fr)); gap: 8px; margin: 12px 0 10px; }
.stat { border: 1px solid var(--line); border-radius: 8px; padding: 8px 12px; background: var(--bg); }
.stat .v { font-size: 20px; font-weight: 700; }
.stat .k { color: var(--muted); font-size: 12px; }
.stat .s { color: var(--muted); font-size: 11.5px; }
.stat.link { cursor: pointer; } .stat.link:hover { border-color: var(--accent); }
.scope { border-left: 4px solid var(--accent); background: var(--accent-bg); padding: 7px 12px; border-radius: 6px; font-size: 13px; }
.cols { display: grid; grid-template-columns: 1.4fr 1fr; gap: 12px; margin-top: 10px; }
@media (max-width: 900px) { .cols { grid-template-columns: 1fr; } }
.box { background: var(--panel); border: 1px solid var(--line); border-radius: 8px; padding: 10px 14px; }
.box h3 { margin: 0 0 6px; font-size: 14px; }
.box ul { margin: 0; padding-left: 0; list-style: none; }
.box li { padding: 3px 0; border-bottom: 1px dashed var(--line); }
.box li:last-child { border-bottom: 0; }
.pend .x { color: var(--p1); font-weight: 700; margin-right: 6px; } .pend .o { color: var(--p3); font-weight: 700; margin-right: 6px; }
.banner { margin-top: 8px; font-size: 12.5px; color: var(--muted); }
.banner.warn { color: var(--p1); font-weight: 600; }
nav { position: sticky; top: 0; z-index: 5; background: var(--panel); border-bottom: 1px solid var(--line);
  display: flex; flex-wrap: wrap; gap: 2px; padding: 0 16px; }
nav button { border: 0; background: none; color: var(--muted); padding: 10px 12px; cursor: pointer;
  font: inherit; border-bottom: 2px solid transparent; }
nav button:hover { color: var(--text); }
nav button.on { color: var(--accent); border-bottom-color: var(--accent); font-weight: 600; }
nav .cnt { font-size: 12px; margin-left: 4px; color: var(--muted); }
main { padding: 16px 24px 48px; max-width: 1600px; }
section { display: none; } section.on { display: block; }
h2 { font-size: 17px; margin: 0 0 4px; }
.desc { color: var(--muted); margin: 0 0 10px; font-size: 13px; }
.toolbar { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; margin: 6px 0 8px; }
.toolbar input[type=search], .toolbar select { font: inherit; padding: 5px 8px; border: 1px solid var(--line); border-radius: 6px;
  background: var(--panel); color: var(--text); }
.toolbar input[type=search] { min-width: 240px; }
.toolbar .count { color: var(--muted); font-size: 13px; }
.seg { display: inline-flex; border: 1px solid var(--line); border-radius: 8px; overflow: hidden; }
.seg button { border: 0; border-right: 1px solid var(--line); background: var(--panel); color: var(--text); padding: 5px 12px; cursor: pointer; font: inherit; }
.seg button:last-child { border-right: 0; }
.seg button.on { background: var(--accent); color: #fff; font-weight: 600; }
.btn { font: inherit; border: 1px solid var(--line); background: var(--panel); color: var(--accent); border-radius: 6px; padding: 4px 10px; cursor: pointer; }
.tw { overflow-x: auto; background: var(--panel); border: 1px solid var(--line); border-radius: 8px; }
table { border-collapse: collapse; width: 100%; }
th, td { text-align: left; vertical-align: top; padding: 6px 9px; border-bottom: 1px solid var(--line); }
th { background: var(--panel); font-size: 12px; color: var(--muted); font-weight: 600; white-space: nowrap; }
tr.click { cursor: pointer; } tr.click:hover td { background: var(--row-hover); }
code, .code { font-family: Consolas, "D2Coding", "Courier New", monospace; font-size: 12.5px; background: var(--code-bg);
  padding: 1px 5px; border-radius: 4px; word-break: break-all; }
.muted { color: var(--muted); } .small { font-size: 12px; } .nowrap { white-space: nowrap; }
.tag { display: inline-block; padding: 1px 8px; border-radius: 10px; font-size: 12px; white-space: nowrap; font-weight: 600; margin-right: 4px; }
.t0 { color: var(--p0); background: var(--p0-bg); } .t1 { color: var(--p1); background: var(--p1-bg); }
.t2 { color: var(--p2); background: var(--p2-bg); } .t3 { color: var(--p3); background: var(--p3-bg); }
.t4 { color: var(--p4); background: var(--p4-bg); } .ta { color: var(--accent); background: var(--accent-bg); }
.idb { font-family: Consolas, monospace; font-weight: 700; font-size: 12.5px; color: var(--accent); }
.lnk { color: var(--accent); cursor: pointer; text-decoration: underline; background: none; border: 0; font: inherit; padding: 0; }
.idl { font-family: Consolas, monospace; font-size: 12px; margin-right: 6px; }
.ccard { background: var(--panel); border: 1px solid var(--line); border-left: 5px solid var(--p4); border-radius: 8px; margin: 14px 0; }
.ccard.p높음 { border-left-color: var(--p0); } .ccard.p중간 { border-left-color: var(--p1); }
.ccard > .hd { padding: 10px 14px 8px; cursor: pointer; }
.ccard > .hd h3 { margin: 4px 0 2px; font-size: 16px; }
.ccard .lead { margin: 6px 0 2px; }
.ccard .loc { color: var(--muted); font-size: 12.5px; }
.ccard > .bd { padding: 0 14px 12px; border-top: 1px solid var(--line); }
.ccard.closed > .bd { display: none; }
h4.step { margin: 14px 0 6px; padding: 5px 10px; background: var(--accent-bg); color: var(--accent); border-radius: 6px; font-size: 13.5px; }
h4.step .q { font-weight: 400; color: var(--muted); margin-left: 8px; font-size: 12px; }
h5 { margin: 10px 0 4px; font-size: 13px; }
table.cmp td:first-child { width: 24%; color: var(--muted); }
table.cmp tr.beh td { background: var(--chg-bg); font-weight: 600; }
table.cmp tr.beh td:first-child { color: var(--text); }
.diff { width: 100%; font-family: Consolas, "D2Coding", monospace; font-size: 12px; border: 1px solid var(--line); border-radius: 6px; table-layout: fixed; }
.diff td { padding: 1px 6px; border: 0; white-space: pre-wrap; word-break: break-all; }
.diff td.n { width: 44px; color: var(--muted); text-align: right; user-select: none; }
.diff tr.del td.o, .diff tr.chg td.o { background: var(--del-bg); }
.diff tr.add td.w, .diff tr.chg td.w { background: var(--add-bg); }
.diff tr.gap td { color: var(--muted); text-align: center; background: var(--bg); }
.diff tr.ind td.o, .diff tr.ind td.w { background: var(--chg-bg); }
.diff th { font-family: inherit; }
.note { font-size: 12.5px; color: var(--p1); background: var(--p1-bg); padding: 4px 10px; border-radius: 6px; margin: 4px 0; }
.ok { font-size: 12.5px; color: var(--p3); background: var(--p3-bg); padding: 5px 10px; border-radius: 6px; }
.fcard { border: 1px solid var(--line); border-left: 4px solid var(--p1); border-radius: 6px; padding: 8px 12px; margin: 8px 0; background: var(--bg); }
.fcard.j0 { border-left-color: var(--p0); } .fcard.j3 { border-left-color: var(--p4); }
.fcard h5 { margin: 0 0 6px; font-size: 14px; }
.kv { display: grid; grid-template-columns: 150px 1fr; gap: 3px 12px; font-size: 13px; }
.kv > div:nth-child(odd) { color: var(--muted); }
tr.rec td { background: var(--bg); padding-top: 4px; padding-bottom: 8px; }
.recf { display: grid; grid-template-columns: 120px 120px 1fr 1.4fr; gap: 6px; align-items: start; }
.recf input, .recf select, .recf textarea { font: inherit; font-size: 12.5px; padding: 3px 6px; border: 1px solid var(--line);
  border-radius: 5px; background: var(--panel); color: var(--text); width: 100%; }
.recf textarea { min-height: 28px; resize: vertical; }
.recf label { font-size: 11px; color: var(--muted); display: block; }
@media (max-width: 900px) { .recf { grid-template-columns: 1fr 1fr; } }
details.ev { background: var(--panel); border: 1px solid var(--line); border-radius: 8px; margin: 8px 0; padding: 6px 12px; }
details.ev > summary { cursor: pointer; font-weight: 600; }
details.more > summary { cursor: pointer; color: var(--accent); font-size: 13px; margin: 6px 0; }
ol.path { margin: 4px 0 8px; padding-left: 22px; font-size: 12.5px; }
.hl { animation: hl 1.6s ease-out; }
@keyframes hl { from { box-shadow: 0 0 0 3px var(--accent); } to { box-shadow: 0 0 0 0 transparent; } }
.legend td:first-child { white-space: nowrap; }
</style>
</head>
<body>
<header id="top"></header>
<nav id="tabs"></nav>
<main id="main"></main>
<script type="application/json" id="data">__DATA__</script>
<script>
var D = JSON.parse(document.getElementById("data").textContent);
(function () {
"use strict";
var ST = D.story || {overview: {counts: {}}, changes: [], findings: [], tests: []};
var OV = ST.overview || {counts: {}};
var A = D.analysis;
var M = D.meta || {};
var CH = {}, FI = {}, TE = {};
ST.changes.forEach(function (c) { CH[c.id] = c; });
ST.findings.forEach(function (f) { FI[f.id] = f; });
ST.tests.forEach(function (t) { TE[t.id] = t; });

// ---------------------------------------------------------------- helpers
function el(tag, attrs, kids) {
  var e = document.createElement(tag);
  if (attrs) Object.keys(attrs).forEach(function (k) {
    var v = attrs[k];
    if (v == null || v === false) return;
    if (k === "text") e.textContent = v;
    else if (k === "class") e.className = v;
    else if (k.slice(0, 2) === "on") e.addEventListener(k.slice(2), v);
    else e.setAttribute(k, v === true ? "" : v);
  });
  (kids || []).forEach(function (c) { if (c == null) return; e.appendChild(typeof c === "string" ? document.createTextNode(c) : c); });
  return e;
}
function frag(kids) { var f = document.createDocumentFragment(); kids.forEach(function (k) { if (k != null) f.appendChild(typeof k === "string" ? document.createTextNode(k) : k); }); return f; }
function code(t) { return el("code", {text: t == null ? "" : String(t)}); }
// `백틱` 을 code 로 바꿔 보여 준다
function rich(t) {
  var parts = String(t == null ? "" : t).split("`"), out = [];
  parts.forEach(function (p, i) { if (p) out.push(i % 2 ? code(p) : document.createTextNode(p)); });
  return frag(out);
}
function span(t, cls) { return el("span", {class: cls, text: t}); }
function table(heads, rows, cls) {
  return el("div", {class: "tw"}, [el("table", {class: cls || ""}, [el("thead", {}, [el("tr", {}, heads.map(function (h) { return el("th", {text: h}); }))]),
    el("tbody", {}, rows)])]);
}
function td(x, cls) { return el("td", {class: cls || ""}, [typeof x === "string" ? rich(x) : x]); }
var PRIO_T = {"높음": "t0", "중간": "t1", "낮음": "t4"};
var JUDGE_T = {"실행 재현 확인": "t0", "분석 모델 내 위반 확인": "t0", "위험 후보": "t1", "분석 불충분": "t4", "변경 사실 확인": "ta"};
var CMP_T = {"신규": "t0", "기존": "t4", "해결": "t3", "판정 보류": "t2", "-": "t4"};
function prioTag(p) { return span("우선순위 " + p, "tag " + (PRIO_T[p] || "t4")); }
function judgeTag(j) { return span(j, "tag " + (JUDGE_T[j] || "t4")); }
function cmpTag(c) { return c && c !== "-" ? span("비교: " + c, "tag " + (CMP_T[c] || "t4")) : null; }
function idLink(id) {
  var go = id[0] === "C" ? goChange : id[0] === "T" ? goTest : (id[0] === "V" || id[0] === "F") ? goFinding : null;
  return go ? el("button", {class: "lnk idl", text: id, onclick: function (e) { e.stopPropagation(); go(id); }}) : span(id, "idl");
}
function flash(node) { if (!node) return; node.classList.remove("hl"); void node.offsetWidth; node.classList.add("hl"); node.scrollIntoView({behavior: "smooth", block: "start"}); }

// ---------------------------------------------------------------- 검증 기록 저장
var KEY = "cia-verify|" + (M.time || "") + "|" + (M.new_dir || "");
var REC = {};
try { REC = JSON.parse(localStorage.getItem(KEY) || "{}") || {}; } catch (e) { REC = {}; }
function save() { try { localStorage.setItem(KEY, JSON.stringify(REC)); } catch (e) {} }
function rec(id) { return REC[id] || {owner: "", result: "", evidence: "", basis: ""}; }
var DONE_RESULTS = ["적합", "부적합", "해당 없음"];
function testStatus(id) {
  var r = rec(id);
  var filled = (r.evidence || "").trim() && (r.basis || "").trim();
  if (DONE_RESULTS.indexOf(r.result) >= 0 && filled) return "완료";
  if (DONE_RESULTS.indexOf(r.result) >= 0) return "증거 필요";
  if (r.result || r.owner || r.evidence || r.basis) return "진행 중";
  return "미수행";
}
var ST_T = {"완료": "t3", "증거 필요": "t1", "진행 중": "t2", "미수행": "t4"};
function changeStatus(c) {
  var ids = c.tests || [], n = ids.length, done = 0, bad = 0, any = 0;
  ids.forEach(function (t) { var s = testStatus(t); if (s === "완료") done++; if (s !== "미수행") any++; if (rec(t).result === "부적합") bad++; });
  if (!n) return {label: "검증 항목 없음", cls: "t4"};
  if (done === n) return {label: bad ? "완료 · 부적합 " + bad : "완료", cls: bad ? "t0" : "t3"};
  if (any) return {label: "진행 중 " + done + "/" + n, cls: "t2"};
  return {label: "미검토 0/" + n, cls: "t4"};
}
var listeners = [];
function onRecord(fn) { listeners.push(fn); }
function changed() { save(); listeners.forEach(function (f) { f(); }); }

function recordRow(t, ncol) {
  var r = rec(t.id);
  function upd(k) {
    return function (e) {
      var o = Object.assign({}, rec(t.id)); o[k] = e.target.value; o.at = new Date().toISOString(); REC[t.id] = o;
      // 같은 시험의 다른 탭 입력칸도 맞춘다
      Array.prototype.forEach.call(document.querySelectorAll('tr.rec[data-rec="' + t.id + '"]'), function (row) {
        if (row.contains(e.target)) return;
        var f = row.querySelectorAll("input, select, textarea");
        f[0].value = o.owner || ""; f[1].value = o.result || ""; f[2].value = o.evidence || ""; f[3].value = o.basis || "";
      });
      changed();
    };
  }
  var sel = el("select", {onchange: upd("result")}, ["", "적합", "부적합", "보류", "해당 없음"].map(function (v) {
    var o = el("option", {value: v, text: v || "결과 선택"}); if (v === r.result) o.selected = true; return o; }));
  return el("tr", {class: "rec", "data-rec": t.id}, [el("td", {colspan: ncol}, [el("div", {class: "recf"}, [
    el("div", {}, [el("label", {text: "담당자"}), el("input", {type: "text", value: r.owner || "", placeholder: "이름", onchange: upd("owner")})]),
    el("div", {}, [el("label", {text: "결과"}), sel]),
    el("div", {}, [el("label", {text: "증거 (시험 로그·파일·커밋)"}), el("input", {type: "text", value: r.evidence || "", placeholder: "예: HIL_0312.log #45", onchange: upd("evidence")})]),
    el("div", {}, [el("label", {text: "판정 근거"}), el("textarea", {placeholder: "기대값·요구사항 ID 와 관찰 결과", onchange: upd("basis")}, [document.createTextNode(r.basis || "")])])])])]);
}
function statusCell(id) {
  var c = el("td", {class: "nowrap", "data-st": id});
  function draw() { var s = testStatus(id); c.textContent = ""; c.appendChild(span(s, "tag " + ST_T[s])); }
  draw(); onRecord(draw);
  return c;
}

// ---------------------------------------------------------------- 상단 요약
function header() {
  var h = document.getElementById("top");
  var v = OV.versions || {old: "변경 전", new: "변경 후"};
  var fc = OV.files || {}, an = OV.analyzed || {}, cn = OV.counts || {};
  h.appendChild(el("div", {}, [el("h1", {text: "변경 검토 보고서"}),
    el("div", {class: "ver"}, [span(v.old), span("→", "arr"), span(v.new)]),
    el("div", {class: "sub", text: (M.old_dir || "") + "  →  " + (M.new_dir || "") + "   ·   생성 " + (M.time || "")})]));
  var progress = el("div", {class: "v"});
  function drawProg() { var n = ST.tests.length, d = ST.tests.filter(function (t) { return testStatus(t.id) === "완료"; }).length; progress.textContent = d + " / " + n; }
  drawProg(); onRecord(drawProg);
  function stat(v2, k, s, go) { return el("div", {class: "stat" + (go ? " link" : ""), onclick: go || null}, [el("div", {class: "v", text: String(v2)}), el("div", {class: "k", text: k}), s ? el("div", {class: "s", text: s}) : null]); }
  h.appendChild(el("div", {class: "stats"}, [
    stat((fc.modified || 0) + "개", "수정 파일", "추가 " + (fc.added || 0) + " · 삭제 " + (fc.removed || 0) + " · .c " + ((OV.c_files || {}).modified || 0) + " / .h " + ((OV.c_files || {}).headers || 0)),
    stat((an.old || 0) + " / " + (an.new || 0), "분석한 C 파일 (변경 전 / 변경 후)"),
    stat((cn.changes || 0) + "건", "변경", "동작이 달라지는 변경 " + (cn.behavior_changes || 0) + "건", function () { show("changes"); }),
    stat((cn.findings || 0) + "건", "고유 문제 (같은 원인 묶음)", null, function () { show("findings"); }),
    stat((cn.review_flags || 0) + "건", "자동 검토 항목 지적", "25개 검토 규칙 기준"),
    stat((cn.impact_paths || 0) + "개", "영향 경로 (정적 분석 후보)"),
    el("div", {class: "stat link", onclick: function () { show("verify"); }}, [progress, el("div", {class: "k", text: "검증 완료 (결과·증거·근거 기록)"}),
      el("div", {class: "s", text: "시험 " + (cn.tests || 0) + " · 판단 " + (cn.questions || 0)})])]));
  var why = (OV.scope_reason || []).join(", ");
  h.appendChild(el("div", {class: "scope"}, [el("b", {text: "분석 범위: "}),
    OV.scope === "changed" ? frag(["변경 파일과 변경 헤더를 사용하는 파일에 한정한 분석입니다" + (why ? " (" + why + ")" : "") + ". ",
      span("다른 C 파일에서 같은 전역 변수·함수를 쓰는 영향은 포함되지 않습니다.", "muted")])
      : frag(["전체 C 파일 분석" + (why ? " (" + why + ")" : "")])]));
  var major = el("ul", {}, (OV.major || []).map(function (id) {
    var c = CH[id]; if (!c) return null;
    return el("li", {}, [prioTag(c.priority), idLink(id), el("b", {text: c.title}), " ", span(c.func + "() · " + c.file, "small muted")]);
  }));
  var pend = el("ul", {class: "pend"}, (OV.pending || []).map(function (p) {
    return el("li", {}, [span(p.done ? "✓" : "!", p.done ? "o" : "x"), el("b", {text: p.item + ": "}), span(p.status)]);
  }));
  h.appendChild(el("div", {class: "cols"}, [el("div", {class: "box"}, [el("h3", {text: "주요 동작 변경"}), major]),
    el("div", {class: "box"}, [el("h3", {text: "미완료 검증"}), pend])]));
  var warn = [];
  ((M.warnings) || []).forEach(function (w) { if (w.indexOf("--scope changed") < 0) warn.push(w); });
  h.appendChild(el("div", {class: "banner"}, ["‘문제 없음’은 이번 분석 범위에서 해당 규칙의 위반이 탐지되지 않았다는 뜻이며, 안전이 확인되었다는 뜻이 아닙니다. 정적 분석 결과는 실제 실행으로 확인되기 전까지 후보입니다."]));
  warn.forEach(function (w) { h.appendChild(el("div", {class: "banner warn", text: "⚠ " + w})); });
}

// ---------------------------------------------------------------- 탭
var panes = {}, btns = {};
function addTab(id, label, cnt, build) {
  var b = el("button", {onclick: function () { show(id); }}, [label, cnt != null ? span(String(cnt), "cnt") : null]);
  document.getElementById("tabs").appendChild(b);
  var s = el("section", {id: "s-" + id});
  document.getElementById("main").appendChild(s);
  panes[id] = {sec: s, build: build, built: false}; btns[id] = b;
}
function show(id) {
  Object.keys(panes).forEach(function (k) { panes[k].sec.classList.toggle("on", k === id); btns[k].classList.toggle("on", k === id); });
  var p = panes[id]; if (!p.built) { p.built = true; p.build(p.sec); }
  try { history.replaceState(null, "", "#" + id); } catch (e) {}
}

// ---------------------------------------------------------------- 코드 차이
function diffTable(rows) {
  if (!rows || !rows.length) return span("코드 차이를 표시할 수 없습니다 (원본 파일을 읽지 못함).", "muted small");
  return el("table", {class: "diff"}, [el("colgroup", {}, [el("col", {style: "width:44px"}), el("col", {}), el("col", {style: "width:44px"}), el("col", {})]),
    el("thead", {}, [el("tr", {}, [el("th", {text: ""}), el("th", {text: "변경 전"}), el("th", {text: ""}), el("th", {text: "변경 후  (빨강 삭제 · 초록 추가 · 노랑 같은 문장의 위치·들여쓰기 변경)"})])]),
    el("tbody", {}, rows.map(function (r) {
      if (r[0] === "gap") return el("tr", {class: "gap"}, [el("td", {colspan: 4, text: "⋯"})]);
      return el("tr", {class: r[0]}, [el("td", {class: "n", text: String(r[1])}), el("td", {class: "o", text: r[2]}),
        el("td", {class: "n", text: String(r[3])}), el("td", {class: "w", text: r[4]})]);
    }))]);
}

// ---------------------------------------------------------------- 발견 사항 카드
function findingCard(f) {
  var ji = ["실행 재현 확인", "분석 모델 내 위반 확인"].indexOf(f.judgment) >= 0 ? "j0" : f.judgment === "분석 불충분" ? "j3" : "";
  var rows = [
    ["확인된 사실", rich(f.fact)], ["문제가 될 수 있는 상황", rich(f.situation)], ["예상 영향", rich(f.impact)],
    ["현재 판정", frag([judgeTag(f.judgment), cmpTag(f.comparison)])],
    ["근거 수준", rich(f.evidence_level.join(" · "))], ["남은 판단", rich(f.remaining)],
    ["연결 시험", f.tests.length ? frag(f.tests.map(idLink)) : span("연결된 시험 없음", "muted")],
  ];
  if (f.merged && f.merged.length) rows.push(["함께 묶인 항목", rich(f.merged.join(", ") + " (같은 위치·같은 원인)")]);
  rows.push(["원래 분류", el("button", {class: "lnk small", text: "근거 상세에서 자동 분류 규칙 보기", onclick: function () { goEvidence(f.change, "EF-" + f.id); }})]);
  var kv = el("div", {class: "kv"}); rows.forEach(function (r) { kv.appendChild(el("div", {text: r[0]})); kv.appendChild(el("div", {}, [r[1]])); });
  return el("div", {class: "fcard " + ji, id: "F-" + f.id}, [el("h5", {}, [span(f.id, "idb"), " ", rich(f.title)]), kv]);
}

// ---------------------------------------------------------------- 변경 카드 (① → ④)
var STATUS_T = function (s) { return /^분석 결과 있음/.test(s) ? "ta" : /^(미실행|결과 미첨부)/.test(s) ? "t1" : "t4"; };
function changeCard(c) {
  var cs = changeStatus(c);
  var stTag = span(cs.label, "tag " + cs.cls);
  onRecord(function () { var x = changeStatus(c); stTag.textContent = "검토: " + x.label; stTag.className = "tag " + x.cls; });
  stTag.textContent = "검토: " + cs.label;
  var card = el("article", {class: "ccard p" + c.priority + (c.priority === "낮음" ? " closed" : ""), id: "C-" + c.id, "data-id": c.id});
  var hd = el("div", {class: "hd", onclick: function () { card.classList.toggle("closed"); }}, [
    el("div", {}, [span(c.id, "idb"), " ", prioTag(c.priority), judgeTag(c.judgment), cmpTag(c.comparison), stTag]),
    el("h3", {text: c.title}),
    el("p", {class: "lead"}, [rich(c.sentences.join(" "))]),
    el("div", {class: "loc"}, [code(c.func + "()"), " · " + c.file + ":" + c.lo + (c.hi !== c.lo ? "–" + c.hi : "") + "  ·  변경 의도: ", el("b", {text: c.intent})])]);
  var bd = el("div", {class: "bd"});
  // ①
  bd.appendChild(el("h4", {class: "step"}, ["① 무엇이 변경되었는가", span("동작 설명 → 변경 전후 비교 → 영향 범위", "q")]));
  bd.appendChild(table(["비교 항목", "변경 전", "변경 후"], c.compare.concat(c.compare_more || []).map(function (r) {
    return el("tr", {class: r.behavior ? "beh" : ""}, [td(r.item), td(r.before), td(r.after)]);
  }), "cmp"));
  bd.appendChild(el("h5", {text: "변경 전후 코드"}));
  bd.appendChild(diffTable(c.code));
  if (c.code_full && c.code_full.length) bd.appendChild(el("details", {class: "more"}, [el("summary", {text: "함수 전체 비교 펼치기"}), diffTable(c.code_full)]));
  var imp = c.impact || {core: [], paths: []};
  bd.appendChild(el("h5", {text: "영향 범위"}));
  if (imp.core.length) {
    imp.core.forEach(function (k, i) {
      if (i) bd.appendChild(span("다른 영향 경로", "small muted"));
      bd.appendChild(table(["단계", "관련 함수", "영향이 연결되는 근거", "연결 방식"], k.stages.map(function (s) {
        return el("tr", {}, [td(s.stage, "nowrap"), el("td", {}, [code(s.func + "()"), el("div", {class: "small muted", text: s.file + ":" + s.line})]), td(s.basis), td(s.link, "small")]);
      })));
    });
    bd.appendChild(el("div", {class: "note", text: imp.note}));
  } else if (imp.n_paths) {
    bd.appendChild(el("div", {class: "small"}, [rich("다른 함수로 이어지는 경로 없음 — `" + c.func + "()` 안에서 변경 문장을 쓰는 문장 " + imp.n_paths + "곳 (" + (imp.note || "") + ")")]));
  } else {
    bd.appendChild(el("div", {class: "small muted", text: "이번 분석 범위에서 다른 문장·함수로 이어지는 의존 경로가 탐지되지 않았습니다."}));
  }
  if ((imp.paths || []).length) bd.appendChild(el("details", {class: "more"}, [el("summary", {text: "전체 영향 경로 " + imp.paths.length + "개 펼치기"}), pathList(imp.paths)]));
  // ②
  bd.appendChild(el("h4", {class: "step"}, ["② 스크립트가 무엇을 확인했는가", span("‘검사를 수행했다’와 ‘정상임을 확인했다’는 다릅니다", "q")]));
  bd.appendChild(table(["ID", "검사 항목", "실행 상태", "스크립트가 확인한 내용", "이 결과로 확정할 수 없는 것"], c.checks.map(function (k) {
    return el("tr", {id: "K-" + k.id}, [td(span(k.id, "idl")), td(k.item, "nowrap"), el("td", {}, [span(k.status, "tag " + STATUS_T(k.status))]), td(k.confirmed), td(k.cannot)]);
  })));
  // ③
  bd.appendChild(el("h4", {class: "step"}, ["③ 어떤 문제가 발견되었는가", span("변경 사실 · 위험 후보 · 확정된 위반을 구분", "q")]));
  if (c.findings.length) c.findings.forEach(function (id) { bd.appendChild(findingCard(FI[id])); });
  else bd.appendChild(el("div", {class: "ok", text: "이번 분석 범위에서 해당 규칙의 위반 미탐지 (검사 " + c.checks.map(function (k) { return k.id; }).join(", ") + ") — 변경 사실 확인만 됨. 동작이 요구사항에 맞는지는 ④에서 확인하세요."}));
  // ④
  bd.appendChild(el("h4", {class: "step"}, ["④ 검증자는 무엇을 추가로 확인해야 하는가", span("결과 · 증거 · 판정 근거를 모두 기록해야 완료", "q")]));
  bd.appendChild(testTable(c.tests.map(function (id) { return TE[id]; }), false));
  bd.appendChild(el("div", {class: "small", style: "margin-top:8px"}, [el("button", {class: "lnk", text: "근거 상세 보기 → (자동 분류 규칙 원문 · 전체 경로 · 검토 항목 지적)", onclick: function () { goEvidence(c.id); }})]));
  card.appendChild(hd); card.appendChild(bd);
  return card;
}
function pathList(paths) {
  return el("div", {}, paths.map(function (p) {
    return el("div", {}, [el("div", {class: "small"}, [code(p.name), " ", span(p.kind + " · " + p.func + "()", "muted")]),
      el("ol", {class: "path"}, p.steps.map(function (s) {
        var e = s.edge || [], how = e[0] === "seed" ? "변경 지점" : e[0] === "memory" ? "공유 변수 " + e[1] : e[0] === "data" ? "값 " + e[1] : e[0] === "control" ? "조건 " + (e[1] === "T" ? "참" : e[1] === "F" ? "거짓" : e[1]) : e[0] === "call" || e[0] === "param" ? "호출 인자 " + (e[1] || "") : e[0] === "return" ? "반환값" : (e[0] || "");
        return el("li", {}, [span("[" + how + "] ", "muted"), code(s.func + "()"), " " + s.file + ":" + s.line + " ", code(s.text)]);
      }))]);
  }));
}
function testTable(ts, withChange) {
  var heads = ["ID", "구분", "검증 항목", "입력·상태 조건", "관찰할 내용", "판정 기준", "연결", "상태"];
  var rows = [];
  ts.forEach(function (t) {
    if (!t) return;
    var links = (withChange ? [t.change] : []).concat(t.findings);
    rows.push(el("tr", {id: (withChange ? "VT-" : "T-") + t.id}, [td(span(t.id, "idl")), td(span(t.kind, "tag " + (t.kind === "시험" ? "ta" : "t2"))),
      td(t.title), td(t.condition), td(t.observe), td(t.criteria), el("td", {}, [links.length ? frag(links.map(idLink)) : span("-", "muted")]), statusCell(t.id)]));
    rows.push(recordRow(t, heads.length));
  });
  return table(heads, rows);
}

// ---------------------------------------------------------------- 탭 1: 변경 검토
var CF = {mode: "all", q: ""};
function buildChanges(sec) {
  sec.appendChild(el("h2", {text: "변경 검토"}));
  sec.appendChild(el("p", {class: "desc", text: "우선순위가 높은 변경부터 정렬했습니다. 변경마다 ① 변경 내용 → ② 자동 분석 → ③ 발견 사항 → ④ 추가 검증 순서로 읽고, 파일·함수 이름으로 위치를 찾으세요. 행을 누르면 해당 변경으로 이동합니다."}));
  var seg = el("div", {class: "seg"});
  [["all", "전체"], ["new", "신규 위험만"], ["risk", "위험 후보 이상"], ["todo", "검토 미완료"]].forEach(function (m) {
    seg.appendChild(el("button", {class: m[0] === CF.mode ? "on" : "", text: m[1], onclick: function () { CF.mode = m[0]; Array.prototype.forEach.call(seg.children, function (b) { b.classList.remove("on"); }); this.classList.add("on"); apply(); }}));
  });
  var q = el("input", {type: "search", placeholder: "제목·함수·파일·ID 검색", oninput: function (e) { CF.q = e.target.value.toLowerCase(); apply(); }});
  var cnt = span("", "count");
  sec.appendChild(el("div", {class: "toolbar"}, [seg, q, cnt]));
  var rows = {}, cards = {};
  var tb = el("tbody", {}, ST.changes.map(function (c) {
    var st = el("td", {class: "nowrap"});
    function draw() { var s = changeStatus(c); st.textContent = ""; st.appendChild(span(s.label, "tag " + s.cls)); }
    draw(); onRecord(draw);
    var r = el("tr", {class: "click", onclick: function () { goChange(c.id); }}, [td(span(c.id, "idb")), el("td", {}, [el("b", {text: c.title}),
      el("div", {class: "small muted", text: c.type_label})]), el("td", {}, [code(c.func + "()"), el("div", {class: "small muted", text: c.file + ":" + c.lo})]),
      el("td", {}, [prioTag(c.priority)]), el("td", {}, [judgeTag(c.judgment)]), el("td", {}, [cmpTag(c.comparison) || span("-", "muted")]),
      td(String(c.findings.length)), st]);
    rows[c.id] = r; return r;
  }));
  sec.appendChild(el("div", {class: "tw"}, [el("table", {}, [el("thead", {}, [el("tr", {}, ["ID", "변경 (기능 중심 제목)", "관련 함수", "우선순위", "판정", "비교 상태", "발견", "검토 상태"].map(function (h) { return el("th", {text: h}); }))]), tb])]));
  if (!ST.changes.length) sec.appendChild(el("div", {class: "ok", style: "margin-top:10px", text: "분석된 변경이 없습니다."}));
  var wrap = el("div", {});
  ST.changes.forEach(function (c) { cards[c.id] = changeCard(c); wrap.appendChild(cards[c.id]); });
  sec.appendChild(wrap);
  function match(c) {
    if (CF.mode === "new" && c.comparison !== "신규") return false;
    if (CF.mode === "risk" && ["실행 재현 확인", "분석 모델 내 위반 확인", "위험 후보"].indexOf(c.judgment) < 0) return false;
    if (CF.mode === "todo" && changeStatus(c).label.indexOf("완료") === 0) return false;
    if (CF.q && (c.id + " " + c.title + " " + c.func + " " + c.file + " " + c.sentences.join(" ")).toLowerCase().indexOf(CF.q) < 0) return false;
    return true;
  }
  function apply() {
    var n = 0;
    ST.changes.forEach(function (c) { var ok = match(c); rows[c.id].style.display = ok ? "" : "none"; cards[c.id].style.display = ok ? "" : "none"; if (ok) n++; });
    cnt.textContent = n + " / " + ST.changes.length + "건";
  }
  apply();
  CF.reset = function () { CF.mode = "all"; CF.q = ""; q.value = ""; Array.prototype.forEach.call(seg.children, function (b, i) { b.classList.toggle("on", i === 0); }); apply(); };
}
function goChange(id, anchor) {
  show("changes");
  var card = document.getElementById("C-" + id);
  if (card && card.style.display === "none" && CF.reset) CF.reset();
  if (card) { card.classList.remove("closed"); flash(anchor ? document.getElementById(anchor) || card : card); }
}
function goFinding(id) { var f = FI[id]; if (f) goChange(f.change, "F-" + id); }
function goTest(id) { var t = TE[id]; if (t) goChange(t.change, "T-" + id); }

// ---------------------------------------------------------------- 탭 2: 발견 사항
function legend() {
  return el("details", {class: "more"}, [el("summary", {text: "판정 표시와 비교 상태의 의미"}),
    table(["판정 (근거 수준)", "의미"], [
      ["변경 사실 확인", "코드가 바뀐 사실을 확인함"], ["위험 후보", "문제가 될 가능성을 찾았으나 위반은 확정하지 못함"],
      ["분석 모델 내 위반 확인", "명시한 가정·속성 아래에서 반례를 찾음 (실제 제품 결함 확정이 아님)"],
      ["실행 재현 확인", "명시한 환경과 입력으로 문제를 재현함"], ["분석 불충분", "판단에 필요한 범위나 증거가 부족함"]].map(function (r) {
      return el("tr", {}, [el("td", {}, [judgeTag(r[0])]), td(r[1])]); }), "legend"),
    el("div", {style: "height:6px"}),
    table(["비교 상태 (변경 전후)", "의미"], [["신규", "변경 후에 새로 생긴 후보"], ["기존", "변경 전에도 있던 후보"], ["해결", "변경 전에 있었으나 변경 후 사라짐"],
      ["판정 보류", "변경 전후 비교로 결론을 내리지 못함"]].map(function (r) { return el("tr", {}, [el("td", {}, [cmpTag(r[0])]), td(r[1])]); }), "legend")]);
}
function buildFindings(sec) {
  sec.appendChild(el("h2", {text: "발견 사항"}));
  sec.appendChild(el("p", {class: "desc", text: "같은 위치·같은 원인에서 나온 결과는 하나로 묶었습니다 (고유 문제 " + ST.findings.length + "건 · 자동 검토 항목 지적 " + ((OV.counts || {}).review_flags || 0) + "건 · 영향 경로 " + ((OV.counts || {}).impact_paths || 0) + "개는 따로 집계). 제목은 기능의 검토 문제로 쓰고, 자동 분류 규칙 이름은 근거 상세에 남겼습니다."}));
  sec.appendChild(legend());
  if (!ST.findings.length) { sec.appendChild(el("div", {class: "ok", text: "이번 분석 범위에서 위험 후보·위반이 탐지되지 않았습니다. (검사 범위는 각 변경의 ② 참고)"})); return; }
  var det = el("div", {});
  sec.appendChild(table(["ID", "검토할 문제", "변경", "현재 판정", "비교 상태", "근거 수준", "연결 시험"], ST.findings.map(function (f) {
    return el("tr", {class: "click", onclick: function () { det.textContent = ""; det.appendChild(findingCard(f)); flash(det.firstChild); }}, [td(span(f.id, "idb")), td(f.title),
      el("td", {}, [idLink(f.change)]), el("td", {}, [judgeTag(f.judgment)]), el("td", {}, [cmpTag(f.comparison)]), td(f.evidence_level.join(" · "), "small"),
      el("td", {}, [frag(f.tests.map(idLink))])]);
  })));
  sec.appendChild(det);
}

// ---------------------------------------------------------------- 탭 3: 검증 기록
function buildVerify(sec) {
  sec.appendChild(el("h2", {text: "검증 기록"}));
  sec.appendChild(el("p", {class: "desc", text: "결과만 고르면 완료가 되지 않습니다. 적합·부적합·해당 없음을 고르고 증거와 판정 근거를 함께 적어야 ‘완료’입니다. 기록은 이 브라우저에 저장되며 CSV 로 내보낼 수 있습니다. 기대값이 ‘미확정’인 항목은 관련 요구사항을 먼저 확인하세요."}));
  var fs = el("select", {}, ["전체", "미수행", "진행 중", "증거 필요", "완료"].map(function (v) { return el("option", {value: v, text: "상태: " + v}); }));
  var fk = el("select", {}, ["전체", "시험", "판단"].map(function (v) { return el("option", {value: v, text: "구분: " + v}); }));
  var sum = span("", "count");
  sec.appendChild(el("div", {class: "toolbar"}, [fs, fk, el("button", {class: "btn", text: "CSV 내보내기", onclick: exportCsv}), sum]));
  var box = el("div", {});
  sec.appendChild(box);
  function draw() {
    box.textContent = "";
    var ts = ST.tests.filter(function (t) { return (fs.value === "전체" || testStatus(t.id) === fs.value) && (fk.value === "전체" || t.kind === fk.value); });
    box.appendChild(testTable(ts, true));
    var c = {}; ST.tests.forEach(function (t) { var s = testStatus(t.id); c[s] = (c[s] || 0) + 1; });
    sum.textContent = "전체 " + ST.tests.length + " · 완료 " + (c["완료"] || 0) + " · 증거 필요 " + (c["증거 필요"] || 0) + " · 진행 중 " + (c["진행 중"] || 0) + " · 미수행 " + (c["미수행"] || 0);
  }
  fs.onchange = draw; fk.onchange = draw;
  draw();
  onRecord(function () { var c = {}; ST.tests.forEach(function (t) { var s = testStatus(t.id); c[s] = (c[s] || 0) + 1; });
    sum.textContent = "전체 " + ST.tests.length + " · 완료 " + (c["완료"] || 0) + " · 증거 필요 " + (c["증거 필요"] || 0) + " · 진행 중 " + (c["진행 중"] || 0) + " · 미수행 " + (c["미수행"] || 0); });
}
function exportCsv() {
  var head = ["시험ID", "변경ID", "변경", "함수", "파일", "발견ID", "구분", "검증 항목", "입력·상태 조건", "관찰할 내용", "판정 기준", "담당자", "결과", "증거", "판정 근거", "상태", "기록 시각"];
  var lines = [head];
  ST.tests.forEach(function (t) {
    var c = CH[t.change] || {}, r = rec(t.id);
    lines.push([t.id, t.change, c.title, c.func, c.file, t.findings.join(" "), t.kind, t.title, t.condition, t.observe, t.criteria,
      r.owner, r.result, r.evidence, r.basis, testStatus(t.id), r.at || ""]);
  });
  var csv = "﻿" + lines.map(function (row) { return row.map(function (x) { x = String(x == null ? "" : x).replace(/`/g, ""); return /[",\n]/.test(x) ? '"' + x.replace(/"/g, '""') + '"' : x; }).join(","); }).join("\r\n");
  var a = el("a", {href: URL.createObjectURL(new Blob([csv], {type: "text/csv"})), download: "verification_" + ((OV.versions || {}).new || "report") + ".csv"});
  document.body.appendChild(a); a.click(); a.remove();
}

// ---------------------------------------------------------------- 탭 4: 근거 상세
function buildEvidence(sec) {
  sec.appendChild(el("h2", {text: "근거 상세"}));
  sec.appendChild(el("p", {class: "desc", text: "판단의 근거를 직접 확인하는 곳입니다: 함수 전체 코드 차이, 전체 영향 경로, 자동 분류 규칙 원문과 검토 항목 지적, 분석 설정·도구 실행 기록."}));
  sec.appendChild(el("h3", {text: "변경별 근거"}));
  ST.changes.forEach(function (c) {
    var d = el("details", {class: "ev", id: "E-" + c.id}, [el("summary", {}, [span(c.id, "idb"), " " + c.title + "  ", span(c.func + "() · " + c.file, "small muted")])]);
    d.appendChild(el("h5", {text: "코드 차이 (함수 전체)"}));
    d.appendChild(diffTable(c.code_full && c.code_full.length ? c.code_full : c.code));
    d.appendChild(el("h5", {text: "AST 변경 기록"}));
    d.appendChild(el("ul", {class: "small"}, c.phrases.map(function (p) { return el("li", {}, [rich(p)]); })));
    d.appendChild(el("h5", {text: "전체 영향 경로 " + ((c.impact || {}).paths || []).length + "개"}));
    d.appendChild(((c.impact || {}).paths || []).length ? pathList(c.impact.paths) : span("없음", "muted small"));
    c.findings.forEach(function (fid) {
      var f = FI[fid];
      var box = el("div", {class: "fcard", id: "EF-" + fid}, [el("h5", {}, [span(fid, "idb"), " 자동 분류 원문"])]);
      (f.sources || []).forEach(function (s) {
        var kv = el("div", {class: "kv"});
        [["규칙 이름", s.rule], ["분류", s.category], ["규칙이 본 위험", s.risk], ["탐지 방식", s.how], ["검증 수준", s.verification_level],
          ["근거", s.evidence], ["대상 식", s.expr ? "`" + s.expr + "` (" + s.line + "행)" : ""], ["변경 전 상태", s.baseline], ["전후 비교", s.status],
          ["Taint 경로", s.taint ? "있음" : "없음"], ["기호 실행", s.symbolic ? JSON.stringify(s.symbolic) : "기록 없음"], ["경계 변화", s.bounds]].forEach(function (r) {
          if (!r[1]) return; kv.appendChild(el("div", {text: r[0]})); kv.appendChild(el("div", {}, [rich(r[1])]));
        });
        box.appendChild(kv);
      });
      (f.review || []).forEach(function (r) { box.appendChild(el("div", {class: "small"}, [span("검토 " + r.item + " · " + r.class + ": ", "muted"), rich(r.summary)])); });
      d.appendChild(box);
    });
    if ((c.review_flags || []).length) {
      d.appendChild(el("h5", {text: "자동 검토 항목 지적 " + c.review_flags.length + "건"}));
      d.appendChild(table(["항목", "결과 분류", "내용"], c.review_flags.map(function (r) { return el("tr", {}, [td(r.item), td(r.class), td(r.summary)]); })));
    }
    sec.appendChild(d);
  });
  // 분석 설정
  sec.appendChild(el("h3", {text: "분석 설정·실행 기록"}));
  var sett = [["변경 전 폴더", M.old_dir], ["변경 후 폴더", M.new_dir], ["분석 범위", (OV.scope === "changed" ? "변경 파일 + 변경 헤더 사용 파일" : "전체") + " — " + (OV.scope_reason || []).join(", ")],
    ["분석한 C 파일", "변경 전 " + ((OV.analyzed || {}).old || 0) + "개 / 변경 후 " + ((OV.analyzed || {}).new || 0) + "개"],
    ["include 경로", "프로젝트 헤더 폴더 " + ((D.include || {}).auto || 0) + "개 자동 등록, 직접 지정 " + ((D.include || {}).user || 0) + "개" + ((D.include || {}).cc ? ", compile_commands.json 사용" : "")],
    ["분석 시간", M.elapsed ? (M.elapsed / 60).toFixed(1) + "분" : "-"]];
  sec.appendChild(table(["항목", "값"], sett.map(function (r) { return el("tr", {}, [td(r[0], "nowrap"), td(String(r[1] == null ? "" : r[1]))]); })));
  if ((D.auto_config || []).length) {
    sec.appendChild(el("h5", {text: "자동 탐지한 빌드 설정 (--auto-config)"}));
    sec.appendChild(table(["확신도", "항목", "값", "근거"], D.auto_config.map(function (d) {
      return el("tr", {}, [el("td", {}, [span(d.confidence, "tag " + (d.confidence === "높음" ? "t3" : d.confidence === "낮음" ? "t1" : "t4"))]), td(d.item), el("td", {}, [code(d.value)]),
        td(d.evidence + ((d.alternatives || []).length ? " (대안: " + d.alternatives.slice(0, 4).join(", ") + ")" : ""), "small")]);
    })));
  }
  var sec2 = A ? (A.security || {}) : {}, eng = sec2.engine || {}, tools = sec2.tools || {};
  sec.appendChild(el("h5", {text: "분석 도구와 실행 상태"}));
  var tr = [["AST 비교·CFG·제어/데이터 의존", "실행", "변경 " + (A ? A.changes.length : 0) + "곳, 영향 경로 " + (A ? A.items.length : 0) + "개"],
    ["CPG", eng.cpg ? "실행" : "-", tools.cpg ? "노드 " + tools.cpg.nodes + " · 간선 " + tools.cpg.edges + " (" + tools.cpg.file + ")" : (eng.cpg || "")],
    ["Taint", eng.taint ? "실행" : "-", (eng.taint || "") + " · 규칙 " + (eng.rules || "-")],
    ["기호 실행 (z3)", eng.symbolic ? "실행" : "미실행", eng.symbolic || "z3 없음"],
    ["Joern", (tools.joern || {}).ran ? "실행" : "실행하지 않음", (tools.joern || {}).note || (tools.joern || {}).script || ""],
    ["CodeQL", (tools.codeql || {}).ran ? "실행" : "실행하지 않음", (tools.codeql || {}).note || (tools.codeql || {}).query || ""],
    ["KLEE", (tools.klee || {}).ran ? "실행 " + tools.klee.ran + "개" : "실행하지 않음", "하네스 " + ((tools.klee || {}).harnesses || 0) + "개 생성"],
    ["SIL·자동 탐색", (D.sil || D.explore) ? "실행" : "결과 미첨부", D.sil ? "SIL 케이스 " + D.sil.cases + "개, 차이 " + D.sil.diff_cases + "개" : D.explore ? "탐색 케이스 " + D.explore.cases + "개, 차이 " + D.explore.diff_cases + "개" : "--scenario / --explore 로 실행"]];
  sec.appendChild(table(["도구", "실행 상태", "기록"], tr.map(function (r) { return el("tr", {}, [td(r[0], "nowrap"), el("td", {}, [span(r[1], "tag " + (r[1].indexOf("실행") === 0 && r[1] !== "실행하지 않음" ? "ta" : "t1"))]), td(r[2], "small")]); })));
  var R = D.review;
  if (R && R.items) {
    sec.appendChild(el("h5", {text: "자동 검토 항목 25개"}));
    sec.appendChild(table(["번호", "자동 분석 내용", "결과", "건수", "검토한 내용", "검증자가 판단할 내용"], R.items.map(function (it) {
      return el("tr", {}, [td(it.id), td(it.title), td(it.overall), td(Object.keys(it.counts).filter(function (k) { return it.counts[k]; }).map(function (k) { return k + " " + it.counts[k]; }).join(" · ") || "-", "small"),
        td(it.notes.join(" · "), "small"), td(it.reviewer_focus, "small")]);
    })));
  }
  if (A) {
    var ex = A.excluded || {}, pe = Object.keys(ex.parse_errors || {});
    sec.appendChild(el("h5", {text: "파싱 실패 (분석 제외) " + pe.length + "개"}));
    var PD = D.parse_diag || [];
    if (PD.length) sec.appendChild(table(["원인", "대상", "파일 수", "예", "해결 방법"], PD.map(function (g) {
      var adv = (g.fix ? "-D \"" + g.fix + "\" — " : "") + ((g.defined_in || []).length ? "정의 위치: " + g.defined_in.slice(0, 3).join(", ") + " — " : "") + g.advice;
      return el("tr", {}, [td(g.cause), el("td", {}, [code(g.detail)]), td(String(g.count)), el("td", {class: "small"}, [code(g.files[0] || ""), g.where ? " (" + g.where + ")" : ""]), td(adv, "small")]);
    })));
    if (pe.length) sec.appendChild(el("details", {class: "more"}, [el("summary", {text: "파일별 오류 " + pe.length + "개"}), el("ul", {class: "small"}, pe.map(function (f) {
      return el("li", {}, [code(f), " ", span(String(ex.parse_errors[f] || "").split("\n").filter(function (x) { return x.trim(); }).slice(-1)[0] || "", "muted")]); }))]));
    else sec.appendChild(span("없음", "muted small"));
    var asm = [];
    if ((ex.extern_functions || []).length) asm.push("본문 없는 외부 함수 " + ex.extern_functions.length + "개: 포인터 인자가 가리키는 메모리를 읽고 쓰는 것으로 보수적으로 가정");
    if ((ex.unresolved_calls || []).length) asm.push("대상을 풀지 못한 함수 포인터 호출 " + ex.unresolved_calls.length + "개: 피호출 함수 내부 영향 누락 가능");
    if ((ex.unresolved_pointers || []).length) asm.push("대상을 모르는 포인터 " + ex.unresolved_pointers.length + "개");
    if ((ex.hw_access || []).length) asm.push("상수 주소(하드웨어 레지스터) 접근 " + ex.hw_access.length + "개: 하드웨어 동작은 모델링하지 않음");
    (ex.assumptions || []).forEach(function (a) { asm.push(a); });
    asm.push("비활성 #if 경로는 분석하지 않음");
    sec.appendChild(el("h5", {text: "분석 가정과 한계"}));
    sec.appendChild(el("ul", {class: "small"}, asm.map(function (a) { return el("li", {text: a}); })));
  }
  sec.appendChild(el("h5", {text: "결과 파일"}));
  sec.appendChild(el("ul", {class: "small"}, (M.outputs || []).map(function (p) { return el("li", {}, [code(p)]); })));
}
function goEvidence(cid, anchor) {
  show("evidence");
  var d = document.getElementById("E-" + cid);
  if (d) { d.open = true; flash(anchor ? document.getElementById(anchor) || d : d); }
}

// ---------------------------------------------------------------- 시작
header();
addTab("changes", "변경 검토", ST.changes.length, buildChanges);
addTab("findings", "발견 사항", ST.findings.length, buildFindings);
addTab("verify", "검증 기록", ST.tests.length, buildVerify);
addTab("evidence", "근거 상세", null, buildEvidence);
var start = (location.hash || "").slice(1);
show(panes[start] ? start : "changes");
})();
</script>
</body>
</html>
"""
