"""변경점 묶음(검토 단위): AST 변경 노드를 '함수 + 논리적으로 연결된 변경 구문'으로 묶고, 검증자의 말로 요약한다.

한 묶음에는 다음이 붙는다.
  - 변경 전후 코드와 한두 문장의 자동 분석 요약 (무엇이 바뀌었는가)
  - 스크립트가 확인한 사실 표 (분석 항목 · 분석 결과 · 상태)
  - 발견된 문제 카드: 보안 분석 결과와 검토 항목 결과를 같은 위치·같은 문제끼리 하나로 합친 것.
    코드 변경 사실과 보안 판단을 나눠 적고, 검증 수준(반례·정적 규칙·Taint·미확정)으로 등급을 정한다.
  - 검증자 체크리스트: 이 묶음과 관련된 검토 항목만, 중복 없이
  - 의존성 변화: 이 묶음에서 바뀐 호출·변수 읽기쓰기

함수 단위로는 영향(직접 변경 → 직접 영향 → 간접 영향)과 호출 변경 대응표를 만든다.
묶는 기준: 같은 함수에서 줄 간격이 가까운 변경(6줄 이내)과, 묶음 안의 바뀐 조건이 제어하는 변경.
"""

from __future__ import annotations

import difflib
import re
from collections import defaultdict
from pathlib import Path

from pycparser import c_ast

from .cexpr import const_eval, gen
from .program import Program, display_path
from .review import DEFAULT_PATTERNS, expr_diff, _expr, _iter

GAP = 6
LEVELS = ["red", "orange", "yellow", "gray"]
LEVEL_KO = {"red": "위반 경로 확인", "orange": "신규 위험 후보", "yellow": "검증자 확인 필요", "gray": "분석 불충분"}


def _rel(prog: Program, path: str) -> str:
    try:
        return Path(path).resolve().relative_to(prog.root.resolve()).as_posix()
    except (ValueError, OSError):
        return path or ""


def _short(s, n=90):
    s = " ".join(str(s or "").split())
    return s if len(s) <= n else s[: n - 1] + "…"


def _calls(prog: Program, n) -> list[str]:
    if n.kind in ("call", "extcall") and n.site is not None:
        return [prog.sites[n.site].name]
    if n.kind == "actual_in":
        return []
    out = []
    for x in _iter(_expr(n)):
        if isinstance(x, c_ast.FuncCall):
            sid = prog.site_of.get(id(x))
            out.append(prog.sites[sid].name if sid is not None else gen(x.name))
    return out


def _cond_ancestors(prog: Program, nid: int, depth: int = 4) -> set[int]:
    out, cur = set(), {nid}
    for _ in range(depth):
        nxt = set()
        for c in cur:
            for p, _lab in prog.cd_parents.get(c, ()):
                if prog.nodes[p].kind == "cond" and p not in out:
                    out.add(p)
                    nxt.add(p)
        cur = nxt
    return out


class _Ch:
    """검토용 변경 하나 (변경 전후 짝을 하나로)."""

    def __init__(self, c, prog, other_prog):
        self.c, self.prog, self.other = c, prog, other_prog
        self.n = prog.nodes[c.node]
        self.o = other_prog.nodes[c.other_node] if c.other_node is not None else None
        self.kind = c.kind
        self.line = c.line
        self.version = c.version


def _phrase(ch: _Ch, P) -> tuple[str, str]:
    """(분석 항목, 사람이 읽는 한 줄 설명)."""
    c, n, o, prog = ch.c, ch.n, ch.o, ch.prog
    k = c.kind
    if k == "func_added":
        return "함수", f"{prog.func_display(c.func)}() 함수 추가"
    if k == "func_removed":
        return "함수", f"{prog.func_display(c.func)}() 함수 삭제"
    if k in ("global_changed", "global_added", "global_removed"):
        return "초기값", {"global_changed": f"초기값 변경: {_short(c.other_text, 50)} → {_short(c.text, 50)}",
                       "global_added": f"전역·정적 변수 추가: {_short(c.text, 60)}",
                       "global_removed": f"전역·정적 변수 삭제: {_short(c.text, 60)}"}[k]
    if k == "type_changed":
        return "자료형", f"{c.text} 변경 — {_short(c.detail, 80)}"
    calls = _calls(prog, n)
    if k == "moved":
        same = o is not None and o.depth == n.depth
        return ("실행 순서" if same else "실행 조건"), f"`{_short(n.text, 60)}` {'실행 순서 이동' if same else '다른 조건 아래로 이동'}"
    if n.kind == "cond":
        loop = n.sig.startswith(("while|", "for|", "dowhile|"))
        what = "반복 조건" if loop else ("분기 조건(switch)" if n.sig.startswith("switch|") else "조건")
        callnote = f" (반환값 검사: {', '.join(calls)}())" if calls else ""
        if k == "removed":
            return "조건문", f"{what} `{_short(n.text, 70)}` 삭제{callnote}"
        if k == "added":
            return "조건문", f"{what} `{_short(n.text, 70)}` 추가{callnote}"
        d = expr_diff(_expr(o), _expr(n)) if o is not None else []
        tail = f" — {c.detail}" if c.detail and c.detail not in ("추가된 문장", "삭제된 문장") else ""
        return "조건문", f"{what} 변경 `{_short(o.text if o else c.other_text, 50)}` → `{_short(n.text, 50)}`" + (
            f" ({'; '.join(d[:2])})" if d else "") + tail
    if n.kind == "return":
        return "조기 종료", f"`{_short(n.text, 60)}` {'추가' if k == 'added' else '삭제' if k == 'removed' else '변경'}"
    e = _expr(n)
    if isinstance(e, c_ast.Assignment) and k == "changed" and o is not None and isinstance(_expr(o), c_ast.Assignment) \
            and gen(_expr(o).lvalue) == gen(e.lvalue):
        lv = gen(e.lvalue)
        d = expr_diff(_expr(o).rvalue, e.rvalue)
        if not calls or all("호출" not in x for x in d):
            cat = "상태값" if const_eval(e.rvalue) is not None or re.match(r"^[A-Z_][A-Z0-9_]+$", gen(e.rvalue)) else "계산식"
            return cat, f"`{lv}` 대입값 {_short(gen(_expr(o).rvalue), 40)} → {_short(gen(e.rvalue), 40)}"
    if calls and n.kind in ("stmt", "extcall", "call", "actual_in"):
        if k == "removed":
            return "함수 호출", f"{', '.join(dict.fromkeys(calls))}() 호출 삭제"
        if k == "added":
            return "함수 호출", f"{', '.join(dict.fromkeys(calls))}() 호출 추가"
        if o is not None:
            oc = _calls(ch.other, o)
            d = expr_diff(_expr(o), e)
            rep = [x for x in d if x.startswith("호출 대상")]
            if rep:
                return "함수 호출", rep[0].replace("호출 대상 ", "호출 대체: ")
            return "함수 호출", f"{', '.join(dict.fromkeys(oc))}() 호출 인자 변경 ({'; '.join(d[:2])})" if d else \
                f"`{_short(n.text, 70)}` 변경"
    if isinstance(e, c_ast.Assignment) or isinstance(e, c_ast.UnaryOp) or isinstance(n.ast, c_ast.Decl):
        target = gen(e.lvalue) if isinstance(e, c_ast.Assignment) else (gen(e.expr) if isinstance(e, c_ast.UnaryOp) else
                                                                        getattr(n.ast, "name", ""))
        cat = "카운터" if P["counter"].search(target or "") else "타이머" if P["timer"].search(target or "") else "대입"
        if k == "added":
            return cat, f"`{_short(n.text, 70)}` 추가"
        if k == "removed":
            return cat, f"`{_short(n.text, 70)}` 삭제"
        d = expr_diff(_expr(o), e) if o is not None else []
        return cat, f"`{_short(o.text if o else c.other_text, 45)}` → `{_short(n.text, 45)}`" + (f" ({'; '.join(d[:2])})" if d else "")
    if n.kind == "formal_in":
        return "함수 인자", f"인자 {_short(n.text, 50)} {'추가' if k == 'added' else '삭제' if k == 'removed' else '변경'}"
    verb = {"added": "추가", "removed": "삭제", "changed": "변경"}.get(k, k)
    return "코드", f"`{_short(n.text, 70)}` {verb}"


def _type_label(phr: list[tuple[str, str]], text: str, P) -> str:
    cats = [c for c, _ in phr]
    if P["error_call"].search(text):
        base = "오류 처리"
    elif "카운터" in cats or P["counter"].search(text):
        base = "재시도·카운터"
    elif "타이머" in cats or P["timer"].search(" ".join(t for _, t in phr)):
        base = "타이머"
    elif "상태값" in cats:
        base = "상태 처리"
    elif "함수" in cats:
        base = "함수 구성"
    elif "초기값" in cats or "자료형" in cats:
        base = "데이터 정의"
    else:
        base = "기능 로직"
    kinds = []
    for c in cats:
        lab = {"조건문": "조건", "함수 호출": "함수 호출", "상태값": "상태값", "계산식": "계산식", "실행 순서": "실행 순서",
               "실행 조건": "실행 조건", "조기 종료": "조기 종료", "카운터": "카운터", "타이머": "타이머", "대입": "대입",
               "초기값": "초기값", "자료형": "자료형", "함수": "함수", "함수 인자": "인자"}.get(c, c)
        if lab not in kinds:
            kinds.append(lab)
    return f"{base} — {'·'.join(kinds[:4])} 변경"


def _human_summary(g: list, phr, type_label: str) -> str:
    """검증자가 읽는 한 문장: 무엇이 어떻게 바뀌었는가 (핵심 사실 우선)."""
    facts = []
    rm = sorted({c for x in g if x.kind == "removed" for c in _calls(x.prog, x.n)})
    ad = sorted({c for x in g if x.kind == "added" for c in _calls(x.prog, x.n)})
    for x in g:
        if x.kind == "changed" and x.o is not None:
            for d in expr_diff(_expr(x.o), _expr(x.n)):
                m = re.match(r"호출 대상 (\w+)\(\) → (\w+)\(\)", d)
                if m:
                    facts.append(f"{m.group(1)}() 호출이 {m.group(2)}() 로 대체됨")
    if rm:
        facts.append(f"{', '.join(rm[:3])}() 호출 삭제")
    if ad:
        facts.append(f"{', '.join(ad[:3])}() 호출 추가")
    for x in g:
        if x.n.kind == "cond" and x.kind == "changed" and x.o is not None:
            d = [y for y in expr_diff(_expr(x.o), _expr(x.n)) if y.startswith(("상수", "연산자"))]
            if d:
                facts.append(f"판정 기준 변경 `{_short(x.o.text, 40)}` → `{_short(x.n.text, 40)}`")
    for c, t in phr:
        if c == "상태값":
            facts.append(t)
    na = sum(1 for x in g if x.n.kind == "cond" and x.kind == "added")
    nr = sum(1 for x in g if x.n.kind == "cond" and x.kind == "removed")
    if nr:
        facts.append(f"검사 조건 {nr}개 삭제")
    if na:
        facts.append(f"분기 조건 {na}개 추가")
    nm = sum(1 for x in g if x.kind == "moved")
    if nm:
        facts.append(f"문장 {nm}개 위치 이동")
    if not facts:
        facts = [t for _, t in phr[:2]]
    seen, out = set(), []
    for f in facts:
        if f not in seen:
            seen.add(f)
            out.append(f)
    head = type_label.split(" — ")[0]
    head = head if head.endswith("로직") else head + " 로직"
    return f"{head} 변경 — " + ", ".join(out[:4]) + (f" 등 {len(phr)}곳" if len(phr) > 4 else "") + "."


def _summary(phr, P) -> str:
    """한두 문장 요약."""
    cats = defaultdict(list)
    for c, t in phr:
        cats[c].append(t)
    parts = []
    for c in ("함수", "조건문", "함수 호출", "조기 종료", "상태값", "계산식", "카운터", "타이머", "실행 조건", "실행 순서",
              "대입", "초기값", "자료형", "함수 인자", "코드"):
        if c in cats:
            ts = cats[c]
            parts.append(ts[0] if len(ts) == 1 else f"{ts[0]} 외 {c} {len(ts) - 1}건")
    s = "; ".join(parts[:3])
    if len(parts) > 3:
        s += f" 외 {len(parts) - 3}종"
    return s + "."


def build(old: Program, new: Program, cs, result: dict, review: dict | None, deps_diff: dict | None,
          patterns: dict | None = None) -> dict:
    P = {k: re.compile(v, re.I) for k, v in {**DEFAULT_PATTERNS, **(patterns or {})}.items()}
    # --- 변경 목록 (짝은 하나로) ---
    chs: list[_Ch] = []
    for c in cs.changes:
        if c.kind in ("changed", "moved", "global_changed") and c.version == "old":
            continue
        prog, other = (new, old) if c.version == "new" else (old, new)
        chs.append(_Ch(c, prog, other))
    # 같은 줄에 문장·조건 변경이 있으면 그 안의 호출 인자·호출 노드 변경은 중복이므로 뺀다
    stmt_lines = {(ch.version, ch.c.func, ch.c.file, ch.line) for ch in chs if ch.n.kind not in ("actual_in", "call", "actual_out")}
    chs = [ch for ch in chs if ch.n.kind not in ("actual_in", "call", "actual_out")
           or (ch.version, ch.c.func, ch.c.file, ch.line) not in stmt_lines]
    by_func = defaultdict(list)
    for ch in chs:
        func = ch.prog.func_display(ch.c.func or "")
        by_func[(_rel(ch.prog, ch.c.file), func)].append(ch)

    # 변경 전 조건 노드 → 변경 후 조건 문장 (바뀐 조건 아래의 삭제 문장도 같은 블록으로 묶기 위해)
    old_to_new_text = {c.other_node: new.nodes[c.node].text for c in cs.changes
                       if c.version == "new" and c.other_node is not None}

    def chain(ch: _Ch) -> list[str]:
        """바깥 → 안쪽 제어 블록 열쇠 (if/else-if 사슬·반복·switch 의 맨 앞 조건 문장)."""
        prog, cur, out, seen = ch.prog, ch.n.id, [], set()
        if ch.n.kind == "cond":
            out.append(ch.n)
        while True:
            ps = [p for p, _ in prog.cd_parents.get(cur, ()) if prog.nodes[p].kind == "cond" and p not in seen]
            if not ps:
                break
            seen.add(ps[0])
            out.append(prog.nodes[ps[0]])
            cur = ps[0]
        keys = []
        for n in reversed(out):  # 바깥부터
            t = old_to_new_text.get(n.id, n.text) if prog is old else n.text
            keys.append(t)
        # else-if 사슬은 맨 앞 if 로 합친다: 거짓 쪽 제어 의존이면 같은 블록
        return keys

    units = []
    for (file, func), lst in sorted(by_func.items()):
        lst.sort(key=lambda x: (x.line, 0 if x.version == "old" else 1))
        keyed = [(chain(x), x) for x in lst]
        groups: list[list[_Ch]] = []
        top: dict[str, list] = {}
        for keys, ch in keyed:
            if keys:
                g = top.get(keys[0])
                if g is None:
                    g = top[keys[0]] = []
                    groups.append(g)
                g.append(ch)
            else:
                if groups and groups[-1] and not chain(groups[-1][-1]) and ch.line - max(x.line for x in groups[-1]) <= 2:
                    groups[-1].append(ch)
                else:
                    groups.append([ch])
        # 너무 큰 블록(큰 switch 등)은 한 단계 안쪽 블록으로 다시 나눈다
        final = []
        for g in groups:
            if len(g) <= 10:
                final.append(g)
                continue
            sub: dict = {}
            for ch in g:
                ks = chain(ch)
                sub.setdefault(ks[1] if len(ks) > 1 else ks[0] if ks else ch.line, []).append(ch)
            final += list(sub.values())
        for g in final:
            phr = [_phrase(x, P) for x in g]
            text = " ".join(x.n.text or "" for x in g) + " " + " ".join(t for _, t in phr)
            lo, hi = min(x.line for x in g), max(x.line for x in g)
            calls_rm, calls_add = [], []
            writes = set()
            for x in g:
                cl = _calls(x.prog, x.n)
                if x.kind == "removed" or (x.version == "old" and x.kind == "func_removed"):
                    calls_rm += cl
                elif x.kind in ("added", "func_added"):
                    calls_add += cl
                elif x.kind == "changed" and x.o is not None:
                    oc = _calls(x.other, x.o)
                    calls_rm += [z for z in oc if z not in cl]
                    calls_add += [z for z in cl if z not in oc]
                for p in x.n.defs:
                    v = x.prog.vars.get(p[0])
                    if v and v.kind in ("global", "static", "static_local"):
                        writes.add(display_path(x.prog, p))
            units.append({
                "id": f"U{len(units) + 1}", "file": file, "func": func, "lo": lo, "hi": hi,
                "type_label": _type_label(phr, text, P), "summary": _human_summary(g, phr, _type_label(phr, text, P)),
                "detail_summary": _summary(phr, P),
                "changes": [{"line": x.line, "kind": x.kind, "version": x.version,
                             "old": (x.c.other_text if x.kind in ("changed", "moved", "global_changed") else
                                     (x.c.text if x.version == "old" else "")),
                             "new": (x.c.text if x.version == "new" else ""), "detail": x.c.detail,
                             "category": phr[i][0], "phrase": phr[i][1]} if x.kind != "type_changed" else
                            {"line": x.line, "kind": x.kind, "version": x.version, "old": "", "new": f"{x.c.text}: {x.c.detail}",
                             "detail": "", "category": phr[i][0], "phrase": phr[i][1]} for i, x in enumerate(g)],
                "deps": {"calls_added": sorted(set(calls_add)), "calls_removed": sorted(set(calls_rm)),
                         "writes": sorted(writes)},
                "_lines": (lo, hi),
            })

    # --- 분석 결과를 묶음에 붙이기 ---
    def unit_for(file, func, line, text=""):
        cands = [u for u in units if u["file"] == file and u["func"] == func]
        if not cands or not line:
            return None
        inside = [u for u in cands if u["lo"] <= line <= u["hi"]] or \
                 [u for u in cands if u["lo"] - 2 <= line <= u["hi"] + 2]
        if not inside:
            return min(cands, key=lambda u: min(abs(u["lo"] - line), abs(u["hi"] - line)))
        if len(inside) > 1 and text:
            def score(u):
                return sum(1 for c in u["changes"] for t in (c["old"], c["new"]) if t and _short(t, 30)[:-1] in text)
            inside.sort(key=score, reverse=True)
        return inside[0]

    risks = {r["id"]: r for r in result.get("risks", [])}
    for u in units:
        u.update(risks=[], prop_risks=[], review=[], func_review=[])
    loose = []
    for r in result.get("risks", []):
        ch = r.get("change") or {}
        u = unit_for(ch.get("file", ""), ch.get("func", ""), ch.get("line", 0), (ch.get("after") or "") + (ch.get("before") or "")) if ch else None
        if u is None:
            u = unit_for(r["file"], r["func"], r["line"], r["expr"])
        if u is None:
            loose.append(r["id"])
            continue
        (u["risks"] if r.get("relation", "직접") == "직접" else u["prop_risks"]).append(r["id"])
    file_level = defaultdict(list)
    func_level = defaultdict(list)
    global_level = []
    unit_keys = {(u["file"], u["func"]) for u in units}
    for f in (review or {}).get("findings", []):
        u = unit_for(f["file"], f["func"], f["line"], f["summary"] + " ".join(f.get("evidence") or []))
        if u is not None:
            u["review"].append(f)
            continue
        key = next(((fl, fn) for fl, fn in unit_keys if fn == f["func"] and (fl == f["file"] or not f["file"]
                                                                          or f["file"].endswith(fl) or fl.endswith(f["file"]))), None)
        if key:
            func_level[key].append(f)
        elif f["file"]:
            file_level[f["file"]].append(f)
        else:
            global_level.append(f)

    # --- 묶음별 사실 표 · 문제 카드 · 체크리스트 · 등급 ---
    items = {it["id"]: it for it in (review or {}).get("items", [])}
    for u in units:
        facts = []
        for c in u["changes"]:
            facts.append({"area": c["category"], "result": c["phrase"], "status": "변경 확인"})
        issues = []
        for rid in u["risks"] + u["prop_risks"]:
            r = risks[rid]
            lvl = _risk_level(r)
            prop = rid in u["prop_risks"]
            facts.append({"area": "보안 분석", "result": f"[{rid}] {r['kind_label']} — {_vl_ko(r)}" + (f" (← 전파: {r['func']})" if prop else ""),
                          "status": {"red": "위반 경로 확인", "orange": "추가 검증 필요", "yellow": "추가 검증 필요"}.get(lvl, "판정 보류")})
            b = r.get("baseline") or {}
            issues.append({
                "level": lvl, "title": r["kind_label"] + (" (다른 함수로 전파)" if prop else ""),
                "where": f"{r['func']}() {r['file']}:{r['line']}", "expr": r["expr"],
                "basis": r.get("how", ""), "before": b.get("old_state_label", ""),
                "after": {"violated": "위반 (반례·증명)", "unknown": "위반 미확정", "holds": "안전"}.get(r.get("state"), ""),
                "risk": r.get("risk", ""), "verification_level": _vl_ko(r), "risk_ids": [rid], "items": [],
                "fact_vs_judgement": ("보안 판단: 안전 속성 위반이 증명됨" if lvl == "red" else
                                      "보안 판단 아님: 코드 변경 사실과 데이터 흐름만 확인됨 — 실제 취약점 여부는 검증 필요"),
                "evidence": [x for x in [r.get("evidence", ""), (r.get("bounds") or {}).get("change", "")] if x],
                "line": r["line"],
            })
        merged: dict = {}
        for f in u["review"]:
            if f["item"] in ("1-2", "1-3") or f["class"] == "변경 사실 확인":
                continue
            if f["summary"].startswith("["):  # 보안 분석 결과를 옮긴 항목은 위에서 이미 카드로 만듦
                continue
            if f["class"] in ("위험 후보 탐지", "위반 경로 확인"):
                key = (f["line"], _norm_issue(f["summary"]))
                m = merged.get(key)
                if m is None:
                    lvl = "red" if f["class"] == "위반 경로 확인" else "orange"
                    m = {"level": lvl, "title": _short(f["summary"], 90), "where": f"{u['func']}() {u['file']}:{f['line'] or u['lo']}",
                         "expr": "", "basis": "", "before": "", "after": "", "risk": f.get("reviewer", ""),
                         "verification_level": "정적 규칙(검토 항목)", "risk_ids": [], "items": [],
                         "fact_vs_judgement": "코드 변경 사실에서 도출한 결함 후보 — 요구사항으로 판단 필요",
                         "evidence": list(f.get("evidence") or []), "line": f["line"]}
                    merged[key] = m
                    issues.append(m)
                m["items"].append(f["item"])
            facts.append({"area": f"검토 {f['item']}", "result": _short(f["summary"], 110),
                          "status": {"위험 후보 탐지": "위험 후보", "위반 경로 확인": "위반 경로 확인",
                                     "검증자 확인 필요": "검증자 확인 필요", "분석 불충분": "분석 불충분"}.get(f["class"], f["class"])})
        # 같은 사실 중복 제거
        seen, fx = set(), []
        for x in facts:
            k = (x["area"], x["result"])
            if k not in seen:
                seen.add(k)
                fx.append(x)
        u["facts"] = fx
        issues.sort(key=lambda x: (LEVELS + ["old"]).index(x["level"]))
        u["issues"] = issues
        # 체크리스트: 관련 검토 항목만, 항목 번호당 하나
        ql, seen_q = [], set()
        for f in sorted(u["review"], key=lambda f: ["위반 경로 확인", "위험 후보 탐지", "검증자 확인 필요", "분석 불충분", "변경 사실 확인"].index(f["class"])):
            if f["class"] == "변경 사실 확인" or not f.get("reviewer") or f["item"] == "1-3":
                continue
            it = items.get(f["item"], {})
            k = f["item"]
            if k in seen_q:
                continue
            seen_q.add(k)
            ql.append({"qid": f"{u['id']}:{k}", "area": it.get("reviewer_focus", k), "item": k,
                       "question": f["reviewer"], "basis": _short(f["summary"], 120)})
        sem = [f for f in u["review"] if f["item"] == "1-3"]
        if sem:
            ql.insert(0, {"qid": f"{u['id']}:1-3", "area": "의도한 동작 변경인지", "item": "1-3",
                          "question": f"변경 전후 의미 차이 {len(sem)}곳이 변경 요청과 일치하는가?",
                          "basis": "; ".join(_short(f['summary'].split('`: ')[-1], 50) for f in sem[:3])})
        for rid in u["risks"] + u["prop_risks"]:
            r = risks[rid]
            if r.get("verification"):
                ql.append({"qid": f"{u['id']}:{rid}", "area": "재현·회귀 시험", "item": rid,
                           "question": r["verification"], "basis": f"{r['kind_label']} {r['expr']}"})
        if not ql:
            ql.append({"qid": f"{u['id']}:basic", "area": "변경 확인", "item": "-",
                       "question": "변경 내용이 변경 요청과 일치하고, 정상·오류 경로 시험이 충분한가?", "basis": u["summary"]})
        u["checklist"] = ql
        lv = [i["level"] for i in issues]
        u["level"] = next((x for x in LEVELS if x in lv), "yellow" if any(f["class"] == "검증자 확인 필요" for f in u["review"])
                          else "gray" if any(f["class"] == "분석 불충분" for f in u["review"]) else "none")
        vls = [_vl_ko(risks[r]) for r in u["risks"] + u["prop_risks"]]
        u["verification_level"] = next((x for x in ("반례 발견", "정적 규칙 위반", "Taint 경로 발견", "위반 미확정") if x in vls),
                                       "해당 위험 없음" if not vls else vls[0])
        u.pop("_lines", None)
        u.pop("review", None)

    # --- 함수 단위: 호출 대응표 · 영향 3단계 · 공유 데이터 · 검증할 동작 ---
    deps = {row["func"]: row for row in (deps_diff or {}).get("functions", [])}
    shared = {s["var"]: s for s in result.get("shared", [])}
    funcs = []
    items_list = result.get("items", [])
    for (file, func) in sorted({(u["file"], u["func"]) for u in units}):
        us = [u for u in units if u["file"] == file and u["func"] == func]
        row = deps.get(func)
        rm = sorted({c for u in us for c in u["deps"]["calls_removed"]})
        ad = sorted({c for u in us for c in u["deps"]["calls_added"]})
        if row:
            rm = sorted(set(rm) | set(row.get("calls_removed", [])))
            ad = sorted(set(ad) | set(row.get("calls_added", [])))
        pairs, used = [], set()
        for a in rm:
            best = max(((difflib.SequenceMatcher(None, a.lower(), b.lower()).ratio(), b) for b in ad if b not in used), default=(0, None))
            if best[1] and best[0] >= 0.5:
                used.add(best[1])
                pairs.append([a, best[1]])
            else:
                pairs.append([a, ""])
        pairs += [["", b] for b in ad if b not in used]
        direct, indirect = [], []
        for i, it in enumerate(items_list):
            p0 = (it.get("path") or [{}])[0]
            if p0.get("func") != func or p0.get("file") != file:
                continue
            (direct if it["func_display"] == func else indirect).append(i)
        vars_touched = set()
        if row:
            for k in ("reads_added", "reads_removed", "writes_added", "writes_removed"):
                vars_touched |= set(row.get(k, []))
        vars_touched |= {w for u in us for w in u["deps"]["writes"]}
        sh = [s for name, s in shared.items() if name in vars_touched or any(items_list[i]["name"] == name for i in direct)]
        behaviors = []
        for u in us:
            for q in u["checklist"]:
                if q["area"] not in behaviors and q["item"] not in ("-",) and not q["item"].startswith("V"):
                    behaviors.append(q["area"])
        fr = func_level.get((file, func), [])
        fq, seen_f = [], set()
        for f in fr:
            if f["class"] != "변경 사실 확인" and f.get("reviewer") and f["item"] not in seen_f:
                seen_f.add(f["item"])
                fq.append({"qid": f"F:{file}:{func}:{f['item']}", "area": items.get(f["item"], {}).get("reviewer_focus", f["item"]),
                           "item": f["item"], "question": f["reviewer"], "basis": _short(f["summary"], 120)})
        funcs.append({"func": func, "file": file, "units": [u["id"] for u in us], "summary": " ".join(u["summary"] for u in us[:2]),
                      "review": fr, "checklist": fq,
                      "call_pairs": pairs, "deps": row, "direct": direct, "indirect": indirect,
                      "shared": sh, "taint_risks": [r for u in us for r in u["risks"] + u["prop_risks"]
                                                    if (risks[r].get("taint") or {}).get("flow_detected")],
                      "behaviors": behaviors[:8], "level": next((x for x in LEVELS if any(u["level"] == x for u in us)), "none")})

    # --- 파일 단위 ---
    files = []
    for fname in sorted({u["file"] for u in units} | set(file_level)):
        us = [u for u in units if u["file"] == fname]
        lv = next((x for x in LEVELS if any(u["level"] == x for u in us)), "none")
        files.append({"file": fname, "funcs": sorted({u["func"] for u in us}), "units": [u["id"] for u in us], "level": lv,
                      "n_issues": sum(len(u["issues"]) for u in us),
                      "has_deps": any(f["deps"] for f in funcs if f["file"] == fname),
                      "file_review": file_level.get(fname, [])})
    files.sort(key=lambda f: (LEVELS.index(f["level"]) if f["level"] in LEVELS else 9, 0 if f["file"].endswith(".c") else 1, f["file"]))
    return {"units": units, "functions": funcs, "files": files, "global_review": global_level, "loose_risks": loose,
            "level_ko": LEVEL_KO}


def _norm_issue(s: str) -> str:
    return re.sub(r"`[^`]*`", "", s)[:40]


def _vl_ko(r: dict) -> str:
    v = r.get("verification_level", "")
    return {"기호 실행 반례 발견": "반례 발견", "정적 규칙 위반": "정적 규칙 위반", "Taint 경로 발견": "Taint 경로 발견",
            "검증 불충분": "위반 미확정"}.get(v, v or "위반 미확정")


def _risk_level(r: dict) -> str:
    """화면 등급: 증명된 위반만 빨강, 변경으로 새로 생긴 미확정 후보는 주황, 나머지 판정 보류는 노랑."""
    if r.get("status_code") in ("EXISTING", "FIXED"):
        return "old"
    if r.get("state") == "violated":
        return "red"
    if r.get("status_code") == "NEW_REGRESSION" or (r.get("taint") or {}).get("flow_detected"):
        return "orange"
    return "yellow"
