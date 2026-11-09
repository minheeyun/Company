"""변경 전후 요소 대응 및 변경점 추출.

* 대응: 심벌 키(USR 기반) -> 파일 이동/이름 변경(AST 구조 유사도) -> AST 노드(GumTree)
* 변경 유형 분류: 임계값·비교 연산자·조건·대입·호출·지역변수·흐름(return/break/...)·순서·
  시그니처·전역·구조체 레이아웃·typedef·enum·매크로/헤더·조건부 컴파일·태스크 설정
* 그래프 앵커: 변경된 AST 노드가 속한 CFG 노드/호출 정점/actual-in을 변경 전·후 그래프에서
  각각 기록한다. 삭제는 변경 전 그래프, 추가는 변경 후 그래프, 수정은 양쪽에 앵커를 둔다.
* 노드 대응표(node_map): 변경 전 그래프 노드 -> 변경 후 그래프 노드. 두 그래프의 간선을
  합치지 않고, 결과를 연결할 때만 사용한다.
"""
from __future__ import annotations

import os
from collections import defaultdict
from typing import Dict, List, Optional, Set, Tuple

from .ast_diff import Mapping, Tree, diff, similarity
from .model import EK, NK, AnalysisGraph, walk_ast

COMPARISON = {"<", "<=", ">", ">=", "==", "!="}
LOGICAL = {"&&", "||"}
STMT_KINDS = {"ExprStmt", "Var", "Return", "If", "While", "Do", "For", "Switch", "Case", "Default", "Break",
              "Continue", "Goto", "Label", "Asm", "Null", "DeclStmt"}
FLOW_KINDS = {"Return", "Break", "Continue", "Goto"}

# 변경 유형: 코드 -> (한글 이름, 영향 추적 시작 범주). 목록 순서가 묶음 대표 유형의 우선순위.
CHANGE_TYPES = {
    "early_exit_added": ("조기 반환·탈출 추가", "flow"),
    "early_exit_removed": ("조기 반환·탈출 삭제", "flow"),
    "control_flow_change": ("return/break/continue/goto 변경", "flow"),
    "loop_added": ("반복문 추가", "flow"),
    "loop_removed": ("반복문 삭제", "flow"),
    "branch_added": ("조건문 추가", "control"),
    "branch_removed": ("조건문 삭제", "control"),
    "comparison_operator_change": ("비교 연산자 변경", "control"),
    "threshold_change": ("조건식 임계값 변경", "control"),
    "logic_operator_change": ("논리 연산자 변경(단락 평가)", "control"),
    "short_circuit_order_change": ("단락 평가 순서 변경", "control"),
    "condition_change": ("조건식 변경", "control"),
    "call_added": ("함수 호출 추가", "call"),
    "call_removed": ("함수 호출 삭제", "call"),
    "callee_change": ("호출 대상 변경", "call"),
    "assignment_added": ("대입문 추가", "data"),
    "assignment_removed": ("대입문 삭제", "data"),
    "local_var_added": ("지역변수 추가", "decl"),
    "local_var_removed": ("지역변수 삭제", "decl"),
    "local_type_change": ("지역변수 자료형 변경", "data"),
    "value_change": ("대입·계산식 변경", "data"),
    "reference_change": ("참조 변수 변경", "data"),
    "expression_change": ("식 구조 변경", "data"),
    "cast_change": ("형변환 변경", "data"),
    "statement_added": ("실행문 추가", "data"),
    "statement_removed": ("실행문 삭제", "data"),
    "statement_reorder": ("실행문 순서 변경", "order"),
    "code_moved": ("코드 이동", "data"),
    "function_added": ("함수 추가", "call"),
    "function_removed": ("함수 삭제", "call"),
    "function_renamed": ("함수 이름 변경·이동", "call"),
    "signature_change": ("인자·반환형 변경", "interface"),
    "global_added": ("전역변수 추가", "decl"),
    "global_removed": ("전역변수 삭제", "decl"),
    "global_type_change": ("전역변수 자료형 변경", "type"),
    "global_init_change": ("전역변수 초기값 변경", "data"),
    "qualifier_change": ("저장 클래스·한정자 변경", "type"),
    "type_layout_change": ("구조체 필드·메모리 배치 변경", "type"),
    "typedef_change": ("typedef 자료형 변경", "type"),
    "enum_value_change": ("열거형 값 변경", "data"),
    "macro_change": ("매크로·헤더 변경", "macro"),
    "macro_added": ("매크로 추가", "macro"),
    "macro_removed": ("매크로 삭제", "macro"),
    "conditional_compilation_change": ("조건부 컴파일 구간 변경", "config"),
    "task_config_change": ("태스크 주기·이벤트 설정 변경", "timing"),
    "signal_definition_change": ("통신 신호 정의 변경", "interface"),
}
_PRIORITY = {k: i for i, k in enumerate(CHANGE_TYPES)}


class SourceView:
    def __init__(self, root: str):
        self.root = root
        self.cache: Dict[str, List[str]] = {}

    def lines(self, rel: Optional[str]) -> List[str]:
        if not rel:
            return []
        if rel not in self.cache:
            try:
                with open(os.path.join(self.root, rel), "r", encoding="utf-8", errors="replace") as f:
                    self.cache[rel] = f.read().split("\n")
            except OSError:
                self.cache[rel] = []
        return self.cache[rel]

    def get(self, rel: Optional[str], l1: Optional[int], l2: Optional[int] = None, ctx: int = 0) -> str:
        ls = self.lines(rel)
        if not ls or not l1:
            return ""
        a = max(1, l1 - ctx)
        b = min(len(ls), (l2 or l1) + ctx)
        if b - a > 25:
            b = a + 25
        return "\n".join(ls[a - 1:b])


def _cond_child(n: dict) -> Optional[int]:
    k = n.get("k")
    ch = n.get("ch") or []
    if k in ("If", "While", "Switch") and ch:
        return ch[0]["id"]
    if k == "Do" and len(ch) > 1:
        return ch[1]["id"]
    if k == "For" and (n.get("has") or {}).get("cond"):
        return ch[1 if (n.get("has") or {}).get("init") else 0]["id"]
    return None


class FuncDiffContext:
    def __init__(self, unit_old: str, unit_new: str, t1: Tree, t2: Tree, m: Mapping):
        self.uo, self.un = unit_old, unit_new
        self.t1, self.t2, self.m = t1, t2, m

    def stmt_anc(self, t: Tree, i: int) -> int:
        x: Optional[int] = i
        while x is not None:
            if t.kind(x) in STMT_KINDS:
                return x
            x = t.parent.get(x)
        return i

    def in_condition(self, t: Tree, i: int) -> bool:
        x: Optional[int] = i
        while x is not None:
            p = t.parent.get(x)
            if p is None:
                return False
            if t.kind(p) in ("If", "While", "Do", "Switch", "For"):
                return _cond_child(t.nodes[p]) == x
            if t.kind(p) in STMT_KINDS:
                return False
            x = p
        return False

    def anchors(self, t: Tree, unit: str, i: int, subtree: bool) -> Set[str]:
        out: Set[str] = set()
        nodes = [i] + (list(t.desc[i]) if subtree else [])
        for x in nodes:
            n = t.nodes[x]
            if n.get("a"):
                out.add(n["a"])
            if n.get("k") == "Call":
                out.add(f"{unit}#{x}c")
        # 가장 가까운 호출의 인자 안에 있으면 actual-in
        x = i
        while x is not None:
            p = t.parent.get(x)
            if p is None:
                break
            if t.kind(p) == "Call":
                idx = t.children[p].index(x)
                if idx >= 1:
                    out.add(f"{unit}#{p}c:in{idx - 1}")
                else:
                    out.add(f"{unit}#{p}c")
                break
            if t.kind(p) in STMT_KINDS:
                break
            x = p
        return out


def _has_side_effect(t: Tree, i: int) -> List[str]:
    reasons = []
    for x in [i] + list(t.desc[i]):
        n = t.nodes[x]
        k = n.get("k")
        if k == "Call":
            reasons.append(f"함수 호출 {n.get('v') or '(간접)'}()")
        elif k in ("Assign", "CAssign"):
            reasons.append("대입")
        elif k == "Un" and n.get("v") in ("pre++", "pre--", "post++", "post--"):
            reasons.append("증감 연산")
        elif (n.get("ty") or {}).get("vol") and k == "Ref":
            reasons.append(f"volatile 접근 {n.get('v')}")
    return sorted(set(reasons))


def _refs(t: Tree, i: int) -> List[str]:
    out = set()
    for x in [i] + list(t.desc[i]):
        n = t.nodes[x]
        if n.get("k") == "Ref" and n.get("rk") in ("g", "l", "p"):
            out.add(n.get("v"))
        if n.get("k") == "Member":
            out.add(n.get("v"))
    return sorted(out)


class ChangeDetector:
    def __init__(self, g_old: AnalysisGraph, g_new: AnalysisGraph):
        self.go, self.gn = g_old, g_new
        self.src_old = SourceView(g_old.meta.get("root", ""))
        self.src_new = SourceView(g_new.meta.get("root", ""))
        self.changes: List[dict] = []
        self.unit_map: Dict[str, str] = {}
        self.node_map: Dict[str, str] = {}
        self.func_ctx: Dict[str, FuncDiffContext] = {}
        self.file_moves: Dict[str, str] = {}
        self.changed_macros: Set[str] = set()
        self.notes: List[str] = []

    # ------------------------------------------------------------------
    def run(self) -> dict:
        self._match_units()
        self._macros()
        for uo, un in sorted(self.unit_map.items()):
            k = self.go.kind(uo)
            if k == NK.FUNCTION:
                self._function(uo, un)
            elif k == NK.GLOBAL:
                self._global(uo, un)
            elif k == NK.RECORD:
                self._record(uo, un)
            elif k == NK.TYPEDEF:
                self._typedef(uo, un)
            elif k == NK.ENUM:
                self._enum(uo, un)
        self._added_removed()
        self._conditional_compilation()
        self._vehicle_config()
        self._finalize()
        return {
            "changes": self.changes,
            "unit_map": self.unit_map,
            "node_map_size": len(self.node_map),
            "file_moves": self.file_moves,
            "notes": self.notes,
        }

    def _add(self, ctype: str, unit_old: Optional[str], unit_new: Optional[str], summary: str,
             old: Optional[dict] = None, new: Optional[dict] = None, **extra) -> dict:
        label, cat = CHANGE_TYPES[ctype]
        ch = {
            "id": "", "type": ctype, "type_label": label, "category": cat,
            "unit_old": unit_old, "unit_new": unit_new,
            "unit_name": (self.go.node(unit_old).get("name") if unit_old and self.go.has_node(unit_old)
                          else self.gn.node(unit_new).get("name") if unit_new and self.gn.has_node(unit_new) else None),
            "summary": summary,
            "old": old or {"anchors": []}, "new": new or {"anchors": []},
        }
        ch.update(extra)
        for side in ("old", "new"):
            ch[side]["anchors"] = sorted(set(ch[side].get("anchors", [])))
        self.changes.append(ch)
        return ch

    # ------------------------------------------------------------------
    def _units(self, g: AnalysisGraph) -> Dict[str, str]:
        out = {}
        for key in g.asts:
            k = g.kind(key)
            if k in (NK.FUNCTION, NK.GLOBAL, NK.RECORD, NK.TYPEDEF, NK.ENUM, NK.MACRO):
                out[key] = k
        return out

    def _match_units(self) -> None:
        uo, un = self._units(self.go), self._units(self.gn)
        for k in uo:
            if k in un and uo[k] == un[k]:
                self.unit_map[k] = k
        # 파일 이동: 사라진 파일과 새 파일을 심벌 이름 집합 유사도로 대응
        fo = {self.go.node(f)["name"] for f in self.go.nodes_of(NK.FILE)}
        fn = {self.gn.node(f)["name"] for f in self.gn.nodes_of(NK.FILE)}
        gone, born = sorted(fo - fn), sorted(fn - fo)

        def names(g, rel):
            return {g.node(s).get("name") for s in g.succ(f"file:{rel}", [EK.CONTAINS])}
        for a in gone:
            best, bs = None, 0.0
            na = names(self.go, a)
            for b in born:
                nb = names(self.gn, b)
                s = len(na & nb) / max(1, len(na | nb))
                if s > bs:
                    best, bs = b, s
            if best and bs >= 0.5:
                self.file_moves[a] = best
                self.notes.append(f"파일 이동/이름 변경 추정: {a} -> {best} (심벌 유사도 {bs:.2f})")
        # static 심벌 키의 파일 경로 치환, 이름 변경된 함수(구조 유사도)
        rest_o = [k for k in uo if k not in self.unit_map]
        rest_n = {k for k in un if k not in self.unit_map.values()}
        for k in list(rest_o):
            for a, b in self.file_moves.items():
                k2 = k.replace(f":{a}:", f":{b}:")
                if k2 != k and k2 in rest_n and uo[k] == un[k2]:
                    self.unit_map[k] = k2
                    rest_n.discard(k2)
                    rest_o.remove(k)
                    break
        for k in list(rest_o):
            if uo[k] != NK.FUNCTION:
                continue
            best, bs = None, 0.0
            for k2 in rest_n:
                if un[k2] != NK.FUNCTION:
                    continue
                if len(self.go.node(k).get("params") or []) != len(self.gn.node(k2).get("params") or []):
                    continue
                s = similarity(self.go.asts[k], self.gn.asts[k2])
                if s > bs:
                    best, bs = k2, s
            if best and bs >= 0.75:
                self.unit_map[k] = best
                rest_n.discard(best)
                self._add("function_renamed", k, best,
                          f"{self.go.node(k)['name']} -> {self.gn.node(best)['name']} (구조 유사도 {bs:.2f})",
                          {"anchors": self._fn_boundary(self.go, k)}, {"anchors": self._fn_boundary(self.gn, best)})
        # 노드 대응표: 함수 경계 노드, 전역 위치
        for a, b in self.unit_map.items():
            if self.go.kind(a) == NK.FUNCTION:
                for suf in ("#entry", "#exit", "#fo_ret", "#fo_mem"):
                    if self.go.has_node(a + suf) and self.gn.has_node(b + suf):
                        self.node_map[a + suf] = b + suf
                i = 0
                while self.go.has_node(f"{a}#fi{i}") and self.gn.has_node(f"{b}#fi{i}"):
                    self.node_map[f"{a}#fi{i}"] = f"{b}#fi{i}"
                    i += 1
                self.node_map[a] = b
            elif self.go.kind(a) == NK.GLOBAL:
                self.node_map[a] = b
        for l in self.go.nodes_of(NK.LOCATION):
            root = self.go.node(l).get("root")
            if root in self.unit_map:
                l2 = l.replace(root, self.unit_map[root], 1)
                if self.gn.has_node(l2):
                    self.node_map[l] = l2
        for n in self.go.nodes_of(NK.SIGNAL, NK.TASK, NK.FEATURE, NK.OBSERVATION, NK.REQUIREMENT):
            if self.gn.has_node(n):
                self.node_map[n] = n

    def _fn_boundary(self, g: AnalysisGraph, f: str) -> List[str]:
        out = [f"{f}#entry"]
        out += [n for n in g.nodes_of(NK.CALL_SITE) if f in (g.node(n).get("callees") or [])]
        return [x for x in out if g.has_node(x)]

    # ------------------------------------------------------------------
    def _function(self, uo: str, un: str) -> None:
        a, b = self.go.asts[uo], self.gn.asts[un]
        do, dn = self.go.node(uo), self.gn.node(un)
        t1, t2, m, acts = diff(a, b)
        ctx = FuncDiffContext(uo, un, t1, t2, m)
        self.func_ctx[uo] = ctx
        # 노드 대응표(CFG 앵커, 호출 지점)
        for x, y in m.a2b.items():
            n1, n2 = t1.nodes[x], t2.nodes[y]
            if n1.get("a") == f"{uo}#{x}" and n2.get("a"):
                self.node_map[n1["a"]] = n2["a"]
            if n1.get("k") == "Call" and n2.get("k") == "Call":
                cs1, cs2 = f"{uo}#{x}c", f"{un}#{y}c"
                self.node_map[cs1] = cs2
                self.node_map[cs1 + ":out"] = cs2 + ":out"
                for i in range(max(0, len(t1.children[x]) - 1)):
                    self.node_map[f"{cs1}:in{i}"] = f"{cs2}:in{i}"
        # 시그니처
        sig_o = [(p["name"], (p.get("ty") or {}).get("cn")) for p in a.get("params", [])]
        sig_n = [(p["name"], (p.get("ty") or {}).get("cn")) for p in b.get("params", [])]
        ret_o, ret_n = (a.get("ret") or {}).get("cn"), (b.get("ret") or {}).get("cn")
        if sig_o != sig_n or ret_o != ret_n or a.get("static") != b.get("static"):
            parts = []
            if ret_o != ret_n:
                parts.append(f"반환형 {ret_o} -> {ret_n}")
            if [t for _, t in sig_o] != [t for _, t in sig_n]:
                parts.append(f"인자 자료형 ({', '.join(t for _, t in sig_o)}) -> ({', '.join(t for _, t in sig_n)})")
            elif sig_o != sig_n:
                parts.append(f"인자 이름 ({', '.join(n for n, _ in sig_o)}) -> ({', '.join(n for n, _ in sig_n)})")
            if a.get("static") != b.get("static"):
                parts.append(f"static {a.get('static')} -> {b.get('static')}")
            ao = self._fn_boundary(self.go, uo) + [f"{uo}#fi{i}" for i in range(len(sig_o))]
            an = self._fn_boundary(self.gn, un) + [f"{un}#fi{i}" for i in range(len(sig_n))]
            for g, f, lst in ((self.go, uo, ao), (self.gn, un, an)):
                for cs in [n for n in g.nodes_of(NK.CALL_SITE) if f in (g.node(n).get("callees") or [])]:
                    lst.extend(x for x in g.succ(cs) if x.startswith(cs + ":in"))
            self._add("signature_change", uo, un, "; ".join(parts),
                      {"anchors": [x for x in ao if self.go.has_node(x)], "file": do.get("file"), "line": do.get("line"),
                       "code": do.get("sig")},
                      {"anchors": [x for x in an if self.gn.has_node(x)], "file": dn.get("file"), "line": dn.get("line"),
                       "code": dn.get("sig")})
        if not acts:
            return
        groups: Dict[Tuple[Optional[int], Optional[int]], List] = defaultdict(list)
        for act in acts:
            if act.op == "insert":
                ng = ctx.stmt_anc(t2, act.b)
                og = m.b2a.get(ng) if ng != act.b else None
            elif act.op == "delete":
                og = ctx.stmt_anc(t1, act.a)
                ng = m.a2b.get(og) if og != act.a else None
            else:
                og, ng = ctx.stmt_anc(t1, act.a), ctx.stmt_anc(t2, act.b)
            groups[(og, ng)].append(act)
        for (og, ng), alist in groups.items():
            self._function_group(ctx, og, ng, alist)

    def _classify(self, ctx: FuncDiffContext, act) -> Tuple[str, str, dict]:
        t1, t2 = ctx.t1, ctx.t2
        info: dict = {}
        if act.op == "update":
            n1, n2 = t1.nodes[act.a], t2.nodes[act.b]
            k = n1.get("k")
            cond = ctx.in_condition(t1, act.a) or ctx.in_condition(t2, act.b)
            if k != n2.get("k"):
                return ("condition_change" if cond else "expression_change"), f"{k} -> {n2.get('k')}", info
            if k == "Lit":
                info["old_value"], info["new_value"] = n1.get("v"), n2.get("v")
                info["old_macro"], info["new_macro"] = n1.get("mac"), n2.get("mac")
                txt = f"{_lit(n1)} -> {_lit(n2)}"
                return ("threshold_change" if cond else "value_change"), txt, info
            if k in ("Bin", "Un", "CAssign"):
                o1, o2 = n1.get("v"), n2.get("v")
                info["old_op"], info["new_op"] = o1, o2
                if cond and o1 in COMPARISON and o2 in COMPARISON:
                    return "comparison_operator_change", f"'{o1}' -> '{o2}'", info
                if cond and (o1 in LOGICAL or o2 in LOGICAL):
                    return "logic_operator_change", f"'{o1}' -> '{o2}'", info
                return ("condition_change" if cond else "value_change"), f"연산자 '{o1}' -> '{o2}'", info
            if k == "Call":
                return "callee_change", f"{n1.get('v')}() -> {n2.get('v')}()", info
            if k == "Var":
                if n1.get("v") != n2.get("v"):
                    return "reference_change", f"변수 이름 {n1.get('v')} -> {n2.get('v')}", info
                return "local_type_change", f"{n1.get('v')}: {(n1.get('ty') or {}).get('n')} -> {(n2.get('ty') or {}).get('n')}", info
            if k in ("ICast", "Cast"):
                return "cast_change", f"({(n1.get('ty') or {}).get('n')}) -> ({(n2.get('ty') or {}).get('n')})", info
            if k in ("Ref", "Member"):
                return ("condition_change" if cond else "reference_change"), f"{n1.get('v')} -> {n2.get('v')}", info
            if k == "Case":
                return "condition_change", f"case {n1.get('v')} -> case {n2.get('v')}", info
            return ("condition_change" if cond else "expression_change"), act.detail, info
        if act.op in ("insert", "delete"):
            t, i = (t2, act.b) if act.op == "insert" else (t1, act.a)
            add = act.op == "insert"
            n = t.nodes[i]
            k = n.get("k")
            sub_kinds = {t.kind(x) for x in t.desc[i]} | {k}
            if k == "If":
                if sub_kinds & FLOW_KINDS:
                    kinds = "/".join(sorted(x.lower() for x in sub_kinds & FLOW_KINDS))
                    cond = t.nodes[t.children[i][0]] if t.children[i] else {}
                    src = self.src_new if add else self.src_old
                    unit = ctx.un if add else ctx.uo
                    f = cond.get("f") or (self.gn if add else self.go).node(unit).get("file")
                    ctext = src.get(f, cond.get("l")).strip()
                    return ("early_exit_added" if add else "early_exit_removed"), f"조건부 {kinds} ({ctext})", info
                return ("branch_added" if add else "branch_removed"), "if 문", info
            if k in FLOW_KINDS:
                return "control_flow_change", f"{k} {'추가' if add else '삭제'}", info
            if k in ("While", "Do", "For"):
                return ("loop_added" if add else "loop_removed"), k, info
            if k in ("Switch", "Case", "Default"):
                return ("branch_added" if add else "branch_removed"), k, info
            if k in ("DeclStmt", "Var"):
                se = _has_side_effect(t, i)
                info["side_effect"] = se
                names = [t.nodes[x].get("v") for x in [i] + list(t.desc[i]) if t.kind(x) == "Var"]
                return ("local_var_added" if add else "local_var_removed"), \
                    f"{', '.join(names)}" + (f" (초기화 부작용: {', '.join(se)})" if se else ""), info
            if k == "ExprStmt":
                e = t.nodes[t.children[i][0]] if t.children[i] else {}
                ek = e.get("k")
                if ek in ("Assign", "CAssign") or (ek == "Un" and e.get("v") in ("pre++", "pre--", "post++", "post--")):
                    return ("assignment_added" if add else "assignment_removed"), "대입", info
                if ek == "Call" or (ek in ("Cast", "ICast") and "Call" in sub_kinds):
                    return ("call_added" if add else "call_removed"), f"{_call_name(t, i)}()", info
                return ("statement_added" if add else "statement_removed"), "실행문", info
            if k == "Call":
                return ("call_added" if add else "call_removed"), f"{n.get('v') or '(간접)'}()", info
            if "Call" in sub_kinds:
                info["calls"] = sorted({t.nodes[x].get("v") or "(간접)" for x in [i] + list(t.desc[i]) if t.kind(x) == "Call"})
            cond = ctx.in_condition(t, i)
            if cond:
                return "condition_change", f"조건식 일부 {'추가' if add else '삭제'}", info
            if info.get("calls"):
                return ("call_added" if add else "call_removed"), ", ".join(c + "()" for c in info["calls"]), info
            if k in ("Compound", "Null"):
                return ("statement_added" if add else "statement_removed"), "블록", info
            return "expression_change", f"식 {'추가' if add else '삭제'}", info
        # move
        n1 = t1.nodes[act.a]
        p1 = t1.parent.get(act.a)
        if act.detail == "순서 변경" and p1 is not None:
            if t1.kind(p1) == "Compound":
                return "statement_reorder", "같은 블록 안 실행문 순서 변경", info
            if t1.kind(p1) == "Bin" and t1.label(p1) in LOGICAL:
                return "short_circuit_order_change", f"'{t1.label(p1)}' 피연산자 순서 변경", info
        return "code_moved", f"{n1.get('k')} 위치 이동", info

    def _function_group(self, ctx: FuncDiffContext, og: Optional[int], ng: Optional[int], acts: list) -> None:
        t1, t2 = ctx.t1, ctx.t2
        types, details, infos = [], [], []
        anc_o: Set[str] = set()
        anc_n: Set[str] = set()
        macro_sites = []
        for act in acts:
            ctype, det, info = self._classify(ctx, act)
            # 변경된 매크로의 전개 지점은 매크로 변경으로 귀속
            if (act.op == "update" and info.get("old_macro") and info.get("old_macro") == info.get("new_macro")
                    and info["old_macro"] in self.changed_macros):
                macro_sites.append((info["old_macro"], act))
                continue
            types.append(ctype)
            details.append(det)
            infos.append(info)
            if act.a is not None:
                anc_o |= ctx.anchors(t1, ctx.uo, act.a, act.op == "delete")
            if act.b is not None:
                anc_n |= ctx.anchors(t2, ctx.un, act.b, act.op == "insert")
        for mac, act in macro_sites:
            self._macro_site(mac, ctx, act)
        if not types:
            return
        main = min(types, key=lambda x: _PRIORITY.get(x, 999))
        side = sorted({s for i in infos for s in i.get("side_effect", [])})
        old_side = self._side_info(ctx, t1, og, ctx.uo, self.go, self.src_old)
        new_side = self._side_info(ctx, t2, ng, ctx.un, self.gn, self.src_new)
        old_side["anchors"] = sorted(anc_o)
        new_side["anchors"] = sorted(anc_n)
        # 조건식 변경은 조건 노드 자체를 감시 대상으로 남긴다(behavior_checker)
        watch = {"old": [], "new": []}
        for act in acts:
            if act.a is not None and t1.nodes[act.a].get("a"):
                watch["old"].append(t1.nodes[act.a]["a"])
            if act.b is not None and t2.nodes[act.b].get("a"):
                watch["new"].append(t2.nodes[act.b]["a"])
        summary = "; ".join(sorted(set(details)))
        lit = next((i for i in infos if "old_value" in i), None)
        if lit and (lit.get("old_macro") or lit.get("new_macro")) and lit.get("old_macro") != lit.get("new_macro"):
            summary += f" (매크로 {lit.get('old_macro') or '-'} -> {lit.get('new_macro') or '리터럴'})"
        refs = sorted(set((_refs(t1, og) if og is not None else []) + (_refs(t2, ng) if ng is not None else [])))
        self._add(main, ctx.uo, ctx.un, summary, old_side, new_side,
                  subtypes=sorted(set(types)), side_effect=side or None,
                  related_variables=refs, actions=[f"{a.op}:{a.detail}" for a in acts],
                  watch={k: sorted(set(v)) for k, v in watch.items()}, value_info=lit)

    def _side_info(self, ctx, t: Optional[Tree], gid: Optional[int], unit: str, g: AnalysisGraph,
                   src: SourceView) -> dict:
        if gid is None or t is None:
            return {}
        n = t.nodes[gid]
        f = n.get("f") or g.node(unit).get("file")
        l1, l2 = n.get("l"), n.get("el")
        if n.get("k") in ("If", "While", "For", "Switch") and n.get("ch"):
            c = n["ch"][0] if n["k"] != "For" else n["ch"][min(1, len(n["ch"]) - 1)]
            l2 = max(l1 or 0, c.get("el") or 0)
        return {"file": f, "line": l1, "end_line": l2, "code": src.get(f, l1, l2), "ast_id": gid}

    # ------------------------------------------------------------------
    def _macros(self) -> None:
        mo = {k: self.go.node(k) for k in self.go.nodes_of(NK.MACRO) if k in self.go.asts}
        mn = {k: self.gn.node(k) for k in self.gn.nodes_of(NK.MACRO) if k in self.gn.asts}
        self._macro_changes: Dict[str, dict] = {}
        for k in sorted(set(mo) & set(mn)):
            a, b = mo[k], mn[k]
            if a.get("include_guard") or a.get("body") == b.get("body"):
                continue
            self.changed_macros.add(a["name"])
            ao = self._macro_anchors(self.go, k)
            an = self._macro_anchors(self.gn, k)
            ch = self._add("macro_change", k, k, f"#define {a['name']} {a.get('body')} -> {b.get('body')}",
                           {"anchors": ao, "file": a.get("file"), "line": a.get("line"),
                            "code": self.src_old.get(a.get("file"), a.get("line"))},
                           {"anchors": an, "file": b.get("file"), "line": b.get("line"),
                            "code": self.src_new.get(b.get("file"), b.get("line"))},
                           affected_tus=sorted(set(a.get("tus") or []) | set(b.get("tus") or [])),
                           header=(a.get("file") or "").endswith(".h"),
                           value_info={"old_value": a.get("value"), "new_value": b.get("value")},
                           expansion_sites=[], watch={"old": [], "new": []})
            self._macro_changes[a["name"]] = ch
        for k in sorted(set(mo) - set(mn)):
            if not mo[k].get("include_guard"):
                self._add("macro_removed", k, None, f"#define {mo[k]['name']} 삭제",
                          {"anchors": self._macro_anchors(self.go, k), "file": mo[k].get("file"), "line": mo[k].get("line")},
                          affected_tus=mo[k].get("tus"))
        for k in sorted(set(mn) - set(mo)):
            if not mn[k].get("include_guard"):
                self._add("macro_added", None, k, f"#define {mn[k]['name']} {mn[k].get('body')} 추가",
                          new={"anchors": self._macro_anchors(self.gn, k), "file": mn[k].get("file"), "line": mn[k].get("line")},
                          affected_tus=mn[k].get("tus"))

    def _macro_anchors(self, g: AnalysisGraph, mk: str) -> List[str]:
        out = []
        for s in g.pred(mk, [EK.USES_MACRO]):
            k = g.kind(s)
            if k == NK.STMT:
                out.append(s)
            elif k == NK.GLOBAL and g.has_node(f"loc:{s}"):
                out.append(f"loc:{s}")
        return out

    def _macro_site(self, mac: str, ctx: FuncDiffContext, act) -> None:
        ch = self._macro_changes.get(mac)
        if not ch:
            return
        n1, n2 = ctx.t1.nodes[act.a], ctx.t2.nodes[act.b]
        ch["expansion_sites"].append({"function": self.go.node(ctx.uo).get("name"),
                                      "old_line": n1.get("l"), "new_line": n2.get("l"),
                                      "old": n1.get("v"), "new": n2.get("v")})
        for side, n in (("old", n1), ("new", n2)):
            if n.get("a"):
                ch[side]["anchors"] = sorted(set(ch[side]["anchors"]) | {n["a"]})
                ch["watch"][side].append(n["a"])

    # ------------------------------------------------------------------
    def _global(self, uo: str, un: str) -> None:
        a, b = self.go.asts[uo], self.gn.asts[un]
        do, dn = self.go.node(uo), self.gn.node(un)
        locs_o = [l for l in self.go.nodes_of(NK.LOCATION) if self.go.node(l).get("root") == uo]
        locs_n = [l for l in self.gn.nodes_of(NK.LOCATION) if self.gn.node(l).get("root") == un]
        old = {"anchors": locs_o, "file": do.get("file"), "line": do.get("line"),
               "code": self.src_old.get(do.get("file"), do.get("line"), do.get("end_line"))}
        new = {"anchors": locs_n, "file": dn.get("file"), "line": dn.get("line"),
               "code": self.src_new.get(dn.get("file"), dn.get("line"), dn.get("end_line"))}
        ta, tb = (a.get("ty") or {}).get("cn"), (b.get("ty") or {}).get("cn")
        if ta != tb:
            self._add("global_type_change", uo, un, f"{do['name']}: {ta} -> {tb}", dict(old), dict(new))
        if (do.get("storage"), do.get("volatile"), do.get("const")) != (dn.get("storage"), dn.get("volatile"), dn.get("const")):
            self._add("qualifier_change", uo, un,
                      f"{do['name']}: storage {do.get('storage')}->{dn.get('storage')}, volatile {do.get('volatile')}->{dn.get('volatile')}, const {do.get('const')}->{dn.get('const')}",
                      dict(old), dict(new))
        from .project_indexer import ast_hash
        ia = [ast_hash(c) for c in a.get("ch", [])]
        ib = [ast_hash(c) for c in b.get("ch", [])]
        if ia != ib:
            macros_only = self._init_macro_only(a, b)
            if not macros_only:
                self._add("global_init_change", uo, un, f"{do['name']} 초기값 변경", dict(old), dict(new))

    def _init_macro_only(self, a: dict, b: dict) -> bool:
        la = [(n.get("k"), n.get("v"), n.get("mac")) for n in walk_ast(a)]
        lb = [(n.get("k"), n.get("v"), n.get("mac")) for n in walk_ast(b)]
        if len(la) != len(lb):
            return False
        for x, y in zip(la, lb):
            if x != y and not (x[2] and x[2] == y[2] and x[2] in self.changed_macros):
                if (x[0], x[1]) != (y[0], y[1]):
                    return False
        return True

    def _record(self, uo: str, un: str) -> None:
        go, gn = self.go, self.gn
        fo = {go.node(f)["name"]: go.node(f) for f in go.succ(uo, [EK.HAS_FIELD])}
        fn = {gn.node(f)["name"]: gn.node(f) for f in gn.succ(un, [EK.HAS_FIELD])}
        order_o = [n["v"] for n in go.asts[uo].get("ch", [])]
        order_n = [n["v"] for n in gn.asts[un].get("ch", [])]
        parts = []
        changed_fields = set()
        for f in order_n:
            if f not in fo:
                parts.append(f"필드 {f} 추가")
                changed_fields.add(f)
        for f in order_o:
            if f not in fn:
                parts.append(f"필드 {f} 삭제")
                changed_fields.add(f)
        type_changed = set()
        for f in order_o:
            if f in fn:
                a, b = fo[f], fn[f]
                if a.get("ty") != b.get("ty"):
                    parts.append(f"{f} 자료형 {a.get('ty')} -> {b.get('ty')}")
                    type_changed.add(f)
                if a.get("offset_bits") != b.get("offset_bits"):
                    parts.append(f"{f} 오프셋 {a.get('offset_bits')}->{b.get('offset_bits')}비트")
                    changed_fields.add(f)
        if [f for f in order_o if f in fn] != [f for f in order_n if f in fo]:
            parts.append("필드 순서 변경")
        so, sn = go.node(uo).get("size"), gn.node(un).get("size")
        if so != sn:
            parts.append(f"크기 {so} -> {sn} 바이트")
        if not parts:
            return
        rname = go.node(uo)["name"]

        def users(g, rec, fields):
            stmts, funcs = set(), set()
            for f in g.succ(rec, [EK.HAS_FIELD]):
                if g.node(f)["name"] in fields:
                    for s in g.pred(f, [EK.USES_FIELD]):
                        if g.kind(s) == NK.STMT:
                            stmts.add(s)
                        elif g.kind(s) == NK.FUNCTION:
                            funcs.add(s)
            for s in g.pred(rec, [EK.USES_TYPE]):
                funcs.add(s)
            return stmts, funcs
        # 자료형이 바뀐 필드의 사용처는 값 의미가 바뀔 수 있어 영향 추적 시작점으로 사용
        so_s, so_f = users(go, uo, type_changed)
        sn_s, sn_f = users(gn, un, type_changed)
        _, lay_f_o = users(go, uo, changed_fields)
        _, lay_f_n = users(gn, un, changed_fields)
        d1, d2 = go.node(uo), gn.node(un)
        tus = sorted(set(go.node(uo).get("tus") or []) | set(gn.node(un).get("tus") or []))
        self._add("type_layout_change", uo, un, f"{rname}: " + "; ".join(parts),
                  {"anchors": sorted(so_s), "file": d1.get("file"), "line": d1.get("line"),
                   "code": self.src_old.get(d1.get("file"), d1.get("line"), d1.get("end_line"))},
                  {"anchors": sorted(sn_s), "file": d2.get("file"), "line": d2.get("line"),
                   "code": self.src_new.get(d2.get("file"), d2.get("line"), d2.get("end_line"))},
                  affected_tus=tus, header=(d1.get("file") or "").endswith(".h"),
                  layout_users=sorted({go.node(f).get("name") for f in lay_f_o | so_f} |
                                      {gn.node(f).get("name") for f in lay_f_n | sn_f}),
                  review_note="메모리 배치(오프셋·크기) 변경: 동일 빌드 내 필드 접근은 재컴파일로 일관되지만, "
                              "NVM 저장·통신 직렬화·memcpy·외부 모듈(라이브러리)과의 인터페이스는 별도 확인 필요")

    def _typedef(self, uo: str, un: str) -> None:
        a, b = self.go.asts[uo], self.gn.asts[un]
        ta, tb = (a.get("ty") or {}).get("cn"), (b.get("ty") or {}).get("cn")
        if ta == tb:
            return
        name = a.get("v")

        def users(g):
            out = set()
            for f in g.nodes_of(NK.FUNCTION):
                ast = g.asts.get(f)
                if not ast:
                    continue
                for n in walk_ast(ast):
                    if (n.get("ty") or {}).get("n") == name and n.get("a"):
                        out.add(n["a"])
            return sorted(out)
        d1, d2 = self.go.node(uo), self.gn.node(un)
        self._add("typedef_change", uo, un, f"typedef {name}: {ta} -> {tb}",
                  {"anchors": users(self.go), "file": d1.get("file"), "line": d1.get("line")},
                  {"anchors": users(self.gn), "file": d2.get("file"), "line": d2.get("line")},
                  affected_tus=sorted(set(d1.get("tus") or []) | set(d2.get("tus") or [])))

    def _enum(self, uo: str, un: str) -> None:
        a = {n["v"]: n.get("val") for n in self.go.asts[uo].get("ch", [])}
        b = {n["v"]: n.get("val") for n in self.gn.asts[un].get("ch", [])}
        diffs = [f"{k}: {a.get(k)} -> {b.get(k)}" for k in sorted(set(a) | set(b)) if a.get(k) != b.get(k)]
        if not diffs:
            return

        def users(g):
            out = set()
            for k in set(a) | set(b):
                if a.get(k) != b.get(k) and g.has_node(f"ec:{k}"):
                    out |= {s for s in g.pred(f"ec:{k}", [EK.USES_ENUM]) if g.kind(s) == NK.STMT}
            return sorted(out)
        self._add("enum_value_change", uo, un, "; ".join(diffs), {"anchors": users(self.go)}, {"anchors": users(self.gn)})

    # ------------------------------------------------------------------
    def _added_removed(self) -> None:
        uo, un = self._units(self.go), self._units(self.gn)
        mapped_new = set(self.unit_map.values())
        for k, kind in sorted(uo.items()):
            if k in self.unit_map or kind == NK.MACRO:
                continue
            d = self.go.node(k)
            if kind == NK.FUNCTION:
                anchors = self._fn_boundary(self.go, k) + [f"{k}#fo_ret", f"{k}#fo_mem"]
                still = [n for n in self.gn.nodes_of(NK.CALL_SITE)
                         if any(c.split(":")[-1] == d["name"] for c in (self.gn.node(n).get("callees") or []))]
                decl = [x for x in (self.gn.meta.get("fn_decls") or {}).get(k, [])]
                risk = None
                if still or decl:
                    risk = f"삭제된 함수가 변경 후에도 호출 {len(still)}곳/선언 {len(decl)}곳에 남아 있음 → 링크 오류 가능"
                self._add("function_removed", k, None, f"{d['name']}() 삭제",
                          {"anchors": [a for a in anchors if self.go.has_node(a)], "file": d.get("file"),
                           "line": d.get("line"), "code": self.src_old.get(d.get("file"), d.get("line"), d.get("end_line"))},
                          {"anchors": still}, link_risk=risk)
            elif kind == NK.GLOBAL:
                self._add("global_removed", k, None, f"{d['name']} 삭제",
                          {"anchors": [l for l in self.go.nodes_of(NK.LOCATION) if self.go.node(l).get("root") == k],
                           "file": d.get("file"), "line": d.get("line")})
        for k, kind in sorted(un.items()):
            if k in mapped_new or kind == NK.MACRO:
                continue
            d = self.gn.node(k)
            if kind == NK.FUNCTION:
                callers = [n for n in self.gn.nodes_of(NK.CALL_SITE) if k in (self.gn.node(n).get("callees") or [])]
                regs = self.gn.pred(k, [EK.ADDR_TAKEN])
                acts = self.gn.pred(k, [EK.ACTIVATES])
                note = None if (callers or regs or acts) else "호출·등록·실행 문맥 없음(미사용이거나 외부에서 등록됨)"
                self._add("function_added", None, k, f"{d['name']}() 추가",
                          new={"anchors": [f"{k}#entry"] + callers, "file": d.get("file"), "line": d.get("line"),
                               "code": self.src_new.get(d.get("file"), d.get("line"), d.get("end_line"))},
                          callers=[self.gn.node(c).get("func") for c in callers], note=note)
            elif kind == NK.GLOBAL:
                self._add("global_added", None, k, f"{d['name']} 추가",
                          new={"anchors": [l for l in self.gn.nodes_of(NK.LOCATION) if self.gn.node(l).get("root") == k],
                               "file": d.get("file"), "line": d.get("line")})

    def _conditional_compilation(self) -> None:
        fo = self.go.meta.get("files") or {}
        fn = self.gn.meta.get("files") or {}
        for rel in sorted(set(fo) & set(fn)):
            a, b = fo[rel], fn[rel]
            if sorted(map(tuple, a.get("inactive", []))) != sorted(map(tuple, b.get("inactive", []))) or \
                    a.get("cond_macros") != b.get("cond_macros"):
                if a.get("hash") == b.get("hash") and a.get("cond_macros") == b.get("cond_macros"):
                    continue
                ca, cb = len(a.get("inactive", [])), len(b.get("inactive", []))
                if a.get("cond_macros") == b.get("cond_macros") and ca == cb:
                    continue   # 줄 이동으로 인한 구간 위치 변화
                self._add("conditional_compilation_change", f"file:{rel}", f"file:{rel}",
                          f"{rel}: #if 조건 매크로 {a.get('cond_macros')} -> {b.get('cond_macros')}, "
                          f"비활성 구간 {ca}개 -> {cb}개",
                          {"anchors": [], "file": rel}, {"anchors": [], "file": rel}, affected_tus=b.get("tus"))

    def _vehicle_config(self) -> None:
        go, gn = self.go, self.gn
        for t in sorted(set(go.nodes_of(NK.TASK)) | set(gn.nodes_of(NK.TASK))):
            a = go.node(t) if go.has_node(t) else None
            b = gn.node(t) if gn.has_node(t) else None
            keys = ("period_ms", "priority", "ctx_kind", "trigger", "core")
            if a and b and all(a.get(k) == b.get(k) for k in keys) and \
                    sorted(go.succ(t, [EK.ACTIVATES])) == sorted(gn.succ(t, [EK.ACTIVATES])):
                continue
            parts = []
            if not a:
                parts.append("실행 문맥 추가")
            elif not b:
                parts.append("실행 문맥 삭제")
            else:
                for k in keys:
                    if a.get(k) != b.get(k):
                        parts.append(f"{k} {a.get(k)} -> {b.get(k)}")
                if sorted(go.succ(t, [EK.ACTIVATES])) != sorted(gn.succ(t, [EK.ACTIVATES])):
                    parts.append("진입 함수 변경")
            funcs_o = sorted(go.pred(t, [EK.RUNS_IN])) if a else []
            funcs_n = sorted(gn.pred(t, [EK.RUNS_IN])) if b else []
            self._add("task_config_change", t if a else None, t if b else None,
                      f"{t[5:]}: " + ", ".join(parts),
                      {"anchors": []}, {"anchors": []},
                      context_functions=sorted({x.split(':')[-1] for x in funcs_o + funcs_n}))
        for s in sorted(set(go.nodes_of(NK.SIGNAL)) | set(gn.nodes_of(NK.SIGNAL))):
            a = go.node(s) if go.has_node(s) else None
            b = gn.node(s) if gn.has_node(s) else None
            if a and b:
                continue
            self._add("signal_definition_change", s if a else None, s if b else None,
                      f"신호 {s[4:]} {'삭제' if a else '추가'}",
                      {"anchors": [s] if a else []}, {"anchors": [s] if b else []})

    def _finalize(self) -> None:
        def key(c):
            return (_PRIORITY.get(c["type"], 999) if c["category"] not in ("timing", "config") else 900,
                    c.get("unit_name") or "", (c["old"].get("line") or c["new"].get("line") or 0))
        self.changes.sort(key=key)
        for i, c in enumerate(self.changes, 1):
            c["id"] = f"C{i:03d}"


def _lit(n: dict) -> str:
    s = str(n.get("v"))
    if n.get("mac"):
        s += f"({n['mac']})"
    return s


def _call_name(t: Tree, i: int) -> str:
    for x in [i] + sorted(t.desc[i]):
        if t.kind(x) == "Call":
            return t.nodes[x].get("v") or "(간접)"
    return "?"


def detect_changes(g_old: AnalysisGraph, g_new: AnalysisGraph) -> Tuple[dict, ChangeDetector]:
    cd = ChangeDetector(g_old, g_new)
    res = cd.run()
    return res, cd
