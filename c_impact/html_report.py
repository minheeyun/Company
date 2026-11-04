"""폴더 비교 결과를 항목별 탭으로 묶은 하나의 HTML 파일로 만든다.

외부 파일·인터넷 연결 없이 열리도록 데이터(JSON)와 화면 코드(CSS·JS)를 모두 한 파일에 넣는다.
표마다 검색·필터·'더 보기'가 있고, 영향 항목을 누르면 조건과 전달 경로가 펼쳐진다.
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
<title>변경 영향 분석</title>
<style>
:root {
  --bg: #f6f7f9; --panel: #ffffff; --text: #1d2330; --muted: #5d6677; --line: #dfe3ea;
  --accent: #2f5bd3; --accent-bg: #e8eefc; --code-bg: #f1f3f7; --row-hover: #f3f6fc;
  --p0: #b42318; --p0-bg: #fdecea; --p1: #9a5b00; --p1-bg: #fff3df; --p2: #6b3fb3; --p2-bg: #f1eafb;
  --p3: #1f6f4a; --p3-bg: #e5f5ec; --p4: #4a5363; --p4-bg: #eef0f3;
  --add: #1f6f4a; --del: #b42318;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --bg: #14171c; --panel: #1c2027; --text: #e4e7ec; --muted: #9aa3b2; --line: #2e343e;
    --accent: #8aa8ff; --accent-bg: #23304d; --code-bg: #252a33; --row-hover: #232935;
    --p0: #ff8a7a; --p0-bg: #3d1f1c; --p1: #f3b75a; --p1-bg: #3a2c14; --p2: #c4a3ff; --p2-bg: #2e2440;
    --p3: #6fd3a0; --p3-bg: #183326; --p4: #b3bac6; --p4-bg: #2a2f37;
    --add: #6fd3a0; --del: #ff8a7a;
  }
}
:root[data-theme="dark"] {
  --bg: #14171c; --panel: #1c2027; --text: #e4e7ec; --muted: #9aa3b2; --line: #2e343e;
  --accent: #8aa8ff; --accent-bg: #23304d; --code-bg: #252a33; --row-hover: #232935;
  --p0: #ff8a7a; --p0-bg: #3d1f1c; --p1: #f3b75a; --p1-bg: #3a2c14; --p2: #c4a3ff; --p2-bg: #2e2440;
  --p3: #6fd3a0; --p3-bg: #183326; --p4: #b3bac6; --p4-bg: #2a2f37;
  --add: #6fd3a0; --del: #ff8a7a;
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--text);
  font: 14px/1.5 "Malgun Gothic", "Apple SD Gothic Neo", "Noto Sans KR", system-ui, sans-serif; }
header { padding: 18px 24px 12px; background: var(--panel); border-bottom: 1px solid var(--line); }
header h1 { margin: 0 0 6px; font-size: 20px; }
header .paths { color: var(--muted); font-size: 13px; word-break: break-all; }
nav { position: sticky; top: 0; z-index: 5; background: var(--panel); border-bottom: 1px solid var(--line);
  display: flex; flex-wrap: wrap; gap: 2px; padding: 0 16px; }
nav button { border: 0; background: none; color: var(--muted); padding: 10px 12px; cursor: pointer;
  font: inherit; border-bottom: 2px solid transparent; }
nav button:hover { color: var(--text); }
nav button.on { color: var(--accent); border-bottom-color: var(--accent); font-weight: 600; }
nav .cnt { font-size: 12px; margin-left: 4px; color: var(--muted); }
main { padding: 20px 24px 48px; max-width: 1600px; }
section { display: none; }
section.on { display: block; }
h2 { font-size: 18px; margin: 0 0 4px; }
.desc { color: var(--muted); margin: 0 0 14px; }
.cards { display: grid; grid-template-columns: repeat(auto-fill, minmax(170px, 1fr)); gap: 10px; margin-bottom: 18px; }
.card { background: var(--panel); border: 1px solid var(--line); border-radius: 8px; padding: 12px 14px; cursor: default; }
.card.link { cursor: pointer; }
.card.link:hover { border-color: var(--accent); }
.card .v { font-size: 24px; font-weight: 700; }
.card .k { color: var(--muted); font-size: 12px; }
.box { background: var(--panel); border: 1px solid var(--line); border-radius: 8px; padding: 12px 16px; margin-bottom: 14px; }
.box h3 { margin: 0 0 8px; font-size: 15px; }
.box ul { margin: 0; padding-left: 20px; }
.warn { border-left: 4px solid var(--p1); }
.toolbar { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; margin-bottom: 8px; }
.toolbar input[type=search], .toolbar select { font: inherit; padding: 5px 8px; border: 1px solid var(--line); border-radius: 6px;
  background: var(--panel); color: var(--text); }
.toolbar input[type=search] { min-width: 260px; }
.toolbar label { display: inline-flex; align-items: center; gap: 4px; }
.toolbar .count { color: var(--muted); font-size: 13px; }
.toolbar .clear { font: inherit; border: 1px solid var(--line); background: var(--panel); color: var(--accent);
  border-radius: 6px; padding: 4px 10px; cursor: pointer; }
.tw { overflow-x: auto; background: var(--panel); border: 1px solid var(--line); border-radius: 8px; }
table { border-collapse: collapse; width: 100%; }
th, td { text-align: left; vertical-align: top; padding: 7px 10px; border-bottom: 1px solid var(--line); }
th { position: sticky; top: 0; background: var(--panel); font-size: 12px; color: var(--muted); font-weight: 600; white-space: nowrap; }
tr.row.click { cursor: pointer; }
tr.row.click:hover td { background: var(--row-hover); }
tr.row.open td { background: var(--row-hover); }
tr.det td { background: var(--bg); padding: 12px 16px 14px; }
code, .code { font-family: Consolas, "D2Coding", "Courier New", monospace; font-size: 12.5px; background: var(--code-bg);
  padding: 1px 5px; border-radius: 4px; word-break: break-all; }
.muted { color: var(--muted); }
.nowrap { white-space: nowrap; }
.small { font-size: 12px; }
.tag { display: inline-block; padding: 1px 8px; border-radius: 10px; font-size: 12px; white-space: nowrap; font-weight: 600; }
.t0 { color: var(--p0); background: var(--p0-bg); } .t1 { color: var(--p1); background: var(--p1-bg); }
.t2 { color: var(--p2); background: var(--p2-bg); } .t3 { color: var(--p3); background: var(--p3-bg); }
.t4 { color: var(--p4); background: var(--p4-bg); }
.sev { display: inline-block; padding: 1px 8px; border-radius: 10px; font-size: 12px; white-space: nowrap; font-weight: 700; }
.sev.c { color: #fff; background: var(--p0); } .sev.u { color: var(--p1); background: var(--p1-bg); } .sev.l { color: var(--p4); background: var(--p4-bg); }
.card.alert { border-color: var(--p0); } .card.alert .v { color: var(--p0); }
.arrow { color: var(--accent); font-weight: 600; margin-right: 4px; }
.old { color: var(--del); } .new { color: var(--add); }
ol.path { margin: 4px 0 0; padding-left: 22px; }
ol.path li { margin: 2px 0; }
.more { display: block; margin: 10px auto 0; font: inherit; padding: 6px 16px; border: 1px solid var(--line);
  border-radius: 6px; background: var(--panel); color: var(--accent); cursor: pointer; }
.lnk { color: var(--accent); cursor: pointer; text-decoration: underline; background: none; border: 0; font: inherit; padding: 0; }
.det h4 { margin: 10px 0 4px; font-size: 13px; }
.det h4:first-child { margin-top: 0; }
.kv { display: grid; grid-template-columns: max-content 1fr; gap: 2px 14px; }
.kv div:nth-child(odd) { color: var(--muted); }
.seg { display: inline-flex; border: 1px solid var(--line); border-radius: 8px; overflow: hidden; }
.seg button { border: 0; border-right: 1px solid var(--line); background: var(--panel); color: var(--text); padding: 6px 14px; cursor: pointer; font: inherit; }
.seg button:last-child { border-right: 0; }
.seg button.on { background: var(--accent); color: #fff; font-weight: 600; }
.fileblk { display: block; background: var(--panel); border: 1px solid var(--line); border-radius: 10px; margin-bottom: 14px; padding: 4px 14px 10px; }
.fh { padding: 10px 2px 8px; border-bottom: 1px solid var(--line); margin-bottom: 6px; }
details.fn { border-bottom: 1px dashed var(--line); padding: 6px 2px; }
details.fn:last-child { border-bottom: 0; }
details.fn > summary { cursor: pointer; padding: 4px 0; }
details.more-d > summary { cursor: pointer; color: var(--accent); font-size: 13px; margin: 4px 0; }
table.ct { width: auto; max-width: 100%; }
table.ct td { padding: 3px 10px 3px 0; border: 0; }
.rcard { border: 1px solid var(--line); border-left: 4px solid var(--p1); border-radius: 6px; padding: 8px 12px; margin: 6px 0; background: var(--bg); }
.rcard.new { border-left-color: var(--p0); }
.rf div { margin-top: 2px; }
h4.step { margin: 12px 0 6px; padding: 4px 8px; background: var(--accent-bg); color: var(--accent); border-radius: 6px; font-size: 13px; }
.rv { border-left: 3px solid var(--line); padding: 4px 10px; margin: 4px 0; }
.rv-s { margin-top: 2px; }
ul.ql { list-style: none; padding-left: 4px; margin: 4px 0; }
ul.ql li.q { margin: 6px 0; padding: 6px 8px; border: 1px solid var(--line); border-radius: 6px; background: var(--bg); }
ul.ql li.q input { vertical-align: middle; }
.banner { margin-top: 8px; font-size: 13px; padding: 6px 10px; border-radius: 6px; background: var(--accent-bg); }
.banner.warn { background: var(--p1-bg); color: var(--p1); font-weight: 600; }
.ucard { background: var(--panel); border: 1px solid var(--line); border-left: 5px solid var(--line); border-radius: 8px; padding: 10px 14px 12px; margin: 10px 0 16px; }
.ucard.red { border-left-color: var(--p0); } .ucard.orange { border-left-color: var(--p1); } .ucard.yellow { border-left-color: var(--p2); }
.ucard.flash { box-shadow: 0 0 0 3px var(--accent); }
.uh { padding-bottom: 6px; border-bottom: 1px solid var(--line); margin-bottom: 8px; }
table.kvt th { width: 110px; color: var(--muted); font-weight: 600; white-space: nowrap; background: none; position: static; font-size: 13px; }
table.kvt td, table.kvt th { padding: 4px 8px; border-bottom: 1px solid var(--line); }
table.kvt.sum { background: var(--bg); border-radius: 6px; }
table.cl { width: 100%; margin: 4px 0; } table.cl th { position: static; } table.cl td { font-size: 13px; }
.diff { font-family: Consolas, "D2Coding", monospace; font-size: 12.5px; border: 1px solid var(--line); border-radius: 6px; overflow-x: auto; }
.dh { border-bottom: 1px solid var(--line); padding: 4px 8px; } .dh:last-child { border-bottom: 0; }
.dl .ln { color: var(--muted); font-weight: 600; } .dl { font-family: inherit; }
.rm { color: var(--del); background: var(--p0-bg); white-space: pre-wrap; padding: 1px 4px; }
.ad { color: var(--add); background: var(--p3-bg); white-space: pre-wrap; padding: 1px 4px; }
.mv { color: var(--p2); background: var(--p2-bg); white-space: pre-wrap; padding: 1px 4px; }
.conc { margin: 8px 0; padding: 8px 10px; background: var(--accent-bg); border-radius: 6px; font-size: 13.5px; }
.icard { border: 1px solid var(--line); border-radius: 8px; margin: 6px 0; overflow: hidden; }
.icard .ih { padding: 6px 10px; font-size: 13.5px; }
.icard.red .ih { background: var(--p0-bg); color: var(--p0); } .icard.orange .ih { background: var(--p1-bg); color: var(--p1); }
.icard.yellow .ih { background: var(--p2-bg); color: var(--p2); } .icard.old .ih { background: var(--p4-bg); color: var(--p4); }
.btns { display: flex; flex-wrap: wrap; gap: 14px; align-items: flex-start; margin-top: 10px; padding-top: 8px; border-top: 1px dashed var(--line); }
select.ans { font: inherit; padding: 3px 6px; border: 1px solid var(--line); border-radius: 6px; background: var(--panel); color: var(--text); }
h3.fnh { margin: 22px 0 4px; font-size: 16px; }
.flow { display: flex; flex-direction: column; align-items: stretch; max-width: 760px; margin: 6px 0 10px; }
.fb { border: 1px solid var(--line); border-radius: 8px; padding: 8px 12px; background: var(--bg); }
.fb.dir { border-style: dashed; background: var(--accent-bg); }
.arr { text-align: center; color: var(--muted); line-height: 1.2; }
.lvl { margin: 4px 0; }
.vuln { margin: 10px 0; padding: 8px 12px; border: 1px solid var(--p1); border-radius: 8px; background: var(--p1-bg); }
#theme { margin-left: auto; border: 0; background: none; color: var(--muted); cursor: pointer; font: inherit; padding: 10px; }
@media (max-width: 700px) { main { padding: 14px 16px 40px; } header { padding: 14px 16px 10px; } .toolbar input[type=search] { min-width: 0; width: 100%; } }
</style>
</head>
<body>
<header>
  <h1>변경 영향 분석 보고서</h1>
  <div class="paths" id="paths"></div>
  <div id="banner"></div>
</header>
<nav id="nav"></nav>
<main id="main"></main>
<script type="application/json" id="data">__DATA__</script>
<script>
(function () {
"use strict";
var D = JSON.parse(document.getElementById("data").textContent);
var A = D.analysis;
var PRIO = ["외부 출력", "레지스터", "여러 진입점 공유", "반환값", "상태 변수"];

function el(tag, attrs, kids) {
  var e = document.createElement(tag);
  if (attrs) for (var k in attrs) {
    if (k === "class") e.className = attrs[k];
    else if (k === "text") e.textContent = attrs[k];
    else if (k.slice(0, 2) === "on") e.addEventListener(k.slice(2), attrs[k]);
    else e.setAttribute(k, attrs[k]);
  }
  (kids || []).forEach(function (c) {
    if (c === null || c === undefined || c === "") return;
    e.appendChild(typeof c === "string" || typeof c === "number" ? document.createTextNode(String(c)) : c);
  });
  return e;
}
function code(t) { return el("code", {text: t}); }
function tag(p) { return el("span", {class: "tag t" + p, text: PRIO[p]}); }
function frag(list) { var f = document.createDocumentFragment(); list.forEach(function (x) { if (x) f.appendChild(typeof x === "string" ? document.createTextNode(x) : x); }); return f; }
function loc(file, line) { return el("span", {class: "small muted", text: file + (line ? ":" + line : "")}); }
function short(t, n) { t = String(t || "").replace(/\s+/g, " "); return t.length > n ? t.slice(0, n - 1) + "…" : t; }
function change(c) {
  if (c.old && c.new) return frag([el("span", {class: "code old", text: short(c.old, 200)}), " → ", el("span", {class: "code new", text: short(c.new, 200)})]);
  return el("span", {class: "code " + (c.new ? "new" : "old"), text: short(c.new || c.old, 200)});
}

/* ---------- 표: 검색·필터·더 보기·행 펼치기 ---------- */
function Table(opt) {
  var self = this;
  this.opt = opt; this.page = opt.pageSize || 200; this.shown = this.page; this.vals = {}; this.q = "";
  var bar = el("div", {class: "toolbar"});
  if (opt.search) {
    this.input = el("input", {type: "search", placeholder: opt.placeholder || "검색 (함수·변수·파일·코드)"});
    this.input.addEventListener("input", function () { self.q = self.input.value.toLowerCase(); self.shown = self.page; self.draw(); });
    bar.appendChild(this.input);
  }
  this.selects = {};
  (opt.filters || []).forEach(function (f) {
    var s = el("select", {}, [el("option", {value: "", text: f.label + ": 전체"})].concat(
      f.options.map(function (o) { return el("option", {value: o, text: o}); })));
    s.addEventListener("change", function () { self.vals[f.name] = s.value; self.shown = self.page; self.draw(); });
    self.selects[f.name] = s; bar.appendChild(s);
  });
  this.clearBtn = el("button", {class: "clear", text: "필터 해제", onclick: function () { self.reset(); }});
  this.count = el("span", {class: "count"});
  bar.appendChild(this.clearBtn); bar.appendChild(this.count);
  this.tbody = el("tbody");
  var head = el("tr", {}, opt.columns.map(function (c) { return el("th", {text: c.label}); }));
  this.more = el("button", {class: "more", text: "더 보기", onclick: function () { self.shown += self.page; self.draw(); }});
  this.node = el("div", {}, [bar, el("div", {class: "tw"}, [el("table", {}, [el("thead", {}, [head]), this.tbody])]), this.more]);
  this.draw();
}
Table.prototype.reset = function () {
  this.vals = {}; this.q = ""; this.extra = null;
  if (this.input) this.input.value = "";
  for (var k in this.selects) this.selects[k].value = "";
  this.shown = this.page; this.draw();
};
Table.prototype.set = function (name, val, label) {
  this.reset();
  if (this.selects[name]) { this.selects[name].value = val; this.vals[name] = val; }
  else this.extra = {name: name, val: val, label: label};
  this.draw();
};
Table.prototype.rows = function () {
  var o = this.opt, q = this.q, vals = this.vals, extra = this.extra;
  return o.rows.filter(function (r) {
    for (var i = 0; i < (o.filters || []).length; i++) {
      var f = o.filters[i], v = vals[f.name];
      if (v && !f.test(r, v)) return false;
    }
    if (extra && !o.extraTest(r, extra)) return false;
    if (q && o.search(r).toLowerCase().indexOf(q) < 0) return false;
    return true;
  });
};
Table.prototype.draw = function () {
  var o = this.opt, rows = this.rows(), tb = this.tbody, ncol = o.columns.length;
  tb.textContent = "";
  rows.slice(0, this.shown).forEach(function (r) {
    var tr = el("tr", {class: "row" + (o.detail ? " click" : "")}, o.columns.map(function (c) {
      var v = c.render(r); return el("td", {class: c.cls || ""}, [v]);
    }));
    tb.appendChild(tr);
    if (o.detail) tr.addEventListener("click", function (ev) {
      if (ev.target.closest("button, a")) return;
      var nx = tr.nextSibling;
      if (nx && nx.classList.contains("det")) { nx.remove(); tr.classList.remove("open"); return; }
      tr.classList.add("open");
      tr.parentNode.insertBefore(el("tr", {class: "det"}, [el("td", {colspan: ncol}, [o.detail(r)])]), tr.nextSibling);
    });
  });
  if (!rows.length) tb.appendChild(el("tr", {}, [el("td", {colspan: ncol, class: "muted", text: "해당하는 항목이 없습니다."})]));
  var filtered = rows.length !== o.rows.length;
  this.count.textContent = (filtered ? rows.length + "개 / 전체 " + o.rows.length + "개" : "전체 " + o.rows.length + "개")
    + (this.extra ? " · " + this.extra.label : "") + (rows.length > this.shown ? " (" + this.shown + "개 표시)" : "");
  this.clearBtn.style.display = filtered || this.extra ? "" : "none";
  this.more.style.display = rows.length > this.shown ? "" : "none";
};

/* ---------- 영향 항목 상세 ---------- */
function itemDetail(it) {
  var conds = it.conditions.length ? el("ul", {}, it.conditions.map(function (c) { return el("li", {}, [code(c)]); }))
                                   : el("div", {class: "muted", text: "모든 실행 (조건 없음)"});
  var path = el("ol", {class: "path"}, it.path.map(function (s) {
    return el("li", {}, [el("span", {class: "arrow", text: s.arrow || ""}), code(s.func), " ", loc(s.file, s.line), " ", code(s.text)]);
  }));
  return el("div", {}, [
    el("div", {class: "kv"}, [
      el("div", {text: "위치"}), el("div", {}, [loc(it.file, it.line)]),
      el("div", {text: "문장"}), el("div", {}, [code(it.src || "")]),
      el("div", {text: "어떻게"}), el("div", {}, [el("b", {text: it.how_kind + ": "}), it.how]),
      el("div", {text: "원인 변경"}), el("div", {}, [code(it.origin), " ", loc(it.origin_file)]),
      el("div", {text: "버전"}), el("div", {text: it.versions.map(function (v) { return v === "old" ? "변경 전" : "변경 후"; }).join(", ")})
    ]),
    el("h4", {text: "차이가 발생하는 조건"}), conds,
    el("h4", {text: "전달 경로 (변경 지점 → 이 항목)"}), path
  ]);
}
function condCell(it) {
  if (!it.conditions.length) return el("span", {class: "muted", text: "모든 실행"});
  var kids = it.conditions.slice(0, 2).map(function (c) { return el("div", {}, [el("span", {class: "code", text: short(c, 110)})]); });
  if (it.conditions.length > 2) kids.push(el("div", {class: "muted small", text: "외 " + (it.conditions.length - 2) + "개 (눌러서 보기)"}));
  return el("div", {}, kids);
}
function itemTable(rows, extraFilters) {
  var funcs = uniq(rows.map(function (r) { return r.func; }));
  return new Table({
    rows: rows, search: function (r) { return [r.func, r.name, r.file, r.how, r.origin, r.src].concat(r.conditions).join(" "); },
    filters: [
      {name: "prio", label: "구분", options: uniq(rows.map(function (r) { return r.prio_label; }), PRIO), test: function (r, v) { return r.prio_label === v; }},
      {name: "how", label: "영향 방식", options: uniq(rows.map(function (r) { return r.how_kind; })), test: function (r, v) { return r.how_kind === v; }},
      {name: "func", label: "영향받는 함수", options: funcs, test: function (r, v) { return r.func === v; }}
    ].concat(extraFilters || []),
    extraTest: function (r, x) { return x.name === "originKey" ? r.origin + "|" + r.origin_file === x.val : true; },
    columns: [
      {label: "구분", render: function (r) { return tag(r.prio); }, cls: "nowrap"},
      {label: "영향받는 함수", render: function (r) { return frag([code(r.func), el("br"), loc(r.file, r.line)]); }},
      {label: "변수·출력", render: function (r) { return code(r.name); }},
      {label: "어떻게", render: function (r) { return frag([el("b", {text: r.how_kind}), ": " + short(r.how, 140)]); }},
      {label: "차이가 발생하는 조건", render: condCell},
      {label: "원인 변경", render: function (r) { return frag([code(r.origin), el("br"), loc(r.origin_file)]); }}
    ],
    detail: itemDetail
  });
}
function sev(r) {
  var c = r.level === "High" ? "c" : (r.level === "Medium" ? "u" : "l");
  return el("span", {class: "sev " + c, text: r.level || r.severity});
}
function stat(r) { return el("span", {class: "tag " + ({NEW_REGRESSION: "t0", EXISTING: "t1", FIXED: "t3"}[r.status_code] || "t4"), text: r.status}); }
function symText(r) {
  var s = r.symbolic;
  if (!s) return r.proof || "-";
  var t = {violated: "위반 (반례 있음)", holds: "안전", unknown: "판정 보류"}[s.result] || s.result;
  var cex = s.counterexample ? Object.keys(s.counterexample).map(function (k) { return k + " = " + s.counterexample[k]; }).join(", ") : "";
  return t + (cex ? ": " + cex : "");
}
function steps(list) {
  return el("ol", {class: "path"}, (list || []).map(function (s) {
    return el("li", {}, [el("span", {class: "arrow", text: s.arrow || ""}), code(s.func), " ", loc(s.file, s.line), " ", code(s.text)]);
  }));
}
function riskDetail(r) {
  var conds = r.conditions.length ? el("ul", {}, r.conditions.map(function (c) { return el("li", {}, [code(c)]); }))
                                  : el("div", {class: "muted", text: "모든 실행 (조건 없음)"});
  var t = r.taint || {}, s = r.symbolic, b = r.baseline || {}, ch = r.change;
  var kv = [
    el("div", {text: "판정"}), el("div", {}, [sev(r), " ", stat(r), " ", el("b", {text: r.kind_of_result || ""}), r.cwe ? el("b", {text: " " + r.cwe + " " + (r.cwe_name || "")}) : null]),
    el("div", {text: "검증 수준"}), el("div", {}, [vlev(r), el("span", {class: "small muted", text: "  Taint 경로 " + ((r.verification_flags || {}).taint ? "있음" : "없음")
      + " · 정적 규칙 위반 " + ((r.verification_flags || {}).static_rule ? "있음" : "없음") + " · 기호 실행 " + ((r.verification_flags || {}).symbolic || "미실행")})]),
    el("div", {text: "변경 함수와의 관계"}), el("div", {text: r.relation_label || r.relation || "-"}),
    el("div", {text: "위험"}), el("div", {}, [el("b", {text: r.kind_label}), " — " + r.risk]),
    el("div", {text: "위치"}), el("div", {}, [code(r.func), " ", loc(r.file, r.line)]),
    el("div", {text: "문장"}), el("div", {}, [code(r.src || r.expr)]),
    el("div", {text: "위험 연산"}), el("div", {}, [code(r.expr), "  피연산자 ", code(r.operand)])
  ];
  if (ch) kv.push(el("div", {text: "원인 변경"}), el("div", {}, [loc(ch.file, ch.line), " ", code(ch.func), " ",
    el("span", {class: "code old", text: ch.before || "(없음)"}), " → ", el("span", {class: "code new", text: ch.after || "(삭제)"}),
    ch.detail ? el("span", {class: "small muted", text: "  " + ch.detail}) : null]));
  kv.push(el("div", {text: "CPG"}), el("div", {text: r.how + (r.values && r.values.length ? " — 영향받은 값 " + r.values.join(", ") : "")
    + ((r.cpg || {}).control_dependency_changed ? " · 제어 의존 변경" : "")}));
  kv.push(el("div", {text: "Taint"}), el("div", {}, [t.flow_detected ? frag([el("b", {text: "외부 입력 도달 "}), code(t.source + ": " + t.source_label), " → ", code(t.value || r.operand)])
    : el("span", {class: "muted", text: "규칙의 외부 입력(Source)에서 이 피연산자까지 데이터 흐름 없음"})]));
  if (t.change) kv.push(el("div", {text: "Source→Sink 경로 변화"}), el("div", {}, [el("b", {text: t.change}), t.old_source ? el("span", {class: "small muted", text: "  변경 전 출발점 " + t.old_source}) : null]));
  var bd = r.bounds || {};
  if (bd.change) kv.push(el("div", {text: "버퍼·입력 범위"}), el("div", {}, [el("b", {text: bd.change}),
    el("div", {class: "small muted", text: "변경 후: " + ((bd.new || {}).limit || "-") + " · 허용 " + ((bd.new || {}).input_range || "-")
      + (bd.old ? "   |   변경 전: " + bd.old.limit + " · 허용 " + bd.old.input_range : "   |   변경 전: 해당 연산 없음")})]));
  kv.push(el("div", {text: "기호 실행"}), el("div", {}, [el("b", {text: symText(r)}), s ? el("span", {class: "small muted", text: "  속성 " + s.property + " · " + (s.detail || "") + " · " + s.engine}) : null]));
  kv.push(el("div", {text: "근거"}), el("div", {text: r.evidence || "-"}));
  var stKo = {violated: "위반 (반례 있음)", holds: "안전", unknown: "판정 보류"};
  kv.push(el("div", {text: "안전 속성 검증 (전 → 후)"}), el("div", {}, [
    el("b", {text: "변경 전 " + (b.old_symbolic ? (stKo[b.old_symbolic.result] || b.old_symbolic.result) : (b.old_state_label || "-"))
      + "  →  변경 후 " + (s ? (stKo[s.result] || s.result) : ({violated: "위반", holds: "안전", unknown: "판정 보류"}[r.state] || r.state))}),
    el("div", {class: "small muted", text: (b.old_location ? "변경 전 위치 " + b.old_location + " · " : "") + (b.old_evidence || "")
      + (b.old_symbolic && b.old_symbolic.counterexample ? " · 변경 전 반례 " + Object.keys(b.old_symbolic.counterexample).map(function (k) { return k + " = " + b.old_symbolic.counterexample[k]; }).join(", ") : "")})]));
  kv.push(el("div", {text: "검증 방법"}), el("div", {}, [el("b", {text: r.verification || "-"}), r.klee_harness ? el("div", {class: "small muted", text: "KLEE 하네스: " + r.klee_harness}) : null]));
  if (r.external) kv.push(el("div", {text: "외부 도구"}), el("div", {text: Object.keys(r.external).map(function (k) { return k + (r.external[k] ? " 경로 확인" : " 경로 없음"); }).join(" · ")}));
  return el("div", {}, [
    el("div", {class: "kv"}, kv),
    el("h4", {text: "차이가 발생하는 조건"}), conds,
    el("h4", {text: "근거 경로 1 — 변경 지점 → 위험 연산 (CPG)"}), steps(r.path),
    t.flow_detected ? el("h4", {text: "근거 경로 2 — 외부 입력(Source) → 위험 연산 (Taint)"}) : null,
    t.flow_detected ? steps(t.path) : null
  ]);
}
function riskTable(rows) {
  var showCwe = rows.some(function (r) { return !!r.cwe; });
  return new Table({
    rows: rows, search: function (r) { return [r.id, r.cwe || "", r.kind_label, r.func, r.file, r.expr, r.operand, r.origin, r.evidence, r.src, r.verification].concat(r.values || [], r.conditions).join(" "); },
    placeholder: "ID·위험 종류·함수·파일·연산 검색",
    filters: [
      {name: "level", label: "등급", options: uniq(rows.map(function (r) { return r.level; }), ["High", "Medium", "Low"]), test: function (r, v) { return r.level === v; }},
      {name: "status", label: "상태", options: uniq(rows.map(function (r) { return r.status; }), ["신규 발생", "판정 보류", "기존 존재", "해결"]), test: function (r, v) { return r.status === v; }},
      showCwe ? {name: "cwe", label: "CWE", options: uniq(rows.map(function (r) { return r.cwe; })), test: function (r, v) { return r.cwe === v; }}
              : {name: "kind", label: "위험 종류", options: uniq(rows.map(function (r) { return r.kind_label; })), test: function (r, v) { return r.kind_label === v; }},
      {name: "taint", label: "외부 입력", options: ["도달", "없음"], test: function (r, v) { return !!(r.taint || {}).flow_detected === (v === "도달"); }},
      {name: "pchg", label: "경로 변화", options: uniq(rows.map(function (r) { return (r.taint || {}).change || "없음"; })), test: function (r, v) { return ((r.taint || {}).change || "없음") === v; }},
      {name: "sym", label: "기호 실행", options: ["위반 (반례 있음)", "안전", "판정 보류", "미실행"], test: function (r, v) {
        var s = r.symbolic ? ({violated: "위반 (반례 있음)", holds: "안전", unknown: "판정 보류"}[r.symbolic.result]) : "미실행"; return s === v; }},
      {name: "origin", label: "원인 변경", options: uniq(rows.map(function (r) { return r.origin; })), test: function (r, v) { return r.origin === v; }}
    ],
    columns: [
      {label: "ID", render: function (r) { return el("b", {text: r.id}); }, cls: "nowrap"},
      {label: "등급·상태", render: function (r) { return frag([sev(r), el("br"), stat(r)]); }, cls: "nowrap"},
      {label: showCwe ? "CWE" : "위험 종류", render: function (r) { return showCwe ? frag([el("b", {text: r.cwe || "-"}), el("br"), el("span", {class: "small muted", text: r.kind_label})])
                                                               : frag([el("b", {text: r.kind_label}), el("br"), el("span", {class: "small muted", text: r.risk})]); }},
      {label: "위치", render: function (r) { return frag([code(r.func), el("br"), loc(r.file, r.line)]); }},
      {label: "위험 연산 (피연산자)", render: function (r) { return frag([code(short(r.expr, 110)), el("br"), el("span", {class: "small muted", text: "피연산자 "}), code(short(r.operand, 70))]); }},
      {label: "외부 입력 (Taint)", render: function (r) { var t = r.taint || {}; return frag([t.flow_detected ? frag([el("b", {text: t.source}), el("br"), el("span", {class: "small", text: t.source_label})]) : el("span", {class: "muted", text: "-"}),
        t.change && t.change !== "없음" ? el("div", {class: "small muted", text: t.change}) : null]); }},
      {label: "버퍼·입력 범위 (전 → 후)", render: function (r) { return el("span", {class: "small", text: short((r.bounds || {}).change || "-", 120)}); }},
      {label: "기호 실행", render: function (r) { return el("span", {class: "small", text: short(symText(r), 90)}); }},
      {label: "원인 변경", render: function (r) { var ch = r.change; return ch ? frag([el("span", {class: "code old", text: short(ch.before || "(없음)", 40)}), " → ",
        el("span", {class: "code new", text: short(ch.after || "(삭제)", 40)}), el("br"), loc(ch.file, ch.line)]) : frag([code(r.origin), el("br"), loc(r.origin_file, r.origin_line)]); }},
      {label: "검증 방법", render: function (r) { return el("span", {class: "small", text: short(r.verification || "", 140)}); }}
    ],
    detail: riskDetail
  });
}
function uniq(xs, order) {
  var s = {}; xs.forEach(function (x) { s[x] = 1; });
  var out = Object.keys(s);
  if (order) return order.filter(function (x) { return s[x]; });
  return out.sort();
}

/* ---------- 탭 ---------- */
var tabs = [], panes = {}, built = {};
function addTab(id, label, count, build) { tabs.push({id: id, label: label, count: count, build: build}); }
function show(id) {
  tabs.forEach(function (t) {
    t.btn.classList.toggle("on", t.id === id);
    if (t.id === id && !built[id]) { built[id] = t.build(panes[id]) || true; }
    panes[t.id].classList.toggle("on", t.id === id);
  });
  if (location.hash !== "#" + id) history.replaceState(null, "", "#" + id);
  window.scrollTo(0, 0);
}
function head(pane, title, desc) {
  pane.appendChild(el("h2", {text: title}));
  if (desc) pane.appendChild(el("p", {class: "desc", text: desc}));
}

var VL = {"기호 실행 반례 발견": "t0", "정적 규칙 위반": "t1", "Taint 경로 발견": "t2", "검증 불충분": "t4"};
function vlev(r) { return el("span", {class: "tag " + (VL[r.verification_level] || "t4"), title: "검증 수준", text: r.verification_level || "검증 불충분"}); }
var CLS = {"위반 경로 확인": ["t0", "⛔"], "위험 후보 탐지": ["t1", "⚠"], "검증자 확인 필요": ["t2", "❓"], "분석 불충분": ["t4", "…"], "변경 사실 확인": ["t3", "✓"], "해당 변경 없음": ["t4", "–"]};
function cls(name) { var c = CLS[name] || ["t4", ""]; return el("span", {class: "tag " + c[0], text: c[1] + " " + name}); }
var ITEM = {};
((D.review || {}).items || []).forEach(function (it) { ITEM[it.id] = it; });

/* ---------- 공통: 검토 상태 저장 (이 브라우저에만) ---------- */
var U = D.units || {units: [], functions: [], files: [], global_review: []};
function fnm(f) { return f.charAt(0) === "(" ? f : f + "()"; }
var UNIT = {}; U.units.forEach(function (u) { UNIT[u.id] = u; });
var RISK = {}; (A ? A.risks : []).forEach(function (r) { RISK[r.id] = r; });
var FUNCS = {}; U.functions.forEach(function (f) { FUNCS[f.file + "|" + f.func] = f; });
var LV = {red: ["🔴", "위반 경로 확인", "t0"], orange: ["🟠", "신규 위험 후보", "t1"], yellow: ["🟡", "검증자 확인 필요", "t2"],
          gray: ["⚪", "분석 불충분", "t4"], old: ["⚫", "기존·해결 위험", "t4"], none: ["", "관련 위험 결과 없음", "t3"]};
function lvchip(l, txt) { var x = LV[l] || LV.none; return el("span", {class: "tag " + x[2], text: (x[0] ? x[0] + " " : "") + (txt || x[1])}); }
var STORE_KEY = "cia-review|" + D.meta.time + "|" + D.meta.new_dir;
var ANS = {};
try { ANS = JSON.parse(localStorage.getItem(STORE_KEY) || "{}") || {}; } catch (e) { ANS = {}; }
function saveAns() { try { localStorage.setItem(STORE_KEY, JSON.stringify(ANS)); } catch (e) {} }
var CHOICES = ["", "적합", "부적합", "보류", "해당 없음"];
function qsOfUnit(u) { return u.checklist || []; }
function qsOfFile(fname) {
  var out = [];
  U.units.forEach(function (u) { if (u.file === fname) out = out.concat(qsOfUnit(u)); });
  U.functions.forEach(function (f) { if (f.file === fname) out = out.concat(f.checklist || []); });
  return out;
}
function statusOf(qs) {
  if (!qs.length) return "검토 대상 없음";
  var n = qs.filter(function (q) { return ANS[q.qid]; }).length;
  if (qs.some(function (q) { return ANS[q.qid] === "부적합"; })) return "부적합 있음";
  if (!n) return "미검토";
  return n === qs.length ? "검토 완료" : "검토 중 (" + n + "/" + qs.length + ")";
}
function statusChip(s) {
  var c = s.indexOf("부적합") === 0 ? "t0" : s === "검토 완료" ? "t3" : s.indexOf("검토 중") === 0 ? "t2" : "t4";
  return el("span", {class: "tag " + c, text: s});
}
var LISTENERS = [];
function onAnswer(fn) { LISTENERS.push(fn); }
function answerSelect(q) {
  var s = el("select", {class: "ans"}, CHOICES.map(function (c) { var o = el("option", {value: c, text: c || "선택"}); if ((ANS[q.qid] || "") === c) o.selected = true; return o; }));
  s.addEventListener("change", function () { if (s.value) ANS[q.qid] = s.value; else delete ANS[q.qid]; saveAns(); LISTENERS.forEach(function (f) { f(); }); });
  return s;
}
function exportCsv() {
  var rows = [["파일", "함수", "변경점", "라인", "검증 항목", "확인할 내용", "근거", "결과"]];
  U.units.forEach(function (u) {
    qsOfUnit(u).forEach(function (q) { rows.push([u.file, u.func, u.id, u.lo + "-" + u.hi, q.area, q.question, q.basis || "", ANS[q.qid] || ""]); });
  });
  U.functions.forEach(function (f) {
    (f.checklist || []).forEach(function (q) { rows.push([f.file, f.func, "함수 전체", "", q.area, q.question, q.basis || "", ANS[q.qid] || ""]); });
  });
  var csv = "﻿" + rows.map(function (r) { return r.map(function (x) { return '"' + String(x).replace(/"/g, '""') + '"'; }).join(","); }).join("\r\n");
  var a = el("a", {href: URL.createObjectURL(new Blob([csv], {type: "text/csv;charset=utf-8"})), download: "검토결과.csv"});
  document.body.appendChild(a); a.click(); a.remove();
}

/* ---------- 탭 01: 파일별 변경 검토 ---------- */
var FILES_API = null;
function diffBlock(list) {
  return el("div", {class: "diff"}, list.map(function (c) {
    var rows = [el("div", {class: "dl"}, [el("span", {class: "ln", text: "Line " + c.line}), el("span", {class: "small muted", text: " " + c.phrase})])];
    if (c.kind === "moved") rows.push(el("div", {class: "mv", text: "↕ " + (c.new || c.old)}));
    else {
      if (c.old) rows.push(el("div", {class: "rm", text: "- " + c.old}));
      if (c.new) rows.push(el("div", {class: "ad", text: "+ " + c.new}));
    }
    return el("div", {class: "dh"}, rows);
  }));
}
function issueCard(i) {
  var x = LV[i.level] || LV.yellow;
  var rows = [["변경 위치", i.where], ["위험 유형", i.title + (i.risk ? " — " + i.risk : "")], ["발견 근거", [i.basis].concat(i.evidence || []).concat((i.items || []).length ? ["검토 항목 " + i.items.join(", ")] : []).filter(function (t) { return t; }).join(" · ") || "-"]];
  if (i.before || i.after) rows.push(["변경 전 → 후", (i.before || "-") + " → " + (i.after || "-")]);
  rows.push(["검증 수준", i.verification_level]);
  rows.push(["판단 구분", i.fact_vs_judgement]);
  return el("div", {class: "icard " + i.level}, [el("div", {class: "ih"}, [el("b", {text: x[0] + " " + x[1] + " — " + i.title})]),
    el("table", {class: "kvt"}, [el("tbody", {}, rows.map(function (r) { return el("tr", {}, [el("th", {text: r[0]}), el("td", {text: r[1]})]); }))])]);
}
function checklistTable(qs, onchange) {
  if (!qs.length) return el("div", {class: "small muted", text: "검증자가 따로 확인할 항목 없음"});
  return el("table", {class: "cl"}, [el("thead", {}, [el("tr", {}, ["검증 항목", "검증자가 확인할 내용", "결과"].map(function (h) { return el("th", {text: h}); }))]),
    el("tbody", {}, qs.map(function (q) {
      return el("tr", {}, [el("td", {class: "nowrap"}, [el("b", {text: q.area}), el("div", {class: "small muted", text: q.item !== "-" ? "항목 " + q.item : ""})]),
        el("td", {}, [q.question, q.basis ? el("div", {class: "small muted", text: "근거: " + q.basis}) : null]), el("td", {}, [answerSelect(q)])]);
    }))]);
}
function unitCard(u, showOld) {
  var issues = u.issues.filter(function (i) { return showOld || i.level !== "old"; });
  var deps = [];
  if (u.deps.calls_removed.length) deps.push("호출 삭제 " + u.deps.calls_removed.join(", "));
  if (u.deps.calls_added.length) deps.push("호출 추가 " + u.deps.calls_added.join(", "));
  if (u.deps.writes.length) deps.push("쓰는 전역 " + u.deps.writes.slice(0, 4).join(", "));
  var st = el("span");
  function refresh() { st.textContent = ""; st.appendChild(statusChip(statusOf(qsOfUnit(u)))); }
  refresh(); onAnswer(refresh);
  var sumRows = [["변경 유형", u.type_label], ["자동 분석", u.summary],
    ["발견된 위험", issues.length ? issues.map(function (i) { return (LV[i.level] || LV.yellow)[0] + " " + i.title; }).join(" / ") : "없음 (변경 사실만 확인)"],
    ["의존성 변화", deps.join(" · ") || "-"], ["검증 수준", u.verification_level]];
  var facts = u.facts || [];
  var ftab = function (list) { return el("table", {class: "cl"}, [el("thead", {}, [el("tr", {}, ["분석 항목", "분석 결과", "상태"].map(function (h) { return el("th", {text: h}); }))]),
    el("tbody", {}, list.map(function (f) { return el("tr", {}, [el("td", {class: "nowrap", text: f.area}), el("td", {text: f.result}), el("td", {class: "nowrap"}, [el("span", {class: "tag " + (f.status === "변경 확인" ? "t3" : f.status === "위반 경로 확인" ? "t0" : f.status === "위험 후보" || f.status === "추가 검증 필요" ? "t1" : "t2"), text: f.status})])]); }))]); };
  var kids = [
    el("div", {class: "uh"}, [el("b", {text: fnm(u.func) + "  "}), el("span", {class: "small muted", text: "변경점 " + u.id + " · Line " + (u.lo === u.hi ? u.lo : u.lo + " ~ " + u.hi) + "  "}), lvchip(u.level), " ", st]),
    el("table", {class: "kvt sum"}, [el("tbody", {}, sumRows.map(function (r) { return el("tr", {}, [el("th", {text: r[0]}), el("td", {text: r[1]})]); }).concat([el("tr", {}, [el("th", {text: "검토 상태"}), el("td", {}, [st])])]))]),
    el("h4", {class: "step", text: "① 무엇이 변경되었는가"}), diffBlock(u.changes.slice(0, 6)),
    u.changes.length > 6 ? el("details", {class: "more-d"}, [el("summary", {text: "변경 코드 전체 보기 (" + u.changes.length + "곳)"}), diffBlock(u.changes)]) : null,
    el("div", {class: "conc"}, [el("b", {text: "자동 분석 요약  "}), u.summary]),
    el("h4", {class: "step", text: "② 스크립트가 무엇을 확인했는가"}), ftab(facts.slice(0, 8)),
    facts.length > 8 ? el("details", {class: "more-d"}, [el("summary", {text: "확인 결과 더 보기 (" + facts.length + "건)"}), ftab(facts.slice(8))]) : null,
    el("h4", {class: "step", text: "③ 어떤 문제가 발견되었는가"}),
    issues.length ? frag(issues.map(issueCard)) : el("div", {class: "small muted", text: "발견된 문제 없음 — 코드 변경 사실만 확인되었습니다."}),
    el("h4", {class: "step", text: "④ 검증자가 추가로 확인할 사항"}), checklistTable(qsOfUnit(u)),
    el("div", {class: "btns"}, [
      el("details", {class: "more-d"}, [el("summary", {text: "변경 코드 전체 보기"}), diffBlock(u.changes)]),
      el("button", {class: "lnk", text: "의존성 경로 보기 →", onclick: function () { goFunc(u.file, u.func); }}),
      el("details", {class: "more-d"}, [el("summary", {text: "분석 근거 보기"}), frag(u.risks.concat(u.prop_risks).map(function (rid) { return RISK[rid] ? el("div", {class: "rcard"}, [el("b", {text: rid + " "}), riskDetail(RISK[rid])]) : null; })),
        u.risks.length + u.prop_risks.length ? null : el("div", {class: "small muted", text: "이 변경점에 연결된 Taint·기호 실행 결과 없음 (AST·CFG 비교 결과만)"})])
    ])
  ];
  return el("div", {class: "ucard " + u.level, id: "unit-" + u.id}, kids);
}
addTab("files", "📁 파일별 변경 검토", U.files.length, function (p) {
  var showOld = false;
  head(p, "파일별 변경 검토", "변경된 파일을 선택하면 변경 함수별로 ① 무엇이 변경되었는가 → ② 스크립트가 확인한 것 → ③ 발견된 문제 → ④ 검증자 확인 사항을 같은 순서로 보여 줍니다. "
    + "체크리스트 결과는 이 브라우저에만 저장되며 'CSV 내보내기'로 공유할 수 있습니다.");
  var nUnits = U.units.length, cnt = {red: 0, orange: 0, yellow: 0};
  U.units.forEach(function (u) { u.issues.forEach(function (i) { if (cnt[i.level] !== undefined) cnt[i.level]++; }); });
  var allQ = []; U.units.forEach(function (u) { allQ = allQ.concat(qsOfUnit(u)); }); U.functions.forEach(function (f) { allQ = allQ.concat(f.checklist || []); });
  var prog = el("div", {class: "v"});
  function refreshProg() { prog.textContent = allQ.filter(function (q) { return ANS[q.qid]; }).length + " / " + allQ.length; }
  refreshProg(); onAnswer(refreshProg);
  p.appendChild(el("div", {class: "cards"}, [
    el("div", {class: "card"}, [el("div", {class: "v", text: U.files.length}), el("div", {class: "k", text: "변경 파일"})]),
    el("div", {class: "card"}, [el("div", {class: "v", text: U.functions.length}), el("div", {class: "k", text: "변경 함수"})]),
    el("div", {class: "card"}, [el("div", {class: "v", text: nUnits}), el("div", {class: "k", text: "변경점 묶음"})]),
    el("div", {class: "card" + (cnt.red ? " alert" : "")}, [el("div", {class: "v", text: cnt.red}), el("div", {class: "k", text: "🔴 위반 경로 확인"})]),
    el("div", {class: "card"}, [el("div", {class: "v", text: cnt.orange}), el("div", {class: "k", text: "🟠 신규 위험 후보"})]),
    el("div", {class: "card"}, [prog, el("div", {class: "k", text: "체크리스트 진행"})])]));
  var listView = el("div"), detailView = el("div");
  p.appendChild(listView); p.appendChild(detailView);
  function fileRows() {
    var q = el("input", {type: "search", placeholder: "파일·함수 검색"});
    var tb = el("tbody");
    function draw() {
      tb.textContent = "";
      U.files.forEach(function (f) {
        var t = (f.file + " " + f.funcs.join(" ")).toLowerCase();
        if (q.value && t.indexOf(q.value.toLowerCase()) < 0) return;
        var risk = f.level === "red" ? lvchip("red") : f.level === "orange" ? lvchip("orange", "⚠️ 검토 필요") : f.level === "yellow" ? lvchip("yellow") : f.level === "gray" ? lvchip("gray") : el("span", {class: "muted", text: "관련 위험 결과 없음"});
        var tr = el("tr", {class: "row click"}, [el("td", {}, [el("b", {}, [code(f.file)])]),
          el("td", {}, [f.funcs.length ? f.funcs.length + "개" : "-", el("div", {class: "small muted", text: short(f.funcs.join(", "), 80)})]),
          el("td", {}, [risk, f.n_issues ? el("span", {class: "small muted", text: " " + f.n_issues + "건"}) : null]),
          el("td", {text: f.has_deps ? "있음" : "-"}), el("td", {text: f.units.length + "곳"}), el("td", {}, [statusChip(statusOf(qsOfFile(f.file)))])]);
        tr.addEventListener("click", function () { openFile(f.file); });
        tb.appendChild(tr);
      });
    }
    q.addEventListener("input", draw); onAnswer(draw); draw();
    return el("div", {}, [el("div", {class: "toolbar"}, [q, el("button", {class: "clear", text: "CSV 내보내기 (체크리스트 결과)", onclick: exportCsv})]),
      el("div", {class: "tw"}, [el("table", {}, [el("thead", {}, [el("tr", {}, ["파일명", "변경 함수", "위험 확인", "의존성 변화", "변경점", "검토 상태"].map(function (h) { return el("th", {text: h}); }))]), tb])]),
      el("p", {class: "small muted", text: "정렬: 위반 경로가 확인된 파일 → 신규 위험 후보가 있는 파일 → 나머지 변경 파일. 위험 결과가 없다는 것은 안전이 확인되었다는 뜻이 아닙니다(분석 범위는 '분석 근거 및 범위' 탭)."})]);
  }
  listView.appendChild(fileRows());
  function openFile(fname, uid) {
    var f = U.files.filter(function (x) { return x.file === fname; })[0];
    if (!f) return;
    listView.style.display = "none"; detailView.textContent = "";
    var oldChk = el("input", {type: "checkbox"});
    oldChk.checked = showOld;
    oldChk.addEventListener("change", function () { showOld = oldChk.checked; openFile(fname); });
    detailView.appendChild(el("div", {class: "toolbar"}, [el("button", {class: "clear", text: "← 파일 목록", onclick: function () { detailView.textContent = ""; listView.style.display = ""; window.scrollTo(0, 0); }}),
      el("label", {class: "small"}, [oldChk, " 기존·해결 위험도 표시"])]));
    detailView.appendChild(el("h2", {text: "📄 " + f.file}));
    if ((f.file_review || []).length) {
      var fq = [], seen = {};
      f.file_review.forEach(function (x) { if (x["class"] !== "변경 사실 확인" && x.reviewer && !seen[x.item]) { seen[x.item] = 1; fq.push({qid: "FILE:" + f.file + ":" + x.item, area: (ITEM[x.item] || {}).reviewer_focus || x.item, item: x.item, question: x.reviewer, basis: x.summary}); } });
      detailView.appendChild(el("details", {class: "fn", open: true}, [el("summary", {}, [el("b", {text: "파일 수준 변경 (매크로·조건부 컴파일·헤더 등) " + f.file_review.length + "건"})]),
        el("ul", {class: "small"}, f.file_review.map(function (x) { return el("li", {}, [cls(x["class"]), " " + x.summary]); })), checklistTable(fq)]));
    }
    f.funcs.forEach(function (fn) {
      var F = FUNCS[f.file + "|" + fn] || {checklist: []};
      detailView.appendChild(el("h3", {class: "fnh", text: "변경 함수: " + fnm(fn)}));
      if ((F.checklist || []).length) detailView.appendChild(el("details", {class: "more-d"}, [el("summary", {text: "함수 전체에 대한 확인 사항 " + F.checklist.length + "건 (의존성·인터페이스)"}), checklistTable(F.checklist)]));
      U.units.filter(function (u) { return u.file === f.file && u.func === fn; }).forEach(function (u) { detailView.appendChild(unitCard(u, showOld)); });
    });
    var target = uid ? document.getElementById("unit-" + uid) : null;
    if (target) { target.scrollIntoView({behavior: "instant", block: "start"}); target.classList.add("flash"); } else window.scrollTo(0, 0);
  }
  FILES_API = {openFile: openFile};
});
function goUnit(uid) {
  var u = UNIT[uid]; if (!u) return;
  show("files"); if (FILES_API) FILES_API.openFile(u.file, uid);
}

/* ---------- 탭 02: 검토 필요 항목 ---------- */
addTab("issues", "⚠️ 검토 필요 항목", null, function (p) {
  head(p, "검토 필요 항목", "변경 파일을 하나씩 열지 않고, 위험이 발견되었거나 검증자 판단이 필요한 변경점만 모아 봅니다. 행을 누르면 '파일별 변경 검토'의 해당 변경점으로 이동합니다. "
    + "같은 위치·같은 문제를 여러 분석이 함께 찾은 경우 하나로 합쳤습니다.");
  p.appendChild(el("table", {class: "cl"}, [el("thead", {}, [el("tr", {}, ["구분", "의미", "검토 우선순위"].map(function (h) { return el("th", {text: h}); }))]),
    el("tbody", {}, [["red", "정의된 안전 속성을 위반하는 반례 또는 경로가 확인됨 (기호 실행 반례·상수·조건 구간 증명)", "높음"],
      ["orange", "변경 후 새로 나타난 잠재적 결함 — Taint 경로나 정적 규칙으로 찾았지만 위반은 확정되지 않음", "높음"],
      ["yellow", "코드 분석만으로 적합 여부를 판단할 수 없음 (요구사항·시험 근거 필요)", "보통"],
      ["gray", "분석 범위·모델링 한계로 판단 불가", "별도 관리"]].map(function (r) { return el("tr", {}, [el("td", {}, [lvchip(r[0])]), el("td", {text: r[1]}), el("td", {text: r[2]})]); }))]));
  var newRisks = (A ? A.risks : []).filter(function (r) { return r.status_code === "NEW_REGRESSION"; });
  if (newRisks.length) {
    var proof = newRisks.filter(function (r) { return r.state === "violated"; }).length;
    p.appendChild(el("p", {class: "small", text: "변경으로 새로 생긴 위험 레코드 " + newRisks.length + "개 중 반례·증명으로 위반이 확인된 것 " + proof + "개, Taint 경로·조건 변경만 확인된 후보 " + (newRisks.length - proof) + "개입니다. 후보는 취약점으로 확정된 것이 아닙니다."}));
  }
  var rows = [];
  U.units.forEach(function (u) { u.issues.forEach(function (i) { if (i.level !== "old") rows.push({lv: i.level, u: u, title: i.title, vl: i.verification_level, extra: i.risk}); }); });
  U.units.forEach(function (u) {
    if (u.level === "yellow" && !u.issues.some(function (i) { return i.level === "yellow"; })) rows.push({lv: "yellow", u: u, title: u.type_label, vl: "검증자 판단 " + qsOfUnit(u).length + "건", extra: u.summary});
  });
  var gray = (U.global_review || []).filter(function (f) { return f["class"] === "분석 불충분"; });
  U.files.forEach(function (f) { (f.file_review || []).forEach(function (x) { if (x["class"] === "분석 불충분") gray.push(x); }); });
  ["red", "orange", "yellow"].forEach(function (l) {
    var rs = rows.filter(function (r) { return r.lv === l; });
    var tb = el("tbody", {}, rs.map(function (r) {
      var tr = el("tr", {class: "row click"}, [el("td", {}, [code(r.u.file)]), el("td", {}, [code(r.u.func)]), el("td", {}, [el("b", {text: r.title}), el("div", {class: "small muted", text: short(r.extra || "", 140)})]),
        el("td", {text: r.vl}), el("td", {}, [statusChip(statusOf(qsOfUnit(r.u)))])]);
      tr.addEventListener("click", function () { goUnit(r.u.id); });
      return tr;
    }));
    p.appendChild(el("details", {class: "fn", open: l !== "yellow" || rs.length <= 30}, [el("summary", {}, [lvchip(l), " " + rs.length + "건"]),
      rs.length ? el("div", {class: "tw"}, [el("table", {}, [el("thead", {}, [el("tr", {}, ["파일", "함수", "발견된 문제", "검증 수준", "상태"].map(function (h) { return el("th", {text: h}); }))]), tb])])
                : el("div", {class: "small muted", text: "없음"})]));
  });
  p.appendChild(el("details", {class: "fn"}, [el("summary", {}, [lvchip("gray"), " " + gray.length + "건"]),
    el("ul", {class: "small"}, gray.map(function (x) { return el("li", {}, [x.summary, x.reviewer ? el("div", {class: "muted", text: "→ " + x.reviewer}) : null]); }))]));
});

/* ---------- 탭 03: 변경 영향 및 의존성 ---------- */
var IMPACT_API = null;
function goFunc(file, func) { show("impact"); if (IMPACT_API) IMPACT_API.open(file, func); }
addTab("impact", "🔗 변경 영향 및 의존성", U.functions.length, function (p) {
  head(p, "변경 영향 및 의존성", "변경 함수에서 출발해 영향이 어디까지 전달되는지 보여 줍니다. 기본으로 직접 변경과 직접 영향만 보이고, 간접 영향은 '영향 경로 더 보기'로 펼칩니다. "
    + "함수 호출·변수 접근의 변화는 의존성 변화(영향 분석)이며, 검증되지 않은 입력이 위험 연산에 닿는 것(잠재 취약점)과 따로 표시합니다.");
  var q = el("input", {type: "search", placeholder: "함수·파일 검색"});
  var only = el("input", {type: "checkbox"}); only.checked = true;
  var box = el("div");
  p.appendChild(el("div", {class: "toolbar"}, [q, el("label", {class: "small"}, [only, " 영향·의존성 변화가 있는 함수만"])]));
  p.appendChild(box);
  function itemLine(i) {
    var it = A.items[i];
    return el("li", {}, [code(it.func), " → ", code(it.name), el("span", {class: "small muted", text: "  " + it.how_kind + ": " + short(it.how, 80)}),
      el("details", {class: "more-d"}, [el("summary", {text: "경로"}), steps(it.path)])]);
  }
  function card(f) {
    var row = f.deps || {};
    var calls = (row.calls_added || []).map(function (x) { return "+ " + x; }).concat((row.calls_removed || []).map(function (x) { return "− " + x; }));
    var vars = (row.writes_added || []).map(function (x) { return "쓰기 + " + x; }).concat((row.writes_removed || []).map(function (x) { return "쓰기 − " + x; }))
      .concat((row.reads_added || []).map(function (x) { return "읽기 + " + x; })).concat((row.reads_removed || []).map(function (x) { return "읽기 − " + x; }));
    var related = f.shared.map(function (s) { return s.var + (s.isr ? " (ISR 공유)" : " (여러 진입점 공유)"); })
      .concat(f.direct.slice(0, 6).map(function (i) { return A.items[i].name; }));
    related = related.filter(function (x, i) { return related.indexOf(x) === i; });
    var flow = el("div", {class: "flow"}, [
      el("div", {class: "fb fact"}, [el("div", {class: "small muted", text: "변경 함수"}), el("b", {text: fnm(f.func)}), el("div", {class: "small", text: f.summary})]),
      el("div", {class: "arr", text: "▼"}),
      el("div", {class: "fb fact"}, [el("div", {class: "small muted", text: "변경된 함수 호출 · 변수 접근 (자동 분석 확인)"}), el("div", {class: "small", text: calls.concat(vars).join(" · ") || "호출·변수 접근 변화 없음 (값·조건만 변경)"})]),
      el("div", {class: "arr", text: "▼"}),
      el("div", {class: "fb fact"}, [el("div", {class: "small muted", text: "연관 상태 및 데이터 (자동 분석 확인)"}), el("div", {class: "small", text: related.join(" · ") || "-"})]),
      el("div", {class: "arr", text: "▼"}),
      el("div", {class: "fb dir"}, [el("div", {class: "small muted", text: "검증이 필요한 동작 (변경에서 도출한 검토 방향)"}), el("div", {class: "small", text: f.behaviors.join(" · ") || "-"})])]);
    var kids = [el("div", {class: "uh"}, [el("b", {text: fnm(f.func) + "  "}), el("span", {class: "small muted", text: f.file + "  "}), lvchip(f.level)]), flow];
    if (f.call_pairs.length) kids.push(el("h4", {text: "함수 호출 변경"}), el("table", {class: "cl"}, [el("thead", {}, [el("tr", {}, [el("th", {text: "변경 전"}), el("th", {text: "변경 후"})])]),
      el("tbody", {}, f.call_pairs.map(function (pr) { return el("tr", {}, [el("td", {}, [pr[0] ? code(pr[0] + "()") : el("span", {class: "muted", text: "(없음)"})]), el("td", {}, [pr[1] ? code(pr[1] + "()") : el("span", {class: "muted", text: "(삭제)"})])]); }))]));
    kids.push(el("h4", {text: "의존성 3단계"}));
    kids.push(el("div", {class: "lvl"}, [el("b", {text: "① 직접 변경  "}), el("span", {class: "small", text: calls.concat(vars).join(" · ") || "-"})]));
    kids.push(el("div", {class: "lvl"}, [el("b", {text: "② 직접 영향  "}), f.direct.length ? el("ul", {class: "small"}, f.direct.slice(0, 12).map(itemLine)) : el("span", {class: "small muted", text: "-"})]));
    kids.push(el("details", {class: "more-d"}, [el("summary", {text: "③ 간접 영향 — 영향 경로 더 보기 (" + f.indirect.length + "건)"}), el("ul", {class: "small"}, f.indirect.slice(0, 200).map(itemLine))]));
    if (f.taint_risks.length) kids.push(el("div", {class: "vuln"}, [el("b", {text: "⚠️ 잠재 취약점 (외부 입력 → 위험 연산) — 의존성 변화와 별개"}),
      el("ul", {class: "small"}, f.taint_risks.map(function (rid) { var r = RISK[rid]; return el("li", {}, [el("button", {class: "lnk", text: rid, onclick: function () {
        var uu = U.units.filter(function (u) { return u.risks.concat(u.prop_risks).indexOf(rid) >= 0; })[0]; if (uu) goUnit(uu.id); }}), " " + (r ? r.kind_label + " " + r.expr + " — " + r.verification_level : "")]); }))]));
    if (f.shared.length) kids.push(el("div", {class: "small muted", text: "공유 데이터 접근 (의존 관계, 위험 아님): " + f.shared.map(function (s) { return s.var + " ← " + s.entries.join(", "); }).join(" · ")}));
    return el("div", {class: "ucard", id: "fn-" + f.file + "|" + f.func}, kids);
  }
  function draw() {
    box.textContent = "";
    U.functions.slice().sort(function (a, b) { var o = ["red", "orange", "yellow", "gray", "none"]; return o.indexOf(a.level) - o.indexOf(b.level); }).forEach(function (f) {
      if (q.value && (f.func + " " + f.file).toLowerCase().indexOf(q.value.toLowerCase()) < 0) return;
      if (only.checked && !f.deps && !f.direct.length && !f.indirect.length && !f.taint_risks.length) return;
      box.appendChild(card(f));
    });
    if (A && A.entries.length) box.appendChild(el("details", {class: "fn"}, [el("summary", {text: "참고: 진입점(태스크·ISR)별 영향 함수 " + A.entries.length + "개"}),
      el("ul", {class: "small"}, A.entries.map(function (e) { return el("li", {}, [code(e.entry), (e.isr ? " (ISR 추정)" : "") + ": " + e.affected_functions.join(", ")]); }))]));
  }
  q.addEventListener("input", draw); only.addEventListener("change", draw); draw();
  IMPACT_API = {open: function (file, func) { q.value = func; only.checked = false; draw(); var t = document.getElementById("fn-" + file + "|" + func); if (t) t.scrollIntoView({block: "start"}); }};
});

/* ---------- 탭 04: 분석 근거 및 범위 ---------- */
addTab("scope", "🔍 분석 근거 및 범위", null, function (p) {
  head(p, "분석 근거 및 범위", "어떤 기술로 무엇을 분석했고, 무엇을 분석하지 못했는지입니다. 위험이 탐지되지 않았다는 것을 안전 확인으로 해석하지 않도록, 분석 한계를 함께 확인하세요.");
  var m = D.meta, sec = (A && A.security) || {}, eng = sec.engine || {}, tools = sec.tools || {};
  if ((m.warnings || []).length) p.appendChild(el("div", {class: "box warn"}, [el("h3", {text: "분석 범위 경고"}), el("ul", {}, m.warnings.map(function (w) { return el("li", {text: w}); }))]));
  if ((D.auto_config || []).length) {
    p.appendChild(el("h3", {text: "자동 탐지한 빌드 설정 (--auto-config)"}));
    p.appendChild(el("table", {class: "cl"}, [el("thead", {}, [el("tr", {}, ["확신도", "항목", "값", "근거"].map(function (h) { return el("th", {text: h}); }))]),
      el("tbody", {}, D.auto_config.map(function (d) {
        return el("tr", {}, [el("td", {}, [el("span", {class: "tag " + (d.confidence === "높음" ? "t3" : d.confidence === "낮음" ? "t1" : "t2"), text: d.confidence})]),
          el("td", {text: d.item}), el("td", {}, [code(d.value)]),
          el("td", {class: "small", text: d.evidence + (d.alternatives.length ? " (대안: " + d.alternatives.slice(0, 4).join(", ") + ")" : "")})]);
      }))]));
  }
  var R = D.review || {items: [], groups: [], classes: []};
  var nRisk = A ? A.risks.length : 0, nSym = A ? A.risks.filter(function (r) { return r.symbolic; }).length : 0;
  var ex = A ? A.excluded : {};
  var algo = [
    ["AST Diff", "변경 노드 " + (A ? A.changes.length : 0) + "개 (변경 전후 문장·조건·식 비교, 의미 차이: 연산자·상수·호출·형변환·평가 순서)", "파일별 변경 검토 ①"],
    ["CFG", "변경 함수의 분기·반복·조기 종료·문장 이동, else-if 사슬", "파일별 변경 검토 ②, 검토 항목 3-1·3-2·4-4"],
    ["CPG", eng.cpg ? eng.cpg + (tools.cpg ? " — 노드 " + tools.cpg.nodes + "개, 간선 " + tools.cpg.edges + "개 (security/cpg.json)" : "") : "-", "변경 영향 및 의존성"],
    ["Taint Analysis", (eng.taint || "-") + " · 규칙 " + (eng.rules || "-"), "검토 필요 항목, 분석 근거 보기"],
    ["Symbolic Execution", (eng.symbolic || "-") + " · 위험 연산 " + nRisk + "개 중 " + nSym + "개 기호 실행", "분석 근거 보기 (반례·속성)"],
    ["분석 한계", "파싱 실패 " + Object.keys(ex.parse_errors || {}).length + "개 · 본문 없는 외부 함수 " + (ex.extern_functions || []).length + "개 · 풀지 못한 함수 포인터 " + (ex.unresolved_calls || []).length + "개 · 비활성 #if 경로 미분석", "아래 상세"]];
  p.appendChild(el("h3", {text: "사용한 분석 기술"}));
  p.appendChild(el("table", {class: "cl"}, [el("thead", {}, [el("tr", {}, ["분석 기술", "이 보고서의 분석 결과", "보는 곳"].map(function (h) { return el("th", {text: h}); }))]),
    el("tbody", {}, algo.map(function (r) { return el("tr", {}, [el("td", {}, [el("b", {text: r[0]})]), el("td", {text: r[1]}), el("td", {class: "small", text: r[2]})]); }))]));
  if (R.items.length) {
    p.appendChild(el("h3", {text: "자동 검토 항목 (25개)"}));
    p.appendChild(el("div", {class: "small muted", text: "각 항목에서 무엇을 검토했는지와 결과 분류별 건수입니다. 항목별 상세 결과는 파일별 변경 검토의 각 변경점에 연결되어 있습니다."}));
    R.groups.forEach(function (g) {
      var its = R.items.filter(function (it) { return it.group === g.name; });
      p.appendChild(el("details", {class: "fn"}, [el("summary", {}, [el("b", {text: g.id + ". " + g.name}), el("span", {class: "small muted", text: "  " + g.algorithms})]),
        el("table", {class: "cl"}, [el("thead", {}, [el("tr", {}, ["번호", "자동 분석 내용", "결과", "건수", "검토한 내용", "검증자가 판단할 내용"].map(function (h) { return el("th", {text: h}); }))]),
          el("tbody", {}, its.map(function (it) {
            return el("tr", {}, [el("td", {text: it.id}), el("td", {text: it.title}), el("td", {}, [cls(it.overall)]),
              el("td", {class: "small", text: Object.keys(it.counts).filter(function (k) { return it.counts[k]; }).map(function (k) { return k + " " + it.counts[k]; }).join(" · ") || "-"}),
              el("td", {class: "small", text: it.notes.join(" · ")}), el("td", {class: "small", text: it.reviewer_focus})]);
          }))])]));
    });
  }
  if (A) {
    var pe = Object.keys(ex.parse_errors || {});
    var PD = D.parse_diag || [];
    p.appendChild(el("div", {class: "box" + (pe.length ? " warn" : "")}, [el("h3", {text: "파싱 실패 (분석 제외) " + pe.length + "개"}),
      PD.length ? el("table", {class: "cl"}, [el("thead", {}, [el("tr", {}, ["원인", "대상", "파일 수", "예", "해결 방법"].map(function (h) { return el("th", {text: h}); }))]),
        el("tbody", {}, PD.map(function (g) {
          var adv = (g.fix ? "-D \"" + g.fix + "\" — " : "") + ((g.defined_in || []).length ? "정의 위치: " + g.defined_in.slice(0, 3).join(", ") + " — " : "") + g.advice;
          return el("tr", {}, [el("td", {}, [el("b", {text: g.cause})]), el("td", {}, [code(g.detail)]), el("td", {text: String(g.count)}),
            el("td", {class: "small"}, [code(g.files[0] || ""), g.where ? " (" + g.where + ")" : "", g.line_text ? el("div", {class: "muted"}, [code(g.line_text)]) : null]),
            el("td", {class: "small", text: adv})]);
        }))]) : null,
      pe.length ? el("details", {class: "fn"}, [el("summary", {text: "파일별 오류 " + pe.length + "개"}), el("ul", {}, pe.map(function (f) { return el("li", {}, [code(f), " ", el("span", {class: "small muted", text: String(ex.parse_errors[f] || "").split("\n").filter(function (x) { return x.trim(); }).slice(-1)[0] || ""})]); }))])
                : el("div", {class: "muted", text: "없음 (변경 전 " + m.analyzed.old + "개 / 변경 후 " + m.analyzed.new + "개 파일 모두 분석)"})]));
    var secBox = function (title, desc, xs, fmt) {
      if (!xs || !xs.length) return;
      p.appendChild(el("details", {class: "fn"}, [el("summary", {text: title + " " + xs.length + "개"}), desc ? el("p", {class: "small muted", text: desc}) : null,
        el("div", {class: "small"}, [frag(xs.map(function (x, i) { return frag([i ? ", " : "", fmt(x)]); }))])]));
    };
    secBox("본문 없는 외부 함수", "포인터 인자가 가리키는 메모리를 읽고 쓰는 것으로 보수적으로 가정했습니다 (const 포인터 인자는 읽기만).", ex.extern_functions, function (e) { return frag([code(e.name), e.modeled ? " (모델)" : ""]); });
    secBox("대상을 풀지 못한 함수 포인터 호출", "반환값이 모든 인자에 의존한다고 보았고, 피호출 함수 내부 영향은 빠져 있습니다.", ex.unresolved_calls, function (u) { return frag([code(u.func), ":" + u.line + " ", code(u.call + "(...)")]); });
    secBox("대상을 모르는 포인터", "실제로 전역을 가리키면 영향이 누락될 수 있습니다.", ex.unresolved_pointers, code);
    secBox("상수 주소(하드웨어 레지스터) 접근", "읽기·쓰기 효과만 반영하며 하드웨어 동작은 모델링하지 않습니다.", ex.hw_access, code);
    if ((ex.assumptions || []).length) p.appendChild(el("details", {class: "fn"}, [el("summary", {text: "분석 가정"}), el("ul", {class: "small"}, ex.assumptions.map(function (w) { return el("li", {text: w}); }))]));
    if (A.notes.length) p.appendChild(el("details", {class: "fn"}, [el("summary", {text: "동작 변화 없음으로 본 변경 " + A.notes.length + "개"}), el("ul", {class: "small"}, A.notes.map(function (n) { return el("li", {text: n}); }))]));
    var tl = [];
    if (tools.joern) tl.push("Joern: " + (tools.joern.ran ? "실행함" : (tools.joern.note || "")) + " — security/joern/taint.sc");
    if (tools.codeql) tl.push("CodeQL: " + (tools.codeql.ran ? "실행함" : (tools.codeql.note || "")) + " — security/codeql/ChangeTaint.ql");
    if (tools.klee) tl.push("KLEE: 하네스 " + tools.klee.harnesses + "개, 실행 " + tools.klee.ran + "개 — security/klee/");
    if (tl.length) p.appendChild(el("details", {class: "fn"}, [el("summary", {text: "외부 도구 교차 검증 입력"}), el("ul", {class: "small"}, tl.map(function (x) { return el("li", {text: x}); }))]));
    p.appendChild(el("details", {class: "fn"}, [el("summary", {text: "전체 보안 분석 결과 표 (" + A.risks.length + "건)"}), riskTable(A.risks).node]));
    p.appendChild(el("details", {class: "fn"}, [el("summary", {text: "전체 영향 목록 (" + A.items.length + "건)"}), itemTable(A.items).node]));
  }
  if ((m.outputs || []).length) p.appendChild(el("details", {class: "fn"}, [el("summary", {text: "결과 파일"}), el("ul", {class: "small"}, m.outputs.map(function (o) { return el("li", {}, [code(o)]); }))]));
});

/* ---------- 화면 구성 ---------- */
document.getElementById("paths").appendChild(frag([
  "변경 전 ", code(D.meta.old_dir), "  →  변경 후 ", code(D.meta.new_dir), "   ·   " + D.meta.time]));
(function () {
  var m = D.meta, an = m.analyzed || {}, ws = m.warnings || [];
  var partial = ws.some(function (w) { return w.indexOf("--scope changed") >= 0; });
  var pe = A ? Object.keys(A.excluded.parse_errors || {}).length : 0;
  var msg = partial ? "⚠️ 부분 분석 — 변경 및 연관 C 파일 " + an.new + "개 분석 (변경 전 " + an.old + "개). 분석 범위 밖의 함수 호출·전역 변수 영향은 누락될 수 있습니다."
                    : "전체 분석 — 변경 전 " + an.old + "개 / 변경 후 " + an.new + "개 C 파일.";
  if (pe) msg += "  ⚠️ 파싱 실패 " + pe + "개 파일은 분석에서 빠졌습니다.";
  msg += "  위험이 탐지되지 않은 것은 안전이 확인되었다는 뜻이 아닙니다.";
  document.getElementById("banner").appendChild(el("div", {class: "banner" + (partial || pe ? " warn" : ""), text: msg}));
})();
var nav = document.getElementById("nav"), main = document.getElementById("main");
tabs.forEach(function (t) {
  t.btn = el("button", {onclick: function () { show(t.id); }}, [t.label, t.count !== null && t.count !== undefined ? el("span", {class: "cnt", text: String(t.count)}) : null]);
  nav.appendChild(t.btn);
  panes[t.id] = el("section"); main.appendChild(panes[t.id]);
});
var th = el("button", {id: "theme", title: "밝은 화면 / 어두운 화면 전환", text: "화면 밝기 전환", onclick: function () {
  var r = document.documentElement, dark = r.getAttribute("data-theme") ? r.getAttribute("data-theme") === "dark" : matchMedia("(prefers-color-scheme: dark)").matches;
  r.setAttribute("data-theme", dark ? "light" : "dark");
}});
nav.appendChild(th);
var start = location.hash.slice(1);
show(panes[start] ? start : "files");
})();
</script>
</body>
</html>
"""
