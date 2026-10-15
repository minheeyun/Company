"""5단계: 통합 영향 전파 (Program Dependence Graph 근사).

변경점(시드)에서 출발해 다음 간선으로 '영향 사실(fact)'을 전파한다.
  DATA          함수 내부 def→use (도달 정의)
  CONTROL       분기 조건 → 제어 의존 문장
  GLOBAL        전역/static 변수 쓰기 → 다른 함수(또는 다음 호출)의 진입 시점 읽기
  PARAM         호출 인자 → 피호출 함수 매개변수 사용
  RETURN        return 값 → 호출 지점의 사용
  OUT_PARAM     포인터 인자를 통한 쓰기 → 호출자 변수
  ALIAS         포인터 별칭(p = &x)을 통한 쓰기
  CALL_COND     호출 여부/조건 변경 → 피호출 함수의 모든 외부 효과
  MACRO / TYPE / SIGNATURE / GLOBAL_DEF   변경된 정의의 사용 위치(시드)
  FIELD_ALIAS / FP_CALL / UNKNOWN         불확실 간선 → POTENTIAL

수준 판정
  DIRECT    변경 문장 자체, 변경된 정의의 사용 위치, 같은 함수 안에서 1단계 의존 문장
  INDIRECT  확정 간선만으로 도달한 그 밖의 위치
  POTENTIAL 경로에 불확실 간선(함수 포인터, 필드 기반 별칭, 파싱 오류, 비활성 코드, 깊이 제한)이 포함
  NO_IMPACT_CONFIRMED  설정 범위·가정 하에서 어떤 간선으로도 도달하지 않은 함수
"""

from __future__ import annotations

import heapq
import itertools

from .ast_extract import iter_stmts, kbase, key_parts
from .cfg import CFG, ENTRY, BRANCH_KINDS
from .dataflow import FunctionDF, PointsTo, overlap, root_key
from .roles import key_name

LEVELS = ["DIRECT", "INDIRECT", "POTENTIAL", "NO_IMPACT_CONFIRMED"]
INTERPROC = {"GLOBAL", "PARAM", "RETURN", "OUT_PARAM", "CALL_COND", "FP_CALL", "FIELD_ALIAS", "ALIAS",
             "UNKNOWN_DEREF"}
EDGE_LABEL = {
    "SEED": "변경", "DATA": "데이터(def-use)", "CONTROL": "제어 의존", "GLOBAL": "공유(전역) 변수",
    "PARAM": "인자 전달", "RETURN": "반환값", "OUT_PARAM": "포인터 인자 출력", "ALIAS": "포인터 별칭",
    "CALL_COND": "호출 조건/호출 여부", "FP_CALL": "함수 포인터 호출", "FIELD_ALIAS": "필드 기반 별칭",
    "UNKNOWN_DEREF": "미확정 포인터 쓰기", "MACRO": "매크로 사용", "TYPE": "자료형 사용", "SIGNATURE": "시그니처 변경",
    "GLOBAL_DEF": "전역 정의 변경", "CALL": "상위 호출", "INVOKED": "피호출 함수 효과",
}


class Fact:
    __slots__ = ("id", "fid", "node", "keys", "def_keys", "kind", "edge", "certain", "depth", "parent", "seed",
                 "change", "reason", "context", "level", "invoke", "bound")

    def __init__(self, **kw):
        for k in self.__slots__:
            setattr(self, k, kw.get(k))


class ImpactAnalyzer:
    def __init__(self, base, curr, changes, cg, roles, config, base_cg=None):
        self.b = base
        self.p = curr
        self.cs = changes
        self.cg = cg
        self.base_cg = base_cg
        self.roles = roles
        self.cfg = config
        self.df: dict[str, FunctionDF] = {}
        self.cfgs: dict[str, CFG] = {}
        self.pts = PointsTo(curr)
        self.facts: list[Fact] = []
        self.best: dict[tuple, Fact] = {}  # (fid,node) -> 대표 fact
        self.state: dict[tuple, dict] = {}
        self.truncated: list[dict] = []
        self.limitations: list[str] = []
        self.external_sinks: list[dict] = []
        self._seq = itertools.count()
        self._heap = []
        self._index()

    # ----------------------------------------------------------- 인덱스
    def dfa(self, fid) -> FunctionDF:
        if fid not in self.df:
            fn = self.p.functions[fid]
            g = CFG(fn)
            self.cfgs[fid] = g
            self.df[fid] = FunctionDF(fn, g)
        return self.df[fid]

    def _index(self):
        p = self.p
        self.callsites: dict[str, list[tuple]] = {}
        self.field_uses: dict[str, list[tuple]] = {}
        self.addr_taken_vars: set[str] = set()
        for fid, fn in p.functions.items():
            for st in iter_stmts(fn["body"]):
                for u in st.get("uses", []):
                    if u.startswith("&G:"):
                        self.addr_taken_vars.add(root_key(u[1:]))
                # 포인터 경유 필드 접근만 필드 기반 별칭 후보
                if any(k.startswith("*") for k in st.get("uses", [])):
                    for fk in st.get("fuses", []):
                        self.field_uses.setdefault(fk, []).append((fid, st["id"]))
            for call in fn["calls"]:
                for e in self.cg.callees.get(fid, []):
                    if e["call_id"] == call["id"]:
                        self.callsites.setdefault(e["callee"], []).append((fid, call.get("stmt"), call, e))
        for g in p.globals.values():
            for t in g.get("addr_init", []):
                self.addr_taken_vars.add(root_key(t))

    # ------------------------------------------------------------- 전파
    def push(self, **kw):
        parent = kw.get("parent")
        edge = kw["edge"]
        certain = kw.get("certain", True) and (parent.certain if parent else True)
        depth = (parent.depth if parent else 0) + (1 if edge in INTERPROC else 0)
        if depth > self.cfg.max_depth:
            self.truncated.append({"function": kw["fid"], "edge": edge,
                                   "from": parent.fid if parent else None, "reason": "max_depth 초과"})
            return
        fid = kw["fid"]
        if fid not in self.p.functions:
            return
        f = Fact(id=next(self._seq), depth=depth, certain=certain, **{k: v for k, v in kw.items()
                                                                     if k not in ("certain",)})
        if f.seed is None:
            f.seed = False
        # 호출 문맥 한정 여부: 특정 호출 지점에서 들어온 영향(인자/호출 조건)은 그 호출자에만 해당
        if edge in ("PARAM", "CALL_COND", "FP_CALL"):
            f.bound = True
        elif edge in ("DATA", "CONTROL", "INVOKED"):
            f.bound = bool(parent.bound) if parent else False
        else:
            f.bound = False
        if parent is not None and f.change is None:
            f.change = parent.change
        f.level = self._level(f)
        if len(self.facts) >= self.cfg.max_facts:
            self.truncated.append({"function": fid, "edge": edge, "reason": "max_facts 초과"})
            return
        heapq.heappush(self._heap, ((0 if f.certain else 1), f.depth, f.id, f))

    def _level(self, f: Fact):
        if not f.certain:
            return "POTENTIAL"
        if f.seed:
            return "DIRECT"
        par = f.parent
        if par is not None and par.seed and par.fid == f.fid and f.edge in ("DATA", "CONTROL"):
            return "DIRECT"
        return "INDIRECT"

    def _new_info(self, f: Fact) -> bool:
        """같은 (함수, 노드)에 이미 같거나 더 강한 정보가 있으면 False."""
        st = self.state.setdefault((f.fid, f.node), {"whole": None, "keys": {}, "ctrl": None, "invoke": None})
        c = f.certain
        new = False
        if f.kind == "control":
            if st["ctrl"] is None or (c and not st["ctrl"]):
                st["ctrl"] = c
                new = True
        elif f.kind == "invoke":
            if st["invoke"] is None or (c and not st["invoke"]):
                st["invoke"] = c
                new = True
        elif f.keys is None and f.def_keys is None:
            if st["whole"] is None or (c and not st["whole"]):
                st["whole"] = c
                new = True
        else:
            ks = f.def_keys if f.def_keys is not None else f.keys
            for k in (ks or ["<anchor>"]):
                cur = st["keys"].get(k)
                if cur is None or (c and not cur):
                    st["keys"][k] = c
                    new = True
        return new

    def run(self, change_ids=None):
        """change_ids 가 주어지면 해당 변경점만 시드로 사용(변경점별 영향 분석)."""
        self.facts, self.best, self.state = [], {}, {}
        self.truncated, self.limitations, self.external_sinks = [], [], []
        self._heap = []
        self.only = set(change_ids) if change_ids else None
        self._seeds()
        while self._heap:
            _, _, _, f = heapq.heappop(self._heap)
            if not self._new_info(f):
                continue
            self.facts.append(f)
            key = (f.fid, f.node)
            cur = self.best.get(key)
            if cur is None or LEVELS.index(f.level) < LEVELS.index(cur.level):
                self.best[key] = f
            self._propagate(f)
        return self

    # ------------------------------------------------------------- 시드
    def _seeds(self):
        p = self.p
        for ch in self.cs.changes:
            if self.only is not None and ch["id"] not in self.only:
                continue
            types = set(ch["types"])
            fid = ch.get("function")
            if "FORMAT_ONLY" in types or "FUNCTION_MOVED" in types:
                continue  # 의미 변경 없음(이동한 static 함수의 호출자 변경은 별도 변경점으로 잡힌다)
            if ch.get("summary_only"):
                continue
            if "FUNCTION_ADDED" in types and fid in p.functions:
                self._seed_invoke(fid, ch, "추가된 함수")
                for (cf, sid, call, e) in self.callsites.get(fid, []):
                    self.push(fid=cf, node=sid, edge="SEED", seed=True, change=ch["id"], kind="data",
                              reason=f"추가된 함수 {key_name(fid)} 호출 위치")
                continue
            if "FUNCTION_REMOVED" in types:
                for (cf, sid, call, e) in self.callsites.get(fid, []):
                    self.push(fid=cf, node=sid, edge="SEED", seed=True, change=ch["id"], kind="data",
                              reason=f"삭제된 함수 {key_name(fid)} 를 여전히 호출(정의 없음)")
                continue
            if "SIGNATURE_CHANGED" in types and fid in p.functions:
                self._seed_invoke(fid, ch, "시그니처 변경")
                for (cf, sid, call, e) in self.callsites.get(fid, []):
                    self.push(fid=cf, node=sid, edge="SIGNATURE", seed=True, change=ch["id"], kind="data",
                              reason=f"시그니처가 변경된 {key_name(fid)} 의 호출 위치")
                continue
            if ch.get("curr_stmt") is not None and fid in p.functions:
                inv = bool(types & {"CALL_ADDED", "CALL_REMOVED"})
                for sid in ch.get("curr_stmts") or [ch["curr_stmt"]]:
                    self.push(fid=fid, node=sid, edge="SEED", seed=True, change=ch["id"], kind="data",
                              invoke=inv, reason=ch["description"])
                    st = p.stmt_index(fid).get(sid)
                    if st is not None and st["kind"] in ("break", "continue", "goto", "return", "case", "default"):
                        self._seed_jump(fid, ch, st)
                continue
            if ch.get("base_stmt") is not None and fid in p.functions:
                for bsid in ch.get("base_stmts") or [ch["base_stmt"]]:
                    self._seed_removed(fid, ch, bsid)
                continue
            if ch.get("variable"):
                self._seed_global(ch)
                continue
            if ch.get("macro"):
                self._seed_macro(ch)
                continue
            if ch.get("type_name"):
                self._seed_type(ch)
                continue
            if types & {"INACTIVE_CODE_CHANGED", "UNMAPPED_CHANGE", "UNPARSED_FILE_CHANGE", "FUNCTION_MODIFIED"}:
                self._seed_uncertain(ch)

    def _seed_invoke(self, fid, ch, why):
        self.push(fid=fid, node=ENTRY, edge="SEED", seed=True, change=ch["id"], kind="invoke",
                  reason=f"{why}: {key_name(fid)} 전체 동작")

    def _seed_jump(self, fid, ch, st):
        g = self.cfg_of(fid)
        for b, _ in g.control_deps().get(st["id"], []):
            for m in g.dependents(b):
                self.push(fid=fid, node=m, edge="CONTROL", seed=True, change=ch["id"], kind="control",
                          reason=f"분기/점프 변경('{st['text']}')으로 실행 흐름 변경")

    def _seed_removed(self, fid, ch, bsid):
        bfn = self.b.functions.get(fid)
        fn = self.p.functions[fid]
        anchor = ch.get("anchor_curr_stmt")
        bst = self.b.stmt_index(fid).get(bsid) if bfn else None
        if bst is None:
            return
        names = {}
        for k, v in fn["locals"].items():
            names.setdefault(v["name"], k)
        keys = []
        for d in bst.get("defs", []):
            if d.startswith("L:"):
                nm = key_name(d)
                if nm in names:
                    rest = d.split(".", 1)[1] if "." in d.split("#", 1)[-1] else ""
                    keys.append(names[nm] + ("." + rest if rest else ""))
            else:
                keys.append(d)
        if anchor is not None and keys:
            self.push(fid=fid, node=anchor, edge="SEED", seed=True, change=ch["id"], kind="data", def_keys=keys,
                      reason=f"삭제된 문장 '{bst['text']}' 이 정의하던 값({', '.join(key_name(k) for k in keys)})")
        elif anchor is not None:
            self.push(fid=fid, node=anchor, edge="SEED", seed=True, change=ch["id"], kind="data", def_keys=[],
                      reason=f"삭제된 문장 '{bst['text']}' 위치")
        # 삭제된 호출 → 피호출 함수 호출 여부 변경
        for cid in bst.get("calls", []):
            callee = bfn["calls"][cid].get("callee")
            if callee and callee in self.p.functions:
                self.push(fid=callee, node=ENTRY, edge="CALL_COND", seed=False, change=ch["id"], kind="invoke",
                          parent=self._virtual(fid, anchor, ch), reason=f"{key_name(fid)} 에서 호출 삭제")
        # 삭제된 분기 → 기존 제어 의존 문장(현재 버전 위치)
        if bst["kind"] in BRANCH_KINDS:
            gb = CFG(bfn)
            inv = {v: k for k, v in self.cs.stmt_map.get(fid, {}).items()}
            for m in gb.dependents(bst["id"]):
                if m in inv:
                    self.push(fid=fid, node=inv[m], edge="CONTROL", seed=True, change=ch["id"], kind="control",
                              reason=f"분기 '{bst['text']}' 삭제로 실행 조건 변경")

    def _virtual(self, fid, node, ch):
        f = Fact(id=next(self._seq), fid=fid, node=node, edge="SEED", certain=True, depth=0, seed=True,
                 change=ch["id"], kind="data", reason=ch["description"], parent=None)
        f.level = "DIRECT"
        return f

    def _seed_global(self, ch):
        key = ch["variable"]
        types = set(ch["types"])
        all_uses = bool(types & {"GLOBAL_TYPE_CHANGED", "GLOBAL_QUALIFIER_CHANGED", "GLOBAL_REMOVED"})
        for fid in self.p.functions:
            df = self.dfa(fid)
            if all_uses:
                nodes = [n for n in df.cfg.stmts if any(overlap(key, k) for k in df.uses[n] + df.defs[n])]
            else:
                nodes = df.upward_uses(key)
            for n in nodes:
                self.push(fid=fid, node=n, edge="GLOBAL_DEF", seed=True, change=ch["id"], kind="data", keys=[key],
                          reason=f"정의가 변경된 전역변수 {key_name(key)} 사용")

    def _macro_closure(self, ch):
        names = {ch["macro_name"]}
        keys = {ch["macro"]}
        changed = True
        while changed:
            changed = False
            for k, m in self.p.macros.items():
                if k not in keys and names & set(m["idents"]):
                    keys.add(k)
                    names.add(m["name"])
                    changed = True
        return keys, names

    def _seed_macro(self, ch):
        keys, names = self._macro_closure(ch)
        for fid, fn in self.p.functions.items():
            for mu in fn.get("macro_uses", []):
                hit = (mu.get("def") in keys) if mu.get("def") else (mu["name"] in names)
                if hit and mu.get("stmt") is not None:
                    self.push(fid=fid, node=mu["stmt"], edge="MACRO", seed=True, change=ch["id"], kind="data",
                              reason=f"변경된 매크로 {ch['macro_name']} 사용"
                                     + (f"(매크로 {mu['name']} 경유)" if mu["name"] != ch["macro_name"] else ""))
        # 전역 초기화식에서 사용 → 해당 전역의 진입 시점 읽기
        for rel, fm in self.p.files.items():
            for mu in fm.get("global_macro_uses", []):
                hit = (mu.get("def") in keys) if mu.get("def") else (mu["name"] in names)
                if not hit:
                    continue
                for g in self.p.globals.values():
                    if g["file"] == rel and g["line"] == mu["line"]:
                        self._seed_global({"variable": g["key"], "types": ["GLOBAL_INIT_CHANGED"], "id": ch["id"]})

    def _seed_type(self, ch):
        name = ch["type_name"]
        kind = ch.get("type_kind")
        det = ch.get("detail") or {}
        changed_consts = set()
        if kind == "ENUM":
            a, b = det.get("from") or {}, det.get("to") or {}
            changed_consts = {k for k in set(a) | set(b) if a.get(k) != b.get(k)}
        for fid, fn in self.p.functions.items():
            for st in iter_stmts(fn["body"]):
                hit = False
                for k in st.get("uses", []) + st.get("defs", []):
                    vi = self.p.var_info(k.lstrip("&"), fid)
                    if vi and name in _type_words(vi.get("type", {})):
                        hit = True
                        break
                if not hit and changed_consts:
                    toks = set(st["text"].split())
                    hit = bool(toks & changed_consts)
                if not hit and kind == "STRUCT":
                    hit = any(k.startswith(f"F:{name}.") for k in st.get("fuses", []) + st.get("fdefs", []))
                if hit:
                    self.push(fid=fid, node=st["id"], edge="TYPE", seed=True, change=ch["id"], kind="data",
                              reason=f"정의가 변경된 자료형 {name} 사용")
            if any(name in _type_words(p_["type"]) for p_ in fn["params"]) or name in _type_words(fn["ret"]):
                self.push(fid=fid, node=ENTRY, edge="TYPE", seed=True, change=ch["id"], kind="invoke",
                          reason=f"인자/반환형이 변경된 자료형 {name} 사용")

    def _seed_uncertain(self, ch):
        loc = ch.get("curr") or ch.get("base") or {}
        rel, line = loc.get("file"), loc.get("line")
        for fid, fn in self.p.functions.items():
            if fn["file"] == rel and line and fn["line"] <= line <= fn["end_line"]:
                self.push(fid=fid, node=ENTRY, edge="UNKNOWN_DEREF", certain=False, seed=True, change=ch["id"],
                          kind="invoke", reason=ch["description"])

    def readers(self, key):
        """key 의 진입 시점 값을 읽는 (함수, 노드) — 다른 함수 또는 다음 호출에서의 읽기."""
        cache = self.__dict__.setdefault("_readers", {})
        if key not in cache:
            out = []
            for gfid in self.p.functions:
                for m in self.dfa(gfid).upward_uses(key):
                    out.append((gfid, m))
            cache[key] = out
        return cache[key]

    def cfg_of(self, fid):
        self.dfa(fid)
        return self.cfgs[fid]

    # ------------------------------------------------------- 전파 규칙
    def _propagate(self, f: Fact):
        fid = f.fid
        df = self.dfa(fid)
        g = self.cfgs[fid]
        fn = self.p.functions[fid]
        if f.kind == "invoke":
            self._invoke(f, fn, g)
            return
        st = g.stmts.get(f.node)
        producer_only = f.def_keys is not None  # 이 노드는 값을 '받기만' 함(출력 인자/삭제 문장 위치)
        # 오염된 정의
        if f.def_keys is not None:
            tdefs = list(f.def_keys)
        elif f.node == ENTRY:
            tdefs = list(f.keys or [])
        else:
            tdefs = list(df.defs.get(f.node, []))
        # 1) 함수 내부 데이터
        for d in tdefs:
            if f.def_keys is not None or f.node == ENTRY:
                targets = self._forward_uses(fid, f.node, d)
            else:
                targets = df.uses_of(f.node, d)
            for u in targets:
                if u == f.node and f.def_keys is None:
                    continue
                self.push(fid=fid, node=u, edge="DATA", parent=f, kind="data", keys=[d],
                          reason=f"{key_name(d)} 값 전달 ({fn['name']} L{g.nodes[u]['line']})")
        # 2) 공유 변수 / 별칭 / 출력 인자
        for d in tdefs:
            self._shared(f, fid, d)
        if st is None or producer_only:
            return
        # 3) 제어 의존
        if st["kind"] in BRANCH_KINDS:
            for m in g.dependents(f.node):
                if m == f.node:
                    continue
                self.push(fid=fid, node=m, edge="CONTROL", parent=f, kind="control",
                          reason=f"조건 '{st['text']}' 에 제어 의존 (L{g.nodes[m]['line']})")
        # 4) 반환값
        if st["kind"] == "return" and st.get("expr") is not None:
            self._return(f, fid)
        # 5) 호출
        for cid in st.get("calls", []):
            self._call(f, fid, fn, fn["calls"][cid])
        # 6) 포인터 필드 쓰기(필드 기반 근사)
        for fk in st.get("fdefs", []):
            if any(d.startswith("*") for d in tdefs):
                for (ofid, onode) in self.field_uses.get(fk, []):
                    self.push(fid=ofid, node=onode, edge="FIELD_ALIAS", parent=f, certain=False, kind="data",
                              keys=[fk], reason=f"포인터를 통한 {fk[2:]} 쓰기 — 같은 필드를 포인터로 읽는 위치(별칭 미확정)")

    def _forward_uses(self, fid, start, key):
        """start 이후(포함) key 의 강한 재정의 전까지 key 를 읽는 노드."""
        g = self.cfgs[fid]
        df = self.df[fid]
        out = []
        seen = set()
        stack = [start] if start != ENTRY else [s for s, _ in g.succ.get(ENTRY, [])]
        first = start != ENTRY
        while stack:
            n = stack.pop()
            if n in seen:
                continue
            seen.add(n)
            if any(overlap(key, u) for u in df.uses.get(n, [])):
                out.append(n)
            if not (first and n == start) and key in df.strong.get(n, set()):
                continue
            for s, _ in g.succ.get(n, []):
                stack.append(s)
        return out

    def _shared(self, f, fid, d):
        resolved = []
        if d.startswith("G:"):
            resolved.append((d, True, "GLOBAL"))
        elif d.startswith("*"):
            base = kbase(d[1:])
            if base.startswith("P:"):
                self._out_param(f, fid, d)
            targets, certain = self.pts.resolve(fid, d)
            for scope, t in targets:
                if t.startswith("G:"):
                    resolved.append((t, certain, "ALIAS"))
            if not targets and not base.startswith("P:"):
                # 대상 미확정 포인터 쓰기: 주소가 취해진 전역 중 같은 자료형 → 잠재
                self.limitations.append(f"{key_name(fid)}: 포인터 '{key_name(base)}' 의 대상 미확정 쓰기")
        for key, certain, edge in resolved:
            for gfid, m in self.readers(key):
                if gfid == fid and m == f.node:
                    continue
                self.push(fid=gfid, node=m, edge=edge, parent=f, certain=certain, kind="data", keys=[key],
                          reason=f"{key_name(key)} 공유: {key_name(fid)} 에서 쓰고 "
                                 f"{key_name(gfid)} L{self.cfgs[gfid].nodes[m]['line']} 에서 읽음")

    def _out_param(self, f, fid, d):
        inner = d[1:]
        _, base, suffix = key_parts(inner)
        fn = self.p.functions[fid]
        idx = next((p["index"] for p in fn["params"] if p["key"] == base), None)
        if idx is None:
            return
        for (cf, sid, call, e) in self.callsites.get(fid, []):
            if f.context and f.context != (cf, sid):
                continue
            if sid is None or idx >= len(call.get("arg_addr", [])):
                continue
            addr, ptr = call["arg_addr"][idx], call["arg_ptr"][idx]
            tgt = (addr + suffix) if addr else ("*" + ptr + suffix) if ptr else None
            if not tgt:
                continue
            self.push(fid=cf, node=sid, edge="OUT_PARAM", parent=f, certain=e["certainty"] == "confirmed",
                      kind="data", def_keys=[tgt],
                      reason=f"{key_name(fid)} 가 포인터 인자 {key_name(base)} 로 {key_name(tgt)} 갱신")

    def _return(self, f, fid):
        for (cf, sid, call, e) in self.callsites.get(fid, []):
            if sid is None:
                continue
            if f.context and f.context != (cf, sid):
                continue
            self.push(fid=cf, node=sid, edge="RETURN", parent=f, certain=e["certainty"] == "confirmed",
                      kind="data", keys=[f"<ret:{fid}>"],
                      reason=f"{key_name(fid)} 반환값 사용 ({key_name(cf)} L{call['line']})")

    def _call(self, f, fid, fn, call):
        edges = [e for e in self.cg.callees.get(fid, []) if e["call_id"] == call["id"]]
        control = f.kind == "control" or f.invoke
        whole = f.keys is None and f.def_keys is None
        for e in edges:
            callee = e["callee"]
            certain = e["certainty"] == "confirmed"
            if callee not in self.p.functions:
                self.external_sinks.append({"function": fid, "line": call["line"], "callee": callee,
                                            "call": call["text"], "level": f.level, "fact": f.id,
                                            "certain": certain and f.certain})
                continue
            edge = "FP_CALL" if e["kind"] == "indirect" else ("CALL_COND" if control else "PARAM")
            if control:
                self.push(fid=callee, node=ENTRY, edge=edge, parent=f, certain=certain, kind="invoke",
                          reason=f"{key_name(fid)} L{call['line']} 의 {key_name(callee)} 호출 조건 변경")
                continue
            cfn = self.p.functions[callee]
            for i, keys in enumerate(call.get("arg_keys", [])):
                if i >= len(cfn["params"]):
                    break
                hit = whole or any(overlap(t, k) for t in (f.keys or []) for k in keys) or \
                    any(t.startswith("<ret:") and t[5:-1].split("::")[-1] + " (" in (call["args"][i] + " (")
                        for t in (f.keys or []))
                if hit:
                    pk = cfn["params"][i]["key"]
                    self.push(fid=callee, node=ENTRY, edge=edge, parent=f, certain=certain, kind="data",
                              keys=[pk], context=(fid, call.get("stmt")),
                              reason=f"{key_name(fid)} L{call['line']} 에서 인자 '{call['args'][i]}' → "
                                     f"{key_name(callee)}({cfn['params'][i]['name']})")

    def _invoke(self, f, fn, g):
        """호출 여부/조건이 바뀐 함수: 외부 효과를 내는 모든 문장."""
        fid = f.fid
        for n, st in g.stmts.items():
            effect = st["kind"] == "return" or st.get("calls") or any(
                d.startswith(("G:", "*")) for d in st.get("defs", []))
            if effect:
                self.push(fid=fid, node=n, edge="INVOKED" if not f.seed else "SEED", parent=f, kind="control",
                          seed=f.seed, certain=f.certain,
                          reason=f"{fn['name']} 호출 여부/조건 변경 → L{st['line']} '{st['text'][:50]}' 실행 여부 변경")

    # ----------------------------------------------------------- 결과
    def path_of(self, f: Fact) -> list[dict]:
        out = []
        guard = 0
        while f is not None and guard < 200:
            fn = self.p.functions.get(f.fid, {})
            line = None
            text = ""
            if f.node is not None and f.node != ENTRY:
                st = self.p.stmt_index(f.fid).get(f.node)
                if st:
                    line, text = st["line"], st["text"]
            out.append({"function": f.fid, "name": fn.get("name"), "file": fn.get("file"), "line": line,
                        "code": text[:120], "edge": f.edge, "edge_label": EDGE_LABEL.get(f.edge, f.edge),
                        "certain": f.certain, "reason": f.reason, "change": f.change})
            f = f.parent
            guard += 1
        return list(reversed(out))

    def observable(self, fid) -> tuple[list[str], bool]:
        """호출 문맥과 무관하게 영향받은 문장 중 외부로 관측 가능한 효과, 그리고 확정 여부.
        (특정 호출 지점의 인자/호출 조건으로 들어온 영향은 그 호출자에 이미 반영되므로 제외)"""
        eff = []
        certain = False
        g = self.cfgs.get(fid)
        if g is None:
            return eff, certain
        for fact in self.facts:
            if fact.fid != fid or fact.node == ENTRY or fact.bound:
                continue
            n = fact.node
            before = len(eff)
            st = g.stmts.get(n)
            if st is None:
                continue
            if st["kind"] == "return" and st.get("expr") is not None:
                eff.append("return")
            for d in st.get("defs", []):
                if d.startswith("G:"):
                    readers = bool(self.readers(d))
                    if readers or self.roles.is_output_var(d):
                        eff.append(f"global:{key_name(d)}")
                elif d.startswith("*"):
                    eff.append(f"pointer:{key_name(d)}")
            if st.get("calls"):
                eff.append("call")
            if len(eff) > before and fact.certain:
                certain = True
        return sorted(set(eff)), certain

    def summarize(self) -> dict:
        p = self.p
        funcs: dict[str, dict] = {}
        for (fid, n), f in self.best.items():
            ent = funcs.setdefault(fid, {"function": fid, "name": p.functions[fid]["name"],
                                         "file": p.functions[fid]["file"], "level": f.level, "nodes": [],
                                         "edges": set(), "changes": set(), "facts": []})
            if LEVELS.index(f.level) < LEVELS.index(ent["level"]):
                ent["level"] = f.level
            st = p.stmt_index(fid).get(n) if n != ENTRY else None
            ent["nodes"].append({"line": st["line"] if st else p.functions[fid]["line"],
                                 "code": st["text"][:160] if st else "(함수 진입)", "level": f.level,
                                 "edge": f.edge, "reason": f.reason,
                                 "roles": self.roles.stmt_roles(fid, st) if st else []})
            ent["edges"].add(f.edge)
            if f.change:
                ent["changes"].add(f.change)
            ent["facts"].append(f)
        # 상위 호출 함수(관측 가능한 효과가 있을 때만)
        for fid in list(funcs):
            obs, obs_certain = self.observable(fid)
            funcs[fid]["observable_effects"] = obs
            if not obs:
                continue
            for caller, (depth, pot) in self.cg.upward(fid, self.cfg.max_depth).items():
                pot = pot or not obs_certain
                if caller in funcs or caller not in p.functions:
                    if caller in funcs:
                        funcs[caller].setdefault("via_callee", []).append(fid)
                    continue
                funcs[caller] = {"function": caller, "name": p.functions[caller]["name"],
                                 "file": p.functions[caller]["file"],
                                 "level": "POTENTIAL" if pot else "INDIRECT", "nodes": [], "edges": {"CALL"},
                                 "changes": set(funcs[fid]["changes"]), "facts": [], "call_context_only": True,
                                 "via_callee": [fid], "call_depth": depth}
        # 피호출 함수 중 영향 없음(인자·호출 조건 불변)
        changed_funcs = {c.get("function") for c in self.cs.changes if c.get("function")}
        unaffected_callees = {}
        for cf in changed_funcs:
            for e in self.cg.callees.get(cf, []):
                c = e["callee"]
                if c in self.p.functions and c not in funcs:
                    unaffected_callees.setdefault(c, []).append(cf)
        # 나머지 함수 분류
        failed_files = {x["file"] for x in p.failures}
        trunc_funcs = {t["function"] for t in self.truncated}
        trunc_reach = set()
        for t in trunc_funcs:
            if t in p.functions:
                trunc_reach |= {t, *self.cg.upward(t).keys(), *self.cg.downward(t).keys()}
        no_impact = []
        for fid, fn in p.functions.items():
            if fid in funcs:
                continue
            if fn["file"] in failed_files or not fn.get("parse_ok", True):
                funcs[fid] = {"function": fid, "name": fn["name"], "file": fn["file"], "level": "POTENTIAL",
                              "nodes": [], "edges": {"PARSE_ERROR"}, "changes": set(), "facts": [],
                              "reason": "파싱 오류가 있는 파일 — 영향 여부 확정 불가"}
                continue
            if fid in trunc_reach:
                funcs[fid] = {"function": fid, "name": fn["name"], "file": fn["file"], "level": "POTENTIAL",
                              "nodes": [], "edges": {"DEPTH_LIMIT"}, "changes": set(), "facts": [],
                              "reason": "분석 깊이/규모 제한으로 탐색이 중단된 영역과 연결됨"}
                continue
            reason = "설정된 분석 범위에서 어떤 의존 간선으로도 도달하지 않음"
            if fid in unaffected_callees:
                reason = (f"변경 함수({', '.join(key_name(x) for x in unaffected_callees[fid])})가 호출하지만 "
                          "인자·호출 조건·공유 데이터 변화 없음(단순 호출 관계)")
            no_impact.append({"function": fid, "name": fn["name"], "file": fn["file"], "reason": reason,
                              "callee_of_changed": fid in unaffected_callees})
        return {"functions": funcs, "no_impact": no_impact}


def _type_words(t: dict) -> set[str]:
    s = (t or {}).get("spelling", "")
    return set(s.replace("*", " ").replace("[", " ").replace("]", " ").split())
