"""검증 결과 보고서 생성 (report.json → report.md / report.html / test_cases.csv).

보고서는 report.json 하나에서 재생성 가능하다 (--report-only).
각 검출 결과에는 요구사항 1~10번 항목을 모두 싣는다:
 1 요구사항 ID·원문  2 코드 위치  3 유형·심각도  4 사양 vs 코드  5 발생 조건·실행 경로
 6 예상 동작 vs 추론 동작  7 판단 근거·불확실성  8 권장 수정  9 추가 확인  10 테스트 케이스
"""

from __future__ import annotations

import csv
import html
import json
from collections import Counter
from pathlib import Path

from .evidence import VERDICT_KO

CATEGORY_KO = {
    "MISSING_IMPLEMENTATION": "구현 누락(의심)", "CONDITION_MISMATCH": "조건식 불일치",
    "THRESHOLD_BOUNDARY_MISMATCH": "임계값·경계 불일치", "TIMER_COUNTER_MISMATCH": "타이머·카운터 불일치",
    "STATE_TRANSITION_MISMATCH": "상태 전이 불일치", "PRIORITY_MISMATCH": "우선순위 불일치",
    "EXCEPTION_HANDLING_MISMATCH": "예외 처리 불일치", "INIT_RECOVERY_MISMATCH": "초기화·복구 불일치",
    "SPEC_GAP": "사양 공백", "AMBIGUOUS": "사양 모호", "RUNTIME_ERROR": "런타임 오류",
    "LOGIC_ERROR": "기능·논리 오류", "CONCURRENCY": "동시성",
}
STAGE_KO = {"3_consistency": "정합성", "4_gaps": "사양 공백", "5_defects": "코드 결함"}
SEV_ORDER = {"HIGH": 0, "MEDIUM": 1, "LOW": 2, "INFO": 3}
VERDICT_RANK = {"CONFIRMED_DEFECT": 0, "PROBABLE_DEFECT": 1, "POTENTIAL_RISK": 2, "NEEDS_REVIEW": 3,
                "SPEC_CLARIFICATION": 4, "CONSISTENT": 5}
TRACE_KO = {"TRACED": "추적됨", "PARTIAL": "부분 추적", "TRACE_NOT_FOUND": "TRACE_NOT_FOUND"}
CONS_KO = {"CONSISTENT": "일치", "INCONSISTENT": "불일치", "PARTIALLY_CONSISTENT": "부분 일치",
           "CANNOT_DETERMINE": "판단 불가"}
STATUS_KO = {"DEFINED": "정의됨", "AMBIGUOUS": "AMBIGUOUS", "UNDEFINED": "UNDEFINED"}
BASIS_KO = {"FACT": "사실", "INFERENCE": "추론", "HYPOTHESIS": "가설"}
ASSESS_KO = {"CONFIRMED": "확정", "PROBABLE": "가능성 높음", "POTENTIAL": "잠재", "LIKELY_FALSE_POSITIVE": "오탐 추정",
             "NEEDS_INFO": "정보 필요"}


def sorted_findings(findings: list) -> list:
    return sorted(findings, key=lambda f: (VERDICT_RANK.get(f.get("verdict"), 9) >= 4,
                                           SEV_ORDER.get(f.get("severity"), 9),
                                           VERDICT_RANK.get(f.get("verdict"), 9), f.get("id", "")))


def all_test_cases(result: dict) -> list[dict]:
    rows = []
    for f in sorted_findings(result.get("findings", [])):
        for i, tc in enumerate(f.get("test_cases") or [], 1):
            rows.append({"tc_id": f"TC-{f['id']}-{i}", "finding": f["id"], "req_ids": ", ".join(f.get("req_ids", [])),
                         **{k: tc.get(k, "") for k in ("title", "technique", "preconditions", "steps", "expected",
                                                        "observation")}})
    return rows


# ---------------------------------------------------------------------------
# Markdown
# ---------------------------------------------------------------------------


def _fn(name) -> str:
    return "" if not name or name in ("-", "전역", "global") else f"{name}()"


def _md(s) -> str:
    return str(s if s is not None else "").replace("|", "\\|").replace("\n", "<br>")


def _ref_md(r: dict) -> str:
    chk = r.get("check", {})
    mark = "✔" if chk.get("verified") else "✖"
    note = "" if chk.get("note") in (None, "확인됨") else f" ({chk.get('note')})"
    sym = f" `{r['symbol']}`" if r.get("symbol") else ""
    role = f" — {r['role']}" if r.get("role") else ""
    return f"{mark} `{r.get('file')}:{r.get('line')}` {_fn(r.get('function'))}{sym}{role}{note}"


def to_markdown(result: dict) -> str:
    meta, out = result["meta"], []
    static_only = not result.get("requirements")
    out.append(f"# SW 검증 리뷰 보고서 — {meta.get('spec') or '정적 분석'}\n")
    out.append("> 자연어 기능사양서와 Embedded C 코드 간 의미적 정합성 검증 및 사양 공백·잠재 결함 분석 결과입니다. "
               "모든 판정은 검증자의 최종 확인이 필요합니다.\n")
    out.append("## 0. 분석 개요\n")
    out.append("| 항목 | 내용 |\n|---|---|")
    for k, v in [("사양서", meta.get("spec") or "-"), ("소스", ", ".join(meta.get("sources", []))),
                 ("실행 환경 정보", result["environment"]["source"]), ("LLM", meta.get("llm")),
                 ("수행 단계", ", ".join(meta.get("stages", []))), ("전처리기", result["code"].get("preprocessor")),
                 ("외부 도구", "; ".join(result["code"].get("tool_notes", [])) or "-"), ("생성 시각", meta.get("generated_at"))]:
        out.append(f"| {k} | {_md(v)} |")
    out.append("\n### 분석 전제 및 제한\n")
    for a in result["environment"]["assumptions"]:
        out.append(f"- {a}")
    for i in result["code"].get("issues", []):
        out.append(f"- [코드 분석 제한] {i['file']}: {i['message']}")
    g = sum(1 for f in result.get("findings", []) if f.get("guardrail_notes"))
    if g:
        out.append(f"- 근거 검증·판정 가드레일로 조정된 검출 결과: {g}건 (각 항목의 '가드레일 조정' 참조)")

    out.append("\n## 1. 요약\n")
    findings = sorted_findings(result.get("findings", []))
    if static_only:
        cc = Counter(c["certainty"] for c in result.get("candidates", []))
        out.append("| 확실성 | 건수 | 의미 |\n|---|---|---|")
        for k, m in [("CERTAIN", "자료형·상수만으로 성립"), ("PROBABLE", "구조적 근거가 강함"),
                     ("POSSIBLE", "런타임 조건에 따라 발생"), ("REVIEW", "판단 정보 부족")]:
            out.append(f"| {k} | {cc.get(k, 0)} | {m} |")
        out.append("\n> LLM 단계를 수행하지 않았으므로 요구사항 정합성·사양 공백 분석은 포함되지 않습니다.")
    if findings:
        vc = Counter(f.get("verdict") for f in findings)
        out.append("| 판정 | 건수 |\n|---|---|")
        for v in VERDICT_RANK:
            if vc.get(v):
                out.append(f"| {VERDICT_KO.get(v, v)} | {vc[v]} |")
        cc = Counter(f.get("category") for f in findings)
        out.append("\n| 유형 | 건수 | HIGH |\n|---|---|---|")
        for c, n in cc.most_common():
            hi = sum(1 for f in findings if f.get("category") == c and f.get("severity") == "HIGH")
            out.append(f"| {CATEGORY_KO.get(c, c)} | {n} | {hi} |")
    if result.get("requirements"):
        out.append("\n### 요구사항 커버리지\n")
        out.append("| 요구사항 | 제목 | 추적 | 정합성 | 검출 결과 | 모호/미정의 |\n|---|---|---|---|---|---|")
        tr = {t["req_id"]: t for t in result.get("traces", {}).get("traces", [])}
        rr = {r["req_id"]: r for r in result.get("requirement_results", [])}
        for r in result["requirements"].get("requirements", []):
            fs = [f["id"] for f in findings if r["id"] in f.get("req_ids", [])]
            amb = len(r.get("ambiguities", [])) + len(r.get("undefined_items", []))
            out.append(f"| {r['id']} | {_md(r.get('title'))} | {TRACE_KO.get(tr.get(r['id'], {}).get('status'), '-')} | "
                       f"{CONS_KO.get(rr.get(r['id'], {}).get('verdict'), '-')} | {', '.join(fs) or '-'} | {amb or '-'} |")

    if findings:
        out.append("\n## 2. 검출 결과 상세\n")
        for f in findings:
            out += _finding_md(f, result)
    gaps = [f for f in findings if f.get("category") in ("SPEC_GAP", "AMBIGUOUS")]
    if gaps:
        out.append("\n## 3. 사양 보완 질문 목록\n")
        out.append("| ID | 관련 요구사항 | 질문 | 위험 시나리오 | 현재 코드 동작 |\n|---|---|---|---|---|")
        for f in gaps:
            out.append(f"| {f['id']} | {_md(', '.join(f.get('req_ids', [])))} | {_md(f.get('spec_question'))} | "
                       f"{_md(f.get('risk_scenario'))} | {_md(f.get('inferred_behavior'))} |")
    if result.get("requirements"):
        out.append("\n## 4. 요구사항 구조화 결과\n")
        for r in result["requirements"].get("requirements", []):
            out += _req_md(r)
        out.append("\n## 5. 추적 매트릭스\n")
        for t in result.get("traces", {}).get("traces", []):
            out.append(f"### {t['req_id']} — {TRACE_KO.get(t.get('status'), t.get('status'))} (신뢰도 {t.get('confidence', '-')})\n")
            for loc in t.get("locations", []):
                chk = loc.get("check", {})
                out.append(f"- {'✔' if chk.get('verified') else '✖'} `{loc.get('file')}:{loc.get('line_start')}"
                           f"{'-' + str(loc.get('line_end')) if loc.get('line_end') and loc.get('line_end') != loc.get('line_start') else ''}` "
                           f"{_fn(loc.get('function'))} [{loc.get('role')}] {loc.get('element')}: {loc.get('explanation')}"
                           + ("" if chk.get("note") in (None, "확인됨") else f" ({chk.get('note')})"))
            if t.get("call_path"):
                out.append(f"- 호출 경로: {' → '.join(t['call_path'])}")
            if t.get("unmatched_elements"):
                out.append(f"- 대응 코드 없음: {', '.join(t['unmatched_elements'])}")
            if t.get("search_notes"):
                out.append(f"- 탐색 메모: {t['search_notes']}")
            for n in t.get("guardrail_notes", []):
                out.append(f"- ⚠ {n}")
            out.append("")
    out.append(f"\n## {'2' if static_only else '6'}. 정적 분석 후보{' 및 판정' if not static_only else ''}\n")
    reviews = {r["candidate_id"]: r for r in result.get("candidate_reviews", [])}
    out.append("| ID | 규칙 | 확실성 | 심각도 | 위치 | 내용 | LLM 판정 |\n|---|---|---|---|---|---|---|")
    for c in result.get("candidates", []):
        rv = reviews.get(c["id"])
        rv_txt = f"{ASSESS_KO.get(rv['assessment'], rv['assessment'])}: {rv.get('reason', '')}" if rv else "-"
        tool = ("<br>도구: " + "<br>".join(c["tool_evidence"])) if c.get("tool_evidence") else ""
        out.append(f"| {c['id']} | {c['rule']} | {c['certainty']} | {c['severity']} | `{c['file']}:{c['line']}` "
                   f"{_fn(c['function'])} | {_md(c['message'])}{_md(tool)} | {_md(rv_txt)} |")
    tcs = all_test_cases(result)
    if tcs:
        out.append("\n## 7. 테스트 케이스 일람\n")
        out.append("| TC | 관련 | 기법 | 사전 조건 | 조작 | 기대 결과 | 관찰 수단 |\n|---|---|---|---|---|---|---|")
        for t in tcs:
            out.append(f"| {t['tc_id']} | {t['req_ids'] or t['finding']} | {t['technique']} | {_md(t['preconditions'])} | "
                       f"{_md(t['steps'])} | {_md(t['expected'])} | {_md(t['observation'])} |")
    if result.get("llm_usage"):
        out.append("\n## 부록. LLM 호출 기록\n")
        out.append("| 호출 | 모델 | 입력 | 캐시 읽기 | 출력 | 시간(s) |\n|---|---|---|---|---|---|")
        for u in result["llm_usage"]:
            out.append(f"| {u.get('label')} | {u.get('model')} | {u.get('input_tokens', '-')} | "
                       f"{u.get('cache_read_input_tokens', '-')} | {u.get('output_tokens', '-')} | {u.get('seconds', '-')} |")
    return "\n".join(out) + "\n"


def _finding_md(f: dict, result: dict) -> list[str]:
    o = [f"### [{f['id']}] {f.get('title')}\n"]
    o.append(f"**{VERDICT_KO.get(f.get('verdict'), f.get('verdict'))}** · 심각도 {f.get('severity')} · "
             f"{CATEGORY_KO.get(f.get('category'), f.get('category'))}"
             + (f" / {f['subcategory']}" if f.get("subcategory") else "")
             + f" · 단계: {STAGE_KO.get(f.get('stage'), f.get('stage'))}\n")
    o.append(f"1. **요구사항**: {', '.join(f.get('req_ids') or ['-'])}")
    if f.get("req_quote"):
        o.append(f"   > {f['req_quote']}")
    o.append("2. **코드 위치**:")
    for r in f.get("code_refs") or []:
        o.append(f"   - {_ref_md(r)}")
        if r.get("snippet"):
            o.append("     ```c\n" + "\n".join("     " + x for x in r["snippet"].splitlines()) + "\n     ```")
    if not f.get("code_refs"):
        o.append("   - (없음)")
    o.append(f"3. **유형/심각도**: {CATEGORY_KO.get(f.get('category'))} / {f.get('severity')}")
    o.append(f"4. **사양 vs 코드**\n   - 사양: {f.get('spec_says') or '-'}\n   - 코드: {f.get('code_does') or '-'}\n"
             f"   - 비교: {f.get('comparison') or '-'}")
    o.append(f"5. **발생 조건**: {f.get('trigger_condition') or '-'}")
    for i, p in enumerate(f.get("execution_path") or [], 1):
        o.append(f"   {i}) {p}")
    o.append(f"6. **예상 동작(사양)**: {f.get('expected_behavior') or '-'}\n   **코드 추론 동작**: {f.get('inferred_behavior') or '-'}")
    o.append("7. **판단 근거**")
    for b in f.get("basis") or []:
        o.append(f"   - [{BASIS_KO.get(b.get('kind'), b.get('kind'))}] {b.get('statement')}"
                 + (f" — 근거: {b['evidence']}" if b.get("evidence") else ""))
    o.append(f"   - 불확실성: {f.get('uncertainty') or '-'}")
    for n in f.get("guardrail_notes") or []:
        o.append(f"   - ⚠ 가드레일 조정: {n}")
    o.append(f"8. **권장 수정 방향**: {f.get('recommendation') or '-'}")
    o.append("9. **추가 확인 필요**: " + ("; ".join(f.get("additional_checks") or []) or "-"))
    if f.get("spec_question"):
        o.append(f"   - 사양 질문: {f['spec_question']}")
    if f.get("risk_scenario"):
        o.append(f"   - 위험 시나리오: {f['risk_scenario']}")
    o.append("10. **테스트 케이스**\n")
    tcs = f.get("test_cases") or []
    if tcs:
        o.append("| # | 기법 | 사전 조건 | 조작 | 기대 결과 | 관찰 수단 |\n|---|---|---|---|---|---|")
        for i, t in enumerate(tcs, 1):
            o.append(f"| {i} | {t.get('technique')} | {_md(t.get('preconditions'))} | {_md(t.get('steps'))} | "
                     f"{_md(t.get('expected'))} | {_md(t.get('observation'))} |")
    extra = []
    if f.get("static_candidates"):
        extra.append(f"관련 정적 후보: {', '.join(f['static_candidates'])}")
    if f.get("related"):
        extra.append(f"같은 코드 위치의 다른 결과: {', '.join(f['related'])}")
    if extra:
        o.append("\n" + " · ".join(extra))
    o.append("")
    return o


def _req_md(r: dict) -> list[str]:
    src = r.get("source", {})
    chk = r.get("source_check", {})
    o = [f"### {r['id']} {r.get('title', '')}" + (" (생성 ID)" if r.get("id_origin") == "GENERATED" else "")]
    o.append(f"- 원문(S{src.get('line_start')}-S{src.get('line_end')}"
             f"{'' if chk.get('quote_found', True) else ', ✖ 원문 대조 실패'}): {src.get('quote')}")
    if chk.get("note"):
        o.append(f"- ⚠ {chk['note']}")
    o.append(f"- 목적: {r.get('purpose', '-')}")
    rows = []
    for key, label in [("inputs", "입력"), ("outputs", "출력"), ("execution_conditions", "실행 조건"),
                       ("decision_conditions", "판단 조건"), ("states", "상태"), ("exceptions", "예외"),
                       ("priorities", "우선순위")]:
        for e in r.get(key, []):
            rows.append((label, e.get("name"), f"{e.get('detail', '')} {e.get('value', '')}{e.get('unit', '')}".strip(),
                         e.get("status")))
    for t in r.get("transitions", []):
        rows.append(("상태 전이", f"{t.get('from')} → {t.get('to')}", t.get("condition"), t.get("status")))
    for t in r.get("timers_counters", []):
        rows.append((f"타이머/카운터({t.get('kind')})", t.get("name"), f"{t.get('value')}{t.get('unit')} {t.get('behavior')}",
                     t.get("status")))
    for t in r.get("thresholds_boundaries", []):
        rows.append(("임계/경계", t.get("item"), f"{t.get('value')}{t.get('unit')} ({t.get('inclusive')})", t.get("status")))
    if rows:
        o.append("\n| 요소 | 이름 | 내용 | 상태 |\n|---|---|---|---|")
        for a, b, c, d in rows:
            o.append(f"| {a} | {_md(b)} | {_md(c)} | {STATUS_KO.get(d, d)} |")
    for a in r.get("ambiguities", []):
        o.append(f"- **AMBIGUOUS** {a.get('element')}: \"{a.get('quote')}\" — {a.get('reason')} → 질문: {a.get('question')}")
    for u in r.get("undefined_items", []):
        o.append(f"- **UNDEFINED** {u.get('element')}: {u.get('description')} → 질문: {u.get('question')}")
    o.append("")
    return o


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------

CSS = """
:root{--bg:#f7f8fa;--card:#fff;--fg:#1d2330;--muted:#5b6475;--line:#dde1e8;--soft:#eef1f5;--accent:#2a5bd7;
--hi:#c62828;--hi-bg:#fdecea;--md:#b26a00;--md-bg:#fff4e0;--lo:#2e7d32;--lo-bg:#e9f5ea;--info:#546e7a;--info-bg:#eceff1;
--code:#f3f5f8;--ok:#2e7d32;--bad:#c62828}
@media (prefers-color-scheme: dark){:root:not([data-theme="light"]){--bg:#11141a;--card:#191d25;--fg:#e4e7ee;--muted:#9aa3b2;
--line:#2c323d;--soft:#222833;--accent:#7aa2ff;--hi:#ff8a80;--hi-bg:#3a1f1f;--md:#ffcc80;--md-bg:#3a2e1a;--lo:#a5d6a7;
--lo-bg:#1d3320;--info:#b0bec5;--info-bg:#263238;--code:#0f1217;--ok:#81c784;--bad:#ff8a80}}
:root[data-theme="dark"]{--bg:#11141a;--card:#191d25;--fg:#e4e7ee;--muted:#9aa3b2;--line:#2c323d;--soft:#222833;
--accent:#7aa2ff;--hi:#ff8a80;--hi-bg:#3a1f1f;--md:#ffcc80;--md-bg:#3a2e1a;--lo:#a5d6a7;--lo-bg:#1d3320;--info:#b0bec5;
--info-bg:#263238;--code:#0f1217;--ok:#81c784;--bad:#ff8a80}
*{box-sizing:border-box}
a{color:var(--accent)}
body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.6 -apple-system,"Segoe UI","Malgun Gothic","Apple SD Gothic Neo",sans-serif}
header{padding:20px 24px;border-bottom:1px solid var(--line);background:var(--card)}
header h1{margin:0 0 4px;font-size:20px}header p{margin:0;color:var(--muted)}
nav{position:sticky;top:0;z-index:5;display:flex;gap:4px;flex-wrap:wrap;padding:8px 24px;background:var(--card);border-bottom:1px solid var(--line)}
nav a{color:var(--fg);text-decoration:none;padding:4px 10px;border-radius:6px;font-size:13px}nav a:hover{background:var(--soft)}
main{max-width:1240px;margin:0 auto;padding:16px}
section{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:8px 20px 16px;margin-bottom:16px}
h2{font-size:17px;margin:12px 0 10px}h3{font-size:15px;margin:14px 0 6px}
table{border-collapse:collapse;width:100%;margin:6px 0;font-size:13px}
th,td{border:1px solid var(--line);padding:5px 8px;text-align:left;vertical-align:top}th{background:var(--soft);font-weight:600}
.tw{overflow-x:auto}
code,pre{font-family:ui-monospace,SFMono-Regular,Consolas,"D2Coding",monospace;font-size:12.5px}
code{background:var(--code);padding:1px 4px;border-radius:4px}
pre{background:var(--code);padding:8px 10px;border-radius:6px;overflow-x:auto;margin:4px 0;line-height:1.45}
.chip{display:inline-block;padding:1px 8px;border-radius:10px;font-size:12px;font-weight:600;border:1px solid transparent;white-space:nowrap}
.sev-HIGH{color:var(--hi);background:var(--hi-bg)}.sev-MEDIUM{color:var(--md);background:var(--md-bg)}
.sev-LOW{color:var(--lo);background:var(--lo-bg)}.sev-INFO{color:var(--info);background:var(--info-bg)}
.v{border-color:var(--line);color:var(--fg)}
.v-CONFIRMED_DEFECT{color:#fff;background:var(--hi)}.v-PROBABLE_DEFECT{color:var(--hi);border-color:var(--hi)}
.v-POTENTIAL_RISK{color:var(--md);border-color:var(--md)}.v-NEEDS_REVIEW{color:var(--muted);border-color:var(--muted)}
.v-SPEC_CLARIFICATION{color:var(--accent);border-color:var(--accent)}
.ok{color:var(--ok)}.bad{color:var(--bad)}.muted{color:var(--muted)}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin:8px 0}
.card{border:1px solid var(--line);border-radius:8px;padding:10px 12px;background:var(--soft)}
.card b{display:block;font-size:22px}.card span{color:var(--muted);font-size:12px}
details.f{border:1px solid var(--line);border-radius:8px;margin:10px 0;background:var(--card)}
details.f>summary{cursor:pointer;padding:10px 12px;list-style:none;display:flex;gap:8px;align-items:center;flex-wrap:wrap}
details.f>summary::-webkit-details-marker{display:none}
details.f[open]>summary{border-bottom:1px solid var(--line)}
.fbody{padding:4px 14px 12px}
.kv{display:grid;grid-template-columns:150px 1fr;gap:4px 12px;margin:8px 0}
.kv>div:nth-child(odd){color:var(--muted);font-weight:600}
blockquote{margin:4px 0;padding:4px 10px;border-left:3px solid var(--accent);background:var(--soft)}
.basis li{margin:2px 0}.tag{font-size:11px;font-weight:700;padding:0 6px;border-radius:4px;margin-right:4px}
.FACT{background:var(--lo-bg);color:var(--lo)}.INFERENCE{background:var(--md-bg);color:var(--md)}.HYPOTHESIS{background:var(--info-bg);color:var(--info)}
.guard{color:var(--md)}
.filters{display:flex;gap:6px;flex-wrap:wrap;margin:8px 0}
.filters button{border:1px solid var(--line);background:var(--card);color:var(--fg);border-radius:14px;padding:3px 10px;cursor:pointer;font-size:12px}
.filters button.on{background:var(--accent);color:#fff;border-color:var(--accent)}
.review{margin-top:10px;padding:8px 10px;border:1px dashed var(--line);border-radius:6px;display:flex;gap:8px;flex-wrap:wrap;align-items:center}
.review select,.review input{background:var(--card);color:var(--fg);border:1px solid var(--line);border-radius:4px;padding:3px 6px;font:inherit}
.review input{flex:1;min-width:200px}
.btn{border:1px solid var(--accent);color:var(--accent);background:transparent;border-radius:6px;padding:4px 12px;cursor:pointer;font:inherit}
.st-AMBIGUOUS{color:var(--md);font-weight:600}.st-UNDEFINED{color:var(--hi);font-weight:600}
@media (max-width:640px){main{padding:8px}section{padding:6px 12px 12px}.kv{grid-template-columns:1fr}header,nav{padding-left:16px;padding-right:16px}}
@media print{nav,.filters,.review,.btn{display:none}details.f{break-inside:avoid}details.f:not([open])>.fbody{display:block}}
"""

JS = r"""
(function(){
  var KEY='specverify-review:'+document.body.dataset.rid;
  var store={};try{store=JSON.parse(localStorage.getItem(KEY)||'{}')}catch(e){}
  function save(){try{localStorage.setItem(KEY,JSON.stringify(store))}catch(e){}}
  document.querySelectorAll('.review').forEach(function(el){
    var id=el.dataset.id,s=el.querySelector('select'),i=el.querySelector('input');
    if(store[id]){s.value=store[id].decision||'';i.value=store[id].note||''}
    function up(){store[id]={decision:s.value,note:i.value};save();count()}
    s.addEventListener('change',up);i.addEventListener('input',up);
  });
  function count(){var n=0;for(var k in store){if(store[k].decision)n++}var c=document.getElementById('rv-count');if(c)c.textContent=n}
  count();
  var ex=document.getElementById('export');
  if(ex)ex.addEventListener('click',function(){
    var blob=new Blob([JSON.stringify({report:document.body.dataset.rid,reviews:store},null,2)],{type:'application/json'});
    var a=document.createElement('a');a.href=URL.createObjectURL(blob);a.download='verifier_review.json';a.click();
  });
  var active={};
  document.querySelectorAll('.filters button').forEach(function(b){
    b.addEventListener('click',function(){
      var k=b.dataset.k,v=b.dataset.v;
      if(active[k]===v){delete active[k];b.classList.remove('on')}
      else{document.querySelectorAll('.filters button[data-k="'+k+'"]').forEach(function(x){x.classList.remove('on')});active[k]=v;b.classList.add('on')}
      document.querySelectorAll('details.f').forEach(function(d){
        var show=true;for(var kk in active){if(d.dataset[kk]!==active[kk])show=false}
        d.style.display=show?'':'none';
      });
    });
  });
  var t=document.getElementById('expand');
  if(t)t.addEventListener('click',function(){var o=t.dataset.o!=='1';document.querySelectorAll('details.f').forEach(function(d){if(d.style.display!=='none')d.open=o});t.dataset.o=o?'1':'0';t.textContent=o?'모두 접기':'모두 펼치기'});
})();
"""


def e(s) -> str:
    return html.escape(str(s if s is not None else ""))


def _chip(cls: str, text: str) -> str:
    return f'<span class="chip {cls}">{e(text)}</span>'


def _ref_html(r: dict) -> str:
    chk = r.get("check", {})
    ok = chk.get("verified")
    note = "" if chk.get("note") in (None, "확인됨") else f' <span class="muted">({e(chk.get("note"))})</span>'
    sym = f" <code>{e(r['symbol'])}</code>" if r.get("symbol") else ""
    role = f" — {e(r['role'])}" if r.get("role") else ""
    snip = f"<pre>{e(r['snippet'])}</pre>" if r.get("snippet") else ""
    return (f'<li><span class="{"ok" if ok else "bad"}" title="근거 위치 검증">{"✔" if ok else "✖"}</span> '
            f'<code>{e(r.get("file"))}:{e(r.get("line"))}</code> {e(_fn(r.get("function")))}{sym}{role}{note}{snip}</li>')


def _table(head: list, rows: list) -> str:
    return ('<div class="tw"><table><thead><tr>' + "".join(f"<th>{h}</th>" for h in head) + "</tr></thead><tbody>"
            + "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in rows) + "</tbody></table></div>")


def _finding_html(f: dict) -> str:
    kv = []

    def row(k, v):
        kv.append(f"<div>{k}</div><div>{v}</div>")

    reqs = ", ".join(e(x) for x in f.get("req_ids") or []) or "-"
    quote = f"<blockquote>{e(f['req_quote'])}</blockquote>" if f.get("req_quote") else ""
    row("1. 요구사항", reqs + quote)
    refs = "".join(_ref_html(r) for r in f.get("code_refs") or [])
    row("2. 코드 위치", f"<ul>{refs}</ul>" if refs else "-")
    row("3. 유형·심각도", f"{e(CATEGORY_KO.get(f.get('category'), f.get('category')))}"
        + (f" / {e(f['subcategory'])}" if f.get("subcategory") else "") + f" · {_chip('sev-' + str(f.get('severity')), f.get('severity'))}")
    row("4. 사양 vs 코드", f"<b>사양</b>: {e(f.get('spec_says') or '-')}<br><b>코드</b>: {e(f.get('code_does') or '-')}"
        f"<br><b>비교</b>: {e(f.get('comparison') or '-')}")
    path = "".join(f"<li>{e(p)}</li>" for p in f.get("execution_path") or [])
    row("5. 발생 조건·경로", e(f.get("trigger_condition") or "-") + (f"<ol>{path}</ol>" if path else ""))
    row("6. 예상 vs 추론 동작", f"<b>사양 기준</b>: {e(f.get('expected_behavior') or '-')}<br>"
        f"<b>코드 추론</b>: {e(f.get('inferred_behavior') or '-')}")
    basis = "".join(f'<li><span class="tag {e(b.get("kind"))}">{e(BASIS_KO.get(b.get("kind"), b.get("kind")))}</span>'
                    f'{e(b.get("statement"))}' + (f' <span class="muted">— {e(b["evidence"])}</span>' if b.get("evidence") else "")
                    + "</li>" for b in f.get("basis") or [])
    guard = "".join(f'<li class="guard">⚠ 가드레일 조정: {e(n)}</li>' for n in f.get("guardrail_notes") or [])
    row("7. 판단 근거", f'<ul class="basis">{basis}{guard}</ul><b>불확실성</b>: {e(f.get("uncertainty") or "-")}')
    row("8. 권장 수정 방향", e(f.get("recommendation") or "-"))
    checks = "".join(f"<li>{e(x)}</li>" for x in f.get("additional_checks") or [])
    extra = ""
    if f.get("spec_question"):
        extra += f"<li><b>사양 질문</b>: {e(f['spec_question'])}</li>"
    if f.get("risk_scenario"):
        extra += f"<li><b>위험 시나리오</b>: {e(f['risk_scenario'])}</li>"
    row("9. 추가 확인 필요", f"<ul>{checks}{extra}</ul>" if checks or extra else "-")
    tcs = f.get("test_cases") or []
    tc_html = _table(["#", "기법", "사전 조건", "조작", "기대 결과", "관찰 수단"],
                     [[str(i), e(t.get("technique")), e(t.get("preconditions")), e(t.get("steps")), e(t.get("expected")),
                       e(t.get("observation"))] for i, t in enumerate(tcs, 1)]) if tcs else "-"
    row("10. 테스트 케이스", tc_html)
    links = []
    if f.get("static_candidates"):
        links.append("관련 정적 후보: " + ", ".join(f'<a href="#{e(c)}">{e(c)}</a>' for c in f["static_candidates"]))
    if f.get("related"):
        links.append("같은 코드 위치: " + ", ".join(f'<a href="#{e(c)}">{e(c)}</a>' for c in f["related"]))
    review = (f'<div class="review" data-id="{e(f["id"])}"><b>검증자 판정</b><select><option value="">-</option>'
              '<option value="ACCEPT">수용(결함/보완 필요)</option><option value="REJECT">기각(오탐)</option>'
              '<option value="DEFER">보류(추가 확인)</option></select><input placeholder="메모 (근거, 담당, 티켓 번호)"></div>')
    summary = (f"<summary>{_chip('v v-' + str(f.get('verdict')), VERDICT_KO.get(f.get('verdict'), f.get('verdict')))}"
               f"{_chip('sev-' + str(f.get('severity')), f.get('severity'))}<b>[{e(f['id'])}]</b> {e(f.get('title'))}"
               f' <span class="muted">· {e(CATEGORY_KO.get(f.get("category"), f.get("category")))} · {reqs}</span></summary>')
    return (f'<details class="f" id="{e(f["id"])}" data-verdict="{e(f.get("verdict"))}" data-severity="{e(f.get("severity"))}" '
            f'data-stage="{e(f.get("stage"))}">{summary}<div class="fbody"><div class="kv">{"".join(kv)}</div>'
            + (f'<p class="muted">{" · ".join(links)}</p>' if links else "") + review + "</div></details>")


def to_html(result: dict) -> str:
    meta = result["meta"]
    title = f"SW 검증 리뷰 — {meta.get('spec') or '정적 분석'}"
    findings = sorted_findings(result.get("findings", []))
    static_only = not result.get("requirements")
    nav, body = [], []

    # 개요
    vc = Counter(f.get("verdict") for f in findings)
    cards = [("검출 결과", len(findings)), ("결함 확정", vc.get("CONFIRMED_DEFECT", 0)),
             ("결함 가능성 높음", vc.get("PROBABLE_DEFECT", 0)), ("잠재 위험", vc.get("POTENTIAL_RISK", 0)),
             ("사양 보완 필요", vc.get("SPEC_CLARIFICATION", 0)), ("검토 필요", vc.get("NEEDS_REVIEW", 0)),
             ("정적 후보", len(result.get("candidates", [])))]
    if static_only:
        cc = Counter(c["certainty"] for c in result.get("candidates", []))
        cards = [("정적 후보", len(result.get("candidates", [])))] + [(k, cc.get(k, 0)) for k in ("CERTAIN", "PROBABLE", "POSSIBLE", "REVIEW")]
    over = '<div class="cards">' + "".join(f'<div class="card"><b>{n}</b><span>{e(k)}</span></div>' for k, n in cards) + "</div>"
    info = _table(["항목", "내용"], [[e(k), e(v)] for k, v in [
        ("사양서", meta.get("spec") or "-"), ("소스", ", ".join(meta.get("sources", []))),
        ("실행 환경 정보", result["environment"]["source"]), ("LLM", meta.get("llm")),
        ("수행 단계", ", ".join(meta.get("stages", []))), ("전처리기", result["code"].get("preprocessor")),
        ("외부 도구", "; ".join(result["code"].get("tool_notes", [])) or "-"), ("생성 시각", meta.get("generated_at"))]])
    lim = "".join(f"<li>{e(a)}</li>" for a in result["environment"]["assumptions"])
    lim += "".join(f"<li>[코드 분석 제한] {e(i['file'])}: {e(i['message'])}</li>" for i in result["code"].get("issues", []))
    nav.append(("overview", "개요"))
    body.append(f'<section id="overview"><h2>0. 분석 개요</h2>{over}{info}<h3>분석 전제 및 제한</h3><ul>{lim}</ul>'
                '<p class="muted">✔/✖ 표시는 LLM이 제시한 근거 위치를 원본 코드·사양과 기계적으로 대조한 결과입니다. '
                '모든 판정은 검증자의 최종 확인이 필요합니다.</p></section>')

    if result.get("requirements"):
        tr = {t["req_id"]: t for t in result.get("traces", {}).get("traces", [])}
        rr = {r["req_id"]: r for r in result.get("requirement_results", [])}
        rows = []
        for r in result["requirements"].get("requirements", []):
            fs = [f for f in findings if r["id"] in f.get("req_ids", [])]
            amb = len(r.get("ambiguities", [])) + len(r.get("undefined_items", []))
            st = tr.get(r["id"], {}).get("status")
            rows.append([f'<a href="#req-{e(r["id"])}">{e(r["id"])}</a>', e(r.get("title")),
                         f'<span class="{"bad" if st == "TRACE_NOT_FOUND" else ""}">{e(TRACE_KO.get(st, "-"))}</span>',
                         e(CONS_KO.get(rr.get(r["id"], {}).get("verdict"), "-")),
                         " ".join(f'<a href="#{e(f["id"])}">{_chip("sev-" + str(f.get("severity")), f["id"])}</a>' for f in fs) or "-",
                         str(amb or "-")])
        nav.append(("coverage", "요구사항 커버리지"))
        body.append('<section id="coverage"><h2>1. 요구사항 커버리지</h2>'
                    + _table(["요구사항", "제목", "추적", "정합성", "검출 결과", "모호/미정의"], rows) + "</section>")

    if findings:
        def btns(k, vals, labels):
            return "".join(f'<button data-k="{k}" data-v="{e(v)}">{e(labels.get(v, v))}</button>' for v in vals)
        filt = ('<div class="filters">' + btns("verdict", [v for v in VERDICT_RANK if vc.get(v)], VERDICT_KO)
                + btns("severity", [s for s in SEV_ORDER if any(f.get("severity") == s for f in findings)], {})
                + btns("stage", [s for s in STAGE_KO if any(f.get("stage") == s for f in findings)], STAGE_KO)
                + '<button id="expand" class="btn" style="margin-left:auto">모두 펼치기</button></div>')
        nav.append(("findings", "검출 결과"))
        body.append(f'<section id="findings"><h2>2. 검출 결과 상세</h2>{filt}'
                    f'<p class="muted">검증자 판정: <span id="rv-count">0</span>/{len(findings)} 입력됨 (이 브라우저에 저장) '
                    f'<button id="export" class="btn">판정 내보내기(JSON)</button></p>'
                    + "".join(_finding_html(f) for f in findings) + "</section>")
    gaps = [f for f in findings if f.get("category") in ("SPEC_GAP", "AMBIGUOUS")]
    if gaps:
        nav.append(("questions", "사양 질문"))
        body.append('<section id="questions"><h2>3. 사양 보완 질문 목록</h2>' + _table(
            ["ID", "관련 요구사항", "질문", "위험 시나리오", "현재 코드 동작"],
            [[f'<a href="#{e(f["id"])}">{e(f["id"])}</a>', e(", ".join(f.get("req_ids", []))), e(f.get("spec_question")),
              e(f.get("risk_scenario")), e(f.get("inferred_behavior"))] for f in gaps]) + "</section>")

    if result.get("requirements"):
        parts = []
        for r in result["requirements"].get("requirements", []):
            src, chk = r.get("source", {}), r.get("source_check", {})
            rows = []
            for key, label in [("inputs", "입력"), ("outputs", "출력"), ("execution_conditions", "실행 조건"),
                               ("decision_conditions", "판단 조건"), ("states", "상태"), ("exceptions", "예외"),
                               ("priorities", "우선순위")]:
                for x in r.get(key, []):
                    rows.append([label, e(x.get("name")), e(f"{x.get('detail', '')} {x.get('value', '')}{x.get('unit', '')}".strip()),
                                 x.get("status")])
            for t in r.get("transitions", []):
                rows.append(["상태 전이", e(f"{t.get('from')} → {t.get('to')}"), e(t.get("condition")), t.get("status")])
            for t in r.get("timers_counters", []):
                rows.append(["타이머/카운터", e(t.get("name")), e(f"{t.get('value')}{t.get('unit')} · {t.get('behavior')}"), t.get("status")])
            for t in r.get("thresholds_boundaries", []):
                rows.append(["임계/경계", e(t.get("item")), e(f"{t.get('value')}{t.get('unit')} ({t.get('inclusive')})"), t.get("status")])
            rows = [a[:3] + [f'<span class="st-{e(a[3])}">{e(STATUS_KO.get(a[3], a[3]))}</span>'] for a in rows]
            amb = "".join(f'<li><span class="st-AMBIGUOUS">AMBIGUOUS</span> {e(a.get("element"))}: “{e(a.get("quote"))}” — '
                          f'{e(a.get("reason"))}<br><span class="muted">질문: {e(a.get("question"))}</span></li>' for a in r.get("ambiguities", []))
            amb += "".join(f'<li><span class="st-UNDEFINED">UNDEFINED</span> {e(u.get("element"))}: {e(u.get("description"))}'
                           f'<br><span class="muted">질문: {e(u.get("question"))}</span></li>' for u in r.get("undefined_items", []))
            warn = "" if chk.get("quote_found", True) else ' <span class="bad">✖ 원문 대조 실패</span>'
            note = f'<p class="guard">⚠ {e(chk["note"])}</p>' if chk.get("note") else ""
            parts.append(f'<h3 id="req-{e(r["id"])}">{e(r["id"])} {e(r.get("title"))}'
                         + (' <span class="muted">(생성 ID)</span>' if r.get("id_origin") == "GENERATED" else "") + "</h3>"
                         f'<blockquote>S{e(src.get("line_start"))}-S{e(src.get("line_end"))}{warn}: {e(src.get("quote"))}</blockquote>{note}'
                         f'<p>목적: {e(r.get("purpose"))}</p>' + (_table(["요소", "이름", "내용", "상태"], rows) if rows else "")
                         + (f"<ul>{amb}</ul>" if amb else ""))
        nav.append(("requirements", "요구사항 구조화"))
        body.append('<section id="requirements"><h2>4. 요구사항 구조화 결과</h2>' + "".join(parts) + "</section>")
        tparts = []
        for t in result.get("traces", {}).get("traces", []):
            locs = "".join(
                f'<li><span class="{"ok" if l.get("check", {}).get("verified") else "bad"}">{"✔" if l.get("check", {}).get("verified") else "✖"}</span> '
                f'<code>{e(l.get("file"))}:{e(l.get("line_start"))}</code> {e(_fn(l.get("function")))} <span class="chip v">{e(l.get("role"))}</span> '
                f'{e(l.get("element"))}: {e(l.get("explanation"))}'
                + ("" if l.get("check", {}).get("note") in (None, "확인됨") else f' <span class="muted">({e(l["check"]["note"])})</span>')
                + "</li>" for l in t.get("locations", []))
            extra = ""
            if t.get("call_path"):
                extra += f"<li>호출 경로: {e(' → '.join(t['call_path']))}</li>"
            if t.get("unmatched_elements"):
                extra += f"<li>대응 코드 없음: {e(', '.join(t['unmatched_elements']))}</li>"
            if t.get("search_notes"):
                extra += f'<li class="muted">탐색 메모: {e(t["search_notes"])}</li>'
            extra += "".join(f'<li class="guard">⚠ {e(n)}</li>' for n in t.get("guardrail_notes", []))
            tparts.append(f'<h3>{e(t["req_id"])} — {e(TRACE_KO.get(t.get("status"), t.get("status")))} '
                          f'<span class="muted">(신뢰도 {e(t.get("confidence"))})</span></h3><ul>{locs}{extra}</ul>')
        nav.append(("traces", "추적 매트릭스"))
        body.append('<section id="traces"><h2>5. 추적 매트릭스</h2>' + "".join(tparts) + "</section>")

    reviews = {r["candidate_id"]: r for r in result.get("candidate_reviews", [])}
    crow = []
    for c in result.get("candidates", []):
        rv = reviews.get(c["id"])
        ev = "".join(f"<li>{e(x)}</li>" for x in c.get("evidence", []) if x)
        ev += "".join(f'<li class="ok">도구: {e(x)}</li>' for x in c.get("tool_evidence", []))
        need = f'<div class="muted">확정에 필요한 정보: {e("; ".join(c["needs"]))}</div>' if c.get("needs") else ""
        snip = f"<pre>{e(c['snippet'])}</pre>" if c.get("snippet") else ""
        crow.append([f'<span id="{e(c["id"])}">{e(c["id"])}</span>', e(c["rule"]), e(c["certainty"]),
                     _chip("sev-" + c["severity"], c["severity"]), f'<code>{e(c["file"])}:{e(c["line"])}</code><br>{e(_fn(c["function"]))}',
                     e(c["message"]) + (f"<ul>{ev}</ul>" if ev else "") + need + snip,
                     (f"<b>{e(ASSESS_KO.get(rv['assessment'], rv['assessment']))}</b>: {e(rv.get('reason'))}"
                      + (f' → <a href="#{e(rv["finding_id"])}">{e(rv["finding_id"])}</a>' if rv.get("finding_id") else "")) if rv else "-"])
    nav.append(("static", "정적 분석"))
    body.append(f'<section id="static"><h2>{"2" if static_only else "6"}. 정적 분석 후보{"" if static_only else " 및 판정"}</h2>'
                '<p class="muted">CERTAIN=자료형·상수만으로 성립 · PROBABLE=구조적 근거 강함 · POSSIBLE=런타임 조건 의존 · REVIEW=정보 부족</p>'
                + _table(["ID", "규칙", "확실성", "심각도", "위치", "내용·근거", "LLM 판정"], crow) + "</section>")

    tcs = all_test_cases(result)
    if tcs:
        nav.append(("tests", "테스트 케이스"))
        body.append('<section id="tests"><h2>7. 테스트 케이스 일람</h2><p class="muted">test_cases.csv로도 저장됩니다.</p>'
                    + _table(["TC", "관련", "기법", "사전 조건", "조작", "기대 결과", "관찰 수단"],
                             [[f'<a href="#{e(t["finding"])}">{e(t["tc_id"])}</a>', e(t["req_ids"] or t["finding"]),
                               e(t["technique"]), e(t["preconditions"]), e(t["steps"]), e(t["expected"]), e(t["observation"])]
                              for t in tcs]) + "</section>")
    navh = "".join(f'<a href="#{k}">{e(v)}</a>' for k, v in nav)
    rid = e(f"{meta.get('spec')}|{meta.get('generated_at')}")
    return (f'<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            f"<title>{e(title)}</title><style>{CSS}</style></head><body data-rid=\"{rid}\">"
            f"<header><h1>{e(title)}</h1><p>자연어 기능사양서–Embedded C 의미적 정합성 검증 · 사양 공백 · 잠재 결함 분석</p></header>"
            f"<nav>{navh}</nav><main>{''.join(body)}</main><script>{JS}</script></body></html>")


# ---------------------------------------------------------------------------
# 저장
# ---------------------------------------------------------------------------


def write_reports(result: dict, out: Path) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    paths = {"json": out / "report.json", "md": out / "report.md", "html": out / "report.html"}
    paths["json"].write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    paths["md"].write_text(to_markdown(result), encoding="utf-8")
    paths["html"].write_text(to_html(result), encoding="utf-8")
    tcs = all_test_cases(result)
    if tcs:
        paths["csv"] = out / "test_cases.csv"
        with paths["csv"].open("w", encoding="utf-8-sig", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=list(tcs[0]))
            w.writeheader()
            w.writerows(tcs)
    return paths
