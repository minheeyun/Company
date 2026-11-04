"""변경 기반 보안 취약점 분석: CPG · Taint Analysis · Symbolic Execution 을 하나의 파이프라인으로 묶는다.

  1. 변경점        diff (AST Diff)                         어떤 함수·조건·변수·상수가 바뀌었나
  2. CPG           program + slicer (AST·CFG·CDG·DDG·호출)  변경이 어떤 실행 경로·데이터에 영향을 주나
                   → 변경 영향이 닿는 위험 연산 후보 (taint.find_risks)
  3. Taint         cpg.CPG.taint (규칙의 Source → 데이터 흐름)  외부 입력이 그 위험 연산의 피연산자에 도달하나
  4. Symbolic      symexec.verify (z3, 후보에만)               안전 속성을 어기는 입력(반례)이 실제로 있나
  5. 변경 전후 비교 같은 위험 연산을 변경 전 코드에서도 판정       신규 발생 · 기존 존재 · 해결 · 판정 보류
  6. 보안 조건 변경 규칙의 권한·실패 횟수·무결성 검사 조건, 외부 입력 검증 조건이 바뀌거나 삭제됨

결과 항목에는 CWE 유형, 등급(High/Medium/Low), 상태, 영향 함수, 발생 조건, 근거 경로(변경 → 위험 연산,
Source → 위험 연산), 기호 실행 반례, 검증 방법이 들어간다. 판정은 정적 분석 근거이며 '재현 확인 필요' 상태로 본다.
"""

from __future__ import annotations

import time

from .cexpr import norm
from .cpg import CPG
from .diff import Change
from .program import Program, display_path
from .rules import Rules
from .slicer import Hit, path_to
from . import symexec
from .taint import CANDIDATE, CERTAIN, KIND_LABEL, KIND_RISK, _Env, _judge, _reads, find_risks, sinks_of

CWE_NAME = {
    "CWE-787": "Out-of-bounds Write (범위 밖 쓰기)",
    "CWE-125": "Out-of-bounds Read (범위 밖 읽기)",
    "CWE-190": "Integer Overflow or Wraparound (정수 넘침)",
    "CWE-197": "Numeric Truncation Error (값 잘림)",
    "CWE-369": "Divide By Zero (0으로 나누기)",
    "CWE-1335": "Incorrect Bitwise Shift of Integer (잘못된 시프트)",
    "CWE-476": "NULL Pointer Dereference (NULL 역참조)",
    "CWE-822": "Untrusted Pointer Dereference (신뢰할 수 없는 포인터)",
    "CWE-834": "Excessive Iteration (과도한 반복)",
    "CWE-20": "Improper Input Validation (입력 검증 부적절)",
    "CWE-252": "Unchecked Return Value (반환값 미검사)",
    "CWE-862": "Missing Authorization (권한 확인 누락)",
    "CWE-307": "Improper Restriction of Excessive Authentication Attempts (인증 시도 제한 부적절)",
    "CWE-354": "Improper Validation of Integrity Check Value (무결성 검증 부적절)",
    "CWE-840": "Business Logic Errors (보안 상태 전이 오류)",
}
STATUS = {"NEW_REGRESSION": "신규 발생", "EXISTING": "기존 존재", "FIXED": "해결", "PENDING": "판정 보류"}
STATE_KO = {"violated": "위반 확인", "holds": "안전 확인", "unknown": "판정 불가", "absent": "해당 연산 없음"}
_LEVEL_ORDER = {"High": 0, "Medium": 1, "Low": 2}
_STATUS_ORDER = {"NEW_REGRESSION": 0, "PENDING": 1, "EXISTING": 2, "FIXED": 3}


def cwe_of(sk, external: bool) -> str:
    k = sk.kind
    if k == "index":
        return "CWE-787" if getattr(sk, "write", False) else "CWE-125"
    if k == "length":
        return "CWE-787"
    if k == "div":
        return "CWE-369"
    if k == "shift":
        return "CWE-1335"
    if k == "narrow":
        from pycparser import c_ast
        op = sk.operand
        while isinstance(op, c_ast.Cast):
            op = op.expr
        return "CWE-190" if isinstance(op, c_ast.BinaryOp) and op.op in ("+", "-", "*", "<<") else "CWE-197"
    if k == "deref":
        return "CWE-822" if external else "CWE-476"
    if k == "loop":
        return "CWE-834"
    if k == "fptr":
        return "CWE-822"
    return ""


class _Budget:
    def __init__(self, seconds: float):
        self.end = time.time() + seconds

    def left(self) -> float:
        return max(0.0, self.end - time.time())


def _state(prog: Program, nid: int, sk, seeds: set[int], symbolic: bool, budget: _Budget, sym_paths: int):
    """(상태, 근거, 기호 실행 결과). 상태: violated | holds | unknown."""
    from .analysis import _guards

    n = prog.nodes[nid]
    E = _Env(prog, n)
    verdict, ev = _judge(E, prog, sk, _guards(prog, nid), seeds)
    if verdict == "safe":
        return "holds", ev or "감싸는 조건·자료형 범위로 안전", None
    sym = None
    if symbolic and sk.kind in symexec.SUPPORTED and budget.left() > 0.5:
        sym = symexec.verify(prog, nid, sk, max_paths=sym_paths, budget_s=min(10.0, budget.left()))
    if sym is not None and sym["result"] in ("violated", "holds"):
        return sym["result"], ev, sym
    return ("violated" if verdict == "certain" else "unknown"), ev, sym


def _bounds(prog: Program, nid: int, sk, seeds: set[int]) -> dict:
    """위험 연산의 한계(배열·버퍼 크기 등)와, 감싸는 조건·자료형이 허용하는 피연산자 범위."""
    from .analysis import _guards
    from .diff import INF, fmt_set
    from .taint import _allowed, _strip
    from .cexpr import gen

    E = _Env(prog, prog.nodes[nid])
    limit = {"index": f"배열 크기 {sk.dim}" if sk.dim is not None else "배열 크기 모름",
             "length": f"버퍼 {sk.dim}바이트" if sk.dim is not None else "버퍼 크기 모름",
             "shift": f"{sk.width or 32}비트", "div": "0 이면 안 됨",
             "narrow": f"대상 범위 {sk.target[0]}..{sk.target[1]}" if sk.target else ""}.get(sk.kind, "")
    if sk.kind in ("deref", "fptr", "loop"):
        return {"limit": limit, "input_range": "", "limit_value": None}
    op = sk.operand
    var = gen(_strip(op))
    c = E.const(op)
    if c is not None:
        rng = f"상수 {c}"
    else:
        r = E.rng(op)
        dom = (r[0], r[1]) if r else (-INF, INF)
        allowed, _, _ = _allowed(prog, _guards(prog, nid), op, dom, seeds)
        if allowed is not None:
            rng = fmt_set(var, allowed, dom)
        elif r:
            rng = f"{var}: 자료형 범위 {int(r[0])}..{int(r[1])} (감싸는 검사 없음)"
        else:
            rng = f"{var}: 범위 모름"
    return {"limit": limit, "input_range": rng, "limit_value": sk.dim if sk.kind in ("index", "length") else None}


def _range_change(new_b: dict | None, old_b: dict | None) -> str:
    if not new_b:
        return ""
    if not old_b:
        return f"{new_b['limit']} · 허용 {new_b['input_range']} (변경 전 없음)"
    parts = []
    if old_b["limit"] != new_b["limit"]:
        parts.append(f"{old_b['limit']} → {new_b['limit']}")
    else:
        parts.append(new_b["limit"])
    if old_b["input_range"] != new_b["input_range"]:
        parts.append(f"허용 {old_b['input_range']} → {new_b['input_range']}")
    elif new_b["input_range"]:
        parts.append(f"허용 {new_b['input_range']} (같음)")
    return " · ".join(p for p in parts if p)


def _path_change(new_flow: bool, old_flow: bool | None) -> str:
    if old_flow is None:
        return "신규 경로 (변경 전에는 이 연산이 없음)" if new_flow else "없음"
    if new_flow and not old_flow:
        return "신규 경로 (변경 전에는 외부 입력이 닿지 않음)"
    if new_flow and old_flow:
        return "기존 경로 유지"
    if old_flow and not new_flow:
        return "경로 사라짐 (변경 전에만 외부 입력이 닿음)"
    return "없음"


def _anchor(sk) -> str:
    """피연산자가 바뀌어도 '같은 연산'으로 볼 기준: 호출 함수·목적지, 배열, 대입 대상 등."""
    from pycparser import c_ast
    e = sk.expr
    if isinstance(e, c_ast.FuncCall):
        args = e.args.exprs if e.args else []
        return f"call:{norm(e.name)}:{norm(args[0]) if args else ''}"
    if isinstance(e, c_ast.ArrayRef):
        return f"index:{norm(e.name)}"
    if isinstance(e, c_ast.BinaryOp):
        return f"{e.op}:{norm(e.left)}"
    if isinstance(e, c_ast.Assignment):
        return f"assign:{norm(e.lvalue)}"
    if isinstance(e, c_ast.Decl):
        return f"decl:{e.name}"
    return f"{sk.kind}:{norm(e)}"


def _counterpart(prog: Program, func_key: str, sk, rules, line: int | None = None):
    """다른 버전의 같은 함수에서 같은 위험 연산: 피연산자가 같은 것 → 같은 대상(호출·배열·대입)인 것 순."""
    f = prog.funcs.get(func_key)
    if f is None:
        return None
    want_op, want_expr, want_anchor = norm(sk.operand), norm(sk.expr), _anchor(sk)
    best = None
    for nid in f.nodes:
        n = prog.nodes[nid]
        if n.dead or not n.visible:
            continue
        for s2 in sinks_of(prog, n, rules):
            if s2.kind != sk.kind:
                continue
            if norm(s2.operand) == want_op:
                score = 0 if norm(s2.expr) == want_expr else 1
            elif _anchor(s2) == want_anchor:
                score = 2
            else:
                continue
            dist = abs(n.line - line) if line is not None else 0
            if best is None or (score, dist) < best[0]:
                best = ((score, dist), nid, s2)
    return None if best is None else (best[1], best[2])


def _status(new_state: str, old_state: str) -> str:
    if new_state == "violated":
        return "EXISTING" if old_state == "violated" else "NEW_REGRESSION"
    if new_state == "unknown":
        return "PENDING"
    return "FIXED" if old_state == "violated" else ""


def _level(state: str, external: bool, sk_kind: str, write: bool) -> str:
    memory = sk_kind == "length" or (sk_kind == "index" and write)
    if state == "violated":
        return "High" if external or memory else "Medium"
    return "Medium" if external else "Low"


def _fmt_cex(cex: dict | None) -> str:
    if not cex:
        return ""
    items = [f"{k} = {v}" for k, v in cex.items()]
    return ", ".join(items[:8]) + (" …" if len(items) > 8 else "")


def _verification(f: dict, change: Change | None) -> str:
    sym = f.get("symbolic") or {}
    prop = sym.get("property") or ""
    if f["state"] == "violated" and sym.get("counterexample"):
        return (f"재현 입력 {_fmt_cex(sym['counterexample'])} 에서 안전 속성 `{prop}` 위반 — 이 입력으로 변경 전/후를 실행해 "
                "재현 여부를 확인하세요 (KLEE·SIL 하네스로 재현 가능).")
    if f["state"] == "violated":
        return f"{f['evidence'] or '상수로 항상 발생'} — 코드 검토로 확인하고 해당 값으로 시험하세요."
    tests = (change.interval or {}).get("tests") if change is not None and change.interval else None
    if tests:
        var = change.interval.get("var")
        return f"경계값 {var} = {', '.join(map(str, tests[:8]))} 로 변경 전/후를 시험하세요."
    if prop:
        return f"안전 속성 `{prop}` 이 모든 호출 경로에서 지켜지는지 호출자의 입력 범위를 확인하세요."
    return "변경 전/후 동작 비교와 코드 검토로 확인하세요."


def analyze_security(old: Program, new: Program, cs, hits_new, hits_old, lines, rules: Rules | None = None, *,
                     symbolic: bool = True, sym_paths: int = 64, sym_budget: float = 120.0, cwe: bool = False) -> dict:
    """cwe=False 이면 결과에서 CWE 분류(cwe, cwe_name)를 뺀다."""
    from .analysis import _Ctx, _rel, _step

    rules = rules or Rules()
    t0 = time.time()
    cpg_new, cpg_old = CPG(new), CPG(old)
    taint_new, taint_old = cpg_new.taint(rules), cpg_old.taint(rules)
    ctx_new = _Ctx(new, hits_new, cs, "new", lines)
    ctx_old = _Ctx(old, hits_old, cs, "old", lines)
    new_rows = find_risks(ctx_new, old, rules)
    old_rows = find_risks(ctx_old, new, rules)
    budget = _Budget(sym_budget)
    use_sym = symbolic and symexec.available()
    findings: list[dict] = []
    matched_old: set[tuple] = set()

    def taint_info(prog, ctx, cpg, taint, nid, sk):
        reads = _reads(prog, sk.expr if sk.kind == "loop" else sk.operand)
        fl = cpg.reachable_by_flows(nid, reads, taint) if reads else None
        if fl is None:
            return {"flow_detected": False}
        d, u, kind = fl
        src = taint.source_of(d)
        hits = path_to(taint.hits, d) + [Hit(nid, d, (kind, display_path(prog, u)), 0)]
        return {"flow_detected": True, "source": taint.sources.get(src, ""), "source_label": taint.labels.get(src, ""),
                "value": display_path(prog, u), "path": [_step(ctx, h) for h in hits]}

    for row in new_rows:
        m, sk, verdict = row.pop("_node"), row.pop("_sink"), row.pop("_verdict")
        origin_node = row.pop("_origin_node")
        state, ev, sym = _state(new, m, sk, cs.seeds_new, use_sym, budget, sym_paths)
        if state == "holds" and verdict != "certain" and sym is None:
            continue
        n = new.nodes[m]
        cp = _counterpart(old, n.func, sk, rules, n.line)
        old_ti, old_b = None, None
        if cp is None:
            old_state, old_ev, old_sym, old_loc = "absent", "", None, ""
        else:
            onid, osk = cp
            old_state, old_ev, old_sym = _state(old, onid, osk, cs.seeds_old, use_sym, budget, sym_paths)
            on = old.nodes[onid]
            old_loc = f"{_rel(old, on.file)}:{on.line}"
            matched_old.add((on.file, on.line, osk.kind, norm(osk.operand)))
            old_ti = taint_info(old, ctx_old, cpg_old, taint_old, onid, osk)
            old_b = _bounds(old, onid, osk, cs.seeds_old)
        new_b = _bounds(new, m, sk, cs.seeds_new)
        status = _status(state, old_state)
        if not status:
            continue  # 변경 후 안전이 증명되고 변경 전에도 문제 없음
        ti = taint_info(new, ctx_new, cpg_new, taint_new, m, sk)
        change = ctx_new.change_by_node.get(origin_node)
        f = dict(row)
        ti["change"] = _path_change(ti["flow_detected"], None if old_ti is None else old_ti["flow_detected"])
        if old_ti is not None and old_ti["flow_detected"]:
            ti["old_source"] = f"{old_ti.get('source')}: {old_ti.get('source_label')}"
        f.update({
            "category": "위험 연산",
            "bounds": {"new": new_b, "old": old_b, "change": _range_change(new_b, old_b)},
            "state": state, "evidence": ev or row["evidence"], "static_rule": verdict == "certain",
            "proof": ("기호 실행 반례" if sym and sym["result"] == "violated" else
                      "기호 실행으로 안전" if sym and sym["result"] == "holds" else
                      "상수·조건 구간 증명" if state == "violated" else ""),
            "cwe": cwe_of(sk, ti["flow_detected"]),
            "taint": ti,
            "symbolic": dict(sym) if sym else None,
            "baseline": {"old_state": old_state, "old_state_label": STATE_KO[old_state], "old_location": old_loc,
                         "old_evidence": old_ev, "old_symbolic": dict(old_sym) if old_sym else None},
            "status_code": status, "status": STATUS[status],
            "level": _level(state, ti["flow_detected"], sk.kind, getattr(sk, "write", False)),
            "change": _change_info(change, new, old) if change else None,
            "cpg": {"affected_sink": row["expr"], "control_dependency_changed": row["how"] == "실행 조건 변경",
                    "data_dependency": row["how"] == "값 전달", "sink_kind": sk.kind},
            "severity": CERTAIN if state == "violated" else CANDIDATE,
        })
        f["cwe_name"] = CWE_NAME.get(f["cwe"], "")
        f["verification"] = _verification(f, change)
        f["_sink"], f["_node"], f["_prog"] = sk, m, "new"
        findings.append(f)

    # 해결: 변경 전에는 위반이었는데 변경 후에는 안전하거나 사라진 연산
    for row in old_rows:
        m, sk = row.pop("_node"), row.pop("_sink")
        row.pop("_verdict")
        row.pop("_origin_node")
        on = old.nodes[m]
        if (on.file, on.line, sk.kind, norm(sk.operand)) in matched_old:
            continue
        old_state, old_ev, old_sym = _state(old, m, sk, cs.seeds_old, use_sym, budget, sym_paths)
        if old_state != "violated":
            continue
        cp = _counterpart(new, on.func, sk, rules, on.line)
        new_state = "absent"
        if cp is not None:
            new_state, _, _ = _state(new, cp[0], cp[1], cs.seeds_new, use_sym, budget, sym_paths)
        if new_state not in ("holds", "absent"):
            continue
        ti = taint_info(old, ctx_old, cpg_old, taint_old, m, sk)
        new_ti, new_b = None, None
        if cp is not None:
            new_ti = taint_info(new, ctx_new, cpg_new, taint_new, cp[0], cp[1])
            new_b = _bounds(new, cp[0], cp[1], cs.seeds_new)
        old_b = _bounds(old, m, sk, cs.seeds_old)
        ti["change"] = _path_change(bool(new_ti and new_ti["flow_detected"]), ti["flow_detected"]) if new_ti else \
            ("경로 사라짐 (변경 후 이 연산이 없음)" if ti["flow_detected"] else "없음")
        f = dict(row)
        f.update({
            "bounds": {"new": new_b, "old": old_b,
                       "change": _range_change(new_b, old_b) if new_b else f"{old_b['limit']} · 허용 {old_b['input_range']} → (연산 없음)"},
            "category": "위험 연산", "state": "holds", "evidence": old_ev or row["evidence"],
            "static_rule": not (old_sym and old_sym.get("result") == "violated"),
            "proof": "변경 후 " + STATE_KO[new_state], "cwe": cwe_of(sk, ti["flow_detected"]), "taint": ti,
            "symbolic": None, "status_code": "FIXED", "status": STATUS["FIXED"], "level": "Low",
            "baseline": {"old_state": "violated", "old_state_label": STATE_KO["violated"],
                         "old_location": f"{row['file']}:{row['line']}", "old_evidence": old_ev,
                         "old_symbolic": dict(old_sym) if old_sym else None},
            "change": None, "cpg": {"affected_sink": row["expr"], "control_dependency_changed": False,
                                    "data_dependency": False, "sink_kind": sk.kind},
            "severity": CANDIDATE, "verification": "변경 후 코드에서 같은 입력으로 문제가 재현되지 않는지 확인하세요.",
        })
        f["cwe_name"] = CWE_NAME.get(f["cwe"], "")
        findings.append(f)

    findings += _check_findings(new, old, cs, ctx_new, ctx_old, cpg_new, cpg_old, taint_new, taint_old, rules, findings)
    findings = _classify(findings, new, old, cs)
    findings.sort(key=_rank)
    for i, f in enumerate(findings, 1):
        f["id"] = f"V{i}"
        if not cwe:
            f.pop("cwe", None)
            f.pop("cwe_name", None)
    counts = {s: sum(1 for f in findings if f["status_code"] == s) for s in STATUS}
    return {
        "findings": findings,
        "summary": {
            "total": len(findings),
            "by_status": {STATUS[k]: v for k, v in counts.items()},
            "by_level": {lv: sum(1 for f in findings if f["level"] == lv) for lv in ("High", "Medium", "Low")},
            "violated": sum(1 for f in findings if f["state"] == "violated"),
        },
        "engine": {
            "cpg": "내장 (AST·CFG·CDG·DDG·호출 그래프)",
            "taint": f"내장 (Source {len(taint_new.sources)}곳, 오염 노드 {len(taint_new.hits)}개)",
            "symbolic": ("z3 " + symexec.z3.get_version_string()) if use_sym else
                        ("꺼짐 (--no-symbolic)" if not symbolic else "z3 없음 — pip install z3-solver"),
            "rules": rules.path or "내장 기본 규칙",
            "elapsed": round(time.time() - t0, 1),
            "sources": sorted({f"{taint_new.sources[n]}: {taint_new.labels[n]}" for n in taint_new.sources})[:200],
        },
        "_cpg_new": cpg_new,
    }


def _change_info(c: Change, new: Program, old: Program) -> dict:
    from .analysis import _rel
    prog = new if c.version == "new" else old
    before, after = (c.other_text, c.text) if c.version == "new" else (c.text, c.other_text)
    if c.kind in ("added", "func_added", "global_added"):
        before = ""
    if c.kind in ("removed", "func_removed", "global_removed"):
        after = ""
    return {"before": before, "after": after, "kind": c.kind, "func": prog.func_display(c.func),
            "file": _rel(prog, c.file), "line": c.line, "detail": c.detail}


def _check_findings(new, old, cs, ctx_new, ctx_old, cpg_new, cpg_old, taint_new, taint_old, rules, findings):
    """보안 검사 조건(권한·실패 횟수·무결성·보안 상태)과 외부 입력 검증 조건의 변경·삭제."""
    from .analysis import _rel, _step

    out = []
    covered = {(f.get("change") or {}).get("file", "") + f":{(f.get('change') or {}).get('line', '')}"
               for f in findings if f.get("taint", {}).get("flow_detected")}
    seen = set()
    for c in cs.changes:
        if c.kind not in ("changed", "added", "moved", "removed"):
            continue
        if c.kind in ("changed", "added", "moved") and c.version != "new":
            continue
        prog, ctx, cpg, taint = (new, ctx_new, cpg_new, taint_new) if c.version == "new" else (old, ctx_old, cpg_old, taint_old)
        n = prog.nodes[c.node]
        if n.kind != "cond":
            continue
        reads = set(n.uses)
        names = " ".join(display_path(prog, u) for u in reads)
        rule = rules.security_check(f"{n.text} {names}")
        fl = cpg.reachable_by_flows(c.node, reads, taint) if reads else None
        loc = f"{_rel(prog, n.file)}:{n.line}"
        if rule is None and fl is None:
            continue
        if rule is None and loc in covered:
            continue  # 같은 변경이 이미 외부 입력 → 위험 연산 항목으로 보고됨
        key = (loc, c.kind)
        if key in seen:
            continue
        seen.add(key)
        removed = c.kind == "removed"
        ti = {"flow_detected": False}
        if fl is not None:
            d, u, kind = fl
            src = taint.source_of(d)
            ti = {"flow_detected": True, "source": taint.sources.get(src, ""), "source_label": taint.labels.get(src, ""),
                  "value": display_path(prog, u),
                  "path": [_step(ctx, h) for h in path_to(taint.hits, d) + [Hit(c.node, d, (kind, display_path(prog, u)), 0)]]}
        # 조건이 외부 값을 '직접' 비교하는지, 함수 반환값(오류 코드)을 검사하는지 구분한다.
        # 반환값 검사 조건의 삭제는 오류 처리 경로 변경이며, 그것만으로 입력 검증·인가 우회로 단정하지 않는다.
        direct_vals = [u for u in reads if "::$out" not in u[0]]
        ret_check = any("::$out" in u[0] for u in reads) and not any(
            fl is not None and u == fl[1] for u in direct_vals)
        if rule is not None:
            cwe, label = rule.get("cwe", "CWE-840"), rule.get("label", rule.get("name", "보안 검사"))
            risk = "보안 검사 우회·약화 가능 (요구사항 확인 필요)"
        elif ret_check:
            cwe, label = "CWE-252", "반환값·오류 검사"
            risk = "오류 처리 경로 변경 — 반환값 확인 생략 시 상태 정리가 되는지 확인 필요 (보안 취약점으로 단정하지 않음)"
        else:
            cwe, label = "CWE-20", "외부 입력 검증"
            risk = "검증되지 않은 외부 입력 허용 범위 변화"
        verb = "삭제" if removed else {"changed": "변경", "added": "추가", "moved": "위치 이동"}[c.kind]
        f = {
            "category": "보안 조건 변경", "kind": "check", "kind_label": f"{label} 조건 {verb}",
            "risk": risk,
            "how": f"조건 {verb}", "func": prog.func_display(n.func), "file": _rel(prog, n.file), "line": n.line,
            "src": ctx.lines.get(n.file, n.line), "expr": n.text, "operand": names, "values": [],
            "evidence": c.detail or ("검사 조건이 삭제됨" if removed else ""),
            "conditions": [f"[{prog.func_display(n.func)}] {c.detail}"] if c.detail else [],
            "path": [_step(ctx, Hit(c.node, None, ("seed", ""), 0))],
            "origin": {"func": prog.func_display(n.func), "file": _rel(prog, n.file), "line": n.line},
            "state": "unknown", "proof": "", "cwe": cwe, "cwe_name": CWE_NAME.get(cwe, ""), "taint": ti, "symbolic": None,
            "baseline": {"old_state": "absent" if c.kind == "added" else "holds",
                         "old_state_label": "변경 전 조건" + ("" if c.kind == "added" else " 존재"),
                         "old_location": "", "old_evidence": "", "old_symbolic": None},
            "status_code": "NEW_REGRESSION" if removed else "PENDING",
            "status": STATUS["NEW_REGRESSION" if removed else "PENDING"],
            "level": "High" if (removed and not ret_check) or (rule and fl) else "Medium",
            "change": _change_info(c, new, old),
            "cpg": {"affected_sink": n.text, "control_dependency_changed": True, "data_dependency": False, "sink_kind": "check"},
            "severity": CANDIDATE,
        }
        f["verification"] = _verification(f, c) if c.interval else \
            ("삭제된 검사 없이도 해당 기능이 허용되지 않는지(권한·세션·실패 횟수) 시험하세요." if removed else
             "바뀐 조건의 참·거짓 경계에서 허용·거부가 의도대로인지 시험하세요.")
        out.append(f)
    return out


VERIFY_LEVELS = ["기호 실행 반례 발견", "정적 규칙 위반", "Taint 경로 발견", "검증 불충분"]
# 다른 함수로 전파된 위험은 명확한 의존 경로가 있을 때만 남긴다: 값이 실제로 전달되었거나(값 전달),
# 변경된 자료형·버퍼 크기 자체를 쓰는 연산(자료형·크기 변경)
_CLEAR_PATH = ("값 전달", "자료형·크기 변경")


def _classify(findings: list[dict], new: Program, old: Program, cs) -> list[dict]:
    """변경 함수와의 관계(직접·전파), 검증 수준, 보안 위험·판정 보류 구분을 붙인다."""
    changed = {new.func_display(k) for k in cs.changed_funcs if k in new.funcs} | \
              {old.func_display(k) for k in cs.changed_funcs if k in old.funcs}
    out = []
    for f in findings:
        direct = f["func"] in changed
        if not direct and f["category"] == "위험 연산" and f.get("how") not in _CLEAR_PATH:
            continue  # 의존 경로가 분명하지 않은 전파 위험은 보고하지 않음
        f["relation"] = "직접" if direct else "전파"
        f["relation_label"] = ("변경 함수 안의 위험 연산" if direct else
                               f"다른 함수로 전파 ({f.get('how', '')} 경로: {f['origin']['func']} → {f['func']})")
        sym = f.get("symbolic") or {}
        old_sym = (f.get("baseline") or {}).get("old_symbolic") or {}
        if f["status_code"] == "FIXED":
            sym = old_sym
        if sym.get("result") == "violated":
            lv = "기호 실행 반례 발견"
        elif f.get("static_rule") and f["state"] in ("violated",) or (f["status_code"] == "FIXED" and f.get("static_rule")):
            lv = "정적 규칙 위반"
        elif (f.get("taint") or {}).get("flow_detected"):
            lv = "Taint 경로 발견"
        else:
            lv = "검증 불충분"
        f["verification_level"] = lv
        f["verification_flags"] = {
            "taint": bool((f.get("taint") or {}).get("flow_detected")),
            "static_rule": bool(f.get("static_rule")) and f["state"] == "violated",
            "symbolic": sym.get("result") or "미실행",
        }
        # ③ 보안 위험 = 안전 속성(검증 조건) 위반이 증명된 것. 그 밖의 후보·조건 변경은 '판정 보류'로 둔다
        f["kind_of_result"] = "보안 위험" if f["state"] == "violated" or f["status_code"] == "FIXED" else "판정 보류"
        out.append(f)
    return out


def _rank(f: dict) -> tuple:
    return (_STATUS_ORDER.get(f["status_code"], 9) if f["status_code"] in ("FIXED", "EXISTING") else 0,
            0 if f.get("relation") == "직접" else 1,
            _LEVEL_ORDER.get(f["level"], 3),
            VERIFY_LEVELS.index(f.get("verification_level", "검증 불충분")),
            _STATUS_ORDER.get(f["status_code"], 9), f["file"], f["line"])


def strip_internal(findings: list[dict]) -> list[dict]:
    for f in findings:
        for k in [k for k in f if k.startswith("_")]:
            f.pop(k, None)
    return findings


__all__ = ["analyze_security", "strip_internal", "cwe_of", "CWE_NAME", "STATUS", "KIND_LABEL", "KIND_RISK"]
