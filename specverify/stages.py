"""분석 파이프라인 (단계 0~5) 실행과 단계별 결과 저장/재사용."""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

from . import cfront as C
from . import checks as K
from . import evidence as EV
from . import schemas as SC
from . import tools as T
from .env import Environment, describe
from .spec import SpecDoc

PROMPT_DIR = Path(__file__).resolve().parent.parent / "prompts" / "specverify"

STAGES = [
    ("1_requirements", "사양 분석 (요구사항 구조화)"),
    ("2_traces", "구현 추적"),
    ("3_consistency", "정합성 분석"),
    ("4_gaps", "사양 공백 분석"),
    ("5_defects", "코드 결함 분석"),
]


@dataclass
class Options:
    batch_size: int = 6
    gap_batch_size: int = 12
    candidate_batch: int = 40
    max_source_chars: int = 400_000
    resume: bool = False
    stop_after: int = 5
    prompt_dir: Path = PROMPT_DIR


@dataclass
class Run:
    spec: SpecDoc
    model: C.CodeModel
    env: Environment
    candidates: list
    tool_notes: list
    out: Path
    llm: object = None
    opts: Options = field(default_factory=Options)
    log: object = None

    def say(self, msg: str):
        print(msg, file=self.log or sys.stderr)


# ---------------------------------------------------------------------------
# 단계 0: 정적 분석
# ---------------------------------------------------------------------------


def static_analysis(sources: list[str], env: Environment, use_gcc: bool = True, gcc: str = "gcc",
                    analyzer: bool = False, cppcheck: bool = False):
    model = C.build_model(sources, env)
    cands = K.run_checks(model)
    notes, tool_findings = [], []
    if use_gcc and model.files:
        f, note = T.run_gcc(model, gcc, analyzer)
        tool_findings += f
        notes.append(note)
    if cppcheck:
        f, note = T.run_cppcheck(model)
        tool_findings += f
        notes.append(note)
    if tool_findings:
        cands = T.attach(cands, tool_findings)
    return model, cands, notes


# ---------------------------------------------------------------------------
# 프롬프트 구성
# ---------------------------------------------------------------------------


def _prompt(run: Run, name: str) -> str:
    return (run.opts.prompt_dir / name).read_text(encoding="utf-8")


def _env_block(run: Run) -> str:
    lines = [describe(run.env), "", "분석 전제(가정):"] + [f"- {a}" for a in run.env.assumptions()]
    if run.model.issues:
        lines += ["", "코드 분석 제한:"] + [f"- {i.file}: {i.message}" for i in run.model.issues]
    return "\n".join(lines)


def _source_block(run: Run) -> str | None:
    parts = []
    for sf in run.model.files.values():
        parts.append(f"=== {sf.path} ===\n" + C.render_excerpt(run.model, sf.path, 1, len(sf.lines)))
    text = "\n\n".join(parts)
    return text if len(text) <= run.opts.max_source_chars else None


def _req_json(reqs: dict, ids: set | None = None) -> str:
    items = []
    for r in reqs.get("requirements", []):
        if ids is not None and r["id"] not in ids:
            continue
        r = {k: v for k, v in r.items() if k != "source_check"}
        items.append(r)
    return json.dumps({"requirements": items}, ensure_ascii=False)


def _shared_context(run: Run, reqs: dict) -> dict:
    """단계 2~5가 공유하는 큰 문맥 (프롬프트 캐시 대상)."""
    parts = [f"<분석_환경>\n{_env_block(run)}\n</분석_환경>",
             f"<기능사양서 이름=\"{run.spec.name}\">\n{run.spec.numbered()}\n</기능사양서>",
             f"<구조화된_요구사항>\n{_req_json(reqs)}\n</구조화된_요구사항>",
             f"<코드_색인>\n{C.render_index(run.model)}\n</코드_색인>"]
    src = _source_block(run)
    if src is not None:
        parts.append("<소스코드 설명=\"라인번호, '>'=관련 라인, '⊘'=현재 빌드에서 비활성\">\n" + src + "\n</소스코드>")
    return {"type": "text", "text": "\n\n".join(parts), "cache_control": {"type": "ephemeral"}}


def _excerpts(run: Run, keys: set, marks: dict) -> str:
    """전체 소스를 넣지 못할 때 관련 함수만 발췌 (호출자/피호출자 1단계 포함)."""
    if _source_block(run) is not None:
        return ""
    fns = {}
    for k in keys:
        fn = run.model.functions.get(k)
        if not fn:
            continue
        fns[fn.key] = fn
        for g in run.model.callees(fn) + run.model.callers(fn):
            fns.setdefault(g.key, g)
    budget = run.opts.max_source_chars
    out, used = [], 0
    for fn in fns.values():
        t = C.render_function(run.model, fn, marks.get(fn.file))
        if used + len(t) > budget:
            out.append(f"(발췌 한도 초과로 생략: {fn.file}::{fn.name})")
            continue
        used += len(t)
        out.append(t)
    return "<관련_소스_발췌>\n" + "\n\n".join(out) + "\n</관련_소스_발췌>" if out else ""


# ---------------------------------------------------------------------------
# 결정적 추적 후보 (요구 값 ↔ 매크로 값, 신호명 ↔ 식별자)
# ---------------------------------------------------------------------------

_NUM = re.compile(r"(?<![\w.])(\d+(?:\.\d+)?)\s*(ms|밀리초|s|sec|초|분|min|mv|v|%|회)?", re.I)


def _tokens(name: str) -> set:
    parts = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", name).replace("_", " ").split()
    return {p.lower() for p in parts if len(p) > 1 and not p.isdigit()}


def trace_hints(run: Run, req: dict) -> list[str]:
    m = run.model
    values: dict = {}

    def add_num(text: str, why: str):
        for mm in _NUM.finditer(text or ""):
            v = float(mm.group(1))
            unit = (mm.group(2) or "").lower()
            vals = {v}
            ms = {"ms": v, "밀리초": v, "s": v * 1000, "sec": v * 1000, "초": v * 1000, "분": v * 60000,
                  "min": v * 60000}.get(unit)
            if unit == "v":
                vals.add(v * 1000)
            if ms is not None:
                for ctx in run.env.contexts or []:
                    if ctx.period_ms:
                        vals.add(ms / ctx.period_ms)
                periods = [float(x) for x in re.findall(r"(\d+)\s*ms\s*주기", "\n".join(run.spec.lines))]
                for p in periods:
                    vals.add(ms / p)
            for x in vals:
                if x == int(x):
                    values.setdefault(int(x), []).append(f"{why} '{mm.group(0).strip()}'")

    for key in ("thresholds_boundaries", "timers_counters", "decision_conditions", "execution_conditions"):
        for e in req.get(key, []):
            add_num(" ".join(str(e.get(k, "")) for k in ("value", "unit", "detail", "quote", "behavior")), key)
    hints = []
    for mac in m.macros.values():
        if isinstance(mac.numeric, (int, float)) and mac.numeric in values and mac.numeric not in (0, 1):
            uses = [(fn, c.line, c.raw) for fn in m.functions.values() for c in fn.conditions if mac.name in c.macros]
            uses += [(fn, a.line, a.raw) for fn in m.functions.values() for a in fn.assigns
                     if re.search(rf"\b{mac.name}\b", a.raw)]
            why = values[mac.numeric][0]
            hints.append(f"매크로 {mac.name}={mac.numeric} ({mac.file}:{mac.line}) ↔ 요구 값 {why}")
            for fn, line, raw in uses[:4]:
                hints.append(f"  · {fn.file}:{line} {fn.name}(): {raw}")
    names = []
    for key in ("inputs", "outputs", "states"):
        names += [e.get("name", "") for e in req.get(key, [])]
    for n in names:
        toks = _tokens(n)
        if not toks:
            continue
        for v in m.variables.values():
            vt = _tokens(v.name)
            if n.lower() in v.name.lower() or len(toks & vt) >= max(1, min(2, len(toks))):
                hints.append(f"신호 '{n}' ↔ 변수 {v.name} ({v.file}:{v.line})")
        for fn in m.functions.values():
            if n.lower() in fn.name.lower() or (len(toks) > 1 and len(toks & _tokens(fn.name)) >= 2):
                hints.append(f"신호 '{n}' ↔ 함수 {fn.name}() ({fn.file}:{fn.start})")
    seen, out = set(), []
    for h in hints:
        if h not in seen:
            seen.add(h)
            out.append(h)
    return out[:25]


# ---------------------------------------------------------------------------
# 단계 실행
# ---------------------------------------------------------------------------


def _stage_path(run: Run, key: str) -> Path:
    return run.out / "stages" / f"{key}.json"


def _load(run: Run, key: str) -> dict | None:
    p = _stage_path(run, key)
    if run.opts.resume and p.is_file():
        run.say(f"  · 저장된 결과 사용: {p}")
        return json.loads(p.read_text(encoding="utf-8"))
    return None


def _save(run: Run, key: str, data: dict):
    p = _stage_path(run, key)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


def _batches(items: list, size: int):
    for i in range(0, len(items), max(1, size)):
        yield items[i: i + size]


def _renumber(findings: list, prefix: str, start: int = 1) -> dict:
    mapping = {}
    for i, f in enumerate(findings, start):
        new = f"{prefix}-{i:03d}"
        if f.get("id"):
            mapping[f["id"]] = new
        f["id"] = new
    return mapping


def stage1(run: Run) -> dict:
    cached = _load(run, "1_requirements")
    if cached:
        return cached
    run.say("[1/5] 사양 분석: 요구사항 구조화")
    ids = ", ".join(f"{i}(S{n})" for i, n in run.spec.ids) or "(원문에 ID 형식 없음)"
    block = {"type": "text", "text": (
        f"<기능사양서 이름=\"{run.spec.name}\">\n{run.spec.numbered()}\n</기능사양서>\n\n"
        f"<원문_ID_후보 설명=\"정규식으로 찾은 ID 형식 문자열. 요구사항 ID가 아닐 수도 있음\">\n{ids}\n</원문_ID_후보>\n\n"
        + _prompt(run, "stage1_spec.md"))}
    data = run.llm.call("stage1_requirements", _prompt(run, "common.md"), [block], SC.REQUIREMENTS)
    EV.verify_requirements(run.spec, data)
    _save(run, "1_requirements", data)
    return data


def stage2(run: Run, reqs: dict) -> dict:
    cached = _load(run, "2_traces")
    if cached:
        return cached
    run.say("[2/5] 구현 추적")
    shared = _shared_context(run, reqs)
    traces = []
    rl = reqs.get("requirements", [])
    for bi, batch in enumerate(_batches(rl, run.opts.batch_size * 2), 1):
        hints = []
        for r in batch:
            h = trace_hints(run, r)
            hints.append(f"[{r['id']}]\n" + ("\n".join(h) if h else "(후보 없음)"))
        text = (f"<대상_요구사항>{', '.join(r['id'] for r in batch)}</대상_요구사항>\n\n"
                f"<결정적_추적_후보>\n" + "\n\n".join(hints) + "\n</결정적_추적_후보>\n\n" + _prompt(run, "stage2_trace.md"))
        data = run.llm.call(f"stage2_traces_b{bi}", _prompt(run, "common.md"), [shared, {"type": "text", "text": text}],
                            SC.TRACES)
        traces += data.get("traces", [])
    result = {"traces": traces}
    for r in rl:
        if not any(t.get("req_id") == r["id"] for t in traces):
            traces.append({"req_id": r["id"], "status": "TRACE_NOT_FOUND", "locations": [], "call_path": [],
                           "unmatched_elements": [], "search_notes": "추적 단계 응답에 누락됨", "confidence": "LOW"})
    EV.verify_traces(run.model, result)
    _save(run, "2_traces", result)
    return result


def _trace_keys(run: Run, traces: dict, ids: set) -> tuple[set, dict]:
    keys, marks = set(), {}
    for t in traces.get("traces", []):
        if t.get("req_id") not in ids:
            continue
        for loc in t.get("locations", []):
            fn = run.model.function_at(loc.get("file", ""), int(loc.get("line_start") or 0))
            if fn:
                keys.add(fn.key)
                marks.setdefault(fn.file, set()).update(range(int(loc.get("line_start") or 0),
                                                              int(loc.get("line_end") or loc.get("line_start") or 0) + 1))
    return keys, marks


def _trace_json(traces: dict, ids: set) -> str:
    return json.dumps({"traces": [t for t in traces.get("traces", []) if t.get("req_id") in ids]}, ensure_ascii=False)


def stage3(run: Run, reqs: dict, traces: dict) -> dict:
    cached = _load(run, "3_consistency")
    if cached:
        return cached
    run.say("[3/5] 정합성 분석")
    shared = _shared_context(run, reqs)
    results, findings = [], []
    rl = reqs.get("requirements", [])
    for bi, batch in enumerate(_batches(rl, run.opts.batch_size), 1):
        ids = {r["id"] for r in batch}
        keys, marks = _trace_keys(run, traces, ids)
        related = [c for c in run.candidates if any(run.model.function_at(c.file, c.line) is run.model.functions.get(k)
                                                    for k in keys)]
        text = "\n\n".join(x for x in [
            f"<대상_요구사항>{', '.join(sorted(ids))}</대상_요구사항>",
            f"<추적_결과>\n{_trace_json(traces, ids)}\n</추적_결과>",
            f"<정적_분석_후보>\n{K.render_candidates(related)}\n</정적_분석_후보>",
            _excerpts(run, keys, marks),
            _prompt(run, "stage3_consistency.md")] if x)
        data = run.llm.call(f"stage3_consistency_b{bi}", _prompt(run, "common.md"),
                            [shared, {"type": "text", "text": text}], SC.CONSISTENCY)
        mapping = _renumber(data.get("findings", []), "C", len(findings) + 1)
        for rr in data.get("requirement_results", []):
            rr["finding_ids"] = [mapping.get(x, x) for x in rr.get("finding_ids", [])]
        results += data.get("requirement_results", [])
        findings += data.get("findings", [])
    out = {"requirement_results": results, "findings": findings}
    _save(run, "3_consistency", out)
    return out


def _brief_findings(findings: list) -> str:
    return "\n".join(f"- {f['id']} [{f.get('category')}] {', '.join(f.get('req_ids', []))}: {f.get('title')}"
                     for f in findings) or "(없음)"


def stage4(run: Run, reqs: dict, traces: dict, cons: dict) -> dict:
    cached = _load(run, "4_gaps")
    if cached:
        return cached
    run.say("[4/5] 사양 공백 분석")
    shared = _shared_context(run, reqs)
    findings = []
    rl = reqs.get("requirements", [])
    for bi, batch in enumerate(_batches(rl, run.opts.gap_batch_size), 1):
        ids = {r["id"] for r in batch}
        keys, marks = _trace_keys(run, traces, ids)
        seeds = []
        for r in batch:
            for a in r.get("ambiguities", []):
                seeds.append(f"- [{r['id']}] 모호: {a.get('element')} — {a.get('reason')} (질문: {a.get('question')})")
            for u in r.get("undefined_items", []):
                seeds.append(f"- [{r['id']}] 미정의: {u.get('element')} — {u.get('description')}")
        text = "\n\n".join(x for x in [
            f"<대상_요구사항>{', '.join(sorted(ids))}</대상_요구사항>",
            f"<추적_결과>\n{_trace_json(traces, ids)}\n</추적_결과>",
            "<단계1_모호·미정의_항목 설명=\"출발점일 뿐이며 이것만으로 한정하지 말 것\">\n" + ("\n".join(seeds) or "(없음)")
            + "\n</단계1_모호·미정의_항목>",
            f"<정적_분석_후보>\n{K.render_candidates(run.candidates)}\n</정적_분석_후보>",
            f"<단계3_결과>\n{_brief_findings(cons.get('findings', []))}\n</단계3_결과>",
            _excerpts(run, keys, marks),
            _prompt(run, "stage4_gap.md")] if x)
        data = run.llm.call(f"stage4_gaps_b{bi}", _prompt(run, "common.md"),
                            [shared, {"type": "text", "text": text}], SC.GAPS)
        _renumber(data.get("findings", []), "G", len(findings) + 1)
        findings += data.get("findings", [])
    out = {"findings": findings}
    _save(run, "4_gaps", out)
    return out


def stage5(run: Run, reqs: dict, cons: dict, gaps: dict) -> dict:
    cached = _load(run, "5_defects")
    if cached:
        return cached
    run.say("[5/5] 코드 결함 분석")
    shared = _shared_context(run, reqs)
    reviews, findings = [], []
    cands = run.candidates or []
    batches = list(_batches(cands, run.opts.candidate_batch)) or [[]]
    for bi, batch in enumerate(batches, 1):
        keys = {fn.key for c in batch for fn in [run.model.function_at(c.file, c.line)] if fn}
        marks = {}
        for c in batch:
            marks.setdefault(c.file, set()).add(c.line)
        text = "\n\n".join(x for x in [
            f"<정적_분석_후보 설명=\"규칙 기반 계산 결과. certainty: CERTAIN=자료형·상수만으로 성립, PROBABLE, POSSIBLE, REVIEW=정보 부족\">\n"
            f"{K.render_candidates(batch)}\n</정적_분석_후보>",
            "<외부_도구>\n" + ("\n".join(run.tool_notes) or "사용 안 함") + "\n</외부_도구>",
            f"<단계3_결과>\n{_brief_findings(cons.get('findings', []))}\n</단계3_결과>",
            f"<단계4_결과>\n{_brief_findings(gaps.get('findings', []))}\n</단계4_결과>",
            _excerpts(run, keys, marks),
            _prompt(run, "stage5_defect.md")] if x)
        data = run.llm.call(f"stage5_defects_b{bi}", _prompt(run, "common.md"),
                            [shared, {"type": "text", "text": text}], SC.DEFECTS)
        mapping = _renumber(data.get("findings", []), "D", len(findings) + 1)
        for rv in data.get("candidate_reviews", []):
            rv["finding_id"] = mapping.get(rv.get("finding_id", ""), rv.get("finding_id", ""))
        reviews += data.get("candidate_reviews", [])
        findings += data.get("findings", [])
    out = {"candidate_reviews": reviews, "findings": findings}
    _save(run, "5_defects", out)
    return out


def run_pipeline(run: Run) -> dict:
    """LLM 단계 실행 → 근거 검증·가드레일 → 보고서용 결과 dict"""
    stop = run.opts.stop_after
    reqs = stage1(run)
    traces = stage2(run, reqs) if stop >= 2 else {"traces": []}
    cons = stage3(run, reqs, traces) if stop >= 3 else {"requirement_results": [], "findings": []}
    gaps = stage4(run, reqs, traces, cons) if stop >= 4 else {"findings": []}
    defs = stage5(run, reqs, cons, gaps) if stop >= 5 else {"candidate_reviews": [], "findings": []}
    findings = []
    for stage, data in (("3_consistency", cons), ("4_gaps", gaps), ("5_defects", defs)):
        for f in data.get("findings", []):
            f["stage"] = stage
            findings.append(f)
    EV.apply_guardrails(run.model, findings, traces, run.candidates)
    EV.link_related(findings)
    return {"requirements": reqs, "traces": traces, "requirement_results": cons.get("requirement_results", []),
            "findings": findings, "candidate_reviews": defs.get("candidate_reviews", [])}
