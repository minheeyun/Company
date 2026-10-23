"""보고서 출력: Markdown(검토용), JSON(도구 연계), DOT(Graphviz 시각화), 터미널 요약."""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

KIND_KO = {
    "changed": "수정", "added": "추가", "removed": "삭제", "func_added": "함수 추가", "func_removed": "함수 삭제",
    "global_changed": "전역 변경", "global_added": "전역 추가", "global_removed": "전역 삭제", "type_changed": "자료형 변경",
    "moved": "위치 이동",
}

STATUS_STATIC = "정적 분석상 영향 후보"
STATUS_CONFIRMED = "실행으로 차이 확인"
STATUS_SAME = "실행에서 차이 없음(시나리오 한정)"


def _cell(s) -> str:
    return str(s if s is not None else "").replace("|", "\\|").replace("\n", " ")


def _code(s: str) -> str:
    s = _cell(s)
    return f"`{s}`" if s else ""


def _sil_label(it: dict) -> str:
    if it["kind"] == "return":
        return it["func_display"] + "()"
    if it["kind"] == "extcall":
        return "call:" + it["name"].split("()")[0]
    return it["name"]


def merge_sil(result: dict, sil: dict) -> None:
    """SIL 결과를 영향 항목에 붙여 '정적 분석상 후보'와 '실행으로 확인'을 구분한다."""
    obs = sil.get("observables", {})
    matched = set()
    for it in result["items"]:
        lab = _sil_label(it)
        ob = obs.get(lab)
        if ob is None:
            it["status"] = STATUS_STATIC
            continue
        matched.add(lab)
        if ob["diff_cases"]:
            it["status"] = STATUS_CONFIRMED
            it["evidence"] = ob["example"]
        else:
            it["status"] = STATUS_SAME
    result["sil"] = {
        "cases": len(sil.get("cases", [])),
        "diff_cases": sil.get("diff_cases", 0),
        "unexpected": sorted(l for l, o in obs.items() if o["diff_cases"] and l not in matched),
        "observables": obs,
        "stubs": sil.get("stubs", {}),
    }


def write_outputs(result: dict, out_dir: Path, formats: set[str], sil_template: bool = True) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    if "json" in formats:
        p = out_dir / "impact.json"
        p.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        written.append(p)
    if "md" in formats:
        p = out_dir / "impact_report.md"
        p.write_text(render_md(result), encoding="utf-8")
        written.append(p)
    if "dot" in formats:
        p = out_dir / "impact.dot"
        p.write_text(render_dot(result), encoding="utf-8")
        written.append(p)
    if sil_template:
        p = out_dir / "sil_scenario.json"
        p.write_text(json.dumps(result["sil_template"], ensure_ascii=False, indent=2), encoding="utf-8")
        written.append(p)
    return written


def _arrow(edge) -> str:
    if not edge:
        return ""
    kind, label = edge
    if kind == "seed":
        return "변경 지점"
    if kind in ("data", "memory"):
        return f"[{label}]"
    if kind == "control":
        if label == "T":
            return "[조건 참]"
        if label == "F":
            return "[조건 거짓]"
        return f"[{label}]" if label else "[호출됨]"
    if kind == "param":
        return f"[인자 {label}]"
    if kind == "call":
        return "[호출]"
    if kind == "return":
        return "[반환]"
    return f"[{kind}]"


def path_line(path: list[dict]) -> str:
    parts = []
    for st in path:
        if st["kind"] in ("actual_out", "nop", "exit"):
            continue
        text = st["src"] or st["text"]
        parts.append(f"{st['func']}:{st['line']} `{_cell(text)}`")
    return " → ".join(parts)


def render_md(r: dict) -> str:
    out: list[str] = []
    st = r["stats"]
    b = r["build"]
    items = r["items"]
    has_sil = "sil" in r
    out += [
        "# C 변경 영향 분석 보고서",
        "",
        f"- 변경 전: `{r['old_root']}`",
        f"- 변경 후: `{r['new_root']}`",
        f"- 분석 파일: 변경 전 {len(r['files']['old'])}개, 변경 후 {len(r['files']['new'])}개 · 전처리기 `{b['cpp']}`",
    ]
    if b["defines"] or b["include_dirs"] or b["compile_commands_files"]:
        out.append(f"- 빌드 조건: -D {', '.join(b['defines']) or '없음'} / -I {', '.join(b['include_dirs']) or '없음'}"
                   f" / compile_commands 항목 {b['compile_commands_files']}개")
    out.append(f"- 호출 문맥 구분 길이: {r['options']['context_depth']} · 도구 {r['tool']}")
    out += ["", "## 요약", ""]
    confirmed = sum(1 for it in items if it.get("status") == STATUS_CONFIRMED)
    out.append(f"- 변경 지점 **{len(r['changes'])}개**, 영향 함수 **{st['affected_functions']}개**, "
               f"영향 변수·출력 **{len(items)}개**" + (f" (실행으로 차이 확인 {confirmed}개)" if has_sil else ""))
    if r["excluded"]["parse_errors"]:
        out.append(f"- ⚠ 파싱하지 못한 파일 {len(r['excluded']['parse_errors'])}개가 있습니다 (7절). 해당 파일의 영향은 빠져 있습니다.")
    out.append("- 결과는 **정적 분석상 영향 후보**입니다. 동작 차이가 의도된 변경인지 결함인지는 별도로 판단해야 합니다.")

    out += ["", "## 1. 변경 위치", "",
            "전처리 후 코드 기준으로 비교합니다. 헤더의 매크로·자료형 변경도 사용 위치의 변경으로 나타납니다.", "",
            "| # | 위치 | 함수 | 종류 | 원본 소스 | 변경 전 (전처리 후) | 변경 후 (전처리 후) | 비고 |", "|---|---|---|---|---|---|---|---|"]
    for i, c in enumerate(r["changes"], 1):
        out.append(f"| {i} | {c['file']}:{c['line']} | {_cell(c['func'])} | {KIND_KO.get(c['kind'], c['kind'])} | "
                   f"{_code(c['src'])} | {_code(c['old'])} | {_code(c['new'])} | {_cell(c['detail'])} |")
    if not r["changes"]:
        out.append("| - | - | - | - | - | - | - | 변경 없음 |")

    out += _items_section(items, "## 2. 영향받는 함수·변수", status=has_sil)
    out += _paths_section(items, "## 3. 영향 전달 경로", "변경 지점")
    out += _entries_section(r, "## 4. 진입점(태스크·ISR·콜백) 관점")

    out += ["", "## 5. 확인용 테스트 후보", ""]
    if r["tests"]:
        out += ["| 함수 | 변수 | 변경 전 참 구간 | 변경 후 참 구간 | 판정이 달라지는 구간 | 경계값 후보 | 입력 방법 |",
                "|---|---|---|---|---|---|---|"]
        for t in r["tests"]:
            diff = ", ".join(f"[{_inf(a, '-∞')}, {_inf(b, '+∞')}]" for a, b in t["diff"])
            out.append(f"| `{_cell(t['func'])}` | `{_cell(t['var'])}` | {_cell(t['old'])} | {_cell(t['new'])} | {diff} | "
                       f"{', '.join(map(str, t['values']))} | {t['drive']} |")
    else:
        out.append("단일 변수 비교식 변경이 없어 자동 경계값 후보는 없습니다. 2절의 조건과 3절 경로를 기준으로 입력을 정하세요.")
    out += ["", "- 2절의 변수·출력을 같은 입력·초기 상태·호출 순서에서 변경 전/후로 비교하는 테스트를 권합니다.",
            "- 카운터·타이머·래치가 영향 항목에 있으면 한 번 호출이 아니라 여러 주기, 임계값 도달, wraparound, 리셋 이후까지 확인하세요."]

    if has_sil:
        s = r["sil"]
        out += ["", "## 6. 실행 확인 (SIL 차등 실행)", "",
                f"- 케이스 {s['cases']}개 중 차이 발생 {s['diff_cases']}개 (상세: `sil/sil_report.md`)"]
        if s["unexpected"]:
            out.append(f"- ⚠ 정적 분석 영향 항목에 없는 관찰 대상에서 차이: {', '.join('`' + u + '`' for u in s['unexpected'])} "
                       "— 분석에서 제외된 구성(7절)이나 관찰식의 이름 차이를 확인하세요.")
        out.append("- 차이가 검출되지 않은 항목은 이 시나리오에 한해 같다는 뜻이며, 모든 실행에서 동일하다는 뜻이 아닙니다.")

    ex = r["excluded"]
    out += ["", f"## {7 if has_sil else 6}. 분석에서 제외되거나 가정한 구성", ""]
    if ex["parse_errors"]:
        out.append("**파싱 실패 (분석 제외)**")
        out.append("")
        for f, e in ex["parse_errors"].items():
            out.append(f"- `{f}`: {_cell(e.splitlines()[0] if e else '')}")
        out.append("")
    if ex["extern_functions"]:
        out.append("**본문 없는 외부 함수** — 포인터 인자가 가리키는 메모리를 읽고 쓰는 것으로 보수적으로 가정했습니다 "
                   "(`const` 포인터 인자는 읽기만). `--extern-model` 로 효과를 지정할 수 있습니다.")
        out.append("")
        out.append(", ".join(f"`{e['name']}`" + (" (모델)" if e["modeled"] else "") for e in ex["extern_functions"]))
        out.append("")
    if ex["unresolved_calls"]:
        out.append("**대상을 풀지 못한 함수 포인터 호출** — 반환값이 모든 인자에 의존한다고 보았고, 피호출 함수 내부 영향은 빠져 있습니다.")
        out.append("")
        for u in ex["unresolved_calls"]:
            out.append(f"- `{u['func']}`:{u['line']} `{u['call']}(...)`")
        out.append("")
    if ex["unresolved_pointers"]:
        out.append("**대상을 모르는 포인터** (호출자가 분석 범위 밖인 인자 등) — 별도의 추상 객체로 보았으므로, 실제로 전역을 가리키면 영향이 누락될 수 있습니다.")
        out.append("")
        out.append(", ".join(f"`{p}`" for p in ex["unresolved_pointers"]))
        out.append("")
    if ex["hw_access"]:
        out.append("**상수 주소(하드웨어 레지스터) 접근** — 읽기·쓰기 효과만 반영하며 하드웨어 동작은 모델링하지 않습니다.")
        out.append("")
        out.append(", ".join(f"`{h}`" for h in ex["hw_access"]))
        out.append("")
    if ex.get("no_effect_changes"):
        out.append("**동작 변화 없음으로 본 변경** — 바뀌었지만 시작점에서 뺀 자료형·초기값 변경입니다.")
        out.append("")
        out += [f"- {_cell(w)}" for w in ex["no_effect_changes"]]
        out.append("")
    if ex["warnings"]:
        out.append("**경고**")
        out.append("")
        out += [f"- {_cell(w)}" for w in ex["warnings"]]
        out.append("")
    out.append("**분석 가정**")
    out.append("")
    out += [f"- {a}" for a in ex["assumptions"]]
    out.append("")
    return "\n".join(out)


def _items_section(items: list[dict], title: str, status: bool = True) -> list[str]:
    out = ["", title, ""]
    head = "| 영향받는 함수 | 변수·출력 | 어떻게 영향을 받는가 | 차이가 발생하는 조건 | 위치 |"
    sep = "|---|---|---|---|---|"
    if status:
        head += " 상태 |"
        sep += "---|"
    out += [head, sep]
    for it in items:
        how = f"**{it['how_kind']}**: {_cell(it['how'])}"
        if it["kind"] == "var":
            how += f" (`{_cell(it['stmt'])}`)"
        cond = "<br>".join(_cell(c) for c in it["conditions"]) or "모든 실행"
        ver = "" if set(it["versions"]) == {"new", "old"} or not status else (
            " (변경 후만)" if it["versions"] == ["new"] else " (변경 전만)")
        row = (f"| `{_cell(it['func_display'])}` | `{_cell(it['name'])}`{ver} | {how} | {cond} | "
               f"{it['file']}:{it['line']} |")
        if status:
            st = it.get("status", STATUS_STATIC)
            if st == STATUS_CONFIRMED and it.get("evidence"):
                ev = it["evidence"]
                st += f": {ev['case']} #{ev['step']} {ev['old']}→{ev['new']}"
            row += f" {_cell(st)} |"
        out.append(row)
    if not items:
        out.append("| - | - | 영향받는 전역 상태·반환값·외부 호출 없음 | - | - |" + (" - |" if status else ""))
    return out


def _paths_section(items: list[dict], title: str, origin: str) -> list[str]:
    out = ["", title, "",
           f"{origin}에서 각 영향 항목까지 가장 짧은 의존 경로입니다. `[변수]`는 값(데이터) 의존, `[조건 참/거짓]`은 제어 의존입니다.", ""]
    for it in items:
        out.append(f"### `{_cell(it['name'])}` — {_cell(it['func_display'])} ({it['file']}:{it['line']})")
        out.append("")
        n = 0
        for stp in it["path"]:
            if stp["kind"] in ("actual_out", "nop", "exit"):
                continue
            n += 1
            text = stp["src"] or stp["text"]
            extra = f" ⟨전처리 후: `{_cell(stp['text'])}`⟩" if stp["src"] and stp["kind"] in ("stmt", "cond", "return") \
                and _norm_ws(stp["src"]) != _norm_ws(stp["text"]) and stp["edge"] and stp["edge"][0] == "seed" else ""
            arrow = _arrow(stp["edge"]).replace("변경 지점", origin)
            out.append(f"{n}. {arrow} `{_cell(stp['func'])}` {stp['file']}:{stp['line']} `{_cell(text)}`{extra}")
        out.append("")
    return out


def _entries_section(r: dict, title: str) -> list[str]:
    out = [title, "",
           "호출자가 없는 함수와 `--entry`로 지정한 함수를 진입점으로 봅니다. 분석용 main() 에서 호출하지 않는 함수도 빠짐없이 포함됩니다.", "",
           "| 진입점 | 구분 | 이 진입점에서 실행되는 영향 함수 |", "|---|---|---|"]
    for e in r["entries"]:
        kind = "ISR 추정" if e["isr"] else "진입점"
        if e["user"]:
            kind += " (지정)"
        out.append(f"| `{_cell(e['entry'])}` | {kind} | {', '.join('`' + _cell(f) + '`' for f in e['affected_functions'])} |")
    if r["shared"]:
        out += ["", "여러 진입점에서 접근하는 영향 변수 (호출 순서·선점에 따라 결과가 달라질 수 있으므로 시퀀스 테스트 권장):", "",
                "| 변수 | 접근 진입점 | ISR 포함 | 접근 함수 |", "|---|---|---|---|"]
        for s in r["shared"]:
            out.append(f"| `{_cell(s['var'])}` | {', '.join(s['entries'])} | {'예' if s['isr'] else '-'} | {', '.join(s['functions'])} |")
    return out


def render_slice_md(r: dict) -> str:
    o = r["options"]
    out = ["# 변경 가정 영향 분석 (전방 슬라이스)", "",
           f"- 대상: `{r['root']}` · 시작 위치: {', '.join(o['at']) or '-'}" + (f" · 변수: `{o['var']}`" if o["var"] else ""),
           f"- 영향 함수 **{len(r['affected_functions'])}개**, 영향 변수·출력 **{len(r['items'])}개** · 호출 문맥 구분 길이 {o['context_depth']}",
           "- 코드를 바꾸기 전, 이 위치를 바꾸면 확인해야 할 함수·변수 후보입니다 (정적 분석 결과).", ""]
    for w in r["warnings"]:
        out.append(f"- ⚠ {_cell(w)}")
    out += ["## 1. 시작 위치", "", "| 위치 | 함수 | 종류 | 원본 소스 | 전처리 후 |", "|---|---|---|---|---|"]
    for s in r["seeds"]:
        out.append(f"| {s['file']}:{s['line']} | `{_cell(s['func'])}` | {s['kind']} | {_code(s['src'])} | {_code(s['text'])} |")
    w = r.get("what_if")
    if w:
        out += ["", f"가정한 변경: `{_cell(w['current'])}` → `{_cell(w['assumed'])}`"]
        if w["detail"]:
            out.append(f"- {_cell(w['detail'])}")
        if w["interval"]:
            iv = w["interval"]
            out.append(f"- 현재 참: {_cell(iv['old'])} / 가정 후 참: {_cell(iv['new'])} / 경계값 후보: {', '.join(map(str, iv['tests']))}")
    out += _items_section(r["items"], "## 2. 확인해야 할 함수·변수", status=False)
    out += _paths_section(r["items"], "## 3. 영향 전달 경로", "시작 위치")
    out += _entries_section(r, "## 4. 진입점(태스크·ISR·콜백) 관점")
    out += ["", "## 5. 확인용 입력 후보", ""]
    if r["tests"]:
        out += ["| 함수 | 줄 | 변수 | 판정이 참인 구간 | 경계값 후보 |", "|---|---|---|---|---|"]
        for t in r["tests"]:
            out.append(f"| `{_cell(t['func'])}` | {t['line']} | `{_cell(t['var'])}` | {_cell(t['true_when'])} | "
                       f"{', '.join(map(str, t['values']))} |")
    else:
        out.append("시작 위치에 단일 변수 비교식이 없어 자동 경계값 후보가 없습니다.")
    out.append("")
    return "\n".join(out)

def _norm_ws(s: str) -> str:
    return "".join(s.split()).rstrip(";{").replace("(", "").replace(")", "")


def _inf(v, s):
    return s if v is None else v


def render_dot(r: dict) -> str:
    lines = ["digraph impact {", '  rankdir=LR; node [shape=box, fontsize=10, fontname="Helvetica"];',
             '  edge [fontsize=9, fontname="Helvetica"];']
    clusters = defaultdict(list)
    for g in r["graph"]:
        clusters[(g["version"], g["func"])].append(g)
    for i, ((ver, func), nodes) in enumerate(sorted(clusters.items())):
        lines.append(f'  subgraph cluster_{i} {{ label="{_dot(func)} ({"변경 후" if ver == "new" else "변경 전"})"; '
                     f'style=rounded; color="{"#4a7" if ver == "new" else "#888"}";')
        for g in nodes:
            style = ', style=filled, fillcolor="#fdd"' if g["seed"] else ""
            lines.append(f'    {g["id"]} [label="{_dot(g["label"])}"{style}];')
        lines.append("  }")
    for g in r["graph"]:
        if g["from"]:
            lines.append(f'  {g["from"]} -> {g["id"]} [label="{_dot(g["edge"])}"];')
    lines.append("}")
    return "\n".join(lines) + "\n"


def _dot(s: str) -> str:
    s = str(s)
    return (s[:60] + "…" if len(s) > 60 else s).replace("\\", "\\\\").replace('"', '\\"')


def terminal_summary(r: dict) -> str:
    out = [f"변경 지점 {len(r['changes'])}개 · 영향 함수 {r['stats']['affected_functions']}개 · 영향 변수·출력 {len(r['items'])}개", ""]
    if r["changes"]:
        out.append("[변경 위치]")
        for c in r["changes"]:
            body = f"{c['old']}  →  {c['new']}" if c["old"] and c["new"] else (c["new"] or c["old"])
            det = f"   ({c['detail']})" if c["detail"] else ""
            out.append(f"  {c['file']}:{c['line']} {c['func']} [{KIND_KO.get(c['kind'], c['kind'])}] {body}{det}")
        out.append("")
    if r["items"]:
        out.append("[영향받는 함수·변수]")
        w = max(len(it["func_display"]) for it in r["items"])
        w2 = max(len(it["name"]) for it in r["items"])
        for it in r["items"]:
            cond = "; ".join(it["conditions"]) or "모든 실행"
            st = f" <{it['status']}>" if it.get("status") else ""
            out.append(f"  {it['func_display']:<{w}}  {it['name']:<{w2}}  {it['how_kind']}: {it['how']}  | 조건: {cond}{st}")
    return "\n".join(out)
