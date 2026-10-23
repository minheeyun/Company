"""분석 실행과 결과 정리: 변경 지점 → 전방 슬라이스 → 함수·변수별 영향 항목·경로·조건."""

from __future__ import annotations

import re
from collections import defaultdict
from pathlib import Path

from . import __version__
from .diff import ChangeSet, compute_changes, predicate, boundary_values
from .frontend import BuildConfig, SourceLines, discover_sources, load_version
from .program import Program, display_path, load_extern_model, overlaps
from .sil import _typename
from .slicer import Hit, forward_slice, path_to

ISR_PATTERN = re.compile(r"(isr|irq|interrupt|handler)", re.I)

ASSUMPTIONS = [
    "전역·static·포인터로 접근하는 메모리는 흐름 비민감으로 연결했습니다(쓰기 → 모든 읽기). "
    "실행 주기 간 상태 전달과 ISR 선점을 놓치지 않기 위한 보수적 선택이며, 중간에 덮어써져 실제로는 영향이 없는 후보가 포함될 수 있습니다.",
    "지역 변수·인자는 도달 정의로 흐름 민감하게 추적했습니다(덮어쓴 값은 영향 후보에서 빠집니다).",
    "상수 조건(매크로로 꺼진 if 등)으로 도달할 수 없는 코드는 제외했습니다.",
    "배열 원소는 하나로 합쳐 보았습니다(a[0] 쓰기 → a[1] 읽기도 후보). 구조체 필드는 구분하고 union 은 구분하지 않습니다.",
    "&&, ||, ?: 안의 호출은 항상 실행되는 것으로 보았습니다.",
    "호출 문맥은 길이 {k} 까지 구분합니다. 포인터 인자를 통한 쓰기는 문맥을 구분하지 않습니다.",
    "실제 인터럽트 지연·물리적 타이밍·DMA 는 정적 분석과 SIL 로 확정할 수 없습니다.",
]


def _rel(prog: Program, path: str) -> str:
    try:
        return Path(path).resolve().relative_to(prog.root.resolve()).as_posix()
    except (ValueError, OSError):
        return path


def load_programs(old_root: Path, new_root: Path, cfg: BuildConfig, include=None, exclude=None,
                  extern_model: str | None = None, jobs: int = 1) -> tuple[Program, Program]:
    model = load_extern_model(extern_model)
    progs = []
    for root, label in ((old_root, "old"), (new_root, "new")):
        files = discover_sources(Path(root), include, exclude)
        progs.append(Program(Path(root).resolve(), load_version(Path(root), files, cfg, jobs,
                                                               "변경 전" if label == "old" else "변경 후"), model, label))
    return progs[0], progs[1]


class _Ctx:
    def __init__(self, prog: Program, hits: dict[int, Hit], cs: ChangeSet, version: str, lines: SourceLines):
        self.prog, self.hits, self.cs, self.version, self.lines = prog, hits, cs, version, lines
        self.change_by_node = {c.node: c for c in cs.changes if c.version == version}


def _step(ctx: _Ctx, h: Hit) -> dict:
    p = ctx.prog
    n = p.nodes[h.node]
    return {
        "func": p.func_display(n.func),
        "file": _rel(p, n.file),
        "line": n.line,
        "kind": n.kind,
        "text": n.text,
        "src": ctx.lines.get(n.file, n.line) if n.kind not in ("entry", "actual_out") else "",
        "edge": list(h.edge) if h.edge else None,
    }


def _branch_text(label) -> str:
    if label == "T":
        return "참"
    if label == "F":
        return "거짓"
    return str(label)


def _how(ctx: _Ctx, h: Hit) -> tuple[str, str]:
    p = ctx.prog
    kind, label = h.edge
    if kind == "seed":
        return "직접 변경", "변경된 문장 자체"
    if kind in ("data", "memory"):
        return "값", f"`{label}` 값 변화가 전달됨"
    if kind == "control":
        pred = p.nodes[h.pred]
        if pred.kind == "entry":
            return "호출 여부", f"{p.func_display(pred.func)}() 의 호출 여부·횟수 변화"
        return "실행 여부", f"`{pred.text}` 판정({_branch_text(label)}) 변화로 실행 여부가 달라짐"
    if kind == "param":
        return "값", f"인자 `{label}` 값 변화"
    if kind == "return":
        return "값", f"{p.func_display(label)}() 반환값 변화"
    if kind == "call":
        return "호출 여부", f"{p.func_display(label)}() 호출 여부 변화"
    return kind, str(label)


def _conditions(ctx: _Ctx, path: list[Hit], target: int) -> list[str]:
    p = ctx.prog
    out: list[str] = []
    origin = path[0].node if path else target
    ch = ctx.change_by_node.get(origin)
    if ch is not None and ch.detail and ch.detail not in ("추가된 문장", "삭제된 문장"):
        out.append(f"[{p.func_display(ch.func)}] {ch.detail}")
    if origin != target:  # 변경된 문장 자체가 실행되는 조건 (바깥 조건부터)
        for pn, label in reversed(_guards(p, origin)):
            out.append(f"[{p.func_display(pn.func)}] `{pn.text}` {_branch_text(label)}")
    for prev, h in zip(path, path[1:]):
        if h.edge and h.edge[0] in ("param", "call") and prev.node != origin:
            # 피호출 함수로 들어간 호출 지점이 실행되는 조건
            for pn, label in reversed(_guards(p, prev.node)):
                s = f"[{p.func_display(pn.func)}] `{pn.text}` {_branch_text(label)}"
                if s not in out:
                    out.append(s)
        if h.edge and h.edge[0] == "control":
            pred = p.nodes[h.pred]
            if pred.kind == "cond":
                s = f"`{pred.text}` {_branch_text(h.edge[1])}"
                if s not in out:
                    out.append(s)
    for par, label in p.cd_parents.get(target, ()):
        pn = p.nodes[par]
        if pn.kind == "cond":
            s = f"`{pn.text}` {_branch_text(label)}"
            if s not in out:
                out.append(s)
    return out


def _guards(p: Program, nid: int, limit: int = 12) -> list[tuple]:
    """nid 를 감싸는 조건들 (안쪽 → 바깥쪽)."""
    out, cur, seen = [], nid, set()
    while len(out) < limit:
        conds = [(p.nodes[x], l) for x, l in p.cd_parents.get(cur, ()) if p.nodes[x].kind == "cond" and x != cur]
        if not conds or conds[0][0].id in seen:
            break
        seen.add(conds[0][0].id)
        out.append(conds[0])
        cur = conds[0][0].id
    return out


def _items_for(ctx: _Ctx) -> list[dict]:
    p = ctx.prog
    items: dict[tuple, dict] = {}
    for h in sorted(ctx.hits.values(), key=lambda x: x.order):
        n = p.nodes[h.node]
        if not n.visible or n.dead:
            continue
        found: list[tuple[str, str]] = []
        for d in sorted(n.defs):
            v = p.vars.get(d[0])
            if v is not None and v.persistent:
                kind = "hw" if v.kind == "hw" else ("pointer" if v.kind == "sym" else "var")
                found.append((kind, display_path(p, d)))
        if n.kind == "return" and n.ast is not None:
            found.append(("return", f"{p.func_display(n.func)}() 반환값"))
        elif n.kind == "extcall":
            found.append(("extcall", f"{p.sites[n.site].name}() 호출 (외부)"))
        for kind, name in found:
            key = (n.func, kind, name)
            if key in items:
                continue
            path = path_to(ctx.hits, h.node)
            how_kind, how = _how(ctx, h)
            if kind == "extcall" and how_kind == "직접 변경":
                how = "변경된 외부 호출"
            v = None
            if kind in ("var", "hw", "pointer"):
                base = next(d[0] for d in n.defs if display_path(p, d) == name)
                v = p.vars.get(base)
            items[key] = {
                "func": n.func,
                "func_display": p.func_display(n.func),
                "file": _rel(p, n.file),
                "line": n.line,
                "kind": kind,
                "name": name,
                "var_kind": v.kind if v else None,
                "path_key": list(next(d for d in n.defs if display_path(p, d) == name)) if v else None,
                "how_kind": how_kind,
                "how": how,
                "stmt": n.text,
                "src": ctx.lines.get(n.file, n.line),
                "conditions": _conditions(ctx, path, h.node),
                "path": [_step(ctx, x) for x in path],
                "versions": [ctx.version],
                "order": h.order,
            }
    return list(items.values())


def _merge_items(new_items: list[dict], old_items: list[dict]) -> list[dict]:
    merged: dict[tuple, dict] = {}
    for it in new_items:
        merged[(it["func"], it["kind"], it["name"])] = it
    for it in old_items:
        key = (it["func"], it["kind"], it["name"])
        if key in merged:
            merged[key]["versions"].append("old")
            for c in it["conditions"]:
                if c not in merged[key]["conditions"]:
                    merged[key]["conditions"].append(c)
        else:
            merged[key] = it
    kind_rank = {"var": 0, "hw": 1, "pointer": 2, "return": 3, "extcall": 4}
    return sorted(merged.values(), key=lambda x: (x["file"], x["line"], kind_rank.get(x["kind"], 9)))


def _reachers(prog: Program, roots: list[str]) -> dict[str, set[str]]:
    """함수 → 그 함수에 (호출 그래프로) 도달하는 진입점."""
    g = prog.call_graph()
    out: dict[str, set[str]] = defaultdict(set)
    for r in roots:
        stack, seen = [r], set()
        while stack:
            f = stack.pop()
            if f in seen:
                continue
            seen.add(f)
            out[f].add(r)
            stack.extend(g.get(f, ()))
    return out


def _entry_view(prog: Program, items: list[dict], entries: list[str], isr: list[str]) -> tuple[list[dict], list[dict]]:
    roots = sorted(set(prog.roots()) | {e for e in entries if e in prog.funcs})
    reach = _reachers(prog, roots)

    def is_isr(r):
        return r in isr or bool(ISR_PATTERN.search(prog.func_display(r)))

    funcs = sorted({it["func"] for it in items if it["func"]})
    entry_rows = []
    for r in roots:
        hit = [prog.func_display(f) for f in funcs if r in reach.get(f, ())]
        if hit:
            entry_rows.append({"entry": prog.func_display(r), "isr": is_isr(r), "user": r in entries,
                               "affected_functions": hit})
    shared = []
    targets: dict[tuple, str] = {}
    for it in items:
        if it["kind"] == "var" and it["var_kind"] in ("global", "static", "static_local") and it["path_key"]:
            targets.setdefault(tuple(it["path_key"]), it["name"])
    for path, name in sorted(targets.items(), key=lambda x: x[1]):
        base = path[0]
        if base not in prog.vars:
            continue
        funcs_access = {prog.nodes[nid].func for nid, q in prog.def_index.get(base, []) + prog.use_index.get(base, [])
                        if overlaps(path, q) and prog.nodes[nid].func}
        rts = sorted({r for f in funcs_access for r in reach.get(f, ())})
        if len(rts) > 1:
            shared.append({
                "var": name,
                "entries": [prog.func_display(r) for r in rts],
                "isr": any(is_isr(r) for r in rts),
                "functions": sorted(prog.func_display(f) for f in funcs_access if f),
            })
    return entry_rows, shared


def _changes_json(cs: ChangeSet, old: Program, new: Program, lines: SourceLines) -> list[dict]:
    out = []
    # 같은 줄에 문장 변경이 있으면 호출 인자 단위 변경은 목록에서 생략 (시작점으로는 그대로 쓴다)
    stmt_lines = {(c.version, c.func, c.file, c.line) for c in cs.changes
                  if (old if c.version == "old" else new).nodes[c.node].kind not in ("actual_in", "call", "actual_out")}
    for c in cs.changes:
        if c.kind in ("changed", "global_changed") and c.version == "old":
            continue  # 같은 변경의 변경 후 쪽 항목 하나만 보고
        kind = (old if c.version == "old" else new).nodes[c.node].kind
        if kind in ("actual_in", "call", "actual_out") and (c.version, c.func, c.file, c.line) in stmt_lines:
            continue
        prog = old if c.version == "old" else new
        if c.kind in ("changed", "global_changed"):
            old_text, new_text = c.other_text, c.text
        elif c.version == "old":
            old_text, new_text = c.text, ""
        else:
            old_text, new_text = "", c.text
        out.append({
            "kind": c.kind,
            "version": c.version,
            "func": prog.func_display(c.func),
            "file": _rel(prog, c.file),
            "line": c.line,
            "old": old_text,
            "new": new_text,
            "src": lines.get(c.file, c.line) if c.file else "",
            "detail": c.detail,
            "interval": c.interval,
        })
    out.sort(key=lambda x: (x["file"], x["line"]))
    return out


def _test_candidates(cs: ChangeSet, new: Program, items: list[dict]) -> list[dict]:
    tests = []
    seen = set()
    for c in cs.changes:
        if c.interval and c.version == "new" and (c.func, c.interval["var"]) not in seen:
            seen.add((c.func, c.interval["var"]))
            f = new.funcs.get(c.func)
            is_param = bool(f and any(new.vars[k].name == c.interval["var"] for k in f.params))
            tests.append({
                "func": new.func_display(c.func),
                "var": c.interval["var"],
                "values": c.interval["tests"],
                "diff": c.interval["diff"],
                "old": c.interval["old"], "new": c.interval["new"],
                "drive": "인자" if is_param else "사전 설정(전역/상태)",
            })
    return tests


def _param_hints(new: Program, funcs: set[str]) -> dict[str, list[int]]:
    """영향 함수 안의 단일 변수 비교식에서 인자 경계값 후보를 모은다 (이름 기준)."""
    hints: dict[str, set[int]] = defaultdict(set)
    for fk in funcs:
        f = new.funcs.get(fk)
        if f is None:
            continue
        pnames = {new.vars[k].name for k in f.params}
        for nid in f.nodes:
            n = new.nodes[nid]
            if n.kind != "cond" or n.dead:
                continue
            pr = predicate(n.ast, new)
            if pr and pr[0] in pnames:
                var, s, dom = pr
                hints[var].update(boundary_values(s, dom))
    return {k: sorted(v) for k, v in hints.items()}


def _returning(prog: Program, roots: list[str]) -> list[str]:
    """끝나지 않는 함수(태스크 무한 루프 등)는 하네스에서 호출할 수 없으므로 그 안에서 호출하는 함수로 바꾼다."""
    out: list[str] = []
    stack = list(reversed(roots))
    seen = set()
    while stack:
        f = stack.pop()
        if f in seen:
            continue
        seen.add(f)
        if not prog.funcs[f].no_return:
            out.append(f)
            continue
        callees = []
        for s in sorted(prog.sites, key=lambda s: s.id):
            if s.caller == f:
                callees += [c for c in sorted(s.callees) if c not in callees]
        stack.extend(reversed(callees))
    return out


def _order_roots(prog: Program, roots: list[str]) -> list[str]:
    """상태를 쓰는 진입점을 그 상태를 읽는 진입점보다 먼저 호출하도록 정렬한다 (위상 정렬, 동률은 소스 순서)."""
    g = prog.call_graph()
    direct_r: dict[str, set] = defaultdict(set)
    direct_w: dict[str, set] = defaultdict(set)
    for n in prog.nodes:
        if not n.func or n.dead:
            continue
        for d in n.defs:
            if d[0] in prog.vars and prog.vars[d[0]].persistent:
                direct_w[n.func].add(d)
        for u in n.uses:
            if u[0] in prog.vars and prog.vars[u[0]].persistent:
                direct_r[n.func].add(u)

    def closure(r):
        rs, ws, stack, seen = set(), set(), [r], set()
        while stack:
            f = stack.pop()
            if f in seen:
                continue
            seen.add(f)
            rs |= direct_r[f]
            ws |= direct_w[f]
            stack.extend(g.get(f, ()))
        return rs, ws

    rw = {r: closure(r) for r in roots}
    key = {r: (prog.funcs[r].tu, prog.funcs[r].line) for r in roots}

    def feeds(a, b):
        return any(overlaps(w, x) for w in rw[a][1] for x in rw[b][0])

    preds = {b: {a for a in roots if a != b and feeds(a, b) and not feeds(b, a)} for b in roots}
    order, left = [], set(roots)
    while left:
        ready = [r for r in left if not (preds[r] & left)] or list(left)
        nxt = min(ready, key=lambda r: key[r])
        order.append(nxt)
        left.remove(nxt)
    return order


def sil_template(new: Program, cs: ChangeSet, items: list[dict], entries: list[str], cycles: int = 3) -> dict:
    affected = {it["func"] for it in items if it["func"]} | {c.func for c in cs.changes if c.func and c.version == "new"}
    roots = sorted(set(new.roots()) | {e for e in entries if e in new.funcs})
    reach = _reachers(new, roots)
    drive = _order_roots(new, _returning(new, [r for r in roots if any(r in reach.get(f, ()) for f in affected)]))
    files = sorted({new.funcs[r].tu for r in drive} | {new.funcs[f].tu for f in affected if f in new.funcs})
    params: dict[str, list] = {}
    for t in _test_candidates(cs, new, items):
        if t["drive"] == "인자":
            params[t["var"]] = t["values"]
    for k, v in _param_hints(new, affected).items():
        params.setdefault(k, v)
    steps, records = [], []
    for r in drive:
        f = new.funcs[r]
        args = []
        for k in f.params:
            name = new.vars[k].name
            v = new.vars[k]
            if name in params:
                args.append("${" + name + "}")
            elif v.env is not None and (v.env.is_pointer(v.type) or v.env.is_array(v.type)):
                args.append("NULL /* TODO: 포인터 인자 */")
            else:
                args.append(0)
        st = {"call": f.name, "args": args}
        if not f.params and not f.env.is_void(f.ret_type):
            st["record"] = True
            records.append(st)
        else:
            steps.append(st)
    observe: list = []
    for it in items:
        if it["kind"] == "var" and it["var_kind"] in ("global", "static") and it["name"] not in observe:
            observe.append(it["name"])
    pages = set()
    for key, v in new.vars.items():
        if v.kind != "hw":
            continue
        addr = int(key[3:], 16)
        pages.add(addr & ~0xFFF)
        if any(it["name"] == v.name for it in items):
            tname = "unsigned int"
            if v.env is not None and v.type is not None:
                try:
                    tname = _typename(v.type)
                except Exception:
                    pass
            vq = "" if tname.startswith("volatile") else "volatile "
            observe.append({"expr": f"*({vq}{tname} *){key[3:]}", "name": v.name, "fmt": "x"})
    body = [{"repeat": cycles, "steps": steps}] if steps else []
    return {
        "_comment": "change_impact.py analyze 가 만든 초안입니다. 호출 순서·인자·초기 상태를 실제 스케줄에 맞게 고친 뒤 "
                    "'change_impact.py sil' 로 실행하세요. params 값이 리스트 안의 리스트이면 repeat 주기별 시퀀스입니다.",
        "sources": files,
        "include_dirs": ["."],
        "defines": [],
        "observe": observe,
        "params": params,
        "init": {},
        **({"memory_map": [[f"0x{p:X}", "0x1000"] for p in sorted(pages)]} if pages else {}),
        "steps": body + records,
    }


def run_analysis(old: Program, new: Program, cfg: BuildConfig, *, k: int = 2, entries: list[str] | None = None,
                 isr: list[str] | None = None, sil_cycles: int = 3) -> dict:
    entries = entries or []
    isr = isr or []
    cs = compute_changes(old, new)
    hits_new = forward_slice(new, cs.seeds_new, k)
    hits_old = forward_slice(old, cs.seeds_old, k)
    lines = SourceLines()
    items = _merge_items(_items_for(_Ctx(new, hits_new, cs, "new", lines)),
                         _items_for(_Ctx(old, hits_old, cs, "old", lines)))
    entry_rows, shared = _entry_view(new, items, entries, isr)

    affected_funcs = defaultdict(lambda: {"items": [], "versions": set()})
    for prog, hits, ver in ((new, hits_new, "new"), (old, hits_old, "old")):
        for nid in hits:
            n = prog.nodes[nid]
            if n.func and n.visible:
                affected_funcs[prog.func_display(n.func)]["versions"].add(ver)
    for it in items:
        affected_funcs[it["func_display"]]["items"].append(it["name"])

    excluded = {
        "parse_errors": {**{f"(변경 전) {k}": v for k, v in old.parse_errors.items()},
                         **{f"(변경 후) {k}": v for k, v in new.parse_errors.items()}},
        "extern_functions": [
            {"name": name, "calls": len(sids), "modeled": name in new.extern_model}
            for name, sids in sorted(new.extern_funcs.items())
        ],
        "unresolved_calls": [
            {"func": new.func_display(new.sites[s].caller), "call": new.sites[s].name,
             "line": new.nodes[new.sites[s].call_node].line}
            for s in new.unresolved_calls
        ],
        "unresolved_pointers": sorted(new.unresolved_ptrs | old.unresolved_ptrs),
        "hw_access": sorted(new.hw_access | old.hw_access),
        "warnings": sorted(set(old.warnings + new.warnings)),
        "assumptions": [a.format(k=k) for a in ASSUMPTIONS],
    }
    result = {
        "tool": f"c_impact {__version__}",
        "old_root": str(old.root),
        "new_root": str(new.root),
        "build": cfg.describe(),
        "options": {"context_depth": k, "entries": entries, "isr": isr},
        "files": {"old": [t.rel for t in old.tus], "new": [t.rel for t in new.tus]},
        "stats": {
            "functions": {"old": len(old.funcs), "new": len(new.funcs)},
            "changes": len(cs.changes),
            "seeds": {"old": len(cs.seeds_old), "new": len(cs.seeds_new)},
            "affected_nodes": {"old": len(hits_old), "new": len(hits_new)},
            "affected_functions": len(affected_funcs),
            "items": len(items),
        },
        "changes": _changes_json(cs, old, new, lines),
        "items": items,
        "functions": [
            {"func": f, "items": sorted(set(v["items"])), "versions": sorted(v["versions"])}
            for f, v in sorted(affected_funcs.items())
        ],
        "entries": entry_rows,
        "shared": shared,
        "tests": _test_candidates(cs, new, items),
        "excluded": excluded,
        "sil_template": sil_template(new, cs, items, entries, sil_cycles),
        "graph": _graph(new, hits_new, "new") + _graph(old, hits_old, "old"),
    }
    return result


def _graph(prog: Program, hits: dict[int, Hit], ver: str) -> list[dict]:
    """DOT 출력용: 보이는 노드 사이의 영향 간선 (내부 노드는 건너뛴다)."""
    def visible_pred(h: Hit):
        cur = h
        labels = []
        while cur.pred is not None:
            labels.append(cur.edge)
            pn = prog.nodes[cur.pred]
            if pn.visible and pn.kind not in ("actual_in", "actual_out", "call"):
                return cur.pred, labels
            cur = hits[cur.pred]
        return None, labels

    edges = []
    for h in hits.values():
        n = prog.nodes[h.node]
        if not n.visible or n.kind in ("actual_in", "actual_out", "call"):
            continue
        src, labels = visible_pred(h)
        edges.append({
            "version": ver,
            "id": f"{ver}{h.node}",
            "func": prog.func_display(n.func),
            "label": f"{n.line}: {n.text}",
            "seed": h.edge[0] == "seed",
            "from": f"{ver}{src}" if src is not None else None,
            "edge": " / ".join(f"{k}:{l}" if l else k for k, l in reversed(labels[-2:])) if labels else "",
        })
    return edges


# ---------------------------------------------------------------------------
# 단일 버전 슬라이스: "이 조건식(줄·변수)을 바꾸면 어디까지 확인해야 하나"
# ---------------------------------------------------------------------------


def _var_match(name: str, q: str) -> bool:
    return name == q or name.startswith(q + ".") or name.startswith(q + "[") or name.endswith("::" + q)


def _node_var_names(prog: Program, paths) -> set[str]:
    out = set()
    for p in paths:
        v = prog.vars.get(p[0])
        if v is not None and v.kind != "temp":
            out.add(display_path(prog, p))
    return out


def _file_match(prog: Program, node_file: str, spec: str) -> bool:
    if not node_file:
        return False
    try:
        if Path(node_file).resolve() == (prog.root / spec).resolve():
            return True
    except OSError:
        pass
    nf = Path(node_file).as_posix()
    return nf.endswith("/" + spec.lstrip("./")) or Path(nf).name == spec


def select_seeds(prog: Program, at: list[str], var: str | None = None) -> tuple[set[int], list[str]]:
    """--at 파일:줄 (여러 개) 과 --var 로 시작 노드를 고른다. 찾지 못하면 가까운 줄을 안내한다."""
    seeds: set[int] = set()
    errors: list[str] = []
    cand = [n for n in prog.nodes if n.visible and not n.dead]
    for spec in at:
        if ":" not in spec or not spec.rsplit(":", 1)[1].isdigit():
            errors.append(f"'{spec}': 파일:줄 형식이 아닙니다 (예: timeout.c:9)")
            continue
        fname, line = spec.rsplit(":", 1)
        line = int(line)
        in_file = [n for n in cand if _file_match(prog, n.file, fname)]
        if not in_file:
            errors.append(f"'{fname}': 분석된 파일에서 찾지 못했습니다.")
            continue
        hit = [n for n in in_file if n.line == line]
        if var:
            hit = [n for n in hit if any(_var_match(x, var) for x in _node_var_names(prog, n.defs | n.uses))]
        main = [n for n in hit if n.kind not in ("call", "actual_out")]
        hit = main or hit
        if not hit:
            near = sorted({n.line for n in in_file if n.kind not in ("entry", "formal_in")}, key=lambda x: abs(x - line))[:5]
            errors.append(f"'{spec}': 분석 대상 문장이 없습니다{' (변수 ' + var + ' 포함)' if var else ''}. "
                          f"가까운 줄: {', '.join(map(str, sorted(near)))}")
            continue
        seeds.update(n.id for n in hit)
    if var and not at:
        for n in cand:
            if any(_var_match(x, var) for x in _node_var_names(prog, n.defs)):
                seeds.add(n.id)
        if not seeds:
            errors.append(f"변수 '{var}' 에 값을 쓰는 문장을 찾지 못했습니다.")
    return seeds, errors


def parse_expr(text: str):
    from pycparser import c_parser
    try:
        ast = c_parser.CParser().parse(f"void __cia_whatif(void) {{ ({text}); }}")
    except Exception as e:  # pycparser.ParseError
        raise ValueError(f"가정한 식을 해석하지 못했습니다: {text} ({e})") from None
    return ast.ext[0].body.block_items[0]


def run_slice(prog: Program, cfg: BuildConfig, *, at: list[str], var: str | None = None, what_if: str | None = None,
              k: int = 2, entries: list[str] | None = None, isr: list[str] | None = None) -> dict:
    from .diff import Change, compare_exprs, expr_of

    entries = entries or []
    isr = isr or []
    seeds, errors = select_seeds(prog, at, var)
    if errors and not seeds:
        raise ValueError("\n".join(errors))
    lines = SourceLines()
    cs = ChangeSet()
    what = None
    if what_if:
        targets = [prog.nodes[s] for s in sorted(seeds) if expr_of(prog.nodes[s]) is not None]
        if len(targets) != 1:
            raise ValueError("--what-if 는 조건식·대입·return 이 하나인 줄에만 쓸 수 있습니다 "
                             f"(대상 {len(targets)}개).")
        n = targets[0]
        cur = expr_of(n)
        new_e = parse_expr(what_if)
        detail, info = compare_exprs(cur, prog, new_e, prog)
        what = {"node": n.id, "current": gen_text(cur), "assumed": what_if, "detail": detail, "interval": info}
        cs.changes.append(Change("new", "what_if", n.id, n.func, n.file, n.line, n.text, what_if, None,
                                 detail or f"`{gen_text(cur)}` → `{what_if}`", info))
    hits = forward_slice(prog, seeds, k)
    items = _merge_items(_items_for(_Ctx(prog, hits, cs, "new", lines)), [])
    entry_rows, shared = _entry_view(prog, items, entries, isr)
    tests = []
    for s in sorted(seeds):
        n = prog.nodes[s]
        e = expr_of(n)
        pr = predicate(e, prog) if e is not None else None
        if pr:
            v, st, dom = pr
            from .diff import fmt_set
            tests.append({"func": prog.func_display(n.func), "line": n.line, "var": v, "true_when": fmt_set(v, st, dom),
                          "values": boundary_values(st, dom)})
    if what and what["interval"]:
        tests.insert(0, {"func": prog.func_display(prog.nodes[what["node"]].func), "line": prog.nodes[what["node"]].line,
                         "var": what["interval"]["var"], "true_when": f"현재 {what['interval']['old']} / 가정 {what['interval']['new']}",
                         "values": what["interval"]["tests"]})
    affected = sorted({prog.func_display(prog.nodes[h].func) for h in hits if prog.nodes[h].func and prog.nodes[h].visible})
    return {
        "tool": f"c_impact {__version__}",
        "root": str(prog.root),
        "build": cfg.describe(),
        "options": {"at": at, "var": var, "what_if": what_if, "context_depth": k},
        "warnings": errors,
        "seeds": [{
            "func": prog.func_display(prog.nodes[s].func), "file": _rel(prog, prog.nodes[s].file),
            "line": prog.nodes[s].line, "kind": prog.nodes[s].kind, "text": prog.nodes[s].text,
            "src": lines.get(prog.nodes[s].file, prog.nodes[s].line),
        } for s in sorted(seeds)],
        "what_if": what,
        "affected_functions": affected,
        "items": items,
        "entries": entry_rows,
        "shared": shared,
        "tests": tests,
        "graph": _graph(prog, hits, "new"),
    }


def gen_text(e) -> str:
    from .cexpr import gen
    return gen(e)


def load_program(root: Path, cfg: BuildConfig, include=None, exclude=None, extern_model: str | None = None,
                 jobs: int = 1) -> Program:
    files = discover_sources(Path(root), include, exclude)
    return Program(Path(root).resolve(), load_version(Path(root), files, cfg, jobs), load_extern_model(extern_model), "new")
