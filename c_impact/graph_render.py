"""영향 경로 시각화: Graphviz DOT 생성 + (dot 가 없을 때도 동작하는) 자체 계층 배치 SVG.

- 확정 의존: 실선, 불확실(POTENTIAL) 의존: 주황 점선
- 노드 모양: 변경(육각), 조건(마름모), 함수(사각), 변수(타원), 출력(이중 사각)
"""

from __future__ import annotations

import html
import shutil
import subprocess

LEVEL_COLOR = {"DIRECT": "#d93636", "INDIRECT": "#e08a1e", "POTENTIAL": "#8a63d2", None: "#7b8794",
               "NO_IMPACT_CONFIRMED": "#3a9d5d"}
KIND_SHAPE = {"change": "hexagon", "condition": "diamond", "function": "box", "variable": "ellipse",
              "output": "box3d"}


def _q(s: str) -> str:
    return '"' + str(s).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'


def to_dot(graph: dict, title="impact") -> str:
    out = [f"digraph {_q(title)} {{", "  rankdir=LR;", '  graph [fontname="Helvetica", fontsize=10];',
           '  node [fontname="Helvetica", fontsize=10, style="filled", fillcolor="white"];',
           '  edge [fontname="Helvetica", fontsize=8];']
    for n in graph["nodes"]:
        color = LEVEL_COLOR.get(n.get("level"), "#7b8794")
        fill = "#fff1f1" if n["kind"] == "change" else "white"
        extra = ', style="filled,dashed"' if n.get("call_context") else ""
        out.append(f"  {_q(n['id'])} [label={_q(n['label'])}, shape={KIND_SHAPE.get(n['kind'], 'box')}, "
                   f"color={_q(color)}, fillcolor={_q(fill)}, penwidth=1.6{extra}];")
    for e in graph["edges"]:
        style = "solid" if e["certain"] else "dashed"
        color = "#444444" if e["certain"] else "#e8710a"
        out.append(f"  {_q(e['from'])} -> {_q(e['to'])} [label={_q(e['label'])}, style={style}, color={_q(color)}, "
                   f"fontcolor={_q(color)}];")
    out.append("}")
    return "\n".join(out)


def render_with_dot(dot_text: str, fmt="svg") -> str | None:
    exe = shutil.which("dot")
    if not exe:
        return None
    try:
        r = subprocess.run([exe, f"-T{fmt}"], input=dot_text.encode(), capture_output=True, timeout=60)
        return r.stdout.decode("utf-8", "replace") if r.returncode == 0 else None
    except Exception:
        return None


# --------------------------------------------------------------------------
def layout(graph: dict):
    """계층(최장 경로) 배치 + 무게중심 정렬. 반환: {노드: (열, 행)}"""
    ids = [n["id"] for n in graph["nodes"]]
    succ = {i: [] for i in ids}
    pred = {i: [] for i in ids}
    for e in graph["edges"]:
        if e["from"] in succ and e["to"] in succ:
            succ[e["from"]].append(e["to"])
            pred[e["to"]].append(e["from"])
    # 역방향 간선 제거(DFS)
    state, back = {}, set()
    roots = [i for i in ids if i.startswith("chg:")] + [i for i in ids if not pred[i]] + ids

    for r in roots:
        if r in state:
            continue
        stack = [(r, iter(succ[r]))]
        state[r] = 1
        while stack:
            v, it = stack[-1]
            for w in it:
                if state.get(w) == 1:
                    back.add((v, w))
                elif w not in state:
                    state[w] = 1
                    stack.append((w, iter(succ[w])))
                    break
            else:
                state[v] = 2
                stack.pop()
    layer = {i: 0 for i in ids}
    order = _topo(ids, succ, back)
    for v in order:
        for w in succ[v]:
            if (v, w) not in back:
                layer[w] = max(layer[w], layer[v] + 1)
    cols: dict[int, list] = {}
    for i in order:
        cols.setdefault(layer[i], []).append(i)
    pos = {}
    for c in sorted(cols):
        for r, i in enumerate(cols[c]):
            pos[i] = (c, r)
    for _ in range(4):
        for c in sorted(cols)[1:]:
            def bc(i):
                ps = [pos[p][1] for p in pred[i] if p in pos and pos[p][0] < c]
                return sum(ps) / len(ps) if ps else pos[i][1]
            cols[c].sort(key=bc)
            for r, i in enumerate(cols[c]):
                pos[i] = (c, r)
    return pos


def _topo(ids, succ, back):
    indeg = {i: 0 for i in ids}
    for v in ids:
        for w in succ[v]:
            if (v, w) not in back:
                indeg[w] += 1
    q = [i for i in ids if indeg[i] == 0]
    out = []
    while q:
        v = q.pop(0)
        out.append(v)
        for w in succ[v]:
            if (v, w) in back:
                continue
            indeg[w] -= 1
            if indeg[w] == 0:
                q.append(w)
    rest = [i for i in ids if i not in set(out)]
    return out + rest


def to_svg(graph: dict, width_per_col=230, height_per_row=64) -> str:
    """자체 배치 SVG. 노드/간선에 data-changes 속성을 넣어 HTML 에서 변경점별 필터링에 쓴다."""
    if not graph["nodes"]:
        return '<svg xmlns="http://www.w3.org/2000/svg" width="300" height="40"><text x="10" y="25">영향 그래프 없음</text></svg>'
    pos = layout(graph)
    nw, nh = 180, 40
    ncol = max(c for c, _ in pos.values()) + 1
    nrow = max(r for _, r in pos.values()) + 1
    W, H = ncol * width_per_col + 40, nrow * height_per_row + 40
    xy = {i: (20 + c * width_per_col, 20 + r * height_per_row) for i, (c, r) in pos.items()}
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" class="impact-graph" viewBox="0 0 {W} {H}" '
             f'width="{W}" height="{H}" font-family="Helvetica, Arial, sans-serif">',
             '<defs><marker id="arr" viewBox="0 0 10 10" refX="10" refY="5" markerWidth="7" markerHeight="7" '
             'orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" fill="context-stroke"/></marker></defs>']
    for e in graph["edges"]:
        if e["from"] not in xy or e["to"] not in xy:
            continue
        x1, y1 = xy[e["from"]]
        x2, y2 = xy[e["to"]]
        sx, sy = x1 + nw, y1 + nh / 2
        tx, ty = x2, y2 + nh / 2
        if x2 <= x1:  # 역방향
            sx, tx = x1 + nw / 2, x2 + nw / 2
            sy, ty = y1 + nh, y2 + nh
            d = f"M{sx},{sy} C{sx},{sy + 40} {tx},{ty + 40} {tx},{ty}"
        else:
            mx = (sx + tx) / 2
            d = f"M{sx},{sy} C{mx},{sy} {mx},{ty} {tx},{ty}"
        cls = "e-certain" if e["certain"] else "e-potential"
        ch = " ".join(e.get("changes", []))
        title = html.escape(f"{e['label']} ({'확정' if e['certain'] else '불확실'})")
        parts.append(f'<g class="edge {cls}" data-changes="{ch}"><title>{title}</title>'
                     f'<path d="{d}" fill="none" marker-end="url(#arr)"/>'
                     f'<text x="{(sx + tx) / 2:.0f}" y="{(sy + ty) / 2 - 4:.0f}" class="elabel">{html.escape(e["label"])}</text></g>')
    for n in graph["nodes"]:
        x, y = xy[n["id"]]
        lv = n.get("level") or "NONE"
        ch = " ".join(n.get("changes", []))
        lines = str(n["label"]).split("\n")
        shape = n["kind"]
        if shape == "condition":
            body = f'<polygon points="{x},{y + nh / 2} {x + 14},{y} {x + nw - 14},{y} {x + nw},{y + nh / 2} {x + nw - 14},{y + nh} {x + 14},{y + nh}"/>'
        elif shape == "variable":
            body = f'<ellipse cx="{x + nw / 2}" cy="{y + nh / 2}" rx="{nw / 2}" ry="{nh / 2}"/>'
        elif shape == "change":
            body = f'<polygon points="{x + 10},{y} {x + nw - 10},{y} {x + nw},{y + nh / 2} {x + nw - 10},{y + nh} {x + 10},{y + nh} {x},{y + nh / 2}"/>'
        elif shape == "output":
            body = f'<rect x="{x}" y="{y}" width="{nw}" height="{nh}" rx="2"/><rect x="{x + 3}" y="{y + 3}" width="{nw - 6}" height="{nh - 6}" rx="2" class="inner"/>'
        else:
            body = f'<rect x="{x}" y="{y}" width="{nw}" height="{nh}" rx="6"/>'
        dash = " callctx" if n.get("call_context") else ""
        text = "".join(f'<tspan x="{x + nw / 2}" dy="{0 if i == 0 else 12}">{html.escape(t[:30])}</tspan>'
                       for i, t in enumerate(lines[:2]))
        ty = y + nh / 2 + (4 if len(lines) == 1 else -2)
        parts.append(f'<g class="node k-{shape} lv-{lv}{dash}" data-id="{html.escape(n["id"])}" data-changes="{ch}">'
                     f'<title>{html.escape(n["label"])} [{lv}]</title>{body}'
                     f'<text x="{x + nw / 2}" y="{ty}" text-anchor="middle">{text}</text></g>')
    parts.append("</svg>")
    return "".join(parts)
