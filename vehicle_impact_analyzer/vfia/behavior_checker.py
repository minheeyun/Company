"""동작 차이 검증: 경로 조건과 출력·상태 차이 확인.

관찰 대상마다(기능 모델의 observation에 function과 입력 정의역이 있을 때)
  1. 변경 전/후 프로그램을 같은 기호 입력·초기 상태로 기호 실행(symexec)
  2. 분기 차이: 변경된 조건/값 노드의 평가값 XOR(같은 입력에서 선택이 달라지는 조건)
  3. 출력 차이: 관찰값(old) != 관찰값(new) 의 충족 가능성 + 증거 입력 + 입력 구간(경계 상자)
  4. 변경 분리: 변경 전 프로그램에 '그 함수의 변경만' 적용한 혼합 프로그램과 비교
  5. 상태 누적 기능: 입력 시퀀스(sequence 단계)와 상태 전이를 함께 비교
  6. 전후 비교 실행: 호스트 gcc로 두 버전을 빌드해 같은 시험 입력(경계값+증거 입력)으로 실행

결과 상태: 모델에서 동작 차이 확인 / 모델에서 동작 차이 미발견(검증 범위 내) /
          실행으로 차이 확인 / 분석 불완전으로 판정 유보
반복 전개·인라인 깊이·시퀀스 길이 등 검증 범위는 결과에 함께 기록한다.
"""
from __future__ import annotations

import itertools
import os
import random
import re
import shutil
import subprocess
import tempfile
from typing import Dict, List, Optional, Set, Tuple

import z3

from .config import AnalysisOptions
from .model import EK, NK, AnalysisGraph, Status
from .symexec import Program, PtrVal, SymExec, Unsupported, bits_of, is_signed, resize


def _anchor_to_ast(a: str) -> Optional[Tuple[str, int]]:
    if "#" not in a:
        return None
    unit, rest = a.rsplit("#", 1)
    if rest.isdigit():
        return unit, int(rest)
    return None


def _val(model: z3.ModelRef, term, signed: bool) -> Optional[int]:
    if term is None or isinstance(term, PtrVal):
        return None
    v = model.eval(term, model_completion=True)
    if not z3.is_bv_value(v):
        return None
    x = v.as_long()
    if signed and x >= 1 << (v.size() - 1):
        x -= 1 << v.size()
    return x


class ObsRun:
    def __init__(self):
        self.values: List = []         # 단계별 관찰값
        self.ex: Optional[SymExec] = None
        self.error: Optional[str] = None
        self.signed = False


class BehaviorChecker:
    def __init__(self, g_old: AnalysisGraph, g_new: AnalysisGraph, cd, options: Optional[AnalysisOptions] = None,
                 exec_compare: str = "auto", workdir: Optional[str] = None, log=print):
        self.go, self.gn = g_old, g_new
        self.cd = cd
        self.opt = options or AnalysisOptions()
        self.exec_mode = exec_compare
        self.workdir = workdir
        self.log = log
        self.p_old = self._program(g_old, "old")
        self.p_new = self._program(g_new, "new")

    def _program(self, g: AnalysisGraph, name: str) -> Program:
        ext = {f for f in g.nodes_of(NK.FUNCTION) if g.node(f).get("external")}
        vol = {k for k in g.nodes_of(NK.GLOBAL) if g.node(k).get("volatile")}
        return Program(g.asts, name, ext, vol)

    # ------------------------------------------------------------------
    def observations(self) -> List[dict]:
        out = []
        for feat in self.gn.nodes_of(NK.FEATURE):
            for o in self.gn.succ(feat, [EK.HAS_OBSERVATION]):
                spec = self.gn.node(o).get("spec") or {}
                if spec.get("kind") in ("function_return", "global") and spec.get("function"):
                    out.append({"feature": self.gn.node(feat).get("fid"), "feature_node": feat, "obs": spec["id"],
                                "spec": spec})
        return out

    def _fn_key(self, g: AnalysisGraph, name: str) -> Optional[str]:
        ks = [k for k in g.functions_by_name(name) if not g.node(k).get("external")]
        return ks[0] if ks else None

    def _reachable(self, g: AnalysisGraph, entry: str) -> Set[str]:
        seen, stack = set(), [entry]
        while stack:
            f = stack.pop()
            if f in seen:
                continue
            seen.add(f)
            stack.extend(g.succ(f, [EK.CALLS]))
        return seen

    def _inputs(self, spec: dict, fast: dict, steps: int) -> Tuple[List[List], List, Dict[str, Tuple]]:
        params = fast.get("params", [])
        doms = spec.get("params") or {}
        per_step, cons, meta = [], [], {}
        for k in range(steps):
            row = []
            for p in params:
                ty = p.get("ty") or {}
                w = bits_of(ty)
                x = z3.BitVec(f"in:{p['index']}:{p['name']}@{k}", w)
                row.append(x)
                s = is_signed(ty)
                lo, hi = (doms.get(p["name"]) or [None, None]) if isinstance(doms.get(p["name"]), list) else (None, None)
                if ty.get("k") == "b":
                    lo, hi = max(0, lo or 0), min(1, 1 if hi is None else hi)
                if lo is not None:
                    cons.append(x >= z3.BitVecVal(lo, w) if s else z3.UGE(x, z3.BitVecVal(lo % (1 << w), w)))
                if hi is not None:
                    cons.append(x <= z3.BitVecVal(hi, w) if s else z3.ULE(x, z3.BitVecVal(hi % (1 << w), w)))
                meta[x.decl().name()] = (p["name"], k, s, lo, hi, w)
            per_step.append(row)
        return per_step, cons, meta

    def _run(self, prog: Program, fkey: str, spec: dict, inputs: List[List], watch: Set, tag: str,
             g: AnalysisGraph) -> ObsRun:
        r = ObsRun()
        ex = SymExec(prog, self.opt.loop_bound, self.opt.inline_depth, self.opt.initial_state, watch, tag=tag)
        r.ex = ex
        try:
            for k, args in enumerate(inputs):
                ex.step = k
                ret = ex.run(fkey, args)
                if spec["kind"] == "function_return":
                    r.values.append(ret)
                    r.signed = is_signed((prog.functions[fkey].get("ret") or {}))
                else:
                    gk = [x for x in g.globals_by_name(spec["variable"])]
                    if not gk:
                        raise Unsupported(f"관찰 전역 변수 {spec['variable']} 없음")
                    gkey = gk[0]
                    ex.ensure_global(gkey)
                    if gkey not in ex.mem:
                        raise Unsupported("구조체 전역 관찰은 필드를 지정해야 함")
                    r.values.append(ex.mem[gkey])
                    r.signed = is_signed(prog.globals[gkey].get("ty"))
        except Unsupported as e:
            r.error = str(e)
        except z3.Z3Exception as e:
            r.error = f"z3 오류: {e}"
        except RecursionError:
            r.error = "재귀 깊이 초과"
        return r

    @staticmethod
    def _neq(a, b):
        if a is None or b is None:
            return z3.BoolVal(False)
        if isinstance(a, PtrVal) or isinstance(b, PtrVal):
            raise Unsupported("포인터 관찰값 비교")
        w = max(a.size(), b.size())
        return resize(a, w, False) != resize(b, w, False)

    def _solver(self) -> z3.Solver:
        s = z3.Solver()
        s.set("timeout", int(self.opt.solver_timeout_ms))
        return s

    def _witnesses(self, base: List, formula, inputs: List[List], meta, extra_terms: Dict[str, Tuple], n: int) -> List[dict]:
        s = self._solver()
        s.add(*base)
        s.add(formula)
        out = []
        flat = [x for row in inputs for x in row]
        for _ in range(n):
            if s.check() != z3.sat:
                break
            m = s.model()
            w = {"inputs": [], "values": {}}
            for k, row in enumerate(inputs):
                step = {}
                for x in row:
                    nm, kk, sg, *_ = meta[x.decl().name()]
                    step[nm] = _val(m, x, sg)
                w["inputs"].append(step)
            for label, (terms, sg) in extra_terms.items():
                w["values"][label] = [_val(m, t, sg) for t in terms]
            env = {}
            for d in m.decls():
                nm = d.name()
                if d.arity() == 0 and (nm.startswith("vol:") or nm.startswith("init:")):
                    env[_env_name(nm)] = _val(m, d(), False)
            if env:
                w["environment"] = dict(sorted(env.items()))
            out.append(w)
            if flat:
                s.add(z3.Or(*[x != m.eval(x, model_completion=True) for x in flat]))
            else:
                break
        return out

    def _region(self, base: List, formula, inputs: List[List], meta) -> Dict[str, List]:
        if len(inputs) != 1:
            return {}
        out = {}
        for x in inputs[0]:
            nm, _, sg, lo, hi, w = meta[x.decl().name()]
            iv = z3.BV2Int(x, is_signed=sg)
            rng = []
            for minimize in (True, False):
                o = z3.Optimize()
                o.set("timeout", int(self.opt.solver_timeout_ms))
                o.add(*base)
                o.add(formula)
                o.minimize(iv) if minimize else o.maximize(iv)
                if o.check() != z3.sat:
                    return {}
                v = o.model().eval(iv, model_completion=True)
                rng.append(v.as_long() if z3.is_int_value(v) else None)
            out[nm] = rng
        return out

    def _check(self, base, formula) -> str:
        s = self._solver()
        s.add(*base)
        s.add(formula)
        r = s.check()
        if r == z3.sat:
            return "sat"
        if r == z3.unsat:
            return "unsat"
        return "unknown"

    def _events_formula(self, ev_o: List, ev_n: List):
        """같은 순번의 평가끼리 짝지어 '값 또는 도달 여부가 다른' 조건."""
        terms = []
        for i in range(max(len(ev_o), len(ev_n))):
            if i < len(ev_o) and i < len(ev_n):
                go, vo = ev_o[i]
                gn, vn = ev_n[i]
                try:
                    neq = self._neq(vo, vn) if vo is not None and vn is not None else z3.BoolVal(False)
                except Unsupported:
                    neq = z3.BoolVal(False)
                terms.append(z3.Or(z3.And(go, gn, neq), go != gn))
            elif i < len(ev_o):
                terms.append(ev_o[i][0])
            else:
                terms.append(ev_n[i][0])
        return z3.Or(*terms) if terms else z3.BoolVal(False)

    @staticmethod
    def _final_value(events: List):
        val = None
        for g, v in events:
            if v is None or isinstance(v, PtrVal):
                continue
            val = v if val is None else z3.If(g, v, val)
        return val

    # ------------------------------------------------------------------
    def check_observation(self, ob: dict, changes: List[dict]) -> dict:
        spec = ob["spec"]
        fo, fn = self._fn_key(self.go, spec["function"]), self._fn_key(self.gn, spec["function"])
        res = {"feature": ob["feature"], "observation": ob["obs"], "function": spec["function"], "kind": spec["kind"],
               "variable": spec.get("variable"), "domain": spec.get("params") or {},
               "bounds": {"loop_bound": self.opt.loop_bound, "inline_depth": self.opt.inline_depth,
                          "sequence": int(spec.get("sequence") or (1 if spec["kind"] == "function_return" else self.opt.sequence_length)),
                          "initial_state": self.opt.initial_state},
               "per_change": {}, "notes": []}
        if not fo or not fn:
            res["status"] = Status.INCONCLUSIVE
            res["error"] = "관찰 함수가 한쪽 버전에 없음"
            return res
        steps = res["bounds"]["sequence"]
        fast = self.p_new.functions[fn]
        if len(fast.get("params", [])) != len(self.p_old.functions[fo].get("params", [])):
            res["status"] = Status.INCONCLUSIVE
            res["error"] = "관찰 함수의 인자 개수가 변경됨"
            return res
        inputs, dom, meta = self._inputs(spec, fast, steps)
        reach_o, reach_n = self._reachable(self.go, fo), self._reachable(self.gn, fn)
        # 이 관찰 모델이 실행하는 범위 안의 변경만 대상으로 한다
        in_scope = []
        for ch in changes:
            units_o = {u for u in [ch.get("unit_old")] if u}
            units_n = {u for u in [ch.get("unit_new")] if u}
            for side in ("old", "new"):
                for a in ch.get("watch", {}).get(side, []) or ch[side].get("anchors", []):
                    t = _anchor_to_ast(a) or ((a.rsplit("#", 1)[0], -1) if "#" in a else None)
                    if t:
                        (units_o if side == "old" else units_n).add(t[0])
            if (units_o & reach_o) or (units_n & reach_n):
                in_scope.append(ch)
            else:
                res["per_change"][ch["id"]] = {"scope": "out",
                                               "note": f"동작 모델({spec['function']} 실행 범위) 밖의 변경: 상위 통합 시나리오 필요"}
        watch_o, watch_n = set(), set()
        ret_watch: Dict[str, Tuple[Set, Set]] = {}
        for ch in in_scope:
            for a in ch.get("watch", {}).get("old", []):
                t = _anchor_to_ast(a)
                if t:
                    watch_o.add(t)
            for a in ch.get("watch", {}).get("new", []):
                t = _anchor_to_ast(a)
                if t:
                    watch_n.add(t)
            ro, rn = set(), set()
            if ch.get("unit_old") in self.p_old.functions:
                ro = {(ch["unit_old"], n["id"]) for n in _walk(self.p_old.functions[ch["unit_old"]]) if n.get("k") == "Return"}
            if ch.get("unit_new") in self.p_new.functions:
                rn = {(ch["unit_new"], n["id"]) for n in _walk(self.p_new.functions[ch["unit_new"]]) if n.get("k") == "Return"}
            ret_watch[ch["id"]] = (ro, rn)
            watch_o |= ro
            watch_n |= rn
        ro = self._run(self.p_old, fo, spec, inputs, watch_o, "o", self.go)
        rn = self._run(self.p_new, fn, spec, inputs, watch_n, "n", self.gn)
        if ro.error or rn.error:
            res["status"] = Status.INCONCLUSIVE
            res["error"] = ro.error or rn.error
            for ch in in_scope:
                res["per_change"][ch["id"]] = {"scope": "in", "status": Status.INCONCLUSIVE, "error": res["error"]}
            return res
        res["notes"] = sorted(ro.ex.notes | rn.ex.notes)
        base = list(dom) + [z3.Not(ro.ex.unwind), z3.Not(rn.ex.unwind)]
        try:
            diff = z3.Or(*[self._neq(a, b) for a, b in zip(ro.values, rn.values)])
        except Unsupported as e:
            res["status"] = Status.INCONCLUSIVE
            res["error"] = str(e)
            return res
        unwind_possible = self._check(list(dom), z3.Or(ro.ex.unwind, rn.ex.unwind))
        res["unwind_possible"] = unwind_possible != "unsat"
        r = self._check(base, diff)
        res["diff"] = r
        terms = {"old": (ro.values, ro.signed), "new": (rn.values, rn.signed)}
        if r == "sat":
            res["status"] = Status.MODEL_DIFF
            res["witnesses"] = self._witnesses(base, diff, inputs, meta, terms, self.opt.max_witnesses)
            res["region"] = self._region(base, diff, inputs, meta)
        elif r == "unsat":
            res["status"] = Status.MODEL_NO_DIFF
        else:
            res["status"] = Status.INCONCLUSIVE
            res["error"] = "솔버 시간 초과/판정 불가"
        if res["unwind_possible"]:
            res["notes"].append(f"반복 전개 한도({self.opt.loop_bound}) 밖의 실행이 있어 그 범위는 검증하지 않음")
        self._z3ctx = (base, diff, inputs, meta, ro, rn)
        # ---- 변경별: 분기/값 차이와 출력 차이의 관계 ----
        for ch in in_scope:
            pc = {"scope": "in"}
            evo = []
            evn = []
            for a in ch.get("watch", {}).get("old", []):
                t = _anchor_to_ast(a)
                if t:
                    evo += ro.ex.events.get(t, [])
            for a in ch.get("watch", {}).get("new", []):
                t = _anchor_to_ast(a)
                if t:
                    evn += rn.ex.events.get(t, [])
            if evo or evn:
                dv = self._events_formula(evo, evn)
                pc["divergence"] = self._check(base, dv)
                rwo, rwn = ret_watch.get(ch["id"], (set(), set()))
                ret_o = self._final_value([e for t in sorted(rwo) for e in ro.ex.events.get(t, [])])
                ret_n = self._final_value([e for t in sorted(rwn) for e in rn.ex.events.get(t, [])])
                extra = dict(terms)
                if ret_o is not None or ret_n is not None:
                    fname = (ch.get("unit_name") or "")
                    sg = is_signed((self.p_new.functions.get(ch.get("unit_new")) or {}).get("ret") or
                                   (self.p_old.functions.get(ch.get("unit_old")) or {}).get("ret"))
                    extra[f"{fname}() 반환값(old)"] = ([ret_o], sg)
                    extra[f"{fname}() 반환값(new)"] = ([ret_n], sg)
                if pc["divergence"] == "sat":
                    pc["divergence_region"] = self._region(base, dv, inputs, meta)
                    pc["divergence_formula"] = _pretty(z3.simplify(dv), meta)
                    pc["with_output_diff"] = self._check(base, z3.And(dv, diff))
                    if pc["with_output_diff"] == "sat":
                        pc["examples_diff"] = self._witnesses(base, z3.And(dv, diff), inputs, meta, extra, 2)
                    pc["examples_same_output"] = self._witnesses(base, z3.And(dv, z3.Not(diff)), inputs, meta, extra, 2)
                pc["divergence_term"] = dv
            # 변경 분리(그 함수의 변경만 적용한 혼합 프로그램)
            if ch.get("unit_old") and ch.get("unit_old") == ch.get("unit_new") and ch["unit_old"] in self.p_old.functions \
                    and ch["unit_new"] in self.p_new.functions:
                key = ch["unit_old"]
                iso = self._isolation_cache(key, fo, spec, inputs, ro, base, meta, terms, dom)
                pc["isolated"] = iso
            pc["status"] = self._change_status(pc)
            res["per_change"][ch["id"]] = pc
        # 추가/삭제된 함수: 그 함수를 호출하는 변경 함수의 분리 검증에 포함되면 그 결과를 따른다
        for ch in in_scope:
            if ch["type"] not in ("function_added", "function_removed"):
                continue
            callers = {c for c in (ch.get("callers") or []) if c}
            isos = [pc2 for cid2, pc2 in res["per_change"].items()
                    for ch2 in in_scope if ch2["id"] == cid2 and ch2.get("unit_new") in callers and pc2.get("isolated")]
            sts = [x["isolated"]["status"] for x in isos]
            if sts and all(s_ in ("sat", "unsat") for s_ in sts):
                pc = res["per_change"][ch["id"]]
                pc["status"] = Status.MODEL_DIFF if "sat" in sts else Status.MODEL_NO_DIFF
                pc["note"] = f"호출 함수({', '.join(sorted(c.split(':')[-1] for c in callers))})의 변경 분리 검증에 포함하여 판정"
        return res

    def _isolation_cache(self, key, fo, spec, inputs, ro, base, meta, terms, dom) -> dict:
        cache = self.__dict__.setdefault("_iso", {})
        ck = (key, spec["id"])
        if ck in cache:
            return cache[ck]
        ph = self.p_old.hybrid(self.p_new, {key}, f"old+{key}")
        rh = self._run(ph, fo, spec, inputs, set(), "h", self.go)
        if rh.error:
            out = {"status": "unsupported", "error": rh.error}
        else:
            try:
                d = z3.Or(*[self._neq(a, b) for a, b in zip(ro.values, rh.values)])
                b2 = list(dom) + [z3.Not(ro.ex.unwind), z3.Not(rh.ex.unwind)]
                r = self._check(b2, d)
                out = {"status": r, "function": key.split(":")[-1]}
                if r == "sat":
                    out["witnesses"] = self._witnesses(b2, d, inputs, meta,
                                                       {"old": (ro.values, ro.signed), "isolated": (rh.values, rh.signed)}, 2)
                    out["region"] = self._region(b2, d, inputs, meta)
            except Unsupported as e:
                out = {"status": "unsupported", "error": str(e)}
        cache[ck] = out
        return out

    @staticmethod
    def _change_status(pc: dict) -> str:
        iso = pc.get("isolated")
        if iso:
            if iso["status"] == "sat":
                return Status.MODEL_DIFF
            if iso["status"] == "unsat":
                return Status.MODEL_NO_DIFF
        if pc.get("with_output_diff") == "sat":
            return Status.MODEL_DIFF
        if pc.get("divergence") == "sat" and pc.get("with_output_diff") == "unsat":
            return Status.MODEL_NO_DIFF
        if pc.get("divergence") == "unsat":
            return Status.MODEL_NO_DIFF
        return Status.INCONCLUSIVE

    # ------------------------------------------------------------------
    def run(self, impact_results: List[dict]) -> List[dict]:
        out = []
        by_feature: Dict[str, List[dict]] = {}
        for ob in self.observations():
            rel = [ch for ch in impact_results
                   if any(f["feature"] == ob["feature"] for f in ch["impact"]["features"])]
            self.log(f"  동작 검증: {ob['feature']}/{ob['obs']} ({ob['spec']['function']}, 관련 변경 {len(rel)}개)")
            r = self.check_observation(ob, rel)
            if self.exec_mode != "off" and r.get("status") in (Status.MODEL_DIFF, Status.MODEL_NO_DIFF):
                r["exec"] = self.exec_compare(ob, r)
            out.append(r)
            by_feature.setdefault(ob["feature"], []).append(r)
        # 영향 결과에 반영
        rank = [Status.EXEC_DIFF, Status.MODEL_DIFF, Status.EXEC_NO_DIFF, Status.MODEL_NO_DIFF,
                Status.INCONCLUSIVE, Status.STATIC_CANDIDATE, Status.NO_PATH]
        for ch in impact_results:
            for f in ch["impact"]["features"]:
                checks = []
                for r in by_feature.get(f["feature"], []):
                    pc = r["per_change"].get(ch["id"])
                    if not pc:
                        continue
                    item = {"observation": r["observation"], "function": r["function"],
                            **{k: v for k, v in pc.items() if k != "divergence_term"}}
                    ex = r.get("exec") or {}
                    if pc.get("status") == Status.MODEL_DIFF and ex.get("status") == Status.EXEC_DIFF:
                        dv = pc.get("divergence_term")
                        hit = self._exec_attrib(dv, r, ex) if dv is not None else None
                        if hit or (hit is None and pc.get("isolated", {}).get("status") == "sat"):
                            item["status"] = Status.EXEC_DIFF
                            item["exec_examples"] = (hit or ex.get("diffs", []))[:3]
                    checks.append(item)
                if checks:
                    f["behavior"] = checks
                    in_scope = [c for c in checks if c.get("scope") == "in" and c.get("status")]
                    if in_scope and f["static_status"] == Status.STATIC_CANDIDATE:
                        best = min(in_scope, key=lambda c: rank.index(c["status"]) if c["status"] in rank else 99)
                        f["status"] = best["status"]
                        if best["status"] == Status.MODEL_NO_DIFF and any(c.get("scope") == "out" for c in checks):
                            f["status"] = Status.STATIC_CANDIDATE
                            f["status_note"] = "일부 관찰 모델에서는 차이가 없으나 모델 범위 밖 경로가 남아 있음"
                    # 모델 범위 밖 관찰만 있으면 정적 상태 유지
        for r in out:
            for pc in r["per_change"].values():
                pc.pop("divergence_term", None)
        return out

    def _exec_attrib(self, dv, r: dict, ex: dict) -> List[dict]:
        """실행 비교에서 출력이 달랐던 입력 중, 이 변경의 분기/값 차이 조건을 만족하는 입력."""
        if not getattr(self, "_z3ctx", None):
            return []
        hits = []
        for d in ex.get("diffs", [])[:200]:
            subs = []
            for k, step in enumerate(d["inputs"]):
                for name, val in step.items():
                    for x_name, meta in self._meta_by_name.get(r["observation"], {}).items():
                        if meta[0] == name and meta[1] == k:
                            subs.append((z3.BitVec(x_name, meta[5]), z3.BitVecVal(val % (1 << meta[5]), meta[5])))
            try:
                v = z3.simplify(z3.substitute(dv, *subs)) if subs else z3.simplify(dv)
            except z3.Z3Exception:
                continue
            if z3.is_true(v):
                hits.append(d)
        return hits

    # ------------------------------------------------------------------
    def exec_compare(self, ob: dict, model_res: dict) -> dict:
        spec = ob["spec"]
        gcc = shutil.which("gcc") or shutil.which("cc")
        if not gcc:
            return {"status": "skipped", "reason": "호스트 C 컴파일러 없음"}
        fo, fn = self._fn_key(self.go, spec["function"]), self._fn_key(self.gn, spec["function"])
        if not fo or self.go.node(fo).get("static") or self.gn.node(fn).get("static"):
            return {"status": "skipped", "reason": "관찰 함수가 static이라 외부 시험 하네스에서 호출 불가"}
        gvar = None
        if spec["kind"] == "global":
            gk = self.gn.globals_by_name(spec["variable"])
            if not gk or gk[0].startswith("gv:") and gk[0].count(":") > 1:
                return {"status": "skipped", "reason": "관찰 전역 변수가 static이라 하네스에서 접근 불가"}
            gvar = (spec["variable"], (self.gn.asts[gk[0]].get("ty") or {}).get("cn"))
        fast = self.p_new.functions[fn]
        params = fast.get("params", [])
        if any((p.get("ty") or {}).get("k") not in ("i", "b") for p in params):
            return {"status": "skipped", "reason": "정수가 아닌 인자"}
        steps = model_res["bounds"]["sequence"]
        vectors = self._vectors(spec, params, steps, model_res)
        work = self.workdir or tempfile.mkdtemp(prefix="vfia_exec_")
        os.makedirs(work, exist_ok=True)
        outs, notes = {}, []
        for side, g in (("old", self.go), ("new", self.gn)):
            exe, err, stubs = self._build(g, side, work, spec, fast, gvar, gcc)
            if not exe:
                return {"status": "skipped", "reason": f"{side} 빌드 실패: {err}"}
            if stubs:
                notes.append(f"{side}: 외부 심벌 스텁 사용 {', '.join(sorted(stubs))} (양쪽 동일 동작으로 가정)")
            try:
                inp = "\n".join(" ".join(str(v) for step in vec for v in step) for vec in vectors) + "\n"
                p = subprocess.run([exe], input=inp, capture_output=True, text=True, timeout=60)
                outs[side] = p.stdout.strip().split("\n")
            except subprocess.TimeoutExpired:
                return {"status": "skipped", "reason": f"{side} 실행 시간 초과"}
        diffs = []
        for vec, a, b in zip(vectors, outs["old"], outs["new"]):
            if a != b:
                diffs.append({"inputs": [dict(zip([p["name"] for p in params], step)) for step in vec],
                              "old": a.split(), "new": b.split()})
        meta = {}
        for k in range(steps):
            for p in params:
                ty = p.get("ty") or {}
                meta[f"in:{p['index']}:{p['name']}@{k}"] = (p["name"], k, is_signed(ty), None, None, bits_of(ty))
        self.__dict__.setdefault("_meta_by_name", {})[ob["obs"]] = meta
        return {"status": Status.EXEC_DIFF if diffs else Status.EXEC_NO_DIFF, "vectors": len(vectors),
                "diff_count": len(diffs), "diffs": diffs[:20], "notes": notes, "workdir": work,
                "compiler": os.path.basename(gcc)}

    def _vectors(self, spec: dict, params: List[dict], steps: int, model_res: dict) -> List[List[List[int]]]:
        doms = spec.get("params") or {}
        consts: Set[int] = set()
        for pc in model_res.get("per_change", {}).values():
            for reg in (pc.get("divergence_region") or {}).values():
                for v in reg:
                    if v is not None:
                        consts |= {v - 1, v, v + 1}
        vals = []
        for p in params:
            ty = p.get("ty") or {}
            w = bits_of(ty)
            s = is_signed(ty)
            lo, hi = (doms.get(p["name"]) if isinstance(doms.get(p["name"]), list) else [None, None])
            if ty.get("k") == "b":
                lo, hi = 0, 1
            lo = lo if lo is not None else (-(1 << (w - 1)) if s else 0)
            hi = hi if hi is not None else ((1 << (w - 1)) - 1 if s else (1 << w) - 1)
            cand = {lo, hi, (lo + hi) // 2} | {c for c in consts if lo <= c <= hi}
            if hi - lo <= 16:
                cand |= set(range(lo, hi + 1))
            vals.append(sorted(cand))
        step_vecs = [list(x) for x in itertools.product(*vals)] if vals else [[]]
        rnd = random.Random(0)
        if steps == 1:
            vecs = [[v] for v in step_vecs]
        else:
            total = len(step_vecs) ** steps
            if total <= self.opt.exec_max_vectors:
                vecs = [list(x) for x in itertools.product(step_vecs, repeat=steps)]
            else:
                vecs = [[rnd.choice(step_vecs) for _ in range(steps)] for _ in range(self.opt.exec_max_vectors)]
        for w in model_res.get("witnesses", []) or []:
            vec = [[st.get(p["name"], 0) for p in params] for st in w["inputs"]]
            if len(vec) == steps:
                vecs.append(vec)
        if len(vecs) > self.opt.exec_max_vectors:
            vecs = rnd.sample(vecs, self.opt.exec_max_vectors)
        return vecs

    def _build(self, g: AnalysisGraph, side: str, work: str, spec: dict, fast: dict, gvar, gcc: str):
        root = g.meta.get("root")
        objdir = os.path.join(work, side)
        os.makedirs(objdir, exist_ok=True)
        objs = []
        for t in g.meta.get("tus", []):
            src = os.path.join(root, t["file"])
            args = []
            it = iter(t.get("args", []))
            for a in it:
                if a == "-isystem":
                    nxt = next(it, "")
                    if "/clang/" in nxt:
                        continue
                    args += [a, nxt]
                    continue
                if a in ("-fparse-all-comments",) or a.startswith("-working-directory"):
                    continue
                args.append(a)
            obj = os.path.join(objdir, re.sub(r"[^\w]", "_", t["file"]) + ".o")
            cmd = [gcc] + args + ["-w", "-O0", "-fno-common", "-Dmain=vfia_unused_main", "-c", src, "-o", obj]
            p = subprocess.run(cmd, capture_output=True, text=True, cwd=root)
            if p.returncode != 0:
                return None, f"{t['file']}: {p.stderr.strip()[:300]}", None
            objs.append(obj)
        harness = os.path.join(objdir, "vfia_harness.c")
        params = fast.get("params", [])
        ret_cn = (fast.get("ret") or {}).get("cn", "int")
        proto = f"extern {ret_cn} {spec['function']}({', '.join((p.get('ty') or {}).get('cn', 'int') for p in params) or 'void'});"
        steps = int(spec.get("sequence") or (1 if spec["kind"] == "function_return" else self.opt.sequence_length))
        n = len(params)
        lines = ["#include <stdio.h>", "#include <stdlib.h>", "#include <unistd.h>", "#include <sys/wait.h>", proto]
        if gvar:
            lines.append(f"extern {gvar[1]} {gvar[0]};")
        lines += ["int main(void) {", f"  long long v[{max(1, steps * n)}];", "  for (;;) {", "    int ok = 1;",
                  f"    for (int i = 0; i < {steps * n}; i++) if (scanf(\"%lld\", &v[i]) != 1) {{ ok = 0; break; }}",
                  "    if (!ok) break;", "    fflush(stdout);", "    pid_t pid = fork();", "    if (pid == 0) {"]
        for k in range(steps):
            args = ", ".join(f"({(p.get('ty') or {}).get('cn', 'int')})v[{k * n + i}]" for i, p in enumerate(params))
            if spec["kind"] == "function_return":
                lines.append(f"      printf(\"%lld \", (long long){spec['function']}({args}));")
            else:
                lines.append(f"      {spec['function']}({args}); printf(\"%lld \", (long long){gvar[0]});")
        lines += ["      printf(\"\\n\"); fflush(stdout); _exit(0);", "    }", "    int st; waitpid(pid, &st, 0);",
                  "  }", "  return 0;", "}"]
        with open(harness, "w") as f:
            f.write("\n".join(lines) + "\n")
        exe = os.path.join(objdir, "vfia_harness")
        stubs: Set[str] = set()
        for attempt in range(3):
            extra = []
            if stubs:
                sp = os.path.join(objdir, "vfia_stubs.c")
                with open(sp, "w") as f:
                    for s in sorted(stubs):
                        f.write(f"long long {s}() {{ return 0; }}\n")
                extra = [sp]
            p = subprocess.run([gcc, "-w", "-O0", harness] + objs + extra + ["-o", exe], capture_output=True, text=True)
            if p.returncode == 0:
                return exe, None, stubs
            und = set(re.findall(r"undefined reference to [`']([\w]+)'", p.stderr))
            if not und or und <= stubs:
                return None, p.stderr.strip()[:300], None
            stubs |= und
        return None, "링크 실패", None


def _walk(n: dict):
    stack = [n]
    while stack:
        x = stack.pop()
        yield x
        for c in x.get("ch", []) or []:
            if c is not None:
                stack.append(c)


def _env_name(nm: str) -> str:
    """'vol:gv:g_rx_valid@2#0' -> 'g_rx_valid[단계2, 읽기0]',  'init:gv:x|f' -> 'x.f(초기값)'"""
    kind, _, rest = nm.partition(":")
    if kind == "vol":
        loc, _, sc = rest.rpartition("@")
        step, _, c = sc.partition("#")
        return f"{_short(loc)}[단계{step}, 읽기{c}]"
    return f"{_short(rest)}(초기값)"


def _short(loc: str) -> str:
    root, _, fld = loc.partition("|")
    return root.split(":")[-1] + ("." + fld.replace("|", ".") if fld else "")


def _pretty(expr, meta: Dict[str, Tuple]) -> str:
    s = str(expr)
    for name, m in meta.items():
        s = s.replace(name, m[0] if m[1] == 0 else f"{m[0]}[{m[1]}]")
    s = re.sub(r"vol:[\w:./|]+@\d+#\d+", lambda mm: _env_name(mm.group(0)), s)
    s = re.sub(r"\s+", " ", s)
    return s[:600]
