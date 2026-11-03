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
.toolbar input, .toolbar select { font: inherit; padding: 5px 8px; border: 1px solid var(--line); border-radius: 6px;
  background: var(--panel); color: var(--text); }
.toolbar input { min-width: 260px; }
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
#theme { margin-left: auto; border: 0; background: none; color: var(--muted); cursor: pointer; font: inherit; padding: 10px; }
@media (max-width: 700px) { main { padding: 14px 16px 40px; } header { padding: 14px 16px 10px; } .toolbar input { min-width: 0; width: 100%; } }
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
    el("div", {text: "취약점"}), el("div", {}, [sev(r), " ", stat(r), " ", el("b", {text: r.cwe + " " + (r.cwe_name || "")})]),
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
  kv.push(el("div", {text: "기호 실행"}), el("div", {}, [el("b", {text: symText(r)}), s ? el("span", {class: "small muted", text: "  속성 " + s.property + " · " + (s.detail || "") + " · " + s.engine}) : null]));
  kv.push(el("div", {text: "근거"}), el("div", {text: r.evidence || "-"}));
  kv.push(el("div", {text: "변경 전 비교"}), el("div", {text: (b.old_state_label || "-") + (b.old_location ? " (" + b.old_location + ")" : "")
    + (b.old_evidence ? " — " + b.old_evidence : "") + (b.old_symbolic && b.old_symbolic.result ? " · 기호 실행 " + b.old_symbolic.result : "")}));
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
  return new Table({
    rows: rows, search: function (r) { return [r.id, r.cwe, r.func, r.file, r.expr, r.operand, r.origin, r.evidence, r.src, r.verification].concat(r.values || [], r.conditions).join(" "); },
    placeholder: "ID·CWE·함수·파일·연산 검색",
    filters: [
      {name: "level", label: "등급", options: uniq(rows.map(function (r) { return r.level; }), ["High", "Medium", "Low"]), test: function (r, v) { return r.level === v; }},
      {name: "status", label: "상태", options: uniq(rows.map(function (r) { return r.status; }), ["신규 발생", "판정 보류", "기존 존재", "해결"]), test: function (r, v) { return r.status === v; }},
      {name: "cwe", label: "CWE", options: uniq(rows.map(function (r) { return r.cwe; })), test: function (r, v) { return r.cwe === v; }},
      {name: "taint", label: "외부 입력", options: ["도달", "없음"], test: function (r, v) { return !!(r.taint || {}).flow_detected === (v === "도달"); }},
      {name: "sym", label: "기호 실행", options: ["위반 (반례 있음)", "안전", "판정 보류", "미실행"], test: function (r, v) {
        var s = r.symbolic ? ({violated: "위반 (반례 있음)", holds: "안전", unknown: "판정 보류"}[r.symbolic.result]) : "미실행"; return s === v; }},
      {name: "origin", label: "원인 변경", options: uniq(rows.map(function (r) { return r.origin; })), test: function (r, v) { return r.origin === v; }}
    ],
    columns: [
      {label: "ID", render: function (r) { return el("b", {text: r.id}); }, cls: "nowrap"},
      {label: "등급·상태", render: function (r) { return frag([sev(r), el("br"), stat(r)]); }, cls: "nowrap"},
      {label: "CWE", render: function (r) { return frag([el("b", {text: r.cwe}), el("br"), el("span", {class: "small muted", text: r.kind_label})]); }},
      {label: "위치", render: function (r) { return frag([code(r.func), el("br"), loc(r.file, r.line)]); }},
      {label: "위험 연산 (피연산자)", render: function (r) { return frag([code(short(r.expr, 110)), el("br"), el("span", {class: "small muted", text: "피연산자 "}), code(short(r.operand, 70))]); }},
      {label: "외부 입력 (Taint)", render: function (r) { var t = r.taint || {}; return t.flow_detected ? frag([el("b", {text: t.source}), el("br"), el("span", {class: "small", text: t.source_label})]) : el("span", {class: "muted", text: "-"}); }},
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

/* 1. 개요 */
addTab("overview", "개요", null, function (p) {
  var m = D.meta, fc = D.file_counts;
  head(p, "개요", "분석 조건과 결과 규모입니다. 카드를 누르면 해당 항목으로 이동합니다.");
  var cards = [
    ["수정된 파일", fc.modified + fc.added + fc.removed, "files"]
  ];
  if (A) {
    var nkey = A.items.filter(function (i) { return i.key; }).length;
    cards = cards.concat([
      ["변경 지점", A.stats.changes, "changes"], ["변경된 함수", A.groups.length, "groups"],
      ["우선 확인할 영향", nkey, "key"], ["영향 함수", A.stats.affected_functions, "all"],
      ["영향 변수·출력", A.stats.items, "all"]
    ]);
    if (A.notes.length) cards.push(["동작 변화 없음", A.notes.length, "notes"]);
    var nh = A.risks.filter(function (r) { return r.level === "High" && r.status_code !== "FIXED"; }).length;
    var nnew = A.risks.filter(function (r) { return r.status_code === "NEW_REGRESSION"; }).length;
    cards.splice(3, 0, ["보안 취약점 High", nh, "risks", nh ? "alert" : ""], ["신규 발생 취약점", nnew, "risks", nnew ? "alert" : ""],
      ["보안 분석 항목 전체", A.risks.length, "risks"]);
  }
  p.appendChild(el("div", {class: "cards"}, cards.map(function (c) {
    return el("div", {class: "card link " + (c[3] || ""), onclick: function () { if (panes[c[2]]) show(c[2]); }}, [el("div", {class: "v", text: c[1]}), el("div", {class: "k", text: c[0]})]);
  })));
  var inc = "자동 " + D.include.auto + "개" + (D.include.user ? " + 지정 " + D.include.user + "개" : "") + (D.include.cc ? " · compile_commands.json" : "");
  p.appendChild(el("div", {class: "box"}, [el("h3", {text: "분석 조건"}), el("div", {class: "kv"}, [
    el("div", {text: "변경 전"}), el("div", {}, [code(m.old_dir)]),
    el("div", {text: "변경 후"}), el("div", {}, [code(m.new_dir)]),
    el("div", {text: "실행"}), el("div", {text: m.time + " · 소요 " + Number(m.elapsed || 0).toFixed(1) + "초"}),
    el("div", {text: "분석 범위"}), el("div", {text: (m.scope_reason || []).join(", ") + " → 변경 전 " + m.analyzed.old + "개 / 변경 후 " + m.analyzed.new + "개 파일"}),
    el("div", {text: "include 경로"}), el("div", {text: inc})
  ])]));
  if (A && A.groups.length) {
    var byPrio = [0, 0, 0, 0];
    A.items.forEach(function (i) { if (i.key) byPrio[i.prio]++; });
    p.appendChild(el("div", {class: "box"}, [el("h3", {text: "우선 확인할 영향 구성"}),
      frag(byPrio.map(function (n, i) { return n ? frag([tag(i), " " + n + "개   "]) : null; })),
      el("p", {class: "small muted", text: "같은 함수의 같은 대상은 하나로 합쳐 셉니다. 나머지 상태 변수 영향은 '전체 영향 목록'에 있습니다."})]));
  }
  var warns = (m.warnings || []).slice();
  if (A && Object.keys(A.excluded.parse_errors || {}).length) warns.push("파싱하지 못해 분석에서 빠진 파일 " + Object.keys(A.excluded.parse_errors).length + "개 ('분석 범위와 주의' 탭)");
  if (warns.length) p.appendChild(el("div", {class: "box warn"}, [el("h3", {text: "주의"}), el("ul", {}, warns.map(function (w) { return el("li", {text: w}); }))]));
});

/* 2. 파일 변경 */
addTab("files", "파일 변경", D.files.length, function (p) {
  var fc = D.file_counts;
  head(p, "파일 변경 (*.c, *.h)", "추가 " + fc.added + " · 삭제 " + fc.removed + " · 수정 " + fc.modified + " · 동일 " + fc.unchanged + " (줄바꿈 CRLF/LF 차이는 무시)");
  var t = new Table({rows: D.files, search: function (r) { return r.file; }, placeholder: "파일 이름 검색",
    filters: [{name: "st", label: "상태", options: uniq(D.files.map(function (r) { return r.status; })), test: function (r, v) { return r.status === v; }}],
    columns: [{label: "상태", render: function (r) { return r.status; }, cls: "nowrap"}, {label: "파일", render: function (r) { return code(r.file); }}]});
  p.appendChild(t.node);
  var dup = D.meta.duplicate_headers || {};
  if (Object.keys(dup).length) p.appendChild(el("div", {class: "box warn", style: "margin-top:14px"}, [el("h3", {text: "같은 이름의 헤더가 여러 폴더에 있음"}),
    el("p", {class: "small muted", text: "자동 include 순서(루트 → 폴더 이름순)에 따라 다른 헤더가 쓰일 수 있습니다. 필요하면 -I 나 --compile-commands 로 지정하세요."}),
    el("ul", {}, Object.keys(dup).map(function (k) { return el("li", {}, [code(k), ": " + dup[k].join(", ")]); }))]));
});

if (A) {
  var allTable = null;
  /* 3. 변경 함수별 요약 */
  addTab("groups", "변경 함수별 요약", A.groups.length, function (p) {
    head(p, "변경된 함수별 요약", "바뀐 함수마다 변경 내용과 그 변경이 닿는 영향을 묶었습니다. 행을 누르면 변경 위치 전체와 우선 확인할 영향이 펼쳐집니다.");
    var t = new Table({rows: A.groups,
      search: function (g) { return [g.func, g.file].concat(g.changes.map(function (i) { return A.changes[i].old + " " + A.changes[i].new; })).join(" "); },
      placeholder: "함수·파일·코드 검색",
      filters: [
        {name: "file", label: "파일", options: uniq(A.groups.map(function (g) { return g.file; })), test: function (g, v) { return g.file === v; }},
        {name: "kind", label: "변경 종류", options: uniq([].concat.apply([], A.groups.map(function (g) { return Object.keys(g.kinds); }))), test: function (g, v) { return !!g.kinds[v]; }},
        {name: "imp", label: "영향", options: ["우선 확인할 영향 있음", "영향 없음"], test: function (g, v) { return (g.top.length > 0) === (v === "우선 확인할 영향 있음"); }},
        {name: "risk", label: "보안 취약점", options: ["High 있음", "신규 발생 있음", "취약점 있음", "취약점 없음"], test: function (g, v) {
          if (v === "취약점 없음") return !g.risks.length;
          if (v === "취약점 있음") return g.risks.length > 0;
          if (v === "신규 발생 있음") return g.risks.some(function (i) { return A.risks[i].status_code === "NEW_REGRESSION"; });
          return g.risks.some(function (i) { return A.risks[i].level === "High"; }); }}
      ],
      columns: [
        {label: "변경 함수", render: function (g) { return frag([code(g.func), el("br"), loc(g.file)]); }},
        {label: "변경 내용", render: function (g) {
          var k = Object.keys(g.kinds).map(function (x) { return x + " " + g.kinds[x]; }).join(" · ");
          return el("div", {}, [el("div", {class: "small muted", text: k})].concat(g.changes.slice(0, 2).map(function (i) { return el("div", {}, [change(A.changes[i])]); }))
            .concat(g.changes.length > 2 ? [el("div", {class: "small muted", text: "외 " + (g.changes.length - 2) + "곳 (눌러서 보기)"})] : []));
        }},
        {label: "영향 함수", render: function (g) { return String(g.n_funcs); }, cls: "nowrap"},
        {label: "영향 변수·출력", render: function (g) { return String(g.items.length); }, cls: "nowrap"},
        {label: "보안 취약점", render: function (g) {
          if (!g.risks.length) return el("span", {class: "muted", text: "-"});
          var lv = {High: 0, Medium: 0, Low: 0};
          g.risks.forEach(function (i) { lv[A.risks[i].level] = (lv[A.risks[i].level] || 0) + 1; });
          return frag(["High", "Medium", "Low"].map(function (k) { return lv[k] ? frag([el("span", {class: "sev " + (k === "High" ? "c" : k === "Medium" ? "u" : "l"), text: k + " " + lv[k]}), " "]) : null; }));
        }, cls: "nowrap"},
        {label: "우선 확인할 영향", render: function (g) {
          if (!g.top.length) return el("span", {class: "muted", text: "-"});
          return el("div", {}, g.top.map(function (i) { var it = A.items[i]; return el("div", {}, [tag(it.prio), " ", code(it.name)]); }));
        }}
      ],
      detail: function (g) {
        var key = g.items.map(function (i) { return A.items[i]; }).filter(function (it) { return it.key; }).slice(0, 30);
        var d = el("div", {}, [
          el("h4", {text: "변경 위치 " + g.changes.length + "곳"}),
          el("ol", {class: "path"}, g.changes.map(function (i) { var c = A.changes[i];
            return el("li", {}, [loc(c.file, c.line), " ", el("b", {text: c.kind}), " ", change(c), c.detail ? el("span", {class: "small muted", text: "  — " + c.detail}) : null]); })),
          el("h4", {text: "우선 확인할 영향" + (key.length ? " (상위 " + key.length + "개)" : "")}),
          key.length ? el("ul", {}, key.map(function (it) { return el("li", {}, [tag(it.prio), " ", code(it.func), " → ", code(it.name), el("span", {class: "small muted", text: "  " + it.how_kind})]); }))
                     : el("div", {class: "muted", text: "외부 출력·레지스터·공유 변수·반환값에 닿는 영향이 없습니다."})
        ]);
        if (g.risks.length) {
          d.insertBefore(el("ul", {}, g.risks.slice(0, 20).map(function (i) { var r = A.risks[i];
            return el("li", {}, [el("b", {text: r.id + " "}), sev(r), " ", stat(r), " ", el("b", {text: r.cwe + " " + r.kind_label}), " ", code(r.func), " ", loc(r.file, r.line), " ", code(short(r.expr, 90)),
              el("span", {class: "small muted", text: "  " + symText(r)})]); })), d.childNodes[2]);
          d.insertBefore(el("h4", {text: "보안 취약점 " + g.risks.length + "개"}), d.childNodes[2]);
        }
        if (g.items.length) d.appendChild(el("p", {}, [el("button", {class: "lnk", text: "이 변경의 영향 " + g.items.length + "개 모두 보기 →",
          onclick: function () { show("all"); allTable.set("originKey", g.func + "|" + g.file, "원인 변경: " + g.func); }})]));
        return d;
      }});
    p.appendChild(t.node);
  });

  /* 보안 취약점 */
  addTab("risks", "보안 취약점", A.risks.length, function (p) {
    var sec = A.security || {}, eng = sec.engine || {}, tools = sec.tools || {};
    head(p, "변경 기반 보안 취약점 (CPG + Taint + Symbolic Execution)", "변경점에서 CPG(제어·데이터 의존)를 따라 영향이 닿는 위험 연산을 찾고, 규칙의 외부 입력(Source)이 그 피연산자에 도달하는지(Taint), "
      + "안전 속성을 어기는 입력이 있는지(기호 실행)를 확인한 뒤 변경 전 코드와 비교했습니다. 행을 누르면 원인 변경·CPG·Taint·기호 실행·변경 전 비교·검증 방법과 두 근거 경로가 펼쳐집니다.");
    var counts = (sec.summary || {}).by_status || {};
    var lv = (sec.summary || {}).by_level || {};
    p.appendChild(el("div", {class: "cards"}, [
      el("div", {class: "card" + (lv.High ? " alert" : "")}, [el("div", {class: "v", text: lv.High || 0}), el("div", {class: "k", text: "High"})]),
      el("div", {class: "card"}, [el("div", {class: "v", text: lv.Medium || 0}), el("div", {class: "k", text: "Medium"})]),
      el("div", {class: "card"}, [el("div", {class: "v", text: lv.Low || 0}), el("div", {class: "k", text: "Low"})])
    ].concat(Object.keys(counts).map(function (k) {
      return el("div", {class: "card" + (k === "신규 발생" && counts[k] ? " alert" : "")}, [el("div", {class: "v", text: counts[k]}), el("div", {class: "k", text: k})]);
    }))));
    var tl = [];
    if (tools.cpg) tl.push("CPG JSON: security/cpg.json (함수 " + tools.cpg.methods + "개, 노드 " + tools.cpg.nodes + "개, 간선 " + tools.cpg.edges + "개)");
    if (tools.joern) tl.push("Joern: " + (tools.joern.ran ? "실행함 (경로 " + tools.joern.flows.length + "개)" : (tools.joern.note || "")) + " — security/joern/taint.sc");
    if (tools.codeql) tl.push("CodeQL: " + (tools.codeql.ran ? "실행함 (결과 " + tools.codeql.results.length + "개)" : (tools.codeql.note || "")) + " — security/codeql/ChangeTaint.ql");
    if (tools.klee) tl.push("KLEE: 하네스 " + tools.klee.harnesses + "개 생성, 실행 " + tools.klee.ran + "개 — security/klee/");
    p.appendChild(el("div", {class: "box small"}, [el("div", {}, [el("b", {text: "엔진  "}), "CPG " + (eng.cpg || "-") + " · Taint " + (eng.taint || "-") + " · 기호 실행 " + (eng.symbolic || "-") + " · 규칙 " + (eng.rules || "-")]),
      tl.length ? el("ul", {}, tl.map(function (x) { return el("li", {text: x}); })) : null,
      el("div", {class: "muted", text: "상태: 신규 발생 = 변경 후 위반 확인 & 변경 전 안전·없음 · 기존 존재 = 변경 전에도 위반 · 해결 = 변경 전 위반 & 변경 후 안전 · 판정 보류 = 위반을 증명하지 못한 후보. 등급: 위반 확인 + (외부 입력 도달 또는 메모리 쓰기) = High."})]));
    if (!A.risks.length) { p.appendChild(el("p", {class: "muted", text: "변경으로 영향받는 위험 연산·보안 검사 조건이 없습니다."})); return; }
    p.appendChild(riskTable(A.risks).node);
  });

  /* 4. 우선 확인할 영향 */
  var keyRows = A.items.filter(function (i) { return i.key; });
  addTab("key", "우선 확인할 영향", keyRows.length, function (p) {
    head(p, "우선 확인할 영향", "외부 출력 → 레지스터 → 여러 진입점 공유 변수 → 반환값 순입니다. 같은 구분 안에서는 직접 바뀐 것 → 전달 단계가 짧은 것 → 메모리 복사·대기 같은 공용 호출 순이고, 같은 함수의 같은 대상은 하나로 합쳤습니다. 행을 누르면 조건과 전달 경로가 펼쳐집니다.");
    p.appendChild(itemTable(keyRows).node);
  });

  /* 5. 전체 영향 목록 */
  addTab("all", "전체 영향 목록", A.items.length, function (p) {
    head(p, "전체 영향 목록", "상태 변수를 포함한 모든 영향 후보입니다. 행을 누르면 조건과 전달 경로가 펼쳐집니다.");
    allTable = itemTable(A.items, [{name: "origin", label: "원인 변경", options: uniq(A.items.map(function (r) { return r.origin; })), test: function (r, v) { return r.origin === v; }}]);
    p.appendChild(allTable.node);
  });

  /* 6. 변경 위치 */
  addTab("changes", "변경 위치", A.changes.length, function (p) {
    head(p, "변경 위치", "전처리 후 문장 단위로 비교한 변경 지점입니다. 같은 줄의 호출 인자 단위 변경은 하나로 합쳤습니다.");
    p.appendChild(new Table({rows: A.changes, search: function (c) { return [c.file, c.func, c.old, c.new, c.detail, c.src].join(" "); },
      filters: [
        {name: "kind", label: "종류", options: uniq(A.changes.map(function (c) { return c.kind; })), test: function (c, v) { return c.kind === v; }},
        {name: "file", label: "파일", options: uniq(A.changes.map(function (c) { return c.file; })), test: function (c, v) { return c.file === v; }}
      ],
      columns: [
        {label: "위치", render: function (c) { return loc(c.file, c.line); }},
        {label: "함수", render: function (c) { return code(c.func); }},
        {label: "종류", render: function (c) { return c.kind; }, cls: "nowrap"},
        {label: "변경 전 → 변경 후", render: change},
        {label: "비고", render: function (c) { return el("span", {class: "small", text: c.detail || ""}); }}
      ],
      detail: function (c) { return el("div", {class: "kv"}, [el("div", {text: "소스"}), el("div", {}, [code(c.src || "")]),
        el("div", {text: "변경 전"}), el("div", {}, [code(c.old || "(없음)")]), el("div", {text: "변경 후"}), el("div", {}, [code(c.new || "(없음)")]),
        el("div", {text: "비고"}), el("div", {text: c.detail || "-"})]); }
    }).node);
  });

  /* 7. 동작 변화 없음 */
  if (A.notes.length) addTab("notes", "동작 변화 없음", A.notes.length, function (p) {
    head(p, "동작 변화 없음으로 본 변경", "바뀌었지만 동작에는 영향이 없다고 보고 영향 분석 시작점에서 뺀 자료형·초기값 변경입니다.");
    p.appendChild(new Table({rows: A.notes, search: function (n) { return n; }, columns: [{label: "내용", render: function (n) { return n; }}]}).node);
  });
}

/* 8. 의존성 변화 */
if (D.deps) addTab("deps", "의존성 변화", D.deps.functions.length, function (p) {
  head(p, "함수·변수 의존성 변화 (읽기·쓰기·호출)", "변경 전후 버전 각각의 함수별 읽기·쓰기 변수와 호출 대상을 비교했습니다. 전체 표는 deps/deps_new.md, deps/deps_old.md 에 있습니다.");
  function pm(r, k) {
    if (r.status !== "변경") return list(r[k]);
    var a = r[k + "_added"], m = r[k + "_removed"];
    if (!a.length && !m.length) return el("span", {class: "muted", text: "-"});
    return el("div", {}, [a.length ? el("div", {class: "new"}, ["+ ", list(a)]) : null, m.length ? el("div", {class: "old"}, ["− ", list(m)]) : null]);
  }
  function list(xs) { return xs.length ? frag(xs.map(function (x, i) { return frag([i ? ", " : "", code(x)]); })) : el("span", {class: "muted", text: "-"}); }
  if (D.deps.functions.length) p.appendChild(new Table({rows: D.deps.functions,
    search: function (r) { return [r.func].concat(r.reads || [], r.writes || [], r.calls || [], r.reads_added || [], r.reads_removed || [], r.writes_added || [], r.writes_removed || [], r.calls_added || [], r.calls_removed || []).join(" "); },
    filters: [{name: "st", label: "상태", options: uniq(D.deps.functions.map(function (r) { return r.status; })), test: function (r, v) { return r.status === v; }}],
    columns: [
      {label: "함수", render: function (r) { return code(r.func); }}, {label: "상태", render: function (r) { return r.status; }, cls: "nowrap"},
      {label: "읽기 +/−", render: function (r) { return pm(r, "reads"); }}, {label: "쓰기 +/−", render: function (r) { return pm(r, "writes"); }},
      {label: "호출 +/−", render: function (r) { return pm(r, "calls"); }}
    ]}).node);
  else p.appendChild(el("p", {class: "muted", text: "함수별 읽기·쓰기·호출 관계는 바뀌지 않았습니다 (값이나 조건만 바뀐 변경)."}));
  if (D.deps.vars_added.length || D.deps.vars_removed.length) p.appendChild(el("div", {class: "box", style: "margin-top:14px"}, [
    el("h3", {text: "상태 변수"}), el("div", {class: "kv"}, [el("div", {text: "새 상태 변수"}), el("div", {}, [list(D.deps.vars_added)]),
      el("div", {text: "사라진 상태 변수"}), el("div", {}, [list(D.deps.vars_removed)])])]));
});

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
show(panes[start] ? start : "overview");
})();
</script>
</body>
</html>
"""
