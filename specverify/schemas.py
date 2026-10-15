"""단계별 출력 JSON 스키마 (Claude structured outputs: output_config.format).

structured outputs 제약: 모든 object에 additionalProperties=false, 재귀/수치 제약 미지원.
"""

from __future__ import annotations

STATUS = ["DEFINED", "AMBIGUOUS", "UNDEFINED"]
SEVERITY = ["HIGH", "MEDIUM", "LOW", "INFO"]
VERDICT = ["CONFIRMED_DEFECT", "PROBABLE_DEFECT", "POTENTIAL_RISK", "NEEDS_REVIEW", "SPEC_CLARIFICATION"]
CATEGORY = [
    # ③ 정합성
    "MISSING_IMPLEMENTATION", "CONDITION_MISMATCH", "THRESHOLD_BOUNDARY_MISMATCH", "TIMER_COUNTER_MISMATCH",
    "STATE_TRANSITION_MISMATCH", "PRIORITY_MISMATCH", "EXCEPTION_HANDLING_MISMATCH", "INIT_RECOVERY_MISMATCH",
    # ④ 사양 공백
    "SPEC_GAP", "AMBIGUOUS",
    # ⑤ 코드 결함
    "RUNTIME_ERROR", "LOGIC_ERROR", "CONCURRENCY",
]
GAP_KIND = [
    "NONE", "INPUT_COMBINATION", "INVALID_OR_OUT_OF_RANGE_INPUT", "BOUNDARY_VALUE", "TIMEOUT_TIMER_EXPIRY",
    "COUNTER_LIMIT_WRAP", "SIMULTANEOUS_CONDITIONS_PRIORITY", "UNEXPECTED_EVENT_DURING_TRANSITION",
    "INITIAL_OR_ABNORMAL_STATE", "RECOVERY_REENTRY", "OTHER",
]
TECHNIQUE = ["REQUIREMENT_BASED", "BOUNDARY_VALUE", "EQUIVALENCE_CLASS", "STATE_TRANSITION", "FAULT_INJECTION",
             "TIMING", "CONCURRENCY", "REGRESSION"]


def obj(props: dict, required: list | None = None) -> dict:
    return {"type": "object", "properties": props, "required": list(props) if required is None else required,
            "additionalProperties": False}


def arr(items: dict) -> dict:
    return {"type": "array", "items": items}


S = {"type": "string"}
I = {"type": "integer"}


def enum(values) -> dict:
    return {"type": "string", "enum": list(values)}


ELEMENT = obj({"name": S, "detail": S, "value": S, "unit": S, "status": enum(STATUS), "quote": S})

REQUIREMENTS = obj({
    "requirements": arr(obj({
        "id": S,
        "id_origin": enum(["SPEC", "GENERATED"]),
        "title": S,
        "source": obj({"line_start": I, "line_end": I, "quote": S}),
        "purpose": S,
        "inputs": arr(ELEMENT),
        "outputs": arr(ELEMENT),
        "execution_conditions": arr(ELEMENT),
        "decision_conditions": arr(ELEMENT),
        "states": arr(ELEMENT),
        "transitions": arr(obj({"from": S, "to": S, "condition": S, "status": enum(STATUS), "quote": S})),
        "timers_counters": arr(obj({"name": S, "kind": enum(["TIMER", "COUNTER", "DEBOUNCE", "DELAY", "OTHER"]),
                                    "value": S, "unit": S, "behavior": S, "status": enum(STATUS), "quote": S})),
        "thresholds_boundaries": arr(obj({"item": S, "value": S, "unit": S,
                                          "inclusive": enum(["INCLUSIVE", "EXCLUSIVE", "UNSPECIFIED"]),
                                          "status": enum(STATUS), "quote": S})),
        "exceptions": arr(ELEMENT),
        "priorities": arr(ELEMENT),
        "ambiguities": arr(obj({"element": S, "quote": S, "reason": S, "question": S})),
        "undefined_items": arr(obj({"element": S, "description": S, "question": S})),
    })),
    "glossary": arr(obj({"term": S, "definition": S, "line": I})),
    "document_notes": arr(S),
})

TRACES = obj({
    "traces": arr(obj({
        "req_id": S,
        "status": enum(["TRACED", "PARTIAL", "TRACE_NOT_FOUND"]),
        "locations": arr(obj({
            "file": S, "function": S, "line_start": I, "line_end": I,
            "role": enum(["INPUT_READ", "CONDITION", "THRESHOLD", "TIMER_COUNTER", "STATE_TRANSITION",
                          "OUTPUT_WRITE", "INIT", "EXCEPTION_HANDLING", "CALL_PATH", "OTHER"]),
            "element": S, "symbol": S, "explanation": S,
        })),
        "call_path": arr(S),
        "unmatched_elements": arr(S),
        "search_notes": S,
        "confidence": enum(["HIGH", "MEDIUM", "LOW"]),
    })),
})

CODE_REF = obj({"file": S, "function": S, "line": I, "symbol": S, "role": S})

FINDING = obj({
    "id": S,
    "category": enum(CATEGORY),
    "subcategory": S,
    "verdict": enum(VERDICT),
    "severity": enum(SEVERITY),
    "title": S,
    "req_ids": arr(S),
    "req_quote": S,
    "code_refs": arr(CODE_REF),
    "spec_says": S,
    "code_does": S,
    "comparison": S,
    "trigger_condition": S,
    "execution_path": arr(S),
    "expected_behavior": S,
    "inferred_behavior": S,
    "basis": arr(obj({"kind": enum(["FACT", "INFERENCE", "HYPOTHESIS"]), "statement": S, "evidence": S})),
    "uncertainty": S,
    "recommendation": S,
    "additional_checks": arr(S),
    "test_cases": arr(obj({"title": S, "technique": enum(TECHNIQUE), "preconditions": S, "steps": S,
                           "expected": S, "observation": S})),
    "static_candidates": arr(S),
    "gap_kind": enum(GAP_KIND),
    "spec_question": S,
    "risk_scenario": S,
})

CONSISTENCY = obj({
    "requirement_results": arr(obj({
        "req_id": S,
        "verdict": enum(["CONSISTENT", "INCONSISTENT", "PARTIALLY_CONSISTENT", "CANNOT_DETERMINE"]),
        "summary": S,
        "finding_ids": arr(S),
    })),
    "findings": arr(FINDING),
})

GAPS = obj({"findings": arr(FINDING)})

DEFECTS = obj({
    "candidate_reviews": arr(obj({
        "candidate_id": S,
        "assessment": enum(["CONFIRMED", "PROBABLE", "POTENTIAL", "LIKELY_FALSE_POSITIVE", "NEEDS_INFO"]),
        "reason": S,
        "finding_id": S,
    })),
    "findings": arr(FINDING),
})
