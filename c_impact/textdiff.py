"""텍스트 수준 diff (git diff 와 동일한 unified hunk) 및 주석/공백 정규화."""

from __future__ import annotations

import difflib
import os
import re

from .project import discover, file_sha


def strip_comments(text: str) -> str:
    """C 주석 제거(문자열 리터럴 내부 보존)."""
    out = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c in "\"'":
            q = c
            j = i + 1
            while j < n and text[j] != q:
                j += 2 if text[j] == "\\" else 1
            out.append(text[i:j + 1])
            i = j + 1
        elif text.startswith("/*", i):
            j = text.find("*/", i + 2)
            seg = text[i:(j + 2 if j >= 0 else n)]
            out.append("\n" * seg.count("\n") or " ")
            i = j + 2 if j >= 0 else n
        elif text.startswith("//", i):
            j = text.find("\n", i)
            i = j if j >= 0 else n
        else:
            out.append(c)
            i += 1
    return "".join(out)


def squash(text: str) -> str:
    return re.sub(r"\s+", "", strip_comments(text))


def read_lines(path: str) -> list[str]:
    with open(path, encoding="utf-8", errors="replace") as f:
        return f.read().splitlines()


def file_pairs(base_root: str, curr_root: str, exclude_globs=()) -> list[dict]:
    b = {os.path.relpath(p, base_root).replace(os.sep, "/"): p for p in discover(base_root, exclude_globs)}
    c = {os.path.relpath(p, curr_root).replace(os.sep, "/"): p for p in discover(curr_root, exclude_globs)}
    out = []
    for rel in sorted(set(b) | set(c)):
        bp, cp = b.get(rel), c.get(rel)
        if bp and cp:
            status = "unchanged" if file_sha(bp) == file_sha(cp) else "modified"
        else:
            status = "added" if cp else "removed"
        out.append({"file": rel, "status": status, "base": bp, "curr": cp})
    return out


def hunks(base_path: str | None, curr_path: str | None, context: int = 0) -> list[dict]:
    a = read_lines(base_path) if base_path else []
    b = read_lines(curr_path) if curr_path else []
    out = []
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    for group in sm.get_grouped_opcodes(context):
        for tag, i1, i2, j1, j2 in group:
            if tag == "equal":
                continue
            old = a[i1:i2]
            new = b[j1:j2]
            out.append({
                "tag": tag, "base_start": i1 + 1, "base_end": i2, "curr_start": j1 + 1, "curr_end": j2,
                "old": old, "new": new,
                "format_only": squash("\n".join(old)) == squash("\n".join(new)),
            })
    return out


def unified(base_path: str | None, curr_path: str | None, rel: str, context: int = 3) -> str:
    a = read_lines(base_path) if base_path else []
    b = read_lines(curr_path) if curr_path else []
    return "\n".join(difflib.unified_diff(a, b, f"a/{rel}", f"b/{rel}", n=context, lineterm=""))
