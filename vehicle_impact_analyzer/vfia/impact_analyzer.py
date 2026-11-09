"""변경 영향 분석: 변경점의 전방 슬라이스 ∩ 기능 출력의 후방 슬라이스.

* 변경 전 그래프와 변경 후 그래프에서 각각 계산한다(간선을 합치지 않는다).
  - 삭제·수정: 변경 전 그래프에서 기존 영향 경로를 찾는다.
  - 추가·수정: 변경 후 그래프에서 새 영향 경로를 찾는다.
  - 삭제로 '도달 정의가 바뀐' 사용 지점은 변경 전 그래프의 1단계 의존 노드를 대응표로
    변경 후 그래프에 옮겨 추가 시작점으로 사용한다.
* 근거 경로: 2단계(HRB) 전방 슬라이스의 부모 기록으로 실현 가능한 최단 경로를 재구성.
* 결과 상태: 정적 영향 후보 / 현재 분석 범위에서 영향 경로 미발견 / 분석 불완전으로 판정 유보.
  (모델·실행 기반 상태는 behavior_checker가 갱신한다.)
"""
from __future__ import annotations

from collections import defaultdict
from typing import Dict, List, Optional, Set, Tuple

from .change_detector import ChangeDetector
from .model import EK, NK, AnalysisGraph, Status, loc_display
from .slicing import SliceResult, backward_slice, forward_slice

_FIRST_HOP = {EK.DATA, EK.GLOBAL_WRITE, EK.CONTROL}


class ImpactAnalyzer:
    def __init__(self, g_old: AnalysisGraph, g_new: AnalysisGraph, cd: ChangeDetector, changes: List[dict],
                 feature_maps: Optional[Dict[str, dict]] = None, max_paths: int = 3):
        self.go, self.gn = g_old, g_new
        self.cd = cd
        self.changes = changes
        self.max_paths = max_paths
        self.feature_maps = feature_maps or {}
        self._bs: Dict[Tuple[str, str], Optional[SliceResult]] = {}
        self.inv_map = {v: k for k, v in cd.node_map.items()}
        self.incomplete = {"old": self._incomplete(g_old), "new": self._incomplete(g_new)}

    @staticmethod
    def _incomplete(g: AnalysisGraph) -> Dict[str, List[Tuple[str, bool]]]:
        """노드 -> [(사유, 경로를 숨길 수 있는가)]"""
        out: Dict[str, List[Tuple[str, bool]]] = defaultdict(list)
        for d in g.diagnostics:
            if d.get("code") == "unresolved_indirect_call" and d.get("node"):
                out[d["node"]].append((d["message"], True))
            elif d.get("code") == "incomplete" and d.get("node"):
                hide = "쓰기" in d["message"] or "어셈블리" in d["message"]
                out[d["node"]].append((d["message"], hide))
        for cs in g.nodes_of(NK.CALL_SITE):
            dd = g.node(cs)
            if dd.get("external"):
                names = ",".join(c.split(":")[-1] for c in dd.get("callees") or []) or "?"
                # 함수 포인터 인자(콜백 등록)는 외부에서 호출될 수 있어 경로를 숨길 수 있음
                fp_arg = False
                ast = g.asts.get(dd.get("func"), {})
                for n in _walk(ast):
                    if n.get("id") == dd.get("ast_id"):
                        for a in (n.get("ch") or [])[1:]:
                            ty = a.get("ty") or {}
                            if ty.get("k") == "p" and (ty.get("pt") or {}).get("k") == "f":
                                fp_arg = True
                out[cs].append((f"본문 없는 외부 함수 호출({names}): 보수적 요약(인자→결과, 포인터 인자 대상 쓰기) 사용"
                                + (" — 함수 포인터 전달(콜백 등록 가능)" if fp_arg else ""), fp_arg))
        return out

    # ------------------------------------------------------------------
    def features(self) -> List[str]:
        fs = set(self.gn.nodes_of(NK.FEATURE)) | set(self.go.nodes_of(NK.FEATURE))
        return sorted(fs)

    def anchors(self, g: AnalysisGraph, feat: str) -> List[str]:
        if not g.has_node(feat):
            return []
        out = []
        for o in g.succ(feat, [EK.HAS_OBSERVATION]):
            out += g.succ(o, [EK.OBSERVES])
        return sorted(set(out))

    def backward(self, side: str, feat: str) -> Optional[SliceResult]:
        key = (side, feat)
        if key not in self._bs:
            g = self.go if side == "old" else self.gn
            a = self.anchors(g, feat)
            self._bs[key] = backward_slice(g, a) if a else None
        return self._bs[key]

    def seeds(self, ch: dict) -> Tuple[List[str], List[str], List[dict]]:
        so = [a for a in ch["old"].get("anchors", []) if self.go.has_node(a)]
        sn = [a for a in ch["new"].get("anchors", []) if self.gn.has_node(a)]
        derived: List[dict] = []
        # 삭제된(대응 없는) 변경 전 노드의 1단계 의존 노드 -> 변경 후 그래프의 대응 노드
        for a in so:
            if a in self.cd.node_map:
                continue
            for _, d, data in self.go.out_edges(a, _FIRST_HOP):
                tgt = self.cd.node_map.get(d)
                if tgt and self.gn.has_node(tgt) and tgt not in sn:
                    sn.append(tgt)
                    derived.append({"from_old": a, "old_dependent": d, "new_seed": tgt, "edge": data["kind"],
                                    "label": data.get("label"),
                                    "reason": "삭제된 코드에 의존하던 사용 지점(변경 후 도달 정의·실행 여부가 달라짐)"})
        # 추가된(대응 없는) 변경 후 노드도 동일하게 변경 전 쪽 의존 지점을 표시(설명용)
        for a in sn:
            if a in self.inv_map:
                continue
            for _, d, data in self.gn.out_edges(a, _FIRST_HOP):
                src = self.inv_map.get(d)
                if src and self.go.has_node(src) and src not in so:
                    so.append(src)
                    derived.append({"from_new": a, "new_dependent": d, "old_seed": src, "edge": data["kind"],
                                    "label": data.get("label"),
                                    "reason": "추가된 코드가 영향을 주는 기존 사용 지점(변경 전 경로와 비교용)"})
        return sorted(set(so)), sorted(set(sn)), derived

    def fmt_path(self, g: AnalysisGraph, path: List[Tuple[str, str, str]]) -> List[dict]:
        out = []
        for n, ek, lab in path:
            d = g.node(n) if g.has_node(n) else {}
            k = d.get("kind")
            if k == NK.LOCATION:
                text = f"전역 상태 {d.get('name')}"
            elif k == NK.SIGNAL:
                text = f"통신 신호 {d.get('name')}"
            else:
                text = d.get("text") or g.describe(n)
            out.append({"node": n, "kind": k, "func": (d.get("func") or "").split(":")[-1] or None,
                        "file": d.get("file"), "line": d.get("line"), "text": text, "via": ek, "label": lab})
        return out

    def slice_summary(self, g: AnalysisGraph, sl: SliceResult) -> dict:
        funcs: Dict[str, int] = defaultdict(int)
        globs, sigs, tasks = set(), set(), set()
        for n in sl.nodes:
            f = g.func_of(n)
            if f:
                funcs[f] += 1
            k = g.kind(n)
            if k == NK.LOCATION:
                globs.add(loc_display(g.node(n).get("loc") or g.node(n).get("root") or n[4:]))
            elif k == NK.SIGNAL:
                sigs.add(g.node(n).get("name"))
        for f in funcs:
            for t in g.succ(f, [EK.RUNS_IN]):
                tasks.add(g.node(t).get("name"))
        return {"nodes": len(sl), "functions": sorted(x.split(":")[-1] for x in funcs),
                "globals": sorted(globs), "signals": sorted(sigs), "contexts": sorted(tasks)}

    def contexts_and_sharing(self, g: AnalysisGraph, nodes: Set[str]) -> dict:
        ctx_of: Dict[str, Set[str]] = defaultdict(set)
        funcs = {g.func_of(n) for n in nodes if g.func_of(n)}
        for f in funcs:
            for t in g.succ(f, [EK.RUNS_IN]):
                ctx_of[f].add(t)
        shared = []
        for n in nodes:
            if g.kind(n) != NK.LOCATION:
                continue
            w_ctx, r_ctx = set(), set()
            for w in g.pred(n, [EK.GLOBAL_WRITE]):
                for t in g.succ(g.func_of(w) or "", [EK.RUNS_IN]) if g.func_of(w) else []:
                    w_ctx.add(t)
            for r in g.succ(n, [EK.GLOBAL_READ]):
                for t in g.succ(g.func_of(r) or "", [EK.RUNS_IN]) if g.func_of(r) else []:
                    r_ctx.add(t)
            if w_ctx and r_ctx and (w_ctx != r_ctx or len(w_ctx | r_ctx) > 1):
                info = []
                for t in sorted(w_ctx | r_ctx):
                    d = g.node(t)
                    info.append(f"{d.get('name')}({d.get('ctx_kind')}, 주기 {d.get('period_ms')}ms, 우선순위 {d.get('priority')})")
                shared.append({"location": g.node(n).get("name"), "writers": sorted(g.node(t)["name"] for t in w_ctx),
                               "readers": sorted(g.node(t)["name"] for t in r_ctx), "contexts": info,
                               "volatile": bool(g.node(n).get("volatile"))})
        contexts = sorted({g.node(t).get("name") for ts in ctx_of.values() for t in ts})
        return {"contexts": contexts, "shared_state": shared}

    # ------------------------------------------------------------------
    def analyze_change(self, ch: dict) -> dict:
        so, sn, derived = self.seeds(ch)
        fs_o = forward_slice(self.go, so) if so else None
        fs_n = forward_slice(self.gn, sn) if sn else None
        res = {
            "seeds": {"old": so, "new": sn}, "derived_seeds": derived,
            "slice": {"old": self.slice_summary(self.go, fs_o) if fs_o else None,
                      "new": self.slice_summary(self.gn, fs_n) if fs_n else None},
            "features": [],
        }
        incomplete_hits = []
        for side, fs in (("old", fs_o), ("new", fs_n)):
            if fs is None:
                continue
            for n in fs.nodes:
                if n in self.incomplete[side]:
                    incomplete_hits.append({"side": side, "node": n,
                                            "reasons": [r for r, _ in self.incomplete[side][n]],
                                            "may_hide": any(h for _, h in self.incomplete[side][n])})
        res["incomplete_in_slice"] = incomplete_hits[:30]
        for feat in self.features():
            entry = self._feature_impact(ch, feat, fs_o, fs_n, incomplete_hits)
            if entry:
                res["features"].append(entry)
        return res

    def _feature_impact(self, ch: dict, feat: str, fs_o, fs_n, incomplete_hits) -> Optional[dict]:
        g_any = self.gn if self.gn.has_node(feat) else self.go
        fd = g_any.node(feat)
        per_side = {}
        found = False
        chop_nodes: Dict[str, Set[str]] = {}
        for side, g, fs in (("old", self.go, fs_o), ("new", self.gn, fs_n)):
            if fs is None or not g.has_node(feat):
                continue
            anchors = self.anchors(g, feat)
            reached = [a for a in anchors if a in fs]
            if not reached:
                continue
            found = True
            bs = self.backward(side, feat)
            chop = (fs.nodes & bs.nodes) if bs else set()
            chop_nodes[side] = chop
            paths = []
            for a in sorted(reached, key=lambda x: len(fs.path_to(x)))[: self.max_paths]:
                p = fs.path_to(a)
                obs = [g.node(o).get("name") for o in g.pred(a, [EK.OBSERVES])]
                paths.append({"observation": obs, "anchor": a, "length": len(p), "steps": self.fmt_path(g, p)})
            ctx = self.contexts_and_sharing(g, chop)
            per_side[side] = {
                "reached_observations": sorted({g.node(o).get("name") for a in reached for o in g.pred(a, [EK.OBSERVES])}),
                "paths": paths, "chop_size": len(chop),
                "functions": sorted({(g.func_of(n) or "").split(":")[-1] for n in chop if g.func_of(n)}),
                "contexts": ctx["contexts"], "shared_state": ctx["shared_state"],
            }
        # 기능 분류 결과에서 변경 단위의 역할(보조 근거)
        roles = []
        for side, unit in (("old", ch.get("unit_old")), ("new", ch.get("unit_new"))):
            fmap = self.feature_maps.get(side)
            if not fmap or not unit:
                continue
            for f in fmap["features"]:
                if f["node"] != feat:
                    continue
                for x in f["elements"]:
                    if x["element"] == unit:
                        roles.append({"side": side, "role": x["role"], "status": x["status"]})
        timing = None
        if ch["category"] == "timing":
            timing = self._timing_impact(ch, feat)
            if timing:
                found = True
        hiding = [h for h in incomplete_hits if h.get("may_hide")]
        direct_role = any(r["role"] == "direct" for r in roles)
        if not found and not (hiding and roles) and not direct_role:
            return None
        if found:
            status = Status.STATIC_CANDIDATE
        else:
            # 경로를 숨길 수 있는 미해결 영역이 있으면 판정 유보, 아니면 '현재 범위에서 경로 미발견'
            status = Status.INCONCLUSIVE if hiding else Status.NO_PATH
        return {
            "feature": fd.get("fid"), "feature_name": fd.get("name"), "status": status,
            "static_status": status, "sides": per_side, "unit_roles": roles, "timing": timing,
            "behavior": None,
        }

    def _timing_impact(self, ch: dict, feat: str) -> Optional[dict]:
        t = ch.get("unit_new") or ch.get("unit_old")
        g = self.gn if self.gn.has_node(feat) else self.go
        if not t or not g.has_node(t):
            return None
        funcs = set(g.pred(t, [EK.RUNS_IN]))
        bs = self.backward("new" if g is self.gn else "old", feat)
        if not bs:
            return None
        involved = sorted({(g.func_of(n) or "").split(":")[-1] for n in bs.nodes if g.func_of(n) in funcs})
        if not involved:
            return None
        written = set()
        for n in bs.nodes:
            if g.kind(n) == NK.STMT and g.func_of(n) in funcs:
                for _, l, _ in g.out_edges(n, [EK.GLOBAL_WRITE]):
                    if l in bs.nodes:
                        written.add(g.node(l).get("name"))
        return {"functions": involved, "updated_state": sorted(written),
                "note": "기능 출력 경로상의 함수가 이 실행 문맥에서 실행됨: 실행 시점·데이터 갱신 주기·공유 상태 갱신 순서 확인 필요"}

    def run(self) -> List[dict]:
        out = []
        for ch in self.changes:
            r = self.analyze_change(ch)
            out.append({**ch, "impact": r})
        return out


def _walk(n: dict):
    stack = [n]
    while stack:
        x = stack.pop()
        yield x
        for c in x.get("ch", []) or []:
            if c is not None:
                stack.append(c)


def feature_summary(results: List[dict]) -> List[dict]:
    by_f: Dict[str, dict] = {}
    order = [Status.EXEC_DIFF, Status.MODEL_DIFF, Status.STATIC_CANDIDATE, Status.INCONCLUSIVE,
             Status.MODEL_NO_DIFF, Status.EXEC_NO_DIFF, Status.NO_PATH]
    for ch in results:
        for f in ch["impact"]["features"]:
            e = by_f.setdefault(f["feature"], {"feature": f["feature"], "name": f["feature_name"], "changes": []})
            e["changes"].append({"change": ch["id"], "type": ch["type_label"], "unit": ch.get("unit_name"),
                                 "status": f["status"]})
    for e in by_f.values():
        sts = [c["status"] for c in e["changes"]]
        e["overall"] = min(sts, key=lambda s: order.index(s) if s in order else 99) if sts else Status.NO_PATH
    return sorted(by_f.values(), key=lambda e: e["feature"])
