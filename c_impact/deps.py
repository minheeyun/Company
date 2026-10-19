"""함수 간 의존성 표: 프로젝트에서 어떤 함수가 어떤 변수를 읽고 쓰는가.

변경과 관계없이 한 버전 전체를 대상으로 한다. 읽기·쓰기는 포인터 분석을 거친 실제 메모리 위치
(구조체 필드 단위) 기준이므로, `p->counter = 0` 처럼 포인터로 쓰는 경우도 대상 전역 변수로 나타난다.
"""

from __future__ import annotations

import csv
import io
import re
from collections import defaultdict
from pathlib import Path

from .program import Program, display_path

ISR_PATTERN = re.compile(r"(isr|irq|interrupt|handler)", re.I)

VAR_KIND_KO = {"global": "전역", "static": "파일 static", "static_local": "함수 내 static", "hw": "하드웨어 주소",
               "sym": "미해결 포인터 대상"}


def _rel(prog: Program, path: str) -> str:
    try:
        return Path(path).resolve().relative_to(prog.root.resolve()).as_posix()
    except (ValueError, OSError):
        return path


def var_label(prog: Program, path: tuple) -> str | None:
    """보고용 변수 이름. 지역 변수·임시값이면 None."""
    v = prog.vars.get(path[0])
    if v is None or not v.persistent:
        return None
    name = display_path(prog, path)
    if v.kind == "static_local":
        name = f"{prog.func_display(v.func)}::{name}"
    return name


def func_labels(prog: Program) -> dict[str, str]:
    """함수 키 → 표시 이름 (static 함수 이름이 겹치면 파일을 붙인다)."""
    count = defaultdict(int)
    for k in prog.funcs:
        count[prog.func_display(k)] += 1
    return {k: (prog.func_display(k) if count[prog.func_display(k)] == 1 else f"{prog.func_display(k)} ({prog.funcs[k].tu})")
            for k in prog.funcs}


def dependency_table(prog: Program, entries: list[str] | None = None, isr: list[str] | None = None) -> dict:
    entries = entries or []
    isr = isr or []
    label = func_labels(prog)
    reads: dict[str, dict[str, set[int]]] = defaultdict(lambda: defaultdict(set))
    writes: dict[str, dict[str, set[int]]] = defaultdict(lambda: defaultdict(set))
    var_info: dict[str, dict] = {}
    for n in prog.nodes:
        if not n.func or n.dead or n.func not in prog.funcs:
            continue
        for acc, paths in ((writes, n.defs), (reads, n.uses)):
            for p in paths:
                name = var_label(prog, p)
                if name is None:
                    continue
                acc[n.func][name].add(n.line)
                if name not in var_info:
                    v = prog.vars[p[0]]
                    var_info[name] = {"kind": v.kind, "file": _rel(prog, v.file) if v.file else "", "line": v.line}

    calls: dict[str, set[str]] = defaultdict(set)
    indirect: dict[str, set[str]] = defaultdict(set)
    externs: dict[str, set[str]] = defaultdict(set)
    unresolved: dict[str, list[str]] = defaultdict(list)
    for s in prog.sites:
        if s.external:
            externs[s.caller].add(s.name)
        elif s.direct is None:
            if s.callees:
                indirect[s.caller] |= s.callees
            else:
                unresolved[s.caller].append(s.name)
        calls[s.caller] |= s.callees
    called_by: dict[str, set[str]] = defaultdict(set)
    for f, cs in calls.items():
        for c in cs:
            called_by[c].add(f)

    roots = sorted(set(prog.roots()) | {e for e in entries if e in prog.funcs})
    reach: dict[str, set[str]] = defaultdict(set)
    for r in roots:
        stack, seen = [r], set()
        while stack:
            f = stack.pop()
            if f in seen:
                continue
            seen.add(f)
            reach[f].add(r)
            stack.extend(calls.get(f, ()))

    def is_isr(r):
        return r in isr or bool(ISR_PATTERN.search(prog.func_display(r)))

    def closure(f, table):
        out, stack, seen = set(), [f], set()
        while stack:
            g = stack.pop()
            if g in seen:
                continue
            seen.add(g)
            out |= set(table.get(g, {}))
            stack.extend(calls.get(g, ()))
        return out

    functions = []
    for k in sorted(prog.funcs, key=lambda k: (prog.funcs[k].tu, prog.funcs[k].line)):
        f = prog.funcs[k]
        r_direct, w_direct = set(reads.get(k, {})), set(writes.get(k, {}))
        functions.append({
            "key": k,
            "func": label[k],
            "file": _rel(prog, f.file),
            "line": f.line,
            "reads": {v: sorted(reads[k][v]) for v in sorted(r_direct)},
            "writes": {v: sorted(writes[k][v]) for v in sorted(w_direct)},
            "reads_via_calls": sorted(closure(k, reads) - r_direct),
            "writes_via_calls": sorted(closure(k, writes) - w_direct),
            "calls": sorted(label[c] for c in calls.get(k, ())),
            "calls_via_pointer": sorted(label[c] for c in indirect.get(k, ())),
            "unresolved_calls": sorted(set(unresolved.get(k, []))),
            "extern_calls": sorted(externs.get(k, ())),
            "called_by": sorted(label[c] for c in called_by.get(k, ())),
            "entries": sorted(label[r] for r in reach.get(k, ())),
            "is_entry": k in roots,
            "is_isr": k in roots and is_isr(k),
        })

    variables = []
    for name in sorted(var_info):
        writers = {label[f]: sorted(writes[f][name]) for f in sorted(writes) if name in writes[f]}
        readers = {label[f]: sorted(reads[f][name]) for f in sorted(reads) if name in reads[f]}
        keys = [f for f in prog.funcs if name in writes.get(f, {}) or name in reads.get(f, {})]
        ents = sorted({r for f in keys for r in reach.get(f, ())})
        info = var_info[name]
        variables.append({
            "var": name,
            "kind": info["kind"],
            "file": info["file"],
            "line": info["line"],
            "writers": writers,
            "readers": readers,
            "entries": [label[r] for r in ents],
            "isr_shared": len(ents) > 1 and any(is_isr(r) for r in ents),
            "write_only": bool(writers) and not readers,
            "read_only": bool(readers) and not writers,
        })
    return {
        "root": str(prog.root),
        "files": [t.rel for t in prog.tus],
        "parse_errors": prog.parse_errors,
        "functions": functions,
        "variables": variables,
        "extern_functions": sorted(prog.extern_funcs),
        "unresolved_pointers": sorted(prog.unresolved_ptrs),
    }


# ---------------------------------------------------------------------------
# 출력
# ---------------------------------------------------------------------------


def _c(s) -> str:
    return str(s).replace("|", "\\|")


def _names(d, limit=12) -> str:
    items = list(d)
    s = ", ".join(f"`{_c(x)}`" for x in items[:limit])
    return s + (f" 외 {len(items) - limit}개" if len(items) > limit else "") if items else "-"


def render_md(t: dict, transitive: bool = False) -> str:
    out = ["# 함수·변수 의존성", "",
           f"- 대상: `{t['root']}` (파일 {len(t['files'])}개, 함수 {len(t['functions'])}개, 상태 변수 {len(t['variables'])}개)",
           "- 상태 변수 = 전역·파일 static·함수 내 static·하드웨어 주소. 읽기·쓰기는 포인터를 거친 실제 대상(구조체 필드 단위)입니다.",
           ""]
    if t["parse_errors"]:
        out.append(f"- ⚠ 파싱 실패로 빠진 파일: {', '.join(t['parse_errors'])}")
        out.append("")
    out += ["## 1. 함수별 읽기·쓰기", ""]
    head = "| 함수 | 위치 | 읽는 변수 | 쓰는 변수 |"
    sep = "|---|---|---|---|"
    if transitive:
        head += " 호출을 통해 추가로 읽음 | 호출을 통해 추가로 씀 |"
        sep += "---|---|"
    head += " 호출 | 호출됨 | 진입점 |"
    sep += "---|---|---|"
    out += [head, sep]
    for f in t["functions"]:
        calls = list(f["calls"])
        calls = [c + (" (포인터)" if c in f["calls_via_pointer"] else "") for c in calls]
        calls += [e + " (외부)" for e in f["extern_calls"]] + [u + " (미해결)" for u in f["unresolved_calls"]]
        name = f"`{_c(f['func'])}`" + (" 🔹ISR" if f["is_isr"] else (" 🔸진입점" if f["is_entry"] else ""))
        row = f"| {name} | {f['file']}:{f['line']} | {_names(f['reads'])} | {_names(f['writes'])} |"
        if transitive:
            row += f" {_names(f['reads_via_calls'])} | {_names(f['writes_via_calls'])} |"
        row += f" {_names(calls)} | {_names(f['called_by'])} | {_names(f['entries'])} |"
        out.append(row)
    out += ["", "## 2. 변수별 접근 함수", "",
            "| 변수 | 종류 | 선언 | 쓰는 함수 (줄) | 읽는 함수 (줄) | 접근 진입점 | 비고 |", "|---|---|---|---|---|---|---|"]
    for v in t["variables"]:
        def acc(d):
            return ", ".join(f"`{_c(f)}`({','.join(map(str, ls[:4]))}{'…' if len(ls) > 4 else ''})" for f, ls in d.items()) or "-"
        notes = []
        if v["isr_shared"]:
            notes.append("ISR·태스크 공유")
        elif len(v["entries"]) > 1:
            notes.append("여러 진입점 공유")
        if v["write_only"]:
            notes.append("쓰기만 함")
        if v["read_only"]:
            notes.append("읽기만 함(초기값/외부 설정)")
        decl = f"{v['file']}:{v['line']}" if v["file"] else "-"
        out.append(f"| `{_c(v['var'])}` | {VAR_KIND_KO.get(v['kind'], v['kind'])} | {decl} | {acc(v['writers'])} | "
                   f"{acc(v['readers'])} | {_names(v['entries'])} | {', '.join(notes)} |")
    if t["extern_functions"] or t["unresolved_pointers"]:
        out += ["", "## 3. 분석 범위 밖", ""]
        if t["extern_functions"]:
            out.append(f"- 본문 없는 외부 함수 (포인터 인자 대상을 읽고 쓴다고 가정): {_names(t['extern_functions'], 50)}")
        if t["unresolved_pointers"]:
            out.append(f"- 대상을 모르는 포인터: {_names(t['unresolved_pointers'], 50)}")
    out.append("")
    return "\n".join(out)


def render_csv(t: dict) -> str:
    """피벗용 tidy 형식: 함수, 위치, 변수, 접근(read/write), 경로(direct/via_call), 줄."""
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["function", "file", "func_line", "variable", "access", "via", "lines"])
    for f in t["functions"]:
        for acc, key, via_key in (("read", "reads", "reads_via_calls"), ("write", "writes", "writes_via_calls")):
            for v, lines in f[key].items():
                w.writerow([f["func"], f["file"], f["line"], v, acc, "direct", " ".join(map(str, lines))])
            for v in f[via_key]:
                w.writerow([f["func"], f["file"], f["line"], v, acc, "via_call", ""])
    return buf.getvalue()


def query(t: dict, func: str | None = None, var: str | None = None) -> str:
    out = []
    if func:
        hits = [f for f in t["functions"] if f["func"] == func or f["func"].startswith(func + " (")]
        if not hits:
            return f"함수 '{func}' 를 찾지 못했습니다."
        for f in hits:
            out += [f"[함수] {f['func']}  ({f['file']}:{f['line']})",
                    f"  읽기: {_plain(f['reads'])}",
                    f"  쓰기: {_plain(f['writes'])}",
                    f"  호출을 통해 추가로 읽음: {', '.join(f['reads_via_calls']) or '-'}",
                    f"  호출을 통해 추가로 씀: {', '.join(f['writes_via_calls']) or '-'}",
                    f"  호출: {', '.join(f['calls'] + [e + '(외부)' for e in f['extern_calls']]) or '-'}",
                    f"  호출됨: {', '.join(f['called_by']) or '-'}",
                    f"  진입점: {', '.join(f['entries']) or '-'}"]
    if var:
        hits = [v for v in t["variables"] if v["var"] == var or v["var"].startswith(var + ".")
                or v["var"].startswith(var + "[") or v["var"].endswith("::" + var)]
        if not hits:
            return "\n".join(out + [f"변수 '{var}' 를 찾지 못했습니다."])
        for v in hits:
            out += [f"[변수] {v['var']}  ({VAR_KIND_KO.get(v['kind'], v['kind'])}, {v['file']}:{v['line']})",
                    f"  쓰는 함수: {_plain(v['writers'])}",
                    f"  읽는 함수: {_plain(v['readers'])}",
                    f"  접근 진입점: {', '.join(v['entries']) or '-'}" + ("  ※ ISR·태스크 공유" if v["isr_shared"] else "")]
    return "\n".join(out)


def _plain(d: dict) -> str:
    return ", ".join(f"{k}({','.join(map(str, v))})" for k, v in d.items()) or "-"
