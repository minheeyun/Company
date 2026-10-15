"""compile_commands.json 처리와 파일별 컴파일 인자 결정.

변경 전/후 트리가 서로 다른 디렉터리에 있어도 같은 컴파일 DB 를 쓸 수 있도록
DB 루트(--compdb-root, 기본: 프로젝트 루트) 기준 상대경로로 정규화해 두고,
분석할 트리의 루트로 다시 치환한다.
"""

from __future__ import annotations

import json
import os
import shlex
from pathlib import Path

ROOT_MARK = "{ROOT}"

# 분석에 의미 있는 옵션만 남긴다
_KEEP_WITH_ARG = {"-I", "-D", "-U", "-include", "-isystem", "-iquote", "-idirafter", "--target", "-target",
                  "-std", "-imacros"}
_KEEP_PREFIX = ("-I", "-D", "-U", "-std=", "--target=", "-isystem", "-iquote", "-m", "-f", "-W")
_DROP_PREFIX = ("-W", "-fdiagnostics", "-fcolor", "-MF", "-MT", "-MQ")


class CompileDB:
    def __init__(self, path: str | Path, db_root: str | Path | None = None):
        self.path = Path(path).resolve()
        self.db_root = os.path.realpath(str(db_root)) if db_root else None
        self.entries: dict[str, list[str]] = {}
        data = json.loads(self.path.read_text(encoding="utf-8"))
        for ent in data:
            d = ent.get("directory", ".")
            if not os.path.isabs(d):  # 확장: DB 파일 기준 상대 디렉터리 허용
                d = str((self.path.parent / d).resolve())
            f = ent["file"]
            if not os.path.isabs(f):
                f = os.path.join(d, f)
            f = os.path.realpath(f)
            args = ent.get("arguments") or shlex.split(ent.get("command", ""))
            self.entries[self._norm(f)] = self._filter(args[1:], d, f)

    def _norm(self, p: str) -> str:
        if self.db_root and (p == self.db_root or p.startswith(self.db_root + os.sep)):
            return ROOT_MARK + "/" + os.path.relpath(p, self.db_root).replace(os.sep, "/")
        return p

    def _filter(self, args, directory, src):
        out = []
        i = 0
        while i < len(args):
            a = args[i]
            if a in ("-o", "-MF", "-MT", "-MQ"):
                i += 2
                continue
            if a == "-c" or os.path.realpath(os.path.join(directory, a)) == src:
                i += 1
                continue
            if a in _KEEP_WITH_ARG and i + 1 < len(args):
                val = args[i + 1]
                if a in ("-I", "-isystem", "-iquote", "-idirafter", "-include", "-imacros"):
                    val = self._norm(os.path.realpath(os.path.join(directory, val)))
                out += [a, val]
                i += 2
                continue
            if a.startswith(_DROP_PREFIX):
                i += 1
                continue
            if a.startswith("-I") and len(a) > 2:
                out.append("-I" + self._norm(os.path.realpath(os.path.join(directory, a[2:]))))
            elif a.startswith(_KEEP_PREFIX):
                out.append(a)
            i += 1
        return out

    def args_for(self, abs_file: str, tree_root: str) -> list[str] | None:
        """분석 대상 트리의 파일에 대한 인자. DB 에 없으면 None."""
        tree_root = os.path.realpath(tree_root)
        rel = os.path.relpath(os.path.realpath(abs_file), tree_root).replace(os.sep, "/")
        key = ROOT_MARK + "/" + rel
        args = self.entries.get(key) or self.entries.get(os.path.realpath(abs_file))
        if args is None:
            return None
        return [a.replace(ROOT_MARK, tree_root) for a in args]


def default_args(tree_root: str, include_dirs=(), defines=(), std="c99", target=None, extra=()) -> list[str]:
    """컴파일 DB 가 없을 때의 제한 모드 인자: 지정 include + 프로젝트 내 헤더 디렉터리 전체."""
    args = [f"-std={std}"]
    if target:
        args.append(f"--target={target}")
    for d in include_dirs:
        args.append("-I" + (d if os.path.isabs(d) else os.path.join(tree_root, d)))
    seen = set()
    for dp, dn, fn in os.walk(tree_root):
        dn[:] = [x for x in dn if not x.startswith(".") and x not in ("build", "out")]
        if any(f.endswith(".h") for f in fn) and dp not in seen:
            seen.add(dp)
            args.append("-I" + dp)
    for d in defines:
        args.append("-D" + d)
    args.extend(extra)
    return args
