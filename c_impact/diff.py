"""변경 전후 프로그램 비교 → 변경 지점(슬라이싱 시작점) 추출.

비교는 전처리 후 AST 를 정규화한 문장 서명으로 한다. 그래서
  - 헤더의 #define 값 변경, typedef·구조체 정의 변경, 전역 초기값 변경도 드러나고
  - 줄 번호 이동, 정수 상수 표기(100U ↔ 0x64) 차이는 변경으로 보지 않는다.
삭제된 문장은 변경 전 프로그램에서, 추가·수정된 문장은 변경 후 프로그램에서 시작점이 된다
(수정된 문장은 양쪽 모두에서 시작점).

단일 변수 비교식(예: tick >= 100U → tick >= 200U)은 두 판정이 달라지는 입력 구간을 계산한다.
"""

from __future__ import annotations

import difflib
import os
import re
from dataclasses import dataclass, field

from pycparser import c_ast

from .cexpr import const_eval, gen, norm
from .program import Node, Program

INF = float("inf")


@dataclass
class Change:
    version: str  # old | new
    kind: str  # changed | added | removed | func_added | func_removed | global_changed | global_added | global_removed | type_changed
    node: int
    func: str
    file: str
    line: int
    text: str
    other_text: str = ""  # 짝이 되는 반대 버전 문장
    other_node: int | None = None
    detail: str = ""
    interval: dict | None = None


@dataclass
class ChangeSet:
    changes: list[Change] = field(default_factory=list)
    seeds_old: set[int] = field(default_factory=set)
    seeds_new: set[int] = field(default_factory=set)
    changed_funcs: set[str] = field(default_factory=set)
    notes: list[str] = field(default_factory=list)  # 동작에 영향 없다고 보고 시작점에서 뺀 변경

    def add(self, ch: Change):
        self.changes.append(ch)
        (self.seeds_old if ch.version == "old" else self.seeds_new).add(ch.node)
        if ch.func:
            self.changed_funcs.add(ch.func)


def _seq(prog: Program, fkey: str) -> list[Node]:
    return [prog.nodes[i] for i in prog.funcs[fkey].nodes if prog.nodes[i].visible]


def _sig(n: Node) -> str:
    return _plain_sig(n) + ("|dead" if n.dead else "")


def _plain_sig(n: Node) -> str:
    """인자·지역 선언은 풀어 쓴 자료형 대신 소스에 적힌 자료형 이름으로 비교한다.
    구조체에 필드가 추가된 것만으로 그 구조체를 쓰는 모든 선언이 '수정'으로 잡히지 않게 하고,
    자료형 자체의 변경은 _type_changes 가 변수 단위로 판단한다."""
    if n.kind == "formal_in" and n.ast is not None:
        return f"formal_in|{gen(n.ast)}"
    if n.kind == "stmt" and isinstance(n.ast, c_ast.Decl) and n.sig.startswith("decl|"):
        return f"decl|{n.depth}|{gen(n.ast)}"
    return n.sig


_DEPTH_RE = re.compile(r"^(\w+)\|\d+\|")


def _sig_no_depth(n: Node) -> str:
    """중첩 깊이를 뺀 서명: 같으면 문장은 그대로이고 감싸는 조건 구조만 바뀐 것."""
    return _DEPTH_RE.sub(r"\1||", _plain_sig(n))


def compute_changes(old: Program, new: Program) -> ChangeSet:
    cs = ChangeSet()
    for fkey in sorted(set(old.funcs) | set(new.funcs)):
        if fkey not in new.funcs:
            f = old.funcs[fkey]
            cs.add(Change("old", "func_removed", f.entry, fkey, f.file, f.line, f"{f.name}() 삭제"))
            cs.seeds_old.update(i for i in f.nodes if not old.nodes[i].dead)
            continue
        if fkey not in old.funcs:
            f = new.funcs[fkey]
            cs.add(Change("new", "func_added", f.entry, fkey, f.file, f.line, f"{f.name}() 추가"))
            cs.seeds_new.update(i for i in f.nodes if not new.nodes[i].dead)
            continue
        a, b = _seq(old, fkey), _seq(new, fkey)
        sm = difflib.SequenceMatcher(None, [_sig(n) for n in a], [_sig(n) for n in b], autojunk=False)
        for tag, i1, i2, j1, j2 in sm.get_opcodes():
            if tag == "equal":
                continue
            olds, news = a[i1:i2], b[j1:j2]
            pairs, used = [], set()
            for o in olds:
                match = next((n for n in news if n.id not in used and n.kind == o.kind), None)
                if match is not None:
                    used.add(match.id)
                pairs.append((o, match))
            for o, n in pairs:
                if n is None:
                    _add_node_change(cs, "old", "removed", old, o)
                elif _sig_no_depth(o) == _sig_no_depth(n) and o.dead == n.dead:
                    det = "같은 문장이 다른 조건 아래로 이동 (감싸는 if·반복문·switch 가 바뀜)"
                    cs.add(Change("old", "moved", o.id, fkey, o.file, o.line, o.text, n.text, n.id, det))
                    cs.add(Change("new", "moved", n.id, fkey, n.file, n.line, n.text, o.text, o.id, det))
                else:
                    det, iv = describe_change(old, o, new, n)
                    cs.add(Change("old", "changed", o.id, fkey, o.file, o.line, o.text, n.text, n.id, det, iv))
                    cs.add(Change("new", "changed", n.id, fkey, n.file, n.line, n.text, o.text, o.id, det, iv))
            for n in news:
                if n.id not in used:
                    _add_node_change(cs, "new", "added", new, n)
    _global_changes(cs, old, new)
    _type_changes(cs, old, new)
    return cs


def _add_node_change(cs: ChangeSet, version: str, kind: str, prog: Program, n: Node):
    cs.add(Change(version, kind, n.id, n.func, n.file, n.line, n.text,
                  detail="삭제된 문장" if kind == "removed" else "추가된 문장"))


def _ginits(prog: Program) -> dict[str, Node]:
    return {n.var: n for n in prog.nodes if n.kind == "ginit"}


def _global_changes(cs: ChangeSet, old: Program, new: Program):
    go, gn = _ginits(old), _ginits(new)
    for key in sorted(set(go) | set(gn)):
        o, n = go.get(key), gn.get(key)
        if o is None:
            cs.add(Change("new", "global_added", n.id, n.func, n.file, n.line, n.text, detail="새 전역/정적 변수"))
        elif n is None:
            cs.add(Change("old", "global_removed", o.id, o.func, o.file, o.line, o.text, detail="삭제된 전역/정적 변수"))
        elif o.sig != n.sig:
            if norm(o.ast) == norm(n.ast):
                continue  # 초기값은 같고 자료형만 바뀜 → _type_changes 가 보고하고 시작점으로 잡는다
            v, vo_ = new.vars.get(key), old.vars.get(key)
            full = _init_diff(o.ast, n.ast, v.type if v else None, v.env if v else None,
                              t_old=vo_.type if vo_ else None, env_old=vo_.env if vo_ else None)
            diffs = [d[:3] for d in full]
            name = v.name if v else key
            if not diffs:
                continue  # 원소·필드 단위로는 같음 (표기만 다름)
            if all(d[3] for d in full):
                cs.notes.append(f"{name} ({_relp(new, n.file)}:{n.line}): 새 필드 {len(diffs)}개를 0으로 초기화 — "
                                "정적 변수는 원래 0으로 초기화되므로 동작 변화 없음")
                continue
            vo, vn = const_eval(o.ast) if o.ast is not None else 0, const_eval(n.ast) if n.ast is not None else 0
            if vo is not None and vn is not None and vo != vn:
                det = f"초기값 {vo} → {vn}"
            else:
                det = f"초기값 {len(diffs)}곳 변경"
            old_t, new_t = _fmt_init_diffs(name, diffs, 1), _fmt_init_diffs(name, diffs, 2)
            cs.add(Change("old", "global_changed", o.id, o.func, o.file, o.line, old_t, new_t, n.id, det))
            cs.add(Change("new", "global_changed", n.id, n.func, n.file, n.line, new_t, old_t, o.id, det))


def _relp(prog: Program, file: str) -> str:
    try:
        return os.path.relpath(file, prog.root).replace("\\", "/")
    except (ValueError, TypeError):
        return file


def _short(s: str, n: int = 80) -> str:
    s = " ".join(s.split())
    return s if len(s) <= n else s[: n - 1] + "…"


def _fmt_init_diffs(name: str, diffs: list[tuple[str, str, str]], side: int, limit: int = 6) -> str:
    parts = [f"{name}{d[0]} = {_short(d[side])}" for d in diffs[:limit]]
    if len(diffs) > limit:
        parts.append(f"외 {len(diffs) - limit}곳")
    return "; ".join(parts)


_MISSING = "(없음)"


def init_flat(e, t=None, env=None, path: str = "", out: dict | None = None) -> dict:
    """초기값을 (원소·필드 위치 → 값 AST)로 펼친다. 위치 이름은 그 버전의 자료형으로 정한다."""
    out = {} if out is None else out
    if not isinstance(e, c_ast.InitList):
        out[path] = e
        return out
    core = env.core(t) if env is not None and t is not None else None
    names = env.fields(t) if isinstance(core, (c_ast.Struct, c_ast.Union)) else []
    idx = 0
    for x in e.exprs:
        sub_t, label = None, f"[{idx}]"
        if isinstance(x, c_ast.NamedInitializer):
            label = "".join(f".{d.name}" if isinstance(d, c_ast.ID) else f"[{gen(d)}]" for d in x.name)
            first = x.name[0] if x.name else None
            if len(x.name) == 1 and isinstance(first, c_ast.ID) and names:
                sub_t = env.field(t, first.name)
                idx = names.index(first.name) + 1 if first.name in names else idx + 1
            elif len(x.name) == 1 and not isinstance(first, c_ast.ID) and isinstance(core, c_ast.ArrayDecl):
                sub_t = env.element(t)
                v = const_eval(first)
                idx = int(v) + 1 if v is not None else idx + 1
            else:
                idx += 1
            x = x.expr
        elif isinstance(core, c_ast.ArrayDecl):
            sub_t = env.element(t)
            idx += 1
        elif names and idx < len(names):
            label, sub_t = f".{names[idx]}", env.field(t, names[idx])
            idx += 1
        else:
            idx += 1
        init_flat(x, sub_t, env, path + label, out)
    return out


def _txt(e) -> str:
    return gen(e) if e is not None else "(0으로 초기화)"


def init_diff(o, n, t=None, env=None, path: str = "", t_old=None, env_old=None) -> list[tuple[str, str, str]]:
    """초기값 두 개를 원소·필드 단위로 비교해 (위치, 변경 전, 변경 후) 목록을 돌려준다.
    변경 전 값은 변경 전 자료형으로, 변경 후 값은 변경 후 자료형으로 위치를 매겨
    구조체에 필드가 끼어들어도 같은 이름의 필드끼리 비교한다."""
    return [(k, a, b) for k, a, b, _ in _init_diff(o, n, t, env, path, t_old, env_old)]


def _init_diff(o, n, t=None, env=None, path: str = "", t_old=None, env_old=None) -> list[tuple[str, str, str, bool]]:
    """(위치, 변경 전, 변경 후, 변경 전에 없던 위치에 0을 넣은 것인지)."""
    if norm(o) == norm(n):
        return []
    if t_old is None:
        t_old, env_old = t, env
    if not (isinstance(o, c_ast.InitList) or isinstance(n, c_ast.InitList)):
        return [(path, _txt(o), _txt(n), False)]
    fo = init_flat(o, t_old, env_old, path)
    fn = init_flat(n, t, env, path)
    out = []
    for k in list(fn) + [k for k in fo if k not in fn]:
        if k in fo and k in fn:
            if norm(fo[k]) != norm(fn[k]):
                out.append((k, _txt(fo[k]), _txt(fn[k]), False))
        elif k in fn:
            out.append((k, _MISSING, _txt(fn[k]), fn[k] is not None and const_eval(fn[k]) == 0))
        else:
            out.append((k, _txt(fo[k]), _MISSING, False))
    return out


def type_diff(vo, vn) -> str:
    """자료형 변경을 짧게: 구조체는 추가·삭제·바뀐 필드만, 그 밖은 앞뒤 자료형."""
    to, tn = vo.env.core(vo.type), vn.env.core(vn.type)
    dims = ""
    while (isinstance(to, c_ast.ArrayDecl) and isinstance(tn, c_ast.ArrayDecl)) or \
            (isinstance(to, c_ast.PtrDecl) and isinstance(tn, c_ast.PtrDecl)):
        if isinstance(to, c_ast.ArrayDecl):
            do, dn = gen(to.dim), gen(tn.dim)
            if do != dn:
                dims = f"배열 크기 [{do}] → [{dn}]; "
        else:
            dims += "*"
        to, tn = vo.env.core(to.type), vn.env.core(tn.type)
    tname = ""
    t = vn.type
    while True:
        while isinstance(t, (c_ast.ArrayDecl, c_ast.PtrDecl)):
            t = t.type
        # 포인터 typedef 는 한 번 풀어서 가리키는 자료형 이름을 찾는다
        if isinstance(t, c_ast.TypeDecl) and isinstance(t.type, c_ast.IdentifierType) and dims.endswith("*") \
                and isinstance(vn.env.core(t), c_ast.PtrDecl) and t.type.names[0] in vn.env.typedefs:
            t = vn.env.typedefs[t.type.names[0]]
            continue
        break
    if isinstance(t, c_ast.TypeDecl) and isinstance(t.type, c_ast.IdentifierType):
        tname = " ".join(t.type.names)
    stars = dims.count("*")
    dims = dims.replace("*", "")
    tname = (tname + "*" * stars + (" (포인터)" if stars else "") + ": ") if tname else ("포인터: " if stars else "")
    if isinstance(to, (c_ast.Struct, c_ast.Union)) and isinstance(tn, (c_ast.Struct, c_ast.Union)) and to.decls and tn.decls:
        fo = {d.name: vo.env.signature(d.type) for d in to.decls if d.name}
        fn = {d.name: vn.env.signature(d.type) for d in tn.decls if d.name}
        parts = []
        added = [f"{k}({_short(fn[k], 30)})" for k in fn if k not in fo]
        removed = [k for k in fo if k not in fn]
        changed = [f"{k}: {_short(fo[k], 30)} → {_short(fn[k], 30)}" for k in fn if k in fo and fo[k] != fn[k]]
        if added:
            parts.append("필드 추가 " + ", ".join(added))
        if removed:
            parts.append("필드 삭제 " + ", ".join(removed))
        if changed:
            parts.append("필드 자료형 변경 " + ", ".join(changed))
        if [k for k in fo if k in fn] != [k for k in fn if k in fo]:
            parts.append("필드 순서 변경")
        return f"자료형 변경: {tname}{dims}" + ("; ".join(parts) or "구조체 안쪽 정의 변경")
    return f"자료형 변경: {tname}{dims}{_short(vo.env.signature(vo.type), 100)} → {_short(vn.env.signature(vn.type), 100)}"


def _type_sigs(prog: Program) -> dict[str, str]:
    return {k: v.env.signature(v.type) for k, v in prog.vars.items()
            if v.env is not None and v.type is not None and v.kind not in ("sym", "hw", "temp")}


def struct_delta(vo, vn):
    """자료형 변경이 '구조체 정의 변경'뿐이면 (바뀐 필드 이름 집합, 포인터를 거쳐 가리키는지)를 돌려준다.
    바뀐 필드 = 추가·삭제·자료형이 바뀐 필드. 배열 크기나 기본 자료형이 바뀐 경우는 None."""
    to, tn = vo.env.core(vo.type), vn.env.core(vn.type)
    via_ptr = False
    while True:
        if isinstance(to, c_ast.ArrayDecl) and isinstance(tn, c_ast.ArrayDecl):
            if gen(to.dim) != gen(tn.dim):
                return None
        elif isinstance(to, c_ast.PtrDecl) and isinstance(tn, c_ast.PtrDecl):
            via_ptr = True
        else:
            break
        to, tn = vo.env.core(to.type), vn.env.core(tn.type)
    if not (isinstance(to, (c_ast.Struct, c_ast.Union)) and isinstance(tn, (c_ast.Struct, c_ast.Union))
            and type(to) is type(tn) and to.decls and tn.decls):
        return None
    fo = {d.name: vo.env.signature(d.type) for d in to.decls if d.name}
    fn = {d.name: vn.env.signature(d.type) for d in tn.decls if d.name}
    if len(fo) != len([d for d in to.decls]) or len(fn) != len([d for d in tn.decls]):
        return None  # 익명 멤버가 있으면 보수적으로
    changed = {k for k in fo.keys() | fn.keys() if fo.get(k) != fn.get(k)}
    return changed, via_ptr


def _touches(n, key: str, fields: set[str]) -> bool:
    for p in n.defs | n.uses:
        if p[0] == key and any(str(x).lstrip("|") in fields for x in p[1:]):
            return True
    return False


def _type_changes(cs: ChangeSet, old: Program, new: Program):
    """자료형이 바뀐 변수: 그 변수를 쓰거나 읽는 문장이 영향(절단·부호·오버플로 동작 변화).
    구조체 정의만 바뀐 경우(필드 추가 등)는 바뀐 필드를 직접 쓰거나 읽는 문장만 시작점으로 잡는다.
    기존 필드 접근, 구조체 통째 복사·memset 은 바뀐 필드 값을 새로 만들거나 읽지 않으므로 동작이 같다."""
    to, tn = _type_sigs(old), _type_sigs(new)
    benign: dict[str, list[str]] = {}
    for key in sorted(set(to) & set(tn)):
        if to[key] == tn[key]:
            continue
        vo, vn = old.vars[key], new.vars[key]
        det = type_diff(vo, vn)
        delta = struct_delta(vo, vn)
        seeds_by = {"old": [], "new": []}
        for ver, prog in (("old", old), ("new", new)):
            for n in prog.nodes:
                if n.dead:
                    continue
                if delta is None:
                    hit = any(p[0] == key for p in n.defs | n.uses)
                else:
                    hit = not delta[1] and _touches(n, key, delta[0])
                if hit:
                    seeds_by[ver].append(n.id)
        if delta is not None and not seeds_by["old"] and not seeds_by["new"]:
            benign.setdefault(det, []).append(vn.name)
            continue
        first = seeds_by["new"][0] if seeds_by["new"] else _first_node(new, key)
        cs.add(Change("new", "type_changed", first, vn.func or "", vn.file, vn.line,
                      f"{vn.name} 자료형", detail=det))
        cs.seeds_old.update(seeds_by["old"])
        cs.seeds_new.update(seeds_by["new"])
    for det, names in sorted(benign.items()):
        uniq = sorted(set(names))
        shown = ", ".join(uniq[:8]) + (f" 외 {len(uniq) - 8}개" if len(uniq) > 8 else "")
        cs.notes.append(f"{det} — 이 자료형을 쓰는 변수 {len(names)}개({shown})는 바뀐 필드를 직접 쓰지 않아 "
                        "동작 변화 없음으로 보고 시작점에서 뺐습니다")


def _first_node(prog: Program, key: str) -> int:
    for n in prog.nodes:
        if any(p[0] == key for p in n.defs | n.uses):
            return n.id
    return 0


# ---------------------------------------------------------------------------
# 변경 설명: 값 변화, 조건 차이 구간
# ---------------------------------------------------------------------------


def _expr_of(n: Node):
    e = n.ast
    if n.kind == "stmt":
        if isinstance(e, c_ast.Decl):
            return e.init
        if isinstance(e, c_ast.Assignment) and e.op == "=":
            return e.rvalue
        return None
    if n.kind in ("cond", "return", "actual_in"):
        return e
    return None


def describe_change(old: Program, o: Node, new: Program, n: Node) -> tuple[str, dict | None]:
    eo, en = _expr_of(o), _expr_of(n)
    if eo is None or en is None:
        return "", None
    return compare_exprs(eo, old, en, new)


def compare_exprs(eo, old: Program, en, new: Program) -> tuple[str, dict | None]:
    """두 식(변경 전/후 또는 현재/가정)의 차이 설명과, 단일 변수 비교식이면 판정이 달라지는 구간."""
    co, cn = const_eval(eo), const_eval(en)
    if co is not None and cn is not None:
        return (f"값 {co} → {cn}" if co != cn else ""), None
    po, pn = predicate(eo, old), predicate(en, new)
    if po and pn and po[0] == pn[0]:
        var, so, dom = po
        _, sn, dom2 = pn
        dom = _intersect([dom], [dom2]) or [dom]
        so, sn = _intersect(so, dom), _intersect(sn, dom)
        diff = _union(_intersect(so, _complement(sn, dom[0])), _intersect(sn, _complement(so, dom[0])))
        if not diff:
            return f"판정이 같은 식으로 정리됨 ({var})", None
        tests = boundary_values(diff, dom[0])
        text = ", ".join(fmt_interval(var, iv) for iv in diff)
        info = {
            "var": var,
            "old": fmt_set(var, so, dom[0]), "new": fmt_set(var, sn, dom[0]),
            "diff": [[_j(lo), _j(hi)] for lo, hi in diff],
            "domain": [_j(dom[0][0]), _j(dom[0][1])],
            "tests": tests,
        }
        return f"판정 차이 구간: {text}", info
    return "", None


def expr_of(n: Node):
    """노드에서 값·판정을 결정하는 식 (조건식, 대입의 오른쪽, 초기값, return 식, 인자)."""
    return _expr_of(n)


def _j(x):
    return None if x in (INF, -INF) else int(x)


def predicate(e, prog: Program):
    """e 가 단일 변수 비교식이면 (변수 표기, 참이 되는 구간 집합, 변수 범위)를 돌려준다."""
    r = _pred(e)
    if r is None:
        return None
    var_text, var_expr, s = r
    dom = (-INF, INF)
    try:
        for l in prog.lvals(var_expr):
            t, env = prog.path_type(l)
            if env is not None:
                rng = env.int_range(t)
                if rng:
                    dom = rng
                break
    except Exception:
        pass
    s = _intersect(s, [dom])
    return var_text, s, dom


def _strip(e):
    while isinstance(e, c_ast.Cast):
        e = e.expr
    return e


_FLIP = {"<": ">", "<=": ">=", ">": "<", ">=": "<=", "==": "==", "!=": "!="}


def _pred(e):
    e = _strip(e)
    if isinstance(e, c_ast.BinaryOp) and e.op in _FLIP:
        lc, rc = const_eval(e.left), const_eval(e.right)
        if rc is not None and lc is None:
            var, op, c = _strip(e.left), e.op, rc
        elif lc is not None and rc is None:
            var, op, c = _strip(e.right), _FLIP[e.op], lc
        else:
            return None
        if not isinstance(var, (c_ast.ID, c_ast.StructRef, c_ast.ArrayRef, c_ast.UnaryOp, c_ast.FuncCall)):
            return None
        s = {
            "<": [(-INF, c - 1)], "<=": [(-INF, c)], ">": [(c + 1, INF)], ">=": [(c, INF)],
            "==": [(c, c)], "!=": [(-INF, c - 1), (c + 1, INF)],
        }[op]
        return norm(var), var, s
    if isinstance(e, c_ast.BinaryOp) and e.op in ("&&", "||"):
        a, b = _pred(e.left), _pred(e.right)
        if a and b and a[0] == b[0]:
            return a[0], a[1], (_intersect(a[2], b[2]) if e.op == "&&" else _union(a[2], b[2]))
        return None
    if isinstance(e, c_ast.UnaryOp) and e.op == "!":
        a = _pred(e.expr)
        return (a[0], a[1], _complement(a[2], (-INF, INF))) if a else None
    if isinstance(e, (c_ast.ID, c_ast.StructRef, c_ast.ArrayRef)):
        return norm(e), e, [(-INF, -1), (1, INF)]
    return None


def _union(a, b):
    xs = sorted(a + b)
    out = []
    for lo, hi in xs:
        if out and lo <= out[-1][1] + 1:
            out[-1] = (out[-1][0], max(out[-1][1], hi))
        else:
            out.append((lo, hi))
    return out


def _intersect(a, b):
    out = []
    for lo1, hi1 in a:
        for lo2, hi2 in b:
            lo, hi = max(lo1, lo2), min(hi1, hi2)
            if lo <= hi:
                out.append((lo, hi))
    return _union(out, [])


def _complement(a, dom):
    out = []
    cur = dom[0]
    for lo, hi in sorted(a):
        if lo > cur:
            out.append((cur, min(lo - 1, dom[1])))
        if hi >= dom[1]:
            return out
        cur = max(cur, hi + 1)
    if cur <= dom[1]:
        out.append((cur, dom[1]))
    return out


def fmt_interval(var: str, iv) -> str:
    lo, hi = iv
    if lo == hi:
        return f"{var} = {int(lo)}"
    if lo == -INF and hi == INF:
        return f"모든 {var}"
    if lo == -INF:
        return f"{var} ≤ {int(hi)}"
    if hi == INF:
        return f"{var} ≥ {int(lo)}"
    return f"{int(lo)} ≤ {var} ≤ {int(hi)}"


def fmt_set(var: str, s, dom=None) -> str:
    """자료형 범위 끝까지 이어지는 구간은 한쪽 비교로 표기한다 (예: 100 ≤ t ≤ 65535 → t ≥ 100)."""
    if not s:
        return "항상 거짓"
    lo_d, hi_d = dom if dom else (-INF, INF)
    shown = [((-INF if lo <= lo_d else lo), (INF if hi >= hi_d else hi)) for lo, hi in s]
    return " 또는 ".join(fmt_interval(var, iv) for iv in shown)


def boundary_values(diff, dom) -> list[int]:
    vals = set()
    for lo, hi in diff:
        for v in (lo - 1, lo, hi, hi + 1):
            if v not in (INF, -INF) and abs(v) != INF and dom[0] <= v <= dom[1]:
                vals.add(int(v))
        if lo != -INF and hi != INF and 2 < hi - lo <= 10000:
            vals.add(int((lo + hi) // 2))
    return sorted(vals)
