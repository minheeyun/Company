"""6단계(a): 변경에 따른 잠재 결함 분석.

정적 분석으로 확정 가능한 항목(예: 자료형 범위 밖 상수와의 비교로 조건이 항상 거짓)은
CONFIRMED_BY_STATIC_ANALYSIS, 그 외는 NEEDS_VERIFICATION(추가 검증 필요)으로 분류한다.
"""

from __future__ import annotations

from .ast_extract import expr_text, iter_stmts, kbase
from .cfg import ENTRY
from .roles import comparisons, iter_expr, key_name, lvalue_key
from .valuerange import Env, eval_cond, expr_interval, type_range

CONFIRMED = "CONFIRMED_BY_STATIC_ANALYSIS"
VERIFY = "NEEDS_VERIFICATION"

CATS = {"LOGIC": "조건 및 논리", "ARITH": "자료형 및 연산", "STATE": "상태 및 타이머", "CONC": "공유 자원 및 동시성",
        "FLOW": "제어 흐름"}


class DefectAnalyzer:
    def __init__(self, base, curr, changes, impact, cg, roles, config, cfg_cmp: dict):
        self.b, self.p, self.cs, self.ia, self.cg = base, curr, changes, impact, cg
        self.roles, self.cfg, self.cfg_cmp = roles, config, cfg_cmp
        self.items: list[dict] = []
        self._seen = set()

    def add(self, cat, typ, sev, status, fid, line, code, desc, evidence, change=None, rec=None):
        k = (typ, fid, line, desc)
        if k in self._seen:
            return
        self._seen.add(k)
        fn = self.p.functions.get(fid) if fid else None
        self.items.append({"id": f"R{len(self.items) + 1:03d}", "category": CATS[cat], "type": typ, "severity": sev,
                           "status": status, "function": fid, "file": fn["file"] if fn else None, "line": line,
                           "code": code, "description": desc, "evidence": evidence, "change": change,
                           "recommendation": rec or ""})

    def run(self):
        for ch in self.cs.changes:
            self._from_change(ch)
        self._from_impacted_nodes()
        self._from_cfg()
        self._concurrency()
        sev = {"High": 0, "Medium": 1, "Low": 2}
        self.items.sort(key=lambda x: (sev[x["severity"]], x["status"] != CONFIRMED, x["id"]))
        for i, it in enumerate(self.items):
            it["id"] = f"R{i + 1:03d}"
        return self.items

    # ------------------------------------------------------- 변경점 기반
    def _sev(self, ch, base="Medium"):
        t = set(ch["types"])
        if t & {"STATE_TRANSITION_CHANGED", "FAULT_LOGIC_CHANGED", "OUTPUT_LOGIC_CHANGED", "TIMER_COUNTER_CHANGED"}:
            return "High"
        return base

    def _from_change(self, ch):
        t = set(ch["types"])
        fid = ch.get("function")
        cur = ch.get("curr") or {}
        line, code = cur.get("line"), cur.get("code")
        d = ch.get("detail") or {}
        cid = ch["id"]
        if "BOUNDARY_OPERATOR" in t:
            ops = ", ".join(f"{o['from']} → {o['to']}" for o in d.get("operators", []))
            self.add("LOGIC", "BOUNDARY_OPERATOR_CHANGE", self._sev(ch, "High"), VERIFY, fid, line, code,
                     f"경계 연산자 변경({ops}): 피연산자가 임계값과 같을 때 분기 결과가 반전됨",
                     d.get("from", "") + "  →  " + d.get("to", ""), cid,
                     "임계값 정확히 같은 입력에서 요구사항상 기대 동작을 확인(경계값 테스트)")
        if "LOGICAL_OPERATOR" in t:
            self.add("LOGIC", "LOGICAL_OPERATOR_CHANGE", "High", VERIFY, fid, line, code,
                     "AND/OR 변경: 조건이 성립하는 입력 조합이 크게 달라짐",
                     d.get("from", "") + "  →  " + d.get("to", ""), cid, "각 항의 참/거짓 조합(MC/DC) 테스트")
        if "CONDITION_TERM_REMOVED" in t:
            self.add("LOGIC", "GUARD_REMOVED", "High", VERIFY, fid, line, code,
                     "조건 항 삭제: 기존 보호/예외 조건이 우회될 수 있음 "
                     f"(삭제된 항: {', '.join(d.get('terms_removed', [])) or '구조 변경'})",
                     d.get("from", "") + "  →  " + d.get("to", ""), cid, "삭제된 조건이 거짓인 상황에서의 동작 확인")
        if "CONDITION_TERM_ADDED" in t:
            self.add("LOGIC", "CONDITION_ADDED", "Medium", VERIFY, fid, line, code,
                     "조건 항 추가: 기존에 실행되던 경로가 새 조건에 의해 차단될 수 있음 "
                     f"(추가된 항: {', '.join(d.get('terms_added', [])) or '구조 변경'})",
                     d.get("from", "") + "  →  " + d.get("to", ""), cid, "추가된 조건이 거짓인 기존 정상 시나리오 확인")
        if t & {"PRECEDENCE_CHANGED", "NEGATION_CHANGED"}:
            self.add("LOGIC", "PRECEDENCE_OR_NEGATION_CHANGE", "High", VERIFY, fid, line, code,
                     "조건 우선순위(괄호) 또는 부정 변경: 진리표 전체가 달라질 수 있음",
                     d.get("from", "") + "  →  " + d.get("to", ""), cid, "진리표 비교 및 MC/DC 테스트")
        if "BRANCH_ORDER_CHANGED" in t:
            self.add("LOGIC", "BRANCH_PRIORITY_CHANGE", "High", VERIFY, fid, line, code,
                     "else-if 평가 순서 변경: 여러 조건이 동시에 참일 때 선택되는 분기가 달라짐",
                     " / ".join(d.get("order_from", [])) + "  →  " + " / ".join(d.get("order_to", [])), cid,
                     "조건이 겹치는 입력으로 분기 우선순위 확인")
        if t & {"ELSE_REMOVED", "BRANCH_REMOVED"} and ch.get("base"):
            self.add("FLOW", "BRANCH_PATH_REMOVED", self._sev(ch, "High"), VERIFY, fid, ch["base"].get("line"),
                     ch["base"].get("code"), "기존 분기(예외 처리/복구 경로 포함 가능)가 삭제됨",
                     ch["description"], cid, "삭제된 경로가 처리하던 입력에서의 동작 확인")
        if "THRESHOLD_CHANGED" in t or "CONDITION_CHANGED" in t:
            self._threshold(ch)
        if "TIMER_COUNTER_CHANGED" in t:
            self._timer(ch)
        if "STATE_TRANSITION_CHANGED" in t:
            self._state(ch)
        if "FAULT_LOGIC_CHANGED" in t:
            self.add("STATE", "FAULT_DETECTION_LOGIC_CHANGE", "High", VERIFY, fid, line, code,
                     "고장 감지/해제 관련 로직 변경", ch["description"], cid, "고장 발생·유지·해제 시퀀스 테스트")
        if "GLOBAL_TYPE_CHANGED" in t:
            self._type_change(ch)
        if "GLOBAL_INIT_CHANGED" in t:
            v = ch.get("variable")
            sev = "High" if v and (self.roles.is_state(v) or self.roles.is_counter(v)) else "Medium"
            self.add("STATE", "INITIALIZATION_CHANGE", sev, VERIFY, None, line, code,
                     f"전역변수 {key_name(v or '')} 초기값 변경 ({d.get('init_from')} → {d.get('init_to')})",
                     ch["description"], cid, "리셋 직후 첫 주기 동작 확인")
        if "MACRO_CHANGED" in t:
            self._macro(ch)
        if fid and fid in self.p.functions and "init" in fid.lower() and t & {"ASSIGNMENT_CHANGED", "ASSIGNMENT_REMOVED",
                                                                             "CONSTANT_CHANGED"}:
            self.add("STATE", "INIT_FUNCTION_CHANGE", "Medium", VERIFY, fid, line, code,
                     "초기화 함수의 대입 변경: 잘못된 초기화 가능성", ch["description"], cid, "초기화 직후 상태 확인")
        if "INACTIVE_CODE_CHANGED" in t:
            self.add("FLOW", "INACTIVE_BUILD_VARIANT_CODE", "Medium", VERIFY, None, line, code,
                     "현재 빌드 설정에서 비활성인 코드 변경 — 다른 빌드 설정(매크로 정의)으로 별도 분석 필요",
                     ch["description"], cid, "해당 매크로를 정의한 빌드 설정(--variant)으로 재분석")
        if "UNMAPPED_CHANGE" in t or "UNPARSED_FILE_CHANGE" in t:
            self.add("FLOW", "UNANALYZED_CHANGE", "Medium", VERIFY, None, line, code,
                     "AST 요소로 해석되지 않은 변경 — 수동 리뷰 필요", ch["description"], cid)

    def _threshold(self, ch):
        """임계값/연산자 변경 후 비교가 자료형 범위상 항상 참/거짓이 되는지 확인."""
        fid = ch.get("function")
        sid = ch.get("curr_stmt")
        if fid not in self.p.functions or sid is None:
            return
        st = self.p.stmt_index(fid).get(sid)
        if not st or not st.get("cond"):
            return
        self._const_compare(fid, st, ch["id"])

    def _const_compare(self, fid, st, cid):
        plat = self.cfg.platform
        for c in comparisons(st["cond"]):
            lhs = c["lhs"]
            tr = type_range(lhs.get("t"), plat)
            if tr is None or c["const"] is None:
                continue
            r = eval_cond(Env(), {"k": "bin", "op": c["op"], "l": lhs, "r": {"k": "const", "v": c["const"]}}, plat)
            if r is True or r is False:
                var = expr_text(lhs)
                self.add("ARITH", "CONSTANT_OUT_OF_TYPE_RANGE", "High", CONFIRMED, fid, st["line"], st["text"],
                         f"'{var} {c['op']} {c['const_text']}'({c['const']}) 는 {var} 의 자료형 "
                         f"{lhs.get('t', {}).get('spelling')} 범위 [{tr[0]}, {tr[1]}] 에서 항상 "
                         f"{'참' if r else '거짓'} — {'else' if r else 'then'} 경로 도달 불가",
                         f"type_range={tr}, op={c['op']}, const={c['const']}", cid,
                         "임계값과 변수 자료형(범위)을 함께 재검토")

    def _type_change(self, ch):
        v = ch.get("variable")
        d = ch.get("detail") or {}
        g = self.p.globals.get(v[2:]) if v else None
        bg = self.b.globals.get(v[2:]) if v else None
        if not g:
            return
        nb, ob = g["type"].get("bits"), bg["type"].get("bits") if bg else None
        if nb and ob and nb < ob:
            self.add("ARITH", "TYPE_NARROWED", "High", VERIFY, None, g["line"], ch["curr"]["code"] if ch.get("curr") else "",
                     f"{g['name']} 자료형 축소 {d.get('type_from')}({ob}bit) → {d.get('type_to')}({nb}bit): "
                     "값 손실·순환 가능", ch["description"], ch["id"], "최대값 근처 입력과 사용 위치 전수 확인")
        # 사용 위치의 비교/증가 재검사
        for fid, fn in self.p.functions.items():
            for st in iter_stmts(fn["body"]):
                keys = st.get("uses", []) + st.get("defs", [])
                if not any(k == v or k.startswith(v + ".") for k in keys):
                    continue
                if st.get("cond"):
                    self._const_compare(fid, st, ch["id"])
                if v in st.get("rmw", []):
                    self._counter_wrap(fid, st, v, ch["id"])

    def _counter_wrap(self, fid, st, key, cid):
        info = self.p.var_info(key, fid)
        t = (info or {}).get("type", {})
        tr = type_range(t, self.cfg.platform)
        if not tr:
            return
        e = st.get("expr") or {}
        inc = any(x.get("k") == "un" and x["op"] == "++" for x in iter_expr(e)) or any(
            x.get("k") == "assign" and x["op"] == "+=" for x in iter_expr(e))
        dec = any(x.get("k") == "un" and x["op"] == "--" for x in iter_expr(e)) or any(
            x.get("k") == "assign" and x["op"] == "-=" for x in iter_expr(e))
        g = self.ia.cfg_of(fid)
        guards = []
        for b, lab in g.control_deps().get(st["id"], []):
            bst = g.stmts.get(b)
            if bst and bst.get("cond"):
                for c in comparisons(bst["cond"]):
                    if lvalue_key(c["lhs"]) == key:
                        guards.append((c["op"], c["const"], lab))
        # 같은 변수를 상수와 비교하는 위치(함수 전체)
        limits = []
        for s2 in iter_stmts(self.p.functions[fid]["body"]):
            if s2.get("cond"):
                for c in comparisons(s2["cond"]):
                    if lvalue_key(c["lhs"]) == key and c["const"] is not None:
                        limits.append((c["op"], c["const"], s2["line"]))
        name = key_name(key)
        if inc and not guards:
            unreachable = [x for x in limits if x[0] in (">=", ">", "==") and x[1] > tr[1]]
            if unreachable:
                op, c, ln = unreachable[0]
                self.add("STATE", "COUNTER_THRESHOLD_UNREACHABLE", "High", CONFIRMED, fid, st["line"], st["text"],
                         f"카운터 {name}({t.get('spelling')}, 최대 {tr[1]})가 임계값 {c}(L{ln}, '{op}')에 도달할 수 없고 "
                         f"{tr[1]} 다음 0으로 순환 — 판정(고장 확정 등)이 발생하지 않음",
                         f"증가 문장에 상한 가드 없음, 비교 상수 {c} > 자료형 최대 {tr[1]}", cid,
                         "카운터 자료형 확대 또는 임계값/포화 처리 재검토")
            else:
                self.add("ARITH", "COUNTER_WRAP", "Medium", VERIFY, fid, st["line"], st["text"],
                         f"카운터 {name}({t.get('spelling')}) 증가에 상한 가드가 없어 {tr[1]} 이후 순환 가능",
                         f"제어 의존 조건 중 {name} 상한 비교 없음", cid, "최대값 도달 시나리오 테스트")
        if dec and not guards and tr[0] == 0:
            self.add("ARITH", "COUNTER_UNDERFLOW", "Medium", VERIFY, fid, st["line"], st["text"],
                     f"부호 없는 카운터 {name} 감소에 하한(>0) 가드가 없어 0에서 {tr[1]} 로 순환 가능",
                     "제어 의존 조건 중 하한 비교 없음", cid, "0 상태에서 감소 시나리오 테스트")

    def _timer(self, ch):
        fid = ch.get("function")
        d = ch.get("detail") or {}
        cur = ch.get("curr") or {}
        msgs = []
        for th in d.get("thresholds", []):
            msgs.append(f"{th['var']}: 만료/판정 임계값 {th['from']} → {th['to']} ({th['op']})")
        for op in d.get("operators", []):
            if {op["from"], op["to"]} in ({">", ">="}, {"<", "<="}):
                msgs.append(f"경계 연산자 {op['from']} → {op['to']}: 만료 시점 1주기 차이")
        period = self._period_of(fid) if fid else None
        if period and d.get("thresholds"):
            th = d["thresholds"][0]
            try:
                msgs.append(f"주기 {period}ms 기준 약 {th['from'] * period:g}ms → {th['to'] * period:g}ms")
            except TypeError:
                pass
        if fid:
            sid = ch.get("curr_stmt")
            st = self.p.stmt_index(fid).get(sid) if fid in self.p.functions and sid is not None else None
            if st is not None:
                for k in st.get("rmw", []):
                    self._counter_wrap(fid, st, k, ch["id"])
        self.add("STATE", "TIMER_COUNTER_LOGIC_CHANGE", "High", VERIFY, fid, cur.get("line"), cur.get("code"),
                 "타이머/카운터 로직 변경" + (": " + "; ".join(msgs) if msgs else ""), ch["description"], ch["id"],
                 "만료 직전/직후 주기, 리셋 조건, 최대값 도달 시나리오 테스트")

    def _period_of(self, fid):
        tasks = {t.name: t for t in self.cfg.tasks}
        for ep in [fid, *self.cg.upward(fid).keys()]:
            fn = self.p.functions.get(ep)
            if fn and fn["name"] in tasks and tasks[fn["name"]].period_ms:
                return tasks[fn["name"]].period_ms
        return None

    def _state(self, ch):
        fid = ch.get("function")
        d = ch.get("detail") or {}
        cur = ch.get("curr") or {}
        if d.get("transitions_removed"):
            rem = ", ".join(f"{a} → {b}" for a, b in d["transitions_removed"])
            add = ", ".join(f"{a} → {b}" for a, b in d.get("transitions_added", []))
            self.add("STATE", "STATE_TRANSITION_REMOVED", "High", VERIFY, fid, cur.get("line"), None,
                     f"상태 전이 삭제/변경: [{rem}]" + (f", 추가: [{add}]" if add else ""),
                     f"상태 변수 {d.get('variable')}", ch["id"], "삭제된 전이의 대체 경로와 복귀 조건 확인")
            # 더 이상 도달하지 않는 상태
            fb = self.b.functions.get(fid)
            fc = self.p.functions.get(fid)
            if fb and fc:
                from .change_detect import state_transitions
                tb = state_transitions(fb, self.roles)
                tc = state_transitions(fc, self.roles)
                for var in tc:
                    targets_b = {b for _, b in tb.get(var, set())}
                    targets_c = {b for _, b in tc.get(var, set())}
                    for lost in sorted(targets_b - targets_c):
                        self.add("STATE", "STATE_NO_LONGER_ENTERED", "High", VERIFY, fid, cur.get("line"), None,
                                 f"변경 후 함수 {fc['name']} 안에서 상태 {lost} 로 진입하는 대입이 사라짐",
                                 f"변경 전 진입 전이 {sorted(a for a, b in tb[var] if b == lost)}", ch["id"])
        elif d.get("transitions_added"):
            add = ", ".join(f"{a} → {b}" for a, b in d["transitions_added"])
            self.add("STATE", "STATE_TRANSITION_ADDED", "Medium", VERIFY, fid, cur.get("line"), None,
                     f"상태 전이 추가: [{add}]", f"상태 변수 {d.get('variable')}", ch["id"])
        elif ch.get("curr_stmt") is not None:
            self.add("STATE", "STATE_LOGIC_CHANGE", "High", VERIFY, fid, cur.get("line"), cur.get("code"),
                     "상태 변수 갱신/분기 로직 변경", ch["description"], ch["id"], "변경 전후 상태 전이표 비교")

    def _macro(self, ch):
        d = ch.get("detail") or {}
        try:
            a = int(str(d.get("from")).strip("() ").rstrip("uUlL"), 0)
            b = int(str(d.get("to")).strip("() ").rstrip("uUlL"), 0)
        except (TypeError, ValueError):
            a = b = None
        # 매크로 사용 문장에서 범위 초과 비교 재검사
        for fid, fn in self.p.functions.items():
            for mu in fn.get("macro_uses", []):
                if mu["name"] == ch.get("macro_name") and mu.get("stmt") is not None:
                    st = self.p.stmt_index(fid).get(mu["stmt"])
                    if st and st.get("cond"):
                        self._const_compare(fid, st, ch["id"])
        if a is not None and b is not None:
            self.add("LOGIC", "THRESHOLD_MACRO_CHANGE", "Medium", VERIFY, None, (ch.get("curr") or {}).get("line"),
                     (ch.get("curr") or {}).get("code"),
                     f"매크로 {ch.get('macro_name')} 값 {a} → {b}: 이 값을 쓰는 모든 비교·계산의 동작 범위 변경",
                     ch["description"], ch["id"], f"{min(a, b) - 1}..{max(a, b) + 1} 구간 경계값 테스트")

    # -------------------------------------------- 영향 받은 문장 검사
    def _from_impacted_nodes(self):
        plat = self.cfg.platform
        for (fid, n), f in self.ia.best.items():
            if n == ENTRY or f.level not in ("DIRECT", "INDIRECT"):
                continue
            st = self.p.stmt_index(fid).get(n)
            if not st:
                continue
            e = st.get("expr") or st.get("cond")
            if not e:
                continue
            direct = f.level == "DIRECT"
            for x in iter_expr(e):
                k = x.get("k")
                if k == "bin" and x["op"] in ("/", "%"):
                    r = x["r"]
                    if r.get("k") != "const":
                        dk = lvalue_key(r)
                        if dk and not self._guarded_nonzero(fid, n, dk):
                            self.add("ARITH", "DIVISION_BY_ZERO", "Medium", VERIFY, fid, st["line"], st["text"],
                                     f"나눗셈 제수 '{expr_text(r)}' 가 0 인지 확인하는 가드가 없음(영향 받은 문장)",
                                     f"영향 경로 상 문장, 수준 {f.level}", f.change)
                    elif r.get("v") == 0:
                        self.add("ARITH", "DIVISION_BY_ZERO", "High", CONFIRMED, fid, st["line"], st["text"],
                                 "상수 0 으로 나눔", "", f.change)
                if k == "index" and x.get("len"):
                    idx = x["idx"]
                    n_len = x["len"]
                    if idx.get("k") == "const" and idx.get("v") is not None and idx["v"] >= n_len:
                        self.add("ARITH", "ARRAY_INDEX_OUT_OF_RANGE", "High", CONFIRMED, fid, st["line"], st["text"],
                                 f"상수 인덱스 {idx['v']} ≥ 배열 길이 {n_len}", "", f.change)
                    else:
                        ik = lvalue_key(idx)
                        bound = self._index_bound(fid, n, ik) if ik else None
                        if bound is not None and bound >= n_len:
                            self.add("ARITH", "ARRAY_INDEX_OUT_OF_RANGE", "High", CONFIRMED, fid, st["line"],
                                     st["text"], f"가드 조건상 인덱스 최대 {bound} ≥ 배열 길이 {n_len}",
                                     "제어 의존 조건의 상한", f.change)
                        elif ik and bound is None and direct:
                            tr = type_range(idx.get("t"), plat)
                            if not tr or tr[1] >= n_len:
                                self.add("ARITH", "ARRAY_INDEX_UNCHECKED", "Medium", VERIFY, fid, st["line"],
                                         st["text"], f"인덱스 '{expr_text(idx)}' 상한 가드 없음(배열 길이 {n_len})",
                                         "", f.change)
                if k == "assign" and direct:
                    self._narrowing(fid, st, x, f.change)
                if k == "cast" and x.get("explicit") and direct:
                    tb = x.get("t", {}).get("bits")
                    sb = _expr_bits(x["e"])
                    iv = expr_interval(x["e"], plat)
                    trc = type_range(x.get("t"), plat)
                    fits = bool(iv and trc and trc[0] <= iv[0] and iv[1] <= trc[1])
                    if tb and sb and tb < sb and x["e"].get("k") != "const" and not fits:
                        self.add("ARITH", "EXPLICIT_NARROWING_CAST", "Low", VERIFY, fid, st["line"], st["text"],
                                 f"명시적 형변환 ({x['t'].get('spelling')}) 으로 {sb}bit → {tb}bit 값 손실 가능",
                                 expr_text(x), f.change)
            for k in st.get("rmw", []):
                if direct and (self.roles.is_counter(k, fid) or k.startswith("G:")):
                    self._counter_wrap(fid, st, k, f.change)

    def _narrowing(self, fid, st, x, cid):
        lt = x["l"].get("t", {})
        lb = lt.get("bits")
        r = x["r"]
        if not lb or lt.get("kind") != "int":
            return
        if r.get("k") == "const" and r.get("v") is not None:
            tr = type_range(lt, self.cfg.platform)
            if tr and not (tr[0] <= r["v"] <= tr[1]):
                self.add("ARITH", "CONSTANT_TRUNCATION", "High", CONFIRMED, fid, st["line"], st["text"],
                         f"상수 {r['v']} 가 {lt.get('spelling')} 범위 {tr} 를 벗어남 — 잘림", "", cid)
            return
        tr = type_range(lt, self.cfg.platform)
        iv = expr_interval(r, self.cfg.platform)
        if iv and tr and tr[0] <= iv[0] and iv[1] <= tr[1]:
            return  # 값 구간이 대상 자료형 안에 들어감
        rb = _expr_bits(r)
        if iv and tr:
            self.add("ARITH", "IMPLICIT_NARROWING" if not (r.get("k") == "cast" and r.get("explicit"))
                     else "EXPLICIT_NARROWING_CAST", "Medium", VERIFY, fid, st["line"], st["text"],
                     f"식의 값 구간 [{iv[0]}, {iv[1]}] 이 {lt.get('spelling')} 범위 [{tr[0]}, {tr[1]}] 를 벗어날 수 있음"
                     " — 오버플로/값 손실 가능", expr_text(x), cid, "입력 최대/최소 조합 테스트")
            return
        if rb and rb > lb and not (r.get("k") == "cast" and r.get("explicit")):
            self.add("ARITH", "IMPLICIT_NARROWING", "Medium", VERIFY, fid, st["line"], st["text"],
                     f"{rb}bit 식을 {lb}bit {lt.get('spelling')} 에 대입 — 값 손실 가능", expr_text(x), cid)

    def _guarded_nonzero(self, fid, n, key):
        g = self.ia.cfg_of(fid)
        for b, lab in g.control_deps().get(n, []):
            bst = g.stmts.get(b)
            if not bst or not bst.get("cond"):
                continue
            for c in comparisons(bst["cond"]):
                if lvalue_key(c["lhs"]) == key and c["const"] == 0 and (
                        (c["op"] in ("!=", ">") and lab == "T") or (c["op"] == "==" and lab == "F")):
                    return True
            if lvalue_key(bst["cond"]) == key and lab == "T":
                return True
        return False

    def _index_bound(self, fid, n, key):
        g = self.ia.cfg_of(fid)
        best = None
        for b, lab in g.control_deps().get(n, []):
            bst = g.stmts.get(b)
            if not bst or not bst.get("cond"):
                continue
            for c in comparisons(bst["cond"]):
                if lvalue_key(c["lhs"]) != key or c["const"] is None:
                    continue
                op = c["op"] if lab == "T" else {"<": ">=", "<=": ">", ">": "<=", ">=": "<"}.get(c["op"])
                if op == "<":
                    hb = c["const"] - 1
                elif op == "<=":
                    hb = c["const"]
                else:
                    continue
                best = hb if best is None else min(best, hb)
        return best

    # ------------------------------------------------------ CFG 기반
    def _from_cfg(self):
        for fid, cmpr in self.cfg_cmp.items():
            for u in cmpr.get("unreachable_curr", []):
                self.add("FLOW", "UNREACHABLE_CODE", "Medium", CONFIRMED, fid, u["line"], u["text"],
                         "변경 후 CFG 에서 진입 경로가 없는 문장", "ENTRY 로부터 도달 불가", None)
            for x in cmpr.get("static_dead_branches_curr", []):
                if any(d["type"] == "CONSTANT_OUT_OF_TYPE_RANGE" and d["function"] == fid and d["line"] == x["line"]
                       for d in self.items):
                    continue
                self.add("FLOW", "DEAD_BRANCH", "High", CONFIRMED, fid, x["line"], x["text"],
                         f"자료형 범위상 조건이 항상 {'참' if x['always'] else '거짓'} — "
                         f"{'else' if x['always'] else 'then'} 경로 도달 불가 (변경 후 새로 발생)",
                         "구간 추론", None)
            if cmpr["curr"]["truncated"]:
                self.add("FLOW", "PATH_ENUMERATION_TRUNCATED", "Low", VERIFY, fid, None, None,
                         f"경로 수가 한도({self.cfg.max_paths})를 넘어 일부 경로만 비교함", "", None)

    # ------------------------------------------------------- 동시성
    def _concurrency(self):
        tasks = {t.name: t for t in self.cfg.tasks}
        if not tasks:
            return
        ctx = {}
        for t in self.cfg.tasks:
            fid = next((f for f, fn in self.p.functions.items() if fn["name"] == t.name), None)
            if fid is None:
                continue
            ctx[t.name] = (t, {fid, *self.cg.downward(fid).keys()})
        # 영향 받은 문장이 접근하는 전역
        touched: dict[str, list] = {}
        for (fid, n), f in self.ia.best.items():
            if n == ENTRY or f.level not in ("DIRECT", "INDIRECT"):
                continue
            st = self.p.stmt_index(fid).get(n)
            if not st:
                continue
            for k in st.get("defs", []) + st.get("uses", []):
                if k.startswith("G:"):
                    touched.setdefault(kbase(k), []).append((fid, st, f))
        plat = self.cfg.platform
        for key, sites in touched.items():
            gid = key[2:]
            g = self.p.globals.get(gid) or self.p.extern_vars.get(gid)
            if not g or g["type"].get("const"):
                continue
            access = {}
            for fid, fn in self.p.functions.items():
                for st in iter_stmts(fn["body"]):
                    w = any(kbase(d) == key and not d.startswith("*") for d in st.get("defs", []))
                    r = any(kbase(u) == key and not u.startswith(("*", "&")) for u in st.get("uses", []))
                    if w or r:
                        for tn, (t, funcs) in ctx.items():
                            if fid in funcs:
                                a = access.setdefault(tn, {"r": False, "w": False, "rmw": False, "sites": []})
                                a["r"] |= r
                                a["w"] |= w
                                a["rmw"] |= key in [kbase(x) for x in st.get("rmw", [])]
                                a["sites"].append(f"{fn['name']}:L{st['line']}")
            if len(access) < 2 or not any(a["w"] for a in access.values()):
                continue
            kinds = {tasks[t].kind for t in access}
            prios = {tasks[t].priority for t in access}
            if "isr" not in kinds and len(prios) < 2:
                continue
            issues = []
            if "isr" in kinds and not g["type"].get("volatile"):
                issues.append("ISR 와 공유되지만 volatile 아님")
            bits = g["type"].get("bits") or 0
            if bits > plat.atomic_access_bits or g["type"].get("kind") in ("record", "array"):
                issues.append(f"{bits}bit/복합 자료형 — 대상 MCU({plat.atomic_access_bits}bit 원자 접근)에서 비원자적 접근")
            if any(a["rmw"] for a in access.values()):
                issues.append("read-modify-write(++/+=) 연산 — 선점 시 갱신 손실 가능")
            if not issues:
                issues.append("서로 다른 우선순위 문맥에서 읽기/쓰기 — 실행 순서에 따른 값 차이 가능")
            fid, st, f = sites[0]
            sev = "High" if ("isr" in kinds and len(issues) > 0) or any("비원자" in i for i in issues) else "Medium"
            self.add("CONC", "SHARED_VARIABLE_RACE", sev, VERIFY, fid, st["line"], st["text"],
                     f"공유변수 {g['name']}: " + "; ".join(issues),
                     "접근 문맥: " + "; ".join(f"{t}({tasks[t].kind},p{tasks[t].priority})"
                                           f"[{'W' if a['w'] else ''}{'R' if a['r'] else ''}]: "
                                           f"{', '.join(a['sites'][:4])}" for t, a in access.items()),
                     f.change, "인터럽트 금지/임계 구역 또는 스냅샷 복사 적용 여부 리뷰, 선점 타이밍 테스트")


def _expr_bits(e):
    t = (e or {}).get("t") or {}
    if t.get("kind") == "int":
        return t.get("bits")
    return None
