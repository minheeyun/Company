"""차량 기능 연결 정보: 기능·요구사항·관찰 대상·실행 문맥·통신 신호·캘리브레이션.

코드의 직접 호출만으로 드러나지 않는 영향 전달(태스크/ISR 공유 상태, 콜백 등록,
RTE/COM 신호, 캘리브레이션 값)을 그래프에 명시적으로 연결한다.

연결 관계
  feature -REQUIRES-> requirement
  feature -HAS_OBSERVATION-> observation -OBSERVES-> 그래프 앵커(formal_out, loc, signal, stmt)
  task/isr/runnable -ACTIVATES-> function,  function -RUNS_IN-> task (호출 그래프 도달성)
  쓰기 지점 -SIGNAL_WRITE-> signal -SIGNAL_READ-> 읽기 지점
  calibration -CALIBRATES-> location

주의: ARXML이나 신호 이름만으로 차량 기능이 자동 확정되지는 않는다. 기능 연결은
요구사항·설계 정보(이 모델 파일)에 근거해야 한다.
"""
from __future__ import annotations

import os
import re
import xml.etree.ElementTree as ET
from collections import deque
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Union

from .config import load_structured
from .model import EK, NK, AnalysisGraph


@dataclass
class VehicleModel:
    ecu: Optional[str] = None
    features: List[dict] = field(default_factory=list)
    requirements: List[dict] = field(default_factory=list)
    tasks: List[dict] = field(default_factory=list)
    signals: List[dict] = field(default_factory=list)
    calibrations: List[dict] = field(default_factory=list)
    runnables: List[dict] = field(default_factory=list)
    sources: List[str] = field(default_factory=list)

    def feature(self, fid: str) -> Optional[dict]:
        for f in self.features:
            if f["id"] == fid:
                return f
        return None

    def requirement(self, rid: str) -> Optional[dict]:
        for r in self.requirements:
            if r["id"] == rid:
                return r
        return None


def load_vehicle_model(paths: Union[None, str, List[str]]) -> VehicleModel:
    vm = VehicleModel()
    if not paths:
        return vm
    if isinstance(paths, str):
        paths = [paths]
    for p in paths:
        raw = load_structured(p) or {}
        base = os.path.dirname(os.path.abspath(p))
        vm.sources.append(p)
        vm.ecu = raw.get("ecu", vm.ecu)
        vm.features.extend(raw.get("features", []) or [])
        vm.requirements.extend(raw.get("requirements", []) or [])
        ex = raw.get("execution", {}) or {}
        for kind_key, ctx_kind in (("tasks", "task"), ("isrs", "isr"), ("callbacks", "callback"), ("runnables", "runnable")):
            for t in ex.get(kind_key, []) or []:
                t = dict(t)
                t.setdefault("kind", ctx_kind)
                vm.tasks.append(t)
        vm.signals.extend(raw.get("signals", []) or [])
        vm.calibrations.extend(raw.get("calibrations", []) or [])
        for ax in raw.get("arxml", []) or []:
            axp = ax if os.path.isabs(ax) else os.path.join(base, ax)
            runnables = parse_arxml(axp)
            vm.runnables.extend(runnables)
            vm.sources.append(axp)
    return vm


# ---------------------------------------------------------------------------
def _strip_ns(tag: str) -> str:
    return tag.split("}", 1)[1] if "}" in tag else tag


def parse_arxml(path: str) -> List[dict]:
    """ARXML에서 Runnable(SYMBOL), TimingEvent 주기, 포트 데이터 접근을 추출(필수 일부만)."""
    try:
        tree = ET.parse(path)
    except (OSError, ET.ParseError):
        return []
    root = tree.getroot()
    for el in root.iter():
        el.tag = _strip_ns(el.tag)

    def text(el, tag):
        x = el.find(tag)
        return x.text.strip() if x is not None and x.text else None

    out: List[dict] = []
    for swc in root.iter():
        if not swc.tag.endswith("SW-COMPONENT-TYPE"):
            continue
        swc_name = text(swc, "SHORT-NAME")
        periods: Dict[str, float] = {}
        for ev in swc.iter("TIMING-EVENT"):
            ref = text(ev, "START-ON-EVENT-REF")
            per = text(ev, "PERIOD")
            if ref and per:
                try:
                    periods[ref.split("/")[-1]] = float(per) * 1000.0
                except ValueError:
                    pass
        for re_ in swc.iter("RUNNABLE-ENTITY"):
            name = text(re_, "SHORT-NAME")
            sym = text(re_, "SYMBOL") or name
            writes, reads = [], []
            for tag, dst in (("DATA-WRITE-ACCESSS", writes), ("DATA-SEND-POINTS", writes),
                             ("DATA-READ-ACCESSS", reads), ("DATA-RECEIVE-POINT-BY-ARGUMENTS", reads),
                             ("DATA-RECEIVE-POINT-BY-VALUES", reads)):
                for grp in re_.iter(tag):
                    for va in grp.iter("VARIABLE-ACCESS"):
                        port = va.find(".//PORT-PROTOTYPE-REF")
                        elem = va.find(".//TARGET-DATA-PROTOTYPE-REF")
                        if port is not None and elem is not None and port.text and elem.text:
                            dst.append(f"{port.text.strip().split('/')[-1]}_{elem.text.strip().split('/')[-1]}")
            out.append({"name": name, "symbol": sym, "swc": swc_name, "period_ms": periods.get(name),
                        "writes": writes, "reads": reads, "source": os.path.basename(path)})
    return out


# ---------------------------------------------------------------------------
_RTE_W = re.compile(r"^Rte_(Write|IWrite|Send|IWriteRef)_(.+)$")
_RTE_R = re.compile(r"^Rte_(Read|IRead|Receive|DRead)_(.+)$")


class VehicleLinker:
    def __init__(self, g: AnalysisGraph, vm: VehicleModel):
        self.g = g
        self.vm = vm
        self.notes: List[str] = []

    def fn_keys(self, name: str) -> List[str]:
        keys = [k for k in self.g.functions_by_name(name) if not self.g.node(k).get("external")]
        if not keys:
            keys = self.g.functions_by_name(name)
        if len(keys) > 1:
            self.notes.append(f"함수 이름 '{name}'이 여러 정의와 일치: {keys}")
        return keys

    def gv_keys(self, name: str) -> List[str]:
        return self.g.globals_by_name(name)

    def call_sites_to(self, fkey: str) -> List[str]:
        return [n for n in self.g.nodes_of(NK.CALL_SITE) if fkey in (self.g.node(n).get("callees") or [])]

    def apply(self) -> None:
        self._contexts()
        self._signals()
        self._calibrations()
        self._features()
        self.g.meta["vehicle_model"] = {
            "sources": self.vm.sources, "ecu": self.vm.ecu, "notes": self.notes,
            "features": [f["id"] for f in self.vm.features],
            "contexts": [t.get("name") for t in self.vm.tasks] + [r["name"] for r in self.vm.runnables],
        }

    # --- 실행 문맥 ---
    def _contexts(self) -> None:
        g = self.g
        ctxs = list(self.vm.tasks)
        for r in self.vm.runnables:
            ctxs.append({"name": r["name"], "entry": r["symbol"], "kind": "runnable", "period_ms": r.get("period_ms"),
                         "swc": r.get("swc"), "source": r.get("source")})
        for t in ctxs:
            tid = f"task:{t['name']}"
            g.add_node(tid, NK.TASK, name=t["name"], ctx_kind=t.get("kind", "task"), period_ms=t.get("period_ms"),
                       priority=t.get("priority"), swc=t.get("swc"), trigger=t.get("trigger"),
                       core=t.get("core"), source=t.get("source"))
            entries = t.get("entry") or t.get("entries") or t["name"]
            if isinstance(entries, str):
                entries = [entries]
            found = False
            for e in entries:
                for fk in self.fn_keys(e):
                    g.add_edge(tid, fk, EK.ACTIVATES)
                    found = True
            if not found:
                self.notes.append(f"실행 문맥 {t['name']}의 진입 함수 {entries}를 코드에서 찾지 못함")
        # 호출 그래프 도달성으로 실행 문맥 전파
        for tid in g.nodes_of(NK.TASK):
            seen = set()
            q = deque(g.succ(tid, [EK.ACTIVATES]))
            while q:
                f = q.popleft()
                if f in seen:
                    continue
                seen.add(f)
                g.add_edge(f, tid, EK.RUNS_IN)
                q.extend(g.succ(f, [EK.CALLS]))
        for f in g.nodes_of(NK.FUNCTION):
            if g.node(f).get("external"):
                continue
            taken_by = g.pred(f, [EK.ADDR_TAKEN])
            if taken_by and not g.succ(f, [EK.RUNS_IN]):
                # 주소가 사용되지만 실행 문맥이 정해지지 않은 함수(콜백 등록 테이블 등)
                self.notes.append(f"{g.node(f)['name']}: 주소가 사용되지만({', '.join(x.split(':')[-1] for x in taken_by)}) "
                                  "실행 문맥을 확인하지 못함 — 콜백 등록 정보를 모델에 추가 검토")

    # --- 신호 ---
    def _sig(self, name: str, auto: bool = False, **attrs) -> str:
        sid = f"sig:{name}"
        self.g.add_node(sid, NK.SIGNAL, name=name, auto=auto or None, **{k: v for k, v in attrs.items() if v is not None})
        return sid

    def _connect_api(self, sid: str, api: str, arg: int, direction: str) -> int:
        g = self.g
        n = 0
        for fk in g.functions_by_name(api):
            for cs in self.call_sites_to(fk):
                if direction == "write":
                    ai = f"{cs}:in{arg}"
                    if g.has_node(ai):
                        g.add_edge(ai, sid, EK.SIGNAL_WRITE, api)
                        n += 1
                else:
                    stmt = g.node(cs).get("stmt")
                    g.add_edge(sid, f"{cs}:out", EK.SIGNAL_READ, api)
                    if stmt:
                        g.add_edge(sid, stmt, EK.SIGNAL_READ, api)
                    n += 1
        return n

    def _signals(self) -> None:
        g = self.g
        declared = {s["name"] for s in self.vm.signals}
        for s in self.vm.signals:
            sid = self._sig(s["name"], direction=s.get("direction"), ecu=s.get("ecu") or self.vm.ecu, swc=s.get("swc"),
                            consumers=s.get("consumers"), producers=s.get("producers"), can_id=s.get("can_id"),
                            unit=s.get("unit"))
            for w in s.get("writers", []) or []:
                self._link_endpoint(sid, w, "write")
            for r in s.get("readers", []) or []:
                self._link_endpoint(sid, r, "read")
        # RTE 명명 규칙 자동 인식
        for f in g.nodes_of(NK.FUNCTION):
            name = g.node(f).get("name", "")
            m = _RTE_W.match(name)
            if m:
                sname = m.group(2)
                sid = self._sig(sname, auto=sname not in declared)
                self._connect_api(sid, name, 0, "write")
                continue
            m = _RTE_R.match(name)
            if m:
                sname = m.group(2)
                sid = self._sig(sname, auto=sname not in declared)
                self._connect_api(sid, name, 0, "read")
        # COM API: Com_SendSignal(SignalId, ptr) / Com_ReceiveSignal(SignalId, ptr)
        for cs in g.nodes_of(NK.CALL_SITE):
            d = g.node(cs)
            for c in d.get("callees") or []:
                cname = c.split(":")[-1]
                if cname not in ("Com_SendSignal", "Com_ReceiveSignal"):
                    continue
                sname = self._com_signal_name(d)
                if not sname:
                    self.notes.append(f"{d.get('file')}:{d.get('line')} {cname} 신호 ID를 정적으로 확인하지 못함")
                    continue
                sid = self._sig(sname, auto=sname not in declared)
                if cname == "Com_SendSignal" and g.has_node(f"{cs}:in1"):
                    g.add_edge(f"{cs}:in1", sid, EK.SIGNAL_WRITE, cname)
                else:
                    g.add_edge(sid, f"{cs}:out", EK.SIGNAL_READ, cname)
                    if d.get("stmt"):
                        g.add_edge(sid, d["stmt"], EK.SIGNAL_READ, cname)
        for r in self.vm.runnables:
            for sname in r.get("writes", []):
                sid = self._sig(sname, auto=sname not in declared)
                g.node(sid).setdefault("producers", []).append({"runnable": r["name"], "swc": r.get("swc")})
            for sname in r.get("reads", []):
                sid = self._sig(sname, auto=sname not in declared)
                g.node(sid).setdefault("consumers_local", []).append({"runnable": r["name"], "swc": r.get("swc")})

    def _com_signal_name(self, cs_data: dict) -> Optional[str]:
        fn = cs_data.get("func")
        ast = self.g.asts.get(fn)
        if not ast:
            return None
        aid = cs_data.get("ast_id")
        for n in _walk(ast):
            if n.get("id") == aid and n.get("ch") and len(n["ch"]) > 1:
                a0 = n["ch"][1]
                while a0.get("k") == "ICast" and a0.get("ch"):
                    a0 = a0["ch"][0]
                if a0.get("k") == "Ref":
                    return a0.get("v")
                if a0.get("mac"):
                    return a0["mac"]
        return None

    def _link_endpoint(self, sid: str, ep: dict, direction: str) -> None:
        g = self.g
        if "api" in ep:
            n = self._connect_api(sid, ep["api"], int(ep.get("arg", 0)), direction)
            if n == 0:
                self.notes.append(f"신호 {sid[4:]}: API {ep['api']} 호출 지점을 찾지 못함")
        elif "variable" in ep:
            for gk in self.gv_keys(ep["variable"]) or []:
                loc = f"loc:{gk}"
                if not g.has_node(loc):
                    g.add_node(loc, NK.LOCATION, name=ep["variable"], root=gk, loc=gk)
                if direction == "write":
                    g.add_edge(loc, sid, EK.SIGNAL_WRITE, "variable")
                else:
                    g.add_edge(sid, loc, EK.SIGNAL_READ, "variable")
        elif "param" in ep:
            fn, _, pname = ep["param"].partition(".")
            for fk in self.fn_keys(fn):
                params = g.node(fk).get("params") or []
                if pname in params:
                    g.add_edge(sid, f"{fk}#fi{params.index(pname)}", EK.SIGNAL_READ, "param")
        elif "return" in ep:
            for fk in self.fn_keys(ep["return"]):
                if g.has_node(f"{fk}#fo_ret"):
                    g.add_edge(f"{fk}#fo_ret", sid, EK.SIGNAL_WRITE, "return")

    # --- 캘리브레이션 ---
    def _calibrations(self) -> None:
        g = self.g
        for c in self.vm.calibrations:
            cid = f"cal:{c['name']}"
            g.add_node(cid, NK.CALIBRATION, name=c["name"], min=c.get("min"), max=c.get("max"), unit=c.get("unit"))
            for gk in self.gv_keys(c.get("variable", c["name"])):
                loc = f"loc:{gk}"
                if not g.has_node(loc):
                    g.add_node(loc, NK.LOCATION, name=g.node(gk).get("name"), root=gk, loc=gk)
                g.add_edge(cid, loc, EK.CALIBRATES)
                g.node(gk)["calibration"] = c["name"]

    # --- 기능 ---
    def _features(self) -> None:
        g = self.g
        for r in self.vm.requirements:
            g.add_node(f"req:{r['id']}", NK.REQUIREMENT, name=r["id"], text=r.get("text"), source=r.get("source"))
        for f in self.vm.features:
            fid = f"feat:{f['id']}"
            seeds = self.resolve_seeds(f)
            g.add_node(fid, NK.FEATURE, name=f.get("name", f["id"]), fid=f["id"], description=f.get("description"),
                       keywords=f.get("keywords", []), seeds=seeds)
            for rid in f.get("requirements", []) or []:
                if not g.has_node(f"req:{rid}"):
                    g.add_node(f"req:{rid}", NK.REQUIREMENT, name=rid)
                g.add_edge(fid, f"req:{rid}", EK.REQUIRES)
            for o in f.get("observations", []) or []:
                oid = f"obs:{o['id']}"
                anchors = self.observation_anchors(o)
                g.add_node(oid, NK.OBSERVATION, name=o["id"], obs_kind=o.get("kind"), spec=o, anchors=anchors,
                           feature=f["id"])
                g.add_edge(fid, oid, EK.HAS_OBSERVATION)
                for a in anchors:
                    g.add_edge(oid, a, EK.OBSERVES)
                if not anchors:
                    self.notes.append(f"관찰 대상 {o['id']}의 그래프 앵커를 찾지 못함")

    def resolve_seeds(self, f: dict) -> List[str]:
        out: List[str] = []
        seeds = f.get("seeds", {}) or {}
        for name in seeds.get("functions", []) or []:
            out.extend(self.fn_keys(name))
        for name in seeds.get("globals", []) or []:
            out.extend(self.gv_keys(name))
        return out

    def observation_anchors(self, o: dict) -> List[str]:
        g = self.g
        k = o.get("kind")
        out: List[str] = []
        if k == "function_return":
            for fk in self.fn_keys(o["function"]):
                if g.has_node(f"{fk}#fo_ret"):
                    out.append(f"{fk}#fo_ret")
        elif k == "global":
            for gk in self.gv_keys(o["variable"]):
                loc = f"loc:{gk}"
                if not g.has_node(loc):
                    g.add_node(loc, NK.LOCATION, name=o["variable"], root=gk, loc=gk)
                out.append(loc)
                for l in g.nodes_of(NK.LOCATION):
                    if g.node(l).get("root") == gk and l != loc:
                        out.append(l)
        elif k == "signal":
            sid = f"sig:{o['signal']}"
            if not g.has_node(sid):
                self._sig(o["signal"], auto=True)
            out.append(sid)
        elif k == "statement":
            for n in g.nodes_of(NK.STMT):
                d = g.node(n)
                if d.get("file") == o.get("file") and d.get("line") == o.get("line"):
                    out.append(n)
        elif k == "function":
            for fk in self.fn_keys(o["function"]):
                for suffix in ("#fo_ret", "#fo_mem"):
                    if g.has_node(fk + suffix):
                        out.append(fk + suffix)
                for gk in g.succ(fk, [EK.WRITES]):
                    for l in g.nodes_of(NK.LOCATION):
                        if g.node(l).get("root") == gk:
                            out.append(l)
        return sorted(set(out))


def _walk(n: dict):
    stack = [n]
    while stack:
        x = stack.pop()
        yield x
        for c in x.get("ch", []) or []:
            if c is not None:
                stack.append(c)


def apply_vehicle_model(g: AnalysisGraph, vm: VehicleModel) -> List[str]:
    linker = VehicleLinker(g, vm)
    linker.apply()
    return linker.notes


def execution_contexts(g: AnalysisGraph, func: str) -> List[dict]:
    out = []
    for t in g.succ(func, [EK.RUNS_IN]):
        d = g.node(t)
        out.append({"id": t, "name": d.get("name"), "kind": d.get("ctx_kind"), "period_ms": d.get("period_ms"),
                    "priority": d.get("priority")})
    return out
