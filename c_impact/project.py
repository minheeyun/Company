"""프로젝트 단위 파싱, 캐싱, 심볼 테이블 병합."""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import __version__
from . import clang_frontend as cf
from .ast_extract import FileExtractor, ProjectPaths, iter_stmts, kbase
from .compdb import CompileDB, default_args

SOURCE_EXT = (".c",)
HEADER_EXT = (".h",)


def file_sha(path: str) -> str:
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def discover(root: str, exclude_globs=()) -> list[str]:
    out = []
    for dp, dn, fn in os.walk(root):
        dn[:] = sorted(x for x in dn if not x.startswith("."))
        for f in sorted(fn):
            if f.endswith(SOURCE_EXT + HEADER_EXT):
                p = os.path.join(dp, f)
                rel = os.path.relpath(p, root).replace(os.sep, "/")
                if any(fnmatch.fnmatch(rel, g) or fnmatch.fnmatch("/" + rel, g) for g in exclude_globs):
                    continue
                out.append(p)
    return out


class ParseCache:
    """변경되지 않은 파일의 추출 결과 캐시. 키 = (도구 버전, 상대경로, 인자, 파일 해시),
    저장 시 포함 헤더 해시도 함께 기록해 헤더가 바뀌면 무효화한다."""

    def __init__(self, cache_dir: str | None):
        self.dir = Path(cache_dir) if cache_dir else None
        if self.dir:
            self.dir.mkdir(parents=True, exist_ok=True)
        self.hits = 0
        self.misses = 0

    def _key(self, rel, args, sha, tree_root):
        norm_args = [a.replace(os.path.realpath(tree_root), "{ROOT}") for a in args]
        raw = json.dumps([__version__, rel, norm_args, sha])
        return hashlib.sha1(raw.encode()).hexdigest()

    def get(self, rel, args, sha, tree_root):
        if not self.dir:
            return None
        p = self.dir / (self._key(rel, args, sha, tree_root) + ".json")
        if not p.exists():
            self.misses += 1
            return None
        try:
            variants = json.loads(p.read_text(encoding="utf-8")).get("variants", [])
        except (OSError, ValueError, AttributeError):
            self.misses += 1
            return None
        # 같은 .c 라도 포함 헤더 내용이 다르면(변경 전/후 트리) 다른 변형으로 저장되어 있다
        for data in variants:
            ok = True
            for inc, h in data.get("_include_hashes", {}).items():
                ap = os.path.join(tree_root, inc)
                if not os.path.exists(ap) or file_sha(ap) != h:
                    ok = False
                    break
            if ok:
                self.hits += 1
                return data
        self.misses += 1
        return None

    def put(self, rel, args, sha, tree_root, fm):
        if not self.dir:
            return
        fm = dict(fm)
        fm["_include_hashes"] = {inc: file_sha(os.path.join(tree_root, inc)) for inc in fm.get("includes", [])
                                 if os.path.exists(os.path.join(tree_root, inc))}
        p = self.dir / (self._key(rel, args, sha, tree_root) + ".json")
        try:
            variants = json.loads(p.read_text(encoding="utf-8")).get("variants", []) if p.exists() else []
        except (OSError, ValueError, AttributeError):
            variants = []
        variants = [v for v in variants if v.get("_include_hashes") != fm["_include_hashes"]][-3:] + [fm]
        p.write_text(json.dumps({"variants": variants}), encoding="utf-8")


class Project:
    """한 버전(Baseline 또는 Current)의 프로젝트 모델."""

    def __init__(self, root: str, label: str):
        self.root = os.path.realpath(root)
        self.label = label
        self.files: dict[str, dict] = {}
        self.functions: dict[str, dict] = {}
        self.globals: dict[str, dict] = {}
        self.extern_vars: dict[str, dict] = {}
        self.func_decls: dict[str, dict] = {}
        self.macros: dict[str, dict] = {}
        self.typedefs: dict[str, dict] = {}
        self.records: dict[str, dict] = {}
        self.enums: dict[str, dict] = {}
        self.headers: list[str] = []
        self.unparsed_headers: list[str] = []
        self.failures: list[dict] = []
        self.mode = "limited"
        self.global_fp_bindings: list[dict] = []
        self.global_addr_taken: set[str] = set()
        self.skipped: list[list] = []
        self.stats: dict = {}

    # ---------------------------------------------------------- 조회 보조
    def stmt_index(self, fid: str) -> dict[int, dict]:
        cache = getattr(self, "_sidx", None)
        if cache is None:
            cache = self._sidx = {}
        if fid not in cache:
            fn = self.functions[fid]
            cache[fid] = {st["id"]: st for st in iter_stmts(fn["body"])}
        return cache[fid]

    def var_info(self, key: str, fid: str | None = None) -> dict | None:
        """변수 키 → 자료형 등 정보."""
        base = kbase(key)
        if base.startswith("G:"):
            gid = base[2:]
            return self.globals.get(gid) or self.extern_vars.get(gid)
        if fid and fid in self.functions:
            fn = self.functions[fid]
            if base.startswith("L:"):
                loc = fn["locals"].get(base)
                return {"name": loc["name"], "type": loc["type"]} if loc else None
            if base.startswith("P:"):
                for p in fn["params"]:
                    if p["key"] == base:
                        return p
        return None

    def to_summary(self) -> dict:
        return {"root": self.root, "label": self.label, "mode": self.mode, "files": len(self.files),
                "functions": len(self.functions), "globals": len(self.globals), "macros": len(self.macros),
                "headers": len(self.headers), "unparsed_headers": self.unparsed_headers,
                "failures": self.failures, "stats": self.stats}


def load_project(root: str, label: str, *, compdb: CompileDB | None = None, include_dirs=(), defines=(),
                 std="c99", target=None, extra_args=(), cache: ParseCache | None = None,
                 external_globs=(), exclude_globs=(), jobs: int = 1, only_files: set[str] | None = None) -> Project:
    """프로젝트 루트의 모든 .c 를 파싱해 모델을 만든다(원본 파일은 읽기만 한다)."""
    ci = cf.require_clang()
    t0 = time.time()
    proj = Project(root, label)
    paths = ProjectPaths(root, external_globs, exclude_globs)
    all_files = discover(proj.root, exclude_globs)
    sources = [f for f in all_files if f.endswith(SOURCE_EXT) and paths.is_project(f)]
    proj.headers = [paths.rel(f) for f in all_files if f.endswith(HEADER_EXT)]
    proj.mode = "compdb" if compdb else "limited"
    fallback = default_args(proj.root, include_dirs, defines, std, target, extra_args)
    rdir = cf.resource_dir()
    h0, m0 = (cache.hits, cache.misses) if cache else (0, 0)

    def work(path):
        rel = paths.rel(path)
        args = None
        mode = "limited"
        if compdb:
            args = compdb.args_for(path, proj.root)
            if args is not None:
                mode = "compdb"
                args = args + [f"-D{d}" for d in defines] + list(extra_args)
        if args is None:
            args = list(fallback)
        sha = file_sha(path)
        fm = cache.get(rel, args, sha, proj.root) if cache else None
        if fm is not None:
            fm["_cached"] = True
            return fm
        full = list(args) + (["-resource-dir", rdir] if rdir else [])
        try:
            tu = ci.Index.create().parse(path, args=full, options=(
                ci.TranslationUnit.PARSE_DETAILED_PROCESSING_RECORD | ci.TranslationUnit.PARSE_INCOMPLETE))
        except ci.TranslationUnitLoadError as e:
            return {"path": rel, "parse_ok": False, "fatal": str(e), "args": args, "mode": mode,
                    "diagnostics": [{"severity": "fatal", "file": rel, "line": 0, "message": str(e)}]}
        fm = FileExtractor(paths).extract(tu, path, args, mode)
        if cache:
            cache.put(rel, args, sha, proj.root, fm)
        return fm

    if jobs > 1:
        with ThreadPoolExecutor(max_workers=jobs) as ex:
            results = list(ex.map(work, sources))
    else:
        results = [work(p) for p in sources]

    modes = set()
    included = set()
    for fm in results:
        proj.files[fm["path"]] = fm
        modes.add(fm.get("mode", "limited"))
        if not fm.get("parse_ok", False):
            proj.failures.append({"file": fm["path"], "diagnostics": fm.get("diagnostics", [])[:10]})
        if "functions" not in fm:
            continue
        included.update(fm.get("includes", []))
        for fid, fn in fm["functions"].items():
            fn["parse_ok"] = fm["parse_ok"]
            proj.functions.setdefault(fid, fn)
        for gid, g in fm["globals"].items():
            if gid not in proj.globals or g.get("init") is not None:
                proj.globals[gid] = g
        proj.extern_vars.update(fm["extern_vars"])
        for fid, d in fm["func_decls"].items():
            prev = proj.func_decls.get(fid)
            if prev is None or d.get("defined"):
                proj.func_decls[fid] = d
        proj.macros.update(fm["macros"])
        proj.typedefs.update(fm["typedefs"])
        proj.records.update(fm["records"])
        proj.enums.update(fm["enums"])
        proj.global_fp_bindings.extend(fm.get("global_fp_bindings", []))
        proj.global_addr_taken.update(fm.get("global_addr_taken", []))
        proj.skipped.extend(fm.get("skipped", []))
    for gid in list(proj.extern_vars):
        if gid in proj.globals:
            proj.extern_vars.pop(gid)
    proj.unparsed_headers = sorted(h for h in proj.headers if h not in included)
    if modes == {"compdb"}:
        proj.mode = "compdb"
    elif "compdb" in modes:
        proj.mode = "mixed"
    else:
        proj.mode = "limited"
    _link(proj)
    proj.stats = {"parse_seconds": round(time.time() - t0, 3), "sources": len(sources),
                  "cache_hits": cache.hits - h0 if cache else 0, "cache_misses": cache.misses - m0 if cache else 0}
    return proj


def _link(proj: Project):
    """번역 단위 간 심볼 연결: 호출 대상 함수의 정의 존재 여부, 외부 함수 표시."""
    for fn in proj.functions.values():
        for call in fn["calls"]:
            cid = call.get("callee")
            if cid is None:
                continue
            if cid in proj.functions:
                call["external"] = False
                call["defined"] = True
            else:
                d = proj.func_decls.get(cid, {})
                call["defined"] = False
                call["external"] = True
                call["system"] = d.get("system", False)
