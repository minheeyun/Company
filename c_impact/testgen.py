"""6단계(b): SW 검증 관점 회귀 테스트 대상 도출.

기대 결과(expected)는 요구사항 매핑에서 확인되는 경우에만 채운다. 그렇지 않으면
'사양 확인 필요'로 두고, 코드에서 계산되는 변경 전/후 동작(code_behavior)은 별도 필드로 제시한다.
"""

from __future__ import annotations

from .ast_extract import iter_stmts
from .roles import comparisons, key_name, lvalue_key
from .valuerange import type_range

UNKNOWN = "사양 확인 필요 — 요구사항/기능사양서로 기대 결과 확정 후 판정"


def _cmp(v, op, c):
    return {"<": v < c, "<=": v <= c, ">": v > c, ">=": v >= c, "==": v == c, "!=": v != c}[op]


class TestGenerator:
    def __init__(self, base, curr, changes, impact, summary, per_change, cg, roles, config, defects, cfg_cmp):
        self.b, self.p, self.cs, self.ia = base, curr, changes, impact
        self.summary, self.per_change, self.cg, self.roles = summary, per_change, cg, roles
        self.cfg, self.defects, self.cfg_cmp = config, defects, cfg_cmp
        self.tests: list[dict] = []

    # --------------------------------------------------------------- 보조
    def _entry(self, fid):
        chains = self.cg.chains_to_entries(fid, limit=3)
        return [" → ".join(key_name(x) for x in c["chain"]) for c in chains]

    def _inputs(self, fid):
        if fid not in self.p.functions:
            return []
        fn = self.p.functions[fid]
        df = self.ia.dfa(fid)
        ins = [f"인자 {p['name']} ({p['type']['spelling']})" for p in fn["params"]]
        for k in sorted(df.upward):
            if k.startswith("G:"):
                info = self.p.var_info(k, fid) or {}
                ins.append(f"전역 {key_name(k)} ({(info.get('type') or {}).get('spelling', '?')})")
            elif k.startswith("*P:"):
                ins.append(f"포인터 인자 대상 *{key_name(k)}")
        stubs = sorted({c["name"] for c in fn["calls"] if c.get("external")})
        if stubs:
            ins.append("스텁/관측 대상 외부 호출: " + ", ".join(stubs))
        return ins

    def _reqs(self, funcs):
        out = []
        for rid, r in (self.cfg.requirements or {}).items():
            rf = set(r.get("functions", []))
            if rf & {key_name(f) for f in funcs} or rf & set(funcs):
                out.append({"id": rid, "title": r.get("title", ""), "expected": r.get("expected")})
        return out

    def _existing(self, funcs):
        out = []
        names = {key_name(f) for f in funcs} | set(funcs)
        for tid, t in (self.cfg.existing_tests or {}).items():
            if set(t.get("functions", [])) & names:
                out.append({"id": tid, "description": t.get("description", ""), "type": t.get("type", "")})
        return out

    def add(self, kind, priority, purpose, targets, preconditions, scenario, reason, changes, inputs=None,
            code_behavior=None, path=None, expected_code=None):
        reqs = self._reqs(targets)
        exp = [r["expected"] for r in reqs if r.get("expected")]
        t = {
            "id": None, "kind": kind, "priority": priority, "purpose": purpose,
            "target_functions": [key_name(x) for x in targets], "target_ids": targets,
            "drive_from": self._entry(targets[0]) if targets and targets[0] in self.p.functions else [],
            "inputs": inputs if inputs is not None else (self._inputs(targets[0]) if targets else []),
            "preconditions": preconditions, "scenario": scenario,
            "expected": " / ".join(exp) if exp else UNKNOWN,
            "expected_source": "requirement" if exp else "unknown",
            "code_behavior": code_behavior, "code_expected_hint": expected_code,
            "reason": reason, "related_changes": changes, "impact_path": path or [],
            "requirements": reqs, "existing_tests": self._existing(targets),
        }
        self.tests.append(t)
        return t

    # -------------------------------------------------------------- 생성
    def run(self):
        self._direct()
        self._boundaries()
        self._states()
        self._counters()
        self._outputs()
        self._impacted()
        self._error_paths()
        self._concurrency()
        self._potential()
        pr = {"High": 0, "Medium": 1, "Low": 2}
        self.tests.sort(key=lambda t: (pr[t["priority"]], t["kind"]))
        for i, t in enumerate(self.tests):
            t["id"] = f"T{i + 1:03d}"
        return self.tests

    def _direct(self):
        by_fn: dict[str, list] = {}
        for ch in self.cs.changes:
            fid = ch.get("function")
            if fid and fid in self.p.functions and ch.get("semantic", True) and not ch.get("summary_only"):
                by_fn.setdefault(fid, []).append(ch)
        for fid, chs in by_fn.items():
            fn = self.p.functions[fid]
            lines = sorted({(c.get("curr") or {}).get("line") for c in chs if c.get("curr")} - {None})
            pr = "High" if any(set(c["types"]) & {"CONDITION_CHANGED", "STATE_TRANSITION_CHANGED",
                                                  "TIMER_COUNTER_CHANGED", "OUTPUT_LOGIC_CHANGED",
                                                  "FAULT_LOGIC_CHANGED"} for c in chs) else "Medium"
            self.add("DIRECT_FUNCTION", pr, f"변경 함수 {fn['name']} 직접(단위) 테스트 — 변경 문장 커버리지 확보",
                     [fid], ["변경 문장을 포함하는 분기 조건을 만족하도록 입력/전역 설정"],
                     [f"변경 라인 {', '.join(map(str, lines))} 를 실행하는 입력으로 {fn['name']}() 호출",
                      "변경 전/후 분기 커버리지(문장·분기·MC/DC) 확인",
                      "출력(반환값/전역/외부 호출 인자) 기록 후 변경 전 결과와 비교"],
                     "; ".join(c["description"] for c in chs)[:400], [c["id"] for c in chs])
        # 함수 본문은 그대로지만 변경된 정의(매크로·전역·자료형·시그니처)를 직접 사용하는 함수
        for fid, ent in self.summary["functions"].items():
            if fid in by_fn or ent["level"] != "DIRECT" or fid not in self.p.functions:
                continue
            nodes = [n for n in ent.get("nodes", []) if n["edge"] in ("MACRO", "GLOBAL_DEF", "TYPE", "SIGNATURE", "SEED")]
            if not nodes:
                continue
            self.add("DIRECT_FUNCTION", "High",
                     f"{ent['name']} 직접 테스트 — 변경된 정의를 사용하는 문장 확인",
                     [fid], ["해당 문장에 도달하는 입력/전역 설정"],
                     [f"L{n['line']} '{n['code'][:60]}' 실행 ({n['reason'][:50]})" for n in nodes[:6]] +
                     ["변경 전/후 정의 값에서 결과 비교"],
                     "함수 코드는 같지만 사용하는 매크로/전역/자료형 정의가 바뀜", sorted(ent["changes"]))

    def _boundaries(self):
        for ch in self.cs.changes:
            d = ch.get("detail") or {}
            fid = ch.get("function")
            pairs = []
            if d.get("comparisons_to"):
                cf = {(c["var"]): c for c in d.get("comparisons_from", [])}
                for c in d["comparisons_to"]:
                    o = cf.get(c["var"])
                    if o and (o["op"] != c["op"] or o["value"] != c["value"]):
                        pairs.append((c["var"], o, c, fid, (ch.get("curr") or {}).get("code")))
            if "MACRO_CHANGED" in ch["types"]:
                pairs += self._macro_cmps(ch)
            for var, o, c, f2, code in pairs:
                if o["value"] is None or c["value"] is None:
                    continue
                tr = type_range(c.get("type"), self.cfg.platform)
                vals = sorted({v for base in (o["value"], c["value"]) for v in (base - 1, base, base + 1)
                               if tr is None or tr[0] <= v <= tr[1]})
                rows = []
                for v in vals:
                    before, after = _cmp(v, o["op"], o["value"]), _cmp(v, c["op"], c["value"])
                    rows.append({"value": v, "before": before, "after": after, "changed": before != after})
                if not rows:
                    continue
                f2 = f2 or ch.get("function")
                targets = [f2] if f2 else []
                self.add("BOUNDARY", "High",
                         f"'{var}' 경계값 테스트: 변경 전 '{var} {o['op']} {o['value']}' / 변경 후 '{var} {c['op']} {c['value']}'",
                         targets, [f"{var} 외 조건 항은 참이 되도록 고정"],
                         [f"{var} = {r['value']} 입력 → 분기 결과 관찰" for r in rows],
                         "조건의 경계 연산자 또는 임계값이 바뀌어 일부 입력 값에서 분기 결과가 달라짐",
                         [ch["id"]], code_behavior=rows,
                         expected_code="코드 기준: changed=True 인 값에서 변경 전후 분기 결과가 다름 (의도된 변경인지 사양 확인)")

    def _macro_cmps(self, ch):
        out = []
        d = ch.get("detail") or {}
        try:
            old = int(str(d.get("from")).strip("() ").rstrip("uUlL"), 0)
        except (TypeError, ValueError):
            return out
        for fid, fn in self.p.functions.items():
            for mu in fn.get("macro_uses", []):
                if mu["name"] != ch.get("macro_name") or mu.get("stmt") is None:
                    continue
                st = self.p.stmt_index(fid).get(mu["stmt"])
                if not st or not st.get("cond"):
                    continue
                for c in comparisons(st["cond"]):
                    if ch["macro_name"] in (c.get("const_text") or ""):
                        lk = lvalue_key(c["lhs"])
                        var = key_name(lk) if lk else "?"
                        o = {"var": var, "op": c["op"], "value": old}
                        n = {"var": var, "op": c["op"], "value": c["const"], "type": c["lhs"].get("t", {})}
                        out.append((var, o, n, fid, st["text"]))
        return out

    def _states(self):
        for ch in self.cs.changes:
            if "STATE_TRANSITION_CHANGED" not in ch["types"]:
                continue
            d = ch.get("detail") or {}
            fid = ch.get("function")
            if d.get("transitions_added") or d.get("transitions_removed"):
                rows = [{"from": a, "to_after": b, "kind": "추가"} for a, b in d.get("transitions_added", [])] + \
                       [{"from": a, "to_before": b, "kind": "삭제"} for a, b in d.get("transitions_removed", [])]
                self.add("STATE_TRANSITION", "High", f"상태 변수 {d.get('variable')} 전이 테스트 (변경된 전이)",
                         [fid] if fid else [],
                         [f"{d.get('variable')} = {r['from']} 로 설정" for r in rows],
                         ["전이 조건을 만족시키는 입력 인가 후 1주기 실행", "다음 상태 관찰", "복귀/재진입 경로 확인"],
                         "상태 전이 집합이 변경됨 — 전이 누락/추가가 의도된 것인지 확인", [ch["id"]],
                         code_behavior=rows,
                         expected_code="코드 기준 변경 후 전이: " + ", ".join(
                             f"{a} → {b}" for a, b in d.get("transitions_added", [])))
            elif ch.get("curr_stmt") is not None and fid:
                self.add("STATE_TRANSITION", "High", "상태 갱신 로직 변경 테스트", [fid],
                         ["변경 문장에 도달하는 상태로 설정"], ["변경 문장 실행 전후 상태 변수 값 비교"],
                         ch["description"], [ch["id"]])

    def _counters(self):
        for ch in self.cs.changes:
            if "TIMER_COUNTER_CHANGED" not in ch["types"]:
                continue
            var = ch.get("variable")
            fid = ch.get("function")
            keys = [var] if var else []
            if not keys and fid and ch.get("curr_stmt") is not None:
                st = self.p.stmt_index(fid).get(ch["curr_stmt"]) or {}
                keys = [k for k in st.get("defs", []) + st.get("uses", []) if self.roles.is_counter(k, fid)]
            for k in keys:
                info = self.p.var_info(k, fid) or {}
                tr = type_range(info.get("type"), self.cfg.platform)
                users = sorted({f for f, fn in self.p.functions.items() for st in iter_stmts(fn["body"])
                                if k in st.get("defs", []) + st.get("uses", [])})
                limits = []
                for f in users:
                    for st in iter_stmts(self.p.functions[f]["body"]):
                        if st.get("cond"):
                            for c in comparisons(st["cond"]):
                                if lvalue_key(c["lhs"]) == k:
                                    limits.append(f"{c['op']} {c['const_text']}({c['const']})")
                steps = []
                if tr:
                    steps += [f"카운터를 {tr[1] - 1}, {tr[1]} 까지 증가시킨 뒤 1회 더 증가 → 순환(0) 여부 관찰"]
                steps += [f"판정 비교 {', '.join(sorted(set(limits))) or '(없음)'} 의 직전/도달/직후 주기 관찰",
                          "리셋 조건 성립 시 0 으로 초기화되는지 확인", "판정 중 조건이 해제되는 경우(디바운스 중단) 확인"]
                self.add("TIMER_COUNTER", "High", f"타이머/카운터 {key_name(k)} 테스트",
                         users or ([fid] if fid else []), [f"{key_name(k)} = 0 (초기화 후)"], steps,
                         f"카운터 자료형/증감/비교 로직 변경 ({ch['description']})", [ch["id"]],
                         code_behavior={"type": (info.get("type") or {}).get("spelling"), "range": tr,
                                        "comparisons": sorted(set(limits))})

    def _outputs(self):
        for fid, cmpr in self.cfg_cmp.items():
            for rc in cmpr.get("reach_condition_changes", []):
                st = self.p.stmt_index(fid).get(rc["node"])
                if not st:
                    continue
                if "OUTPUT" not in self.roles.stmt_roles(fid, st) and not self.roles.calls_output(fid, st) \
                        and not any(self.roles.is_state(d) for d in st.get("defs", [])):
                    continue
                self.add("OUTPUT_CONDITION", "High",
                         f"출력/상태 갱신 '{rc['text'][:60]}' 도달 조건 테스트", [fid],
                         ["각 도달 조건(변경 후)을 만족하는 입력 구성"],
                         [f"조건 [{' ∧ '.join(c)}] → L{rc['line']} 실행 확인" for c in rc["curr_conditions"][:6]] +
                         [f"(변경 전 조건: {' | '.join(' ∧ '.join(c) for c in (rc['base_conditions'] or [])[:3]) or '없음(새 문장)'})"],
                         "변경 후 해당 출력 문장에 도달하는 경로 조건이 달라짐", [], code_behavior=rc)

    def _impacted(self):
        funcs = self.summary["functions"]
        for fid, ent in funcs.items():
            if ent["level"] not in ("INDIRECT",) or ent.get("call_context_only") or fid not in self.p.functions:
                continue
            nodes = ent.get("nodes", [])
            edges = sorted(ent["edges"])
            roles = sorted({r for n in nodes for r in n.get("roles", [])})
            pr = "High" if set(roles) & {"OUTPUT", "STATE", "FAULT"} else "Medium"
            first = ent["facts"][0] if ent.get("facts") else None
            path = self.ia.path_of(first) if first else []
            self.add("IMPACTED_FUNCTION", pr,
                     f"영향 함수 {ent['name']} 회귀 테스트 (전파: {', '.join(edges)})", [fid],
                     ["변경 함수가 먼저 실행되어 공유 데이터/인자/반환값이 변경 후 값이 되도록 시퀀스 구성"],
                     [f"L{n['line']} '{n['code'][:60]}' 결과 관찰 ({n['reason'][:60]})" for n in nodes[:5]],
                     "직접 호출 관계가 없더라도 데이터/제어 의존으로 변경 영향이 도달함" if "GLOBAL" in edges
                     else "호출 인자·반환값·호출 조건을 통해 변경 영향이 도달함",
                     sorted(ent["changes"]), path=[f"{p['name']}:L{p['line']} [{p['edge_label']}]" for p in path])

    def _error_paths(self):
        for ch in self.cs.changes:
            t = set(ch["types"])
            fid = ch.get("function")
            if not fid or fid not in self.p.functions:
                continue
            code = ((ch.get("curr") or ch.get("base") or {}).get("code") or "")
            if t & {"FAULT_LOGIC_CHANGED", "ELSE_REMOVED", "BRANCH_REMOVED"} or self.cfg.matches("recovery", code) \
                    or self.cfg.matches("fault_var", code):
                self.add("ERROR_RECOVERY", "High", f"{key_name(fid)} 오류 발생·유지·복구 시나리오 테스트", [fid],
                         ["정상 상태에서 시작"],
                         ["비정상 입력(센서 범위 밖/고장 조건) 인가 → 고장 감지 시점 확인",
                          "고장 유지 중 출력/상태 확인", "정상 입력 복귀 → 해제/복구 시점 확인",
                          "경계 근처에서 반복 진입/이탈(채터링) 확인"],
                         "고장 감지/해제 또는 예외 처리 경로가 변경됨", [ch["id"]])
        # 정상/비정상 입력 조합: 변경된 조건의 항별
        for ch in self.cs.changes:
            d = ch.get("detail") or {}
            terms = d.get("comparisons_to") or []
            if "CONDITION_CHANGED" in ch["types"] and len(terms) >= 2:
                self.add("INPUT_COMBINATION", "Medium", "변경 조건의 입력 조합(MC/DC) 테스트",
                         [ch["function"]], ["조건 항 이외 입력 고정"],
                         [f"항 '{c['var']} {c['op']} {c['value']}' 만 참/거짓을 바꾸는 입력 쌍" for c in terms],
                         "복합 조건 변경 — 각 항의 독립 영향 확인", [ch["id"]])

    def _concurrency(self):
        for r in self.defects:
            if r["category"] != "공유 자원 및 동시성":
                continue
            self.add("CONCURRENCY", r["severity"], f"공유변수 경쟁 조건 테스트: {r['description'][:60]}",
                     [r["function"]] if r.get("function") else [],
                     ["태스크/ISR 실행 주기 및 우선순위를 실제와 동일하게 구성(HIL 또는 시뮬레이터)"],
                     ["공유변수 갱신 도중 ISR/상위 태스크 선점을 유도(타이밍 주입)", "읽은 값의 일관성(찢어진 읽기/갱신 손실) 확인",
                      "장시간 반복 실행 스트레스"], r["evidence"], [r["change"]] if r.get("change") else [])

    def _potential(self):
        for fid, ent in self.summary["functions"].items():
            if ent["level"] != "POTENTIAL" or fid not in self.p.functions:
                continue
            reason = ent.get("reason") or ", ".join(sorted(ent["edges"]))
            self.add("POTENTIAL_REVIEW", "Low", f"{ent['name']}: 불확실한 영향 경로 확인(리뷰 또는 통합 테스트)", [fid],
                     ["간접 호출 대상/포인터 별칭/비활성 코드 여부를 리뷰로 먼저 확인"],
                     ["실제 실행 시 해당 경로(함수 포인터 대상 등)가 선택되는 조건을 만들어 실행", "출력 변화 유무 관찰"],
                     f"정적 분석만으로 영향 여부 확정 불가: {reason}", sorted(ent["changes"]))
