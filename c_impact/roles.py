"""변수·함수의 '역할' 태깅 (상태 변수, 카운터/타이머, 고장 플래그, 출력).

역할은 심볼 참조 분석 결과(대입/비교/switch 구조)를 1차 근거로 하고,
이름 패턴(설정 가능)을 보조 근거로 사용한다. 근거는 evidence 에 남긴다.
"""

from __future__ import annotations

from .ast_extract import expr_children, iter_stmts, kbase, key_parts


def key_name(key: str) -> str:
    """'G:file.c::g_x.f' → 'g_x.f', 'L:i#0' → 'i'."""
    k = key.lstrip("*&")
    if k[:2] in ("G:", "L:", "P:", "F:"):
        k = k[2:]
    k = k.split("::")[-1]
    return k.split("#")[0]


def base_key(key: str) -> str:
    return kbase(key)


def iter_expr(e):
    if not e:
        return
    yield e
    for s in expr_children(e):
        yield from iter_expr(s)
    if e.get("k") == "call" and e.get("callee_e"):
        yield from iter_expr(e["callee_e"])


def comparisons(e) -> list[dict]:
    """조건식 안의 '변수 op 상수' 비교 목록(상수가 왼쪽이면 뒤집어 정규화)."""
    flip = {"<": ">", ">": "<", "<=": ">=", ">=": "<=", "==": "==", "!=": "!="}
    out = []
    for x in iter_expr(e):
        if x.get("k") == "bin" and x["op"] in flip:
            l, r = x["l"], x["r"]
            if r.get("k") == "const" and l.get("k") != "const":
                out.append({"lhs": l, "op": x["op"], "const": r.get("v"), "const_text": r.get("text"), "node": x})
            elif l.get("k") == "const" and r.get("k") != "const":
                out.append({"lhs": r, "op": flip[x["op"]], "const": l.get("v"), "const_text": l.get("text"),
                            "node": x})
    return out


class Roles:
    def __init__(self, project, config):
        self.p = project
        self.cfg = config
        self.state = {}
        self.counter = {}
        self.fault = {}
        self.output_vars = {}
        self.output_funcs = {}
        self._build()

    def _add(self, d, key, why):
        d.setdefault(key, [])
        if why not in d[key]:
            d[key].append(why)

    def _build(self):
        cfg = self.cfg
        compared = set()
        rmw = set()
        for fid, fn in self.p.functions.items():
            for st in iter_stmts(fn["body"]):
                for k in st.get("rmw", []):
                    rmw.add(base_key(k) if not k.startswith("*") else k)
                if st.get("cond"):
                    for c in comparisons(st["cond"]):
                        lk = _lkey(c["lhs"])
                        if lk:
                            compared.add(lk)
                if st["kind"] == "switch" and st.get("cond"):
                    sk = _lkey(st["cond"])
                    if sk:
                        assigned = any(sk in s2.get("defs", []) for s2 in iter_stmts(st.get("body", [])))
                        enum_cases = any(s2["kind"] == "case" and (s2.get("cond") or {}).get("enum")
                                         for s2 in st.get("body", []))
                        if assigned:
                            self._add(self.state, sk, f"{fn['name']}: switch 내부에서 자기 자신을 갱신(상태 머신 구조)")
                        elif enum_cases and cfg.matches("state_var", key_name(sk)):
                            self._add(self.state, sk, f"{fn['name']}: 열거형 case 로 분기하는 switch 변수")
            for call in fn["calls"]:
                name = call.get("name") or ""
                if call.get("callee") and cfg.matches("output_func", name):
                    self._add(self.output_funcs, call["callee"], "출력 함수 이름 패턴")
        for gid, g in self.p.globals.items():
            key = g["key"]
            n = g["name"]
            if cfg.matches("state_var", n) and g["type"].get("kind") in ("int", "enum"):
                self._add(self.state, key, "상태 변수 이름 패턴")
            if cfg.matches("counter_var", n) and g["type"].get("kind") == "int":
                self._add(self.counter, key, "카운터/타이머 이름 패턴")
            if key in rmw and key in compared and g["type"].get("kind") == "int":
                self._add(self.counter, key, "증감 연산 후 상수와 비교됨(카운터 구조)")
            if cfg.matches("fault_var", n):
                self._add(self.fault, key, "고장/진단 이름 패턴")
            if cfg.matches("output_var", n):
                self._add(self.output_vars, key, "출력 변수 이름 패턴")
            if g["type"].get("volatile") and g["type"].get("kind") in ("int", "ptr") and not g.get("static_local"):
                if any(s in n.lower() for s in ("reg", "port", "out")):
                    self._add(self.output_vars, key, "volatile 하드웨어 레지스터 추정")
            # 구조체 필드 단위
        for fid, fn in self.p.functions.items():
            for st in iter_stmts(fn["body"]):
                for k in st.get("defs", []):
                    if key_parts(k)[2] and not k.startswith("*"):
                        fld = key_parts(k)[2].split(".")[-1]
                        if cfg.matches("state_var", fld):
                            self._add(self.state, k, "상태 필드 이름 패턴")
                        if cfg.matches("counter_var", fld):
                            self._add(self.counter, k, "카운터 필드 이름 패턴")
                        if cfg.matches("fault_var", fld):
                            self._add(self.fault, k, "고장 필드 이름 패턴")
                        if cfg.matches("output_var", fld):
                            self._add(self.output_vars, k, "출력 필드 이름 패턴")
                    elif k.startswith(("L:", "P:")):
                        pass
                for k in st.get("rmw", []):
                    if k.startswith("L:") and cfg.matches("counter_var", key_name(k)):
                        self._add(self.counter, f"{fid}|{k}", "지역 카운터")
        for fid, d in self.p.func_decls.items():
            if fid not in self.p.functions and not d.get("system") and self.cfg.matches("output_func", d["name"]):
                self._add(self.output_funcs, fid, "출력 함수(정의 없음, 드라이버 추정)")
        for fid, fn in self.p.functions.items():
            if self.cfg.matches("output_func", fn["name"]):
                self._add(self.output_funcs, fid, "출력 함수 이름 패턴")
        # 출력 함수에 (직·간접) 도달하는 함수
        callees = {fid: {c["callee"] for c in fn["calls"] if c.get("callee")} for fid, fn in self.p.functions.items()}
        reach = set(self.output_funcs)
        changed = True
        while changed:
            changed = False
            for fid, cs in callees.items():
                if fid not in reach and cs & reach:
                    reach.add(fid)
                    changed = True
        self.reaches_output = reach

    # ------------------------------------------------------------ 조회
    def _match(self, d, key):
        if key in d:
            return True
        b = base_key(key)
        return b in d and not key.startswith("*")

    def is_state(self, key):
        return self._match(self.state, key)

    def is_counter(self, key, fid=None):
        return self._match(self.counter, key) or (fid is not None and f"{fid}|{key}" in self.counter)

    def is_fault(self, key):
        return self._match(self.fault, key)

    def is_output_var(self, key):
        return self._match(self.output_vars, key)

    def is_output_func(self, fid):
        return fid in self.output_funcs

    def stmt_roles(self, fid, st) -> list[str]:
        r = []
        keys = st.get("defs", []) + st.get("uses", [])
        if any(self.is_state(k) for k in keys):
            r.append("STATE")
        if any(self.is_counter(k, fid) for k in keys):
            r.append("TIMER_COUNTER")
        if any(self.is_fault(k) for k in keys):
            r.append("FAULT")
        fn = self.p.functions.get(fid)
        if any(self.is_output_var(k) for k in st.get("defs", [])):
            r.append("OUTPUT")
        elif fn:
            for cid in st.get("calls", []):
                c = fn["calls"][cid]
                if c.get("callee") and self.is_output_func(c["callee"]):
                    r.append("OUTPUT")
                    break
        return r

    def calls_output(self, fid, st) -> bool:
        """문장이 출력 함수를 직접 또는 간접 호출하는지."""
        fn = self.p.functions.get(fid)
        if not fn:
            return False
        return any(fn["calls"][c].get("callee") in self.reaches_output for c in st.get("calls", []))

    def to_dict(self):
        return {"state": self.state, "counter": self.counter, "fault": self.fault,
                "output_vars": self.output_vars, "output_funcs": self.output_funcs}


def _lkey(e):
    """비교식 피연산자 → 변수 키(필드 포함)."""
    if e is None:
        return None
    k = e.get("k")
    if k == "var":
        return e.get("key")
    if k == "cast":
        return _lkey(e["e"])
    if k == "member":
        b = _lkey(e["base"])
        if not b:
            return None
        return f"*{b}.{e['field']}" if e.get("arrow") else f"{b}.{e['field']}"
    if k == "index":
        return _lkey(e["base"])
    if k == "un" and e["op"] == "*":
        b = _lkey(e["e"])
        return "*" + b if b else None
    return None


lvalue_key = _lkey
