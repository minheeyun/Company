"""결과 저장: JSON / CSV / DOT / SVG / HTML."""

from __future__ import annotations

import csv
import json
import os

from .graph_render import render_with_dot, to_dot, to_svg
from .report_html import render_html


def _default(o):
    if isinstance(o, set):
        return sorted(o)
    if isinstance(o, tuple):
        return list(o)
    return str(o)


def write_all(result: dict, out_dir: str, formats=("json", "csv", "dot", "html")) -> dict[str, str]:
    os.makedirs(out_dir, exist_ok=True)
    files = {}
    if "json" in formats:
        p = os.path.join(out_dir, "impact_result.json")
        with open(p, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2, default=_default)
        files["json"] = p
    if "csv" in formats:
        files.update(write_csv(result, out_dir))
    dot = to_dot(result["graph"], "impact")
    if "dot" in formats:
        p = os.path.join(out_dir, "impact_graph.dot")
        with open(p, "w", encoding="utf-8") as f:
            f.write(dot)
        files["dot"] = p
        svg = render_with_dot(dot)
        if svg:
            p = os.path.join(out_dir, "impact_graph_graphviz.svg")
            with open(p, "w", encoding="utf-8") as f:
                f.write(svg)
            files["graphviz_svg"] = p
    svg = to_svg(result["graph"])
    p = os.path.join(out_dir, "impact_graph.svg")
    with open(p, "w", encoding="utf-8") as f:
        f.write(svg)
    files["svg"] = p
    if "html" in formats:
        p = os.path.join(out_dir, "impact_report.html")
        with open(p, "w", encoding="utf-8") as f:
            f.write(render_html(result, svg))
        files["html"] = p
    return files


def _w(path, header, rows):
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        for r in rows:
            w.writerow(r)
    return path


def write_csv(r: dict, out_dir: str) -> dict[str, str]:
    files = {}
    files["changes_csv"] = _w(os.path.join(out_dir, "changes.csv"),
                              ["ID", "분류", "변경 유형", "파일", "함수/심볼", "변경 전 라인", "변경 후 라인", "변경 전 코드",
                               "변경 후 코드", "설명"],
                              [[c["id"], c["category"], " | ".join(c["labels"]),
                                (c.get("curr") or c.get("base") or {}).get("file"),
                                c.get("function") or c.get("variable") or c.get("macro_name") or c.get("type_name") or "",
                                (c.get("base") or {}).get("line"), (c.get("curr") or {}).get("line"),
                                (c.get("base") or {}).get("code"), (c.get("curr") or {}).get("code"), c["description"]]
                               for c in r["changes"]])
    files["impact_table_csv"] = _w(os.path.join(out_dir, "impact_table.csv"),
                                   ["변경 ID", "변경 위치", "변경 유형", "영향 함수", "영향 파일", "영향 경로", "의존 종류",
                                    "영향 수준", "영향 근거", "검증 우선순위"],
                                   [[t["change"], t["location"], t["change_category"] + ": " + ", ".join(t["change_types"]),
                                     t["function_name"], t["file"], ", ".join(t["path_type"]), ", ".join(t["dependency"]),
                                     t["level"], t["evidence"], t["priority"]] for t in r["impact_table"]])
    files["functions_csv"] = _w(os.path.join(out_dir, "impacted_functions.csv"),
                                ["함수", "파일", "영향 수준", "우선순위", "전파 간선", "관련 변경", "역할", "근거(첫 경로)"],
                                [[f["name"], f["file"], f["level"], f["priority"], ", ".join(f["edge_labels"]),
                                  ", ".join(f["changes"]), ", ".join(f["roles"]),
                                  " → ".join(f"{p.get('name')}:L{p.get('line')}[{p['edge_label']}]"
                                             for p in (f["paths"][0] if f["paths"] else []))]
                                 for f in r["impact"]["functions"]] +
                                [[n["name"], n["file"], "NO_IMPACT_CONFIRMED", "-", "", "", "", n["reason"]]
                                 for n in r["impact"]["no_impact"]])
    files["defects_csv"] = _w(os.path.join(out_dir, "defects.csv"),
                              ["ID", "분류", "유형", "심각도", "판정", "함수", "파일", "라인", "설명", "근거", "관련 변경", "권고"],
                              [[d["id"], d["category"], d["type"], d["severity"], d["status"], d.get("function"),
                                d.get("file"), d.get("line"), d["description"], d["evidence"], d.get("change"),
                                d.get("recommendation")] for d in r["defects"]])
    files["tests_csv"] = _w(os.path.join(out_dir, "regression_tests.csv"),
                            ["ID", "우선순위", "종류", "목적", "대상 함수", "구동 경로", "입력", "사전 조건", "시나리오",
                             "예상 결과", "예상 결과 출처", "필요 이유", "관련 변경", "영향 경로", "관련 요구사항", "재사용 가능 기존 테스트"],
                            [[t["id"], t["priority"], t["kind"], t["purpose"], ", ".join(t["target_functions"]),
                              " | ".join(t["drive_from"]), " | ".join(t["inputs"]), " | ".join(t["preconditions"]),
                              " | ".join(t["scenario"]), t["expected"], t["expected_source"], t["reason"],
                              ", ".join(t["related_changes"]), " → ".join(t["impact_path"]),
                              ", ".join(x["id"] for x in t["requirements"]),
                              ", ".join(x["id"] for x in t["existing_tests"])] for t in r["tests"]])
    return files
