"""차이를 내는 입력·상태 자동 탐색 (SIL 차등 실행 기반).

1. 후보 값: 영향받는 함수(변경 전·후 모두)의 단일 변수 비교식에서 경계값(c-1, c, c+1)과 자료형 끝값을 모은다.
   인자 이름이 같으면 진입점 인자의 후보로, 전역·static 이면 초기 상태 후보로 쓴다.
2. 케이스: 한 변수씩 경계값으로 바꾼 케이스 → 무작위 조합 → 주기별 입력 시퀀스(같은 값 유지 후 전환) 순으로 만든다.
3. 변경 전/후를 같은 케이스로 실행해 차이를 찾고, 관찰 대상마다 가장 이른 단계에서 차이를 내는 케이스를
   더 단순한 입력(시퀀스 → 단일 값, 기본값 복원)으로 줄여 '최소 재현 입력'으로 보고한다.

탐색은 후보 값과 케이스 수 안에서만 의미가 있다. 차이가 없다는 결과가 모든 입력에서 같다는 증명은 아니다.
"""

from __future__ import annotations

import copy
import json
import random
import re
from collections import defaultdict
from pathlib import Path

from pycparser import c_ast

from .diff import INF, _pred, expr_of
from .frontend import BuildConfig
from .program import Program, display_path
from .sil import _scenario_stub_names, case_label, run_sil

_PH = re.compile(r"\$\{(\w+)\}")


# ---------------------------------------------------------------------------
# 후보 값
# ---------------------------------------------------------------------------


def _clip(v, dom):
    return v not in (INF, -INF) and dom[0] <= v <= dom[1]


def collect_candidates(progs: list[Program], func_names: set[str]) -> dict[tuple[str, str], dict]:
    """(종류, 이름) → {"values", "origins", "domain"}.

    종류: param(인자 이름) | state(전역·static) | ret(외부 함수 반환값) | const(지역 변수 등 연결할 곳이 없는 비교 상수)
    """
    out: dict[tuple[str, str], dict] = {}

    def put(key, dom, values, origin, extremes=True):
        ent = out.setdefault(key, {"values": set(), "origins": set(), "domain": dom, "extremes": extremes})
        ent["extremes"] = ent["extremes"] and extremes
        ent["values"].update(int(x) for x in values if _clip(x, dom))
        ent["origins"].add(origin)

    for prog in progs:
        keys = [k for k in prog.funcs if prog.func_display(k) in func_names]
        for fk in keys:
            for nid in prog.funcs[fk].nodes:
                n = prog.nodes[nid]
                if n.dead or n.kind not in ("cond", "stmt", "return"):
                    continue
                e = expr_of(n) if n.kind != "cond" else n.ast
                if n.kind != "cond" and isinstance(e, (c_ast.ID, c_ast.StructRef, c_ast.ArrayRef)):
                    continue  # 단순 복사(x = y)는 비교식이 아니다
                origin = f"{prog.func_display(fk)}:{n.line} `{n.text}`"
                if n.cases:  # switch: case 라벨 값과 어느 case 에도 없는 값
                    vals = set(n.cases) | {max(n.cases) + 1}
                    for path in prog.lvals(n.ast):
                        v = prog.vars.get(path[0])
                        if v is not None and v.kind in ("global", "static") and "[]" not in path and not _is_const(v):
                            put(("state", display_path(prog, path)), (-2**31, 2**31 - 1), vals, origin, False)
                        elif v is not None and v.kind == "param":
                            put(("param", v.name), (-2**31, 2**31 - 1), vals, origin, False)
                    put(("const", "*"), (-2**31, 2**31 - 1), vals, origin, False)
                    continue
                r = _pred(e) if e is not None else None
                if r is None:
                    continue
                _, var_expr, s = r
                bounds = [x for lo, hi in s for x in (lo - 1, lo, hi, hi + 1)]
                if isinstance(var_expr, c_ast.FuncCall):
                    site = prog.site_of.get(id(var_expr))
                    if site is not None and prog.sites[site].external:
                        put(("ret", prog.sites[site].name), (-2**31, 2**31 - 1), bounds, origin)
                    continue
                targets = []
                for path in prog.lvals(var_expr):
                    v = prog.vars.get(path[0])
                    if v is None:
                        continue
                    t, env = prog.path_type(path)
                    dom = (env.int_range(t) if env else None) or (-2**31, 2**31 - 1)
                    if v.kind == "param":
                        targets.append((("param", v.name), dom))
                    elif v.kind in ("global", "static") and "[]" not in path and not _is_const(v):
                        targets.append((("state", display_path(prog, path)), dom))
                for key, dom in targets:
                    put(key, dom, bounds, origin)
                put(("const", "*"), (-2**31, 2**31 - 1), bounds, origin, False)
    for ent in out.values():
        extra = (0, ent["domain"][0], ent["domain"][1]) if ent["extremes"] else (0,)
        ent["values"].update(x for x in extra if _clip(x, ent["domain"]))
    return out


def _extern_inputs(prog: Program, called: set[str]) -> list[dict]:
    """시나리오가 호출하는 함수에서 (직접·간접) 부르는 외부 함수의 반환값·출력 포인터 인자."""
    g = prog.call_graph()
    start = {k for k, f in prog.funcs.items() if f.name in called}
    seen, stack = set(), list(start)
    while stack:
        f = stack.pop()
        if f in seen:
            continue
        seen.add(f)
        stack.extend(g.get(f, ()))
    names = sorted({s.name for s in prog.sites if s.external and s.caller in seen})
    out = []
    for name in names:
        proto = prog.protos.get(name)
        if proto is None:
            continue
        fdecl, env = proto
        if env.is_scalar(fdecl.type) and not env.is_void(fdecl.type):
            out.append({"func": name, "kind": "ret", "index": None, "domain": env.int_range(fdecl.type)})
        params = fdecl.args.params if fdecl.args else []
        for i, p in enumerate(params):
            if not isinstance(p, (c_ast.Decl, c_ast.Typename)):
                continue
            core = env.core(p.type)
            if isinstance(core, c_ast.PtrDecl) and env.is_scalar(core.type) \
                    and "const" not in (getattr(core.type, "quals", None) or []):
                out.append({"func": name, "kind": "out", "index": i, "domain": env.int_range(core.type)})
    return out


def _is_const(v) -> bool:
    t = v.type
    quals = getattr(t, "quals", None) or []
    return "const" in quals


# ---------------------------------------------------------------------------
# 시나리오 분석
# ---------------------------------------------------------------------------


def _placeholders(steps, in_repeat=False, out=None):
    """자리표시자 → repeat 안에서 쓰이는가."""
    out = {} if out is None else out
    for st in steps:
        if "repeat" in st:
            _placeholders(st.get("steps", []), True, out)
            continue
        for name in _PH.findall(json.dumps(st, ensure_ascii=False)):
            out[name] = out.get(name, False) or in_repeat
    return out


def _set_cycles(steps, cycles):
    for st in steps:
        if "repeat" in st:
            st["repeat"] = cycles
            return


def _param_domains(prog: Program, steps) -> dict[str, tuple]:
    """시나리오에서 호출하는 함수의 인자 이름 → 자료형 범위."""
    called = set(_param_domains_names(steps))
    out = {}
    for f in prog.funcs.values():
        if f.name not in called:
            continue
        for k in f.params:
            v = prog.vars[k]
            rng = v.env.int_range(v.type) if v.env else None
            if rng:
                out[v.name] = rng
    return out


def _param_domains_names(steps):
    for st in steps:
        if "repeat" in st:
            yield from _param_domains_names(st.get("steps", []))
        elif "call" in st:
            yield st["call"]


# ---------------------------------------------------------------------------
# 케이스 생성
# ---------------------------------------------------------------------------


def _complexity(b: dict, defaults: dict) -> tuple:
    seqs = [v for v in b.values() if isinstance(v, list)]
    return (
        len(seqs),
        sum(len(set(v)) for v in seqs),
        sum(1 for k, v in b.items() if v != defaults.get(k)),
        sum(abs(x) for v in b.values() for x in (v if isinstance(v, list) else [v])),
    )


def generate_cases(variables: dict[str, dict], defaults: dict, cycles: int, budget: int, seed: int) -> list[dict]:
    rng = random.Random(seed)
    cases: list[dict] = []
    seen: set[str] = set()

    def add(b, kind):
        key = json.dumps(b, sort_keys=True)
        if key in seen or len(cases) >= budget:
            return
        seen.add(key)
        cases.append({"name": case_label(b), "bindings": b, "kind": kind})

    names = sorted(variables)
    add(dict(defaults), "기본값")
    for n in names:  # 한 번에 하나씩 경계값
        for v in variables[n]["pool"]:
            add({**defaults, n: v}, "경계값")
    seq_names = [n for n in names if variables[n]["in_repeat"] and cycles > 1]
    n_random = max(0, budget - len(cases))
    for i in range(n_random * 4):
        if len(cases) >= budget:
            break
        b = dict(defaults)
        if seq_names and i % 2 == 1:
            for n in seq_names:
                if rng.random() < 0.6:
                    b[n] = _random_sequence(rng, variables[n]["pool"], cycles)
            for n in names:
                if n not in seq_names and rng.random() < 0.3:
                    b[n] = rng.choice(variables[n]["pool"])
            add(b, "주기별 시퀀스")
        else:
            for n in names:
                if rng.random() < 0.5:
                    b[n] = rng.choice(variables[n]["pool"])
            add(b, "무작위 조합")
    return cases


def _random_sequence(rng, pool, cycles):
    """같은 값을 몇 주기 유지했다가 바꾸는 시퀀스 (카운터·디바운스·래치 검사용)."""
    seq, cur = [], rng.choice(pool)
    while len(seq) < cycles:
        hold = rng.randint(1, max(1, cycles // 2))
        seq += [cur] * hold
        cur = rng.choice(pool)
    seq = seq[:cycles]
    return seq if len(set(seq)) > 1 else seq[0]


# ---------------------------------------------------------------------------
# 실행·최소화
# ---------------------------------------------------------------------------


def _first_by_label(case: dict) -> dict[str, dict]:
    out = {}
    for d in case["diffs"]:
        out.setdefault(d["label"], d)
    return out


def _shrink_candidates(b: dict, defaults: dict) -> list[dict]:
    out = []
    for k, v in b.items():
        if isinstance(v, list):
            for x in sorted(set(v)):
                out.append({**b, k: x})
            # 바뀌는 지점까지만 남기기 (이후는 마지막 값 유지)
            for cut in range(1, len(v)):
                if v[cut] != v[cut - 1]:
                    out.append({**b, k: v[: cut + 1]})
                    break
            # 구간 합치기: i 번째 값을 앞(또는 뒤) 값으로 바꿔 서로 다른 값의 수를 줄인다
            for i in range(len(v)):
                for j in (i - 1, i + 1):
                    if 0 <= j < len(v) and v[j] != v[i]:
                        nv = v[:i] + [v[j]] + v[i + 1:]
                        out.append({**b, k: nv if len(set(nv)) > 1 else nv[0]})
            # 첫 값 버리기 (길이는 마지막 값으로 유지)
            if len(v) > 1:
                nv = v[1:] + [v[-1]]
                out.append({**b, k: nv if len(set(nv)) > 1 else nv[0]})
        if v != defaults.get(k):
            out.append({**b, k: defaults.get(k)})
    return out


def explore(old: Program, new: Program, result: dict, cfg: BuildConfig, out_dir: Path, *,
            base: dict | None = None, cycles: int = 6, budget: int = 300, seed: int = 1,
            timeout: float = 10.0, shrink_rounds: int = 6) -> dict:
    scn = copy.deepcopy(base or result["sil_template"])
    scn.pop("_comment", None)
    if base is None:
        _set_cycles(scn.get("steps", []), cycles)
    else:
        for st in scn.get("steps", []):
            if "repeat" in st:
                cycles = int(st["repeat"])
                break
    base_params = scn.pop("params", {}) or {}
    scn.pop("cases", None)
    ph = _placeholders(scn.get("steps", []))
    func_names = {f["func"] for f in result.get("functions", [])} | {c["func"] for c in result.get("changes", [])}
    cand = collect_candidates([old, new], func_names)
    pdom = _param_domains(new, scn.get("steps", []))

    variables: dict[str, dict] = {}
    for name, in_repeat in ph.items():
        pool = set()
        origins = set()
        for v in base_params.get(name, []):
            pool.update(v if isinstance(v, list) else [v])
        if base_params.get(name):
            origins.add("시나리오 params")
        ent = cand.get(("param", name))
        if ent:
            pool |= ent["values"]
            origins |= ent["origins"]
        dom = pdom.get(name) or (ent["domain"] if ent else None)
        if dom:
            pool.update(x for x in (0, dom[0], dom[1]))
            pool = {x for x in pool if dom[0] <= x <= dom[1]}
            origins.add(f"자료형 범위 [{dom[0]}, {dom[1]}]")
        if not pool:
            pool = {0}
        variables[name] = {"kind": "인자", "pool": sorted(pool), "in_repeat": in_repeat,
                           "origins": sorted(origins), "target": name}
    init = scn.setdefault("init", {})
    # 외부 함수(스텁)의 반환값·출력 포인터 값도 주기마다 바꿀 수 있는 입력으로 탐색한다
    # (기준 시나리오가 이미 값을 정한 스텁은 그대로 둔다)
    ext_steps = []
    fixed = set(_scenario_stub_names(scn.get("steps", [])))
    const_pool = cand.get(("const", "*"), {}).get("values", set())
    for x in _extern_inputs(new, set(_param_domains_names(scn.get("steps", [])))):
        if x["func"] in fixed:
            continue
        dom = x["domain"] or (-2**31, 2**31 - 1)
        pool = {v for v in const_pool | {0, 1} if dom[0] <= v <= dom[1]}
        ent = cand.get(("ret", x["func"])) if x["kind"] == "ret" else None
        if ent:
            pool |= {v for v in ent["values"] if dom[0] <= v <= dom[1]}
        if x["kind"] == "out":
            pool |= {dom[1]} if dom[1] < 2**31 else set()
        ph_name = f"ext_{x['func']}_" + ("ret" if x["kind"] == "ret" else f"out{x['index']}")
        variables[ph_name] = {"kind": "외부 함수 " + ("반환값" if x["kind"] == "ret" else f"출력 인자{x['index']}"),
                              "pool": sorted(pool), "in_repeat": True,
                              "origins": sorted(ent["origins"]) if ent else ["영향 경로의 비교 상수"],
                              "target": f"{x['func']}()"}
        if x["kind"] == "ret":
            ext_steps.append({"stub_return": {x["func"]: "${" + ph_name + "}"}})
        else:
            ext_steps.append({"stub_out": {x["func"]: {str(x["index"]): "${" + ph_name + "}"}}})
    if ext_steps:
        loop = next((st for st in scn.get("steps", []) if "repeat" in st), None)
        if loop is not None:
            loop["steps"] = ext_steps + loop.get("steps", [])
        else:
            scn["steps"] = ext_steps + scn.get("steps", [])
    observe_names = {o if isinstance(o, str) else o.get("name", o["expr"]) for o in scn.get("observe", [])}
    for (kind, name), ent in sorted(cand.items()):
        if kind != "state" or name in init:
            continue
        ph_name = "st_" + re.sub(r"\W", "_", name)
        init[name] = "${" + ph_name + "}"
        variables[ph_name] = {"kind": "초기 상태", "pool": sorted(ent["values"]), "in_repeat": False,
                              "origins": sorted(ent["origins"]), "target": name}
        if name not in observe_names:
            scn.setdefault("observe", []).append(name)
    defaults = {}
    for n, v in variables.items():
        bp = base_params.get(n)
        if bp:
            first = bp[0]
            defaults[n] = first
        elif v["kind"] == "초기 상태":
            defaults[n] = 0 if 0 in v["pool"] else v["pool"][0]
        else:
            defaults[n] = 0 if 0 in v["pool"] else v["pool"][len(v["pool"]) // 2]

    cases = generate_cases(variables, defaults, cycles, budget, seed)
    scn["cases"] = [{"name": c["name"], "bindings": c["bindings"]} for c in cases]
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "explore_scenario.json").write_text(json.dumps(scn, ensure_ascii=False, indent=2), encoding="utf-8")
    sil = run_sil(old.root, new.root, scn, out_dir / "sil", cfg, timeout)
    kind_of = {c["name"]: c["kind"] for c in cases}

    state_vars = {n for n, v in variables.items() if v["kind"] == "초기 상태"}

    def input_only(b):
        return all(b.get(n) == defaults.get(n) for n in state_vars)

    # 관찰 대상별 가장 이른 차이 케이스: (관찰 대상, any|input) — input 은 초기 상태를 바꾸지 않은 케이스
    witnesses: dict[tuple[str, str], dict] = {}

    def consider(c):
        changed = False
        if c["status"] != "diff":
            return False
        for lab, d in _first_by_label(c).items():
            for mode in ("any", "input"):
                if mode == "input" and not input_only(c["bindings"]):
                    continue
                cur = witnesses.get((lab, mode))
                key = (d["step"], _complexity(c["bindings"], defaults))
                better = cur is None or (key < cur["key"] if cur.get("round", 0) == 0 and not cur.get("shrunk")
                                         else _complexity(c["bindings"], defaults) < _complexity(cur["bindings"], defaults))
                if better:
                    witnesses[(lab, mode)] = {"key": key, "bindings": c["bindings"], "step": d["step"],
                                              "step_label": d["step_label"], "old": d["old"], "new": d["new"]}
                    changed = True
        return changed

    for c in sil["cases"]:
        consider(c)
    for w in witnesses.values():
        w["shrunk"] = True  # 이후에는 더 단순한 입력일 때만 교체

    # 최소화: 더 단순한 입력으로도 같은 관찰 대상에서 차이가 나면 바꾼다
    for rnd in range(shrink_rounds):
        trial: dict[str, dict] = {}
        for (lab, mode), w in witnesses.items():
            for b in _shrink_candidates(w["bindings"], defaults):
                if _complexity(b, defaults) < _complexity(w["bindings"], defaults) and (mode == "any" or input_only(b)):
                    trial[json.dumps(b, sort_keys=True)] = b
        if not trial:
            break
        s2 = copy.deepcopy(scn)
        s2["cases"] = [{"name": case_label(b), "bindings": b} for b in trial.values()]
        res = run_sil(old.root, new.root, s2, out_dir / f"shrink{rnd + 1}", cfg, timeout)
        improved = False
        for c in res["cases"]:
            if consider(c):
                improved = True
        for w in witnesses.values():
            w["shrunk"] = True
        if not improved:
            break

    # 한 변수씩 바꾼 케이스(나머지는 기본값): 값마다 차이 여부와 처음 달라진 관찰 대상
    by_value: dict[str, dict] = defaultdict(dict)
    for c in sil["cases"]:
        if kind_of.get(c["name"]) not in ("경계값", "기본값"):
            continue
        changed = [n for n, v in c["bindings"].items() if v != defaults.get(n)]
        names = changed or list(c["bindings"])
        for n in names:
            v = c["bindings"][n]
            by_value[n][v] = {"diff": c["status"] == "diff",
                              "first": (f"#{c['first_step']} " + ", ".join(sorted(_first_by_label(c))[:3])) if c["diffs"] else ""}

    witness_cases = []
    for _, w in sorted(witnesses.items()):
        witness_cases.append({"name": case_label(w["bindings"]), "bindings": w["bindings"]})
    wscn = copy.deepcopy(scn)
    uniq = {json.dumps(c["bindings"], sort_keys=True): c for c in witness_cases}
    wscn["cases"] = list(uniq.values())
    if wscn["cases"]:
        (out_dir / "explore_witness.json").write_text(json.dumps(wscn, ensure_ascii=False, indent=2), encoding="utf-8")

    kinds = defaultdict(int)
    for c in cases:
        kinds[c["kind"]] += 1
    summary = {
        "cycles": cycles,
        "seed": seed,
        "budget": budget,
        "case_kinds": dict(kinds),
        "cases": len(cases),
        "diff_cases": sil["diff_cases"],
        "error_cases": sum(1 for c in sil["cases"] if c["status"] == "error"),
        "variables": {n: {k: v[k] for k in ("kind", "pool", "in_repeat", "origins", "target")} for n, v in variables.items()},
        "defaults": defaults,
        "witnesses": {lab: {k: v for k, v in w.items() if k not in ("key", "shrunk")}
                      for (lab, mode), w in witnesses.items() if mode == "any"},
        "witnesses_input_only": {lab: {k: v for k, v in w.items() if k not in ("key", "shrunk")}
                                 for (lab, mode), w in witnesses.items() if mode == "input"},
        "state_vars": sorted(state_vars),
        "by_value": {n: {str(v): d[v] for v in sorted(d)} for n, d in by_value.items()},
        "observables": sil["observables"],
        "stubs": sil["stubs"],
        "kind_of_case": kind_of,
    }
    (out_dir / "explore_result.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str),
                                                 encoding="utf-8")
    (out_dir / "explore_report.md").write_text(render_explore_md(summary), encoding="utf-8")
    summary["sil"] = sil
    return summary


def _fmt_b(b: dict, defaults: dict) -> str:
    parts = [f"{k}={json.dumps(v)}" for k, v in b.items() if v != defaults.get(k)]
    return ", ".join(parts) or "기본값"


def render_explore_md(s: dict) -> str:
    out = ["# 차이를 내는 입력·상태 자동 탐색", "",
           f"- 케이스 **{s['cases']}개** 실행 (" + ", ".join(f"{k} {v}" for k, v in s["case_kinds"].items()) +
           f") · 주기 {s['cycles']} · seed {s['seed']}",
           f"- 변경 전/후 결과가 다른 케이스 **{s['diff_cases']}개**" +
           (f", 양쪽 모두 비정상 종료 {s['error_cases']}개" if s["error_cases"] else ""),
           "- 기본값: " + (", ".join(f"{k}={v}" for k, v in s["defaults"].items()) or "없음"),
           "", "## 1. 탐색한 입력·상태와 후보 값의 출처", "",
           "| 이름 | 종류 | 대상 | 후보 값 | 주기별 시퀀스 | 출처 (영향 경로의 비교식) |", "|---|---|---|---|---|---|"]
    for n, v in sorted(s["variables"].items()):
        pool = ", ".join(map(str, v["pool"][:16])) + (" …" if len(v["pool"]) > 16 else "")
        org = "<br>".join(o.replace("|", "\\|") for o in v["origins"][:6])
        out.append(f"| `{n}` | {v['kind']} | `{v['target']}` | {pool} | {'예' if v['in_repeat'] else '-'} | {org} |")
    if not s["variables"]:
        out.append("| - | - | - | 바꿀 입력이 없습니다 (시나리오에 ${...} 자리표시자가 없고 상태 후보도 없음) | - | - |")
    out += ["", "## 2. 관찰 대상별 최소 재현 입력", "",
            "차이를 내는 케이스를 더 단순한 입력으로 줄인 결과입니다 (기본값과 다른 값만 표시). "
            "'입력만'은 초기 상태를 바꾸지 않고 입력(인자·주기별 시퀀스)만으로 재현한 경우입니다. "
            "초기 상태를 바꾼 재현은 그 상태가 실제로 도달 가능한지 따로 확인해야 합니다.", "",
            "| 관찰 대상 | 재현 방법 | 입력·상태 | 최초 차이 단계 | 변경 전 → 변경 후 |", "|---|---|---|---|---|"]
    labels = sorted(set(s["witnesses"]) | set(s["witnesses_input_only"]))
    for lab in labels:
        rows = []
        wi = s["witnesses_input_only"].get(lab)
        wa = s["witnesses"].get(lab)
        if wi:
            rows.append(("입력만", wi))
        if wa and (wi is None or wa["bindings"] != wi["bindings"]):
            rows.append(("초기 상태 포함" if any(wa["bindings"].get(n) != s["defaults"].get(n) for n in s["state_vars"])
                         else "입력만", wa))
        if not wi:
            rows.append(("입력만", None))
        for how, w in rows:
            if w is None:
                out.append(f"| `{lab}` | 입력만 | 탐색 범위에서 찾지 못함 | - | - |")
                continue
            out.append(f"| `{lab}` | {how} | {_fmt_b(w['bindings'], s['defaults'])} | #{w['step']} {w['step_label']} | "
                       f"`{w['old']}` → `{w['new']}` |")
    if not labels:
        out.append("| - | - | 탐색한 범위에서 차이를 내는 입력을 찾지 못했습니다 | - | - |")
    out += ["", "## 3. 값별 차이 여부 (한 변수만 바꾸고 나머지는 기본값)", "",
            "어느 값 구간에서 변경 전/후 결과가 갈리는지 보여 줍니다. ● 차이 있음, ○ 같음.", ""]
    for n, vals in sorted(s["by_value"].items()):
        out.append(f"- `{n}`: " + ", ".join(f"{v} {'●' if c['diff'] else '○'}" for v, c in vals.items()))
        firsts = [f"{v} → {c['first']}" for v, c in vals.items() if c["diff"]][:4]
        if firsts:
            out.append("  - 처음 달라진 단계·대상: " + "; ".join(firsts))
    same = sorted(l for l, o in s["observables"].items() if not o["diff_cases"])
    out += ["", "## 4. 차이가 없었던 관찰 대상", "",
            (", ".join(f"`{l}`" for l in same) or "없음") + " — 탐색한 입력·주기 안에서 차이가 없었다는 뜻이며, 같다는 증명이 아닙니다.",
            "", "## 5. 재현", "",
            "- 전체 케이스: `explore_scenario.json`, 최소 재현 케이스: `explore_witness.json`",
            "- `python3 change_impact.py sil --old … --new … --scenario explore_witness.json` 으로 다시 실행할 수 있습니다.",
            "- 후보 값은 영향 경로의 단일 변수 비교식 경계값과 자료형 끝값입니다. 여러 변수가 얽힌 조건·산술식에서만 생기는 차이는 "
            "무작위 조합·시퀀스로만 탐색되므로 놓칠 수 있습니다. 케이스 수(`--max-cases`)·주기(`--cycles`)·seed 를 바꿔 넓힐 수 있습니다.",
            ""]
    return "\n".join(out)
