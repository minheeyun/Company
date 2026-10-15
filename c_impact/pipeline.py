"""전체 분석 파이프라인: 1~6단계를 순서대로 실행하고 결과 dict 를 만든다."""

from __future__ import annotations

import datetime as _dt

from . import __version__
from .callgraph import CallGraph
from .cfg import ENTRY, compare_function_cfg
from .change_detect import CATEGORY, detect_changes
from .compdb import CompileDB
from .config import AnalysisConfig
from .defects import DefectAnalyzer
from .impact import EDGE_LABEL, LEVELS, ImpactAnalyzer
from .project import ParseCache, load_project
from .roles import Roles, key_name
from .testgen import TestGenerator

PRIO = ["High", "Medium", "Low"]


class Options:
    """파이프라인 옵션(CLI 와 테스트에서 공용)."""

    def __init__(self, **kw):
        self.compdb = kw.get("compdb")
        self.compdb_root = kw.get("compdb_root")
        self.include_dirs = kw.get("include_dirs") or []
        self.defines = kw.get("defines") or []
        self.std = kw.get("std") or "c99"
        self.target = kw.get("target")
        self.extra_args = kw.get("extra_args") or []
        self.cache_dir = kw.get("cache_dir")
        self.jobs = kw.get("jobs") or 1
        self.target_files = kw.get("target_files")
        self.per_change = kw.get("per_change", True)
        self.label = kw.get("label", "")
        self.source_meta = kw.get("source_meta") or {}


def analyze(base_root: str, curr_root: str, config: AnalysisConfig | None = None,
            options: Options | None = None, progress=None) -> dict:
    cfg = config or AnalysisConfig()
    opt = options or Options()
    log = progress or (lambda m: None)
    cache = ParseCache(opt.cache_dir) if opt.cache_dir else None
    compdb = CompileDB(opt.compdb, opt.compdb_root or curr_root) if opt.compdb else None
    common = dict(compdb=compdb, include_dirs=opt.include_dirs, defines=opt.defines, std=opt.std,
                  target=opt.target, extra_args=opt.extra_args, cache=cache, external_globs=cfg.external_globs,
                  exclude_globs=cfg.exclude_globs, jobs=opt.jobs)

    log("[1/6] AST 구조 분석 (Baseline)")
    base = load_project(base_root, "baseline", **common)
    log("[1/6] AST 구조 분석 (Current)")
    curr = load_project(curr_root, "current", **common)
    roles_b, roles_c = Roles(base, cfg), Roles(curr, cfg)

    log("[2/6] 변경점 탐지")
    cs = detect_changes(base, curr, roles_b, roles_c, cfg, only_files=opt.target_files)

    log("[3/6] Call Graph / Def-Use")
    cg = CallGraph(curr, cfg)
    cg_b = CallGraph(base, cfg)

    log("[4/6] CFG 비교")
    cfg_cmp = {}
    for fid, info in cs.functions.items():
        if info.get("status") != "modified" or fid not in base.functions or fid not in curr.functions:
            continue
        cfg_cmp[fid] = compare_function_cfg(fid, base.functions[fid], curr.functions[fid],
                                            cs.stmt_map.get(fid, {}), cfg.platform, cfg.max_paths, cfg.loop_unroll)

    log("[5/6] 영향 전파")
    ia = ImpactAnalyzer(base, curr, cs, cg, roles_c, cfg, base_cg=cg_b)
    per_change = []
    if opt.per_change:
        for ch in cs.changes:
            ia.run([ch["id"]])
            sm = ia.summarize()
            pc = _change_impact(ia, ch, sm)
            pc["graph"] = _graph(ia, cs, sm, curr)
            per_change.append(pc)
    ia.run()
    summary = ia.summarize()
    # 본문은 같지만 사용하는 정의(매크로/전역/자료형)가 바뀐 함수도 CFG(도달성/고정 분기) 비교
    for (fid, n), f in ia.best.items():
        if fid in cfg_cmp or f.edge not in ("MACRO", "GLOBAL_DEF", "TYPE") or fid not in base.functions:
            continue
        fb, fc = base.functions[fid], curr.functions[fid]
        if fb["norm_hash"] == fc["norm_hash"]:
            ident = {sid: sid for sid in curr.stmt_index(fid)}
            cfg_cmp[fid] = compare_function_cfg(fid, fb, fc, ident, cfg.platform, cfg.max_paths, cfg.loop_unroll)
            cfg_cmp[fid]["reason"] = "사용하는 정의(매크로/전역/자료형) 변경"

    log("[6/6] 잠재 결함 / 회귀 테스트 / 보고서 데이터")
    defects = DefectAnalyzer(base, curr, cs, ia, cg, roles_c, cfg, cfg_cmp).run()
    tests = TestGenerator(base, curr, cs, ia, summary, per_change, cg, roles_c, cfg, defects, cfg_cmp).run()

    funcs = _function_rows(ia, summary, cs, defects, roles_c, cg)
    table = _impact_table(cs, per_change, funcs, defects)
    result = {
        "tool": {"name": "c_impact", "version": __version__},
        "generated_at": _dt.datetime.now().isoformat(timespec="seconds"),
        "label": opt.label,
        "build_config": cfg.build_config_name,
        "analysis_mode": curr.mode,
        "completeness": _completeness(base, curr),
        "inputs": {"baseline": base.root, "current": curr.root, **opt.source_meta,
                   "compile_commands": opt.compdb, "include_dirs": opt.include_dirs, "defines": opt.defines,
                   "target_files": sorted(opt.target_files) if opt.target_files else None},
        "config": cfg.to_dict(),
        "projects": {"baseline": base.to_summary(), "current": curr.to_summary()},
        "change_summary": _change_summary(cs),
        "changes": cs.changes,
        "non_semantic": cs.non_semantic,
        "callgraph": cg.to_dict(),
        "call_chains": {fid: cg.chains_to_entries(fid, cfg.max_call_chains)
                        for fid in {c.get("function") for c in cs.changes if c.get("function") in curr.functions}},
        "roles": roles_c.to_dict(),
        "cfg_comparison": {k: {kk: vv for kk, vv in v.items() if not kk.startswith("_")} for k, v in cfg_cmp.items()},
        "impact": {
            "per_change": per_change,
            "functions": funcs,
            "no_impact": summary["no_impact"],
            "variables": _variables(ia, curr, roles_c),
            "outputs": _outputs(ia, roles_c),
            "files": _files(funcs),
        },
        "impact_table": table,
        "defects": defects,
        "tests": tests,
        "requirements_impacted": _requirements(cfg, funcs),
        "uncertainty": _uncertainty(ia, cg, base, curr, cfg, cs),
        "manual_review": _manual_review(cs, funcs, defects, cg, curr),
        "graph": _graph(ia, cs, summary, curr),
    }
    result["summary"] = _summary(result)
    return result


# ---------------------------------------------------------------------------
def _completeness(base, curr):
    notes = []
    if curr.mode != "compdb":
        notes.append("compile_commands.json 없이(또는 일부만) 분석한 제한 모드 — include/매크로 정의가 실제 빌드와 다를 수 있어 "
                     "전체 프로젝트에 대한 완전한 분석이 아님")
    if curr.failures or base.failures:
        notes.append(f"파싱 오류 파일: {len(curr.failures)}(Current) / {len(base.failures)}(Baseline)")
    if curr.unparsed_headers:
        notes.append(f"어떤 .c 에서도 포함되지 않아 분석되지 않은 헤더 {len(curr.unparsed_headers)}개")
    return {"complete": not notes, "notes": notes}


def _change_summary(cs):
    by_cat = {}
    for c in cs.changes:
        by_cat[CATEGORY.get(c["category"], c["category"])] = by_cat.get(CATEGORY.get(c["category"], c["category"]), 0) + 1
    return {"files": cs.files, "functions": cs.functions, "by_category": by_cat,
            "changed_functions": sorted({c["function"] for c in cs.changes if c.get("function")}),
            "changed_variables": sorted({c["variable"] for c in cs.changes if c.get("variable")}),
            "changed_macros": sorted({c["macro_name"] for c in cs.changes if c.get("macro_name")}),
            "changed_conditions": [{"id": c["id"], "function": c.get("function"),
                                    "from": (c.get("detail") or {}).get("from"), "to": (c.get("detail") or {}).get("to")}
                                   for c in cs.changes if "CONDITION_CHANGED" in c["types"]],
            "warnings": cs.warnings}


def _change_impact(ia, ch, sm):
    out = {"change": ch["id"], "location": ch.get("curr") and {k: ch["curr"][k] for k in ("file", "line")} or
           ch.get("base") and {k: ch["base"][k] for k in ("file", "line")}, "function": ch.get("function"),
           "types": ch["types"], "category": ch["category"], "functions": {}, "external_outputs": []}
    for fid, e in sm["functions"].items():
        first = next((f for f in e.get("facts", []) if f.level == e["level"]), None) or (
            e["facts"][0] if e.get("facts") else None)
        path = ia.path_of(first) if first else []
        if e.get("call_context_only"):
            via = e.get("via_callee", [])
            path = [{"function": fid, "name": e["name"], "edge": "CALL", "edge_label": EDGE_LABEL["CALL"],
                     "certain": e["level"] != "POTENTIAL",
                     "reason": f"관측 가능한 효과가 바뀐 {', '.join(key_name(v) for v in via)} 를 호출(호출 체인)"}]
        out["functions"][fid] = {"level": e["level"], "edges": sorted(e["edges"]),
                                 "call_context_only": bool(e.get("call_context_only")), "path": path,
                                 "nodes": [{k: n[k] for k in ("line", "code", "level", "edge", "roles")}
                                           for n in e.get("nodes", [])][:20]}
    for s in ia.external_sinks:
        out["external_outputs"].append({k: s[k] for k in ("function", "line", "callee", "call", "level", "certain")})
    out["no_impact_callers"] = []
    return out


def _priority(level, roles, change_cats, edges, has_high_risk, call_only):
    if call_only:
        return "Low" if level == "POTENTIAL" else "Medium" if roles & {"OUTPUT", "STATE", "FAULT"} else "Low"
    if level == "DIRECT":
        return "High"
    if level == "INDIRECT":
        if has_high_risk or roles & {"OUTPUT", "STATE", "FAULT"} or change_cats & {"CONDITION", "STATE", "TIMER_COUNTER"}:
            return "High"
        return "Medium"
    if level == "POTENTIAL":
        return "Medium" if roles & {"OUTPUT", "STATE", "FAULT"} else "Low"
    return "Low"


def _function_rows(ia, summary, cs, defects, roles, cg):
    rows = []
    cat_of = {c["id"]: c["category"] for c in cs.changes}
    high_risk_funcs = {d["function"] for d in defects if d["severity"] == "High" and d.get("function")}
    for fid, e in summary["functions"].items():
        node_roles = {r for n in e.get("nodes", []) for r in n.get("roles", [])}
        if roles.is_output_func(fid):
            node_roles.add("OUTPUT")
        cats = {cat_of.get(c) for c in e["changes"]}
        prio = _priority(e["level"], node_roles, cats, e["edges"], fid in high_risk_funcs, e.get("call_context_only"))
        facts = sorted(e.get("facts", []), key=lambda f: (LEVELS.index(f.level), f.depth))[:3]
        rows.append({
            "function": fid, "name": e["name"], "file": e["file"], "level": e["level"], "priority": prio,
            "edges": sorted(e["edges"]), "edge_labels": [EDGE_LABEL.get(x, x) for x in sorted(e["edges"])],
            "changes": sorted(e["changes"]), "roles": sorted(node_roles),
            "call_context_only": bool(e.get("call_context_only")), "via_callee": e.get("via_callee", []),
            "observable_effects": e.get("observable_effects", []),
            "reason": e.get("reason"),
            "nodes": [{k: n[k] for k in ("line", "code", "level", "edge", "reason", "roles")}
                      for n in sorted(e.get("nodes", []), key=lambda n: n["line"])][:30],
            "paths": [ia.path_of(f) for f in facts],
            "is_entry": fid in cg.entry_points,
        })
    rows.sort(key=lambda r: (LEVELS.index(r["level"]), PRIO.index(r["priority"]), r["function"]))
    return rows


def _impact_table(cs, per_change, funcs, defects):
    """요구 출력 ②: 변경 위치 × 영향 함수 표."""
    rows = []
    risk_by_change = {}
    for d in defects:
        if d.get("change"):
            risk_by_change.setdefault(d["change"], []).append(d)
    fmap = {f["function"]: f for f in funcs}
    chmap = {c["id"]: c for c in cs.changes}
    for pc in per_change:
        ch = chmap[pc["change"]]
        loc = ch.get("curr") or ch.get("base") or {}
        for fid, e in sorted(pc["functions"].items(), key=lambda kv: LEVELS.index(kv[1]["level"])):
            fr = fmap.get(fid, {})
            edges = [x for x in e["edges"] if x not in ("SEED",)] or ["SEED"]
            path = " → ".join(f"{p.get('name')}{':L' + str(p['line']) if p.get('line') else ''}"
                              f"[{p['edge_label']}]" for p in e["path"])
            prio = _priority(e["level"], set(fr.get("roles", [])), {ch["category"]}, set(edges),
                             any(r["severity"] == "High" for r in risk_by_change.get(ch["id"], [])),
                             e.get("call_context_only"))
            rows.append({
                "change": ch["id"], "location": f"{loc.get('file')}:{key_name(ch.get('function') or '') or '-'}:"
                                                f"L{loc.get('line')}",
                "change_category": CATEGORY.get(ch["category"], ch["category"]), "change_types": ch["labels"],
                "function": fid, "function_name": key_name(fid), "file": fr.get("file"),
                "path_type": [EDGE_LABEL.get(x, x) for x in edges],
                "dependency": sorted({_dep_class(x) for x in edges}),
                "level": e["level"], "evidence": path, "priority": prio,
            })
    return rows


def _dep_class(edge):
    if edge in ("CONTROL", "CALL_COND", "INVOKED"):
        return "Control"
    if edge in ("CALL", "FP_CALL", "SIGNATURE"):
        return "Call"
    if edge in ("SEED",):
        return "Change"
    return "Data"


def _variables(ia, curr, roles):
    out = {}
    for f in ia.facts:
        if f.node is None or f.node == ENTRY:
            continue
        st = curr.stmt_index(f.fid).get(f.node)
        if not st:
            continue
        for d in st.get("defs", []):
            if d.startswith("G:") or d.startswith("*"):
                if f.def_keys is not None and d not in f.def_keys:
                    continue
                ent = out.setdefault(d, {"key": d, "name": key_name(d), "level": f.level, "writers": set(),
                                         "readers": set(), "roles": []})
                if LEVELS.index(f.level) < LEVELS.index(ent["level"]):
                    ent["level"] = f.level
                ent["writers"].add(f"{key_name(f.fid)}:L{st['line']}")
    for k, ent in out.items():
        if k.startswith("G:"):
            for gfid, m in ia.readers(k):
                ent["readers"].add(f"{key_name(gfid)}:L{ia.cfgs[gfid].nodes[m]['line']}")
        ent["writers"] = sorted(ent["writers"])
        ent["readers"] = sorted(ent["readers"])
        ent["roles"] = [r for r, fn in (("STATE", roles.is_state), ("TIMER_COUNTER", roles.is_counter),
                                        ("FAULT", roles.is_fault), ("OUTPUT", roles.is_output_var)) if fn(k)]
    return sorted(out.values(), key=lambda e: (LEVELS.index(e["level"]), e["key"]))


def _outputs(ia, roles):
    seen = {}
    for s in ia.external_sinks:
        key = (s["function"], s["line"], s["callee"])
        lvl = s["level"] if s["certain"] else "POTENTIAL"
        if key not in seen or LEVELS.index(lvl) < LEVELS.index(seen[key]["level"]):
            seen[key] = {"function": s["function"], "line": s["line"], "callee": s["callee"], "call": s["call"],
                         "level": lvl, "is_output": roles.is_output_func(s["callee"])}
    return sorted(seen.values(), key=lambda x: (LEVELS.index(x["level"]), x["function"], x["line"]))


def _files(funcs):
    out = {}
    for f in funcs:
        e = out.setdefault(f["file"], {"file": f["file"], "functions": [], "best_level": f["level"]})
        e["functions"].append(f["name"])
        if LEVELS.index(f["level"]) < LEVELS.index(e["best_level"]):
            e["best_level"] = f["level"]
    return sorted(out.values(), key=lambda e: (LEVELS.index(e["best_level"]), e["file"]))


def _requirements(cfg, funcs):
    out = []
    names = {f["name"]: f for f in funcs}
    for rid, r in (cfg.requirements or {}).items():
        hit = [names[n] for n in r.get("functions", []) if n in names]
        if hit:
            best = min(hit, key=lambda f: LEVELS.index(f["level"]))
            out.append({"id": rid, "title": r.get("title", ""), "level": best["level"],
                        "functions": [f["name"] for f in hit], "expected": r.get("expected")})
    return out


def _uncertainty(ia, cg, base, curr, cfg, cs):
    assumptions = [
        f"빌드 설정 '{cfg.build_config_name}' 의 전처리 결과만 분석(비활성 #if 영역은 의미 분석 제외)",
        "포인터 별칭은 흐름 비민감 points-to(p=&x, 배열, 포인터 복사)만 추적, 포인터 연산·캐스팅·외부 함수가 만든 포인터는 미추적",
        "외부(정의 없는) 함수는 인자로 받은 포인터 대상을 수정하지 않는다고 가정하지 않음 — 외부 출력으로만 표시",
        "인터럽트/태스크 동시성은 설정된 태스크 정보와 호출 관계에 기반한 정적 추정",
        f"함수 경계 전파 깊이 {cfg.max_depth}, 함수당 경로 {cfg.max_paths}개, 반복 {cfg.loop_unroll}회 전개 한도",
        "경로 실행 가능성은 '변수 op 상수' 구간 추론으로만 판단하며 실행 가능성을 확정하지 않음",
        "NO_IMPACT_CONFIRMED 는 위 가정과 분석 범위 내에서의 판정",
    ]
    lim = sorted(set(ia.limitations))
    if not cfg.tasks:
        lim.append("태스크/ISR 실행 정보가 제공되지 않아 동시성(공유자원) 분석을 수행하지 않음")
    if curr.mode != "compdb":
        lim.append("compile_commands.json 미사용(제한 모드) — 실제 컴파일 옵션과 다를 수 있음")
    return {
        "assumptions": assumptions, "limitations": lim,
        "unresolved_indirect_calls": cg.unresolved,
        "potential_indirect_edges": [e for e in cg.edges if e["kind"] == "indirect"],
        "parse_failures": {"baseline": base.failures, "current": curr.failures},
        "unparsed_headers": curr.unparsed_headers,
        "inactive_regions": curr.skipped,
        "truncated": ia.truncated,
        "external_functions": sorted({e["callee"] for e in cg.edges if e.get("external")}),
    }


def _manual_review(cs, funcs, defects, cg, curr):
    items = []
    for c in cs.changes:
        if set(c["types"]) & {"INACTIVE_CODE_CHANGED", "UNMAPPED_CHANGE", "UNPARSED_FILE_CHANGE", "FUNCTION_MODIFIED",
                              "FUNCTION_MOVED"}:
            items.append({"item": c["description"], "reason": "AST 의미 분석 범위 밖 변경", "ref": c["id"]})
    for f in funcs:
        if f["level"] == "POTENTIAL":
            items.append({"item": f"{f['name']} ({f['file']})",
                          "reason": f.get("reason") or "불확실 간선: " + ", ".join(f["edge_labels"]), "ref": f["function"]})
    for d in defects:
        if d["status"] != "CONFIRMED_BY_STATIC_ANALYSIS" and d["severity"] == "High":
            items.append({"item": d["description"], "reason": "정적 분석으로 확정 불가한 고위험 항목", "ref": d["id"]})
    for u in cg.unresolved:
        items.append({"item": f"{u['caller']}:L{u['line']} 간접 호출 '{u.get('expr')}'", "reason": "함수 포인터 대상 미확정",
                      "ref": u["caller"]})
    return items


def _graph(ia, cs, summary, curr):
    """시각화용 그래프: 변경 → 조건/함수 → 변수 → 함수 → 출력."""
    nodes, edges = {}, {}

    def node(nid, label, kind, level=None, **kw):
        n = nodes.setdefault(nid, {"id": nid, "label": label, "kind": kind, "level": level, "changes": set(), **kw})
        if level and (n["level"] is None or LEVELS.index(level) < LEVELS.index(n["level"])):
            n["level"] = level
        return nid

    def edge(a, b, kind, certain, change):
        if a == b:
            return
        k = (a, b, kind)
        e = edges.setdefault(k, {"from": a, "to": b, "kind": kind, "label": EDGE_LABEL.get(kind, kind),
                                 "certain": certain, "changes": set()})
        e["certain"] = e["certain"] or certain
        if change:
            e["changes"].add(change)
            nodes[a]["changes"].add(change)
            nodes[b]["changes"].add(change)

    def ent(f):
        st = curr.stmt_index(f.fid).get(f.node) if f.node not in (None, ENTRY) else None
        fn = curr.functions[f.fid]
        if st and st["kind"] in ("if", "switch", "while", "for", "do") and (f.seed or f.edge in ("MACRO", "GLOBAL_DEF")):
            return node(f"cond:{f.fid}:{f.node}", f"{fn['name']} L{st['line']}\n{st['text'][:40]}", "condition",
                        f.level, function=f.fid)
        return node(f"fn:{f.fid}", fn["name"], "function", f.level, function=f.fid, file=fn["file"])

    chmap = {c["id"]: c for c in cs.changes}
    for f in ia.facts:
        if f.fid not in curr.functions:
            continue
        me = ent(f)
        if f.parent is None:
            ch = chmap.get(f.change)
            if ch:
                cn = node(f"chg:{ch['id']}", f"{ch['id']} {ch['labels'][0]}", "change", "DIRECT")
                edge(cn, me, f.edge if f.edge != "SEED" else "SEED", f.certain, ch["id"])
                if me.startswith("cond:"):
                    edge(me, node(f"fn:{f.fid}", curr.functions[f.fid]["name"], "function", f.level,
                                  function=f.fid, file=curr.functions[f.fid]["file"]), "CONTROL", True, ch["id"])
            continue
        pe = ent(f.parent)
        if f.edge in ("GLOBAL", "ALIAS") and f.keys:
            vk = f.keys[0]
            vn = node(f"var:{vk}", key_name(vk), "variable", f.level)
            edge(pe, vn, "GLOBAL", f.certain, f.change)
            edge(vn, me, f.edge, f.certain, f.change)
        elif f.edge in ("DATA", "CONTROL") and pe == me:
            continue
        else:
            edge(pe, me, f.edge, f.certain, f.change)
    for s in ia.external_sinks:
        if s["function"] not in curr.functions:
            continue
        src = node(f"fn:{s['function']}", curr.functions[s["function"]]["name"], "function")
        on = node(f"out:{s['callee']}", f"{key_name(s['callee'])}()", "output", s["level"])
        edge(src, on, "OUTPUT", s["certain"], None)
    for fid, e in summary["functions"].items():
        if e.get("call_context_only"):
            me = node(f"fn:{fid}", e["name"], "function", e["level"], function=fid, call_context=True)
            for v in e.get("via_callee", []):
                if f"fn:{v}" in nodes:
                    edge(f"fn:{v}", me, "CALL", e["level"] != "POTENTIAL", None)
                    nodes[me]["changes"] |= nodes[f"fn:{v}"]["changes"]
    for n in nodes.values():
        n["changes"] = sorted(n["changes"])
    for e in edges.values():
        e["changes"] = sorted(e["changes"]) or sorted(set(nodes[e["from"]]["changes"]) & set(nodes[e["to"]]["changes"]))
    return {"nodes": list(nodes.values()), "edges": list(edges.values())}


def _summary(r):
    funcs = r["impact"]["functions"]
    cnt = {lv: sum(1 for f in funcs if f["level"] == lv) for lv in LEVELS[:3]}
    cnt["NO_IMPACT_CONFIRMED"] = len(r["impact"]["no_impact"])
    return {
        "changed_files": len(r["change_summary"]["files"]),
        "changes": len(r["changes"]),
        "semantic_changes": sum(1 for c in r["changes"] if c.get("semantic", True)),
        "non_semantic_changes": len(r["non_semantic"]),
        "functions_by_level": cnt,
        "defects": {"total": len(r["defects"]),
                    "confirmed": sum(1 for d in r["defects"] if d["status"] == "CONFIRMED_BY_STATIC_ANALYSIS"),
                    "high": sum(1 for d in r["defects"] if d["severity"] == "High")},
        "tests": {"total": len(r["tests"]), "high": sum(1 for t in r["tests"] if t["priority"] == "High")},
        "analysis_mode": r["analysis_mode"], "complete": r["completeness"]["complete"],
    }
