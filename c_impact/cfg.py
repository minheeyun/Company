"""4단계: CFG 구성, 지배/후지배 관계, 제어 의존, 경로 열거·비교, 도달성 분석.

노드 ID 는 문장 ID 와 같다(ENTRY=-1, EXIT=-2). 복합문(if/while/for/switch/do)은 조건 노드 하나로 표현한다.
"""

from __future__ import annotations

from .ast_extract import expr_text, iter_stmts
from .valuerange import Env, apply_stmt, eval_cond, refine

ENTRY, EXIT = -1, -2
BRANCH_KINDS = ("if", "while", "do", "for", "switch")


class CFG:
    def __init__(self, fn: dict):
        self.fn = fn
        self.nodes: dict[int, dict] = {ENTRY: {"kind": "entry", "text": "ENTRY", "line": fn["line"]},
                                       EXIT: {"kind": "exit", "text": "EXIT", "line": fn["end_line"]}}
        self.succ: dict[int, list] = {ENTRY: [], EXIT: []}
        self.pred: dict[int, list] = {ENTRY: [], EXIT: []}
        self.stmts: dict[int, dict] = {}
        self._brk, self._cont, self._sw = [], [], []
        self._labels = {}
        self._gotos = []
        self.loop_defs: dict[int, set] = {}
        self._build()
        self._ipdom = None
        self._cd = None

    # ------------------------------------------------------------ 구성
    def _node(self, st):
        sid = st["id"]
        self.stmts[sid] = st
        line = st.get("cond_line", st["line"])
        self.nodes[sid] = {"kind": st["kind"], "text": st["text"], "line": line}
        self.succ.setdefault(sid, [])
        self.pred.setdefault(sid, [])
        return sid

    def edge(self, a, b, label=""):
        if (b, label) not in self.succ[a]:
            self.succ[a].append((b, label))
            self.pred.setdefault(b, []).append((a, label))

    def _build(self):
        for st in iter_stmts(self.fn["body"]):
            if st["kind"] == "label":
                self._labels[st.get("label")] = st["id"]
        entry = self._seq(self.fn["body"], EXIT)
        self.edge(ENTRY, entry)
        for sid, lbl in self._gotos:
            if lbl in self._labels:
                self.edge(sid, self._labels[lbl], "goto")
            else:
                self.edge(sid, EXIT, "goto?")

    def _seq(self, stmts, nxt):
        entry = nxt
        for st in reversed(stmts or []):
            entry = self._one(st, entry)
        return entry

    def _one(self, st, nxt):
        k = st["kind"]
        sid = self._node(st)
        if k in ("while", "do", "for"):
            self.loop_defs[sid] = {d for s in iter_stmts((st.get("body") or []) + (st.get("inc") or []))
                                   for d in s.get("defs", [])}
        if k in ("expr", "decl", "null", "unknown", "label"):
            self.edge(sid, nxt)
        elif k == "return":
            self.edge(sid, EXIT)
        elif k == "break":
            self.edge(sid, self._brk[-1] if self._brk else nxt, "break")
        elif k == "continue":
            self.edge(sid, self._cont[-1] if self._cont else nxt, "continue")
        elif k == "goto":
            self._gotos.append((sid, st.get("label")))
        elif k in ("case", "default"):
            self.edge(sid, nxt)
            if self._sw:
                self._sw[-1].append(st)
        elif k == "if":
            t = self._seq(st.get("then"), nxt)
            f = self._seq(st["else"], nxt) if st.get("else") is not None else nxt
            self.edge(sid, t, "T")
            self.edge(sid, f, "F")
        elif k == "while":
            self._brk.append(nxt)
            self._cont.append(sid)
            b = self._seq(st.get("body"), sid)
            self._brk.pop()
            self._cont.pop()
            self.edge(sid, b, "T")
            self.edge(sid, nxt, "F")
        elif k == "do":
            self._brk.append(nxt)
            self._cont.append(sid)
            b = self._seq(st.get("body"), sid)
            self._brk.pop()
            self._cont.pop()
            self.edge(sid, b, "T")
            self.edge(sid, nxt, "F")
            return b
        elif k == "for":
            inc = self._seq(st.get("inc"), sid)
            self._brk.append(nxt)
            self._cont.append(inc)
            b = self._seq(st.get("body"), inc)
            self._brk.pop()
            self._cont.pop()
            if st.get("cond") is not None:
                self.edge(sid, b, "T")
                self.edge(sid, nxt, "F")
            else:
                self.edge(sid, b, "T")  # for(;;) — 무한 반복
            return self._seq(st.get("init"), sid)
        elif k == "switch":
            self._brk.append(nxt)
            self._sw.append([])
            self._seq(st.get("body"), nxt)
            cases = self._sw.pop()
            self._brk.pop()
            has_default = False
            for c in cases:
                if c["kind"] == "default":
                    has_default = True
                    self.edge(sid, c["id"], "default")
                else:
                    self.edge(sid, c["id"], f"case {c.get('value_text')}")
            if not has_default:
                self.edge(sid, nxt, "no-match")
        else:
            self.edge(sid, nxt)
        return sid

    # --------------------------------------------------------- 그래프 계산
    def reachable(self):
        seen, stack = {ENTRY}, [ENTRY]
        while stack:
            n = stack.pop()
            for s, _ in self.succ.get(n, []):
                if s not in seen:
                    seen.add(s)
                    stack.append(s)
        return seen

    def unreachable_nodes(self):
        r = self.reachable()
        return [n for n in self.nodes if n not in r and n not in (ENTRY, EXIT)]

    def ipdom(self):
        """직후 후지배자. EXIT 에 도달 못하는 노드(무한 루프)는 가상 간선으로 EXIT 에 연결한다."""
        if self._ipdom is not None:
            return self._ipdom
        nodes = list(self.nodes)
        rsucc = {n: [s for s, _ in self.succ.get(n, [])] for n in nodes}
        # EXIT 역방향 도달 가능 집합
        can = {EXIT}
        changed = True
        while changed:
            changed = False
            for n in nodes:
                if n not in can and any(s in can for s in rsucc[n]):
                    can.add(n)
                    changed = True
        for n in nodes:
            if n not in can and n != EXIT:
                rsucc[n] = rsucc[n] + [EXIT]
        # 역 후위순서(EXIT 기준, 역방향 그래프)
        rpred = {n: [] for n in nodes}
        for n in nodes:
            for s in rsucc[n]:
                rpred.setdefault(s, []).append(n)
        order, seen = [], set()

        def dfs(n):
            stack = [(n, iter(rpred.get(n, [])))]
            seen.add(n)
            while stack:
                v, it = stack[-1]
                for w in it:
                    if w not in seen:
                        seen.add(w)
                        stack.append((w, iter(rpred.get(w, []))))
                        break
                else:
                    order.append(v)
                    stack.pop()
        dfs(EXIT)
        rpo = list(reversed(order))
        idx = {n: i for i, n in enumerate(rpo)}
        ipd = {EXIT: EXIT}
        changed = True
        while changed:
            changed = False
            for n in rpo:
                if n == EXIT:
                    continue
                cands = [s for s in rsucc[n] if s in ipd]
                if not cands:
                    continue
                new = cands[0]
                for s in cands[1:]:
                    new = _intersect(s, new, ipd, idx)
                if ipd.get(n) != new:
                    ipd[n] = new
                    changed = True
        self._ipdom = ipd
        return ipd

    def control_deps(self) -> dict[int, list[tuple[int, str]]]:
        """노드 → [(분기 노드, 간선 레이블)] 제어 의존."""
        if self._cd is not None:
            return self._cd
        ipd = self.ipdom()
        cd: dict[int, list] = {}
        for a in self.nodes:
            succ = self.succ.get(a, [])
            if len(succ) < 2:
                continue
            stop = ipd.get(a)
            for b, lab in succ:
                runner = b
                guard = 0
                while runner is not None and runner != stop and guard < 10000:
                    if runner != a or b == a:
                        cd.setdefault(runner, [])
                        if (a, lab) not in cd[runner]:
                            cd[runner].append((a, lab))
                    if runner == EXIT:
                        break
                    nxt = ipd.get(runner)
                    if nxt == runner:
                        break
                    runner = nxt
                    guard += 1
        self._cd = cd
        return cd

    def dependents(self, branch: int) -> list[int]:
        """branch 에 제어 의존하는 노드들."""
        return [n for n, deps in self.control_deps().items() if any(b == branch for b, _ in deps)]

    # ------------------------------------------------------------ 경로
    def paths(self, max_paths=200, loop_unroll=1):
        """ENTRY→EXIT 경로 열거. 반환: (경로 목록, 잘림 여부). 경로 = [(노드, 다음 간선 레이블)]."""
        out = []
        truncated = False
        limit = loop_unroll + 1
        count: dict[int, int] = {}
        path = []

        stack = [(ENTRY, None)]
        # 반복 DFS
        it_stack = []
        count[ENTRY] = 1
        path.append([ENTRY, None])
        it_stack.append(iter(self.succ.get(ENTRY, [])))
        while it_stack:
            if len(out) >= max_paths:
                truncated = True
                break
            try:
                s, lab = next(it_stack[-1])
            except StopIteration:
                n = path.pop()[0]
                count[n] -= 1
                it_stack.pop()
                continue
            path[-1][1] = lab
            if s == EXIT:
                out.append([tuple(x) for x in path] + [(EXIT, None)])
                continue
            if count.get(s, 0) >= limit:
                continue
            count[s] = count.get(s, 0) + 1
            path.append([s, None])
            it_stack.append(iter(self.succ.get(s, [])))
        del stack
        return out, truncated

    def path_decisions(self, path):
        """경로의 분기 결정 [(노드, 조건 텍스트, 레이블)]."""
        out = []
        for n, lab in path:
            if n in self.stmts and len(self.succ.get(n, [])) >= 2:
                out.append((n, self.nodes[n]["text"], lab))
        return out

    def feasible(self, path, platform=None) -> bool | None:
        """구간 추론으로 경로 조건 모순 여부 확인. False=모순(실행 불가 추정), True=모순 없음."""
        env = Env()
        for n, lab in path:
            st = self.stmts.get(n)
            if st is None:
                continue
            if st["kind"] in ("if", "while", "for", "do") and st.get("cond") is not None and lab in ("T", "F"):
                if n in self.loop_defs:
                    # 반복문 조건: 반복 횟수를 모르므로 본문에서 바뀌는 변수는 '알 수 없음'으로 확장(widening)
                    env = env.copy()
                    for d in self.loop_defs[n]:
                        env.r.pop(d, None)
                env2 = refine(env, st["cond"], lab == "T", platform)
                if env2 is None:
                    return False
                env = env2
            elif st["kind"] == "switch" and lab and lab.startswith("case "):
                c = next((s for s in st.get("body", []) if s["kind"] == "case"
                          and f"case {s.get('value_text')}" == lab), None)
                if c is not None and c.get("value") is not None:
                    env2 = refine(env, {"k": "bin", "op": "==", "l": st["cond"],
                                        "r": {"k": "const", "v": c["value"]}}, True, platform)
                    if env2 is None:
                        return False
                    env = env2
            else:
                env = apply_stmt(env, st, platform)
        return True

    def static_branch_outcomes(self, platform=None):
        """자료형 범위만으로 결과가 고정되는 분기(도달 불가 분기) 목록."""
        out = []
        for n, st in self.stmts.items():
            if st["kind"] in ("if", "while", "for", "do") and st.get("cond") is not None:
                r = eval_cond(Env(), st["cond"], platform)
                if r is True or r is False:
                    if st["kind"] == "for" and r is True and st.get("cond", {}).get("k") == "const":
                        continue
                    if st["kind"] in ("while", "do") and r is True and st["cond"].get("k") == "const":
                        continue  # while(1) 등 의도적 무한 루프
                    out.append({"node": n, "line": st["line"], "text": st["text"], "always": r,
                                "dead_branch": "F" if r is True else "T"})
        return out

    def to_dot(self, name="cfg", highlight=()):
        lines = [f'digraph "{name}" {{', '  node [shape=box, fontname="Helvetica", fontsize=10];']
        for n, d in self.nodes.items():
            shape = "diamond" if d["kind"] in BRANCH_KINDS else "box"
            color = ', style=filled, fillcolor="#ffd6d6"' if n in highlight else ""
            label = f"{d['line']}: {d['text']}"[:60].replace('"', "'")
            lines.append(f'  n{n if n >= 0 else "_" + str(-n)} [label="{label}", shape={shape}{color}];')
        for a, ss in self.succ.items():
            for b, lab in ss:
                na = f"n{a if a >= 0 else '_' + str(-a)}"
                nb = f"n{b if b >= 0 else '_' + str(-b)}"
                lines.append(f'  {na} -> {nb} [label="{lab}"];')
        lines.append("}")
        return "\n".join(lines)


def _intersect(a, b, ipd, idx):
    guard = 0
    while a != b and guard < 100000:
        while idx.get(a, -1) > idx.get(b, -1):
            a = ipd.get(a, EXIT)
            guard += 1
        while idx.get(b, -1) > idx.get(a, -1):
            b = ipd.get(b, EXIT)
            guard += 1
        if guard > 100000:
            break
    return a


# --------------------------------------------------------------------------
def reach_conditions(cfg: CFG, paths, node: int, platform=None, label_map=None) -> list[tuple]:
    """node 에 도달하는 (실행 가능 추정) 경로들의 분기 조건 집합."""
    conds = set()
    for p in paths:
        ids = [n for n, _ in p]
        if node not in ids:
            continue
        cut = p[:ids.index(node)]
        if cfg.feasible(cut + [(node, None)], platform) is False:
            continue
        dec = tuple(dict.fromkeys((t, l) for _, t, l in cfg.path_decisions(cut)))
        conds.add(dec)
    return sorted(conds)


def compare_function_cfg(fid, fb, fc, stmt_map: dict, platform, max_paths=200, loop_unroll=1,
                         focus_nodes=None) -> dict:
    """변경 전후 CFG 비교: 경로 추가/삭제, 도달 조건 변화, 도달 불가 변화."""
    gb, gc = CFG(fb), CFG(fc)
    pb, tb = gb.paths(max_paths, loop_unroll)
    pc, tc = gc.paths(max_paths, loop_unroll)
    inv = {v: k for k, v in stmt_map.items()}  # base sid → curr sid

    def sig_c(p):
        return tuple(("m", stmt_map[n], lab) if n in stmt_map else ("n", gc.nodes[n]["text"], lab)
                     for n, lab in p if n not in (ENTRY, EXIT))

    def sig_b(p):
        return tuple(("m", n, lab) if n in inv else ("o", gb.nodes[n]["text"], lab)
                     for n, lab in p if n not in (ENTRY, EXIT))

    sb = {sig_b(p): p for p in pb}
    sc = {sig_c(p): p for p in pc}
    added = [sc[s] for s in sc if s not in sb]
    removed = [sb[s] for s in sb if s not in sc]

    def describe(g, p):
        return {"decisions": [f"{t} = {l}" for _, t, l in g.path_decisions(p)],
                "actions": [g.nodes[n]["text"] for n, _ in p if n in g.stmts and g.stmts[n]["kind"] in
                            ("expr", "return", "decl") and (g.stmts[n].get("defs") or g.stmts[n].get("calls")
                                                            or g.stmts[n]["kind"] == "return")][:12],
                "feasible": g.feasible(p, platform)}

    # 도달 조건 변화(주요 노드)
    reach = []
    targets = focus_nodes if focus_nodes is not None else [
        n for n, st in gc.stmts.items() if st["kind"] in ("expr", "return") and (st.get("calls") or st.get("defs"))]
    for n in targets:
        if n not in gc.stmts:
            continue
        rc = reach_conditions(gc, pc, n, platform)
        bn = stmt_map.get(n)
        rb = reach_conditions(gb, pb, bn, platform) if bn is not None else None
        if rb is None or [list(x) for x in rb] != [list(x) for x in rc]:
            reach.append({"node": n, "line": gc.stmts[n]["line"], "text": gc.stmts[n]["text"],
                          "base_conditions": [[f"{t} = {l}" for t, l in c] for c in rb] if rb is not None else None,
                          "curr_conditions": [[f"{t} = {l}" for t, l in c] for c in rc],
                          "new_statement": bn is None})
    # 도달 불가 변화
    ub = {gb.nodes[n]["text"] for n in gb.unreachable_nodes()}
    uc = [{"line": gc.nodes[n]["line"], "text": gc.nodes[n]["text"]} for n in gc.unreachable_nodes()]
    sbo = gb.static_branch_outcomes(platform)
    sco = gc.static_branch_outcomes(platform)
    sbo_t = {x["text"] for x in sbo}
    infeasible_c = sum(1 for p in pc if gc.feasible(p, platform) is False)
    infeasible_b = sum(1 for p in pb if gb.feasible(p, platform) is False)
    return {
        "function": fid,
        "base": {"nodes": len(gb.nodes), "edges": sum(len(v) for v in gb.succ.values()), "paths": len(pb),
                 "truncated": tb, "infeasible_paths": infeasible_b},
        "curr": {"nodes": len(gc.nodes), "edges": sum(len(v) for v in gc.succ.values()), "paths": len(pc),
                 "truncated": tc, "infeasible_paths": infeasible_c},
        "paths_added": [describe(gc, p) for p in added[:20]],
        "paths_removed": [describe(gb, p) for p in removed[:20]],
        "n_paths_added": len(added), "n_paths_removed": len(removed),
        "reach_condition_changes": reach[:40],
        "unreachable_curr": [u for u in uc if u["text"] not in ub],
        "static_dead_branches_curr": [x for x in sco if x["text"] not in sbo_t],
        "static_dead_branches_base": sbo,
        "_cfg_curr": gc, "_cfg_base": gb,
    }


def cond_text(st):
    return expr_text(st.get("cond")) if st.get("cond") else st.get("text")
