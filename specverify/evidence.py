"""LLM 출력의 근거 검증과 판정 가드레일.

LLM이 제시한 근거는 그대로 믿지 않는다.
  - 요구사항 원문 인용 → 사양서 원문에 실제로 있는지, 라인 범위가 맞는지
  - 코드 위치(file/function/line/symbol) → 파일·라인이 존재하는지, 함수 범위 안인지, 심볼이 그 근처에 있는지
  - 판정 수준 → 분석 원칙에 맞게 상한을 둔다 (예: 추적 실패만으로 '미구현 확정' 금지,
    실행 환경 정보 없이 동시성 결함 확정 금지, 도구/결정적 근거 없는 런타임 오류 확정 금지)
조정된 내용은 finding["guardrail_notes"]에 남겨 사람이 재검토할 수 있게 한다.
"""

from __future__ import annotations

import re

from . import cfront as C
from .spec import SpecDoc

VERDICT_ORDER = ["CONSISTENT", "SPEC_CLARIFICATION", "NEEDS_REVIEW", "POTENTIAL_RISK", "PROBABLE_DEFECT", "CONFIRMED_DEFECT"]
VERDICT_KO = {
    "CONFIRMED_DEFECT": "결함 확정", "PROBABLE_DEFECT": "결함 가능성 높음", "POTENTIAL_RISK": "잠재 위험",
    "NEEDS_REVIEW": "검토 필요", "SPEC_CLARIFICATION": "사양 보완 필요", "CONSISTENT": "일치",
}


def _cap(f: dict, verdict: str, note: str):
    cur = f.get("verdict", "NEEDS_REVIEW")
    if cur in VERDICT_ORDER and VERDICT_ORDER.index(cur) > VERDICT_ORDER.index(verdict):
        f["verdict"] = verdict
        f.setdefault("guardrail_notes", []).append(f"{VERDICT_KO.get(cur, cur)} → {VERDICT_KO[verdict]}: {note}")


# ---------------------------------------------------------------------------
# 요구사항 원문
# ---------------------------------------------------------------------------


def verify_requirements(doc: SpecDoc, reqs: dict) -> dict:
    known_ids = {i for i, _ in doc.ids}
    all_text = "\n".join(doc.lines)
    for r in reqs.get("requirements", []):
        src = r.get("source") or {}
        hit = doc.find_quote(src.get("quote", ""), src.get("line_start"), src.get("line_end"))
        check = {"quote_found": bool(hit)}
        if hit:
            check["lines"] = list(hit)
            if src.get("line_start") and not (hit[0] - 3 <= src["line_start"] <= hit[1] + 3):
                check["note"] = f"인용문은 S{hit[0]}-S{hit[1]}에 있음 (모델 제시 라인 S{src.get('line_start')} 보정)"
            src["line_start"], src["line_end"] = hit
        else:
            check["note"] = "원문 인용을 사양서에서 찾지 못함 — 재구성된 문장일 수 있으므로 원문 확인 필요"
        if r.get("id_origin") == "SPEC" and r.get("id") not in known_ids and r.get("id", "") not in all_text:
            check["id_note"] = "사양서에 없는 ID를 원문 ID로 표기함 → 생성 ID로 정정"
            r["id_origin"] = "GENERATED"
        r["source_check"] = check
    return reqs


# ---------------------------------------------------------------------------
# 코드 위치
# ---------------------------------------------------------------------------


def verify_code_ref(model: C.CodeModel, ref: dict) -> dict:
    """ref: {file, function, line, [line_end], symbol} → ref['check'] = {verified, note}"""
    notes = []
    file = model.resolve_file(ref.get("file", ""))
    line = int(ref.get("line") or 0)
    fn_name = (ref.get("function") or "").strip().rstrip("()")
    if not file:
        fn = model.find_function(fn_name) if fn_name else None
        if fn:
            file = fn.file
            notes.append(f"파일명 불일치 → 함수 기준 {file}로 보정")
        else:
            ref["check"] = {"verified": False, "note": f"파일을 찾을 수 없음: {ref.get('file')}"}
            return ref
    ref["file"] = file
    sf = model.files[file]
    if not (1 <= line <= len(sf.lines)):
        ref["check"] = {"verified": False, "note": f"라인 {line}이 파일 범위(1-{len(sf.lines)}) 밖"}
        return ref
    actual = model.function_at(file, line)
    if fn_name and fn_name not in ("-", "전역", "global"):
        if actual is None or actual.name != fn_name:
            fn = model.find_function(fn_name, file)
            if fn and fn.file == file:
                notes.append(f"L{line}은 {fn_name}() 범위(L{fn.start}-{fn.end}) 밖"
                             + (f", 실제로는 {actual.name}() 내부" if actual else ""))
            else:
                notes.append(f"함수 {fn_name}()를 {file}에서 찾지 못함")
    sym = (ref.get("symbol") or "").strip()
    sym_ok = True
    if sym:
        tokens = [t for t in re.findall(r"[A-Za-z_]\w*", sym) if len(t) > 1]
        last = max(line, min(int(ref.get("line_end") or line), len(sf.code)))
        window = "\n".join(sf.code[max(0, line - 3): last + 2])
        if tokens and not any(re.search(rf"\b{re.escape(t)}\b", window) for t in tokens):
            # 매크로로 정의된 이름이면 매크로 정의 위치도 허용
            if not any(t in model.macros and model.macros[t].file == file and abs(model.macros[t].line - line) <= 2
                       for t in tokens):
                sym_ok = False
                notes.append(f"심볼 `{sym}`이 L{line}" + (f"-{last}" if last != line else "") + "±2에 없음")
    if sf.is_inactive(line):
        notes.append("현재 빌드 구성에서 비활성(조건부 컴파일 제외)된 라인")
    verified = sym_ok and not any("찾지 못함" in n or "범위" in n for n in notes)
    ref["check"] = {"verified": verified, "note": "; ".join(notes) if notes else "확인됨"}
    return ref


def verify_traces(model: C.CodeModel, traces: dict) -> dict:
    for t in traces.get("traces", []):
        bad = 0
        for loc in t.get("locations", []):
            ref = {"file": loc.get("file"), "function": loc.get("function"), "line": loc.get("line_start"),
                   "line_end": loc.get("line_end"), "symbol": loc.get("symbol")}
            verify_code_ref(model, ref)
            loc["file"] = ref.get("file", loc.get("file"))
            loc["check"] = ref["check"]
            bad += not ref["check"]["verified"]
        if bad:
            t.setdefault("guardrail_notes", []).append(f"추적 위치 {bad}건이 코드와 일치하지 않음 — 확인 필요")
            if t.get("status") == "TRACED" and bad == len(t.get("locations", [])):
                t["status"] = "PARTIAL"
    return traces


# ---------------------------------------------------------------------------
# 판정 가드레일
# ---------------------------------------------------------------------------

RUNTIME_CATS = {"RUNTIME_ERROR", "CONCURRENCY"}


def apply_guardrails(model: C.CodeModel, findings: list, traces: dict | None, candidates: list) -> list:
    trace_by_req = {t["req_id"]: t for t in (traces or {}).get("traces", [])}
    cand_by_id = {c.id: c for c in candidates}
    for f in findings:
        refs = f.get("code_refs") or []
        for r in refs:
            verify_code_ref(model, r)
        unverified = [r for r in refs if not r["check"]["verified"]]
        if refs and len(unverified) == len(refs):
            _cap(f, "NEEDS_REVIEW", "제시된 코드 위치를 모두 확인할 수 없음 (근거 위치 오류 가능)")
        elif unverified:
            f.setdefault("guardrail_notes", []).append(
                f"코드 위치 {len(unverified)}/{len(refs)}건 불일치: " + "; ".join(r["check"]["note"] for r in unverified))
        cat = f.get("category")
        # 사양 공백·모호성은 결함이 아니라 사양 보완 대상
        if cat in ("SPEC_GAP", "AMBIGUOUS"):
            if f.get("verdict") not in ("SPEC_CLARIFICATION", "NEEDS_REVIEW", "POTENTIAL_RISK"):
                _cap(f, "POTENTIAL_RISK", "사양 미정의 조건은 결함으로 확정하지 않음")
        # 추적 실패만으로 미구현 확정 금지
        if cat == "MISSING_IMPLEMENTATION":
            st = [trace_by_req.get(r, {}).get("status") for r in f.get("req_ids", [])]
            if not refs or any(s in (None, "TRACE_NOT_FOUND") for s in st):
                _cap(f, "NEEDS_REVIEW", "구현 위치를 찾지 못한 것(TRACE_NOT_FOUND)만으로는 미구현을 확정하지 않음")
        # 동시성: 실행 환경 정보 없이는 확정 금지
        if cat == "CONCURRENCY" and not model.env.has_contexts:
            _cap(f, "NEEDS_REVIEW", "태스크/ISR 실행 환경 정보가 없어 경쟁 조건을 확정하지 않음")
        # 런타임/동시성 확정에는 결정적 근거(CERTAIN 후보 또는 도구 경고)가 필요
        if cat in RUNTIME_CATS and f.get("verdict") == "CONFIRMED_DEFECT":
            linked = [cand_by_id[i] for i in f.get("static_candidates", []) if i in cand_by_id]
            strong = [c for c in linked if c.certainty == "CERTAIN" or c.tool_evidence]
            if not strong:
                _cap(f, "PROBABLE_DEFECT", "정적 분석 도구/결정적 계산 근거가 없는 런타임·동시성 결함은 확정하지 않음")
        # 근거 구분: 사실 근거가 하나도 없으면 확정 금지
        basis = f.get("basis") or []
        if f.get("verdict") == "CONFIRMED_DEFECT" and not any(b.get("kind") == "FACT" for b in basis):
            _cap(f, "PROBABLE_DEFECT", "사실(FACT)로 분류된 근거가 없음")
        # 비활성 코드에 근거한 결함
        if refs and all("비활성" in r["check"]["note"] for r in refs):
            _cap(f, "NEEDS_REVIEW", "근거 코드가 현재 빌드 구성에서 비활성")
    return findings


def link_related(findings: list) -> None:
    """같은 코드 라인을 근거로 하는 결과끼리 서로 참조 표시 (중복 검출 정리용)."""
    by_loc: dict = {}
    for f in findings:
        for r in f.get("code_refs") or []:
            if r.get("check", {}).get("verified"):
                by_loc.setdefault((r["file"], int(r["line"])), set()).add(f["id"])
    for f in findings:
        rel = set()
        for r in f.get("code_refs") or []:
            rel |= by_loc.get((r.get("file"), int(r.get("line") or 0)), set())
        rel.discard(f["id"])
        f["related"] = sorted(rel)
