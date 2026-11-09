"""분석 결과 정규화 및 관계 저장: ProjectIndex -> AnalysisGraph(SDG/CPG).

처리 순서
  1. 심벌 노드(파일·함수·전역·자료형·필드·매크로) 생성
  2. 함수별 CFG 생성, AST 노드에 CFG 앵커 기록
  3. 문장별 식 분석: 추상 읽기/쓰기 위치, 호출 지점, points-to 제약
  4. points-to 해석(간접 호출·콜백 테이블 대상 복원)
  5. 함수 부작용 요약(MOD): 강결합요소(SCC)를 포함해 고정점 계산
  6. 함수 내부: Reaching Definitions -> DATA, 후지배자 -> CONTROL
  7. 함수 간: call-site/actual-in/actual-out, PARAM_IN/OUT, CALL, SUMMARY(HRB)
  8. 전역 상태: 쓰기 문장 -> 위치 노드 -> 읽기 문장 (태스크·ISR 사이 공유 상태 포함)
  9. 기능 분류용 요약 관계(CALLS, READS, WRITES, USES_*)
"""
from __future__ import annotations

from collections import defaultdict, deque
from typing import Dict, List, Optional, Set, Tuple

from . import clang_api as ca
from .cfg import CFG, annotate_anchors, build_cfg, control_dependence, ntscd, postdominators, strip_casts
from .config import AnalysisOptions
from .dataflow import ReachingDefs, def_use_edges
from .model import EK, FIELD_SEP, NK, AnalysisGraph, loc_may_alias, loc_root
from .pointsto import NONE, UNK, PointsTo, vunion
from .project_indexer import ProjectIndex


class CallInfo:
    __slots__ = ("cs", "ast_id", "direct", "callee_val", "callee_reads", "args", "node", "func", "ast")

    def __init__(self, cs, ast_id, direct, callee_val, callee_reads, args, node, func, ast):
        self.cs = cs
        self.ast_id = ast_id
        self.direct = direct
        self.callee_val = callee_val
        self.callee_reads = callee_reads
        self.args = args
        self.node = node
        self.func = func
        self.ast = ast


class NodeInfo:
    """CFG 노드 하나의 식 분석 결과(추상 -> 해석 후 구체 위치)."""
    __slots__ = ("reads", "writes", "calls", "volatile", "macros", "fields", "enums", "fn_addrs",
                 "ret_val", "r_reads", "r_writes", "unresolved", "pm_w", "pm_r")

    def __init__(self):
        self.reads: List[tuple] = []
        self.writes: List[Tuple[tuple, bool]] = []
        self.calls: List[CallInfo] = []
        self.volatile = False
        self.macros: Set[str] = set()
        self.fields: Set[str] = set()
        self.enums: Set[str] = set()
        self.fn_addrs: Set[str] = set()
        self.ret_val: Optional[tuple] = None
        self.r_reads: Set[str] = set()
        self.r_writes: List[Tuple[str, bool]] = []
        self.unresolved: List[str] = []
        self.pm_w: Set[str] = set()     # 포인터 매개변수를 통한 쓰기 위치(호출 지점에서 문맥별로 해석)
        self.pm_r: Set[str] = set()     # 포인터 매개변수를 통한 읽기 위치


def _is_direct_lv(lv: tuple) -> bool:
    if lv[0] == "var":
        return True
    if lv[0] == "field":
        return _is_direct_lv(lv[1])
    return False


def _has_deref(lv: tuple) -> bool:
    if lv[0] in ("deref", "elem"):
        return True
    if lv[0] == "field":
        return _has_deref(lv[1])
    return False


class ExprVisitor:
    def __init__(self, gb: "GraphBuilder", func: str, node_id: str, info: NodeInfo):
        self.gb = gb
        self.func = func
        self.node_id = node_id
        self.info = info
        self.reads_stack: List[List[tuple]] = [info.reads]

    def read(self, item: tuple) -> None:
        self.reads_stack[-1].append(item)

    def _note(self, n: dict) -> None:
        if n.get("mac"):
            self.info.macros.add(n["mac"])
        ty = n.get("ty")
        if ty and ty.get("vol"):
            self.info.volatile = True

    def lval(self, n: dict) -> tuple:
        self._note(n)
        k = n["k"]
        if k in ("ICast", "Cast"):
            return self.lval(n["ch"][0]) if n.get("ch") else UNK
        if k == "Ref":
            rk = n.get("rk")
            if rk in ("g", "l", "p") and n.get("ref"):
                return ("var", n["ref"])
            return UNK
        if k == "Member":
            if n.get("ref"):
                self.info.fields.add(n["ref"])
            if not n.get("ch"):
                return UNK
            base = n["ch"][0]
            if n.get("arrow"):
                return ("field", ("deref", self.rval(base)), n["v"])
            return ("field", self.lval(base), n["v"])
        if k == "Index":
            base, idx = n["ch"][0], n["ch"][1]
            self.rval(idx)
            return ("elem", ("deref", self.rval(base)))
        if k == "Un" and n.get("v") == "*":
            return ("deref", self.rval(n["ch"][0]))
        self.rval(n)
        return UNK

    def rval(self, n: Optional[dict]) -> tuple:
        if n is None:
            return NONE
        self._note(n)
        k = n["k"]
        ty = n.get("ty") or {}
        if k in ("Lit", "FLit", "Str"):
            return NONE
        if k == "Ref":
            rk = n.get("rk")
            if rk == "f":
                self.info.fn_addrs.add(n["ref"])
                return ("fn", n["ref"])
            if rk == "e":
                if n.get("ref"):
                    self.info.enums.add(n["ref"])
                return NONE
            if rk in ("g", "l", "p") and n.get("ref"):
                lv = ("var", n["ref"])
                if ty.get("k") == "a":
                    return ("addr", lv)
                self.read(lv)
                return ("load", lv)
            return NONE
        if k in ("Member", "Index") or (k == "Un" and n.get("v") == "*"):
            if ty.get("k") == "f" and k == "Un":
                return self.rval(n["ch"][0])
            lv = self.lval(n)
            if ty.get("k") == "a":
                return ("addr", lv)
            self.read(lv)
            return ("load", lv)
        if k == "Un":
            op = n.get("v")
            if not n.get("ch"):
                return NONE
            c = n["ch"][0]
            if op == "&":
                s = strip_casts(c)
                if s.get("k") == "Ref" and s.get("rk") == "f":
                    self.info.fn_addrs.add(s["ref"])
                    return ("fn", s["ref"])
                return ("addr", self.lval(c))
            if op in ("pre++", "pre--", "post++", "post--"):
                lv = self.lval(c)
                self.read(lv)
                self.info.writes.append((lv, True))
                return ("load", lv)
            v = self.rval(c)
            return v if op == "+" else NONE
        if k == "Assign":
            lhs, rhs = n["ch"][0], n["ch"][1]
            v = self.rval(rhs)
            lv = self.lval(lhs)
            self.info.writes.append((lv, True))
            lty = lhs.get("ty") or {}
            if lty.get("k") == "r":
                src = self._lv_of_rhs(rhs)
                if src is not None:
                    self.gb.pts.add_copyrec(lv, src)
            elif ca.is_ptrish(lty):
                self.gb.pts.add_assign(lv, v)
            return v
        if k == "CAssign":
            lhs, rhs = n["ch"][0], n["ch"][1]
            v = self.rval(rhs)
            lv = self.lval(lhs)
            self.read(lv)
            self.info.writes.append((lv, True))
            if ca.is_ptrish(lhs.get("ty")):
                self.gb.pts.add_assign(lv, v)
            return ("load", lv)
        if k in ("ICast", "Cast"):
            return self.rval(n["ch"][0]) if n.get("ch") else NONE
        if k == "Bin":
            ch = n.get("ch", [])
            a = self.rval(ch[0]) if ch else NONE
            b = self.rval(ch[1]) if len(ch) > 1 else NONE
            if n.get("v") in ("+", "-"):
                return vunion([a, b])
            return NONE
        if k == "Comma":
            ch = n.get("ch", [])
            if ch:
                self.rval(ch[0])
            return self.rval(ch[1]) if len(ch) > 1 else NONE
        if k == "Cond":
            ch = n.get("ch", [])
            self.rval(ch[0])
            return vunion([self.rval(ch[1]), self.rval(ch[2])]) if len(ch) == 3 else NONE
        if k == "Call":
            return self.call(n)
        if k in ("InitList", "CompoundLit"):
            return vunion([self.rval(c) for c in n.get("ch", [])])
        for c in n.get("ch", []) or []:
            self.rval(c)
        if k == "Unknown":
            self.info.unresolved.append(f"해석하지 않은 식 {n.get('v')}")
        return NONE

    def _lv_of_rhs(self, rhs: dict) -> Optional[tuple]:
        s = strip_casts(rhs)
        if s.get("k") in ("Ref", "Member", "Index") or (s.get("k") == "Un" and s.get("v") == "*"):
            saved = list(self.reads_stack[-1])
            lv = self.lval(s)
            self.reads_stack[-1][:] = saved
            return lv
        return None

    def call(self, n: dict) -> tuple:
        ch = n.get("ch", [])
        callee_ast = ch[0] if ch else None
        args = ch[1:]
        cs = f"{self.func}#{n['id']}c"
        direct = n.get("ref")
        if direct is None and callee_ast is not None:
            s = strip_casts(callee_ast)
            if s.get("k") == "Ref" and s.get("rk") == "f":
                direct = s["ref"]
        callee_val = NONE
        callee_reads: List[tuple] = []
        if direct is None and callee_ast is not None:
            self.reads_stack.append(callee_reads)
            callee_val = self.rval(callee_ast)
            self.reads_stack.pop()
        arg_infos = []
        for a in args:
            r: List[tuple] = []
            self.reads_stack.append(r)
            v = self.rval(a)
            self.reads_stack.pop()
            arg_infos.append({"ast": a["id"], "reads": r, "val": v, "ty": a.get("ty") or {}})
        ci = CallInfo(cs, n["id"], direct, callee_val, callee_reads, arg_infos, self.node_id, self.func, n)
        self.info.calls.append(ci)
        self.gb.pts.add_call(cs, self.func, direct, callee_val, [x["val"] for x in arg_infos])
        self.read(("callres", cs))
        return ("ret", cs)

    def init(self, lv: tuple, init: dict, ty: dict) -> None:
        """선언 초기화식: 구조체·배열 초기화 목록을 필드 단위 제약으로 분해."""
        s = strip_casts(init)
        if s.get("k") == "InitList":
            fields = s.get("fields")
            for i, c in enumerate(s.get("ch", [])):
                if ty.get("k") == "r" and fields and i < len(fields):
                    fname = fields[i].split(".", 1)[1] if "." in fields[i] else fields[i]
                    self.init(("field", lv, fname), c, c.get("ty") or {})
                elif ty.get("k") == "a":
                    self.init(lv, c, ty.get("el") or {})
                else:
                    self.init(lv, c, c.get("ty") or {})
            return
        v = self.rval(init)
        if ty.get("k") == "r":
            src = self._lv_of_rhs(init)
            if src is not None:
                self.gb.pts.add_copyrec(lv, src)
        elif ca.is_ptrish(ty) or v != NONE:
            self.gb.pts.add_assign(lv, v)


class GraphBuilder:
    def __init__(self, index: ProjectIndex, options: Optional[AnalysisOptions] = None, log=print):
        self.idx = index
        self.opt = options or AnalysisOptions()
        self.log = log
        self.g = AnalysisGraph()
        self.pts = PointsTo(self.opt.pointsto_max_iter)
        self.cfgs: Dict[str, CFG] = {}
        self.infos: Dict[str, Dict[str, NodeInfo]] = {}     # func -> node -> info
        self.owned: Dict[str, Set[str]] = defaultdict(set)   # func -> 소유 지역/매개변수 키
        self.owner_of: Dict[str, str] = {}
        self.functions = {k: u for k, u in index.units.items() if u["kind"] == "function"}
        self.mod: Dict[str, Set[str]] = defaultdict(set)
        self.direct_mod: Dict[str, Set[str]] = defaultdict(set)
        # 매개변수 매개 부작용 요약: 함수 -> {(매개변수 index, 필드 경로)}
        self.pmod: Dict[str, Set[Tuple[int, str]]] = defaultdict(set)
        self.pref: Dict[str, Set[Tuple[int, str]]] = defaultdict(set)
        self.cs_pm_writes: Dict[str, Set[str]] = defaultdict(set)   # actual-out -> 전역 위치
        self.calls_all: List[CallInfo] = []

    # ------------------------------------------------------------------
    def build(self) -> AnalysisGraph:
        self._meta()
        self._symbols()
        self._cfgs_and_exprs()
        self._global_inits()
        self.pts.solve(set(self.functions))
        self._resolve()
        self._mod_summaries()
        for f in self.functions:
            self._intra(f)
        self._interproc()
        self._summary_edges()
        self._global_flow()
        self._symbol_relations()
        self.g.meta["pointsto"] = self.pts.summary()
        self.g.meta["stats"] = self.g.stats()
        return self.g

    # ------------------------------------------------------------------
    def _meta(self) -> None:
        idx = self.idx
        defines = set()
        sample_args = idx.tus[0]["args"] if idx.tus else []
        for t in idx.tus:
            for a in t["args"]:
                if a.startswith("-D"):
                    defines.add(a[2:])
        self.g.meta.update({
            "version": idx.version,
            "root": idx.root,
            "build": {**idx.build.describe(), "defines": sorted(defines), "sample_args": sample_args,
                      "compile_db_origin": sorted({t.get("origin") for t in idx.tus})},
            "summary": idx.summary(),
            "tus": idx.tus,
            "files": {r: {k: v for k, v in f.items() if k in ("is_header", "tus", "inactive", "cond_macros", "includes", "hash")}
                      for r, f in idx.files.items()},
            "macro_uses": idx.macro_uses,
            "fn_decls": idx.fn_decls,
        })
        for d in idx.diags:
            self.g.diag(d["severity"], d.get("code", "clang"), d["message"], file=d.get("file"), line=d.get("line"))
        if not self.pts.converged:
            self.g.diag("warning", "pointsto_not_converged", "points-to 분석이 반복 한도 내에 수렴하지 않음")

    def _symbols(self) -> None:
        g, idx = self.g, self.idx
        for rel, f in idx.files.items():
            g.add_node(f"file:{rel}", NK.FILE, name=rel, file=rel, is_header=f.get("is_header", False),
                       tus=f.get("tus", []), inactive=f.get("inactive", []))
        for rel, f in idx.files.items():
            for inc in f.get("includes", []):
                g.add_edge(f"file:{rel}", f"file:{inc}", EK.INCLUDES)
        kind_map = {"function": NK.FUNCTION, "global": NK.GLOBAL, "record": NK.RECORD, "typedef": NK.TYPEDEF,
                    "enum": NK.ENUM, "macro": NK.MACRO}
        for key, u in idx.units.items():
            nk = kind_map[u["kind"]]
            attrs = {"name": u["name"], "file": u["file"], "line": u["line"], "end_line": u.get("end_line"),
                     "tus": idx.unit_tus.get(key, []), "comment": u.get("comment"), "hash": u.get("hash")}
            if nk == NK.FUNCTION:
                attrs.update(sig=u.get("sig"), static=u.get("static"), params=[p["name"] for p in u.get("params", [])])
            elif nk == NK.GLOBAL:
                attrs.update(ty=u["ty"].get("n"), storage=u.get("storage"), volatile=u.get("volatile"),
                             const=u.get("const"), owner_fn=u.get("owner_fn"))
            elif nk == NK.MACRO:
                attrs.update(body=u.get("body"), value=u.get("value"), fnlike=u.get("fnlike"),
                             include_guard=(not u.get("body") and u["name"] in
                                            set(idx.files.get(u["file"], {}).get("cond_macros", []))))
            elif nk == NK.RECORD:
                attrs.update(size=u.get("size"))
            g.add_node(key, nk, **attrs)
            g.add_edge(f"file:{u['file']}", key, EK.CONTAINS)
            g.asts[key] = u["ast"]
            if nk == NK.RECORD:
                for fld in u.get("fields", []):
                    g.add_node(fld["key"], NK.FIELD, name=fld["name"], record=key, ty=fld["ty"].get("n"),
                               offset_bits=fld.get("offset_bits"), file=u["file"], line=u["line"])
                    g.add_edge(key, fld["key"], EK.HAS_FIELD)
            if nk == NK.ENUM:
                for cname, val in (u.get("constants") or {}).items():
                    g.add_node(f"ec:{cname}", NK.ENUM_CONST, name=cname, value=val, enum=key, file=u["file"])
        for key, info in idx.ref_functions.items():
            if key not in self.functions:
                g.add_node(key, NK.FUNCTION, name=info["name"], file=info.get("file"), external=True,
                           params=[p["name"] for p in info.get("params", [])])
        for key, info in idx.ref_globals.items():
            if key not in idx.units:
                g.add_node(key, NK.GLOBAL, name=info["name"], file=info.get("file"), external=True,
                           ty=(info.get("ty") or {}).get("n"))
                g.diag("warning", "unresolved_global", f"정의를 찾지 못한 전역 변수 {info['name']}", file=info.get("file"))
        defines = {d.split("=", 1)[0] for d in self.g.meta["build"]["defines"]}
        for rel, f in idx.files.items():
            for m in f.get("cond_macros", []):
                mk = f"mc:{m}"
                if not g.has_node(mk):
                    g.add_node(mk, NK.MACRO, name=m, defined=m in defines, config_only=True)
                g.add_edge(f"file:{rel}", mk, EK.CONFIG_DEPENDS)

    # ------------------------------------------------------------------
    def _cfgs_and_exprs(self) -> None:
        for fkey, u in self.functions.items():
            ast = u["ast"]
            for p in u.get("params", []):
                self.owned[fkey].add(p["key"])
                self.owner_of[p["key"]] = fkey
            for n in _walk(ast):
                if n.get("k") == "Var" and not n.get("static") and n.get("ref"):
                    self.owned[fkey].add(n["ref"])
                    self.owner_of[n["ref"]] = fkey
            self.pts.params[fkey] = [p["key"] for p in u.get("params", [])]
            cfg = build_cfg(fkey, ast)
            annotate_anchors(ast, cfg)
            self.cfgs[fkey] = cfg
            infos: Dict[str, NodeInfo] = {}
            for nid, node in cfg.nodes.items():
                info = NodeInfo()
                infos[nid] = info
                a = node.ast
                if a is None or node.kind in ("entry", "exit", "formal_in", "placeholder", "break", "continue",
                                              "goto", "label", "case", "loophead"):
                    continue
                v = ExprVisitor(self, fkey, nid, info)
                if node.kind == "decl":
                    if a.get("ch"):
                        lv = ("var", a["ref"])
                        v.init(lv, a["ch"][0], a.get("ty") or {})
                        info.writes.append((lv, True))
                elif node.kind == "return":
                    if a.get("ch"):
                        val = v.rval(a["ch"][0])
                        info.ret_val = val
                        if ca.is_ptrish((u.get("ret") or {})):
                            self.pts.add_return(fkey, val)
                elif node.kind == "stmt":
                    for c in a.get("ch", []) or []:
                        v.rval(c)
                elif node.kind == "cond":
                    v.rval(a)
                elif node.kind == "asm":
                    info.unresolved.append("인라인 어셈블리")
                self.calls_all.extend(info.calls)
            self.infos[fkey] = infos

    def _global_inits(self) -> None:
        for key, u in self.idx.units.items():
            if u["kind"] != "global" or not u["ast"].get("ch"):
                continue
            info = NodeInfo()
            v = ExprVisitor(self, f"init:{key}", f"loc:{key}", info)
            v.init(("var", key), u["ast"]["ch"][0], u.get("ty") or {})
            self.g.add_node(f"loc:{key}", NK.LOCATION, name=u["name"], root=key, file=u["file"], line=u["line"],
                            init=True)
            if info.fn_addrs:
                self.g.node(f"loc:{key}")["fn_addrs"] = sorted(info.fn_addrs)

    # ------------------------------------------------------------------
    def _param_deref(self, fkey: str, lv: tuple, reassigned: Set[str]) -> Optional[Tuple[int, str]]:
        """LV가 '현재 함수의 포인터 매개변수 역참조(+필드)'이면 (index, 필드 경로)."""
        suffix: List[str] = []
        cur = lv
        while True:
            t = cur[0]
            if t == "field":
                suffix.insert(0, cur[2])
                cur = cur[1]
            elif t == "elem":
                cur = cur[1]
            elif t == "deref":
                val = cur[1]
                if val[0] == "load" and val[1][0] == "var":
                    pk = val[1][1]
                    params = self.pts.params.get(fkey) or []
                    if pk in params and pk not in reassigned:
                        return params.index(pk), FIELD_SEP.join(suffix)
                return None
            else:
                return None

    def _resolve(self) -> None:
        pts = self.pts
        for fkey, infos in self.infos.items():
            params = set(self.pts.params.get(fkey) or [])
            reassigned = set()
            for info in infos.values():
                for lv, _ in info.writes:
                    if lv[0] == "var" and lv[1] in params:
                        reassigned.add(lv[1])
            for nid, info in infos.items():
                for item in info.reads:
                    if item[0] == "callres":
                        continue
                    locs = pts.resolve_lv(item)
                    if not locs and item != UNK and _has_deref(item):
                        info.unresolved.append("대상을 알 수 없는 포인터 역참조(읽기)")
                    info.r_reads |= locs
                    pm = self._param_deref(fkey, item, reassigned)
                    if pm is not None:
                        self.pref[fkey].add(pm)
                        info.pm_r |= {l for l in locs if loc_root(l) not in self.owned[fkey]}
                for lv, strong in info.writes:
                    locs = pts.resolve_lv(lv)
                    if not locs and lv != UNK:
                        info.unresolved.append("대상을 알 수 없는 포인터 역참조(쓰기)")
                    st = strong and _is_direct_lv(lv) and len(locs) == 1
                    for l in locs:
                        info.r_writes.append((l, st))
                    pm = self._param_deref(fkey, lv, reassigned)
                    if pm is not None:
                        self.pmod[fkey].add(pm)
                        info.pm_w |= {l for l in locs if loc_root(l) not in self.owned[fkey]}
                for l, _ in info.r_writes:
                    if loc_root(l) not in self.owned[fkey] and l not in info.pm_w:
                        self.direct_mod[fkey].add(l)
        self._propagate_param_effects()

    def _propagate_param_effects(self) -> None:
        """f(p){ g(p); } 처럼 포인터 매개변수를 그대로 넘기면 g의 매개변수 부작용을 f의 것으로 승격."""
        changed = True
        rounds = 0
        while changed and rounds < 50:
            changed = False
            rounds += 1
            for ci in self.calls_all:
                f = ci.func
                params = self.pts.params.get(f) or []
                for c in self.callees_of(ci):
                    for table in (self.pmod, self.pref):
                        for i, suf in list(table.get(c, ())):
                            if i >= len(ci.args):
                                continue
                            v = ci.args[i]["val"]
                            if v[0] == "load" and v[1][0] == "var" and v[1][1] in params:
                                k = params.index(v[1][1])
                                if (k, suf) not in table[f]:
                                    table[f].add((k, suf))
                                    changed = True

    def _pm_targets(self, ci: CallInfo, callee: str, table: Dict[str, Set[Tuple[int, str]]]) -> Set[str]:
        out: Set[str] = set()
        for i, suf in table.get(callee, ()):
            if i < len(ci.args):
                for t in self.pts.targets_of(ci.args[i]["val"]):
                    if not t.startswith("fn:"):
                        out.add(f"{t}{FIELD_SEP}{suf}" if suf else t)
        return out

    def callees_of(self, ci: CallInfo) -> Set[str]:
        return set(self.pts.callees.get(ci.cs, set()))

    def _mod_summaries(self) -> None:
        calls_by_fn: Dict[str, List[CallInfo]] = defaultdict(list)
        for ci in self.calls_all:
            calls_by_fn[ci.func].append(ci)
        for f in self.functions:
            self.mod[f] = set(self.direct_mod[f])
        changed = True
        rounds = 0
        while changed and rounds < 100:
            changed = False
            rounds += 1
            for f in self.functions:
                acc = set(self.mod[f])
                for ci in calls_by_fn.get(f, []):
                    for c in self.callees_of(ci):
                        acc |= self.mod.get(c, set())
                acc = {l for l in acc if loc_root(l) not in self.owned[f]}
                if acc != self.mod[f]:
                    self.mod[f] = acc
                    changed = True

    def _ext_out_locs(self, ci: CallInfo) -> Set[str]:
        """본문이 없는 외부 함수: const가 아닌 포인터 인자가 가리키는 위치에 쓴다고 가정."""
        out: Set[str] = set()
        for a in ci.args:
            ty = a["ty"]
            if ty.get("k") == "p" and not (ty.get("pt") or {}).get("const") and (ty.get("pt") or {}).get("k") != "f":
                out |= {x for x in self.pts.targets_of(a["val"]) if not x.startswith("fn:")}
        return out

    # ------------------------------------------------------------------
    def _intra(self, fkey: str) -> None:
        g, cfg, infos = self.g, self.cfgs[fkey], self.infos[fkey]
        u = self.functions[fkey]
        idx = self.idx
        fn_file = u["file"]
        # 노드 생성
        for nid, node in cfg.nodes.items():
            if node.kind == "placeholder":
                continue
            if node.kind == "entry":
                g.add_node(nid, NK.ENTRY, func=fkey, label="ENTRY", file=fn_file, line=u["line"],
                           text=f"{u['name']}() 진입")
            elif node.kind == "exit":
                g.add_node(nid, NK.EXIT, func=fkey, label="EXIT", file=fn_file, line=u.get("end_line"))
            elif node.kind == "formal_in":
                p = node.extra["param"]
                g.add_node(nid, NK.FORMAL_IN, func=fkey, label=f"param:{p['name']}", file=fn_file,
                           line=u["line"], index=p["index"], text=f"매개변수 {p['name']}")
            else:
                a = node.ast or {}
                f = a.get("f") or fn_file
                text = idx.snippet(f, a.get("l", 0), a.get("c", 1), a.get("el"), a.get("ec"))
                if node.kind == "loophead":
                    text = "for (;;)"
                info = infos.get(nid)
                g.add_node(nid, NK.STMT, func=fkey, cfg_kind=node.kind, file=f, line=a.get("l"),
                           end_line=a.get("el"), text=text, ast_id=a.get("id"),
                           unreachable=nid in cfg.unreachable or None,
                           volatile=(info.volatile if info else None) or None,
                           has_call=bool(info and info.calls) or None,
                           switch=node.extra.get("switch"), loop=node.extra.get("loop"))
            g.add_edge(fkey, nid, EK.CONTAINS)
        for a, succs in cfg.succ.items():
            if a not in g.g:
                continue
            for b, lab in succs:
                if b in g.g:
                    g.add_edge(a, b, EK.CFG, lab, fake=(a, b) in cfg.fake_edges or None)
        # 정의/사용
        defs: Dict[str, List[Tuple[str, bool]]] = defaultdict(list)
        uses: Dict[str, Set[str]] = defaultdict(set)
        at: Dict[str, str] = {}
        for nid, node in cfg.nodes.items():
            if node.kind == "formal_in":
                defs[nid].append((node.extra["param"]["key"], True))
            info = infos.get(nid)
            if not info:
                continue
            defs[nid].extend(info.r_writes)
            for ci in info.calls:
                callees = self.callees_of(ci)
                for c in callees:
                    if c in self.functions:
                        for l in self.mod.get(c, ()):
                            if loc_root(l) in self.owned[fkey]:
                                defs[nid].append((l, False))
                        pmw = self._pm_targets(ci, c, self.pmod)
                        for l in pmw:
                            defs[nid].append((l, False))
                        self.cs_pm_writes[f"{ci.cs}:out"] |= {l for l in pmw if loc_root(l).startswith("gv:")}
                    else:
                        for l in self._ext_out_locs(ci):
                            defs[nid].append((l, False))
                if not callees:
                    for l in self._ext_out_locs(ci):
                        defs[nid].append((l, False))
            # 문장 수준 읽기(인자 읽기는 actual-in 노드로 분리)
            for item in info.reads:
                if item[0] != "callres":
                    uses[nid] |= self.pts.resolve_lv(item)
            for ci in info.calls:
                for i, arg in enumerate(ci.args):
                    ai = f"{ci.cs}:in{i}"
                    s: Set[str] = set()
                    for item in arg["reads"]:
                        if item[0] != "callres":
                            s |= self.pts.resolve_lv(item)
                    # 포인터 인자로 전달된 호출자 지역 변수(피호출 함수가 읽을 수 있음)
                    if arg["ty"].get("k") == "p":
                        s |= {x for x in self.pts.targets_of(arg["val"]) if loc_root(x) in self.owned[fkey]}
                    # 피호출 함수가 이 포인터 인자를 통해 읽는 위치(문맥별로 해석)
                    for c in self.callees_of(ci):
                        for ii, suf in self.pref.get(c, ()):
                            if ii == i:
                                for t in self.pts.targets_of(arg["val"]):
                                    if not t.startswith("fn:"):
                                        s.add(f"{t}{FIELD_SEP}{suf}" if suf else t)
                    uses[ai] = s
                    at[ai] = nid
                if ci.callee_reads:
                    cv = ci.cs
                    s2: Set[str] = set()
                    for item in ci.callee_reads:
                        if item[0] != "callres":
                            s2 |= self.pts.resolve_lv(item)
                    uses[cv] = s2
                    at[cv] = nid
        rd = ReachingDefs(cfg, defs)
        self._rd_uses = getattr(self, "_rd_uses", {})
        self._rd_uses[fkey] = (uses, at)
        for d, user, loc in def_use_edges(rd, uses, at):
            if d in g.g:
                g.add_edge(d, user, EK.DATA, _short_loc(loc))
        # 제어 의존성
        if getattr(self.opt, "control_dependence", "ntscd") == "classic":
            cd = control_dependence(cfg, postdominators(cfg))
        else:
            cd = ntscd(cfg)
            # 어떤 분기에도 의존하지 않는 노드는 함수 진입에 제어 의존(호출 정점 -> 진입 -> 본문)
            for nid, node in cfg.nodes.items():
                if node.kind in ("placeholder", "entry", "exit") or cd.get(nid):
                    continue
                cd[nid].add((cfg.entry, ""))
        self._cd = getattr(self, "_cd", {})
        self._cd[fkey] = cd
        for n, ctrls in cd.items():
            if n not in g.g or cfg.nodes[n].kind in ("placeholder",):
                continue
            for c, lab in ctrls:
                if c in g.g:
                    g.add_edge(c, n, EK.CONTROL, lab)
        # 반환값 -> formal-out
        ret = u.get("ret") or {}
        if ret.get("k") not in ("v", None):
            fo = f"{fkey}#fo_ret"
            g.add_node(fo, NK.FORMAL_OUT, func=fkey, label="return", file=fn_file, line=u["line"],
                       text=f"{u['name']}() 반환값")
            g.add_edge(fkey, fo, EK.CONTAINS)
            for nid, node in cfg.nodes.items():
                if node.kind == "return" and node.ast and node.ast.get("ch"):
                    g.add_edge(nid, fo, EK.DATA, "return")
        fom = f"{fkey}#fo_mem"
        g.add_node(fom, NK.FORMAL_OUT, func=fkey, label="memory", file=fn_file, line=u["line"],
                   text=f"{u['name']}() 포인터를 통한 부작용")
        g.add_edge(fkey, fom, EK.CONTAINS)
        for nid, info in infos.items():
            for l, _ in info.r_writes:
                r = loc_root(l)
                if r not in self.owned[fkey] and (not r.startswith("gv:") or l in info.pm_w):
                    g.add_edge(nid, fom, EK.DATA, _short_loc(l))
            for msg in sorted(set(info.unresolved)):
                g.diag("info", "incomplete", msg, node=nid, func=fkey,
                       file=g.node(nid).get("file") if nid in g.g else None,
                       line=g.node(nid).get("line") if nid in g.g else None)

    # ------------------------------------------------------------------
    def _interproc(self) -> None:
        g = self.g
        for ci in self.calls_all:
            s = ci.node
            fkey = ci.func
            u = self.functions[fkey]
            sn = g.node(s)
            callees = self.callees_of(ci)
            names = sorted(self.idx.units.get(c, {}).get("name") or c.split(":")[-1] for c in callees)
            a = ci.ast
            text = self.idx.snippet(a.get("f") or u["file"], a.get("l", 0), a.get("c", 1), a.get("el"), a.get("ec"))
            g.add_node(ci.cs, NK.CALL_SITE, func=fkey, file=sn.get("file"), line=a.get("l"), text=text,
                       callees=sorted(callees), indirect=ci.direct is None or None, stmt=s, ast_id=ci.ast_id)
            g.add_edge(fkey, ci.cs, EK.CONTAINS)
            ao = f"{ci.cs}:out"
            g.add_node(ao, NK.ACTUAL_OUT, func=fkey, file=sn.get("file"), line=a.get("l"),
                       text=f"{'/'.join(names) or '?'}() 결과", call_site=ci.cs)
            g.add_edge(ci.cs, ao, EK.CONTROL, "call")
            g.add_edge(ao, s, EK.DATA, "call-result")
            for c, lab in self._cd.get(fkey, {}).get(s, ()):
                g.add_edge(c, ci.cs, EK.CONTROL, lab)
            ai_ids = []
            for i, arg in enumerate(ci.args):
                ai = f"{ci.cs}:in{i}"
                g.add_node(ai, NK.ACTUAL_IN, func=fkey, file=sn.get("file"), line=a.get("l"), index=i,
                           text=f"{'/'.join(names) or '?'}() 인자 {i}", call_site=ci.cs, ast_id=arg["ast"])
                ai_ids.append(ai)
                for item in arg["reads"]:
                    if item[0] == "callres":
                        g.add_edge(f"{item[1]}:out", ai, EK.DATA, "call-result")
            if ci.direct is None and not callees:
                g.diag("warning", "unresolved_indirect_call", f"간접 호출 대상을 찾지 못함: {text}",
                       node=ci.cs, func=fkey, file=sn.get("file"), line=a.get("l"))
            if ci.direct is None and callees:
                g.node(ci.cs)["resolved_by"] = "points-to"
            ext = False
            for c in sorted(callees):
                if c in self.functions:
                    g.add_edge(ci.cs, f"{c}#entry", EK.CALL)
                    nparams = len(self.functions[c].get("params", []))
                    for i, ai in enumerate(ai_ids):
                        if i < nparams:
                            g.add_edge(ai, f"{c}#fi{i}", EK.PARAM_IN)
                    if g.has_node(f"{c}#fo_ret"):
                        g.add_edge(f"{c}#fo_ret", ao, EK.PARAM_OUT, "return")
                    g.add_edge(f"{c}#fo_mem", ao, EK.PARAM_OUT, "memory")
                else:
                    ext = True
            if ext or not callees:
                g.node(ci.cs)["external"] = True
                for ai in ai_ids:
                    g.add_edge(ai, ao, EK.SUMMARY, "external")
        for ci in self.calls_all:
            for item in ci.callee_reads:
                if item[0] == "callres":
                    g.add_edge(f"{item[1]}:out", ci.cs, EK.DATA, "call-result")

    def _summary_edges(self) -> None:
        """HRB 요약 간선: formal-in에서 formal-out으로의 함수 내부 도달성을 고정점으로 계산."""
        g = self.g
        intra_kinds = {EK.DATA, EK.CONTROL, EK.SUMMARY}
        calls_to: Dict[str, List[CallInfo]] = defaultdict(list)
        for ci in self.calls_all:
            for c in self.callees_of(ci):
                if c in self.functions:
                    calls_to[c].append(ci)
        summ: Dict[str, Set[Tuple[int, str]]] = defaultdict(set)
        work = deque(self.functions)
        inq = set(work)
        rounds = 0
        while work and rounds < 100000:
            rounds += 1
            f = work.popleft()
            inq.discard(f)
            nparams = len(self.functions[f].get("params", []))
            outs = {f"{f}#fo_ret", f"{f}#fo_mem"}
            found: Set[Tuple[int, str]] = set()
            for i in range(nparams):
                start = f"{f}#fi{i}"
                seen = {start}
                q = deque([start])
                while q:
                    n = q.popleft()
                    for _, d, data in g.out_edges(n, intra_kinds):
                        if d in seen:
                            continue
                        if g.func_of(d) != f:
                            continue
                        seen.add(d)
                        q.append(d)
                for o in outs:
                    if o in seen:
                        found.add((i, o))
            if found - summ[f]:
                summ[f] |= found
                for ci in calls_to.get(f, []):
                    for i, o in found:
                        if i < len(ci.args):
                            g.add_edge(f"{ci.cs}:in{i}", f"{ci.cs}:out", EK.SUMMARY, o.rsplit("#", 1)[1])
                    if ci.func not in inq:
                        work.append(ci.func)
                        inq.add(ci.func)
        self.g.meta["summary_rounds"] = rounds

    def _global_flow(self) -> None:
        g = self.g
        writers: Dict[str, Set[str]] = defaultdict(set)   # loc -> nodes
        readers: Dict[str, Set[str]] = defaultdict(set)
        for fkey, infos in self.infos.items():
            uses, at = self._rd_uses[fkey]
            for nid, info in infos.items():
                for l, _ in info.r_writes:
                    if loc_root(l).startswith("gv:") and l not in info.pm_w:
                        writers[l].add(nid)
            for user, locs in uses.items():
                skip = infos[user].pm_r if user in infos else set()
                for l in locs:
                    if loc_root(l).startswith("gv:") and l not in skip:
                        readers[l].add(user)
        for ao, locs in self.cs_pm_writes.items():
            for l in locs:
                writers[l].add(ao)
        by_root: Dict[str, Set[str]] = defaultdict(set)
        for l in set(writers) | set(readers):
            by_root[loc_root(l)].add(l)
        for root, locs in by_root.items():
            gu = self.idx.units.get(root, {})
            for l in locs:
                nid = f"loc:{l}"
                g.add_node(nid, NK.LOCATION, name=_short_loc(l), root=root, loc=l, file=gu.get("file"),
                           line=gu.get("line"), volatile=gu.get("volatile") or None)
            if g.has_node(f"loc:{root}") and root not in locs:
                locs = locs | {root}
            for l in locs:
                for w in writers.get(l, ()):
                    if g.has_node(w):
                        g.add_edge(w, f"loc:{l}", EK.GLOBAL_WRITE, _short_loc(l))
            for lr in locs:
                for r in readers.get(lr, ()):
                    if not g.has_node(r):
                        continue
                    for lw in locs:
                        if loc_may_alias(lw, lr):
                            g.add_edge(f"loc:{lw}", r, EK.GLOBAL_READ, _short_loc(lr))
            # 위치 노드 사이 포함 관계(전체 <-> 필드)도 연결해 위치 노드 기준 관찰이 가능하게 함
            for a in locs:
                for b in locs:
                    if a != b and b.startswith(a + FIELD_SEP):
                        g.add_edge(f"loc:{b}", f"loc:{a}", EK.GLOBAL_READ, "field-of")

    def _symbol_relations(self) -> None:
        g = self.g
        for ci in self.calls_all:
            for c in self.callees_of(ci):
                if not g.has_node(c):
                    g.add_node(c, NK.FUNCTION, name=c.split(":")[-1], external=True)
                g.add_edge(ci.func, c, EK.CALLS, "indirect" if ci.direct is None else "direct", call_site=ci.cs)
        for fkey, infos in self.infos.items():
            reads: Set[str] = set()
            writes: Set[str] = set()
            for nid, info in infos.items():
                for l, _ in info.r_writes:
                    if loc_root(l).startswith("gv:"):
                        writes.add(loc_root(l))
                for m in info.macros:
                    mk = f"mc:{m}"
                    if g.has_node(mk):
                        g.add_edge(nid, mk, EK.USES_MACRO)
                        g.add_edge(fkey, mk, EK.USES_MACRO)
                for fld in info.fields:
                    if g.has_node(fld):
                        g.add_edge(nid, fld, EK.USES_FIELD)
                        g.add_edge(fkey, fld, EK.USES_FIELD)
                        rec = g.node(fld).get("record")
                        if rec:
                            g.add_edge(fkey, rec, EK.USES_TYPE)
                for e in info.enums:
                    if g.has_node(e):
                        g.add_edge(nid, e, EK.USES_ENUM)
                        g.add_edge(fkey, e, EK.USES_ENUM)
                for fa in info.fn_addrs:
                    if fa != fkey:
                        g.add_edge(fkey, fa, EK.ADDR_TAKEN)
            uses, _ = self._rd_uses[fkey]
            for user, locs in uses.items():
                for l in locs:
                    if loc_root(l).startswith("gv:"):
                        reads.add(loc_root(l))
            for gk in reads:
                if g.has_node(gk):
                    g.add_edge(fkey, gk, EK.READS)
            for gk in writes:
                if g.has_node(gk):
                    g.add_edge(fkey, gk, EK.WRITES)
            # 자료형 사용: 변수 선언 자료형이 레코드이면 연결
            for n in _walk(self.functions[fkey]["ast"]):
                ty = n.get("ty") or {}
                rec = ty.get("rec") or (ty.get("pt") or {}).get("rec")
                if rec and g.has_node(f"ty:{rec}"):
                    g.add_edge(fkey, f"ty:{rec}", EK.USES_TYPE)
        for key, u in self.idx.units.items():
            if u["kind"] == "global":
                for n in _walk(u["ast"]):
                    if n.get("mac") and g.has_node(f"mc:{n['mac']}"):
                        g.add_edge(key, f"mc:{n['mac']}", EK.USES_MACRO)
                    if n.get("k") == "Ref" and n.get("rk") == "f":
                        g.add_edge(key, n["ref"], EK.ADDR_TAKEN)
                rec = (u.get("ty") or {}).get("rec") or ((u.get("ty") or {}).get("el") or {}).get("rec")
                if rec and g.has_node(f"ty:{rec}"):
                    g.add_edge(key, f"ty:{rec}", EK.USES_TYPE)


def _short_loc(l: str) -> str:
    root, _, rest = l.partition(FIELD_SEP)
    name = root.split(":")[-1]
    return name + ("." + rest.replace(FIELD_SEP, ".") if rest else "")


def _walk(n: dict):
    stack = [n]
    while stack:
        x = stack.pop()
        yield x
        for c in x.get("ch", []) or []:
            if c is not None:
                stack.append(c)


def build_graph(index: ProjectIndex, options: Optional[AnalysisOptions] = None, log=print) -> AnalysisGraph:
    return GraphBuilder(index, options, log).build()
