"""전방 슬라이싱: 변경 지점에서 의존성을 따라 영향을 전파한다.

호출 문맥(call string, 길이 k)을 구분한다. 인자를 통해 피호출 함수로 들어간 영향은
그 호출 지점의 반환값으로만 돌아오므로, 공용 유틸 함수 하나 때문에 모든 호출자가
영향 후보가 되는 과대 보고를 줄인다. 전역·메모리 경로를 거치면 문맥은 비운다.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

from .program import Program, display_path, overlaps


@dataclass
class Hit:
    node: int
    pred: int | None
    edge: tuple | None  # (종류, 라벨)  종류: seed data memory control param call return
    order: int


def _push(ctx: tuple, site: int, k: int) -> tuple:
    if k <= 0:
        return ()
    new = ctx + (site,)
    if len(new) > k:
        new = (None,) + new[-(k - 1):] if k > 1 else (None,)
    return new


def forward_slice(prog: Program, seeds: set[int], k: int = 2) -> dict[int, Hit]:
    hits: dict[int, Hit] = {}
    seen: set[tuple[int, tuple]] = set()
    q: deque = deque()
    for s in sorted(seeds):
        if prog.nodes[s].dead:
            continue
        hits[s] = Hit(s, None, ("seed", ""), len(hits))
        seen.add((s, ()))
        q.append((s, ()))
    while q:
        nid, ctx = q.popleft()
        for m, nctx, edge in _succ(prog, nid, ctx, k):
            if prog.nodes[m].dead or (m, nctx) in seen:
                continue
            seen.add((m, nctx))
            if m not in hits:
                hits[m] = Hit(m, nid, edge, len(hits))
            q.append((m, nctx))
    return hits


def _succ(prog: Program, nid: int, ctx: tuple, k: int):
    n = prog.nodes[nid]
    for m, var in prog.dd.get(nid, ()):
        yield m, ctx, ("data", var)
    for m, label in prog.cd_children.get(nid, ()):
        yield m, ctx, ("control", label)
    for p in n.defs:
        for m, q in prog.use_index.get(p[0], ()):
            if overlaps(p, q):
                yield m, (), ("memory", display_path(prog, q if len(q) >= len(p) else p))
    if n.kind == "actual_in":
        site = prog.sites[n.site]
        for c in sorted(site.callees):
            f = prog.funcs[c]
            if n.arg < len(f.formal_in):
                pname = prog.vars[f.params[n.arg]].name
                yield f.formal_in[n.arg], _push(ctx, site.id, k), ("param", pname)
    elif n.kind == "call":
        site = prog.sites[n.site]
        for c in sorted(site.callees):
            yield prog.funcs[c].entry, _push(ctx, site.id, k), ("call", c)
    elif n.kind == "return":
        f = n.func
        if ctx and ctx[-1] is not None:
            site = prog.sites[ctx[-1]]
            if f in site.callees and site.actual_out is not None:
                yield site.actual_out, ctx[:-1], ("return", f)
        else:
            for sid in prog.callers.get(f, ()):
                site = prog.sites[sid]
                if site.actual_out is not None:
                    yield site.actual_out, ctx, ("return", f)


def path_to(hits: dict[int, Hit], nid: int, limit: int = 200) -> list[Hit]:
    out = []
    cur = hits.get(nid)
    while cur is not None and len(out) < limit:
        out.append(cur)
        cur = hits.get(cur.pred) if cur.pred is not None else None
    return list(reversed(out))
