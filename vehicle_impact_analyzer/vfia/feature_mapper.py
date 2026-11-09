"""기능 분류기: 구조 추출 -> 기능 후보 탐색 -> 근거가 있는 기능 연결.

근거 종류(약한 것 -> 강한 것)
  keyword      식별자·주석·파일명에 기능 키워드(약어 확장 포함) 일치     ← 이름만으로 확정하지 않음
  similarity   요구사항/기능 설명과 TF-IDF 코사인 유사도                ← 보조 수단
  community    Louvain 군집에서 시드와 같은 묶음                       ← 묶음 ≠ 차량 기능
  propagation  기능 시드에서 준지도 라벨 전파: Zhu-Ghahramani-Lafferty harmonic function을 기능마다
               one-vs-rest로 적용(다른 기능의 시드를 음성 표본으로 고정)해 점수만 사용 → 다중 라벨 허용.
               (networkx.node_classification.harmonic_function은 노드당 최고 라벨 하나만 돌려주고 방향
               그래프를 지원하지 않으므로 같은 식을 무방향 가중 그래프에 직접 반복 계산)
  recon        Software Reconnaissance(Wilde & Scully): 기능을 실행하는 시나리오와 실행하지 않는 시나리오의
               실행 추적 차이로 '기능 고유' 코드와 '공용' 코드를 구분
  cochange     과거 변경 이력에서 시드 파일과 함께 바뀐 파일(ROSE식 공변경 규칙)
  trace        기능 시나리오 실행 추적에서 실행됨                        ← 미실행 ≠ 무관
  slice        기능 관찰 대상(출력)의 후방 슬라이스에 포함              ← 가장 강한 구조적 근거
  seed/review  설계 정보의 시드, 검토자 확정

역할: direct(직접 구현) / support(지원) / shared(공용) / candidate(후보, 구조 근거 없음)
공용 함수는 추천 순위만 낮추고, 그래프의 의존관계는 삭제하지 않는다(영향 분석은 별도).
"""
from __future__ import annotations

import json
import math
import os
import re
from collections import Counter, defaultdict
from typing import Dict, List, Optional, Set, Tuple

import networkx as nx

from .config import AnalysisOptions, load_structured
from .model import EK, NK, AnalysisGraph
from .slicing import backward_slice

ABBREV = {
    "tq": "torque", "trq": "torque", "spd": "speed", "veh": "vehicle", "lim": "limit", "diag": "diagnosis",
    "cmd": "command", "req": "request", "out": "output", "cnt": "counter", "thr": "threshold", "thresh": "threshold",
    "pct": "percent", "sens": "sensor", "plaus": "plausibility", "err": "error", "flt": "filter", "fil": "filter",
    "dtc": "dtc", "isr": "interrupt", "rx": "receive", "tx": "transmit", "brk": "brake", "acc": "accel",
    "temp": "temperature", "volt": "voltage", "curr": "current", "calc": "calculate", "max": "maximum",
    "min": "minimum", "ctrl": "control", "mon": "monitor", "kph": "speed", "rpm": "engine",
}
_STOP = {"the", "a", "an", "of", "and", "or", "to", "in", "is", "if", "for", "by", "on", "with", "void", "uint8",
         "uint16", "uint32", "int16", "int32", "int8", "bool", "true", "false", "static", "return", "get", "set",
         "fn", "gv", "src", "c", "h", "t", "u", "s", "e", "g", "p"}
_CAMEL = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+")
_HANGUL = re.compile(r"[가-힣]+")


def tokenize(text: Optional[str], abbrev: Dict[str, str] = ABBREV) -> List[str]:
    if not text:
        return []
    out: List[str] = []
    for w in _HANGUL.findall(text):
        out.append(w)
        if len(w) > 2:
            out.extend(w[i:i + 2] for i in range(len(w) - 1))
    for part in re.split(r"[^A-Za-z0-9]+", text):
        for t in _CAMEL.findall(part):
            t = t.lower()
            if t in _STOP or t.isdigit():
                continue
            out.append(t)
            if t in abbrev and abbrev[t] != t:
                out.append(abbrev[t])
    return out


class TfIdf:
    def __init__(self, docs: Dict[str, List[str]]):
        self.N = max(1, len(docs))
        df: Counter = Counter()
        for toks in docs.values():
            df.update(set(toks))
        self.idf = {t: math.log((1 + self.N) / (1 + c)) + 1.0 for t, c in df.items()}
        self.vecs = {k: self.vec(v) for k, v in docs.items()}

    def vec(self, toks: List[str]) -> Dict[str, float]:
        tf = Counter(toks)
        v = {t: (1 + math.log(c)) * self.idf.get(t, math.log(1 + self.N) + 1.0) for t, c in tf.items()}
        n = math.sqrt(sum(x * x for x in v.values())) or 1.0
        return {t: x / n for t, x in v.items()}

    @staticmethod
    def cos(a: Dict[str, float], b: Dict[str, float]) -> float:
        if len(a) > len(b):
            a, b = b, a
        return sum(x * b.get(t, 0.0) for t, x in a.items())


def personalized_pagerank(G: nx.Graph, seeds: Dict[str, float], alpha: float = 0.25, iters: int = 60) -> Dict[str, float]:
    """재시작 확률 alpha의 개인화 PageRank(가중 무방향 그래프, 순수 파이썬)."""
    if not seeds:
        return {}
    tot = sum(seeds.values())
    pers = {k: v / tot for k, v in seeds.items() if k in G}
    if not pers:
        return {}
    wdeg = {n: sum(d.get("weight", 1.0) for _, _, d in G.edges(n, data=True)) for n in G}
    r = dict(pers)
    for _ in range(iters):
        nr: Dict[str, float] = defaultdict(float)
        for n, val in r.items():
            if wdeg.get(n, 0) <= 0:
                for s, p in pers.items():
                    nr[s] += (1 - alpha) * val * p
                continue
            for _, m, d in G.edges(n, data=True):
                nr[m] += (1 - alpha) * val * d.get("weight", 1.0) / wdeg[n]
        for s, p in pers.items():
            nr[s] += alpha * p
        diff = sum(abs(nr.get(k, 0) - r.get(k, 0)) for k in set(nr) | set(r))
        r = dict(nr)
        if diff < 1e-9:
            break
    return r


def harmonic_scores(G: nx.Graph, positive: Set[str], negative: Set[str], max_iter: int = 200,
                    tol: float = 1e-6) -> Optional[Dict[str, float]]:
    """Harmonic function(Zhu et al., ICML 2003): 라벨 노드는 고정(양성 1, 음성 0), 나머지는
    f(u) = Σ w(u,v) f(v) / Σ w(u,v) 를 만족하도록 반복(Gauss-Seidel). 음성 표본이 없으면 정의되지 않는다."""
    pos = {n for n in positive if n in G}
    neg = {n for n in negative if n in G and n not in pos}
    if not pos or not neg:
        return None
    f = {n: (1.0 if n in pos else 0.0) for n in G}
    for _ in range(max_iter):
        delta = 0.0
        for u in G:
            if u in pos or u in neg:
                continue
            num = den = 0.0
            for v, d in G[u].items():
                w = d.get("weight", 1.0)
                num += w * f[v]
                den += w
            nv = num / den if den else 0.0
            delta = max(delta, abs(nv - f[u]))
            f[u] = nv
        if delta < tol:
            break
    return f


class FeatureMapper:
    def __init__(self, g: AnalysisGraph, options: Optional[AnalysisOptions] = None,
                 review_file: Optional[str] = None, traces: Optional[Dict[str, str]] = None, log=print,
                 cochange=None):
        self.g = g
        self.opt = options or AnalysisOptions()
        self.review_file = review_file
        self.traces = traces or {}
        self.cochange = cochange
        self.log = log
        self.features = [n for n in g.nodes_of(NK.FEATURE)]

    # ---- 요소와 텍스트 ----
    def elements(self) -> List[str]:
        g = self.g
        els = [n for n in g.nodes_of(NK.FUNCTION) if not g.node(n).get("external")]
        els += [n for n in g.nodes_of(NK.GLOBAL) if not g.node(n).get("external")]
        return els

    def element_doc(self, n: str) -> Tuple[List[str], List[str]]:
        d = self.g.node(n)
        name_toks = tokenize(d.get("name"))
        other = tokenize(d.get("comment")) + tokenize(os.path.splitext(os.path.basename(d.get("file") or ""))[0])
        return name_toks, other

    def feature_doc(self, fid: str) -> List[str]:
        g = self.g
        d = g.node(fid)
        toks = tokenize(d.get("name")) + tokenize(d.get("description"))
        for kw in d.get("keywords") or []:
            toks += tokenize(kw) * 2
        for r in g.succ(fid, [EK.REQUIRES]):
            toks += tokenize(g.node(r).get("text"))
        return toks

    # ---- 관계 그래프(무방향, 가중치) ----
    def relation_graph(self) -> nx.Graph:
        g = self.g
        G = nx.Graph()
        weights = {EK.CALLS: 1.0, EK.READS: 0.7, EK.WRITES: 0.9, EK.ADDR_TAKEN: 0.8, EK.USES_TYPE: 0.3,
                   EK.USES_MACRO: 0.2}
        allowed = set(self.elements())
        for s, d, data in g.g.edges(data=True):
            k = data["kind"]
            if k not in weights:
                continue
            if s not in allowed:
                continue
            if d not in allowed and g.kind(d) not in (NK.RECORD, NK.MACRO):
                continue
            if g.kind(d) == NK.MACRO and g.node(d).get("include_guard"):
                continue
            w = weights[k]
            if G.has_edge(s, d):
                G[s][d]["weight"] += w
            else:
                G.add_edge(s, d, weight=w)
        for n in allowed:
            G.add_node(n)
        return G

    # ---- 실행 ----
    def run(self) -> dict:
        g = self.g
        els = self.elements()
        docs = {}
        name_toks = {}
        for e in els:
            nt, ot = self.element_doc(e)
            name_toks[e] = (set(nt), set(ot))
            docs[e] = nt * 2 + ot
        tfidf = TfIdf(docs)
        G = self.relation_graph()
        try:
            comms = nx.community.louvain_communities(G, weight="weight", seed=self.opt.louvain_seed,
                                                     resolution=self.opt.louvain_resolution)
        except Exception:
            comms = [set(G.nodes)]
        comm_of = {}
        for i, c in enumerate(comms):
            for n in c:
                comm_of[n] = i
        fanin = {n: len({s for s in g.pred(n, [EK.CALLS])}) for n in els if g.kind(n) == NK.FUNCTION}
        reviews = self._load_reviews()
        traces = self._load_traces()

        # 기능별 양성 표본(설계 시드 + 관찰 대상 소유 요소) -> one-vs-rest 음성 표본
        positives: Dict[str, Set[str]] = {}
        for fid in self.features:
            pos = set(g.node(fid).get("seeds") or [])
            for o in g.succ(fid, [EK.HAS_OBSERVATION]):
                for a in g.succ(o, [EK.OBSERVES]):
                    if g.func_of(a):
                        pos.add(g.func_of(a))
                    if g.kind(a) == NK.LOCATION and g.node(a).get("root"):
                        pos.add(g.node(a)["root"])
            positives[fid] = pos
        self.propagation_method = {}
        per_feature: Dict[str, Dict[str, dict]] = {}
        slices: Dict[str, Dict[str, Set[str]]] = {}
        for fid in self.features:
            fd = g.node(fid)
            obs = g.succ(fid, [EK.HAS_OBSERVATION])
            anchors: List[str] = []
            for o in obs:
                anchors += g.succ(o, [EK.OBSERVES])
            sl = backward_slice(g, anchors) if anchors else None
            slice_elems: Dict[str, Set[str]] = defaultdict(set)
            if sl is not None:
                for n in sl.nodes:
                    f = g.func_of(n)
                    if f:
                        slice_elems[f].add(n)
                    k = g.kind(n)
                    if k == NK.LOCATION:
                        root = g.node(n).get("root")
                        if root:
                            slice_elems[root].add(n)
            slices[fid] = slice_elems
            seeds = list(fd.get("seeds") or [])
            # 관찰 대상을 소유한 함수/전역도 시드로 사용(설계 정보에서 온 기준점)
            obs_owner = set()
            for a in anchors:
                if g.func_of(a):
                    obs_owner.add(g.func_of(a))
                if g.kind(a) == NK.LOCATION and g.node(a).get("root"):
                    obs_owner.add(g.node(a)["root"])
            signal_writers = set()
            for a in anchors:
                if g.kind(a) == NK.SIGNAL:
                    for w in g.pred(a, [EK.SIGNAL_WRITE]):
                        if g.func_of(w):
                            signal_writers.add(g.func_of(w))
            negatives = set().union(*[p for f2, p in positives.items() if f2 != fid]) if len(positives) > 1 else set()
            hs = harmonic_scores(G, set(seeds) | obs_owner, negatives, self.opt.harmonic_max_iter)
            if hs is not None:
                ppr, pmax = hs, 1.0
                self.propagation_method[fd.get("fid")] = "harmonic(one-vs-rest)"
            else:
                # 음성 표본이 없으면(기능 1개) harmonic function이 정의되지 않아 개인화 PageRank로 대체
                ppr = personalized_pagerank(G, {s: 1.0 for s in set(seeds) | obs_owner}, alpha=self.opt.ppr_alpha)
                pmax = max(ppr.values()) if ppr else 1.0
                self.propagation_method[fd.get("fid")] = f"personalized PageRank(alpha={self.opt.ppr_alpha}) - 음성 표본 없음"
            seed_files = {g.node(x).get("file") for x in set(seeds) | obs_owner if g.has_node(x) and g.node(x).get("file")}
            fvec = tfidf.vec(self.feature_doc(fid))
            kws = set()
            for kw in fd.get("keywords") or []:
                kws |= set(tokenize(kw))
            seed_comms = {comm_of.get(s) for s in seeds if s in comm_of}
            res: Dict[str, dict] = {}
            for e in els:
                ev: List[dict] = []
                score = 0.0
                nt, ot = name_toks[e]
                km_name = sorted(kws & nt)
                km_other = sorted(kws & ot)
                if km_name:
                    ev.append({"type": "keyword", "detail": f"식별자 키워드 일치: {', '.join(km_name)}"})
                    score += 0.15
                elif km_other:
                    ev.append({"type": "keyword", "detail": f"주석/파일명 키워드 일치: {', '.join(km_other)}"})
                    score += 0.08
                sim = tfidf.cos(fvec, tfidf.vecs[e])
                if sim >= 0.12:
                    ev.append({"type": "similarity", "detail": f"기능 설명·요구사항과 텍스트 유사도 {sim:.2f}"})
                    score += 0.15 * min(1.0, sim * 2)
                pv = ppr.get(e, 0.0) / pmax if pmax else 0.0
                harmonic = self.propagation_method.get(fd.get("fid"), "").startswith("harmonic")
                thr = 0.5 if harmonic else self.opt.feature_score_threshold
                if pv >= thr and e not in seeds:
                    ev.append({"type": "propagation",
                               "detail": (f"harmonic function 점수 {pv:.2f}(이 기능 vs 다른 기능 시드)" if harmonic
                                          else f"기능 시드에서 그래프 전파 점수 {pv:.2f}")})
                    score += 0.25 * pv
                if comm_of.get(e) in seed_comms and e not in seeds:
                    ev.append({"type": "community", "detail": f"시드와 같은 구조 군집 C{comm_of.get(e)}"})
                    score += 0.05
                in_slice = e in slice_elems
                if in_slice:
                    cnt = len(slice_elems[e])
                    ev.append({"type": "slice",
                               "detail": f"관찰 대상({', '.join(o.split(':', 1)[1] for o in obs)})의 후방 슬라이스에 포함(노드 {cnt}개)"})
                    score += 0.45
                recon = None
                tr = traces.get(fd.get("fid"))
                nm = g.node(e).get("name")
                if tr and nm in tr["positive"]:
                    if tr["negative_scenarios"]:
                        if nm in tr["negative"]:
                            recon = "common"
                            ev.append({"type": "recon", "detail": "기능 시나리오와 다른 시나리오에서 모두 실행(공용 코드, "
                                                                  "Software Reconnaissance)"})
                            score += 0.05
                        else:
                            recon = "specific"
                            ev.append({"type": "recon", "detail": f"기능 시나리오({', '.join(tr['positive_scenarios'])})에서만 "
                                                                  "실행(기능 고유 코드, Software Reconnaissance)"})
                            score += 0.3
                    else:
                        ev.append({"type": "trace", "detail": f"시나리오 '{', '.join(tr['positive_scenarios'])}' 실행 추적에서 실행됨"
                                                              "(음성 시나리오 없음: 고유/공용 구분 불가)"})
                        score += 0.1
                if self.cochange and g.kind(e) in (NK.FUNCTION, NK.GLOBAL):
                    ef = g.node(e).get("file")
                    best = (0.0, 0, None)
                    for sf in seed_files:
                        if sf and ef and sf != ef:
                            c, sup = self.cochange.confidence(sf, ef)
                            if c > best[0]:
                                best = (c, sup, sf)
                    if best[0] >= 0.3 and best[1] >= 2:
                        ev.append({"type": "cochange", "detail": f"변경 이력에서 {best[2]}와 함께 변경(신뢰도 {best[0]:.2f}, "
                                                                 f"{best[1]}회)"})
                        score += 0.05 * best[0]
                if e in signal_writers:
                    ev.append({"type": "slice", "detail": "관찰 신호의 송신 지점을 포함"})
                is_seed = e in seeds
                if is_seed:
                    ev.insert(0, {"type": "seed", "detail": "설계 정보(기능 모델)의 시드"})
                    score = max(score, 1.0)
                if not ev:
                    continue
                if not (is_seed or in_slice) and score < 0.12:
                    continue
                res[e] = {"score": round(min(score, 1.5), 3), "evidence": ev, "in_slice": in_slice,
                          "is_seed": is_seed, "obs_owner": e in obs_owner, "ppr": round(pv, 3), "sim": round(sim, 3),
                          "community": comm_of.get(e), "recon": recon}
            per_feature[fid] = res

        # 역할 판정(다중 라벨). 요소마다 '주 기능'(점수 최대 기능)을 정해 직접/지원을 구분한다.
        slice_count: Counter = Counter()
        for fid, sl in slices.items():
            for e in sl:
                slice_count[e] += 1
        primary: Dict[str, str] = {}
        for e in els:
            best = None
            for fid in self.features:
                r = per_feature[fid].get(e)
                if r and (best is None or r["score"] > best[0]):
                    best = (r["score"], fid)
            if best:
                primary[e] = best[1]
        readers = {n: len(g.pred(n, [EK.READS])) for n in els if g.kind(n) == NK.GLOBAL}
        result_features = []
        for fid in self.features:
            fd = g.node(fid)
            items = []
            for e, r in per_feature[fid].items():
                d = g.node(e)
                text_ev = any(x["type"] in ("keyword", "similarity") for x in r["evidence"])
                multi_use = fanin.get(e, 0) >= 2 or readers.get(e, 0) >= 2
                if r["is_seed"] or r["obs_owner"]:
                    role = "direct"
                elif r.get("recon") == "common" and r["in_slice"]:
                    role = "shared"
                elif r["in_slice"] and r.get("recon") == "specific":
                    role = "direct"
                elif r["in_slice"] and slice_count[e] >= self.opt.shared_feature_count and multi_use:
                    role = "shared"
                elif r["in_slice"] and primary.get(e) == fid and text_ev:
                    role = "direct"
                elif r["in_slice"]:
                    role = "support"
                else:
                    role = "candidate"
                rank = r["score"] * (0.6 if role == "shared" else 1.0)
                status = "시드(설계 정보)" if r["is_seed"] else "자동 추정"
                rv = reviews.get((fd.get("fid"), e))
                if rv:
                    status = {"confirmed": "검토 확정", "rejected": "검토 기각"}.get(rv.get("status"), rv.get("status"))
                    role = rv.get("role", role)
                    r["evidence"].append({"type": "review", "detail": f"{rv.get('reviewer', '검토자')}: {rv.get('note', '')}"})
                items.append({
                    "element": e, "kind": d.get("kind"), "name": d.get("name"), "file": d.get("file"),
                    "line": d.get("line"), "role": role, "score": r["score"], "rank_score": round(rank, 3),
                    "evidence": r["evidence"], "status": status, "community": r["community"],
                    "shared_by": slice_count[e], "primary_feature": g.node(primary[e]).get("fid") if e in primary else None,
                })
            for (ffid, e), rv in reviews.items():   # 검토자가 추가한 연결
                if ffid == fd.get("fid") and e not in per_feature[fid] and g.has_node(e):
                    d = g.node(e)
                    items.append({"element": e, "kind": d.get("kind"), "name": d.get("name"), "file": d.get("file"),
                                  "line": d.get("line"), "role": rv.get("role", "support"), "score": 1.0,
                                  "rank_score": 1.0, "evidence": [{"type": "review", "detail": rv.get("note", "")}],
                                  "status": "검토 확정" if rv.get("status") == "confirmed" else rv.get("status"),
                                  "community": comm_of.get(e), "shared_by": slice_count[e]})
            items.sort(key=lambda x: ({"direct": 0, "support": 1, "shared": 2, "candidate": 3}.get(x["role"], 4),
                                      -x["rank_score"], x["name"] or ""))
            result_features.append({
                "id": fd.get("fid"), "node": fid, "name": fd.get("name"), "description": fd.get("description"),
                "requirements": [{"id": g.node(r).get("name"), "text": g.node(r).get("text")} for r in g.succ(fid, [EK.REQUIRES])],
                "observations": [{"id": g.node(o).get("name"), "kind": g.node(o).get("obs_kind"),
                                  "anchors": g.node(o).get("anchors")} for o in g.succ(fid, [EK.HAS_OBSERVATION])],
                "elements": [x for x in items if x["status"] != "검토 기각"],
                "rejected": [x for x in items if x["status"] == "검토 기각"],
            })
        files = self._file_aggregation(result_features)
        mapped = {x["element"] for f in result_features for x in f["elements"] if x["role"] != "candidate"}
        unmapped = []
        for e in els:
            if e not in mapped:
                d = g.node(e)
                unmapped.append({"element": e, "kind": d.get("kind"), "name": d.get("name"), "file": d.get("file"),
                                 "community": comm_of.get(e)})
        communities = []
        for i, c in enumerate(comms):
            members = sorted(n for n in c if n in set(els))
            if not members:
                continue
            labels = Counter()
            for f in result_features:
                for x in f["elements"]:
                    if x["element"] in c and x["role"] != "candidate":
                        labels[f["id"]] += 1
            communities.append({"id": i, "members": [g.node(m).get("name") for m in members],
                                "keys": members, "feature_votes": dict(labels.most_common())})
        return {
            "kind": "feature_classification",
            "version": g.meta.get("version"), "build": g.meta.get("build"),
            "graph_fingerprint": g.meta.get("input_fingerprint"),
            "features": result_features, "files": files, "unmapped": unmapped, "communities": communities,
            "method": {
                "community": "Louvain(networkx, 가중치)",
                "propagation": "; ".join(f"{k}: {v}" for k, v in sorted(self.propagation_method.items())),
                "louvain": f"seed={self.opt.louvain_seed}, resolution={self.opt.louvain_resolution} (재현용 고정)",
                "reconnaissance": "기능 실행 시나리오 vs 비실행 시나리오의 실행 함수 차이(Wilde & Scully)",
                "cochange": (self.cochange.summary() if self.cochange else "변경 이력 미지정"),
                "slice": "관찰 대상 기준 2단계 SDG 후방 슬라이스",
                "text": "식별자 분해+약어 확장, TF-IDF 코사인",
            },
            "notes": [
                "이름·유사도·군집 근거만 있는 항목은 'candidate'이며 기능 연결로 확정하지 않는다.",
                "공용(shared) 요소는 추천 순위만 낮추었고 영향 분석의 의존관계는 그대로 유지된다.",
                "실행 추적에서 실행되지 않았다고 기능과 무관하다고 판단하지 않는다.",
            ],
        }

    def _file_aggregation(self, feats: List[dict]) -> List[dict]:
        by_file: Dict[str, Dict[str, Counter]] = defaultdict(lambda: defaultdict(Counter))
        elems: Dict[str, Dict[str, List[str]]] = defaultdict(lambda: defaultdict(list))
        for f in feats:
            for x in f["elements"]:
                if not x.get("file"):
                    continue
                by_file[x["file"]][f["id"]][x["role"]] += 1
                elems[x["file"]][f["id"]].append(f"{x['name']}({x['role']})")
        out = []
        for file in sorted(by_file):
            fs = []
            for fid, roles in sorted(by_file[file].items(), key=lambda kv: -sum(kv[1].values())):
                fs.append({"feature": fid, "roles": dict(roles), "elements": elems[file][fid],
                           "structural": sum(v for k, v in roles.items() if k != "candidate")})
            out.append({"file": file, "features": fs,
                        "multi_feature": sum(1 for x in fs if x["structural"] > 0) > 1})
        return out

    def _load_reviews(self) -> Dict[Tuple[str, str], dict]:
        out = {}
        if not self.review_file or not os.path.isfile(self.review_file):
            return out
        data = load_structured(self.review_file) or {}
        for r in data.get("reviews", []) or []:
            out[(r["feature"], r["element"])] = r
        return out

    def _load_traces(self) -> Dict[str, dict]:
        """실행 추적 -> 기능별 양성/음성 시나리오 집합(Software Reconnaissance).

        형식(JSON, 단일 객체 또는 목록)
          {"scenario": "...", "features": ["F_A", ...], "functions": [...]}   # 나열된 기능에 양성, 나머지 음성
          {"scenario": "...", "feature": "F_A", "exercises": true|false, "functions": [...]}
        텍스트: '# feature: F_A', '# exercises: false', '# scenario: name' 머리말 + 함수명 줄.
        """
        runs = []
        for scen, path in self.traces.items():
            if not path or not os.path.isfile(path):
                continue
            if path.endswith(".json"):
                d = json.load(open(path, encoding="utf-8"))
                for x in (d if isinstance(d, list) else [d]):
                    runs.append({"scenario": x.get("scenario", scen), "features": x.get("features"),
                                 "feature": x.get("feature"), "exercises": x.get("exercises", True),
                                 "functions": set(x.get("functions", []))})
            else:
                fid, ex, name, funcs = None, True, scen, set()
                for line in open(path, encoding="utf-8"):
                    line = line.strip()
                    if line.startswith("# feature:"):
                        fid = line.split(":", 1)[1].strip()
                    elif line.startswith("# exercises:"):
                        ex = line.split(":", 1)[1].strip().lower() not in ("false", "no", "0")
                    elif line.startswith("# scenario:"):
                        name = line.split(":", 1)[1].strip()
                    elif line and not line.startswith("#"):
                        funcs.add(line.split()[0])
                runs.append({"scenario": name, "features": None, "feature": fid, "exercises": ex, "functions": funcs})
        all_f = [self.g.node(f).get("fid") for f in self.features]
        out: Dict[str, dict] = {}
        for fid in all_f:
            pos, neg, ps, ns = set(), set(), [], []
            for r in runs:
                if r["features"] is not None:
                    hit = fid in r["features"]
                elif r["feature"] == fid:
                    hit = bool(r["exercises"])
                elif r["feature"] is None:
                    continue
                else:
                    continue
                if hit:
                    pos |= r["functions"]
                    ps.append(r["scenario"])
                else:
                    neg |= r["functions"]
                    ns.append(r["scenario"])
            if ps:
                out[fid] = {"positive": pos, "negative": neg, "positive_scenarios": ps, "negative_scenarios": ns}
        return out


def export_llm_prompts(result: dict, path: str) -> int:
    """기능명이 정해지지 않은 군집에 대해 LLM 기능명 후보 생성을 위한 프롬프트를 내보낸다.
    LLM은 이름·설명 후보만 만들고, 영향 경로의 존재 여부는 분석 그래프로 확인한다."""
    n = 0
    with open(path, "w", encoding="utf-8") as f:
        for c in result["communities"]:
            if c["feature_votes"]:
                continue
            prompt = {
                "task": "아래 C 함수/전역변수 묶음이 구현하는 차량 기능 이름 후보 3개와 근거를 제시하라. "
                        "근거가 부족하면 '판단 불가'라고 답하라.",
                "members": c["members"],
            }
            f.write(json.dumps({"community": c["id"], "prompt": prompt}, ensure_ascii=False) + "\n")
            n += 1
    return n
