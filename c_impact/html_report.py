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
#theme { margin-left: auto; border: 0; background: none; color: var(--muted); cursor: pointer; font: inherit; padding: 10px; }
@media (max-width: 700px) { main { padding: 14px 16px 40px; } header { padding: 14px 16px 10px; } .toolbar input[type=search] { min-width: 0; width: 100%; } }
</style>
</head>
<body>
<header>
  <h1>변경 영향 분석 보고서</h1>
  <div class="paths" id="paths"></div>
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

/* ---------- 변경 검토 (기본 화면) ---------- */
var VL = {"기호 실행 반례 발견": "t0", "정적 규칙 위반": "t1", "Taint 경로 발견": "t2", "검증 불충분": "t4"};
function vlev(r) { return el("span", {class: "tag " + (VL[r.verification_level] || "t4"), title: "검증 수준", text: r.verification_level || "검증 불충분"}); }
var MODES = [
  ["new", "신규 위험", "변경으로 새로 생긴 위험 (변경 후 안전 속성 위반 증명, 변경 전 안전·없음)"],
  ["priority", "우선 검토", "신규 위험 + 변경 함수 안에서 외부 입력이 닿거나 보안 조건이 바뀐 판정 보류 후보"],
  ["all", "전체 변경점", "변경된 모든 파일·함수·변경점과 의존성 변화"],
  ["pending", "판정 보류", "위반을 증명하지 못한 후보 (검증 불충분·Taint 경로만 발견)"]
];

function model() {
  var files = {}, order = [];
  function file(name) {
    if (!files[name]) { files[name] = {file: name, status: "", funcs: {}, forder: []}; order.push(name); }
    return files[name];
  }
  function fn(fname, func) {
    var f = file(fname);
    if (!f.funcs[func]) { f.funcs[func] = {func: func, file: fname, changes: [], direct: [], prop: [], deps: null, lo: 1e9, hi: 0}; f.forder.push(func); }
    return f.funcs[func];
  }
  (D.files || []).forEach(function (x) { file(x.file).status = x.status; });
  if (A) {
    A.changes.forEach(function (c) { var b = fn(c.file, c.func); b.changes.push(c); b.lo = Math.min(b.lo, c.line); b.hi = Math.max(b.hi, c.line); });
    A.risks.forEach(function (r) {
      if (r.relation === "전파") { fn(r.origin_file, r.origin).prop.push(r); }
      else { var f = files[r.file] && files[r.file].funcs[r.func] ? files[r.file].funcs[r.func] : fn(r.origin_file, r.origin); f.direct.push(r); }
    });
  }
  var deps = {};
  ((D.deps || {}).functions || []).forEach(function (row) { deps[row.func] = row; });
  order.forEach(function (n) { files[n].forder.forEach(function (k) { var b = files[n].funcs[k]; if (deps[k]) { b.deps = deps[k]; deps[k].used = true; } }); });
  var restDeps = Object.keys(deps).filter(function (k) { return !deps[k].used; }).map(function (k) { return deps[k]; });
  order.sort(function (x, y) {
    var cx = /\.c$/i.test(x) ? 0 : 1, cy = /\.c$/i.test(y) ? 0 : 1;
    return cx - cy || (x < y ? -1 : x > y ? 1 : 0);
  });
  return {files: files, order: order, restDeps: restDeps};
}

function riskVisible(r, st) {
  var old = r.status_code === "EXISTING" || r.status_code === "FIXED";
  if (old) return st.showOld;
  if (st.mode === "new") return r.status_code === "NEW_REGRESSION";
  if (st.mode === "pending") return r.status_code === "PENDING";
  if (st.mode === "priority") return r.status_code === "NEW_REGRESSION" ||
    (r.status_code === "PENDING" && r.relation === "직접" && ((r.taint || {}).flow_detected || r.category === "보안 조건 변경" || r.level === "High"));
  return true;
}

function riskCard(r) {
  var ch = r.change, bd = r.bounds || {}, t = r.taint || {};
  var head = el("div", {class: "rh"}, [sev(r), " ", stat(r), " ", vlev(r), " ", el("b", {text: r.kind_label}), " ",
    code(short(r.expr, 100)), " ", loc(r.file, r.line), r.relation === "전파" ? el("span", {class: "small muted", text: "  ← " + r.origin + "에서 전파"}) : null]);
  var facts = [];
  if (ch) facts.push(frag([el("span", {class: "muted", text: "원인 변경 "}), el("span", {class: "code old", text: short(ch.before || "(없음)", 60)}), " → ", el("span", {class: "code new", text: short(ch.after || "(삭제)", 60)})]));
  if (bd.change) facts.push(frag([el("span", {class: "muted", text: "범위 "}), bd.change]));
  if (t.flow_detected) facts.push(frag([el("span", {class: "muted", text: "외부 입력 "}), t.source + " (" + (t.change || "") + ")"]));
  if (r.symbolic && r.symbolic.result === "violated") facts.push(frag([el("span", {class: "muted", text: "반례 "}), symText(r)]));
  else if (r.evidence) facts.push(frag([el("span", {class: "muted", text: "근거 "}), short(r.evidence, 140)]));
  var det = el("details", {class: "more-d"}, [el("summary", {text: "상세 (조건·근거 경로·검증 방법)"}), riskDetail(r)]);
  return el("div", {class: "rcard " + (r.status_code === "NEW_REGRESSION" ? "new" : "")}, [head,
    el("div", {class: "rf small"}, facts.map(function (x) { return el("div", {}, [x]); })), det]);
}

function depBlock(row) {
  function part(label, add, del) {
    if (!(add || []).length && !(del || []).length) return null;
    return el("div", {}, [el("span", {class: "muted", text: label + " "}),
      (add || []).length ? el("span", {class: "new", text: "+ " + add.join(", ") + "  "}) : null,
      (del || []).length ? el("span", {class: "old", text: "− " + del.join(", ")}) : null]);
  }
  if (row.status !== "변경") {
    return el("div", {class: "small"}, [el("b", {text: "함수 " + row.status + " "}),
      part("호출", row.status === "추가" ? row.calls : [], row.status === "삭제" ? row.calls : []),
      part("읽기", row.status === "추가" ? row.reads : [], row.status === "삭제" ? row.reads : []),
      part("쓰기", row.status === "추가" ? row.writes : [], row.status === "삭제" ? row.writes : [])]);
  }
  return el("div", {class: "small"}, [part("호출", row.calls_added, row.calls_removed), part("읽기", row.reads_added, row.reads_removed),
    part("쓰기", row.writes_added, row.writes_removed)]);
}

function changeRows(list) {
  return el("table", {class: "ct"}, [el("tbody", {}, list.map(function (c) {
    return el("tr", {}, [el("td", {class: "nowrap"}, [loc("", c.line)]), el("td", {class: "nowrap small"}, [c.kind]),
      el("td", {}, [change(c), c.detail ? el("div", {class: "small muted", text: c.detail}) : null])]);
  }))]);
}

addTab("review", "변경 검토", null, function (p) {
  var M = model(), st = {mode: "new", showOld: false, q: ""};
  var all = A ? A.risks : [];
  function cnt(f) { return all.filter(f).length; }
  var nNew = cnt(function (r) { return r.status_code === "NEW_REGRESSION"; });
  var nPend = cnt(function (r) { return r.status_code === "PENDING"; });
  var nOld = cnt(function (r) { return r.status_code === "EXISTING" || r.status_code === "FIXED"; });
  var nFn = 0; M.order.forEach(function (n) { nFn += M.files[n].forder.length; });
  if (!nNew) st.mode = cnt(function (r) { return riskVisible(r, {mode: "priority"}); }) ? "priority" : "all";
  head(p, "변경 검토", "변경된 .c 파일별로 바뀐 함수와 변경점을 보여 주고, 그 함수와 직접 연결된 보안 위험을 먼저 표시합니다. 다른 함수로 전파된 위험은 의존 경로가 분명할 때만 따로 표시하고, "
    + "함수 호출·변수 읽기쓰기의 추가·삭제는 '의존성 변화'(영향 분석)로 구분해 취약점과 섞지 않습니다. 이미 있던 위험은 기본 화면에서 뺐습니다.");
  p.appendChild(el("div", {class: "cards"}, [
    ["변경 파일", D.files.length], ["변경 함수", nFn], ["신규 위험", nNew, nNew ? "alert" : ""],
    ["우선 검토", cnt(function (r) { return riskVisible(r, {mode: "priority"}); })], ["판정 보류", nPend], ["기존·해결 (숨김)", nOld]
  ].map(function (c) { return el("div", {class: "card " + (c[2] || "")}, [el("div", {class: "v", text: c[1]}), el("div", {class: "k", text: c[0]})]); })));
  p.appendChild(el("div", {class: "box small"}, [el("b", {text: "검증 수준  "}),
    frag(["기호 실행 반례 발견", "정적 규칙 위반", "Taint 경로 발견", "검증 불충분"].map(function (k) { return frag([vlev({verification_level: k}), " "]); })),
    el("span", {class: "muted", text: " — 반례: z3가 위반 입력을 찾음 · 정적 규칙: 상수·바뀐 조건 구간으로 위반 증명 · Taint: 외부 입력이 위험 피연산자에 닿음(위반 미증명) · 불충분: 근거 부족"}),
    el("div", {class: "muted", text: "엔진: " + ((A && A.security && A.security.engine) ? ("CPG " + A.security.engine.cpg + " · Taint " + A.security.engine.taint + " · 기호 실행 " + A.security.engine.symbolic) : "-")})]));
  var bar = el("div", {class: "seg"});
  var btns = MODES.map(function (m) {
    var b = el("button", {title: m[2], text: m[1], onclick: function () { st.mode = m[0]; draw(); }});
    bar.appendChild(b); return [m[0], b];
  });
  var chk = el("input", {type: "checkbox", id: "showold"});
  chk.addEventListener("change", function () { st.showOld = chk.checked; draw(); });
  var q = el("input", {type: "search", placeholder: "파일·함수 검색"});
  q.addEventListener("input", function () { st.q = q.value.toLowerCase(); draw(); });
  p.appendChild(el("div", {class: "toolbar"}, [bar, el("label", {class: "small"}, [chk, " 기존·해결 위험도 표시"]), q]));
  var desc = el("p", {class: "small muted"});
  var body = el("div");
  p.appendChild(desc); p.appendChild(body);

  function draw() {
    btns.forEach(function (x) { x[1].classList.toggle("on", x[0] === st.mode); });
    desc.textContent = MODES.filter(function (m) { return m[0] === st.mode; })[0][2];
    body.textContent = "";
    var shown = 0;
    M.order.forEach(function (fname) {
      var F = M.files[fname];
      var blocks = [];
      F.forder.forEach(function (k) {
        var b = F.funcs[k];
        if (st.q && (fname + " " + k).toLowerCase().indexOf(st.q) < 0) return;
        var dv = b.direct.filter(function (r) { return riskVisible(r, st); });
        var pv = b.prop.filter(function (r) { return riskVisible(r, st); });
        if (st.mode !== "all" && !dv.length && !pv.length) return;
        var badges = [];
        var nn = b.direct.concat(b.prop).filter(function (r) { return r.status_code === "NEW_REGRESSION"; }).length;
        var np = b.direct.concat(b.prop).filter(function (r) { return r.status_code === "PENDING"; }).length;
        if (nn) badges.push(el("span", {class: "sev c", text: "신규 위험 " + nn}));
        if (np) badges.push(el("span", {class: "sev u", text: "판정 보류 " + np}));
        if (b.deps) badges.push(el("span", {class: "tag t3", text: "의존성 변화"}));
        var range = b.changes.length ? (b.lo === b.hi ? "라인 " + b.lo : "라인 " + b.lo + "–" + b.hi) : "";
        var kids = [el("summary", {}, [el("b", {}, [code(k)]), " ", el("span", {class: "small muted", text: range + (b.changes.length ? " · 변경점 " + b.changes.length + "곳" : "")}), " "].concat(badges.map(function (x) { return frag([x, " "]); })))];
        if (b.changes.length) {
          kids.push(el("h4", {text: "변경점"}));
          kids.push(changeRows(b.changes.slice(0, 8)));
          if (b.changes.length > 8) kids.push(el("details", {class: "more-d"}, [el("summary", {text: "나머지 변경점 " + (b.changes.length - 8) + "곳"}), changeRows(b.changes.slice(8))]));
        }
        if (dv.length) { kids.push(el("h4", {text: "이 함수의 보안 위험 · 후보 (직접 연결)"})); dv.forEach(function (r) { kids.push(riskCard(r)); }); }
        if (pv.length) { kids.push(el("h4", {text: "다른 함수로 전파된 위험 (의존 경로 확보)"})); pv.forEach(function (r) { kids.push(riskCard(r)); }); }
        if (b.deps && (st.mode === "all" || dv.length || pv.length)) {
          kids.push(el("h4", {text: "의존성 변화 (영향 분석 — 취약점 아님)"}));
          kids.push(depBlock(b.deps));
        }
        var d = el("details", {class: "fn"}, kids);
        if (st.mode !== "all" || dv.length || pv.length) d.open = true;
        blocks.push(d);
      });
      if (!blocks.length && !(st.mode === "all" && !st.q)) return;
      shown++;
      var nRisk = 0;
      F.forder.forEach(function (k) { nRisk += F.funcs[k].direct.concat(F.funcs[k].prop).filter(function (r) { return riskVisible(r, st); }).length; });
      body.appendChild(el("section", {class: "fileblk on"}, [
        el("div", {class: "fh"}, [el("b", {}, [code(fname)]), " ", el("span", {class: "small muted", text: (F.status || "") + " · 변경 함수 " + F.forder.length + "개" + (nRisk ? " · 표시된 위험 " + nRisk + "개" : "")})]),
        blocks.length ? frag(blocks) : el("div", {class: "small muted", text: "함수 단위 변경 없음 (주석·공백·선언만 바뀌었거나, 이 파일을 쓰는 .c 쪽에 변경점이 표시됨)"})
      ]));
    });
    if (st.mode === "all" && M.restDeps.length) {
      body.appendChild(el("section", {class: "fileblk on"}, [el("div", {class: "fh"}, [el("b", {text: "변경점 없이 의존성만 바뀐 함수"})]),
        frag(M.restDeps.map(function (row) { return el("details", {class: "fn"}, [el("summary", {}, [code(row.func), " ", el("span", {class: "tag t3", text: "의존성 변화"})]), depBlock(row)]); }))]));
    }
    if (st.mode === "all" && A && A.notes.length) {
      body.appendChild(el("details", {class: "fn"}, [el("summary", {text: "동작 변화 없음으로 본 변경 " + A.notes.length + "개"}),
        el("ul", {class: "small"}, A.notes.map(function (n) { return el("li", {text: n}); }))]));
    }
    if (!shown) body.appendChild(el("p", {class: "muted", text: st.mode === "new" ? "변경으로 새로 생긴 위험이 없습니다. '우선 검토'나 '판정 보류'에서 후보를 확인하세요." : "표시할 항목이 없습니다."}));
  }
  draw();
});

if (A) {
  var allTable = null;
  var keyRows = A.items.filter(function (i) { return i.key; });
  addTab("key", "우선 확인할 영향", keyRows.length, function (p) {
    head(p, "우선 확인할 영향 (영향 분석)", "변경이 닿는 외부 출력 → 레지스터 → 여러 진입점 공유 변수 → 반환값입니다. 보안 위험이 아니라 동작이 달라질 수 있는 곳입니다. 행을 누르면 조건과 전달 경로가 펼쳐집니다.");
    p.appendChild(itemTable(keyRows).node);
  });
  addTab("all", "전체 영향 목록", A.items.length, function (p) {
    head(p, "전체 영향 목록 (영향 분석)", "상태 변수를 포함한 모든 영향 후보입니다. 행을 누르면 조건과 전달 경로가 펼쳐집니다.");
    allTable = itemTable(A.items, [{name: "origin", label: "원인 변경", options: uniq(A.items.map(function (r) { return r.origin; })), test: function (r, v) { return r.origin === v; }}]);
    p.appendChild(allTable.node);
  });
}

/* 9. 진입점·공유 변수 */
if (A && (A.entries.length || A.shared.length)) addTab("entries", "진입점·공유 변수", A.shared.length || A.entries.length, function (p) {
  head(p, "진입점과 여러 진입점이 공유하는 영향 변수", "호출자가 없는 함수와 --entry 로 지정한 함수를 진입점으로 봅니다. 여러 진입점(태스크·ISR)이 접근하는 변수는 실행 순서·선점에 따라 결과가 달라질 수 있습니다.");
  if (A.shared.length) {
    p.appendChild(el("h3", {text: "여러 진입점이 공유하는 영향 변수"}));
    p.appendChild(new Table({rows: A.shared, search: function (s) { return [s.var].concat(s.entries, s.functions).join(" "); },
      filters: [{name: "isr", label: "ISR 포함", options: ["예", "아니오"], test: function (s, v) { return !!s.isr === (v === "예"); }}],
      columns: [{label: "변수", render: function (s) { return code(s.var); }}, {label: "접근 진입점", render: function (s) { return s.entries.join(", "); }},
        {label: "ISR 포함", render: function (s) { return s.isr ? "예" : "-"; }}, {label: "접근 함수", render: function (s) { return el("span", {class: "small", text: s.functions.join(", ")}); }}]}).node);
  }
  if (A.entries.length) {
    p.appendChild(el("h3", {text: "진입점별 영향 함수"}));
    p.appendChild(new Table({rows: A.entries, search: function (e) { return [e.entry].concat(e.affected_functions).join(" "); },
      columns: [{label: "진입점", render: function (e) { return code(e.entry); }},
        {label: "구분", render: function (e) { return (e.isr ? "ISR 추정" : "진입점") + (e.user ? " (지정)" : ""); }, cls: "nowrap"},
        {label: "영향 함수", render: function (e) { return el("span", {class: "small", text: e.affected_functions.join(", ")}); }}]}).node);
  }
});

/* 10. 분석 범위와 주의 */
addTab("scope", "분석 범위와 주의", null, function (p) {
  head(p, "분석 범위와 주의", "결과는 정적 분석상 영향 후보입니다. 동작 차이가 의도된 변경인지 결함인지는 별도로 판단해야 합니다.");
  var m = D.meta;
  if ((m.warnings || []).length) p.appendChild(el("div", {class: "box warn"}, [el("h3", {text: "경고"}), el("ul", {}, m.warnings.map(function (w) { return el("li", {text: w}); }))]));
  if (D.explore || D.sil) p.appendChild(el("div", {class: "box"}, [el("h3", {text: "실행 확인"}), el("ul", {}, [
    D.explore ? el("li", {text: "자동 탐색: 케이스 " + D.explore.cases + "개 중 차이 " + D.explore.diff_cases + "개 — explore/explore_report.md"}) : null,
    D.sil ? el("li", {text: "지정 시나리오: 케이스 " + D.sil.cases + "개 중 차이 " + D.sil.diff_cases + "개 — sil/sil_report.md"}) : null])]));
  if (A) {
    var ex = A.excluded, pe = Object.keys(ex.parse_errors || {});
    p.appendChild(el("div", {class: "box" + (pe.length ? " warn" : "")}, [el("h3", {text: "파싱 실패 (분석 제외) " + pe.length + "개"}),
      pe.length ? el("ul", {}, pe.map(function (f) { return el("li", {}, [code(f), " ", el("span", {class: "small muted", text: String(ex.parse_errors[f] || "").split("\n")[0]})]); }))
                : el("div", {class: "muted", text: "없음 (변경 전 " + m.analyzed.old + "개 / 변경 후 " + m.analyzed.new + "개 파일 모두 분석)"})]));
    function sec(title, desc, xs, fmt) {
      if (!xs || !xs.length) return;
      p.appendChild(el("div", {class: "box"}, [el("h3", {text: title + " " + xs.length + "개"}), desc ? el("p", {class: "small muted", text: desc}) : null,
        el("div", {class: "small"}, [frag(xs.map(function (x, i) { return frag([i ? ", " : "", fmt(x)]); }))])]));
    }
    sec("본문 없는 외부 함수", "포인터 인자가 가리키는 메모리를 읽고 쓰는 것으로 보수적으로 가정했습니다 (const 포인터 인자는 읽기만). --extern-model 로 효과를 지정할 수 있습니다.",
        ex.extern_functions, function (e) { return frag([code(e.name), e.modeled ? " (모델)" : ""]); });
    sec("대상을 풀지 못한 함수 포인터 호출", "반환값이 모든 인자에 의존한다고 보았고, 피호출 함수 내부 영향은 빠져 있습니다.",
        ex.unresolved_calls, function (u) { return frag([code(u.func), ":" + u.line + " ", code(u.call + "(...)")]); });
    sec("대상을 모르는 포인터", "호출자가 분석 범위 밖인 인자 등. 실제로 전역을 가리키면 영향이 누락될 수 있습니다.", ex.unresolved_pointers, code);
    sec("상수 주소(하드웨어 레지스터) 접근", "읽기·쓰기 효과만 반영하며 하드웨어 동작은 모델링하지 않습니다.", ex.hw_access, code);
    if ((ex.warnings || []).length) p.appendChild(el("div", {class: "box"}, [el("h3", {text: "분석 경고 " + ex.warnings.length + "개"}), el("ul", {class: "small"}, ex.warnings.map(function (w) { return el("li", {text: w}); }))]));
    if ((ex.assumptions || []).length) p.appendChild(el("div", {class: "box"}, [el("h3", {text: "분석 가정"}), el("ul", {class: "small"}, ex.assumptions.map(function (w) { return el("li", {text: w}); }))]));
  }
  if ((m.outputs || []).length) p.appendChild(el("div", {class: "box"}, [el("h3", {text: "결과 파일"}), el("ul", {class: "small"}, m.outputs.map(function (o) { return el("li", {}, [code(o)]); }))]));
});

/* ---------- 화면 구성 ---------- */
document.getElementById("paths").appendChild(frag([
  "변경 전 ", code(D.meta.old_dir), "  →  변경 후 ", code(D.meta.new_dir), "   ·   " + D.meta.time]));
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
show(panes[start] ? start : "review");
})();
</script>
</body>
</html>
"""
