"""3단계(a): Call Graph — 직접 호출, 함수 포인터/콜백/테이블을 통한 간접(잠재) 호출, 진입점, 호출 체인."""

from __future__ import annotations

from collections import deque

from .ast_extract import iter_stmts, kbase


def _fn_type_norm(t: str) -> str:
    return t.replace("(*)", "").replace(" ", "").replace("const", "")


class CallGraph:
    def __init__(self, project, config):
        self.p = project
        self.cfg = config
        self.edges: list[dict] = []
        self.callers: dict[str, list[dict]] = {}
        self.callees: dict[str, list[dict]] = {}
        self.unresolved: list[dict] = []
        self.bindings: list[dict] = []
        self._build()
        self.entry_points = self._entries()

    # ------------------------------------------------------------------
    def _add(self, e):
        self.edges.append(e)
        self.callers.setdefault(e["callee"], []).append(e)
        self.callees.setdefault(e["caller"], []).append(e)

    def _build(self):
        p = self.p
        # 함수 포인터 바인딩 수집(전역 초기화, 대입, 인자 전달)
        binds = list(p.global_fp_bindings)
        for fid, fn in p.functions.items():
            for b in fn.get("fp_bindings", []):
                bb = dict(b)
                bb["scope"] = fid
                binds.append(bb)
        self.bindings = binds
        addr_taken = set(p.global_addr_taken)
        for fn in p.functions.values():
            addr_taken.update(fn.get("addr_taken", []))
        self.addr_taken = addr_taken
        self.fp_values = self._fp_flow(binds)

        for fid, fn in p.functions.items():
            for call in fn["calls"]:
                base = {"caller": fid, "line": call["line"], "call_id": call["id"], "stmt": call.get("stmt"),
                        "file": fn["file"]}
                if not call.get("indirect"):
                    self._add({**base, "callee": call["callee"], "kind": "direct", "certainty": "confirmed",
                               "external": call.get("external", False), "reason": "직접 호출(심볼 참조)"})
                    continue
                cands, how = self.resolve_indirect(fid, fn, call)
                if not cands:
                    self.unresolved.append({**base, "expr": call.get("name"), "fp_type": call.get("fp_type")})
                    self._add({**base, "callee": "<unknown-fp>", "kind": "indirect", "certainty": "potential",
                               "external": True, "reason": "함수 포인터 대상 미확정(후보 없음)"})
                for c in cands:
                    self._add({**base, "callee": c, "kind": "indirect", "certainty": "potential",
                               "external": c not in p.functions, "reason": how,
                               "expr": call.get("name")})

    @staticmethod
    def _scoped(scope, key):
        return key if key.startswith(("G:", "arg:")) else f"{scope}|{key}"

    def _fp_flow(self, binds):
        """함수 포인터 값의 흐름: 바인딩(=함수 대입) + 포인터 복사 + 매개변수 전달을 고정점으로 전파."""
        vals: dict[str, set] = {}
        for b in binds:
            vals.setdefault(self._scoped(b.get("scope"), kbase(b["target"])) if not b["target"].startswith("arg:")
                            else b["target"], set()).add(b["func"])
        flows = []
        for fid, fn in self.p.functions.items():
            for i, prm in enumerate(fn["params"]):
                flows.append((f"{fid}|{prm['key']}", f"arg:{fid}:{i}"))
            for st in iter_stmts(fn["body"]):
                for lhs, rhs in st.get("ptr_assign", []):
                    if rhs.startswith("copy:"):
                        flows.append((self._scoped(fid, kbase(lhs)), self._scoped(fid, kbase(rhs[5:]))))
            for call in fn["calls"]:
                callee = call.get("callee")
                for i, ptr in enumerate(call.get("arg_ptr", [])):
                    if callee and ptr:
                        flows.append((f"arg:{callee}:{i}", self._scoped(fid, kbase(ptr))))
        for _ in range(10):
            changed = False
            for dst, src in flows:
                s = vals.get(src)
                if s and not s <= vals.setdefault(dst, set()):
                    vals[dst] |= s
                    changed = True
            if not changed:
                break
        return vals

    def resolve_indirect(self, fid, fn, call):
        key = call.get("fp_key")
        fkey = call.get("fp_fkey")
        cands = set()
        how = []
        if key:
            base = kbase(key)
            flowed = self.fp_values.get(self._scoped(fid, base), set())
            if flowed:
                cands |= flowed
                how.append(f"'{call.get('name')}' 로 흘러드는 함수 포인터 값(대입/복사/인자 전달 추적)")
            for b in self.bindings:
                tgt = b["target"]
                tb = kbase(tgt)
                scoped = b.get("scope")
                if tgt.startswith("G:") and (tgt == key or tb == base):
                    cands.add(b["func"])
                elif scoped == fid and tb == base:
                    cands.add(b["func"])
            if cands:
                how.append(f"'{call.get('name')}' 에 바인딩된 함수(초기화/대입)")
            if base.startswith("P:"):
                # 콜백 인자: 호출자들이 이 매개변수 위치에 넘긴 함수
                idx = next((pp["index"] for pp in fn["params"] if pp["key"] == base), None)
                for b in self.bindings:
                    if b["target"] == f"arg:{fid}:{idx}":
                        cands.add(b["func"])
                        how.append("호출자가 콜백 인자로 전달한 함수")
            # 지역 포인터가 전역 포인터를 복사한 경우
            for st in iter_stmts(fn["body"]):
                for lhs, rhs in st.get("ptr_assign", []):
                    if lhs == base and rhs.startswith("copy:G:"):
                        src = rhs[5:]
                        for b in self.bindings:
                            if kbase(b["target"]) == src:
                                cands.add(b["func"])
                                how.append("전역 함수 포인터 복사")
        if fkey and not cands:
            fld = fkey.split(".")[-1]
            for b in self.bindings:
                if b["target"].endswith("." + fld):
                    cands.add(b["func"])
            if cands:
                how.append(f"구조체 필드 {fkey[2:]} 에 대입된 함수(필드 기반 근사)")
        if not cands:
            # 최후 수단: 주소가 취해진 함수 중 시그니처 호환
            want = _fn_type_norm(call.get("fp_type", ""))
            for f in sorted(self.addr_taken):
                d = self.p.func_decls.get(f)
                if d and _fn_type_norm(d["type"]) == want:
                    cands.add(f)
            if cands:
                how.append("주소가 취해진 함수 중 타입 호환 후보(보수적 근사)")
        return sorted(cands), "; ".join(dict.fromkeys(how)) or "미확정"

    def _entries(self):
        p = self.p
        tasks = {t.name: t for t in self.cfg.tasks}
        out = {}
        for fid, fn in p.functions.items():
            direct = [e for e in self.callers.get(fid, []) if e["kind"] == "direct"]
            indirect = [e for e in self.callers.get(fid, []) if e["kind"] == "indirect"]
            if fn["name"] in tasks:
                t = tasks[fn["name"]]
                out[fid] = {"reason": f"설정된 {t.kind} (priority={t.priority})", "kind": t.kind,
                            "priority": t.priority}
            elif fn["name"] == "main":
                out[fid] = {"reason": "main", "kind": "background", "priority": 0}
            elif not direct and not indirect:
                out[fid] = {"reason": "프로젝트 내 호출자 없음(외부/스케줄러 진입 추정)", "kind": "unknown",
                            "priority": None}
            elif not direct and indirect:
                out[fid] = {"reason": "함수 포인터로만 호출됨(콜백)", "kind": "callback", "priority": None}
        return out

    # ------------------------------------------------------------------
    def callers_of(self, fid, include_potential=True):
        return [e for e in self.callers.get(fid, []) if include_potential or e["certainty"] == "confirmed"]

    def callees_of(self, fid, include_potential=True):
        return [e for e in self.callees.get(fid, []) if include_potential or e["certainty"] == "confirmed"]

    def upward(self, fid, max_depth=50):
        """상위 호출 함수 전체(순환 안전). {함수: (깊이, potential 경유 여부)}"""
        out = {}
        q = deque([(fid, 0, False)])
        seen = {fid}
        while q:
            f, d, pot = q.popleft()
            if d >= max_depth:
                continue
            for e in self.callers.get(f, []):
                c = e["caller"]
                p2 = pot or e["certainty"] != "confirmed"
                if c not in seen:
                    seen.add(c)
                    out[c] = (d + 1, p2)
                    q.append((c, d + 1, p2))
        return out

    def downward(self, fid, max_depth=50):
        out = {}
        q = deque([(fid, 0, False)])
        seen = {fid}
        while q:
            f, d, pot = q.popleft()
            if d >= max_depth:
                continue
            for e in self.callees.get(f, []):
                c = e["callee"]
                p2 = pot or e["certainty"] != "confirmed"
                if c not in seen:
                    seen.add(c)
                    out[c] = (d + 1, p2)
                    q.append((c, d + 1, p2))
        return out

    def chains_to_entries(self, fid, limit=20, max_len=30):
        """fid 에서 진입점까지의 호출 체인(순환 제외, 개수 제한)."""
        chains = []
        stack = [(fid, [fid], False)]
        while stack and len(chains) < limit:
            f, path, pot = stack.pop()
            ups = self.callers.get(f, [])
            if f in self.entry_points or not ups or len(path) >= max_len:
                chains.append({"chain": list(reversed(path)), "potential": pot,
                               "entry": f, "entry_reason": self.entry_points.get(f, {}).get("reason", "호출자 없음")})
                continue
            for e in ups:
                c = e["caller"]
                if c in path:
                    continue  # 순환 호출
                stack.append((c, path + [c], pot or e["certainty"] != "confirmed"))
        return chains

    def contexts(self):
        """진입점별 도달 함수 집합(동시성 분석용)."""
        out = {}
        for ep, info in self.entry_points.items():
            reach = self.downward(ep)
            out[ep] = {"info": info, "functions": {ep, *reach.keys()}}
        return out

    def cycles(self):
        """순환 호출(재귀) 탐지 — 강연결요소."""
        index, low, onstack, stack, res = {}, {}, set(), [], []
        counter = [0]
        nodes = set(self.p.functions)

        def strong(v):
            index[v] = low[v] = counter[0]
            counter[0] += 1
            stack.append(v)
            onstack.add(v)
            for e in self.callees.get(v, []):
                w = e["callee"]
                if w not in nodes:
                    continue
                if w not in index:
                    strong(w)
                    low[v] = min(low[v], low[w])
                elif w in onstack:
                    low[v] = min(low[v], index[w])
            if low[v] == index[v]:
                comp = []
                while True:
                    w = stack.pop()
                    onstack.discard(w)
                    comp.append(w)
                    if w == v:
                        break
                if len(comp) > 1 or any(e["callee"] == v for e in self.callees.get(v, [])):
                    res.append(sorted(comp))

        import sys
        old = sys.getrecursionlimit()
        sys.setrecursionlimit(max(old, 10000))
        try:
            for v in sorted(nodes):
                if v not in index:
                    strong(v)
        finally:
            sys.setrecursionlimit(old)
        return res

    def to_dict(self):
        return {"edges": [{k: v for k, v in e.items()} for e in self.edges],
                "entry_points": self.entry_points, "unresolved_indirect": self.unresolved,
                "cycles": self.cycles(), "address_taken": sorted(self.addr_taken)}
