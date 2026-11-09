#!/usr/bin/env python3
"""평가: 정답셋 대비 정밀도·재현율, 검토자가 봐야 할 후보 수, 분석 시간, 분석 범위.

  python evaluate.py --config examples/torque_ecu/analysis.yaml [--impact out/impact.json]

정답셋(ground_truth.yaml)은 과거 변경 이력·결함·리뷰 기록에서 '실제로 영향받은 함수와 기능'을
사람이 라벨링한 것이다(예제의 정답셋은 설명용 수동 라벨). 비교 대상(baseline):
  precise          문맥 민감 chop(정적 영향 후보)
  precise+behavior 정적 후보 중 동작 검증에서 '차이 없음'으로 판정된 기능을 뺀 것
  naive_chop       전방∩후방 단순 교집합(함수 단위 비교)
  mvp              2.5단계 함수 단위 간이 분석(호출 그래프+전역 읽기/쓰기+함수 요약)
  callgraph        호출 그래프만
  cochange         공변경 규칙만(변경 이력이 있을 때)
  goto-diff        CBMC goto-diff --forward-impact (설치되어 있을 때)
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from typing import Dict, List, Optional, Set

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from vfia.config import load_config, load_structured  # noqa: E402
from vfia.model import NK, Status  # noqa: E402

NEG = {Status.MODEL_NO_DIFF, Status.EXEC_NO_DIFF}
POS_BEHAVIOR = {Status.MODEL_DIFF, Status.EXEC_DIFF}


def _prf(tp: int, fp: int, fn: int) -> dict:
    p = tp / (tp + fp) if tp + fp else (1.0 if not fn else 0.0)
    r = tp / (tp + fn) if tp + fn else 1.0
    f = 2 * p * r / (p + r) if p + r else 0.0
    return {"precision": round(p, 3), "recall": round(r, 3), "f1": round(f, 3), "tp": tp, "fp": fp, "fn": fn}


def _match(case: dict, changes: List[dict]) -> List[dict]:
    ms = case.get("match")
    ms = ms if isinstance(ms, list) else [ms]
    out = []
    for m in ms:
        for c in changes:
            if m.get("id") and c["id"] == m["id"]:
                out.append(c)
            elif m.get("unit") and c.get("unit_name") == m["unit"] and (not m.get("type") or c["type"] == m["type"]):
                out.append(c)
    return out


def predictions(data: dict, matched: List[dict]) -> Dict[str, dict]:
    ids = {c["id"] for c in matched}
    pred: Dict[str, dict] = {k: {"features": set(), "functions": set()} for k in
                             ("precise", "precise+behavior", "naive_chop", "mvp", "callgraph")}
    for c in matched:
        for f in c["impact"]["features"]:
            if f["status"] == Status.NO_PATH:
                continue
            fn = set()
            nfn = set()
            for sd in f["sides"].values():
                fn |= set(sd.get("functions") or [])
                nfn |= set(sd.get("naive_functions") or sd.get("functions") or [])
            pred["precise"]["features"].add(f["feature"])
            pred["precise"]["functions"] |= fn
            pred["naive_chop"]["features"].add(f["feature"])
            pred["naive_chop"]["functions"] |= nfn
            if f["status"] not in NEG:
                pred["precise+behavior"]["features"].add(f["feature"])
                pred["precise+behavior"]["functions"] |= fn
    for m in data.get("mvp") or []:
        if m["change"] in ids:
            pred["mvp"]["features"] |= set(m["features"])
            pred["mvp"]["functions"] |= set(m["functions"])
            pred["callgraph"]["features"] |= set(m["callgraph_features"])
            pred["callgraph"]["functions"] |= set(m["callgraph_functions"])
    return pred


def behavior_predictions(matched: List[dict]) -> Dict[str, str]:
    out = {}
    for c in matched:
        for f in c["impact"]["features"]:
            if f["status"] in POS_BEHAVIOR:
                out[f["feature"]] = "diff"
            elif f["status"] in NEG and out.get(f["feature"]) != "diff":
                out[f["feature"]] = "no_diff"
    return out


def cochange_predictions(data: dict, matched: List[dict], g) -> Optional[Dict[str, Set[str]]]:
    cc = data.get("cochange")
    if not cc or g is None:
        return None
    files = set()
    for c in matched:
        for r in (cc.get("per_change") or {}).get(c["id"], []):
            files.add(r["entity"])
        for side in ("old", "new"):
            if (c.get(side) or {}).get("file"):
                files.add(c[side]["file"])
    funcs = {g.node(f).get("name") for f in g.nodes_of(NK.FUNCTION) if g.node(f).get("file") in files}
    from vfia.mvp_impact import feature_owners
    feats = {fid for fid, own in feature_owners(g).items() if {g.node(o).get("file") for o in own} & files}
    return {"features": feats, "functions": funcs}


def goto_diff_available() -> bool:
    return bool(shutil.which("goto-diff") and shutil.which("goto-cc"))


def run_goto_diff(cfg, before: str, after: str, workdir: str) -> Optional[Dict[str, Set[int]]]:
    """CBMC goto-diff --forward-impact 결과의 (파일 -> 영향 줄) 집합. 미설치면 None."""
    if not goto_diff_available():
        return None
    os.makedirs(workdir, exist_ok=True)
    gbs = []
    for name in (before, after):
        v = cfg.version(name)
        srcs = []
        for root, _, files in os.walk(v.root):
            srcs += [os.path.join(root, f) for f in files if f.endswith(".c")]
        out = os.path.join(workdir, f"{name}.gb")
        incs = [x for a in v.args for x in [a]] or [f"-I{os.path.join(v.root, 'include')}"]
        p = subprocess.run(["goto-cc", *incs, *srcs, "-o", out], capture_output=True, text=True)
        if p.returncode != 0:
            return None
        gbs.append(out)
    p = subprocess.run(["goto-diff", "--forward-impact", *gbs], capture_output=True, text=True, timeout=600)
    lines: Dict[str, Set[int]] = {}
    for ln in p.stdout.splitlines():
        if " line " in ln and " file " in ln:
            try:
                f = ln.split(" file ", 1)[1].split()[0]
                n = int(ln.split(" line ", 1)[1].split()[0])
                lines.setdefault(os.path.basename(f), set()).add(n)
            except (IndexError, ValueError):
                continue
    return lines


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="변경 영향 분석 평가(정답셋 대비)")
    ap.add_argument("--config", required=True)
    ap.add_argument("--impact", default=None, help="analyze_impact.py 결과 JSON(없으면 실행)")
    ap.add_argument("--ground-truth", default=None)
    ap.add_argument("--before", default="before")
    ap.add_argument("--after", default="after")
    ap.add_argument("--out", default=None)
    a = ap.parse_args(argv)
    cfg = load_config(a.config)
    out = a.out or cfg.output_dir
    os.makedirs(out, exist_ok=True)
    gt_path = a.ground_truth or cfg.ground_truth
    if not gt_path or not os.path.isfile(gt_path):
        print("정답셋(ground_truth)이 없습니다. 설정의 ground_truth 또는 --ground-truth를 지정하세요.")
        return 1
    gt = load_structured(gt_path)
    t0 = time.time()
    impact_path = a.impact or os.path.join(out, "impact.json")
    if not a.impact:
        import analyze_impact
        analyze_impact.main(["--config", a.config, "--before", a.before, "--after", a.after, "--out", out, "--quiet"])
    full_time = round(time.time() - t0, 2)
    data = json.load(open(impact_path, encoding="utf-8"))
    g = None
    try:
        from vfia.pipeline import get_graph
        g = get_graph(cfg, a.after, log=lambda *x: None)
    except Exception:
        pass
    methods = ["precise", "precise+behavior", "naive_chop", "mvp", "callgraph"]
    if data.get("cochange"):
        methods.append("cochange")
    acc = {m: {"f": [0, 0, 0], "fn": [0, 0, 0], "cand_f": 0, "cand_fn": 0} for m in methods}
    beh = [0, 0, 0, 0]   # 동작 판정: 맞은 차이, 틀린 차이, 맞은 무차이, 틀린 무차이
    rows = []
    for case in gt.get("cases", []):
        matched = _match(case, data["changes"])
        tf = set(case.get("impacted_features", []))
        tfn = set(case.get("impacted_functions", []))
        pred = predictions(data, matched)
        cc = cochange_predictions(data, matched, g) if "cochange" in methods else None
        if cc is not None:
            pred["cochange"] = cc
        row = {"case": case["id"], "matched": [c["id"] for c in matched], "truth_features": sorted(tf), "methods": {}}
        for m in methods:
            pf, pfn = pred.get(m, {"features": set(), "functions": set()})["features"], \
                pred.get(m, {"features": set(), "functions": set()})["functions"]
            a_ = acc[m]
            a_["f"][0] += len(pf & tf)
            a_["f"][1] += len(pf - tf)
            a_["f"][2] += len(tf - pf)
            if "impacted_functions" in case:
                a_["fn"][0] += len(pfn & tfn)
                a_["fn"][1] += len(pfn - tfn)
                a_["fn"][2] += len(tfn - pfn)
            a_["cand_f"] += len(pf)
            a_["cand_fn"] += len(pfn)
            row["methods"][m] = {"features": sorted(pf), "functions": len(pfn)}
        bp = behavior_predictions(matched)
        truth_b = set(case.get("behavior_changes", []))
        for f, v in bp.items():
            if v == "diff":
                beh[0 if f in truth_b else 1] += 1
            else:
                beh[2 if f not in truth_b else 3] += 1
        row["behavior"] = bp
        rows.append(row)
    n = max(1, len(rows))
    summary = {}
    for m in methods:
        summary[m] = {"feature": _prf(*acc[m]["f"]), "function": _prf(*acc[m]["fn"]),
                      "avg_feature_candidates": round(acc[m]["cand_f"] / n, 2),
                      "avg_function_candidates": round(acc[m]["cand_fn"] / n, 2)}
    gd = None
    whole = None
    if goto_diff_available():
        gd = run_goto_diff(cfg, a.before, a.after, os.path.join(out, "goto_diff"))
    if gd is not None and g is not None:
        # goto-diff는 전체 diff 단위 결과라 변경별로 나눌 수 없음 -> 전체 변경 기준 함수 집합으로 비교
        gd_funcs = set()
        for f in g.nodes_of(NK.FUNCTION):
            d = g.node(f)
            fl = os.path.basename(d.get("file") or "")
            if fl in gd and any((d.get("line") or 0) <= ln <= (d.get("end_line") or 0) for ln in gd[fl]):
                gd_funcs.add(d.get("name"))
        truth_all = set().union(*[set(c.get("impacted_functions", [])) for c in gt.get("cases", [])])
        ours = set().union(*[predictions(data, _match(c, data["changes"]))["precise"]["functions"]
                             for c in gt.get("cases", [])])
        whole = {"cbmc_goto_diff": _prf(len(gd_funcs & truth_all), len(gd_funcs - truth_all), len(truth_all - gd_funcs)),
                 "precise": _prf(len(ours & truth_all), len(ours - truth_all), len(truth_all - ours)),
                 "goto_diff_functions": sorted(gd_funcs)}
    cov = {}
    for side, ph in (data.get("parse_health") or {}).items():
        if ph:
            cov[side] = {"parse_success_rate": ph.get("success_rate"),
                         "missing_functions": len(ph.get("missing_function_candidates") or []),
                         "partial_ast_functions": len(ph.get("partial_ast_functions") or [])}
    if g is not None:
        ind = [c for c in g.nodes_of(NK.CALL_SITE) if g.node(c).get("indirect")]
        unres = [c for c in ind if not g.node(c).get("callees")]
        cov["indirect_calls"] = len(ind)
        cov["unresolved_indirect_ratio"] = round(len(unres) / len(ind), 3) if ind else 0.0
    cov["unmodeled_externals"] = (data.get("external_models") or {}).get("unmodeled", [])
    res = {"ground_truth": gt_path, "cases": rows, "summary": summary,
           "behavior_judgement": {"diff_correct": beh[0], "diff_wrong": beh[1], "no_diff_correct": beh[2],
                                  "no_diff_wrong": beh[3]},
           "analysis_time_s": (data.get("meta") or {}).get("seconds") or full_time,
           "coverage": cov, "whole_diff_function_level": whole,
           "external_tools": {"cbmc_goto_diff": "미설치" if gd is None else {k: sorted(v) for k, v in gd.items()},
                              "frama_c_impact": "미설치" if not shutil.which("frama-c") else
                              "설치됨: frama-c -impact-pragma <함수> -impact-print 로 수동 비교"},
           "note": gt.get("note")}
    jp = os.path.join(out, "evaluation.json")
    with open(jp, "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, indent=2)
    md = ["# 변경 영향 분석 평가", "", f"정답셋: `{os.path.basename(gt_path)}` ({len(rows)}건). {gt.get('note') or ''}", "",
          "| 방법 | 기능 P | 기능 R | 기능 F1 | 함수 P | 함수 R | 변경당 기능 후보 | 변경당 함수 후보 |",
          "|---|---|---|---|---|---|---|---|"]
    for m, v in summary.items():
        md.append(f"| {m} | {v['feature']['precision']} | {v['feature']['recall']} | {v['feature']['f1']} | "
                  f"{v['function']['precision']} | {v['function']['recall']} | {v['avg_feature_candidates']} | "
                  f"{v['avg_function_candidates']} |")
    b = res["behavior_judgement"]
    md += ["", f"동작 판정: 차이 확인 {b['diff_correct']}건 맞음/{b['diff_wrong']}건 틀림, "
               f"차이 없음 {b['no_diff_correct']}건 맞음/{b['no_diff_wrong']}건 틀림", "",
           f"분석 시간: {res['analysis_time_s']}s, 분석 범위: {json.dumps(cov, ensure_ascii=False)}", "",
           f"외부 도구: {json.dumps(res['external_tools'], ensure_ascii=False)}"]
    if whole:
        md += ["", "전체 변경 기준 함수 단위 비교(goto-diff는 변경별 분리 불가):",
               f"- CBMC goto-diff --forward-impact: P {whole['cbmc_goto_diff']['precision']}, R {whole['cbmc_goto_diff']['recall']}",
               f"- precise(문맥 민감 chop): P {whole['precise']['precision']}, R {whole['precise']['recall']}"]
    with open(os.path.join(out, "evaluation.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(md) + "\n")
    print("\n".join(md))
    return 0


if __name__ == "__main__":
    sys.exit(main())
