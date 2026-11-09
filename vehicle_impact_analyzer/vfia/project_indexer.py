"""프로젝트 인덱서: 소스·빌드 설정·심벌 수집.

* compile_commands.json(번역 단위별 컴파일 명령)을 분석 입력으로 사용한다.
  없으면 sources/args 설정으로 '합성 compile DB'를 만들고 보고서에 표시한다.
* .h는 독립 파싱하지 않는다. 실제 .c에 포함되는 문맥에서만 분석하고, 헤더에서
  정의된 심벌은 원본 위치(헤더 경로)와 포함한 번역 단위 목록을 별도로 관리한다.
  같은 헤더 정의가 번역 단위마다 다르게 전개되면(매크로 문맥 차이) 진단으로 남긴다.
* 변수는 이름이 아니라 선언 위치·스코프로 식별한다(USR -> 안정 키).
    fn:<name> / fn:<file>:<name>(static) / gv:<name> / gv:<file>:<name>(static)
    gv:<file>:<func>/<name>(함수 내 static) / lv:<func>:<name>[#n] / pv:<func>:<name>
    fd:<record>.<field> / ty:<record> / td:<typedef> / en:<enum> / ec:<const> / mc:<macro>
* 결과는 libclang 커서가 아니라 직렬화 가능한 '일반화 AST'로 저장한다. 이 AST를
  CFG 생성, AST 차이 분석, 기호 실행이 공통으로 사용한다.
"""
from __future__ import annotations

import fnmatch
import glob
import hashlib
import json
import os
import re
import shlex
import tempfile
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import clang.cindex as ci

from . import clang_api as ca
from .config import BuildConfig, VersionSpec

K = ci.CursorKind

_COND_RE = re.compile(r"^\s*#\s*(if|ifdef|ifndef|elif)\b(.*)$")
_IDENT_RE = re.compile(r"[A-Za-z_]\w*")


@dataclass
class CompileEntry:
    file: str
    directory: str
    args: List[str]                    # Clang용으로 변환된 인자
    origin: str = "compile_commands"   # compile_commands | build_log | synthetic
    output: Optional[str] = None
    profile: str = "gcc"               # 원래 컴파일러 프로필
    dropped: List[str] = field(default_factory=list)
    original: List[str] = field(default_factory=list)


def _db_from_build_log(v: VersionSpec, build: BuildConfig) -> Optional[List[dict]]:
    from .build_env import parse_build_log
    if not build.build_log or not os.path.isfile(build.build_log):
        return None
    with open(build.build_log, "r", encoding="utf-8", errors="replace") as f:
        return parse_build_log(f.read(), os.path.abspath(v.root), build.compiler, build.compiler_regex)


def load_compile_entries(v: VersionSpec, build: BuildConfig) -> Tuple[List[CompileEntry], List[str]]:
    from .build_env import detect_profile, translate
    notes: List[str] = []
    entries: List[CompileEntry] = []
    root = os.path.abspath(v.root)
    db = None
    origin = "compile_commands"
    db_dir = root
    if v.compile_commands and os.path.isfile(v.compile_commands):
        with open(v.compile_commands, "r", encoding="utf-8") as f:
            db = json.load(f)
        db_dir = os.path.dirname(os.path.abspath(v.compile_commands))
    else:
        db = _db_from_build_log(v, build)
        if db is not None:
            origin = "build_log"
            notes.append(f"빌드 로그 {build.build_log}에서 컴파일 명령 {len(db)}개 추출")
    if db is not None:
        by_file: Dict[str, List[dict]] = {}
        for e in db:
            d = e.get("directory", root)
            if not os.path.isabs(d):   # 비표준이지만 이식성을 위해 compile DB 위치 기준 상대 경로 허용
                d = os.path.normpath(os.path.join(db_dir, d))
                e["directory"] = d
            src = e["file"] if os.path.isabs(e["file"]) else os.path.normpath(os.path.join(d, e["file"]))
            by_file.setdefault(src, []).append(e)
        for src, es in sorted(by_file.items()):
            if not src.startswith(root):
                continue
            rel = os.path.relpath(src, root)
            if any(fnmatch.fnmatch(rel, pat) for pat in v.exclude):
                continue
            chosen = es
            if len(es) > 1:
                if build.variant_filter:
                    chosen = [e for e in es if build.variant_filter in (e.get("command") or " ".join(e.get("arguments", []))
                                                                         + (e.get("output") or ""))]
                    if not chosen:
                        notes.append(f"{rel}: variant_filter '{build.variant_filter}'와 일치하는 항목이 없어 첫 항목 사용")
                        chosen = es[:1]
                else:
                    notes.append(f"{rel}: compile DB에 빌드 설정 {len(es)}개가 있어 첫 항목만 분석함"
                                 " (build.variant_filter로 선택 가능)")
                chosen = chosen[:1]
            e = chosen[0]
            d = e.get("directory", root)
            argv = e.get("arguments") or shlex.split(e.get("command", ""))
            prof = detect_profile(argv, e.get("compiler_profile") or build.compiler)
            tr = translate(argv, d, src, prof)
            entries.append(CompileEntry(src, d, tr.args, origin, e.get("output"), prof, tr.dropped, argv))
    else:
        if v.compile_commands:
            notes.append(f"compile_commands '{v.compile_commands}'가 없어 sources/args로 합성 compile DB 사용")
        else:
            notes.append("compile_commands 미지정: sources/args로 합성 compile DB 사용(실제 빌드 설정과 다를 수 있음)")
        pats = v.sources or ["**/*.c"]
        files = set()
        for pat in pats:
            files.update(glob.glob(os.path.join(root, pat), recursive=True))
        prof = build.compiler if build.compiler != "auto" else "gcc"
        for src in sorted(files):
            rel = os.path.relpath(src, root)
            if any(fnmatch.fnmatch(rel, pat) for pat in v.exclude):
                continue
            argv = ["cc"] + v.args
            tr = translate(argv, root, src, prof)
            entries.append(CompileEntry(os.path.abspath(src), root, tr.args, "synthetic", None, prof, tr.dropped, argv))
    return entries, notes


def write_compile_commands(entries: List[CompileEntry], path: str, compiler: str = "cc") -> None:
    db = [{"directory": e.directory, "file": e.file,
           "arguments": [compiler] + e.args + ["-c", e.file]} for e in entries]
    with open(path, "w", encoding="utf-8") as f:
        json.dump(db, f, indent=2)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:20]


def file_hash(path: str) -> Optional[str]:
    try:
        with open(path, "rb") as f:
            return _sha(f.read())
    except OSError:
        return None


def strip_ast(node: dict) -> dict:
    """위치·ID를 제거한 정규화 AST(해시·동일성 비교용)."""
    out = {k: v for k, v in node.items() if k not in ("id", "l", "c", "el", "ec", "f", "a", "cv", "ai", "comment")}
    if "ch" in node:
        out["ch"] = [strip_ast(c) for c in node["ch"]]
    return out


def ast_hash(node: dict) -> str:
    return _sha(json.dumps(strip_ast(node), sort_keys=True).encode())


class _UnitBuilder:
    """한 단위(함수, 전역 변수 등)의 일반화 AST 노드 생성기."""

    def __init__(self, tux: "TUExtractor", unit_file: Optional[str]):
        self.tux = tux
        self.nid = 0
        self.unit_file = unit_file

    def mk(self, k: str, cur: Optional[ci.Cursor] = None, **kw) -> dict:
        n = {"k": k, "id": self.nid}
        self.nid += 1
        if cur is not None:
            ext = cur.extent
            n["l"], n["c"] = ext.start.line, ext.start.column
            n["el"], n["ec"] = ext.end.line, ext.end.column
            f = self.tux.rel_of(cur)
            if f and f != self.unit_file:
                n["f"] = f
            mac = self.tux.macro_at(cur)
            if mac:
                n["mac"] = mac
        for k2, v in kw.items():
            if v is not None:
                n[k2] = v
        return n


class TUExtractor:
    """번역 단위 하나를 일반화 AST/심벌 정보로 변환한다."""

    def __init__(self, tu: ci.TranslationUnit, root: str, entry: CompileEntry):
        self.tu = tu
        self.root = os.path.abspath(root)
        self.entry = entry
        self.main_rel = os.path.relpath(entry.file, self.root)
        self.units: Dict[str, dict] = {}
        self.fn_decls: List[dict] = []
        self.global_decls: List[dict] = []
        self.files: Dict[str, dict] = {}
        self.macro_inst: Dict[Tuple[str, int, int], str] = {}
        self.macro_uses: List[dict] = []
        self.usr2key: Dict[str, str] = {}
        self.diags: List[dict] = []
        self.ref_functions: Dict[str, dict] = {}   # 참조된 함수(외부 포함)
        self.ref_globals: Dict[str, dict] = {}
        self._rel_cache: Dict[str, Optional[str]] = {}
        self._cur_fn: Optional[str] = None
        self._local_names: Dict[str, int] = {}

    # ---- 위치 ----
    def rel_path(self, name: str) -> Optional[str]:
        if name in self._rel_cache:
            return self._rel_cache[name]
        p = name if os.path.isabs(name) else os.path.join(self.entry.directory, name)
        p = os.path.normpath(p)
        r = os.path.relpath(p, self.root) if p.startswith(self.root + os.sep) else None
        self._rel_cache[name] = r
        return r

    def rel_of(self, cur: ci.Cursor) -> Optional[str]:
        f = cur.location.file
        return self.rel_path(f.name) if f is not None else None

    def macro_at(self, cur: ci.Cursor) -> Optional[str]:
        loc = cur.location
        if loc.file is None:
            return None
        r = self.rel_path(loc.file.name)
        if r is None:
            return None
        return self.macro_inst.get((r, loc.line, loc.column))

    # ---- 키 ----
    def key_of(self, cur: ci.Cursor) -> Optional[str]:
        if cur is None:
            return None
        usr = cur.get_usr()
        if usr and usr in self.usr2key:
            return self.usr2key[usr]
        k = cur.kind
        name = cur.spelling
        key = None
        if k == K.FUNCTION_DECL:
            internal = cur.linkage == ci.LinkageKind.INTERNAL
            f = self.rel_of(cur) or "<ext>"
            key = f"fn:{f}:{name}" if internal else f"fn:{name}"
        elif k == K.VAR_DECL:
            if cur.semantic_parent is not None and cur.semantic_parent.kind == K.TRANSLATION_UNIT:
                internal = cur.linkage == ci.LinkageKind.INTERNAL
                f = self.rel_of(cur) or "<ext>"
                key = f"gv:{f}:{name}" if internal else f"gv:{name}"
            else:
                key = f"lv:?:{name}"
        elif k == K.FIELD_DECL:
            parent = cur.semantic_parent
            key = f"fd:{ca.record_name(parent)}.{name}"
        elif k == K.ENUM_CONSTANT_DECL:
            key = f"ec:{name}"
        elif k in (K.STRUCT_DECL, K.UNION_DECL):
            key = f"ty:{ca.record_name(cur)}"
        elif k == K.TYPEDEF_DECL:
            key = f"td:{name}"
        elif k == K.ENUM_DECL:
            key = f"en:{ca.record_name(cur)}"
        if key and usr:
            self.usr2key[usr] = key
        return key

    # ---- 실행 ----
    def run(self) -> dict:
        for d in self.tu.diagnostics:
            if d.severity >= ci.Diagnostic.Warning:
                loc = d.location
                self.diags.append({
                    "severity": "error" if d.severity >= ci.Diagnostic.Error else "warning",
                    "fatal": d.severity >= ci.Diagnostic.Fatal,
                    "file": self.rel_path(loc.file.name) if loc.file else None,
                    "line": loc.line, "message": d.spelling, "tu": self.main_rel,
                })
        self._collect_files()
        tops = list(self.tu.cursor.get_children())
        for c in tops:   # 1차: 매크로 정의·전개 위치
            if c.kind == K.MACRO_INSTANTIATION:
                r = self.rel_of(c)
                if r:
                    self.macro_inst[(r, c.location.line, c.location.column)] = c.spelling
                    self.macro_uses.append({"macro": c.spelling, "file": r, "line": c.location.line,
                                            "col": c.location.column})
            elif c.kind == K.MACRO_DEFINITION:
                self._macro_def(c)
        for c in tops:   # 2차: 선언
            if c.kind in (K.MACRO_INSTANTIATION, K.MACRO_DEFINITION, K.INCLUSION_DIRECTIVE):
                continue
            r = self.rel_of(c)
            if r is None:
                continue  # 시스템/외부 헤더
            try:
                self._top_decl(c, r)
            except Exception as e:  # 견고성: 한 선언 실패가 전체를 막지 않도록
                self.diags.append({"severity": "warning", "file": r, "line": c.location.line,
                                   "message": f"AST 변환 실패({c.kind.name} {c.spelling}): {e}", "tu": self.main_rel,
                                   "code": "conversion_error"})
        return {
            "tu": self.main_rel,
            "units": self.units,
            "fn_decls": self.fn_decls,
            "global_decls": self.global_decls,
            "files": self.files,
            "macro_uses": self.macro_uses,
            "diags": self.diags,
            "ref_functions": self.ref_functions,
            "ref_globals": self.ref_globals,
        }

    def _collect_files(self) -> None:
        main = self.main_rel
        self.files.setdefault(main, {"includes": [], "is_header": False})
        for inc in self.tu.get_includes():
            src = self.rel_path(inc.source.name) if inc.source else None
            dst = self.rel_path(inc.include.name)
            if dst is None:
                continue
            self.files.setdefault(dst, {"includes": [], "is_header": True})
            if src is not None:
                self.files.setdefault(src, {"includes": [], "is_header": src != main})
                if dst not in self.files[src]["includes"]:
                    self.files[src]["includes"].append(dst)
        for rel, info in self.files.items():
            absf = os.path.join(self.root, rel)
            info["hash"] = file_hash(absf)
            info["inactive"] = ca.skipped_ranges(self.tu, absf)
            conds = set()
            try:
                with open(absf, "r", encoding="utf-8", errors="replace") as f:
                    for line in f:
                        m = _COND_RE.match(line)
                        if m:
                            for ident in _IDENT_RE.findall(m.group(2)):
                                if ident != "defined":
                                    conds.add(ident)
            except OSError:
                pass
            info["cond_macros"] = sorted(conds)

    # ---- 매크로 ----
    def _macro_def(self, c: ci.Cursor) -> None:
        r = self.rel_of(c)
        if r is None:
            return
        toks = [t.spelling for t in c.get_tokens()]
        fnlike = ca.is_macro_function_like(c)
        body_toks = toks[1:]
        params = []
        if fnlike and body_toks and body_toks[0] == "(":
            j = body_toks.index(")") if ")" in body_toks else 0
            params = [t for t in body_toks[1:j] if t != ","]
            body_toks = body_toks[j + 1:]
        body = " ".join(body_toks)
        key = f"mc:{c.spelling}"
        unit = {
            "kind": "macro", "key": key, "name": c.spelling, "file": r, "line": c.location.line,
            "end_line": c.extent.end.line, "fnlike": fnlike, "params": params, "body": body,
            "value": _macro_int_value(body_toks),
            "ast": {"k": "Macro", "v": c.spelling, "id": 0, "body": body, "fnlike": fnlike, "params": params},
        }
        if key in self.units and self.units[key]["body"] != body:
            self.diags.append({"severity": "warning", "code": "macro_redefined", "file": r,
                               "line": c.location.line, "tu": self.main_rel,
                               "message": f"매크로 {c.spelling} 재정의: '{self.units[key]['body']}' -> '{body}'"})
        self.units[key] = unit

    # ---- 최상위 선언 ----
    def _top_decl(self, c: ci.Cursor, r: str) -> None:
        k = c.kind
        if k == K.FUNCTION_DECL:
            key = self.key_of(c)
            if c.is_definition():
                self._function(c, key, r)
            else:
                self.fn_decls.append({"key": key, "name": c.spelling, "file": r, "line": c.location.line,
                                      "sig": _sig_text(c), "sig_cn": _sig_cn(c)})
        elif k == K.VAR_DECL:
            self._global_var(c, r)
        elif k in (K.STRUCT_DECL, K.UNION_DECL):
            if c.is_definition():
                self._record(c, r)
        elif k == K.TYPEDEF_DECL:
            self._typedef(c, r)
        elif k == K.ENUM_DECL:
            if c.is_definition():
                self._enum(c, r, None)

    def _global_var(self, c: ci.Cursor, r: str, owner_fn: Optional[str] = None, key: Optional[str] = None) -> dict:
        key = key or self.key_of(c)
        storage = c.storage_class
        sname = {ci.StorageClass.STATIC: "static", ci.StorageClass.EXTERN: "extern"}.get(storage, "none")
        init = _find_init(c)
        is_def = storage != ci.StorageClass.EXTERN or init is not None
        td = ca.type_desc(c.type)
        if not is_def:
            self.global_decls.append({"key": key, "name": c.spelling, "file": r, "line": c.location.line, "ty": td})
            return {}
        ub = _UnitBuilder(self, r)
        ast = ub.mk("GlobalVar", c, v=c.spelling, ref=key, ty=td)
        ast["ch"] = [self._expr(ub, init)] if init is not None else []
        unit = {
            "kind": "global", "key": key, "name": c.spelling, "file": r, "line": c.location.line,
            "end_line": c.extent.end.line, "ty": td, "storage": sname, "owner_fn": owner_fn,
            "volatile": bool(td.get("vol")), "const": bool(td.get("const")),
            "comment": c.raw_comment, "ast": ast,
        }
        self._add_unit(unit)
        for ch in c.get_children():
            if ch.kind in (K.STRUCT_DECL, K.UNION_DECL) and ch.is_definition():
                self._record(ch, r)
        return unit

    def _add_unit(self, unit: dict) -> None:
        key = unit["key"]
        unit["hash"] = ast_hash(unit["ast"])
        old = self.units.get(key)
        if old is not None and old.get("hash") != unit["hash"]:
            self.diags.append({"severity": "warning", "code": "duplicate_definition", "file": unit["file"],
                               "line": unit["line"], "tu": self.main_rel,
                               "message": f"{key} 정의가 같은 번역 단위에서 중복됨"})
        self.units[key] = unit

    def _record(self, c: ci.Cursor, r: str, name: Optional[str] = None) -> None:
        rname = name or ca.record_name(c)
        key = f"ty:{rname}"
        if c.get_usr():
            self.usr2key[c.get_usr()] = key
        ub = _UnitBuilder(self, r)
        ast = ub.mk("Record", c, v=rname, union=c.kind == K.UNION_DECL)
        fields = []
        ch_nodes = []
        for fc in c.get_children():
            if fc.kind == K.FIELD_DECL:
                fkey = f"fd:{rname}.{fc.spelling}"
                self.usr2key[fc.get_usr()] = fkey
                try:
                    off = c.type.get_offset(fc.spelling)
                except Exception:
                    off = -1
                td = ca.type_desc(fc.type)
                fields.append({"name": fc.spelling, "key": fkey, "ty": td, "offset_bits": off})
                ch_nodes.append(ub.mk("Field", fc, v=fc.spelling, ref=fkey, ty=td, off=off))
            elif fc.kind in (K.STRUCT_DECL, K.UNION_DECL) and fc.is_definition():
                self._record(fc, r)
        ast["ch"] = ch_nodes
        try:
            size = c.type.get_size()
        except Exception:
            size = -1
        self._add_unit({"kind": "record", "key": key, "name": rname, "file": r, "line": c.location.line,
                        "end_line": c.extent.end.line, "fields": fields, "size": size, "ast": ast,
                        "comment": c.raw_comment})

    def _typedef(self, c: ci.Cursor, r: str) -> None:
        name = c.spelling
        key = f"td:{name}"
        self.usr2key[c.get_usr()] = key
        under = c.underlying_typedef_type
        for ch in c.get_children():
            if ch.kind in (K.STRUCT_DECL, K.UNION_DECL) and ch.is_definition():
                rn = ca.record_name(ch)
                self._record(ch, r, rn if not rn.startswith("anon@") else name)
            elif ch.kind == K.ENUM_DECL and ch.is_definition():
                self._enum(ch, r, name)
        ub = _UnitBuilder(self, r)
        ast = ub.mk("Typedef", c, v=name, ty=ca.type_desc(under))
        self._add_unit({"kind": "typedef", "key": key, "name": name, "file": r, "line": c.location.line,
                        "end_line": c.extent.end.line, "ast": ast})

    def _enum(self, c: ci.Cursor, r: str, tdname: Optional[str]) -> None:
        name = ca.record_name(c)
        if name.startswith("anon@") and tdname:
            name = tdname
        key = f"en:{name}"
        ub = _UnitBuilder(self, r)
        ast = ub.mk("Enum", c, v=name)
        consts = []
        for ec in c.get_children():
            if ec.kind == K.ENUM_CONSTANT_DECL:
                ckey = f"ec:{ec.spelling}"
                self.usr2key[ec.get_usr()] = ckey
                consts.append(ub.mk("EnumConst", ec, v=ec.spelling, ref=ckey, val=ec.enum_value))
        ast["ch"] = consts
        self._add_unit({"kind": "enum", "key": key, "name": name, "file": r, "line": c.location.line,
                        "end_line": c.extent.end.line, "ast": ast,
                        "constants": {n["v"]: n["val"] for n in consts}})

    # ---- 함수 ----
    def _function(self, c: ci.Cursor, key: str, r: str) -> None:
        self._cur_fn = key
        self._local_names = {}
        fsuf = key[3:]
        ub = _UnitBuilder(self, r)
        params = []
        for i, p in enumerate(c.get_arguments()):
            pname = p.spelling or f"arg{i}"
            pkey = f"pv:{fsuf}:{pname}"
            if p.get_usr():
                self.usr2key[p.get_usr()] = pkey
            params.append({"name": pname, "key": pkey, "ty": ca.type_desc(p.type), "index": i})
        toks = [t.spelling for t in c.get_tokens()][:8]
        ast = ub.mk("Func", c, v=c.spelling, ref=key, params=params, ret=ca.type_desc(c.result_type),
                    static=c.linkage == ci.LinkageKind.INTERNAL, inline="inline" in toks or "__inline" in toks)
        body = None
        for ch in c.get_children():
            if ch.kind == K.COMPOUND_STMT:
                body = ch
        ast["ch"] = [self._stmt(ub, body)] if body is not None else []
        self._add_unit({
            "kind": "function", "key": key, "name": c.spelling, "file": r, "line": c.location.line,
            "end_line": c.extent.end.line, "static": ast["static"], "comment": c.raw_comment,
            "sig": _sig_text(c), "sig_cn": _sig_cn(c), "params": params, "ret": ast["ret"], "ast": ast,
            "variadic": c.type.is_function_variadic() if c.type.kind == ci.TypeKind.FUNCTIONPROTO else False,
        })
        self._cur_fn = None

    def _local_key(self, name: str) -> str:
        fsuf = (self._cur_fn or "fn:?")[3:]
        n = self._local_names.get(name, 0) + 1
        self._local_names[name] = n
        return f"lv:{fsuf}:{name}" + (f"#{n}" if n > 1 else "")

    def _stmt(self, ub: _UnitBuilder, c: ci.Cursor) -> dict:
        k = c.kind
        chs = list(c.get_children())
        if k == K.COMPOUND_STMT:
            n = ub.mk("Compound", c)
            n["ch"] = [self._stmt(ub, x) for x in chs]
            return n
        if k == K.IF_STMT:
            n = ub.mk("If", c)
            n["ch"] = [self._expr(ub, chs[0]), self._stmt(ub, chs[1])] + ([self._stmt(ub, chs[2])] if len(chs) > 2 else [])
            return n
        if k == K.WHILE_STMT:
            n = ub.mk("While", c)
            n["ch"] = [self._expr(ub, chs[0]), self._stmt(ub, chs[1])]
            return n
        if k == K.DO_STMT:
            n = ub.mk("Do", c)
            n["ch"] = [self._stmt(ub, chs[0]), self._expr(ub, chs[1])]
            return n
        if k == K.FOR_STMT:
            return self._for(ub, c, chs)
        if k == K.SWITCH_STMT:
            n = ub.mk("Switch", c)
            n["ch"] = [self._expr(ub, chs[0]), self._stmt(ub, chs[-1])]
            return n
        if k == K.CASE_STMT:
            val = ca.evaluate(chs[0])
            hi = ca.evaluate(chs[1]) if len(chs) == 3 else None
            n = ub.mk("Case", c, v=val if hi is None else [val, hi], vtext=" ".join(t.spelling for t in chs[0].get_tokens()) or None)
            n["ch"] = [self._stmt(ub, chs[-1])]
            return n
        if k == K.DEFAULT_STMT:
            n = ub.mk("Default", c)
            n["ch"] = [self._stmt(ub, chs[0])] if chs else []
            return n
        if k == K.BREAK_STMT:
            return ub.mk("Break", c)
        if k == K.CONTINUE_STMT:
            return ub.mk("Continue", c)
        if k == K.NULL_STMT:
            return ub.mk("Null", c)
        if k == K.RETURN_STMT:
            n = ub.mk("Return", c)
            n["ch"] = [self._expr(ub, chs[0])] if chs else []
            return n
        if k == K.GOTO_STMT:
            return ub.mk("Goto", c, v=chs[0].spelling if chs else "?")
        if k == K.LABEL_STMT:
            n = ub.mk("Label", c, v=c.spelling)
            n["ch"] = [self._stmt(ub, chs[0])] if chs else []
            return n
        if k == K.DECL_STMT:
            n = ub.mk("DeclStmt", c)
            n["ch"] = [self._local_var(ub, v) for v in chs if v.kind == K.VAR_DECL]
            return n
        if k in (K.ASM_STMT, K.MS_ASM_STMT) or k.name == "GCC_ASM_STMT":
            return ub.mk("Asm", c, text=" ".join(t.spelling for t in c.get_tokens())[:200])
        if k.is_expression():
            n = ub.mk("ExprStmt", c)
            n["ch"] = [self._expr(ub, c)]
            return n
        return ub.mk("UnknownStmt", c, v=k.name)

    def _for(self, ub: _UnitBuilder, c: ci.Cursor, chs: List[ci.Cursor]) -> dict:
        n = ub.mk("For", c)
        body = chs[-1]
        rest = chs[:-1]
        # for ( init ; cond ; inc ) 의 세미콜론 위치로 생략된 부분을 판별한다.
        semis = []
        depth = 0
        for t in c.get_tokens():
            s = t.spelling
            if s == "(":
                depth += 1
            elif s == ")":
                depth -= 1
                if depth == 0:
                    break
            elif s == ";" and depth == 1:
                semis.append(t.extent.start.offset)
        init = cond = inc = None
        if len(semis) >= 2:
            for x in rest:
                off = x.extent.start.offset
                if off < semis[0]:
                    init = x
                elif off < semis[1]:
                    cond = x
                else:
                    inc = x
        else:
            if len(rest) == 3:
                init, cond, inc = rest
            elif len(rest) == 2:
                init, cond = rest
            elif len(rest) == 1:
                cond = rest[0]
        parts = []
        has = {}
        if init is not None:
            parts.append(self._stmt(ub, init) if init.kind == K.DECL_STMT else self._wrap_expr_stmt(ub, init))
            has["init"] = True
        if cond is not None:
            parts.append(self._expr(ub, cond))
            has["cond"] = True
        if inc is not None:
            parts.append(self._wrap_expr_stmt(ub, inc))
            has["inc"] = True
        parts.append(self._stmt(ub, body))
        n["ch"] = parts
        n["has"] = has
        return n

    def _wrap_expr_stmt(self, ub: _UnitBuilder, c: ci.Cursor) -> dict:
        n = ub.mk("ExprStmt", c)
        n["ch"] = [self._expr(ub, c)]
        return n

    def _local_var(self, ub: _UnitBuilder, v: ci.Cursor) -> dict:
        name = v.spelling
        td = ca.type_desc(v.type)
        if v.storage_class == ci.StorageClass.STATIC:
            # 함수 내 static 변수는 호출 사이에 값이 유지되므로 전역 상태로 취급한다.
            fn = self._cur_fn or "fn:?"
            f = ub.unit_file or "?"
            key = f"gv:{f}:{fn.split(':')[-1]}/{name}"
            self.usr2key[v.get_usr()] = key
            self._global_var(v, f, owner_fn=fn, key=key)
            return ub.mk("Var", v, v=name, ref=key, ty=td, static=True)
        key = self._local_key(name)
        self.usr2key[v.get_usr()] = key
        init = _find_init(v)
        n = ub.mk("Var", v, v=name, ref=key, ty=td)
        n["ch"] = [self._expr(ub, init)] if init is not None else []
        return n

    def _expr(self, ub: _UnitBuilder, c: ci.Cursor) -> dict:
        k = c.kind
        chs = [x for x in c.get_children() if x.kind not in (K.TYPE_REF, K.PARM_DECL)]
        if k == K.UNEXPOSED_EXPR or k.name in ("CXX_FUNCTIONAL_CAST_EXPR",):
            if len(chs) == 1:
                inner = self._expr(ub, chs[0])
                td = ca.type_desc(c.type)
                ity = inner.get("ty") or {}
                if ity.get("cn") == td.get("cn"):
                    return inner
                n = ub.mk("ICast", c, ty=td)
                n["ch"] = [inner]
                return n
            if not chs:
                return ub.mk("Lit", c, v=0, ty=ca.type_desc(c.type), implicit=True)
            n = ub.mk("Unknown", c, v="UNEXPOSED", ty=ca.type_desc(c.type))
            n["ch"] = [self._expr(ub, x) for x in chs]
            return n
        if k == K.PAREN_EXPR:
            return self._expr(ub, chs[0]) if chs else ub.mk("Unknown", c, v="PAREN")
        if k == K.CSTYLE_CAST_EXPR:
            n = ub.mk("Cast", c, ty=ca.type_desc(c.type))
            n["ch"] = [self._expr(ub, chs[-1])] if chs else []
            return n
        if k == K.BINARY_OPERATOR:
            op = ca.binary_op(c)
            kind = "Assign" if op == "=" else ("Comma" if op == "," else "Bin")
            n = ub.mk(kind, c, v=None if kind != "Bin" else op, ty=ca.type_desc(c.type))
            n["ch"] = [self._expr(ub, x) for x in chs[:2]]
            return n
        if k == K.COMPOUND_ASSIGNMENT_OPERATOR:
            n = ub.mk("CAssign", c, v=ca.binary_op(c), ty=ca.type_desc(c.type))
            n["ch"] = [self._expr(ub, x) for x in chs[:2]]
            return n
        if k == K.UNARY_OPERATOR:
            n = ub.mk("Un", c, v=ca.unary_op(c), ty=ca.type_desc(c.type))
            n["ch"] = [self._expr(ub, chs[0])] if chs else []
            return n
        if k == K.CXX_UNARY_EXPR:
            return ub.mk("Lit", c, v=ca.evaluate(c), ty=ca.type_desc(c.type), orig="sizeof")
        if k in (K.INTEGER_LITERAL, K.CHARACTER_LITERAL):
            return ub.mk("Lit", c, v=ca.evaluate(c), ty=ca.type_desc(c.type))
        if k == K.FLOATING_LITERAL:
            return ub.mk("FLit", c, v=ca.evaluate(c), ty=ca.type_desc(c.type))
        if k == K.STRING_LITERAL:
            return ub.mk("Str", c, v=c.spelling)
        if k == K.DECL_REF_EXPR:
            return self._ref(ub, c)
        if k == K.MEMBER_REF_EXPR:
            fld = c.referenced
            fkey = self.key_of(fld) if fld is not None else None
            arrow = False
            if chs:
                arrow = chs[0].type.get_canonical().kind == ci.TypeKind.POINTER
            n = ub.mk("Member", c, v=c.spelling, ref=fkey, arrow=arrow, ty=ca.type_desc(c.type))
            n["ch"] = [self._expr(ub, chs[0])] if chs else []
            return n
        if k == K.ARRAY_SUBSCRIPT_EXPR:
            n = ub.mk("Index", c, ty=ca.type_desc(c.type))
            n["ch"] = [self._expr(ub, x) for x in chs[:2]]
            return n
        if k == K.CALL_EXPR:
            ref = c.referenced
            direct = None
            if ref is not None and ref.kind == K.FUNCTION_DECL:
                direct = self.key_of(ref)
                self._note_function_ref(ref, direct)
            n = ub.mk("Call", c, v=c.spelling, ref=direct, ty=ca.type_desc(c.type))
            n["ch"] = [self._expr(ub, x) for x in chs]
            return n
        if k == K.CONDITIONAL_OPERATOR:
            n = ub.mk("Cond", c, ty=ca.type_desc(c.type))
            n["ch"] = [self._expr(ub, x) for x in chs[:3]]
            return n
        if k == K.INIT_LIST_EXPR:
            td = ca.type_desc(c.type)
            n = ub.mk("InitList", c, ty=td)
            n["ch"] = [self._expr(ub, x) for x in chs]
            if td.get("k") == "r":
                decl = c.type.get_canonical().get_declaration()
                rn = td.get("rec")
                n["fields"] = [f"fd:{rn}.{f.spelling}" for f in decl.get_children() if f.kind == K.FIELD_DECL]
            return n
        if k == K.COMPOUND_LITERAL_EXPR:
            n = ub.mk("CompoundLit", c, ty=ca.type_desc(c.type))
            n["ch"] = [self._expr(ub, x) for x in chs]
            return n
        n = ub.mk("Unknown", c, v=k.name, ty=ca.type_desc(c.type) if c.type else None)
        n["ch"] = [self._expr(ub, x) for x in chs if x.kind.is_expression()]
        return n

    def _ref(self, ub: _UnitBuilder, c: ci.Cursor) -> dict:
        ref = c.referenced
        td = ca.type_desc(c.type)
        if ref is None:
            return ub.mk("Ref", c, v=c.spelling, ty=td, rk="?")
        rk = ref.kind
        if rk == K.FUNCTION_DECL:
            key = self.key_of(ref)
            self._note_function_ref(ref, key)
            return ub.mk("Ref", c, v=c.spelling, ref=key, rk="f", ty=td)
        if rk == K.ENUM_CONSTANT_DECL:
            return ub.mk("Ref", c, v=c.spelling, ref=self.key_of(ref), rk="e", val=ref.enum_value, ty=td)
        if rk == K.PARM_DECL:
            key = self.usr2key.get(ref.get_usr()) or f"pv:?:{c.spelling}"
            return ub.mk("Ref", c, v=c.spelling, ref=key, rk="p", ty=td)
        if rk == K.VAR_DECL:
            key = self.usr2key.get(ref.get_usr())
            if key is None:
                key = self.key_of(ref)
            if key.startswith("gv:"):
                if key not in self.ref_globals:
                    self.ref_globals[key] = {"name": ref.spelling, "file": self.rel_of(ref), "ty": ca.type_desc(ref.type)}
                return ub.mk("Ref", c, v=c.spelling, ref=key, rk="g", ty=td)
            return ub.mk("Ref", c, v=c.spelling, ref=key, rk="l", ty=td)
        return ub.mk("Ref", c, v=c.spelling, rk="?", ty=td)

    def _note_function_ref(self, ref: ci.Cursor, key: Optional[str]) -> None:
        if key and key not in self.ref_functions:
            params = []
            try:
                for i, p in enumerate(ref.get_arguments()):
                    params.append({"name": p.spelling or f"arg{i}", "ty": ca.type_desc(p.type), "index": i})
            except Exception:
                pass
            self.ref_functions[key] = {"name": ref.spelling, "file": self.rel_of(ref), "params": params,
                                       "ret": ca.type_desc(ref.result_type)}


def _sig_text(c: ci.Cursor) -> str:
    try:
        return f"{c.result_type.spelling} {c.spelling}(" + ", ".join(
            a.type.spelling for a in c.get_arguments()) + ")"
    except Exception:
        return c.spelling


def _sig_cn(c: ci.Cursor) -> str:
    """정규(canonical) 자료형 기준 시그니처: typedef 이름 차이는 무시하고 실제 자료형만 비교."""
    try:
        return c.result_type.get_canonical().spelling + "(" + ", ".join(
            a.type.get_canonical().spelling for a in c.get_arguments()) + ")"
    except Exception:
        return ""


def _find_init(v: ci.Cursor) -> Optional[ci.Cursor]:
    chs = [x for x in v.get_children() if x.kind.is_expression()]
    if not chs:
        return None
    toks = list(v.get_tokens())
    eq = None
    depth = 0
    for t in toks:
        s = t.spelling
        if s in "([{":
            depth += 1
        elif s in ")]}":
            depth -= 1
        elif s == "=" and depth == 0:
            eq = t.extent.start.offset
            break
    if eq is not None:
        after = [x for x in chs if x.extent.start.offset > eq]
        return after[-1] if after else None
    if not toks:
        # 매크로 전개로 토큰이 없는 경우: 배열 크기식이 아닌 마지막 식을 초기화식으로 추정
        last = chs[-1]
        if v.type.get_canonical().kind != ci.TypeKind.CONSTANTARRAY or last.kind in (K.INIT_LIST_EXPR, K.STRING_LITERAL):
            return last
    return None


def _macro_int_value(toks: List[str]) -> Optional[int]:
    t = [x for x in toks if x not in ("(", ")")]
    if len(t) == 1:
        s = t[0].rstrip("uUlL")
        try:
            return int(s, 0)
        except ValueError:
            return None
    if len(t) == 2 and t[0] == "-":
        try:
            return -int(t[1].rstrip("uUlL"), 0)
        except ValueError:
            return None
    return None


# ---------------------------------------------------------------------------
class ProjectIndex:
    """여러 번역 단위의 추출 결과를 병합한 프로젝트 색인."""

    def __init__(self, version: str, root: str, build: BuildConfig):
        self.version = version
        self.root = root
        self.build = build
        self.units: Dict[str, dict] = {}
        self.unit_tus: Dict[str, List[str]] = {}
        self.fn_decls: Dict[str, List[dict]] = {}
        self.global_decls: Dict[str, List[dict]] = {}
        self.files: Dict[str, dict] = {}
        self.tus: List[dict] = []
        self.macro_uses: List[dict] = []
        self.diags: List[dict] = []
        self.ref_functions: Dict[str, dict] = {}
        self.ref_globals: Dict[str, dict] = {}
        self.notes: List[str] = []
        self.parse_health: dict = {}
        self._src_cache: Dict[str, List[str]] = {}

    def merge(self, res: dict) -> None:
        tu = res["tu"]
        for key, u in res["units"].items():
            self.unit_tus.setdefault(key, [])
            if tu not in self.unit_tus[key]:
                self.unit_tus[key].append(tu)
            old = self.units.get(key)
            if old is None:
                self.units[key] = u
            elif old.get("hash") != u.get("hash"):
                if u["kind"] == "macro":
                    self.diags.append({"severity": "warning", "code": "macro_context_variant", "file": u["file"],
                                       "line": u["line"],
                                       "message": f"매크로 {u['name']}가 번역 단위마다 다르게 정의됨: '{old['body']}' / '{u['body']}' ({tu})"})
                elif old["file"] == u["file"] and not old["file"].endswith(".c"):
                    self.diags.append({"severity": "warning", "code": "header_variant_conflict", "file": u["file"],
                                       "line": u["line"],
                                       "message": f"헤더 정의 {key}가 번역 단위({tu})의 매크로 문맥에 따라 다르게 전개됨. 첫 정의를 사용"})
                else:
                    self.diags.append({"severity": "error", "code": "multiple_definition", "file": u["file"],
                                       "line": u["line"], "message": f"{key}가 여러 번역 단위에서 다르게 정의됨({old['file']}, {u['file']})"})
        for d in res["fn_decls"]:
            self.fn_decls.setdefault(d["key"], []).append(d)
        for d in res["global_decls"]:
            self.global_decls.setdefault(d["key"], []).append(d)
        for rel, info in res["files"].items():
            cur = self.files.setdefault(rel, {"includes": [], "is_header": info["is_header"], "tus": [],
                                              "inactive": [], "cond_macros": [], "hash": info.get("hash")})
            for inc in info["includes"]:
                if inc not in cur["includes"]:
                    cur["includes"].append(inc)
            if tu not in cur["tus"]:
                cur["tus"].append(tu)
            for r in info.get("inactive", []):
                if list(r) not in cur["inactive"]:
                    cur["inactive"].append(list(r))
            cur["cond_macros"] = sorted(set(cur["cond_macros"]) | set(info.get("cond_macros", [])))
        self.macro_uses.extend(res["macro_uses"])
        self.diags.extend(res["diags"])
        for k, v in res["ref_functions"].items():
            self.ref_functions.setdefault(k, v)
        for k, v in res["ref_globals"].items():
            self.ref_globals.setdefault(k, v)

    def symbol_integration(self) -> dict:
        """링커처럼 번역 단위 간 심벌을 통합하고 불일치를 보고한다.

        외부 링크 심벌은 이름으로 합치고(fn:<name>, gv:<name>), 내부 링크(static)는 파일별로 분리한다.
        USR 대신 '파일 경로+이름' 키를 쓰므로 static 심벌은 파일을 옮기면 키가 바뀐다(변경 분석에서
        파일 이동 추정으로 다시 대응시킨다).
        """
        mism = []
        for key, decls in self.fn_decls.items():
            u = self.units.get(key)
            for d in decls:
                if u and d.get("sig_cn") and u.get("sig_cn") and d["sig_cn"] != u["sig_cn"]:
                    mism.append({"symbol": key, "kind": "function", "declared": d["sig_cn"], "defined": u["sig_cn"],
                                 "decl_at": f"{d['file']}:{d['line']}", "def_at": f"{u['file']}:{u['line']}"})
        for key, decls in self.global_decls.items():
            u = self.units.get(key)
            for d in decls:
                a = (d.get("ty") or {}).get("cn")
                b = ((u or {}).get("ty") or {}).get("cn")
                if u and a and b and a.replace("volatile ", "") != b.replace("volatile ", ""):
                    mism.append({"symbol": key, "kind": "variable", "declared": a, "defined": b,
                                 "decl_at": f"{d['file']}:{d['line']}", "def_at": f"{u['file']}:{u['line']}"})
                elif u and a and b and a != b:
                    mism.append({"symbol": key, "kind": "qualifier", "declared": a, "defined": b,
                                 "decl_at": f"{d['file']}:{d['line']}", "def_at": f"{u['file']}:{u['line']}"})
        statics: Dict[str, List[str]] = {}
        for key, u in self.units.items():
            if u["kind"] in ("function", "global") and key.count(":") > 1:
                statics.setdefault(f"{u['kind']}:{u['name']}", []).append(u["file"])
        dup_static = [{"name": k.split(":", 1)[1], "kind": k.split(":", 1)[0], "files": sorted(v)}
                      for k, v in sorted(statics.items()) if len(v) > 1]
        defined = {k for k, u in self.units.items() if u["kind"] in ("function", "global")}
        externs = {k for k in defined if k.count(":") == 1}
        undefined = sorted(k for k in set(self.ref_functions) | set(self.ref_globals) if k not in defined)
        return {"extern_symbols": len(externs), "static_symbols": len(defined) - len(externs),
                "type_mismatches": mism, "same_name_statics": dup_static, "undefined_referenced": undefined,
                "multiple_definitions": [d["message"] for d in self.diags if d.get("code") == "multiple_definition"]}

    def source_lines(self, rel: str) -> List[str]:
        if rel not in self._src_cache:
            try:
                with open(os.path.join(self.root, rel), "r", encoding="utf-8", errors="replace") as f:
                    self._src_cache[rel] = f.read().split("\n")
            except OSError:
                self._src_cache[rel] = []
        return self._src_cache[rel]

    def snippet(self, rel: Optional[str], line: int, col: int = 1, end_line: Optional[int] = None,
                end_col: Optional[int] = None, limit: int = 160) -> str:
        if not rel:
            return ""
        lines = self.source_lines(rel)
        if not lines or line <= 0 or line > len(lines):
            return ""
        el = end_line or line
        if el - line > 6:
            el = line
            end_col = None
        if el == line:
            text = lines[line - 1][col - 1:(end_col - 1) if end_col else None]
        else:
            parts = [lines[line - 1][col - 1:]] + lines[line:el - 1] + [lines[el - 1][:(end_col - 1) if end_col else None]]
            text = " ".join(p.strip() for p in parts)
        text = re.sub(r"\s+", " ", text).strip()
        return text[:limit] + ("…" if len(text) > limit else "")

    def summary(self) -> dict:
        kinds: Dict[str, int] = {}
        for u in self.units.values():
            kinds[u["kind"]] = kinds.get(u["kind"], 0) + 1
        defined_fns = {k for k, u in self.units.items() if u["kind"] == "function"}
        defined_gvs = {k for k, u in self.units.items() if u["kind"] == "global"}
        ext_fns = sorted(k for k in self.ref_functions if k not in defined_fns)
        unresolved_gv = sorted(k for k in set(self.ref_globals) | set(self.global_decls) if k not in defined_gvs)
        return {
            "version": self.version, "root": self.root,
            "translation_units": len(self.tus),
            "parse_failed_tus": [t["file"] for t in self.tus if t.get("errors")],
            "units": kinds,
            "files": len(self.files),
            "headers": sorted(r for r, f in self.files.items() if f.get("is_header")),
            "external_functions": ext_fns,
            "parse_health": self.parse_health,
            "symbol_integration": self.symbol_integration(),
            "unresolved_globals": unresolved_gv,
            "notes": self.notes,
        }


class ProjectIndexer:
    def __init__(self, version: VersionSpec, build: BuildConfig, cache_dir: Optional[str] = None,
                 log=print):
        self.v = version
        self.build = build
        self.cache_dir = cache_dir
        self.log = log
        ca.init_libclang()

    def _args_for(self, e: CompileEntry) -> List[str]:
        from .build_env import clang_args_for
        compat_dir = self.cache_dir or os.path.join(tempfile.gettempdir(), "vfia_compat")
        base, notes = clang_args_for(e.profile, e.args, self.build, compat_dir)
        self._env_notes = sorted(set(getattr(self, "_env_notes", [])) | set(notes))
        args = base + list(self.build.extra_args)
        if self.build.parse_all_comments:
            args.append("-fparse-all-comments")
        res = None
        if self.build.clang_resource_dir == "auto":
            res = ca.detect_resource_include()
        elif self.build.clang_resource_dir:
            res = os.path.join(self.build.clang_resource_dir, "include")
        if res:
            args += ["-isystem", res]
        if not any(a.startswith("-std") for a in args):
            args.append("-std=c99")
        return args

    def _cache_path(self, e: CompileEntry, args: List[str]) -> Optional[str]:
        if not self.cache_dir:
            return None
        h = _sha(json.dumps([os.path.relpath(e.file, self.v.root), args, ca.__name__, 5]).encode())
        return os.path.join(self.cache_dir, f"tu_{h}.json")

    def _cache_valid(self, path: str) -> Optional[dict]:
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return None
        for rel, h in data.get("deps", {}).items():
            if file_hash(os.path.join(self.v.root, rel)) != h:
                return None
        return data

    def run(self) -> ProjectIndex:
        entries, notes = load_compile_entries(self.v, self.build)
        idx = ProjectIndex(self.v.name, os.path.abspath(self.v.root), self.build)
        idx.notes.extend(notes)
        index = ci.Index.create()
        if self.cache_dir:
            os.makedirs(self.cache_dir, exist_ok=True)
        reused = 0
        for e in entries:
            args = self._args_for(e)
            cpath = self._cache_path(e, args)
            data = self._cache_valid(cpath) if cpath else None
            if data is not None:
                reused += 1
                res = data["result"]
            else:
                opts = (ci.TranslationUnit.PARSE_DETAILED_PROCESSING_RECORD
                        | ci.TranslationUnit.PARSE_SKIP_FUNCTION_BODIES * 0)
                try:
                    tu = index.parse(e.file, args=args, options=opts)
                except ci.TranslationUnitLoadError as ex:
                    idx.tus.append({"file": os.path.relpath(e.file, self.v.root), "args": args, "origin": e.origin,
                                    "errors": [f"파싱 불가: {ex}"]})
                    continue
                res = TUExtractor(tu, self.v.root, e).run()
                if cpath:
                    deps = {rel: info.get("hash") for rel, info in res["files"].items()}
                    with open(cpath, "w", encoding="utf-8") as f:
                        json.dump({"deps": deps, "result": res}, f, ensure_ascii=False)
            errs = [d["message"] for d in res["diags"] if d["severity"] == "error"]
            idx.tus.append({"file": res["tu"], "args": args, "origin": e.origin, "errors": errs,
                            "fatal": any(d.get("fatal") for d in res["diags"]),
                            "warnings": sum(1 for d in res["diags"] if d["severity"] == "warning"),
                            "compiler_profile": e.profile, "dropped_options": e.dropped,
                            "original_command": e.original[:60]})
            idx.merge(res)
        if self.cache_dir:
            idx.notes.append(f"번역 단위 캐시 재사용 {reused}/{len(entries)} (포함 헤더 해시 기준 무효화)")
        idx.notes.extend(getattr(self, "_env_notes", []))
        idx.parse_health = self._parse_health(idx)
        return idx

    def _parse_health(self, idx: ProjectIndex) -> dict:
        """단계 2 완료 기준: 번역 단위별 오류 수, 파싱 성공률, 조용히 빠졌을 수 있는 함수.

        libclang은 오류가 있어도 일부 AST를 돌려주므로, 소스 텍스트에서 찾은 함수 정의와
        AST에서 찾은 함수 정의를 비교해 누락 의심 함수를 보고한다.
        """
        from .build_env import function_definitions_in_text
        total = len(idx.tus)
        ok = sum(1 for t in idx.tus if not t.get("errors"))
        in_ast: Dict[str, set] = {}
        for key, u in idx.units.items():
            if u["kind"] == "function":
                in_ast.setdefault(u["file"], set()).add(u["name"])
        missing = []
        for t in idx.tus:
            rel = t["file"]
            try:
                with open(os.path.join(self.v.root, rel), "r", encoding="utf-8", errors="replace") as f:
                    text = f.read()
            except OSError:
                continue
            inactive = [tuple(r) for r in (idx.files.get(rel, {}).get("inactive") or [])]
            found = function_definitions_in_text(text, inactive)
            for name, line in sorted(found.items(), key=lambda x: x[1]):
                if name not in in_ast.get(rel, set()):
                    missing.append({"file": rel, "function": name, "line": line})
        # 오류 위치가 함수 본문 안에 있으면 그 함수의 AST는 부분적일 수 있음(RecoveryExpr 등)
        partial = []
        fn_units = [u for u in idx.units.values() if u["kind"] == "function"]
        for d in idx.diags:
            if d.get("severity") != "error" or not d.get("file"):
                continue
            for u in fn_units:
                if u["file"] == d["file"] and u["line"] <= (d.get("line") or 0) <= (u.get("end_line") or u["line"]):
                    partial.append({"function": u["name"], "file": u["file"], "line": d.get("line"),
                                    "error": d["message"][:160]})
        rate = (ok / total) if total else 0.0
        return {
            "partial_ast_functions": partial,
            "translation_units": total, "parsed_without_error": ok, "success_rate": round(rate, 3),
            "errors_by_tu": {t["file"]: len(t.get("errors") or []) for t in idx.tus if t.get("errors")},
            "fatal_tus": [t["file"] for t in idx.tus if t.get("fatal")],
            "warnings_total": sum(t.get("warnings", 0) for t in idx.tus),
            "missing_function_candidates": missing,
            "dropped_options": sorted({o for t in idx.tus for o in (t.get("dropped_options") or [])})[:50],
            "compiler_profiles": sorted({t.get("compiler_profile") or "?" for t in idx.tus}),
        }
