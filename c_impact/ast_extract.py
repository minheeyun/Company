"""1단계: libclang AST → 파일 모델(dict, JSON 직렬화 가능).

추출 내용
  - 함수 정의/선언, 매개변수, 지역변수(동명 지역변수는 #n 으로 구분), static 지역변수
  - 문장 트리(if/switch/case/loop/return/goto/label/expr/decl) + 문장별 정의(def)·사용(use)·호출
  - 표현식 트리(연산자, 상수 값, 변수 키, 구조체 필드, 역참조, 배열, 형변환, 호출)
  - 전역변수(초기값, 자료형, volatile/const, 배열 길이), 매크로/typedef/구조체/열거형
  - 함수 포인터 바인딩, 주소가 취해진 함수, 포인터 대입(points-to 근거)
  - 매크로 사용 위치, 전처리기로 비활성화된 구간, 컴파일 진단

심볼 키 규칙
  함수 ID : 외부 연결이면 이름, static 이면 "상대경로::이름"
  전역 ID : 외부 연결이면 이름, static 이면 "상대경로::이름", static 지역이면 "함수ID::이름"
  변수 키 : "G:<전역ID>", "P:<매개변수>", "L:<지역변수>#<n>", 필드 접근은 ".필드",
            포인터 역참조는 "*<키>", 필드 기반 별칭 키는 "F:<구조체>.<필드>"
"""

from __future__ import annotations

import fnmatch
import hashlib
import os
from pathlib import Path

from . import clang_frontend as cf

ci = cf.ci


def sha1(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()[:16]


class ProjectPaths:
    """프로젝트 내부/외부 파일 판정과 상대경로 변환."""

    def __init__(self, root: str | Path, external_globs=(), exclude_globs=()):
        self.root = os.path.realpath(str(root))
        self.external_globs = list(external_globs)
        self.exclude_globs = list(exclude_globs)

    def rel(self, path: str) -> str:
        ap = os.path.realpath(path)
        if ap == self.root or ap.startswith(self.root + os.sep):
            return os.path.relpath(ap, self.root).replace(os.sep, "/")
        return "@" + ap

    def is_project(self, path: str | None) -> bool:
        if not path:
            return False
        rel = self.rel(path)
        if rel.startswith("@"):
            return False
        return not any(fnmatch.fnmatch(rel, g) or fnmatch.fnmatch("/" + rel, g) for g in self.external_globs)


# --------------------------------------------------------------------------
# 자료형 정보
# --------------------------------------------------------------------------

_INT_KINDS = None


def _int_kinds():
    global _INT_KINDS
    if _INT_KINDS is None:
        T = ci.TypeKind
        _INT_KINDS = {
            T.BOOL: False, T.CHAR_U: False, T.UCHAR: False, T.USHORT: False, T.UINT: False, T.ULONG: False,
            T.ULONGLONG: False, T.CHAR_S: True, T.SCHAR: True, T.SHORT: True, T.INT: True, T.LONG: True,
            T.LONGLONG: True,
        }
    return _INT_KINDS


def type_info(t) -> dict:
    T = ci.TypeKind
    canon = t.get_canonical()
    info = {"spelling": t.spelling, "canon": canon.spelling, "kind": "other",
            "volatile": canon.is_volatile_qualified(), "const": canon.is_const_qualified()}
    k = canon.kind
    if k in _int_kinds():
        info["kind"] = "int"
        info["signed"] = _int_kinds()[k]
        sz = canon.get_size()
        info["bits"] = sz * 8 if sz and sz > 0 else None
        if k == T.BOOL:
            info["bool"] = True
    elif k == T.ENUM:
        info["kind"] = "enum"
        info["enum"] = canon.get_declaration().spelling
        sz = canon.get_size()
        info["bits"] = sz * 8 if sz and sz > 0 else None
        info["signed"] = True
    elif k == T.POINTER:
        pt = canon.get_pointee()
        info["kind"] = "fptr" if pt.kind in (T.FUNCTIONPROTO, T.FUNCTIONNOPROTO) else "ptr"
        info["pointee"] = pt.spelling
        info["pointee_canon"] = pt.get_canonical().spelling
        info["volatile"] = info["volatile"] or pt.is_volatile_qualified()
    elif k in (T.CONSTANTARRAY, T.INCOMPLETEARRAY, T.VARIABLEARRAY):
        info["kind"] = "array"
        info["elem"] = canon.get_array_element_type().spelling
        n = canon.get_array_size()
        info["len"] = n if n is not None and n >= 0 else None
        et = canon.get_array_element_type()
        if et.kind == T.POINTER and et.get_pointee().kind in (T.FUNCTIONPROTO, T.FUNCTIONNOPROTO):
            info["elem_fptr"] = True
    elif k == T.RECORD:
        info["kind"] = "record"
        info["record"] = canon.get_declaration().spelling or canon.spelling
    elif k in (T.FLOAT, T.DOUBLE, T.LONGDOUBLE):
        info["kind"] = "float"
    if info["kind"] != "int":
        info.setdefault("bits", (canon.get_size() * 8) if canon.get_size() and canon.get_size() > 0 else None)
    return info


# --------------------------------------------------------------------------
# 파일(번역 단위) 추출기
# --------------------------------------------------------------------------


class _FuncCtx:
    def __init__(self, fid):
        self.fid = fid
        self.locals = {}  # key -> info
        self.usr2key = {}
        self.name_count = {}
        self.params = []
        self.calls = []
        self.stmts = []
        self.addr_taken = set()
        self.fp_bindings = []
        self.next_id = 0


class _Acc:
    """한 문장(또는 조건식) 단위의 접근 기록."""

    def __init__(self):
        self.defs = []
        self.uses = []
        self.fdefs = []  # 필드 기반 키 (F:Rec.field)
        self.fuses = []
        self.calls = []
        self.ptr_assign = []  # (포인터 키, 대상 키 | "fn:<fid>" | "copy:<키>")
        self.rmw = []  # read-modify-write 대상 (++ , += ...)

    def add(self, lst, key):
        if key and key not in lst:
            lst.append(key)


class FileExtractor:
    def __init__(self, paths: ProjectPaths):
        self.paths = paths
        self.tok_cache = {}

    # ----------------------------------------------------------------- 공통
    def tokens(self, cur):
        """커서 범위의 토큰(주석 제외)."""
        f = cur.extent.start.file
        if f is None:
            return []
        name = f.name
        ti = self.tok_cache.get(name)
        if ti is None:
            try:
                skip = {ln for f, a, b in self._skipped if f == name for ln in range(a, b + 1)}
                ti = cf.TokenIndex(self.tu, name, skip)
            except Exception:
                ti = None
            self.tok_cache[name] = ti
        if ti is None:
            return [(0, 0, t.spelling, t.location.line) for t in cur.get_tokens()]
        return ti.slice(cur.extent.start.offset, cur.extent.end.offset)

    def text(self, cur) -> str:
        return " ".join(t[2] for t in self.tokens(cur))

    def fid_of(self, decl) -> str:
        name = decl.spelling
        if decl.storage_class == ci.StorageClass.STATIC:
            f = decl.location.file
            return f"{self.paths.rel(f.name) if f else '?'}::{name}"
        return name

    def gid_of(self, decl) -> str:
        name = decl.spelling
        sp = decl.semantic_parent
        if sp is not None and sp.kind == ci.CursorKind.FUNCTION_DECL:
            return f"{self.fid_of(sp)}::{name}"
        if decl.storage_class == ci.StorageClass.STATIC:
            f = decl.location.file
            return f"{self.paths.rel(f.name) if f else '?'}::{name}"
        return name

    # ------------------------------------------------------------- 진입점
    def extract(self, tu, main_path: str, args: list[str], mode: str) -> dict:
        self.tu = tu
        self.tok_cache = {}
        self._skipped = cf.skipped_ranges(tu)
        rel_main = self.paths.rel(main_path)
        fm = {
            "path": rel_main, "args": args, "mode": mode, "parse_ok": True, "diagnostics": [],
            "functions": {}, "globals": {}, "extern_vars": {}, "func_decls": {}, "macros": {},
            "typedefs": {}, "records": {}, "enums": {}, "includes": [], "skipped": [],
            "global_fp_bindings": [], "global_addr_taken": [], "global_macro_uses": [],
        }
        for d in tu.diagnostics:
            if d.severity >= ci.Diagnostic.Error:
                loc = d.location
                fname = loc.file.name if loc.file else ""
                fm["diagnostics"].append({"severity": "fatal" if d.severity >= ci.Diagnostic.Fatal else "error",
                                          "file": self.paths.rel(fname) if fname else "", "line": loc.line,
                                          "message": d.spelling})
                if not fname or self.paths.is_project(fname):
                    fm["parse_ok"] = False
        for inc in tu.get_includes():
            p = inc.include.name
            if self.paths.is_project(p):
                fm["includes"].append(self.paths.rel(p))
        fm["includes"] = sorted(set(fm["includes"]))
        for f, a, b in cf.skipped_ranges(tu):
            if f and self.paths.is_project(f):
                fm["skipped"].append([self.paths.rel(f), a, b])

        macro_uses = []
        for c in tu.cursor.get_children():
            loc_file = c.location.file
            if loc_file is None or not self.paths.is_project(loc_file.name):
                if c.kind == ci.CursorKind.FUNCTION_DECL:
                    self._record_func_decl(fm, c, external=True)
                continue
            k = c.kind
            if k == ci.CursorKind.MACRO_DEFINITION:
                self._macro(fm, c)
            elif k == ci.CursorKind.MACRO_INSTANTIATION:
                if loc_file.name == main_path or os.path.realpath(loc_file.name) == os.path.realpath(main_path):
                    ref = c.referenced
                    dkey = None
                    if ref is not None and ref.location.file is not None:
                        dkey = f"{self.paths.rel(ref.location.file.name)}::{c.spelling}"
                    macro_uses.append((c.spelling, c.extent.start.offset, c.location.line, dkey))
            elif k == ci.CursorKind.FUNCTION_DECL:
                if c.is_definition():
                    fn = self._function(c)
                    fm["functions"][fn["id"]] = fn
                    fm["globals"].update(fn.pop("_static_locals"))
                self._record_func_decl(fm, c, external=False)
            elif k == ci.CursorKind.VAR_DECL:
                self._global(fm, c)
            elif k == ci.CursorKind.TYPEDEF_DECL:
                self._typedef(fm, c)
            elif k in (ci.CursorKind.STRUCT_DECL, ci.CursorKind.UNION_DECL):
                self._record(fm, c)
            elif k == ci.CursorKind.ENUM_DECL:
                self._enum(fm, c)
        self._attach_macro_uses(fm, macro_uses, main_path)
        return fm

    # ------------------------------------------------------- 최상위 선언
    def _record_func_decl(self, fm, c, external: bool):
        fid = self.fid_of(c)
        f = c.location.file
        ent = fm["func_decls"].setdefault(fid, {
            "name": c.spelling, "file": self.paths.rel(f.name) if f else "", "line": c.location.line,
            "type": c.type.spelling, "external": external, "system": c.location.is_in_system_header,
            "params": [type_info(a.type) for a in c.get_arguments()],
        })
        if c.is_definition():
            ent["defined"] = True

    def _macro(self, fm, c):
        toks = [t.spelling for t in c.get_tokens()]
        f = c.location.file
        rel = self.paths.rel(f.name)
        name = c.spelling
        body = toks[1:]
        func_like = False
        params = []
        if len(toks) > 1 and toks[1] == "(":
            # 함수형 매크로 판정: 이름과 '(' 사이 공백 여부는 토큰으로 알 수 없으므로 오프셋 비교
            tk = list(c.get_tokens())
            if tk[1].extent.start.offset == tk[0].extent.end.offset:
                func_like = True
                j = 2
                while j < len(toks) and toks[j] != ")":
                    if toks[j] != ",":
                        params.append(toks[j])
                    j += 1
                body = toks[j + 1:]
        fm["macros"][f"{rel}::{name}"] = {"name": name, "file": rel, "line": c.location.line,
                                          "body": " ".join(body), "params": params, "function_like": func_like,
                                          "idents": sorted({t for t in body if t.isidentifier()})}

    def _typedef(self, fm, c):
        f = c.location.file
        ut = c.underlying_typedef_type
        fm["typedefs"][c.spelling] = {"name": c.spelling, "file": self.paths.rel(f.name), "line": c.location.line,
                                      "underlying": ut.spelling, "canon": ut.get_canonical().spelling,
                                      "bits": (ut.get_canonical().get_size() * 8) if ut.get_canonical().get_size() > 0 else None}
        for ch in c.get_children():
            if ch.kind in (ci.CursorKind.STRUCT_DECL, ci.CursorKind.UNION_DECL) and ch.is_definition():
                self._record(fm, ch, alias=c.spelling)
            elif ch.kind == ci.CursorKind.ENUM_DECL and ch.is_definition():
                self._enum(fm, ch, alias=c.spelling)

    def _record(self, fm, c, alias=None):
        if not c.is_definition():
            return
        name = c.spelling or alias or f"anon@{c.location.line}"
        if alias and (not c.spelling or "unnamed" in c.spelling or "anonymous" in c.spelling):
            name = alias
        fields = [[ch.spelling, ch.type.spelling, ch.type.get_canonical().spelling]
                  for ch in c.get_children() if ch.kind == ci.CursorKind.FIELD_DECL]
        f = c.location.file
        fm["records"][name] = {"name": name, "file": self.paths.rel(f.name), "line": c.location.line,
                               "fields": fields, "canon": c.type.get_canonical().spelling}

    def _enum(self, fm, c, alias=None):
        if not c.is_definition():
            return
        name = c.spelling or alias or f"anon@{c.location.line}"
        if alias and (not c.spelling or "unnamed" in c.spelling or "anonymous" in c.spelling):
            name = alias
        consts = {ch.spelling: ch.enum_value for ch in c.get_children() if ch.kind == ci.CursorKind.ENUM_CONSTANT_DECL}
        f = c.location.file
        fm["enums"][name] = {"name": name, "file": self.paths.rel(f.name), "line": c.location.line, "constants": consts}

    def _global(self, fm, c):
        gid = self.gid_of(c)
        f = c.location.file
        rel = self.paths.rel(f.name)
        ti = type_info(c.type)
        ext = c.storage_class == ci.StorageClass.EXTERN and not c.is_definition()
        init = None
        init_expr = None
        acc = _Acc()
        ctx = _FuncCtx("<global>")
        self.ctx = ctx
        for ch in c.get_children():
            if ch.kind.is_expression():
                init = self.text(ch)
                init_expr = self._expr(ch, ctx, acc)
                self._ptr_bind("G:" + gid, ch, acc, init_expr)
        ent = {"id": gid, "key": "G:" + gid, "name": c.spelling, "file": rel, "line": c.location.line,
               "type": ti, "static": c.storage_class == ci.StorageClass.STATIC, "init": init,
               "init_value": init_expr.get("v") if init_expr and init_expr.get("k") == "const" else None,
               "fp_init": [b[1][3:] for b in acc.ptr_assign if b[1].startswith("fn:")],
               "addr_init": [b[1] for b in acc.ptr_assign if not b[1].startswith(("fn:", "copy:"))]}
        if ext:
            fm["extern_vars"][gid] = ent
            return
        prev = fm["globals"].get(gid)
        if prev is None or init is not None:
            fm["globals"][gid] = ent
        for fid in ctx.addr_taken:
            if fid not in fm["global_addr_taken"]:
                fm["global_addr_taken"].append(fid)
        for tgt in ent["fp_init"]:
            fm["global_fp_bindings"].append({"target": "G:" + gid, "func": tgt, "line": c.location.line})

    # ---------------------------------------------------------------- 함수
    def _function(self, c) -> dict:
        fid = self.fid_of(c)
        ctx = _FuncCtx(fid)
        self.ctx = ctx
        self.static_locals = {}
        params = []
        for i, a in enumerate(c.get_arguments()):
            key = "P:" + (a.spelling or f"arg{i}")
            ctx.usr2key[a.get_usr() or a.hash] = key
            params.append({"name": a.spelling, "index": i, "key": key, "type": type_info(a.type),
                           "line": a.location.line})
        ctx.params = params
        body_cur = None
        for ch in c.get_children():
            if ch.kind == ci.CursorKind.COMPOUND_STMT:
                body_cur = ch
        body = self._stmt_list(body_cur) if body_cur is not None else []
        f = c.location.file
        toks = self.tokens(c)
        norm = " ".join(t[2] for t in toks)
        raw = ""
        try:
            with open(f.name, "rb") as fh:
                fh.seek(c.extent.start.offset)
                raw = fh.read(c.extent.end.offset - c.extent.start.offset).decode("utf-8", "replace")
        except OSError:
            pass
        ret = type_info(c.result_type)
        sig = f"{c.result_type.spelling} {c.spelling}(" + ", ".join(p["type"]["spelling"] for p in params) + ")"
        return {
            "id": fid, "name": c.spelling, "file": self.paths.rel(f.name), "line": c.extent.start.line,
            "end_line": c.extent.end.line, "static": c.storage_class == ci.StorageClass.STATIC,
            "inline": "inline" in [t[2] for t in toks[:6]], "ret": ret, "params": params, "sig": sig,
            "norm_hash": sha1(norm), "raw_hash": sha1(raw), "norm_text": norm,
            "locals": ctx.locals, "body": body, "calls": ctx.calls, "n_stmts": ctx.next_id,
            "addr_taken": sorted(ctx.addr_taken), "fp_bindings": ctx.fp_bindings, "macro_uses": [],
            "_static_locals": self.static_locals,
            "_offsets": [c.extent.start.offset, c.extent.end.offset],
        }

    # ---------------------------------------------------------------- 문장
    def _new_stmt(self, kind, cur, **kw):
        ctx = self.ctx
        st = {"id": ctx.next_id, "kind": kind, "line": cur.extent.start.line, "end": cur.extent.end.line,
              "off": [cur.extent.start.offset, cur.extent.end.offset]}
        ctx.next_id += 1
        st.update(kw)
        return st

    def _fill_acc(self, st, acc: _Acc):
        st["defs"] = acc.defs
        st["uses"] = acc.uses
        st["fdefs"] = acc.fdefs
        st["fuses"] = acc.fuses
        st["calls"] = acc.calls
        if acc.ptr_assign:
            st["ptr_assign"] = acc.ptr_assign
        if acc.rmw:
            st["rmw"] = acc.rmw
        for cid in acc.calls:
            self.ctx.calls[cid]["stmt"] = st["id"]

    def _stmt_list(self, cur) -> list:
        if cur is None:
            return []
        if cur.kind == ci.CursorKind.COMPOUND_STMT:
            out = []
            for ch in cur.get_children():
                out.extend(self._stmt(ch))
            return out
        return self._stmt(cur)

    def _cond(self, cur, kind, prefix, owner=None):
        acc = _Acc()
        e = self._expr(cur, self.ctx, acc)
        st = self._new_stmt(kind, cur, text=f"{prefix} ( {self.text(cur)} )", cond=e)
        st["hdr_end"] = cur.extent.end.line
        self._fill_acc(st, acc)
        return st

    def _stmt(self, cur) -> list:
        K = ci.CursorKind
        k = cur.kind
        ch = list(cur.get_children())
        if k == K.COMPOUND_STMT:
            return self._stmt_list(cur)
        if k == K.DECL_STMT:
            out = []
            for d in ch:
                if d.kind == K.VAR_DECL:
                    out.append(self._decl(d))
            return out
        if k == K.IF_STMT:
            cond = self._cond(ch[0], "if", "if")
            cond["then"] = self._stmt_list(ch[1]) if len(ch) > 1 else []
            cond["else"] = self._stmt_list(ch[2]) if len(ch) > 2 else None
            cond["else_if"] = len(ch) > 2 and ch[2].kind == K.IF_STMT
            cond["end"] = cur.extent.end.line
            return [cond]
        if k == K.SWITCH_STMT:
            st = self._cond(ch[0], "switch", "switch")
            st["body"] = self._stmt_list(ch[-1])
            st["end"] = cur.extent.end.line
            return [st]
        if k == K.CASE_STMT:
            acc = _Acc()
            ve = self._expr(ch[0], self.ctx, acc)
            st = self._new_stmt("case", ch[0], text=f"case {self.text(ch[0])} :", value=ve.get("v"),
                                value_text=self.text(ch[0]), cond=ve)
            st["line"] = cur.extent.start.line
            self._fill_acc(st, _Acc())
            return [st] + (self._stmt(ch[-1]) if len(ch) > 1 else [])
        if k == K.DEFAULT_STMT:
            st = self._new_stmt("default", cur, text="default :")
            st["end"] = st["line"]
            self._fill_acc(st, _Acc())
            return [st] + (self._stmt(ch[-1]) if ch else [])
        if k == K.WHILE_STMT:
            st = self._cond(ch[0], "while", "while")
            st["body"] = self._stmt_list(ch[-1])
            st["end"] = cur.extent.end.line
            return [st]
        if k == K.DO_STMT:
            st = self._cond(ch[-1], "do", "do-while")
            st["body"] = self._stmt_list(ch[0])
            st["line"] = cur.extent.start.line
            st["cond_line"] = ch[-1].extent.start.line
            st["end"] = cur.extent.end.line
            return [st]
        if k == K.FOR_STMT:
            return [self._for(cur, ch)]
        if k == K.RETURN_STMT:
            acc = _Acc()
            e = self._expr(ch[0], self.ctx, acc) if ch else None
            st = self._new_stmt("return", cur, text=self.text(cur), expr=e)
            self._fill_acc(st, acc)
            return [st]
        if k in (K.BREAK_STMT, K.CONTINUE_STMT, K.NULL_STMT):
            st = self._new_stmt({K.BREAK_STMT: "break", K.CONTINUE_STMT: "continue", K.NULL_STMT: "null"}[k], cur,
                                text=self.text(cur))
            self._fill_acc(st, _Acc())
            return [st]
        if k == K.GOTO_STMT:
            lbl = ch[0].spelling if ch else ""
            st = self._new_stmt("goto", cur, text=self.text(cur), label=lbl)
            self._fill_acc(st, _Acc())
            return [st]
        if k == K.LABEL_STMT:
            st = self._new_stmt("label", cur, text=f"{cur.spelling} :", label=cur.spelling)
            st["end"] = st["line"]
            self._fill_acc(st, _Acc())
            return [st] + (self._stmt(ch[-1]) if ch else [])
        if k.is_expression() or k == K.UNEXPOSED_EXPR:
            acc = _Acc()
            e = self._expr(cur, self.ctx, acc)
            st = self._new_stmt("expr", cur, text=self.text(cur), expr=e)
            self._fill_acc(st, acc)
            return [st]
        # 지원하지 않는 문장(asm 등): 내용 불명으로 표시
        st = self._new_stmt("unknown", cur, text=self.text(cur), note=k.name)
        self._fill_acc(st, _Acc())
        return [st]

    def _for(self, cur, ch):
        toks = self.tokens(cur)
        depth, lp, rp, semis = 0, None, None, []
        for i, t in enumerate(toks):
            if t[2] == "(":
                depth += 1
                if depth == 1 and lp is None:
                    lp = t[0]
            elif t[2] == ")":
                depth -= 1
                if depth == 0 and rp is None:
                    rp = t[0]
                    break
            elif t[2] == ";" and depth == 1:
                semis.append(t[0])
        init_c = cond_c = inc_c = body_c = None
        if lp is not None and rp is not None and len(semis) >= 2:
            for c in ch:
                o = c.extent.start.offset
                if o < semis[0]:
                    init_c = c
                elif o < semis[1]:
                    cond_c = c
                elif o < rp:
                    inc_c = c
                else:
                    body_c = c
        else:  # 토큰 분석 실패 시 마지막 자식을 본문으로 간주
            body_c = ch[-1] if ch else None
        init = self._stmt(init_c) if init_c is not None else []
        if cond_c is not None:
            st = self._cond(cond_c, "for", "for")
        else:
            st = self._new_stmt("for", cur, text="for ( ; ; )", cond=None)
            self._fill_acc(st, _Acc())
        st["line"] = cur.extent.start.line
        if lp is not None and rp is not None:
            hdr = [t[2] for t in toks if lp < t[0] < rp]
            st["text"] = "for ( " + " ".join(hdr) + " )"
            st["hdr_end"] = next((t[3] for t in toks if t[0] == rp), st["line"])
        st["init"] = init
        st["inc"] = self._stmt(inc_c) if inc_c is not None else []
        st["body"] = self._stmt_list(body_c) if body_c is not None else []
        st["end"] = cur.extent.end.line
        return st

    def _decl(self, d):
        ctx = self.ctx
        ti = type_info(d.type)
        if d.storage_class == ci.StorageClass.STATIC:
            gid = self.gid_of(d)
            init = None
            for c in d.get_children():
                if c.kind.is_expression():
                    init = self.text(c)
            self.static_locals[gid] = {"id": gid, "key": "G:" + gid, "name": d.spelling,
                                       "file": self.paths.rel(d.location.file.name), "line": d.location.line,
                                       "type": ti, "static": True, "static_local": True, "init": init,
                                       "init_value": None, "fp_init": [], "addr_init": []}
            ctx.usr2key[d.get_usr() or d.hash] = "G:" + gid
            st = self._new_stmt("decl", d, text=self.text(d), decl="G:" + gid, static_local=True)
            self._fill_acc(st, _Acc())
            return st
        n = ctx.name_count.get(d.spelling, 0)
        ctx.name_count[d.spelling] = n + 1
        key = f"L:{d.spelling}#{n}"
        ctx.usr2key[d.get_usr() or d.hash] = key
        ctx.locals[key] = {"name": d.spelling, "type": ti, "line": d.location.line}
        acc = _Acc()
        e = None
        for c in d.get_children():
            if c.kind.is_expression():
                e = self._expr(c, ctx, acc)
                acc.add(acc.defs, key)
                self._ptr_bind(key, c, acc, e)
        st = self._new_stmt("decl", d, text=self.text(d), decl=key, expr=e)
        self._fill_acc(st, acc)
        return st

    # -------------------------------------------------------------- 표현식
    def _strip(self, c):
        K = ci.CursorKind
        while c.kind in (K.UNEXPOSED_EXPR, K.PAREN_EXPR):
            ch = list(c.get_children())
            if len(ch) != 1:
                break
            c = ch[0]
        return c

    def _var_key(self, ref, ctx):
        """참조된 선언 커서 → 변수 키."""
        K = ci.CursorKind
        if ref is None:
            return None
        if ref.kind == K.PARM_DECL or (ref.kind == K.VAR_DECL and ref.semantic_parent is not None
                                       and ref.semantic_parent.kind == K.FUNCTION_DECL):
            key = ctx.usr2key.get(ref.get_usr() or ref.hash)
            if key:
                return key
            if ref.kind == K.VAR_DECL and ref.storage_class == ci.StorageClass.STATIC:
                return "G:" + self.gid_of(ref)
            return "L:" + ref.spelling + "#?"
        if ref.kind == K.VAR_DECL:
            return "G:" + self.gid_of(ref)
        return None

    def lkey(self, e) -> str | None:
        """표현식 트리 → lvalue 키."""
        if e is None:
            return None
        k = e.get("k")
        if k == "var":
            return e["key"]
        if k == "member":
            b = e["base"]
            if e.get("arrow"):
                bk = self.lkey(b)
                return f"*{bk}.{e['field']}" if bk else None
            bk = self.lkey(b)
            return f"{bk}.{e['field']}" if bk else None
        if k == "un" and e["op"] == "*":
            bk = self.lkey(e["e"])
            return "*" + bk if bk else None
        if k == "index":
            bk = self.lkey(e["base"])
            if bk and e["base"].get("t", {}).get("kind") == "ptr":
                return "*" + bk
            return bk
        if k == "cast":
            return self.lkey(e["e"])
        return None

    def _fkey(self, e):
        """구조체 필드 기반 키 (포인터 별칭 보수 처리용)."""
        if e.get("k") == "member" and e.get("rec"):
            return f"F:{e['rec']}.{e['field']}"
        return None

    def _ptr_bind(self, lhs_key, rhs_cur, acc, rhs_e):
        if not lhs_key or rhs_e is None:
            return
        r = rhs_e
        while r.get("k") == "cast":
            r = r["e"]
        if r.get("k") == "un" and r["op"] == "&":
            tk = self.lkey(r["e"])
            if tk:
                acc.ptr_assign.append((lhs_key, tk))
        elif r.get("k") == "fn":
            acc.ptr_assign.append((lhs_key, "fn:" + r["fid"]))
            self.ctx.fp_bindings.append({"target": lhs_key, "func": r["fid"], "line": r.get("line")})
        elif r.get("k") == "var" and r.get("t", {}).get("kind") == "array":
            acc.ptr_assign.append((lhs_key, r["key"]))
        elif r.get("k") == "var" and r.get("t", {}).get("kind") in ("ptr", "fptr"):
            acc.ptr_assign.append((lhs_key, "copy:" + r["key"]))
        elif r.get("k") == "init":
            for it in r["items"]:
                self._ptr_bind(lhs_key, None, acc, it)

    def _record_access(self, e, acc, mode):
        """lvalue 접근을 def/use 로 기록."""
        key = self.lkey(e)
        fk = self._fkey(e)
        if mode in ("def", "rmw"):
            acc.add(acc.defs, key)
            if fk:
                acc.add(acc.fdefs, fk)
        if mode in ("use", "rmw"):
            acc.add(acc.uses, key)
            if fk:
                acc.add(acc.fuses, fk)
        if mode == "rmw" and key:
            acc.rmw.append(key)
        # 하위 식(포인터 기반, 첨자)은 항상 사용
        self._uses_inside_lvalue(e, acc)

    def _uses_inside_lvalue(self, e, acc):
        k = e.get("k")
        if k == "member":
            b = e["base"]
            if e.get("arrow"):
                self._mark_uses(b, acc)
            else:
                self._uses_inside_lvalue(b, acc)
        elif k == "un" and e["op"] == "*":
            self._mark_uses(e["e"], acc)
        elif k == "index":
            if e["base"].get("t", {}).get("kind") == "ptr":
                self._mark_uses(e["base"], acc)
            else:
                self._uses_inside_lvalue(e["base"], acc)
            self._mark_uses(e["idx"], acc)
        elif k == "cast":
            self._uses_inside_lvalue(e["e"], acc)

    def _mark_uses(self, e, acc):
        """이미 만든 표현식 트리의 변수 사용을 기록(lvalue 하위식 용)."""
        k = e.get("k")
        if k in ("var", "member") or (k == "un" and e["op"] == "*") or k == "index":
            self._record_access(e, acc, "use")
        else:
            for sub in _children(e):
                self._mark_uses(sub, acc)

    def _expr(self, cur, ctx, acc: _Acc, mode="use") -> dict:
        """커서 → 표현식 트리. 접근은 acc 에 기록한다."""
        e = self._build(cur, ctx, acc)
        self._collect(e, acc, mode)
        return e

    def _collect(self, e, acc, mode="use"):
        k = e.get("k")
        if k in ("var", "member", "index") or (k == "un" and e.get("op") == "*"):
            self._record_access(e, acc, mode)
            return
        if k == "assign":
            op = e["op"]
            self._collect(e["l"], acc, "def" if op == "=" else "rmw")
            self._collect(e["r"], acc, "use")
            return
        if k == "un" and e["op"] in ("++", "--"):
            self._collect(e["e"], acc, "rmw")
            return
        if k == "un" and e["op"] == "&":
            # 주소 연산: 값 사용이 아니라 별칭 생성. 하위 첨자/포인터는 사용
            inner = e["e"]
            ik = self.lkey(inner)
            if ik:
                acc.add(acc.uses, "&" + ik)
            self._uses_inside_lvalue(inner, acc)
            return
        if k == "call":
            for a in e["args"]:
                self._collect(a, acc, "use")
            if e.get("indirect") and e.get("callee_e") is not None:
                self._collect(e["callee_e"], acc, "use")
            acc.calls.append(e["cid"])
            return
        for sub in _children(e):
            self._collect(sub, acc, "use")

    def _build(self, cur, ctx, acc) -> dict:
        K = ci.CursorKind
        c = self._strip(cur)
        k = c.kind
        line = c.extent.start.line
        if k in (K.INTEGER_LITERAL, K.CHARACTER_LITERAL):
            v = cf.evaluate(c)
            if v is None:
                try:
                    v = int(self.text(c).rstrip("uUlL"), 0)
                except ValueError:
                    v = None
            return {"k": "const", "v": v, "text": self.text(c), "t": _tsmall(c.type)}
        if k == K.FLOATING_LITERAL:
            return {"k": "fconst", "text": self.text(c)}
        if k == K.STRING_LITERAL:
            return {"k": "str", "text": self.text(c)}
        if k == K.DECL_REF_EXPR:
            ref = c.referenced
            if ref is not None and ref.kind == K.ENUM_CONSTANT_DECL:
                return {"k": "const", "v": ref.enum_value, "text": ref.spelling, "enum": True,
                        "t": _tsmall(c.type)}
            if ref is not None and ref.kind == K.FUNCTION_DECL:
                fid = self.fid_of(ref)
                ctx.addr_taken.add(fid)
                return {"k": "fn", "fid": fid, "name": ref.spelling, "line": line}
            key = self._var_key(ref, ctx)
            return {"k": "var", "key": key, "name": c.spelling, "t": _tsmall(c.type), "line": line}
        if k == K.MEMBER_REF_EXPR:
            ch = list(c.get_children())
            base = self._build(ch[0], ctx, acc) if ch else {"k": "other", "text": "?"}
            bt = self._strip(ch[0]).type if ch else None
            arrow = bool(bt is not None and bt.get_canonical().kind == ci.TypeKind.POINTER)
            rec_t = bt.get_canonical().get_pointee() if arrow else (bt.get_canonical() if bt else None)
            rec = None
            if rec_t is not None:
                decl = rec_t.get_declaration()
                rec = decl.spelling or None
                if rec and ("unnamed" in rec or "anonymous" in rec):
                    rec = rec_t.spelling
            return {"k": "member", "base": base, "field": c.spelling, "arrow": arrow, "rec": rec,
                    "t": _tsmall(c.type), "line": line}
        if k == K.ARRAY_SUBSCRIPT_EXPR:
            ch = list(c.get_children())
            base = self._build(ch[0], ctx, acc)
            idx = self._build(ch[1], ctx, acc)
            bt = self._strip(ch[0]).type.get_canonical()
            blen = bt.get_array_size() if bt.kind == ci.TypeKind.CONSTANTARRAY else None
            return {"k": "index", "base": base, "idx": idx, "len": blen, "t": _tsmall(c.type), "line": line}
        if k == K.UNARY_OPERATOR:
            ch = list(c.get_children())
            op, post = cf.unary_operator(c)
            if op is None:
                toks = self.tokens(c)
                sub_start = ch[0].extent.start.offset if ch else 0
                post = bool(toks) and toks[0][0] >= sub_start
                op = toks[-1][2] if post and toks else (toks[0][2] if toks else "?")
            sub = self._build(ch[0], ctx, acc) if ch else {"k": "other", "text": "?"}
            e = {"k": "un", "op": op, "post": post, "e": sub, "t": _tsmall(c.type), "line": line}
            return self._fold(c, e)
        if k in (K.BINARY_OPERATOR, K.COMPOUND_ASSIGNMENT_OPERATOR):
            ch = list(c.get_children())
            op = cf.binary_operator(c)
            if op is None:
                op = "?"
                lend = ch[0].extent.end.offset
                for t in self.tokens(c):
                    if t[0] >= lend:
                        op = t[2]
                        break
            lhs = self._build(ch[0], ctx, acc)
            rhs = self._build(ch[1], ctx, acc)
            if op in ("=", "+=", "-=", "*=", "/=", "%=", "<<=", ">>=", "&=", "|=", "^="):
                e = {"k": "assign", "op": op, "l": lhs, "r": rhs, "t": _tsmall(c.type), "line": line}
                if op == "=":
                    self._ptr_bind(self.lkey(lhs), ch[1], acc, rhs)
                return e
            e = {"k": "bin", "op": op, "l": lhs, "r": rhs, "t": _tsmall(c.type), "line": line}
            return self._fold(c, e)
        if k == K.CONDITIONAL_OPERATOR:
            ch = list(c.get_children())
            parts = [self._build(x, ctx, acc) for x in ch]
            while len(parts) < 3:
                parts.append({"k": "other", "text": "?"})
            return self._fold(c, {"k": "cond", "c": parts[0], "a": parts[1], "b": parts[2],
                                  "t": _tsmall(c.type), "line": line})
        if k in (K.CSTYLE_CAST_EXPR,):
            ch = [x for x in c.get_children() if x.kind.is_expression() or x.kind == K.UNEXPOSED_EXPR]
            sub = self._build(ch[-1], ctx, acc) if ch else {"k": "other", "text": "?"}
            return self._fold(c, {"k": "cast", "e": sub, "t": _tsmall(c.type), "explicit": True, "line": line})
        if k == K.UNEXPOSED_EXPR:  # 자식이 여러 개인 미노출 식
            ch = list(c.get_children())
            if len(ch) == 1:
                return self._build(ch[0], ctx, acc)
            return {"k": "other", "text": self.text(c), "items": [self._build(x, ctx, acc) for x in ch]}
        if k == K.CALL_EXPR:
            return self._call(c, ctx, acc)
        if k == K.INIT_LIST_EXPR:
            return {"k": "init", "items": [self._build(x, ctx, acc) for x in c.get_children()], "line": line}
        if k == K.CXX_UNARY_EXPR:  # sizeof
            return {"k": "const", "v": cf.evaluate(c), "text": self.text(c), "t": _tsmall(c.type)}
        v = cf.evaluate(c)
        if v is not None:
            return {"k": "const", "v": v, "text": self.text(c), "t": _tsmall(c.type)}
        return {"k": "other", "text": self.text(c), "items": [self._build(x, ctx, acc) for x in c.get_children()
                                                              if x.kind.is_expression()]}

    def _fold(self, c, e):
        """하위 식이 모두 상수면 상수로 접는다(매크로 이름은 text 로 보존)."""
        subs = _children(e)
        if subs and all(s.get("k") == "const" for s in subs):
            v = cf.evaluate(c)
            if v is not None:
                return {"k": "const", "v": v, "text": self.text(c), "t": e.get("t"), "folded": True}
        return e

    def _call(self, c, ctx, acc):
        K = ci.CursorKind
        ch = list(c.get_children())
        callee_c = self._strip(ch[0]) if ch else None
        ref = c.referenced
        direct = callee_c is not None and callee_c.kind == K.DECL_REF_EXPR and ref is not None \
            and ref.kind == K.FUNCTION_DECL
        cid = len(ctx.calls)
        call = {"id": cid, "line": c.extent.start.line, "text": self.text(c), "args": [], "arg_addr": [],
                "arg_ptr": [], "arg_keys": []}
        ctx.calls.append(call)
        if direct:
            call.update(callee=self.fid_of(ref), name=ref.spelling, indirect=False,
                        external=not self.paths.is_project(ref.location.file.name if ref.location.file else None),
                        ret=type_info(ref.result_type))
            # 정의가 다른 파일에 있으면 project 단계에서 다시 연결한다.
        else:
            callee_e = self._build(ch[0], ctx, acc) if ch else None
            call["callee_e"] = callee_e
            call.update(callee=None, name=self.text(ch[0]) if ch else "?", indirect=True,
                        fp_key=self.lkey(_strip_e(callee_e)) if callee_e else None,
                        fp_fkey=self._fkey(_strip_e(callee_e)) if callee_e else None,
                        fp_type=ch[0].type.get_canonical().spelling if ch else "",
                        external=False)
        args_e = []
        for a in ch[1:]:
            ae = self._build(a, ctx, acc)
            args_e.append(ae)
            sub = _Acc()
            self._collect(ae, sub, "use")
            call["arg_keys"].append([u for u in sub.uses if not u.startswith("&")])
            r = _strip_e(ae)
            addr = None
            ptr = None
            if r.get("k") == "un" and r["op"] == "&":
                addr = self.lkey(r["e"])
            elif r.get("k") == "var" and r.get("t", {}).get("kind") == "array":
                addr = r["key"]
            elif r.get("k") == "var" and r.get("t", {}).get("kind") == "ptr":
                ptr = r["key"]
            elif r.get("k") == "fn":
                ctx.fp_bindings.append({"target": f"arg:{call.get('callee') or '?'}:{len(args_e) - 1}",
                                        "func": r["fid"], "line": call["line"]})
            call["arg_addr"].append(addr)
            call["arg_ptr"].append(ptr)
        call["args"] = [_expr_text(x) for x in args_e]
        e = {"k": "call", "cid": cid, "callee": call["callee"], "name": call["name"], "args": args_e,
             "indirect": call["indirect"], "t": _tsmall(c.type), "line": call["line"]}
        if call["indirect"]:
            e["callee_e"] = call.pop("callee_e")
        return e

    # ------------------------------------------------------- 매크로 사용
    def _attach_macro_uses(self, fm, uses, main_path):
        funcs = [f for f in fm["functions"].values() if os.path.realpath(
            os.path.join(self.paths.root, f["file"])) == os.path.realpath(main_path)]
        for name, off, line, dkey in uses:
            owner = None
            for f in funcs:
                a, b = f["_offsets"]
                if a <= off < b:
                    owner = f
                    break
            if owner is None:
                fm["global_macro_uses"].append({"name": name, "line": line, "def": dkey})
                continue
            sid = _innermost_stmt(owner["body"], off)
            owner["macro_uses"].append({"name": name, "line": line, "stmt": sid, "def": dkey})
        for f in fm["functions"].values():
            f.pop("_offsets", None)


def _innermost_stmt(stmts, off):
    best = None
    for st in iter_stmts(stmts):
        a, b = st.get("off", (0, 0))
        if a <= off < b:
            if best is None or (b - a) <= (best[1] - best[0]):
                best = (a, b, st["id"])
    return best[2] if best else None


def _strip_e(e):
    while e and e.get("k") == "cast" and not e.get("explicit"):
        e = e["e"]
    return e


def _children(e):
    k = e.get("k")
    if k in ("bin", "assign"):
        return [e["l"], e["r"]]
    if k in ("un", "cast"):
        return [e["e"]]
    if k == "cond":
        return [e["c"], e["a"], e["b"]]
    if k == "member":
        return [e["base"]]
    if k == "index":
        return [e["base"], e["idx"]]
    if k == "call":
        return list(e["args"])
    if k in ("init", "other"):
        return list(e.get("items", []))
    return []


expr_children = _children


def _tsmall(t):
    try:
        ti = type_info(t)
    except Exception:
        return {}
    return {k: v for k, v in ti.items() if k in ("spelling", "kind", "bits", "signed", "len", "record", "volatile")}


_PREC = {"*": 10, "/": 10, "%": 10, "+": 9, "-": 9, "<<": 8, ">>": 8, "<": 7, ">": 7, "<=": 7, ">=": 7,
         "==": 6, "!=": 6, "&": 5, "^": 4, "|": 3, "&&": 2, "||": 1}


def _expr_text(e) -> str:
    """표현식 트리 → 사람이 읽을 수 있는 정규화 문자열(괄호는 트리 구조로 다시 붙인다)."""
    if e is None:
        return ""
    k = e.get("k")
    if k == "const":
        return str(e.get("text") or e.get("v"))
    if k in ("fconst", "str"):
        return e["text"]
    if k == "var":
        return e.get("name") or e.get("key") or "?"
    if k == "fn":
        return e["name"]
    if k == "member":
        return f"{_expr_text(e['base'])}{'->' if e.get('arrow') else '.'}{e['field']}"
    if k == "index":
        return f"{_expr_text(e['base'])}[{_expr_text(e['idx'])}]"
    if k == "un":
        inner = _expr_text(e["e"])
        if e["e"].get("k") in ("bin", "assign", "cond"):
            inner = f"({inner})"
        return f"{inner}{e['op']}" if e.get("post") else f"{e['op']}{inner}"
    if k in ("bin", "assign"):
        def wrap(sub, right=False):
            t = _expr_text(sub)
            if sub.get("k") in ("bin",) and k == "bin":
                p_sub, p_me = _PREC.get(sub["op"], 0), _PREC.get(e["op"], 0)
                if p_sub < p_me or (right and p_sub == p_me) or (
                        e["op"] in ("&&", "||") and sub["op"] in ("&&", "||") and sub["op"] != e["op"]):
                    return f"({t})"
            elif sub.get("k") in ("assign", "cond"):
                return f"({t})"
            return t
        return f"{wrap(e['l'])} {e['op']} {wrap(e['r'], True)}"
    if k == "cond":
        return f"{_expr_text(e['c'])} ? {_expr_text(e['a'])} : {_expr_text(e['b'])}"
    if k == "cast":
        if e.get("explicit"):
            return f"({e.get('t', {}).get('spelling', '?')}){_expr_text(e['e'])}"
        return _expr_text(e["e"])
    if k == "call":
        return f"{e['name']}(" + ", ".join(_expr_text(a) for a in e["args"]) + ")"
    if k == "init":
        return "{" + ", ".join(_expr_text(x) for x in e["items"]) + "}"
    return e.get("text", "?")


expr_text = _expr_text


def iter_stmts(stmts):
    """문장 트리 전위 순회."""
    for st in stmts or []:
        yield st
        for key in ("init", "then", "else", "body", "inc"):
            sub = st.get(key)
            if sub:
                yield from iter_stmts(sub)


def key_parts(key: str) -> tuple[str, str, str]:
    """'*G:src/a.c::s_x.f.g' → ('*', 'G:src/a.c::s_x', '.f.g'). 파일 경로의 '.' 과 필드 구분자를 구별한다."""
    stars = len(key) - len(key.lstrip("*&"))
    pre, k = key[:stars], key[stars:]
    i = k.rfind("::")
    start = i + 2 if i >= 0 else 0
    j = k.find(".", start)
    if j < 0:
        return pre, k, ""
    return pre, k[:j], k[j:]


def kbase(key: str) -> str:
    """접두(*,&)와 필드를 뗀 기본 변수 키."""
    return key_parts(key)[1]
