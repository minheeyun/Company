"""프로그램 의존성 모델.

함수마다 CFG 를 만들고 다음을 계산한다.
  - 제어 의존성: 후위 지배자(post-dominator) 기반. 조기 return/break/goto 도 반영된다.
  - 데이터 의존성:
      * 주소가 노출되지 않은 지역 변수·인자: 도달 정의(reaching definitions)로 흐름 민감하게
      * 전역·static·함수 내 static·포인터로 접근되는 메모리: 흐름 비민감(모든 쓰기 → 모든 읽기).
        함수 호출 간·실행 주기 간·ISR 선점에 의한 상태 전달을 놓치지 않기 위한 보수적 선택이다.
  - 포인터 분석: Andersen 방식(흐름 비민감)의 필드 구분 points-to. 구조체 필드는 경로로 구분하고,
    배열 원소는 하나로 합치며, union 은 필드를 구분하지 않는다. 함수 포인터 호출 대상도 이것으로 푼다.
  - 함수 간 연결: 실인자 → 형식 인자, 호출 → 피호출 함수 진입(호출 여부), return → 호출 결과.

메모리 위치는 (기준 변수 키, 필드, '[]', ...) 형태의 튜플 경로로 나타낸다.
특수 기준:  fn:<함수>  함수 객체,  hw:<주소>  상수 주소(레지스터),  sym:*<포인터>  대상을 모르는 포인터의 대상,
           ret:<함수>  반환값(포인터 분석용)
"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from pycparser import c_ast

from .cexpr import const_eval, gen, norm, walk
from .frontend import TranslationUnit, TypeEnv

PERSISTENT_KINDS = {"global", "static", "static_local", "sym", "hw"}

# 본문이 없는 표준 함수의 효과 (인자 번호 기준)
BUILTIN_MODELS = {
    "memcpy": {"writes_args": [0], "reads_args": [1]},
    "memmove": {"writes_args": [0], "reads_args": [1]},
    "memset": {"writes_args": [0], "reads_args": []},
    "strcpy": {"writes_args": [0], "reads_args": [1]},
    "strncpy": {"writes_args": [0], "reads_args": [1]},
    "strcat": {"writes_args": [0], "reads_args": [0, 1]},
    "memcmp": {"writes_args": [], "reads_args": [0, 1]},
    "strcmp": {"writes_args": [], "reads_args": [0, 1]},
    "strncmp": {"writes_args": [], "reads_args": [0, 1]},
    "strlen": {"writes_args": [], "reads_args": [0]},
    "printf": {"writes_args": [], "reads_args": []},
    "abs": {"writes_args": [], "reads_args": []},
    "labs": {"writes_args": [], "reads_args": []},
    "fabs": {"writes_args": [], "reads_args": []},
    "fabsf": {"writes_args": [], "reads_args": []},
}

_ARITH_ASSIGN = {"+=", "-="}


@dataclass
class Var:
    key: str
    name: str
    kind: str  # global | static | static_local | local | param | temp | sym | hw
    func: str | None
    file: str
    line: int
    type: object = None
    env: TypeEnv | None = None
    defined: bool = True
    init: object = None

    @property
    def persistent(self) -> bool:
        return self.kind in PERSISTENT_KINDS


@dataclass
class Node:
    id: int
    func: str
    kind: str  # entry exit nop stmt cond return call actual_in actual_out extcall formal_in ginit
    file: str
    line: int
    text: str
    sig: str = ""
    ast: object = None
    site: int | None = None
    arg: int | None = None
    var: str | None = None
    depth: int = 0
    defs: set = field(default_factory=set)
    uses: set = field(default_factory=set)
    strong: set = field(default_factory=set)
    dead: bool = False
    cases: list | None = None  # switch 조건: case 라벨 상수값

    @property
    def visible(self) -> bool:
        return self.kind not in ("entry", "exit", "nop")


@dataclass
class CallSite:
    id: int
    caller: str
    ast: c_ast.FuncCall
    name: str
    direct: str | None
    external: bool
    call_node: int = -1
    actual_in: list[int] = field(default_factory=list)
    actual_out: int | None = None
    callees: set[str] = field(default_factory=set)


@dataclass
class Function:
    key: str
    name: str
    file: str
    line: int
    static: bool
    decl: c_ast.Decl
    env: TypeEnv
    tu: str
    params: list[str] = field(default_factory=list)
    nodes: list[int] = field(default_factory=list)
    succ: dict[int, list[tuple[int, str | None]]] = field(default_factory=lambda: defaultdict(list))
    entry: int = -1
    exit: int = -1
    formal_in: list[int] = field(default_factory=list)
    returns: list[int] = field(default_factory=list)
    ret_type: object = None
    no_return: bool = False


def display_path(prog: "Program", path: tuple) -> str:
    base = path[0]
    if base.startswith("hw:"):
        s = f"*({base[3:]})"
    elif base.startswith("sym:"):
        s = base[4:]
    elif base.startswith("fn:"):
        s = base[3:].split("@")[0]
    elif base.startswith("ret:"):
        s = base[4:].split("@")[0] + "()"
    else:
        v = prog.vars.get(base)
        s = v.name if v else base
    for comp in path[1:]:
        s += "[]" if comp == "[]" else "." + comp.lstrip("|")
    return s


def overlaps(a: tuple, b: tuple) -> bool:
    """두 경로가 같은 메모리를 공유하는가 (접두 관계, union 의 서로 다른 멤버는 겹침)."""
    for x, y in zip(a, b):
        if x == y:
            continue
        return x[:1] == "|" and y[:1] == "|"
    return True


class Program:
    def __init__(self, root: Path, tus: list[TranslationUnit], extern_model: dict | None = None, label: str = ""):
        self.root = Path(root)
        self.label = label
        self.tus = tus
        self.extern_model = {**BUILTIN_MODELS, **(extern_model or {})}
        self.vars: dict[str, Var] = {}
        self.funcs: dict[str, Function] = {}
        self.nodes: list[Node] = []
        self.sites: list[CallSite] = []
        self.bind: dict[int, str] = {}  # id(c_ast.ID) → 변수 키 또는 'fn:<함수 키>'
        self.site_of: dict[int, int] = {}  # id(c_ast.FuncCall) → call site
        self.pts: dict[tuple, set] = defaultdict(set)
        self._pts_by_base: dict[str, set] = defaultdict(set)
        self.protos: dict[str, tuple[c_ast.FuncDecl, TypeEnv]] = {}
        self.tu_static: dict[str, dict[str, str]] = defaultdict(dict)  # tu → 이름 → 키
        self.enums: dict[str, dict[str, int]] = defaultdict(dict)  # tu → 열거 상수
        self.address_taken: set[str] = set()
        self.warnings: list[str] = []
        self.parse_errors: dict[str, str] = {tu.rel: tu.error for tu in tus if tu.error}
        self.unresolved_calls: list[int] = []
        self.unresolved_ptrs: set[str] = set()
        self.extern_funcs: dict[str, list[int]] = defaultdict(list)  # 이름 → call site
        self.hw_access: set[str] = set()
        # 의존성 그래프
        self.dd: dict[int, list[tuple[int, str]]] = defaultdict(list)
        self.cd_children: dict[int, list[tuple[int, str | None]]] = defaultdict(list)
        self.cd_parents: dict[int, list[tuple[int, str | None]]] = defaultdict(list)
        self.def_index: dict[str, list[tuple[int, tuple]]] = defaultdict(list)
        self.use_index: dict[str, list[tuple[int, tuple]]] = defaultdict(list)
        self.callers: dict[str, list[int]] = defaultdict(list)  # 함수 → 호출 site
        self._assigns: list[tuple[str, c_ast.Node]] = []
        self._inits: list[tuple[tuple, c_ast.Node]] = []
        self._final = False
        self._static_funcs: set[str] = set()
        self._cd_seen: dict[int, set] = {}
        self._build()

    # ------------------------------------------------------------------
    # 구성
    # ------------------------------------------------------------------

    def _build(self):
        for tu in self.tus:
            if tu.ast is not None:
                self._collect_file_scope(tu)
        for tu in self.tus:
            if tu.ast is None:
                continue
            for ext in tu.ast.ext:
                if isinstance(ext, c_ast.FuncDef):
                    key = self._func_key(tu.rel, ext.decl.name)
                    _FuncBuilder(self, self.funcs[key], ext, tu).build()
        self._global_init_nodes()
        self._points_to()
        self._final = True
        self._resolve_calls()
        for n in self.nodes:
            self._effects(n)
        tracked = self._tracked_vars()
        for f in self.funcs.values():
            self._function_dataflow(f, tracked)
        self._memory_index(tracked)

    def _func_key(self, rel: str, name: str) -> str:
        return self.tu_static[rel].get(name, name)

    def _collect_file_scope(self, tu: TranslationUnit):
        rel = tu.rel
        enums = self.enums[rel]
        for n in walk(tu.ast):
            if isinstance(n, c_ast.Enum) and n.values:
                cur = -1
                for e in n.values.enumerators:
                    v = const_eval(e.value, enums) if e.value is not None else None
                    cur = v if v is not None else cur + 1
                    enums[e.name] = cur
        for ext in tu.ast.ext:
            if isinstance(ext, c_ast.FuncDef):
                d = ext.decl
                static = "static" in (d.storage or [])
                key = f"{d.name}@{rel}" if static else d.name
                if static:
                    self.tu_static[rel][d.name] = key
                    self._static_funcs.add(key)
                if key in self.funcs:
                    self.warnings.append(f"함수 {d.name} 정의가 여러 번 있습니다 ({self.funcs[key].tu}, {rel}). 첫 정의를 사용합니다.")
                    continue
                coord = d.coord
                self.funcs[key] = Function(key, d.name, coord.file if coord else rel, coord.line if coord else 0,
                                           static, d, tu.env, rel, ret_type=d.type.type)
            elif isinstance(ext, c_ast.Decl) and ext.name:
                d = ext
                storage = d.storage or []
                if isinstance(d.type, c_ast.FuncDecl):
                    self.protos.setdefault(d.name, (d.type, tu.env))
                    if "static" in storage:
                        self.tu_static[rel].setdefault(d.name, f"{d.name}@{rel}")
                        self._static_funcs.add(f"{d.name}@{rel}")
                    continue
                static = "static" in storage
                key = f"{d.name}@{rel}" if static else d.name
                if static:
                    self.tu_static[rel][d.name] = key
                coord = d.coord
                is_def = "extern" not in storage or d.init is not None
                v = self.vars.get(key)
                if v is None or (is_def and not v.defined):
                    self.vars[key] = Var(key, d.name, "static" if static else "global", None,
                                         coord.file if coord else rel, coord.line if coord else 0,
                                         d.type, tu.env, is_def, d.init)
                    if d.init is not None:
                        self._bind_expr(d.init, [], rel)

    def _global_init_nodes(self):
        for v in list(self.vars.values()):
            if v.kind in ("global", "static", "static_local") and v.defined:
                env = v.env
                tsig = env.signature(v.type) if env else ""
                text = f"{v.name} = {gen(v.init)}" if v.init is not None else f"{v.name} (0으로 초기화)"
                n = self._new_node(v.func or "", "ginit", v.file, v.line, text,
                                   sig=f"ginit|{tsig}|{norm(v.init)}", ast=v.init)
                n.var = v.key
                if v.init is not None:
                    self._inits.append(((v.key,), v.init))

    def _new_node(self, func, kind, file, line, text, **kw) -> Node:
        n = Node(len(self.nodes), func, kind, file, line, text, **kw)
        self.nodes.append(n)
        if func and func in self.funcs and kind != "ginit":
            self.funcs[func].nodes.append(n.id)
        return n

    # ------------------------------------------------------------------
    # 이름 결합
    # ------------------------------------------------------------------

    def lookup(self, name: str, scopes: list[dict], rel: str) -> str | None:
        for s in reversed(scopes):
            if name in s:
                return s[name]
        if name in self.tu_static[rel]:
            key = self.tu_static[rel][name]
            return "fn:" + key if key in self._static_funcs else key
        if name in self.vars:
            return name
        if name in self.funcs or name in self.protos:
            return "fn:" + name
        if name in self.enums[rel]:
            return None
        return None

    def _bind_expr(self, e, scopes, rel):
        """식 안의 식별자를 변수/함수 키에 결합한다 (필드 이름·타입 이름은 제외)."""
        stack = [e]
        while stack:
            n = stack.pop()
            if n is None:
                continue
            if isinstance(n, c_ast.ID):
                key = self.lookup(n.name, scopes, rel)
                if key is None and n.name not in self.enums[rel]:
                    # 선언을 찾지 못한 식별자는 외부 전역 변수로 본다
                    key = n.name
                    if key not in self.vars:
                        self.vars[key] = Var(key, n.name, "global", None, rel, n.coord.line if n.coord else 0,
                                             None, None, False)
                if key is not None:
                    self.bind[id(n)] = key
                continue
            if isinstance(n, c_ast.StructRef):
                stack.append(n.name)
                continue
            if isinstance(n, c_ast.FuncCall):
                if isinstance(n.name, c_ast.ID) and self.lookup(n.name.name, scopes, rel) is None \
                        and n.name.name not in self.enums[rel]:
                    self.bind[id(n.name)] = "fn:" + n.name.name  # 선언 없이 호출된 함수(암시적 선언)
                else:
                    stack.append(n.name)
                stack.append(n.args)
                continue
            if isinstance(n, c_ast.NamedInitializer):
                stack.append(n.expr)
                for x in n.name:
                    if not isinstance(x, c_ast.ID):
                        stack.append(x)
                continue
            if isinstance(n, (c_ast.Typename, c_ast.TypeDecl, c_ast.PtrDecl, c_ast.ArrayDecl, c_ast.FuncDecl)):
                continue
            if isinstance(n, c_ast.Cast):
                stack.append(n.expr)
                continue
            if isinstance(n, c_ast.UnaryOp) and n.op in ("sizeof", "_Alignof"):
                continue
            if isinstance(n, c_ast.CompoundLiteral):
                stack.append(n.init)
                continue
            stack.extend(c for _, c in n.children())

    # ------------------------------------------------------------------
    # 타입·경로
    # ------------------------------------------------------------------

    def path_type(self, path: tuple):
        v = self.vars.get(path[0])
        if v is None or v.type is None or v.env is None:
            return None, None
        t, env = v.type, v.env
        for comp in path[1:]:
            t = env.element(t) if comp == "[]" else env.field(t, comp.lstrip("|"))
            if t is None:
                return None, None
        return t, env

    def _is_array_path(self, path) -> bool:
        t, env = self.path_type(path)
        return bool(env and env.is_array(t))

    def _extend(self, path: tuple, fld: str) -> tuple:
        t, env = self.path_type(path)
        if env and env.is_union(t):
            return path + ("|" + fld,)  # union 멤버: overlaps() 에서 서로 겹치는 것으로 본다
        return path + (fld,)

    def _sym(self, ptr_path: tuple | None, text: str) -> tuple:
        label = display_path(self, ptr_path) if ptr_path else text
        key = "sym:*" + label
        if key not in self.vars:
            t = env = None
            if ptr_path:
                pt, env = self.path_type(ptr_path)
                t = env.element(pt) if env else None
            self.vars[key] = Var(key, "*" + label, "sym", None, "", 0, t, env, False)
            self.unresolved_ptrs.add("*" + label)
        return (key,)

    # ------------------------------------------------------------------
    # 포인터 분석
    # ------------------------------------------------------------------

    def lvals(self, e) -> set:
        if isinstance(e, c_ast.ID):
            key = self.bind.get(id(e))
            return {(key,)} if key and not key.startswith("fn:") else set()
        if isinstance(e, c_ast.StructRef):
            base = self.lvals(e.name) if e.type == "." else self.pvals(e.name)
            return {self._extend(b, e.field.name) for b in base if not b[0].startswith("fn:")}
        if isinstance(e, c_ast.ArrayRef):
            return {p for p in self.pvals(e.name) if not p[0].startswith("fn:")}
        if isinstance(e, c_ast.UnaryOp) and e.op == "*":
            return self.pvals(e.expr)
        if isinstance(e, c_ast.Cast):
            return self.lvals(e.expr)
        if isinstance(e, c_ast.TernaryOp):
            return self.lvals(e.iftrue) | self.lvals(e.iffalse)
        if isinstance(e, c_ast.ExprList) and e.exprs:
            return self.lvals(e.exprs[-1])
        return set()

    def _loc_pvals(self, e) -> set:
        out = set()
        locs = self.lvals(e)
        for l in locs:
            if l[0].startswith("fn:"):
                out.add(l)
            elif self._is_array_path(l):
                out.add(l + ("[]",))
            else:
                out |= self.pts.get(l, set())
                if self._final and not self.pts.get(l):
                    t, env = self.path_type(l)
                    if env is None or env.is_pointer(t):
                        out.add(self._sym(l, ""))
        if self._final and not locs and not isinstance(e, c_ast.ID):
            out.add(self._sym(None, gen(e)))
        return out

    def pvals(self, e) -> set:
        """식의 값이 가리킬 수 있는 메모리 객체 집합."""
        if isinstance(e, c_ast.ID):
            key = self.bind.get(id(e))
            if key is None:
                return set()
            if key.startswith("fn:"):
                return {(key,)}
            return self._loc_pvals(e)
        if isinstance(e, (c_ast.StructRef, c_ast.ArrayRef)) or (isinstance(e, c_ast.UnaryOp) and e.op == "*"):
            return self._loc_pvals(e)
        if isinstance(e, c_ast.UnaryOp):
            if e.op == "&":
                if isinstance(e.expr, c_ast.ID) and self.bind.get(id(e.expr), "").startswith("fn:"):
                    return {(self.bind[id(e.expr)],)}
                r = self.lvals(e.expr)
                if self._final:
                    self.address_taken.update(p[0] for p in r)
                return r
            if e.op in ("++", "--", "p++", "p--"):
                return self.pvals(e.expr)
            return set()
        if isinstance(e, c_ast.Cast):
            v = const_eval(e.expr)
            if v is not None:
                if v == 0:
                    return set()
                key = f"hw:0x{v:X}"
                if key not in self.vars:
                    t = e.to_type.type
                    self.vars[key] = Var(key, f"*(0x{v:X})", "hw", None, "", 0,
                                         t.type if isinstance(t, c_ast.PtrDecl) else None,
                                         next(iter(tu.env for tu in self.tus if tu.ast is not None), None), False)
                return {(key,)}
            return self.pvals(e.expr)
        if isinstance(e, c_ast.BinaryOp):
            if e.op in ("+", "-"):
                return self.pvals(e.left) | self.pvals(e.right)
            return set()
        if isinstance(e, c_ast.TernaryOp):
            return self.pvals(e.iftrue) | self.pvals(e.iffalse)
        if isinstance(e, c_ast.Assignment):
            return self.pvals(e.rvalue)
        if isinstance(e, c_ast.ExprList) and e.exprs:
            return self.pvals(e.exprs[-1])
        if isinstance(e, c_ast.FuncCall):
            sid = self.site_of.get(id(e))
            if sid is None:
                return set()
            out = set()
            for c in self._site_callees(self.sites[sid]):
                out |= self.pts.get(("ret:" + c,), set())
            return out
        return set()

    def _site_callees(self, site: CallSite) -> set[str]:
        if site.direct is not None:
            return {site.direct}
        return {p[0][3:] for p in self.pvals(site.ast.name) if p[0].startswith("fn:")}

    def _add_pts(self, loc: tuple, vals: set) -> bool:
        cur = self.pts[loc]
        before = len(cur)
        cur |= vals
        if len(cur) != before:
            self._pts_by_base[loc[0]].add(loc)
            return True
        return False

    def _copy_aggregate(self, targets: set, src_expr) -> bool:
        """구조체 대입: 원본의 하위 경로 포인터 값을 대상의 같은 하위 경로로 복사."""
        changed = False
        if not isinstance(src_expr, (c_ast.ID, c_ast.StructRef, c_ast.ArrayRef, c_ast.UnaryOp)):
            return False
        for src in self.lvals(src_expr):
            for k in list(self._pts_by_base.get(src[0], ())):
                if len(k) > len(src) and k[: len(src)] == src:
                    for t in targets:
                        changed |= self._add_pts(t + k[len(src):], self.pts[k])
        return changed

    def _init_pts(self, path: tuple, init) -> bool:
        changed = False
        if isinstance(init, c_ast.InitList):
            t, env = self.path_type(path)
            core = env.core(t) if env else None
            names = env.fields(t) if env and isinstance(core, (c_ast.Struct, c_ast.Union)) else []
            pos = 0
            for x in init.exprs:
                if isinstance(x, c_ast.NamedInitializer):
                    sub = path
                    for nm in x.name:
                        sub = self._extend(sub, nm.name) if isinstance(nm, c_ast.ID) else sub + ("[]",)
                    changed |= self._init_pts(sub, x.expr)
                    if names and isinstance(x.name[0], c_ast.ID) and x.name[0].name in names:
                        pos = names.index(x.name[0].name) + 1
                    continue
                if isinstance(core, c_ast.ArrayDecl):
                    sub = path + ("[]",)
                elif names and pos < len(names):
                    sub = self._extend(path, names[pos])
                else:
                    sub = path
                pos += 1
                changed |= self._init_pts(sub, x)
            return changed
        changed |= self._add_pts(path, self.pvals(init))
        changed |= self._copy_aggregate({path}, init)
        return changed

    def _points_to(self):
        for _ in range(200):
            changed = False
            for _, a in self._assigns:
                if a.op != "=" and a.op not in _ARITH_ASSIGN:
                    continue
                targets = self.lvals(a.lvalue)
                vals = self.pvals(a.rvalue)
                for t in targets:
                    changed |= self._add_pts(t, vals)
                if a.op == "=":
                    changed |= self._copy_aggregate(targets, a.rvalue)
            for path, init in self._inits:
                changed |= self._init_pts(path, init)
            for f in self.funcs.values():
                for r in f.returns:
                    e = self.nodes[r].ast
                    if e is not None:
                        changed |= self._add_pts(("ret:" + f.key,), self.pvals(e))
            for s in self.sites:
                args = s.ast.args.exprs if s.ast.args else []
                for c in self._site_callees(s):
                    f = self.funcs.get(c)
                    if f is None:
                        continue
                    for i, a in enumerate(args[: len(f.params)]):
                        changed |= self._add_pts((f.params[i],), self.pvals(a))
                        changed |= self._copy_aggregate({(f.params[i],)}, a)
            if not changed:
                return
        self.warnings.append("포인터 분석이 반복 한도 안에 수렴하지 않았습니다. 결과가 일부 누락될 수 있습니다.")

    def _resolve_calls(self):
        for s in self.sites:
            if s.external:
                self.extern_funcs[s.name].append(s.id)
                continue
            s.callees = {c for c in self._site_callees(s) if c in self.funcs}
            for c in s.callees:
                self.callers[c].append(s.id)
            if not s.callees:
                self.unresolved_calls.append(s.id)

    # ------------------------------------------------------------------
    # 노드별 정의(쓰기)·사용(읽기)
    # ------------------------------------------------------------------

    def _rv(self, e, D: set, U: set):
        if e is None:
            return
        if isinstance(e, c_ast.ID):
            key = self.bind.get(id(e))
            if key and not key.startswith("fn:") and not self._is_array_path((key,)):
                U.add((key,))
            return
        if isinstance(e, (c_ast.StructRef, c_ast.ArrayRef)) or (isinstance(e, c_ast.UnaryOp) and e.op == "*"):
            for l in self.lvals(e):
                if not self._is_array_path(l) and not l[0].startswith("fn:"):
                    U.add(l)
            self._addr(e, D, U)
            return
        if isinstance(e, c_ast.UnaryOp):
            if e.op == "&":
                self._addr(e.expr, D, U)
                self.pvals(e)
            elif e.op in ("++", "--", "p++", "p--"):
                L = self.lvals(e.expr)
                D |= L
                U |= L
                self._addr(e.expr, D, U)
            elif e.op not in ("sizeof", "_Alignof"):
                self._rv(e.expr, D, U)
            return
        if isinstance(e, c_ast.Assignment):
            L = self.lvals(e.lvalue)
            D |= L
            self._addr(e.lvalue, D, U)
            self._rv(e.rvalue, D, U)
            if e.op != "=":
                U |= L
            return
        if isinstance(e, c_ast.FuncCall):
            sid = self.site_of.get(id(e))
            if sid is not None:
                U.add((f"{self.sites[sid].caller}::$out{sid}",))
            return
        if isinstance(e, c_ast.Cast):
            self._rv(e.expr, D, U)
            return
        if isinstance(e, c_ast.NamedInitializer):
            self._rv(e.expr, D, U)
            return
        if isinstance(e, c_ast.CompoundLiteral):
            self._rv(e.init, D, U)
            return
        if isinstance(e, (c_ast.Typename, c_ast.Constant)):
            return
        for _, c in e.children():
            self._rv(c, D, U)

    def _addr(self, lv, D, U):
        """lvalue 의 주소를 계산하는 데 필요한 읽기."""
        if isinstance(lv, c_ast.StructRef):
            if lv.type == ".":
                self._addr(lv.name, D, U)
            else:
                self._rv(lv.name, D, U)
        elif isinstance(lv, c_ast.ArrayRef):
            self._rv(lv.subscript, D, U)
            self._rv(lv.name, D, U)
        elif isinstance(lv, c_ast.UnaryOp) and lv.op == "*":
            self._rv(lv.expr, D, U)
        elif isinstance(lv, c_ast.Cast):
            self._addr(lv.expr, D, U)

    @staticmethod
    def _direct(lv) -> bool:
        if isinstance(lv, c_ast.ID):
            return True
        if isinstance(lv, c_ast.StructRef) and lv.type == ".":
            return Program._direct(lv.name)
        return False

    def _strong(self, lv, L: set) -> set:
        if len(L) == 1 and self._direct(lv):
            (p,) = L
            if "[]" not in p:
                return {p}
        return set()

    def _effects(self, n: Node):
        D: set = set()
        U: set = set()
        S: set = set()
        e = n.ast
        if n.kind == "stmt":
            if isinstance(e, c_ast.Decl):
                p = (n.var,)
                D.add(p)
                S.add(p)
                self._rv(e.init, D, U)
            else:
                self._rv(e, D, U)
                top = e
                while isinstance(top, c_ast.Cast):
                    top = top.expr
                if isinstance(top, c_ast.Assignment):
                    S |= self._strong(top.lvalue, self.lvals(top.lvalue))
                elif isinstance(top, c_ast.UnaryOp) and top.op in ("++", "--", "p++", "p--"):
                    S |= self._strong(top.expr, self.lvals(top.expr))
        elif n.kind in ("cond", "return", "actual_in"):
            self._rv(e, D, U)
        elif n.kind == "call":
            s = self.sites[n.site]
            if s.direct is None:
                self._rv(s.ast.name, D, U)
        elif n.kind == "actual_out":
            s = self.sites[n.site]
            p = (f"{s.caller}::$out{s.id}",)
            D.add(p)
            S.add(p)
            if not s.callees:  # 호출 대상을 풀지 못함: 모든 인자에 의존한다고 본다
                for a in s.actual_in:
                    U |= self.nodes[a].uses
        elif n.kind == "extcall":
            self._extcall_effects(n, D, U, S)
        elif n.kind in ("formal_in", "ginit"):
            p = (n.var,)
            D.add(p)
            S.add(p)
        n.defs, n.uses, n.strong = D, U, S
        for p in D | U:
            if p[0].startswith("hw:"):
                self.hw_access.add(display_path(self, p))

    def _extcall_effects(self, n: Node, D, U, S):
        s = self.sites[n.site]
        args = s.ast.args.exprs if s.ast.args else []
        for a in args:
            self._rv(a, D, U)
        out = (f"{s.caller}::$out{s.id}",)
        D.add(out)
        S.add(out)
        model = self.extern_model.get(s.name)
        proto = self.protos.get(s.name)
        params = proto[0].args.params if proto and proto[0].args else []
        for i, a in enumerate(args):
            if model is not None and model.get("pure"):
                break
            objs = self.pvals(a) if self._pointerish(a, params, i) else set()
            objs = {o for o in objs if not o[0].startswith("fn:")}
            if not objs:
                continue
            if model is not None and ("writes_args" in model or "reads_args" in model):
                if i in model.get("writes_args", []):
                    D |= objs
                if i in model.get("reads_args", []) or i in model.get("writes_args", []):
                    U |= objs
            else:
                U |= objs
                if not self._const_pointer_param(params, i, proto):
                    D |= objs
        if model:
            for g in model.get("reads", []):
                U.add((g,))
            for g in model.get("writes", []):
                D.add((g,))

    def _pointerish(self, a, params, i) -> bool:
        if isinstance(a, c_ast.UnaryOp) and a.op == "&":
            return True
        for l in self.lvals(a) if isinstance(a, (c_ast.ID, c_ast.StructRef, c_ast.ArrayRef)) else ():
            t, env = self.path_type(l)
            if env is not None and (env.is_pointer(t) or env.is_array(t)):
                return True
            if env is None and self.pts.get(l):
                return True
        return bool(self.pvals(a)) and not isinstance(a, (c_ast.ID, c_ast.StructRef, c_ast.ArrayRef))

    @staticmethod
    def _const_pointer_param(params, i, proto) -> bool:
        if i >= len(params) or proto is None:
            return False
        p = params[i]
        if not isinstance(p, (c_ast.Decl, c_ast.Typename)):
            return False
        env = proto[1]
        t = env.core(p.type)
        if isinstance(t, c_ast.PtrDecl):
            inner = t.type
            quals = getattr(inner, "quals", None) or []
            if isinstance(inner, c_ast.TypeDecl) and "const" in quals:
                return True
        return False

    # ------------------------------------------------------------------
    # 데이터 흐름
    # ------------------------------------------------------------------

    def _tracked_vars(self) -> set[str]:
        """흐름 민감하게 추적할 변수: 주소가 노출되지 않은 지역 변수·인자·임시값."""
        taken = set(self.address_taken)
        for vals in self.pts.values():
            taken.update(p[0] for p in vals)
        return {k for k, v in self.vars.items() if v.kind in ("local", "param", "temp") and k not in taken}

    def _function_dataflow(self, f: Function, tracked: set[str]):
        ids = f.nodes
        idx = {nid: i for i, nid in enumerate(ids)}
        n = len(ids)
        succ = [[(idx[t], l) for t, l in f.succ.get(nid, []) if t in idx] for nid in ids]
        e_i, x_i = idx[f.entry], idx[f.exit]

        # 도달 가능성 (상수 조건으로 막힌 코드는 dead)
        reach = [False] * n
        stack = [e_i]
        while stack:
            i = stack.pop()
            if reach[i]:
                continue
            reach[i] = True
            stack.extend(t for t, _ in succ[i])
        for i, nid in enumerate(ids):
            if not reach[i]:
                self.nodes[nid].dead = True
        f.no_return = not reach[x_i]

        # 후위 지배자: 출구에 닿지 않는 노드(무한 루프)는 가짜 간선으로 출구에 잇는다
        pred = [[] for _ in range(n)]
        for i in range(n):
            for t, _ in succ[i]:
                pred[t].append(i)
        fake: list[int] = []
        while True:
            to_exit = [False] * n
            stack = [x_i]
            while stack:
                i = stack.pop()
                if to_exit[i]:
                    continue
                to_exit[i] = True
                stack.extend(pred[i])
            missing = [i for i in range(n) if reach[i] and not to_exit[i]]
            if not missing:
                break
            loops = [i for i in missing if self.nodes[ids[i]].kind in ("cond", "nop")] or missing
            i = loops[0]
            succ[i].append((x_i, None))
            pred[x_i].append(i)
            fake.append(i)

        nodes_r = [i for i in range(n) if reach[i]]
        full = 0
        for i in nodes_r:
            full |= 1 << i
        pdom = [full] * n
        pdom[x_i] = 1 << x_i
        order = list(reversed(nodes_r))
        changed = True
        while changed:
            changed = False
            for i in order:
                if i == x_i:
                    continue
                ss = [t for t, _ in succ[i] if reach[t]]
                acc = full
                for t in ss:
                    acc &= pdom[t]
                if not ss:
                    acc = 0
                new = acc | (1 << i)
                if new != pdom[i]:
                    pdom[i] = new
                    changed = True
        ipdom = [-1] * n
        for i in nodes_r:
            strict = pdom[i] & ~(1 << i)
            cnt = bin(strict).count("1")
            j = 0
            s = strict
            while s:
                if s & 1 and bin(pdom[j]).count("1") == cnt:
                    ipdom[i] = j
                    break
                s >>= 1
                j += 1

        # 제어 의존성 (Ferrante). 어느 분기에도 의존하지 않는 노드(진입을 후위 지배)는 함수 진입에 의존시킨다.
        edges = [(i, t, l) for i in nodes_r for t, l in succ[i] if reach[t] and not (i in fake and t == x_i)]
        for a, b, label in edges:
            cd_seen = self._cd_seen.setdefault(ids[a], set())
            if pdom[a] >> b & 1 and a != b:
                continue
            runner = b
            stop = ipdom[a]
            guard = 0
            while runner != -1 and runner != stop and guard < n + 1:
                guard += 1
                if runner != x_i:
                    na, nr = ids[a], ids[runner]
                    if (nr, label) not in cd_seen:
                        cd_seen.add((nr, label))
                        self.cd_children[na].append((nr, label))
                        self.cd_parents[nr].append((na, label))
                runner = ipdom[runner]
        for i in nodes_r:
            nid = ids[i]
            if i not in (e_i, x_i) and not self.cd_parents.get(nid):
                self.cd_children[f.entry].append((nid, None))
                self.cd_parents[nid].append((f.entry, None))

        # 도달 정의 (추적 변수만)
        sites: list[tuple[int, tuple]] = []
        gen = [0] * n
        for i, nid in enumerate(ids):
            for p in self.nodes[nid].defs:
                if p[0] in tracked:
                    gen[i] |= 1 << len(sites)
                    sites.append((i, p))
        kill = [0] * n
        for i, nid in enumerate(ids):
            for sp in self.nodes[nid].strong:
                if sp[0] not in tracked:
                    continue
                for k, (j, p) in enumerate(sites):
                    if j != i and p[: len(sp)] == sp:
                        kill[i] |= 1 << k
        IN = [0] * n
        OUT = [0] * n
        preds_r = [[] for _ in range(n)]
        for i in range(n):
            for t, _ in succ[i]:
                if not (i in fake and t == x_i):
                    preds_r[t].append(i)
        changed = True
        while changed:
            changed = False
            for i in nodes_r:
                inn = 0
                for p in preds_r[i]:
                    inn |= OUT[p]
                out = gen[i] | (inn & ~kill[i])
                if inn != IN[i] or out != OUT[i]:
                    IN[i], OUT[i] = inn, out
                    changed = True
        for i, nid in enumerate(ids):
            node = self.nodes[nid]
            if node.dead:
                continue
            uses = [u for u in node.uses if u[0] in tracked]
            if not uses:
                continue
            bits = IN[i]
            k = 0
            seen = set()
            while bits:
                if bits & 1:
                    j, p = sites[k]
                    for u in uses:
                        if overlaps(p, u) and (ids[j], p) not in seen:
                            seen.add((ids[j], p))
                            self.dd[ids[j]].append((nid, display_path(self, p)))
                            break
                bits >>= 1
                k += 1

    def _memory_index(self, tracked: set[str]):
        for n in self.nodes:
            if n.dead:
                continue
            for p in n.defs:
                if p[0] not in tracked:
                    self.def_index[p[0]].append((n.id, p))
            for p in n.uses:
                if p[0] not in tracked:
                    self.use_index[p[0]].append((n.id, p))

    # ------------------------------------------------------------------
    # 조회
    # ------------------------------------------------------------------

    def func_display(self, key: str) -> str:
        if not key:
            return "(전역 초기화)"
        return key.split("@")[0]

    def call_graph(self) -> dict[str, set[str]]:
        g: dict[str, set[str]] = defaultdict(set)
        for s in self.sites:
            g[s.caller] |= s.callees
        return g

    def address_taken_funcs(self) -> set[str]:
        out = set()
        for vals in self.pts.values():
            out.update(p[0][3:] for p in vals if p[0].startswith("fn:"))
        return out

    def roots(self) -> list[str]:
        """호출자가 없는 함수 = 태스크·ISR·콜백·외부 API 진입점 후보."""
        called = {c for s in self.sites for c in s.callees}
        return sorted(k for k in self.funcs if k not in called)

    def summary(self) -> dict:
        return {
            "files": [tu.rel for tu in self.tus],
            "functions": len(self.funcs),
            "nodes": len(self.nodes),
            "call_sites": len(self.sites),
        }


# ---------------------------------------------------------------------------
# 함수 본문 → CFG 노드
# ---------------------------------------------------------------------------


class _FuncBuilder:
    def __init__(self, prog: Program, fn: Function, fdef: c_ast.FuncDef, tu: TranslationUnit):
        self.p = prog
        self.fn = fn
        self.fdef = fdef
        self.tu = tu
        self.rel = tu.rel
        self.env = tu.env
        self.scopes: list[dict[str, str]] = [{}]
        self.breaks: list[list] = []
        self.continues: list[list] = []
        self.labels: dict[str, int] = {}
        self.gotos: list[tuple[list, str]] = []
        self.depth = 0
        self._names: dict[str, int] = defaultdict(int)

    # 노드·간선
    def _coord(self, ast):
        c = getattr(ast, "coord", None)
        if c is None:
            return self.fn.file, self.fn.line
        return c.file, c.line

    def node(self, kind, ast, preds, text=None, sig=None, coord_ast=None, **kw) -> int:
        file, line = self._coord(coord_ast if coord_ast is not None else ast)
        text = text if text is not None else gen(ast)
        sig = sig if sig is not None else f"{kind}|{self.depth}|{norm(ast)}"
        n = self.p._new_node(self.fn.key, kind, file, line, text, sig=sig, ast=ast, depth=self.depth, **kw)
        self.link(preds, n.id)
        return n.id

    def link(self, preds, nid):
        for p, label in preds:
            self.fn.succ[p].append((nid, label))

    def new_var(self, name, kind, type_, coord) -> str:
        self._names[name] += 1
        suffix = "" if self._names[name] == 1 else f"#{self._names[name]}"
        key = f"{self.fn.key}::{name}{suffix}"
        self.p.vars[key] = Var(key, name, kind, self.fn.key, coord.file if coord else self.fn.file,
                               coord.line if coord else self.fn.line, type_, self.env, True)
        return key

    def bind(self, e):
        if e is not None:
            self.p._bind_expr(e, self.scopes, self.rel)
            for x in walk(e):
                if isinstance(x, c_ast.Assignment):
                    self.p._assigns.append((self.fn.key, x))

    # 진입
    def build(self):
        fn = self.fn
        fn.entry = self.node("entry", self.fdef.decl, [], text=f"{fn.name}() 진입", sig="entry")
        preds = [(fn.entry, None)]
        fdecl = self.fdef.decl.type
        params = fdecl.args.params if fdecl.args else []
        for i, prm in enumerate(params):
            if not isinstance(prm, c_ast.Decl) or prm.name is None:
                continue
            key = self.new_var(prm.name, "param", prm.type, prm.coord)
            self.scopes[0][prm.name] = key
            fn.params.append(key)
            tsig = self.env.signature(prm.type)
            nid = self.node("formal_in", prm, preds, text=f"인자 {prm.name}", sig=f"formal_in|{prm.name}|{tsig}", var=key)
            fn.formal_in.append(nid)
            preds = [(nid, None)]
        out = self.stmt(self.fdef.body, preds)
        fn.exit = self.node("exit", None, [], text=f"{fn.name}() 종료", sig="exit", coord_ast=self.fdef.body)
        self.p.nodes[fn.exit].line = self._end_line()
        self.link(out, fn.exit)
        for r in fn.returns:
            self.link([(r, None)], fn.exit)
        for preds_, label in self.gotos:
            if label in self.labels:
                self.link(preds_, self.labels[label])
            else:
                self.p.warnings.append(f"{fn.name}: goto 대상 '{label}' 를 찾지 못했습니다.")

    def _end_line(self) -> int:
        last = self.fn.line
        for x in walk(self.fdef.body):
            c = getattr(x, "coord", None)
            if c is not None and c.line and c.file == self.fn.file:
                last = max(last, c.line)
        return last

    # 식 안의 호출
    def calls(self, e, preds):
        if e is None:
            return preds
        self.bind(e)
        for call in self._calls_postorder(e):
            preds = self.call(call, preds)
        return preds

    def _calls_postorder(self, e):
        out = []

        def visit(n):
            if n is None:
                return
            if isinstance(n, c_ast.UnaryOp) and n.op in ("sizeof", "_Alignof"):
                return
            for _, c in n.children():
                visit(c)
            if isinstance(n, c_ast.FuncCall):
                out.append(n)

        visit(e)
        return out

    def call(self, call: c_ast.FuncCall, preds):
        p = self.p
        direct = None
        name = gen(call.name)
        if isinstance(call.name, c_ast.ID):
            key = p.bind.get(id(call.name))
            if key and key.startswith("fn:"):
                direct = key[3:]
                name = call.name.name
        external = direct is not None and direct not in p.funcs
        sid = len(p.sites)
        site = CallSite(sid, self.fn.key, call, name, direct, external)
        p.sites.append(site)
        p.site_of[id(call)] = sid
        self.new_temp(sid)
        args = call.args.exprs if call.args else []
        if external:
            nid = self.node("extcall", call, preds, sig=f"extcall|{self.depth}|{norm(call)}", site=sid)
            site.call_node = nid
            return [(nid, None)]
        for i, a in enumerate(args):
            nid = self.node("actual_in", a, preds, text=f"{name}() 인자{i + 1}: {gen(a)}",
                            sig=f"actual_in|{self.depth}|{name}|{i}|{norm(a)}", site=sid, arg=i)
            site.actual_in.append(nid)
            preds = [(nid, None)]
        nid = self.node("call", call, preds, text=f"{gen(call)} 호출", sig=f"call|{self.depth}|{norm(call.name)}", site=sid)
        site.call_node = nid
        out = self.node("actual_out", call, [(nid, None)], text=f"{name}() 반환값 수신",
                        sig=f"actual_out|{self.depth}|{name}", site=sid)
        site.actual_out = out
        return [(out, None)]

    def new_temp(self, sid):
        key = f"{self.fn.key}::$out{sid}"
        self.p.vars[key] = Var(key, f"$out{sid}", "temp", self.fn.key, self.fn.file, self.fn.line)

    # 문장
    def stmt(self, s, preds):
        if s is None:
            return preds
        t = type(s).__name__
        handler = getattr(self, "s_" + t, None)
        if handler is not None:
            return handler(s, preds)
        if isinstance(s, (c_ast.EmptyStatement, c_ast.Pragma, c_ast.Typedef)) or t in ("StaticAssert", "Alignas"):
            if isinstance(s, c_ast.Typedef):
                self.env.typedefs[s.name] = s.type
            return preds
        return self.expr_stmt(s, preds)

    def expr_stmt(self, e, preds):
        preds = self.calls(e, preds)
        top = e
        while isinstance(top, c_ast.Cast):
            top = top.expr
        if isinstance(top, c_ast.FuncCall):
            return preds
        if isinstance(top, (c_ast.ID, c_ast.Constant)):
            return preds  # 효과 없는 식 (예: (void)x;)
        nid = self.node("stmt", e, preds)
        return [(nid, None)]

    def s_Compound(self, s, preds):
        self.scopes.append({})
        for item in s.block_items or []:
            preds = self.stmt(item, preds)
        self.scopes.pop()
        return preds

    def s_DeclList(self, s, preds):
        for d in s.decls:
            preds = self.s_Decl(d, preds)
        return preds

    def s_Decl(self, d, preds):
        if d.name is None:
            if isinstance(d.type, (c_ast.Struct, c_ast.Union, c_ast.Enum)):
                self.env.collect(d)
            return preds
        storage = d.storage or []
        if isinstance(d.type, c_ast.FuncDecl):
            return preds
        if "extern" in storage:
            self.scopes[-1][d.name] = self.p.lookup(d.name, [], self.rel) or d.name
            return preds
        if "static" in storage:
            key = self.new_var(d.name, "static_local", d.type, d.coord)
            v = self.p.vars[key]
            v.init = d.init
            self.scopes[-1][d.name] = key
            if d.init is not None:
                self.p._bind_expr(d.init, self.scopes, self.rel)
            return preds
        key = self.new_var(d.name, "local", d.type, d.coord)
        self.scopes[-1][d.name] = key
        if d.init is None:
            return preds
        preds = self.calls(d.init, preds)
        self.p._inits.append(((key,), d.init))
        tsig = self.env.signature(d.type)
        nid = self.node("stmt", d, preds, sig=f"decl|{self.depth}|{tsig}|{d.name}|{norm(d.init)}", var=key)
        return [(nid, None)]

    def _cond(self, cond, preds, text=None, sig=None):
        preds = self.calls(cond, preds)
        cv = const_eval(cond, self.p.enums[self.rel])
        nid = self.node("cond", cond, preds, text=text, sig=sig)
        t = [(nid, "T")] if cv is None or cv else []
        f = [(nid, "F")] if cv is None or not cv else []
        return nid, t, f

    def s_If(self, s, preds):
        c, t, f = self._cond(s.cond, preds, text=f"if ({gen(s.cond)})", sig=f"if|{self.depth}|{norm(s.cond)}")
        self.depth += 1
        t_out = self.stmt(s.iftrue, t)
        f_out = self.stmt(s.iffalse, f) if s.iffalse is not None else f
        self.depth -= 1
        return t_out + f_out

    def _loop(self, header_preds, cond, body, nxt=None, kind="while"):
        h = self.node("nop", None, header_preds, text="loop", sig="nop", coord_ast=cond if cond is not None else body)
        self.breaks.append([])
        self.continues.append([])
        if cond is not None:
            c, t, f = self._cond(cond, [(h, None)], text=f"{kind} ({gen(cond)})", sig=f"{kind}|{self.depth}|{norm(cond)}")
        else:
            t, f = [(h, None)], []
        self.depth += 1
        out = self.stmt(body, t)
        out = out + self.continues.pop()
        if nxt is not None:
            out = self.expr_stmt(nxt, out)
        self.depth -= 1
        self.link(out, h)
        return f + self.breaks.pop()

    def s_While(self, s, preds):
        return self._loop(preds, s.cond, s.stmt)

    def s_For(self, s, preds):
        self.scopes.append({})
        if isinstance(s.init, c_ast.DeclList):
            preds = self.s_DeclList(s.init, preds)
        elif s.init is not None:
            preds = self.expr_stmt(s.init, preds)
        out = self._loop(preds, s.cond, s.stmt, s.next, kind="for")
        self.scopes.pop()
        return out

    def s_DoWhile(self, s, preds):
        h = self.node("nop", None, preds, text="do", sig="nop", coord_ast=s)
        self.breaks.append([])
        self.continues.append([])
        self.depth += 1
        out = self.stmt(s.stmt, [(h, None)])
        self.depth -= 1
        out = out + self.continues.pop()
        c, t, f = self._cond(s.cond, out, text=f"do-while ({gen(s.cond)})", sig=f"dowhile|{self.depth}|{norm(s.cond)}")
        self.link(t, h)
        return f + self.breaks.pop()

    def s_Switch(self, s, preds):
        items = s.stmt.block_items or [] if isinstance(s.stmt, c_ast.Compound) else [s.stmt]
        labels = []
        for it in items:
            if isinstance(it, c_ast.Case):
                labels.append(norm(it.expr))
            elif isinstance(it, c_ast.Default):
                labels.append("default")
        preds = self.calls(s.cond, preds)
        c = self.node("cond", s.cond, preds, text=f"switch ({gen(s.cond)})",
                      sig=f"switch|{self.depth}|{norm(s.cond)}|{','.join(labels)}")
        enums = self.p.enums[self.rel]
        self.p.nodes[c].cases = [v for v in (const_eval(it.expr, enums) for it in items if isinstance(it, c_ast.Case))
                                 if v is not None]
        self.breaks.append([])
        self.depth += 1
        self.scopes.append({})
        ft: list = []
        has_default = False
        for it in items:
            if isinstance(it, c_ast.Case):
                self.bind(it.expr)
                ft = self._seq(it.stmts, ft + [(c, f"case {gen(it.expr)}")])
            elif isinstance(it, c_ast.Default):
                has_default = True
                ft = self._seq(it.stmts, ft + [(c, "default")])
            else:
                ft = self.stmt(it, ft)
        self.scopes.pop()
        self.depth -= 1
        out = ft + self.breaks.pop()
        if not has_default:
            out.append((c, "default"))
        return out

    def _seq(self, stmts, preds):
        for x in stmts or []:
            preds = self.stmt(x, preds)
        return preds

    def s_Case(self, s, preds):
        return self._seq(s.stmts, preds)

    def s_Default(self, s, preds):
        return self._seq(s.stmts, preds)

    def s_Return(self, s, preds):
        preds = self.calls(s.expr, preds)
        tsig = self.env.signature(self.fn.ret_type)
        text = f"return {gen(s.expr)}" if s.expr is not None else "return"
        nid = self.node("return", s.expr, preds, text=text, sig=f"return|{self.depth}|{tsig}|{norm(s.expr)}", coord_ast=s)
        self.fn.returns.append(nid)
        return []

    def s_Break(self, s, preds):
        if self.breaks:
            self.breaks[-1].extend(preds)
        return []

    def s_Continue(self, s, preds):
        if self.continues:
            self.continues[-1].extend(preds)
        return []

    def s_Goto(self, s, preds):
        self.gotos.append((preds, s.name))
        return []

    def s_Label(self, s, preds):
        nid = self.node("nop", None, preds, text=f"{s.name}:", sig="nop", coord_ast=s)
        self.labels[s.name] = nid
        return self.stmt(s.stmt, [(nid, None)])


def load_extern_model(path: str | None) -> dict:
    if not path:
        return {}
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return {k: v for k, v in data.items() if isinstance(v, dict)}  # "_comment" 같은 설명 항목 제외
