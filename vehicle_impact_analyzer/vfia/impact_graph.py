"""영향 그래프: 의존·영향 관계를 그래프로 보여 주기 위한 데이터와 내보내기(DOT, GraphML).

보고서(impact.html)의 '영향 그래프' 탭과 out/graph_export/ 파일이 이 모듈의 결과를 쓴다.

세 가지 그래프
  1. 변경 영향 그래프(변경별)   변경 지점 → 변수 → 함수 → 외부 출력(RTE·CAN 신호, 레지스터, 관찰 대상).
     SDG(문장 단위) 위의 전방 슬라이스·문맥 민감 chop을 함수·변수 단위로 추상화한 것으로, 보고서
     '영향 범위·근거 경로'의 그래프 판이다. chop 위(관찰 대상까지 가는 경로)의 요소는 on_path로 표시.
  2. 함수 호출·데이터 의존 그래프   프로그램 전체를 함수·변수·신호 단위로 추상화하고, 변경 함수 주변
     graph_max_steps 단계까지만 담는다(보고서에서 단계 수를 줄여 볼 수 있음).
  3. 모듈 의존 그래프   파일·폴더 단위. 모듈 간 호출 수(함수 쌍)와 공유 변수 수(쓰는 모듈 → 읽는 모듈).

표현 규칙(보고서·DOT 공통)
  연결 종류: 호출(실선), 공유 변수·신호(점선), 제어 조건(보라색)
    - 변경 영향 그래프의 제어 조건: 변경의 영향이 조건문(제어 의존)을 거쳐 전달된 연결
    - 함수·모듈 그래프의 제어 조건: 조건문 안에서 일어나는 호출·읽기·쓰기(조건부 연결)
  변경 노드(빨강 채움), 위험 후보(관찰 대상까지 가는 근거 경로 위의 요소, 기능 판정에 따라 높음/보통)
  변경 전·후 비교: 프로그램 그래프에 새로 생긴 연결은 초록, 사라진 연결은 빨강

단계(거리) 단위: 호출·반환 1, 변수·신호를 거치는 연결은 한 번에 0.5씩(함수 → 변수 → 함수 = 1단계).
"""
from __future__ import annotations

import heapq
import json
import os
import posixpath
from collections import defaultdict
from typing import Dict, Iterable, List, Optional, Set, Tuple

from .model import EK, NK, AnalysisGraph, Status, loc_display

CYTOSCAPE_VERSION = "3.34.4"
VENDOR_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor")
ASSET_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "report_assets")

# 표시용 연결 종류
CALL, SHARED, SIGNAL, OBSERVE, CHANGE = "call", "shared", "signal", "observe", "change"
STEP_COST = {CALL: 1.0, SHARED: 0.5, SIGNAL: 0.5, OBSERVE: 0.5, CHANGE: 0.0}
KIND_LABEL = {CALL: "호출", SHARED: "공유 변수", SIGNAL: "신호", OBSERVE: "관찰", CHANGE: "변경"}
NODE_KIND_LABEL = {"change": "변경", "function": "함수", "external": "외부 함수(본문 없음)", "variable": "변수",
                   "register": "레지스터(MMIO)", "signal": "신호", "observation": "관찰 대상(기능 출력)",
                   "module": "모듈", "cluster": "묶음"}
_RISK_OF_STATUS = {Status.EXEC_DIFF: 2, Status.MODEL_DIFF: 2, Status.STATIC_CANDIDATE: 1, Status.INCONCLUSIVE: 1}

# SDG 간선 -> 표시 연결 종류(함수가 다른 두 요소 사이일 때만 남김)
_SDG_KIND = {EK.CALL: CALL, EK.PARAM_IN: CALL, EK.PARAM_OUT: CALL, EK.CONTROL: CALL,
             EK.GLOBAL_WRITE: SHARED, EK.GLOBAL_READ: SHARED, EK.DATA: SHARED,
             EK.SIGNAL_WRITE: SIGNAL, EK.SIGNAL_READ: SIGNAL}
_SDG_LABEL = {EK.PARAM_IN: "인자", EK.PARAM_OUT: "반환값", EK.GLOBAL_WRITE: "쓰기", EK.GLOBAL_READ: "읽기",
              EK.SIGNAL_WRITE: "송신", EK.SIGNAL_READ: "수신"}
_IMPACT_EDGES = frozenset(set(_SDG_KIND) | {EK.INTERFERENCE})


def _folder(file: Optional[str]) -> str:
    if not file:
        return "(외부)"
    d = posixpath.dirname(file)
    return d or "."


# ---------------------------------------------------------------------------
# SDG 노드 -> 함수·변수·신호 단위 노드
# ---------------------------------------------------------------------------
class Abstraction:
    """한 버전 그래프에서 SDG 노드를 표시 단위(함수/변수/신호/관찰 대상)로 대응시킨다."""

    def __init__(self, g: AnalysisGraph):
        self.g = g
        self._cache: Dict[str, Optional[str]] = {}
        self.loc_by_name: Dict[str, List[str]] = defaultdict(list)
        for l in g.nodes_of(NK.LOCATION):
            self.loc_by_name[g.node(l).get("name") or ""].append(l)

    def of(self, n: str) -> Optional[str]:
        if n in self._cache:
            return self._cache[n]
        g = self.g
        r: Optional[str] = None
        if g.has_node(n):
            k = g.kind(n)
            if k in (NK.LOCATION, NK.SIGNAL, NK.OBSERVATION, NK.FUNCTION):
                r = n
            else:
                r = g.func_of(n)
        self._cache[n] = r
        return r

    def conditional(self, n: str) -> bool:
        """n이 조건문 안에서 실행되는가(조건 문장에서 오는 제어 의존이 있는가)."""
        g = self.g
        return any(g.kind(p) == NK.STMT for p in g.pred(n, [EK.CONTROL]))

    def info(self, a: str) -> dict:
        """보고서·내보내기용 노드 정보(짧은 키: k 종류, l 이름, f 파일, ln 줄, x 상세)."""
        g = self.g
        d = g.node(a)
        k = d.get("kind")
        x: Dict[str, object] = {}
        if k == NK.FUNCTION:
            kind = "external" if d.get("external") else "function"
            label = d.get("name") or a.split(":")[-1]
            if d.get("sig"):
                x["시그니처"] = d.get("sig")
            ctx = sorted({g.node(t).get("name") for t in g.succ(a, [EK.RUNS_IN])})
            if ctx:
                x["실행 문맥"] = ", ".join(ctx)
            if d.get("static"):
                x["링크"] = "static(파일 내부)"
        elif k == NK.LOCATION:
            kind = "register" if d.get("mmio") or (d.get("root") or "").startswith("gv:mmio:") else "variable"
            label = loc_display(d.get("loc") or a[4:]) if kind == "variable" else (d.get("name") or a)
            if kind == "register":
                x["주소"] = (d.get("root") or "").split(":")[-1]
            if d.get("volatile"):
                x["volatile"] = "예"
            if d.get("calibration"):
                gv = d.get("root") or ""
                gd = g.node(gv) if g.has_node(gv) else {}
                x["캘리브레이션"] = f"{d.get('calibration')} (범위 {gd.get('cal_min')}~{gd.get('cal_max')})"
            writers = sorted({(g.func_of(w) or "").split(":")[-1] for w in g.pred(a, [EK.GLOBAL_WRITE]) if g.func_of(w)})
            readers = sorted({(g.func_of(r) or "").split(":")[-1] for r in g.succ(a, [EK.GLOBAL_READ]) if g.func_of(r)})
            if writers:
                x["쓰는 함수"] = ", ".join(writers[:12]) + (" …" if len(writers) > 12 else "")
            if readers:
                x["읽는 함수"] = ", ".join(readers[:12]) + (" …" if len(readers) > 12 else "")
        elif k == NK.SIGNAL:
            kind = "signal"
            label = d.get("name") or a
            x["종류"] = "CAN 신호" if d.get("can_id") is not None else "RTE 신호"
            for key, name in (("direction", "방향"), ("ecu", "ECU"), ("swc", "SWC"), ("can_id", "CAN ID")):
                if d.get(key) is not None:
                    x[name] = d.get(key)
            if d.get("consumers"):
                x["수신처"] = ", ".join(f"{c.get('ecu') or ''}/{c.get('swc') or ''}" for c in d["consumers"])
        elif k == NK.OBSERVATION:
            kind = "observation"
            label = f"{d.get('name')} ({d.get('feature')})"
            x["기능"] = d.get("feature")
            x["관찰 종류"] = d.get("obs_kind")
            spec = d.get("spec") or {}
            if spec.get("function"):
                x["모델 함수"] = spec.get("function")
        else:
            kind = "function"
            label = a
        out = {"k": kind, "l": label, "f": d.get("file"), "ln": d.get("line")}
        if x:
            out["x"] = {kk: str(vv) for kk, vv in x.items()}
        return out


# ---------------------------------------------------------------------------
# 프로그램 전체(함수·변수·신호 단위) 그래프
# ---------------------------------------------------------------------------
class ProgramGraph:
    """한 버전의 함수 호출·공유 변수·신호·관찰 연결. 간선 키 (출발, 도착, 종류) -> 정보."""

    def __init__(self, g: AnalysisGraph, absr: Abstraction):
        self.g = g
        self.absr = absr
        self.nodes: Set[str] = set()
        self.edges: Dict[Tuple[str, str, str], dict] = {}
        self._build()

    def _add(self, s: str, t: str, kind: str, label: str = "", cond: bool = False) -> None:
        if not s or not t or s == t:
            return
        self.nodes.update((s, t))
        e = self.edges.setdefault((s, t, kind), {"count": 0, "labels": set(), "cond": False})
        e["count"] += 1
        if label:
            e["labels"].add(label)
        e["cond"] = e["cond"] or cond

    def _build(self) -> None:
        g, A = self.g, self.absr
        for f in g.nodes_of(NK.FUNCTION):
            self.nodes.add(f)
        # 호출: 함수 수준 관계(본문 없는 외부 함수 포함) + 호출 지점의 조건부 여부
        cond_calls: Set[Tuple[str, str]] = set()
        for cs in g.nodes_of(NK.CALL_SITE):
            d = g.node(cs)
            if A.conditional(cs) or (d.get("stmt") and g.has_node(d["stmt"]) and A.conditional(d["stmt"])):
                for c in d.get("callees") or []:
                    cond_calls.add((d.get("func"), c))
        for f in g.nodes_of(NK.FUNCTION):
            for c in g.succ(f, [EK.CALLS]):
                self._add(f, c, CALL, "", (f, c) in cond_calls)
        for u, v, data in g.g.edges(data=True):
            k = data.get("kind")
            if k == EK.GLOBAL_WRITE:
                self._add(A.of(u), v, SHARED, "쓰기", A.conditional(u))
            elif k == EK.GLOBAL_READ:
                self._add(u, A.of(v), SHARED, "읽기", A.conditional(v))
            elif k == EK.SIGNAL_WRITE:
                self._add(A.of(u), v, SIGNAL, "송신", A.conditional(u))
            elif k == EK.SIGNAL_READ:
                self._add(u, A.of(v), SIGNAL, "수신", A.conditional(v))
            elif k == EK.OBSERVES:
                self._add(A.of(v), u, OBSERVE, "관찰")

    def has(self, s: str, t: str, kind: str) -> bool:
        if (s, t, kind) in self.edges:
            return True
        return kind == CALL and (t, s, CALL) in self.edges     # 반환값 흐름(피호출 → 호출)도 같은 호출 관계


# ---------------------------------------------------------------------------
# 변경 영향 그래프(변경 하나, 한 버전)
# ---------------------------------------------------------------------------
def _impact_side(g: AnalysisGraph, A: Abstraction, fs, chops: Dict[str, object], seeds: List[str], cid: str
                 ) -> Tuple[Dict[str, dict], Dict[Tuple[str, str, str], dict]]:
    nodes: Dict[str, dict] = {}
    edges: Dict[Tuple[str, str, str], dict] = {}
    S: Set[str] = set(fs.nodes) if fs is not None else set()
    P: Set[str] = set()
    for c in chops.values():
        P |= c.nodes
    S |= P

    def node(a: Optional[str], on_path: bool = False) -> Optional[str]:
        if not a:
            return None
        e = nodes.setdefault(a, {"on_path": False})
        e["on_path"] = e["on_path"] or on_path
        return a

    def edge(s: Optional[str], t: Optional[str], kind: str, label: str, ctrl: bool, on_path: bool) -> None:
        if not s or not t or s == t:
            return
        e = edges.setdefault((s, t, kind), {"count": 0, "labels": set(), "ctrl": False, "on_path": False})
        e["count"] += 1
        if label:
            e["labels"].add(label)
        e["ctrl"] = e["ctrl"] or ctrl
        e["on_path"] = e["on_path"] or on_path

    # 변경의 영향이 조건문을 거쳐 전달되는 노드(슬라이스 안의 조건 문장에 제어 의존)
    ctrl_reached = {n for n in S if any(p in S and g.kind(p) == NK.STMT for p in g.pred(n, [EK.CONTROL]))}
    for n in S:
        node(A.of(n), n in P)
    chg = f"chg:{cid}"
    nodes[chg] = {"on_path": bool(P)}
    for sd in seeds:
        a = node(A.of(sd), sd in P)
        edge(chg, a, CHANGE, "", False, sd in P)
    for n in S:
        an = A.of(n)
        if not an:
            continue
        for _, m, data in g.out_edges(n, _IMPACT_EDGES):
            if m not in S:
                continue
            am = A.of(m)
            k = data.get("kind")
            on_path = n in P and m in P
            ctrl = n in ctrl_reached or k == EK.CONTROL
            if k == EK.INTERFERENCE:
                # 다른 실행 문맥 사이 값 전달: 같은 이름의 전역 위치가 하나면 변수 노드를 거쳐 표시
                locs = A.loc_by_name.get(data.get("label") or "", [])
                if len(locs) == 1:
                    node(locs[0], on_path)
                    edge(an, locs[0], SHARED, "쓰기(간섭)", ctrl, on_path)
                    edge(locs[0], am, SHARED, "읽기(간섭)", False, on_path)
                else:
                    edge(an, am, SHARED, f"간섭: {data.get('label')}", ctrl, on_path)
                continue
            kind = _SDG_KIND.get(k)
            if kind is None or an == am:
                continue
            label = _SDG_LABEL.get(k, "")
            if k == EK.PARAM_IN and data.get("label"):
                label = f"인자 {data.get('label')}"
            edge(an, am, kind, label, ctrl, on_path)
    # 관찰 대상(기능 출력)
    for c in chops.values():
        for a in c.reached_targets():
            for o in g.pred(a, [EK.OBSERVES]):
                node(o, True)
                node(A.of(a), True)
                edge(A.of(a), o, OBSERVE, "관찰", False, True)
    return nodes, edges


def _dijkstra(adj: Dict[str, List[Tuple[str, float]]], sources: Iterable[str], limit: float = 1e9) -> Dict[str, float]:
    dist: Dict[str, float] = {}
    pq = [(0.0, s) for s in sources]
    heapq.heapify(pq)
    while pq:
        d, n = heapq.heappop(pq)
        if n in dist or d > limit:
            continue
        dist[n] = d
        for m, w in adj.get(n, ()):
            if m not in dist:
                heapq.heappush(pq, (d + w, m))
    return dist


# ---------------------------------------------------------------------------
# 전체 데이터
# ---------------------------------------------------------------------------
def build_graph_data(go: AnalysisGraph, gn: AnalysisGraph, results: List[dict], ia, opts=None,
                     before: str = "before", after: str = "after") -> dict:
    """보고서 '영향 그래프' 탭과 내보내기에 쓰는 데이터. ia는 실행을 마친 ImpactAnalyzer."""
    max_steps = float(getattr(opts, "graph_max_steps", 4) or 4)
    node_limit = int(getattr(opts, "graph_node_limit", 600) or 600)
    program_limit = int(getattr(opts, "graph_program_limit", 1500) or 1500)
    Ao, An = Abstraction(go), Abstraction(gn)
    Po, Pn = ProgramGraph(go, Ao), ProgramGraph(gn, An)
    info: Dict[str, dict] = {}

    def ninfo(a: str) -> None:
        if a in info or a.startswith("chg:"):
            return
        if gn.has_node(a):
            info[a] = An.info(a)
        elif go.has_node(a):
            info[a] = Ao.info(a)
        else:
            info[a] = {"k": "function", "l": a.split(":")[-1]}
        i = info[a]
        i["m"] = i.get("f") or ("(신호·외부)" if i["k"] in ("signal", "observation", "external", "register") else "(외부)")
        i["s"] = "added" if not go.has_node(a) else ("removed" if not gn.has_node(a) else "same")

    def estatus(s: str, t: str, kind: str) -> str:
        if kind == CHANGE:
            return "same"
        o, n = Po.has(s, t, kind), Pn.has(s, t, kind)
        if n and not o:
            return "added"
        if o and not n:
            return "removed"
        return "same"

    risk: Dict[str, int] = {}
    changes_out: List[dict] = []
    impact: Dict[str, dict] = {}
    changed_nodes: Dict[str, List[str]] = {}
    for ch in results:
        cid = ch["id"]
        sl = ia.slices.get(cid) or {}
        chops = ia.chops.get(cid) or {}
        merged_n: Dict[str, dict] = {}
        merged_e: Dict[Tuple[str, str, str], dict] = {}
        seeds_abs: Set[str] = set()
        for side, g, A in (("old", go, Ao), ("new", gn, An)):
            seeds = (sl.get("seeds") or {}).get(side) or []
            side_chops = {f: c for (sd, f), c in chops.items() if sd == side}
            if not seeds and not side_chops:
                continue
            seeds_abs |= {A.of(s) for s in seeds if A.of(s)}
            ns, es = _impact_side(g, A, sl.get(side), side_chops, seeds, cid)
            bit = 1 if side == "old" else 2
            for a, d in ns.items():
                m = merged_n.setdefault(a, {"on_path": False, "side": 0})
                m["on_path"] |= d["on_path"]
                m["side"] |= bit
            for key, d in es.items():
                m = merged_e.setdefault(key, {"count": 0, "labels": set(), "ctrl": False, "on_path": False, "side": 0})
                m["count"] = max(m["count"], d["count"])
                m["labels"] |= d["labels"]
                m["ctrl"] |= d["ctrl"]
                m["on_path"] |= d["on_path"]
                m["side"] |= bit
        # 변경 함수 단위 대상(함수 그래프의 중심): 변경 단위 + 시작점이 속한 함수·변수
        centers = set(seeds_abs)
        for u in (ch.get("unit_old"), ch.get("unit_new")):
            for g in (go, gn):
                if not u or not g.has_node(u):
                    continue
                k = g.kind(u)
                if k == NK.FUNCTION:
                    centers.add(u)
                elif k == NK.TASK:          # 태스크·ISR 설정 변경: 그 문맥에서 실행되는 함수
                    centers |= set(g.succ(u, [EK.ACTIVATES]))
                elif k == NK.MACRO:         # 매크로 변경: 그 매크로를 쓰는 함수
                    centers |= {g.func_of(x) or (x if g.kind(x) == NK.FUNCTION else None)
                                for x in g.pred(u, [EK.USES_MACRO])} - {None}
                elif k in (NK.RECORD, NK.TYPEDEF):   # 자료형 변경: 그 자료형을 쓰는 함수
                    centers |= {x for x in g.pred(u, [EK.USES_TYPE]) if g.kind(x) == NK.FUNCTION}
                elif k == NK.GLOBAL:        # 전역 변수 선언·초기값 변경: 그 변수의 메모리 위치
                    centers |= {l for l in g.nodes_of(NK.LOCATION) if g.node(l).get("root") == u}
        centers = {c for c in centers if not c.startswith("obs:")}
        changed_nodes[cid] = sorted(centers)
        chg = f"chg:{cid}"
        merged_n.setdefault(chg, {"on_path": False, "side": 3})
        crisk: Dict[str, int] = {}
        # 근거 경로(보고서의 경로 카드와 같은 경로)를 표시 단위 노드 열로
        paths = []
        for fi in ch["impact"]["features"]:
            lvl = _RISK_OF_STATUS.get(fi["status"], 0)
            for side, sd in (fi.get("sides") or {}).items():
                A = Ao if side == "old" else An
                for i, p in enumerate(sd.get("paths") or []):
                    seq: List[str] = [chg]
                    for stp in p.get("steps") or []:
                        a = A.of(stp.get("node") or "")
                        if a and seq[-1] != a:
                            seq.append(a)
                    gg = go if side == "old" else gn
                    for o in gg.pred(p.get("anchor") or "", [EK.OBSERVES]):
                        if gg.node(o).get("feature") == fi["feature"]:
                            seq.append(o)
                    paths.append({"card": f"{cid}-{fi['feature']}", "feature": fi["feature"], "side": side,
                                  "status": fi["status"], "obs": p.get("observation"), "nodes": seq})
            # 위험 후보: 기능 출력까지 가는 근거 chop 위의 요소(기능 판정에 따른 수준)
            for side in (fi.get("sides") or {}):
                c = chops.get((side, fi["feature"]))
                if c is None or not lvl:
                    continue
                A = Ao if side == "old" else An
                for n in c.nodes:
                    a = A.of(n)
                    if a and a not in centers:
                        crisk[a] = max(crisk.get(a, 0), lvl)
                for a in c.reached_targets():
                    for o in (go if side == "old" else gn).pred(a, [EK.OBSERVES]):
                        crisk[o] = max(crisk.get(o, 0), lvl)
        for a, lvl in crisk.items():
            risk[a] = max(risk.get(a, 0), lvl)
        # 단계(거리): 변경 노드에서 영향 방향으로
        adj: Dict[str, List[Tuple[str, float]]] = defaultdict(list)
        for (s, t, k) in merged_e:
            adj[s].append((t, STEP_COST[k]))
        dist = _dijkstra(adj, [chg])
        keep = set(merged_n)
        truncated = False
        if len(keep) > node_limit:
            # 상한 초과: 변경·변경 대상·출력·보고서 근거 경로 위 노드는 항상, 그다음 출력 경로 위 노드, 나머지 순(가까운 순)
            truncated = True
            must = {chg} | (centers & keep) | {a for a in keep if (info.get(a) or {}).get("k") == "observation"
                                               or a.startswith(("obs:", "sig:"))}
            for p in paths:
                must |= set(p["nodes"]) & keep
            by_dist = lambda a: (dist.get(a, 1e9), a)
            onp = sorted((a for a in keep if merged_n[a]["on_path"] and a not in must), key=by_dist)
            rest = sorted((a for a in keep if not merged_n[a]["on_path"] and a not in must), key=by_dist)
            room = max(0, node_limit - len(must))
            keep = must | set(onp[:room]) | set(rest[: max(0, room - len(onp))])
        for a in keep:
            ninfo(a)
        impact[cid] = {
            "n": [[a, round(dist.get(a, 0.0), 2) if a in dist else None, int(d["on_path"]), d["side"], crisk.get(a, 0)]
                  for a, d in sorted(merged_n.items()) if a in keep],
            "e": [[s, t, k, int(d["ctrl"]), int(d["on_path"]), d["side"], d["count"],
                   ", ".join(sorted(d["labels"]))[:80], estatus(s, t, k)]
                  for (s, t, k), d in sorted(merged_e.items()) if s in keep and t in keep],
            "paths": paths, "truncated": truncated, "total_nodes": len(merged_n),
        }
        old, new = ch.get("old") or {}, ch.get("new") or {}
        changes_out.append({"id": cid, "type": ch.get("type_label") or ch.get("type"), "summary": ch.get("summary"),
                            "unit": ch.get("unit_name"), "file": new.get("file") or old.get("file"),
                            "line": new.get("line") or old.get("line"), "centers": changed_nodes[cid],
                            "features": [{"feature": fi["feature"], "status": fi["status"]}
                                         for fi in ch["impact"]["features"]],
                            "code_old": (old.get("code") or "")[:400], "code_new": (new.get("code") or "")[:400]})
    for a in risk:
        ninfo(a)

    # ---- 함수 호출·데이터 의존 그래프: 변경 대상 주변 max_steps 단계(두 버전 합집합) ----
    pe: Dict[Tuple[str, str, str], dict] = {}
    for P, bit in ((Po, 1), (Pn, 2)):
        for key, d in P.edges.items():
            m = pe.setdefault(key, {"side": 0, "cond": False, "count": 0, "labels": set()})
            m["side"] |= bit
            m["cond"] |= d["cond"]
            m["count"] = max(m["count"], d["count"])
            m["labels"] |= d["labels"]
    uadj: Dict[str, List[Tuple[str, float]]] = defaultdict(list)
    for (s, t, k) in pe:
        uadj[s].append((t, STEP_COST[k]))
        uadj[t].append((s, STEP_COST[k]))
    all_centers = sorted({c for cs in changed_nodes.values() for c in cs})
    pdist = _dijkstra(uadj, all_centers, limit=max_steps)
    pkeep = sorted(pdist, key=lambda a: (pdist[a], a))[:program_limit]
    pset = set(pkeep)
    for a in pset:
        ninfo(a)
    program = {
        "n": sorted(pset),
        "e": [[s, t, k, int(d["cond"]), d["side"], d["count"], ", ".join(sorted(d["labels"]))[:60],
               "added" if d["side"] == 2 else ("removed" if d["side"] == 1 else "same")]
              for (s, t, k), d in sorted(pe.items()) if s in pset and t in pset],
        "max_steps": max_steps, "truncated": len(pdist) > program_limit, "reachable": len(pdist),
    }

    # ---- 모듈 의존 그래프(파일 / 폴더) ----
    modules = {"file": _module_graph(Po, Pn, go, gn, lambda f: f or "(외부)", all_centers, risk),
               "folder": _module_graph(Po, Pn, go, gn, _folder, all_centers, risk)}

    return {"version": 1, "cytoscape": CYTOSCAPE_VERSION, "before": before, "after": after,
            "nodes": info, "changes": changes_out, "impact": impact, "program": program, "modules": modules,
            "risk": risk, "step_cost": STEP_COST, "max_steps": max_steps,
            "cluster_threshold": int(getattr(opts, "graph_cluster_threshold", 150) or 150)}


def _module_graph(Po: ProgramGraph, Pn: ProgramGraph, go: AnalysisGraph, gn: AnalysisGraph, key_of, centers,
                  risk: Dict[str, int]) -> dict:
    """모듈 간 호출 수(서로 다른 함수 쌍)·공유 변수 수(쓰는 모듈 → 읽는 모듈, 변수 개수). 버전별 값."""
    def file_of(g: AnalysisGraph, a: str) -> Optional[str]:
        return g.node(a).get("file") if g.has_node(a) else None

    stats: Dict[Tuple[str, str], dict] = {}
    mods: Dict[str, dict] = {}
    for P, g, side in ((Po, go, "old"), (Pn, gn, "new")):
        funcs = [f for f in g.nodes_of(NK.FUNCTION) if not g.node(f).get("external")]
        for f in funcs:
            m = key_of(file_of(g, f))
            e = mods.setdefault(m, {"old": 0, "new": 0, "changed": False, "risk": 0})
            e[side] += 1
        calls: Dict[Tuple[str, str], Set[Tuple[str, str]]] = defaultdict(set)
        cond_calls: Dict[Tuple[str, str], int] = defaultdict(int)
        writers: Dict[str, Set[str]] = defaultdict(set)
        readers: Dict[str, Set[str]] = defaultdict(set)
        for (s, t, k), d in P.edges.items():
            if k == CALL and g.has_node(s) and g.has_node(t) and not g.node(t).get("external"):
                ms, mt = key_of(file_of(g, s)), key_of(file_of(g, t))
                if ms != mt:
                    calls[(ms, mt)].add((s, t))
                    if d["cond"]:
                        cond_calls[(ms, mt)] += 1
            elif k == SHARED and g.has_node(s) and g.kind(s) == NK.FUNCTION:
                writers[t].add(key_of(file_of(g, s)))
            elif k == SHARED and g.has_node(t) and g.kind(t) == NK.FUNCTION:
                readers[s].add(key_of(file_of(g, t)))
        shared: Dict[Tuple[str, str], Set[str]] = defaultdict(set)
        for loc, ws in writers.items():
            for w in ws:
                for r in readers.get(loc, ()):
                    if w != r:
                        shared[(w, r)].add(loc)
        for pair in set(calls) | set(shared):
            e = stats.setdefault(pair, {"calls_old": 0, "calls_new": 0, "shared_old": 0, "shared_new": 0,
                                        "cond": 0, "vars": set()})
            e[f"calls_{side}"] = len(calls.get(pair, ()))
            e[f"shared_{side}"] = len(shared.get(pair, ()))
            e["cond"] = max(e["cond"], cond_calls.get(pair, 0))
            e["vars"] |= {loc_display((g.node(v).get("loc") or v[4:])) for v in shared.get(pair, ())}
    for a in centers:
        f = file_of(gn, a) or file_of(go, a)
        if f:
            mods.setdefault(key_of(f), {"old": 0, "new": 0, "changed": False, "risk": 0})["changed"] = True
    for a, lvl in risk.items():
        f = file_of(gn, a) or file_of(go, a)
        if f and key_of(f) in mods:
            mods[key_of(f)]["risk"] = max(mods[key_of(f)]["risk"], lvl)
    for (s, t) in stats:
        for x in (s, t):
            mods.setdefault(x, {"old": 0, "new": 0, "changed": False, "risk": 0})
    edges = []
    for (s, t), e in sorted(stats.items()):
        o = e["calls_old"] + e["shared_old"]
        n = e["calls_new"] + e["shared_new"]
        st = "added" if o == 0 else ("removed" if n == 0 else ("changed" if (e["calls_old"], e["shared_old"]) !=
                                                                (e["calls_new"], e["shared_new"]) else "same"))
        edges.append([s, t, e["calls_old"], e["calls_new"], e["shared_old"], e["shared_new"], e["cond"], st,
                      ", ".join(sorted(e["vars"]))[:120]])
    nodes = [[m, d["old"], d["new"], int(d["changed"]), d["risk"],
              "added" if d["old"] == 0 and d["new"] else ("removed" if d["new"] == 0 and d["old"] else "same")]
             for m, d in sorted(mods.items())]
    return {"n": nodes, "e": edges}


# ---------------------------------------------------------------------------
# 내보내기(DOT, GraphML): 다른 도구(Graphviz, Gephi, yEd)에서 열기용
# ---------------------------------------------------------------------------
_COLORS = {"added": "#2e8b57", "removed": "#c62828", "ctrl": "#7b3fbf", "edge": "#7a7a75", "change": "#c62828",
           "risk2": "#e65100", "risk1": "#f2a900"}


def _dot_id(s: str) -> str:
    return '"' + str(s).replace("\\", "\\\\").replace('"', '\\"') + '"'


def _shape(kind: str) -> str:
    return {"change": "octagon", "function": "box", "external": "box", "variable": "ellipse", "register": "hexagon",
            "signal": "cds", "observation": "doubleoctagon", "module": "folder"}.get(kind, "box")


def export_views(data: dict) -> Dict[str, Tuple[List[dict], List[dict]]]:
    """내보낼 그래프들: 이름 -> (노드 목록, 간선 목록). 각 항목은 평평한 속성 사전."""
    info = data["nodes"]
    views: Dict[str, Tuple[List[dict], List[dict]]] = {}
    chg_by_id = {c["id"]: c for c in data["changes"]}
    for cid, im in data["impact"].items():
        ns, es = [], []
        for a, dist, on_path, side, crisk in im["n"]:
            if a.startswith("chg:"):
                c = chg_by_id.get(cid, {})
                ns.append({"id": a, "label": f"{cid} {c.get('type') or ''}", "kind": "change", "changed": True,
                           "risk": 0, "on_path": bool(on_path), "step": dist or 0.0, "file": c.get("file") or "",
                           "status": "same"})
                continue
            i = info.get(a, {})
            ns.append({"id": a, "label": i.get("l", a), "kind": i.get("k", ""), "changed": a in chg_by_id[cid]["centers"],
                       "risk": crisk if on_path else 0, "on_path": bool(on_path),
                       "step": dist if dist is not None else -1.0, "file": i.get("f") or "", "status": i.get("s", "same")})
        for s, t, k, ctrl, on_path, side, cnt, lab, st in im["e"]:
            es.append({"source": s, "target": t, "kind": k, "control": bool(ctrl), "on_path": bool(on_path),
                       "count": cnt, "label": lab, "status": st})
        views[f"impact_{cid}"] = (ns, es)
    ns = []
    for a in data["program"]["n"]:
        i = info.get(a, {})
        ns.append({"id": a, "label": i.get("l", a), "kind": i.get("k", ""), "file": i.get("f") or "",
                   "changed": any(a in c["centers"] for c in data["changes"]), "risk": data["risk"].get(a, 0),
                   "status": i.get("s", "same")})
    es = [{"source": s, "target": t, "kind": k, "control": bool(c), "count": cnt, "label": lab, "status": st}
          for s, t, k, c, side, cnt, lab, st in data["program"]["e"]]
    views["functions"] = (ns, es)
    for gran, mg in data["modules"].items():
        ns = [{"id": m, "label": m, "kind": "module", "functions_old": o, "functions_new": n, "changed": bool(ch),
               "risk": r, "status": st} for m, o, n, ch, r, st in mg["n"]]
        es = [{"source": s, "target": t, "kind": "module", "calls_old": co, "calls_new": cn, "shared_old": so,
               "shared_new": sn, "control": bool(cond), "status": st, "label": f"호출 {cn} · 공유 변수 {sn}",
               "variables": vars_} for s, t, co, cn, so, sn, cond, st, vars_ in mg["e"]]
        views[f"modules_{gran}"] = (ns, es)
    return views


def to_dot(name: str, nodes: List[dict], edges: List[dict]) -> str:
    lines = [f"digraph {_dot_id(name)} {{", "  graph [rankdir=LR, fontname=\"Malgun Gothic\", overlap=false];",
             "  node [fontname=\"Malgun Gothic\", fontsize=10, style=filled, fillcolor=\"#ffffff\"];",
             "  edge [fontname=\"Malgun Gothic\", fontsize=9];"]
    for n in nodes:
        attrs = {"label": n.get("label") or n["id"], "shape": _shape(n.get("kind", "")), "color": "#555555"}
        if n.get("changed"):
            attrs.update(fillcolor="#ffd6d6", color=_COLORS["change"], penwidth="2.5")
        elif n.get("risk"):
            attrs.update(color=_COLORS["risk2" if n["risk"] >= 2 else "risk1"], penwidth="2.5")
        if n.get("status") == "added":
            attrs.update(style="filled,dashed", color=_COLORS["added"])
        elif n.get("status") == "removed":
            attrs.update(style="filled,dashed", color=_COLORS["removed"])
        extra = {k: v for k, v in n.items() if k not in ("id", "label")}
        attrs.update({f"vfia_{k}": v for k, v in extra.items()})
        lines.append(f"  {_dot_id(n['id'])} [" + ", ".join(f"{k}={_dot_id(v)}" for k, v in attrs.items()) + "];")
    for e in edges:
        k = e.get("kind")
        color = _COLORS["ctrl"] if e.get("control") else _COLORS["edge"]
        if e.get("status") == "added":
            color = _COLORS["added"]
        elif e.get("status") == "removed":
            color = _COLORS["removed"]
        attrs = {"style": "dashed" if k in (SHARED, SIGNAL, OBSERVE) else ("bold" if k == CHANGE else "solid"),
                 "color": color, "label": e.get("label") or ""}
        if k == CHANGE:
            attrs["color"] = _COLORS["change"]
        attrs.update({f"vfia_{kk}": v for kk, v in e.items() if kk not in ("source", "target", "label")})
        lines.append(f"  {_dot_id(e['source'])} -> {_dot_id(e['target'])} ["
                     + ", ".join(f"{kk}={_dot_id(v)}" for kk, v in attrs.items()) + "];")
    lines.append("}")
    return "\n".join(lines) + "\n"


def to_graphml(name: str, nodes: List[dict], edges: List[dict]) -> str:
    from xml.sax.saxutils import escape, quoteattr

    def typ(v) -> str:
        if isinstance(v, bool):
            return "boolean"
        if isinstance(v, int):
            return "int"
        if isinstance(v, float):
            return "double"
        return "string"
    nkeys: Dict[str, str] = {}
    ekeys: Dict[str, str] = {}
    for n in nodes:
        for k, v in n.items():
            if k != "id":
                nkeys.setdefault(k, typ(v))
    for e in edges:
        for k, v in e.items():
            if k not in ("source", "target"):
                ekeys.setdefault(k, typ(v))
    out = ['<?xml version="1.0" encoding="UTF-8"?>',
           '<graphml xmlns="http://graphml.graphdrawing.org/xmlns" '
           'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
           'xsi:schemaLocation="http://graphml.graphdrawing.org/xmlns '
           'http://graphml.graphdrawing.org/xmlns/1.0/graphml.xsd">']
    for k, t in nkeys.items():
        out.append(f'  <key id={quoteattr("n_" + k)} for="node" attr.name={quoteattr(k)} attr.type="{t}"/>')
    for k, t in ekeys.items():
        out.append(f'  <key id={quoteattr("e_" + k)} for="edge" attr.name={quoteattr(k)} attr.type="{t}"/>')
    out.append(f'  <graph id={quoteattr(name)} edgedefault="directed">')

    def val(v) -> str:
        if isinstance(v, bool):
            return "true" if v else "false"
        return escape("" if v is None else str(v))
    for n in nodes:
        out.append(f"    <node id={quoteattr(n['id'])}>" + "".join(
            f'<data key={quoteattr("n_" + k)}>{val(v)}</data>' for k, v in n.items() if k != "id") + "</node>")
    for i, e in enumerate(edges):
        out.append(f"    <edge id=\"e{i}\" source={quoteattr(e['source'])} target={quoteattr(e['target'])}>" + "".join(
            f'<data key={quoteattr("e_" + k)}>{val(v)}</data>' for k, v in e.items()
            if k not in ("source", "target")) + "</edge>")
    out += ["  </graph>", "</graphml>"]
    return "\n".join(out) + "\n"


def write_exports(data: dict, out_dir: str) -> List[str]:
    """out_dir에 그래프별 .dot/.graphml 파일을 쓴다. 쓴 파일 목록(out_dir 기준 상대 경로)."""
    os.makedirs(out_dir, exist_ok=True)
    written = []
    for name, (ns, es) in export_views(data).items():
        for ext, fn in (("dot", to_dot), ("graphml", to_graphml)):
            p = os.path.join(out_dir, f"{name}.{ext}")
            with open(p, "w", encoding="utf-8", newline="\n") as f:
                f.write(fn(name, ns, es))
            written.append(f"{name}.{ext}")
    return written


# ---------------------------------------------------------------------------
# 보고서 HTML 조각
# ---------------------------------------------------------------------------
def _read(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def _script_safe(text: str) -> str:
    """<script> 안에 넣어도 HTML 파서가 스크립트를 일찍 끝내지 않도록(JS·JSON 의미는 그대로)."""
    import re
    return re.sub(r"</(script)", r"<\\/\1", text, flags=re.I).replace("<!--", "<\\!--")


def _json_safe(text: str) -> str:
    return text.replace("</", "<\\/").replace("<!--", "<\\u0021--")


def graph_tab_html(data: dict, export_dir_rel: Optional[str] = None, exported: Optional[List[str]] = None) -> str:
    """'영향 그래프' 탭 본문(라이브러리·데이터·스크립트 포함, 인터넷 없이 동작)."""
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    lib = _read(os.path.join(VENDOR_DIR, "cytoscape.min.js"))
    js = _read(os.path.join(ASSET_DIR, "impact_graph.js"))
    body = _read(os.path.join(ASSET_DIR, "impact_graph.html"))
    files = ""
    if exported:
        groups: Dict[str, List[str]] = defaultdict(list)
        for f in exported:
            groups[f.rsplit(".", 1)[0]].append(f)
        files = "".join(f"<li><code>{name}</code> " + " ".join(
            f"<a href='{export_dir_rel}/{f}' download>{f.rsplit('.', 1)[1].upper()}</a>" for f in sorted(fs)) + "</li>"
            for name, fs in sorted(groups.items()))
        files = (f"<details class='ig-files'><summary>전체 그래프 파일({export_dir_rel}/, 다른 도구에서 열기)</summary>"
                 f"<ul>{files}</ul></details>")
    return (body.replace("<!--EXPORT_FILES-->", files)
            + f"<script id='ig-data' type='application/json'>{_json_safe(payload)}</script>"
            + f"<script>/* Cytoscape.js {CYTOSCAPE_VERSION} (MIT) */\n{_script_safe(lib)}</script>"
            + f"<script>{_script_safe(js)}</script>")
