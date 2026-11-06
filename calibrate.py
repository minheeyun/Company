#!/usr/bin/env python3
"""결과 대조(표본 검증) 집계와 보정안 작성.

report.html 의 '결과 대조' 탭에서 내보낸 cia_feedback_*.json (여러 개 가능: 검증자·버전별)을 모아
  1. 표본 대조 현황, 제목·설명 정확도, 영향 범위 정확도
  2. 우선순위 일치율과 어긋난 변경의 공통 특징 → 우선순위 규칙 보정안
  3. 자동 분류 규칙·검토 항목별 정확 / 오탐 / 판정 수준 다름 → 오탐이 많은 규칙의 제외(suppress) 후보
  4. 누락 기록 (보고서가 놓친 변경·영향·문제)
를 calibration_report.md 로 쓰고, 현재 cia_project.yaml 에 보정안을 더한 cia_project.calibrated.yaml 을 만든다.
보정안은 자동으로 적용하지 않는다 — 검토 후 변경 후 폴더의 상위 폴더에 cia_project.yaml 로 저장하면 다음 실행에 반영된다.

사용 예)
  python calibrate.py cia_feedback_26A22.json
  python calibrate.py 대조\\*.json --project-config "S:\\...\\LSS\\cia_project.yaml" -o "S:\\...\\LSS\\CIA_Result_26A22"
"""

from __future__ import annotations

import argparse
import copy
import glob
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

from c_impact import project as project_mod

PRIO = ["높음", "중간", "낮음"]
FP = "오탐"


def load_feedback(paths: list[str]) -> list[dict]:
    out = []
    for pat in paths:
        for p in sorted(glob.glob(pat)) or [pat]:
            d = json.loads(Path(p).read_text(encoding="utf-8"))
            if d.get("format") != "cia-feedback/1":
                raise ValueError(f"{p}: report.html '결과 대조' 탭에서 내보낸 파일이 아닙니다")
            d["_file"] = p
            out.append(d)
    return out


def _pct(a: int, b: int) -> str:
    return f"{a * 100 // b}%" if b else "-"


def analyze(fbs: list[dict], fp_threshold: float = 0.7, min_n: int = 2) -> dict:
    changes, missed = [], []
    for d in fbs:
        tag = f"{(d.get('versions') or {}).get('old', '?')}→{(d.get('versions') or {}).get('new', '?')}"
        for c in d.get("changes", []):
            changes.append({**c, "_run": tag, "_reviewer": d.get("reviewer", "")})
        for m in d.get("missed", []):
            missed.append({**m, "_run": tag})
    reviewed = [c for c in changes if any((c.get("feedback") or {}).get(k) for k in ("title", "priority", "impact"))]
    title = Counter((c["feedback"] or {}).get("title") for c in reviewed if (c["feedback"] or {}).get("title"))
    bad_titles = [c for c in reviewed if (c["feedback"] or {}).get("title") == "부정확"]
    impact = Counter((c["feedback"] or {}).get("impact") for c in reviewed if (c["feedback"] or {}).get("impact"))
    prio = defaultdict(Counter)
    up, down = [], []
    for c in reviewed:
        v = (c["feedback"] or {}).get("priority")
        if not v:
            continue
        prio[c["priority"]][v] += 1
        if v == "높여야 함":
            up.append(c)
        elif v == "낮춰야 함":
            down.append(c)
    # 규칙·검토 항목별 판정
    rule_stats: dict = defaultdict(Counter)
    item_stats: dict = defaultdict(Counter)
    examples: dict = defaultdict(list)
    for c in changes:
        for f in c.get("findings", []):
            v = (f.get("feedback") or {}).get("verdict")
            if not v:
                continue
            key = "오탐" if v.startswith(FP) else v
            for r in f.get("rules") or []:
                rule_stats[r][key] += 1
                examples[("rule", r)].append((c, f))
            for it in f.get("review_items") or []:
                item_stats[it][key] += 1
                examples[("item", it)].append((c, f))
            if not f.get("rules") and not f.get("review_items"):
                rule_stats["(분류 없음)"][key] += 1

    def fp_candidates(stats, kind):
        out = []
        for k, cn in stats.items():
            n = sum(cn.values())
            if n >= min_n and cn["오탐"] / n >= fp_threshold:
                out.append({"kind": kind, "key": k, "n": n, "fp": cn["오탐"], "rate": cn["오탐"] / n})
        return sorted(out, key=lambda x: (-x["rate"], -x["n"]))
    sup = fp_candidates(rule_stats, "risk_rules") + fp_candidates(item_stats, "review_items")
    overall = Counter()
    for c in changes:
        for f in c.get("findings", []):
            v = (f.get("feedback") or {}).get("verdict")
            if v:
                overall["오탐" if v.startswith(FP) else v] += 1
    return {"runs": len(fbs), "changes": changes, "reviewed": reviewed, "title": title, "bad_titles": bad_titles,
            "impact": impact, "prio": prio, "up": up, "down": down, "rule_stats": rule_stats, "item_stats": item_stats,
            "examples": examples, "suppress": sup, "missed": missed, "overall": overall}


def priority_suggestions(up: list[dict], down: list[dict], rules: list[dict], min_n: int = 2) -> tuple[list[dict], list[str]]:
    """어긋난 변경의 공통 특징으로 규칙을 고친다: (새 규칙 목록, 설명)."""
    rules = copy.deepcopy(rules)
    notes = []
    # 높여야 함: 공통 특징이 있으면 그 조건으로 '높음' 규칙을 맨 앞에 추가
    if len(up) >= min_n:
        cond = {}
        js = {c["judgment"] for c in up}
        if len(js) == 1:
            cond["judgment"] = sorted(js)
        if all(c.get("cross_function") for c in up):
            cond["cross_function"] = True
        if all(c.get("external_output") for c in up):
            cond["external_output"] = True
        if all(c.get("review_items") for c in up):
            cond["review_flags"] = True
        if cond:
            new = {"priority": "높음", **cond, "reason": "결과 대조 보정: " + ", ".join(f"{k}={v}" for k, v in cond.items())}
            rules.insert(0, new)
            notes.append(f"'높여야 함' {len(up)}건의 공통 특징({', '.join(cond)})으로 '높음' 규칙을 맨 앞에 추가")
        else:
            notes.append(f"'높여야 함' {len(up)}건에 공통 특징이 없어 규칙을 바꾸지 않음 — 아래 목록을 보고 files·funcs 조건으로 직접 추가하세요")
    # 낮춰야 함: 같은 근거(규칙)로 올라간 것이 여럿이면 그 규칙을 한 단계 낮춘다
    by_reason = Counter(c.get("priority_reason", "") for c in down)
    for reason, n in by_reason.items():
        if n < min_n or not reason:
            continue
        for r in rules:
            if r.get("reason") == reason and r.get("priority") in PRIO[:2]:
                old = r["priority"]
                r["priority"] = PRIO[PRIO.index(old) + 1]
                r["reason"] = reason + f" (결과 대조 보정: {old}→{r['priority']})"
                notes.append(f"근거 '{reason}' 로 정해진 우선순위가 '낮춰야 함' {n}건 → {old} 에서 {r['priority']} 로")
                break
    return rules, notes


def render(a: dict, prio_rules: list[dict], prio_notes: list[str], cfg_path: str, out_yaml: Path) -> str:
    L = ["# 결과 대조 집계와 보정안", "",
         f"- 대조 파일 {a['runs']}개 · 변경 {len(a['changes'])}건 중 대조한 변경 {len(a['reviewed'])}건 · 누락 기록 {len(a['missed'])}건",
         f"- 현재 프로젝트 설정: {cfg_path or '없음'}",
         f"- 보정안: `{out_yaml}` (자동 적용되지 않음 — 검토 후 변경 후 폴더의 상위 폴더에 `cia_project.yaml` 로 저장)", ""]
    t = a["title"]
    L += ["## 1. 제목·설명", "", f"정확 {t['정확']} · 부정확 {t['부정확']} (정확도 {_pct(t['정확'], t['정확'] + t['부정확'])})", ""]
    if a["bad_titles"]:
        by_type = Counter(c.get("type_label", "").split(" — ")[0] for c in a["bad_titles"])
        L += ["부정확한 제목이 많은 변경 유형: " + ", ".join(f"{k} {n}건" for k, n in by_type.most_common()), "",
              "| 실행 | ID | 함수 | 자동 제목 | 메모 |", "|---|---|---|---|---|"]
        for c in a["bad_titles"]:
            L.append(f"| {c['_run']} | {c['id']} | `{c['func']}` | {c['title']} | {(c['feedback'] or {}).get('note', '')} |")
        L += ["", "→ 호출 이름의 뜻이 틀리게 잡혔으면 `call_verbs` 에, 변수 역할이 틀리면 `review_patterns` 에 프로젝트 약어를 추가하세요.", ""]
    L += ["## 2. 우선순위", "", "| 보고서 우선순위 | 적절 | 높여야 함 | 낮춰야 함 |", "|---|---|---|---|"]
    tot = ok = 0
    for p in PRIO:
        cn = a["prio"].get(p, Counter())
        tot += sum(cn.values())
        ok += cn["적절"]
        L.append(f"| {p} | {cn['적절']} | {cn['높여야 함']} | {cn['낮춰야 함']} |")
    L += ["", f"일치율 {_pct(ok, tot)}", ""]
    for title, lst in (("높여야 함", a["up"]), ("낮춰야 함", a["down"])):
        if lst:
            L += [f"**{title} {len(lst)}건**", "", "| ID | 함수 | 우선순위 (근거) | 판정 | 비교 | 다른 함수 영향 | 외부 출력 | 메모 |", "|---|---|---|---|---|---|---|---|"]
            for c in lst:
                L.append(f"| {c['id']} | `{c['func']}` | {c['priority']} ({c.get('priority_reason', '')}) | {c['judgment']} | {c['comparison']} | "
                         f"{c.get('cross_function', 0)} | {'있음' if c.get('external_output') else '-'} | {(c['feedback'] or {}).get('note', '')} |")
            L.append("")
    L += ["보정안:", ""] + ([f"- {n}" for n in prio_notes] or ["- 바꿀 근거가 부족함 (같은 유형의 어긋남이 2건 이상일 때 제안)"]) + [""]
    L += ["## 3. 발견 사항 — 규칙별 정확도", "", "| 구분 | 규칙·항목 | 정확 | 오탐 | 판정 수준 다름 | 중복 | 오탐 비율 |", "|---|---|---|---|---|---|---|"]
    for kind, stats in (("자동 분류", a["rule_stats"]), ("검토 항목", a["item_stats"])):
        for k, cn in sorted(stats.items(), key=lambda kv: -sum(kv[1].values())):
            n = sum(cn.values())
            L.append(f"| {kind} | {k} | {cn['정확']} | {cn['오탐']} | {cn['판정 수준 다름']} | {cn['중복']} | {_pct(cn['오탐'], n)} |")
    allc = a["overall"]
    L += ["", f"전체 발견 {sum(allc.values())}건: 정확 {allc['정확']} · 오탐 {allc['오탐']} · 판정 수준 다름 {allc['판정 수준 다름']} · "
              f"중복 {allc['중복']} (정밀도 {_pct(allc['정확'], allc['정확'] + allc['오탐'])})", ""]
    if a["suppress"]:
        L += ["제외(suppress) 후보 — 오탐 비율이 높은 규칙. 같은 규칙이 실제 문제를 잡은 적이 없는지 확인한 뒤 적용하세요:", ""]
        for s in a["suppress"]:
            L.append(f"- `{s['kind']}`: **{s['key']}** — {s['n']}건 중 오탐 {s['fp']}건 ({s['rate']:.0%})")
            for c, f in a["examples"][("rule" if s["kind"] == "risk_rules" else "item", s["key"])][:3]:
                L.append(f"  - {c['_run']} {f['id']} `{c['func']}`: {f['title']} — {(f.get('feedback') or {}).get('note', '')}")
        L.append("")
    else:
        L += ["제외 후보 없음 (같은 규칙의 대조 결과가 2건 이상이고 오탐 비율이 기준 이상일 때 제안)", ""]
    im = a["impact"]
    L += ["## 4. 영향 범위", "", f"정확 {im['정확']} · 과다 {im['과다 (불필요한 경로)']} · 누락 있음 {im['누락 있음']}", ""]
    notes = [c for c in a["reviewed"] if (c["feedback"] or {}).get("impact") in ("과다 (불필요한 경로)", "누락 있음")]
    for c in notes:
        L.append(f"- {c['_run']} {c['id']} `{c['func']}`: {(c['feedback'] or {}).get('impact')} — {(c['feedback'] or {}).get('note', '')}")
    L += ["", "## 5. 누락 기록 (보고서가 놓친 것)", ""]
    if a["missed"]:
        by = Counter(m["kind"] for m in a["missed"])
        L.append(", ".join(f"{k} {n}건" for k, n in by.most_common()))
        L += ["", "| 실행 | 종류 | 관련 변경 | 위치 | 내용 |", "|---|---|---|---|---|"]
        for m in a["missed"]:
            L.append(f"| {m['_run']} | {m['kind']} | {m.get('change', '')} | {m.get('where', '')} | {m['desc']} |")
        L += ["", "→ 영향 누락이 분석 범위 밖(다른 C 파일)이면 `--scope all` 로, 파싱 실패 파일 때문이면 run_log.txt 의 파싱 실패 원인을 먼저 해결하세요.",
              "  그 밖의 누락은 이 표를 개발 쪽에 전달해 분석 규칙을 추가합니다."]
    else:
        L.append("기록된 누락 없음")
    L += ["", "## 6. 보정안 요약 (cia_project.calibrated.yaml)", "",
          f"- 우선순위 규칙 {len(prio_rules)}개" + (f" (변경: {len(prio_notes)}건)" if prio_notes else " (변경 없음)"),
          f"- 제외 후보 {len(a['suppress'])}건 (suppress 에 추가)", ""]
    return "\n".join(L)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="결과 대조 집계와 보정안 (cia_project.yaml)")
    ap.add_argument("feedback", nargs="+", help="report.html 에서 내보낸 cia_feedback_*.json (와일드카드 가능)")
    ap.add_argument("--project-config", help="현재 cia_project.yaml (기본: 대조한 변경 후 폴더의 상위 폴더에서 찾음)")
    ap.add_argument("-o", "--out", help="결과 폴더 (기본: 첫 대조 파일이 있는 폴더)")
    ap.add_argument("--fp-threshold", type=float, default=0.7, help="제외 후보로 볼 오탐 비율 (기본 0.7)")
    ap.add_argument("--min", type=int, default=2, help="보정안을 낼 최소 건수 (기본 2)")
    args = ap.parse_args(argv)
    try:
        fbs = load_feedback(args.feedback)
    except (OSError, ValueError) as e:
        print(f"오류: {e}", file=sys.stderr)
        return 1
    if not fbs:
        print("오류: 대조 파일이 없습니다", file=sys.stderr)
        return 1
    out = Path(args.out) if args.out else Path(fbs[0]["_file"]).resolve().parent
    out.mkdir(parents=True, exist_ok=True)
    cfg_path = args.project_config
    if not cfg_path and fbs[0].get("project_config"):
        cfg_path = fbs[0]["project_config"]
    if not cfg_path and fbs[0].get("new_dir"):
        p = project_mod.find(Path(fbs[0]["new_dir"]), None) if Path(fbs[0]["new_dir"]).exists() else None
        cfg_path = str(p) if p else ""
    try:
        cur = project_mod.load(Path(cfg_path)) if cfg_path and Path(cfg_path).is_file() else {}
    except ValueError as e:
        print(f"오류: {e}", file=sys.stderr)
        return 1
    a = analyze(fbs, args.fp_threshold, args.min)
    rules, notes = priority_suggestions(a["up"], a["down"], cur.get("priority_rules") or project_mod.DEFAULT_PRIORITY_RULES, args.min)
    new = {k: v for k, v in cur.items() if not k.startswith("_")}
    new["priority_rules"] = rules
    sup = dict(new.get("suppress") or {})
    for s in a["suppress"]:
        lst = list(sup.get(s["kind"]) or [])
        if s["key"] not in lst:
            lst.append(s["key"])
        sup[s["kind"]] = lst
    new["suppress"] = {"review_items": sup.get("review_items", []), "risk_rules": sup.get("risk_rules", [])}
    new.setdefault("tests", dict(project_mod.DEFAULT_TESTS))
    yml = out / "cia_project.calibrated.yaml"
    head = ["# calibrate.py 가 만든 보정안 — 검토 후 변경 후 폴더의 상위 폴더에 cia_project.yaml 로 저장하세요.",
            f"# 근거: {', '.join(Path(d['_file']).name for d in fbs)} (calibration_report.md 참고)", ""]
    yml.write_text("\n".join(head) + project_mod.to_yaml(new), encoding="utf-8")
    md = out / "calibration_report.md"
    md.write_text(render(a, rules, notes, cfg_path or "", yml), encoding="utf-8")
    t, cn = a["title"], a["overall"]
    print(f"대조한 변경 {len(a['reviewed'])}건 · 제목 정확 {t['정확']}/{t['정확'] + t['부정확']} · 발견 정확 {cn['정확']} · 오탐 {cn['오탐']} · "
          f"누락 {len(a['missed'])}건 · 제외 후보 {len(a['suppress'])}건 · 우선순위 보정 {len(notes)}건")
    print(f"집계: {md}")
    print(f"보정안: {yml}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
