"""검토 화면용 '변경 이야기': 변경 한 건을 ① 변경 내용 → ② 자동 분석 내용 → ③ 발견 사항 → ④ 추가 검증으로 정리한다.

units.build() 가 만든 변경 묶음(검토 단위)마다
  ① 동작 중심 제목과 2~3문장 설명, 변경 전후 비교표(상위 조건·실행 순서·대입 대상 포함), 영향 범위(단계별 함수와 연결 근거)
  ② 스크립트가 실제로 한 검사: 실행 상태 · 확인한 내용 · 이 결과로 확정할 수 없는 것
  ③ 발견 사항: 같은 원인은 하나로 묶고, 변경 사실 / 위험 후보 / 분석 모델 내 위반 / 실행 재현 / 분석 불충분을 구분.
     전후 비교 상태(신규·기존·해결·판정 보류)는 근거 수준과 따로 둔다. 자동 분류 규칙 이름은 근거로만 남긴다.
  ④ 추가 검증: 입력·상태 조건, 관찰할 내용, 판정 기준. 요구사항이 없으면 기대값을 채우지 않고 '기대값 미확정'으로 둔다.
를 만든다. 변경(C)·검사(C-K)·발견(V/F)·시험(T) 은 ID 로 서로 연결된다.

여기서 만드는 문장은 모두 정적 분석 기록(코드 차이, 제어·데이터 의존, 영향 경로)에 근거하며,
실행으로 확인하지 않은 내용은 '미확인'·검증 과제로 남긴다.
"""

from __future__ import annotations

import difflib
import re
from collections import defaultdict
from pathlib import Path

from pycparser import c_ast

from .cexpr import gen
from .program import Program, display_path
from .review import DEFAULT_PATTERNS, _expr, _iter, expr_diff

ERR_VAR = re.compile(r"(?i)(err|fault|fail|dtc|diag|abnormal)")
FLAG_VAR = re.compile(r"(?i)(flag|flg|_f\b|enable|ena\b|req\b|lock)")
COND_RE = re.compile(r"^(?:if|while|switch|for)\s*\((.*)\)$", re.S)
NO_REQ = "기대값 미확정 — 관련 요구사항 확인 필요"
JUDGE = ["실행 재현 확인", "분석 모델 내 위반 확인", "위험 후보", "분석 불충분", "변경 사실 확인"]
PRIO = ["높음", "중간", "낮음"]


def _short(s, n=80):
    s = " ".join(str(s or "").split())
    return s if len(s) <= n else s[: n - 1] + "…"


def _rel(prog: Program, path: str) -> str:
    try:
        return Path(path).resolve().relative_to(prog.root.resolve()).as_posix()
    except (ValueError, OSError):
        return path or ""


def cond_expr(text: str) -> str:
    m = COND_RE.match((text or "").strip())
    return m.group(1).strip() if m else (text or "").strip()


def guards(prog: Program, nid: int) -> list[tuple[str, str]]:
    """노드를 감싸는 조건들 (바깥 → 안쪽): [(조건식, 'T'|'F'|case)]."""
    out, cur, seen = [], nid, set()
    while True:
        ps = [(p, lab) for p, lab in prog.cd_parents.get(cur, ()) if prog.nodes[p].kind == "cond" and p not in seen]
        if not ps:
            break
        p, lab = ps[0]
        seen.add(p)
        out.append((cond_expr(prog.nodes[p].text), lab or "T"))
        cur = p
    return list(reversed(out))


def guard_str(gs: list[tuple[str, str]]) -> str:
    if not gs:
        return "조건 없음 (함수가 실행될 때마다)"
    parts = []
    for e, lab in gs:
        if lab == "T":
            parts.append(f"`{e}`")
        elif lab == "F":
            parts.append(f"`{e}` 거짓")
        else:
            parts.append(f"`{e}` → {lab}")
    return " 그리고 ".join(parts)


def _writes(prog: Program, n) -> list[str]:
    out = []
    for p in n.defs:
        v = prog.vars.get(p[0])
        if v and v.kind in ("global", "static", "static_local"):
            out.append(display_path(prog, p))
    return sorted(set(out))


def _calls(prog: Program, n) -> list[str]:
    if n.kind in ("call", "extcall") and n.site is not None:
        return [prog.sites[n.site].name]
    out = []
    for x in _iter(_expr(n)):
        if isinstance(x, c_ast.FuncCall):
            sid = prog.site_of.get(id(x))
            out.append(prog.sites[sid].name if sid is not None else gen(x.name))
    return out


VERBS = [("adjust", "조정"), ("adj", "조정"), ("reset", "리셋"), ("init", "초기화"), ("clear", "해제"), ("clr", "해제"),
         ("update", "갱신"), ("upd", "갱신"), ("write", "쓰기"), ("read", "읽기"), ("send", "송신"), ("transmit", "송신"),
         ("tx", "송신"), ("rx", "수신"), ("report", "보고"), ("check", "검사"), ("calc", "계산"), ("start", "시작"),
         ("stop", "정지"), ("set", "설정"), ("get", "조회"), ("enable", "활성화"), ("disable", "비활성화")]


def call_verb(name: str) -> str | None:
    toks = [t.lower() for t in re.findall(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])|\d+", name.split("_")[-1] or name)] or [name.lower()]
    for t in reversed(toks):
        for k, v in VERBS:
            if t == k:
                return v
    return None


class _Labeler:
    def __init__(self, P):
        self.P = P

    def var(self, name: str) -> str:
        """상태 갱신 대상의 이름 (갱신 경로 설명용)."""
        if self.P["counter"].search(name):
            return "재시도 카운터" if re.search(r"(?i)retry|rtry", name) else "카운터"
        if ERR_VAR.search(name):
            return "오류 상태"
        if self.P["timer"].search(name):
            return "타이머"
        if FLAG_VAR.search(name):
            return "플래그"
        return f"`{name}`"

    def var_short(self, name: str) -> str:
        """처리 이름 (블록 이름용)."""
        if self.P["counter"].search(name):
            return "재시도" if re.search(r"(?i)retry|rtry", name) else "카운터"
        if ERR_VAR.search(name):
            return "오류 처리"
        if self.P["timer"].search(name):
            return "타이머"
        if FLAG_VAR.search(name):
            return "플래그"
        return f"`{name}`"

    def call(self, name: str) -> str:
        if self.P["error_call"].search(name):
            return f"오류 처리 `{name}()`"
        return f"`{name}()` 호출"

    def call_short(self, name: str) -> str:
        if self.P["error_call"].search(name):
            return "오류 처리"
        return call_verb(name) or f"`{name}()`"

    def _collect(self, prog, nodes, fc, fv, skip=()):
        labs = []
        for n in nodes:
            for c in (_calls(prog, n) if fc else []):
                lab = fc(c)
                if lab not in labs:
                    labs.append(lab)
            for w in _writes(prog, n):
                if w in skip:
                    continue
                lab = fv(w)
                if lab not in labs:
                    labs.append(lab)
        return labs

    def effects(self, prog: Program, nodes, skip=()) -> list[str]:
        """호출·상태 갱신 (관찰 항목용)."""
        return self._collect(prog, nodes, self.call, self.var, skip)

    def states(self, prog: Program, nodes, skip=()) -> list[str]:
        """갱신되는 상태만."""
        return self._collect(prog, nodes, None, self.var, skip)

    def block(self, prog: Program, nodes, skip=()) -> str:
        labs = self._collect(prog, nodes, self.call_short, self.var_short, skip)
        return "·".join(x.replace("`", "") for x in labs[:3]) or "기존"


def _proc(b: str) -> str:
    return b if b.endswith("처리") else b + " 처리"


def _topic(type_label: str) -> str:
    base = type_label.split(" — ")[0]
    return {"재시도·카운터": "재시도 처리", "오류 처리": "오류 처리", "타이머": "타이머 처리", "상태 처리": "상태 처리",
            "함수 구성": "함수 구성", "데이터 정의": "데이터 정의", "기능 로직": "기능 로직"}.get(base, base)


# ---------------------------------------------------------------------------
# ① 무엇이 변경되었는가
# ---------------------------------------------------------------------------


def _analyze_unit(u: dict, g: list, new: Program, old: Program, L: _Labeler, ctl_vars: list[dict]) -> dict:
    """비교표·설명·제목·검증 설계에 쓰는 구조 정보."""
    func = u["func"]
    added_conds = [x for x in g if x.kind == "added" and x.n.kind == "cond"]
    removed_conds = [x for x in g if x.kind == "removed" and x.n.kind == "cond"]
    changed_conds = [x for x in g if x.kind == "changed" and x.n.kind == "cond" and x.o is not None]
    moved = [x for x in g if x.kind == "moved" and x.o is not None]
    added = [x for x in g if x.kind == "added" and x.n.kind != "cond"]
    removed = [x for x in g if x.kind == "removed" and x.n.kind != "cond"]
    changed = [x for x in g if x.kind == "changed" and x.n.kind != "cond" and x.o is not None]

    # 새 조건이 감싸게 된 기존 문장 (이동한 문장 중 새 상위 조건에 그 조건이 들어 있는 것)
    gating = []
    for c in added_conds:
        ce = cond_expr(c.n.text)
        under = [x for x in moved if any(e == ce for e, _ in guards(new, x.n.id))]
        under_add = [x for x in added if any(e == ce for e, _ in guards(new, x.n.id))]
        if under or under_add:
            gating.append({"cond": c, "expr": ce, "moved": under, "added": under_add})
    gated_ids = {id(x) for gt in gating for x in gt["moved"]}
    order_moves = [x for x in moved if id(x) not in gated_ids]

    rows: list[dict] = []
    behaviors: list[tuple[str, str]] = []
    sents: list[str] = []
    var_names = [v["name"] for v in ctl_vars]
    if ctl_vars:
        rows.append({"item": "제어 변수", "before": "신규 변수 없음",
                     "after": ", ".join(f"`{v['name']}` 추가" + (f" (초기값 {v['init']})" if v.get("init") else "") for v in ctl_vars)})

    for gt in gating:
        top = [x for x in gt["moved"] if guards(new, x.n.id) and guards(new, x.n.id)[-1][0] == gt["expr"]] or gt["moved"]
        gvars = set(re.findall(r"[A-Za-z_]\w*", gt["expr"]))
        nodes = [x.n for x in gt["moved"]] + [x.n for x in gt["added"]]
        blk = L.block(new, nodes, gvars)
        gt["block"] = blk
        gt["effects"] = L.effects(new, nodes)
        gt["states"] = L.states(new, nodes, gvars)
        old_g = guards(old, top[0].o.id) if top else []
        new_g = [x for x in guards(new, top[0].n.id) if x[0] != gt["expr"]] if top else []
        rows.append({"item": f"{blk} 블록 진입 조건",
                     "before": guard_str(old_g) if old_g else "해당 조건 없음",
                     "after": (guard_str(new_g) + " 그리고 " if new_g else "") + f"`{gt['expr']}` (추가)"})
        before_b = (guard_str(old_g) + " 이면 " if old_g else "") + "해당 블록 실행"
        after_b = f"추가된 `{gt['expr']}` 조건도 만족해야 해당 블록 실행"
        behaviors.append((before_b, after_b))
        uses_var = next((v for v in var_names if re.search(r"\b" + re.escape(v) + r"\b", gt["expr"])), None)
        flagish = bool(uses_var and FLAG_VAR.search(uses_var)) or bool(re.search(r"(?i)flag|flg", gt["expr"]))
        gt["flag"] = flagish
        gt["var"] = uses_var or (re.findall(r"[A-Za-z_]\w*", gt["expr"]) or [""])[0]
        lead = f"`{uses_var}` 변수가 추가되고, " if uses_var else ""
        sents.append(f"{lead}기존 {_proc(blk)}에 {'이 변수를 확인하는 ' if uses_var else ''}`{gt['expr']}` 조건이 추가되었습니다.")
        # 같은 블록 안에서 조건 변수를 바꾸는 문장 (한 번 지나면 다음부터 블록에 들어가지 않는 구조)
        for x in gt["added"]:
            e = _expr(x.n)
            if isinstance(e, c_ast.Assignment) and gen(e.lvalue) in gvars:
                inner = [gg for gg in guards(new, x.n.id) if gg[0] != gt["expr"]]
                where = guard_str(inner[-1:]) + " 경로" if inner else "블록 안"
                back = [v for v in _var_values(new, gen(e.lvalue)) if v != gen(e.rvalue)]
                sents.append(f"{where}에 `{gen(e.lvalue)} = {gen(e.rvalue)}` 이 추가되어, 이 경로를 지난 뒤에는 "
                             f"`{gt['expr']}` 조건이 성립하지 않습니다"
                             + ("." if len(back) > 1 else " (다시 바꾸는 코드는 이번 분석 범위에서 찾지 못함)."))
                gt["latch"] = {"var": gen(e.lvalue), "value": gen(e.rvalue), "reset_found": len(back) > 1}
    for x in moved:
        if x.n.kind != "cond":
            continue
        e_old, e_new = cond_expr(x.o.text), cond_expr(x.n.text)
        rows.append({"item": f"`{_short(e_new, 50)}` 비교", "before": f"`{e_old}`",
                     "after": "동일 (실행 위치만 이동)" if e_old == e_new else f"`{e_new}`"})
        if e_old == e_new and id(x) in gated_ids:
            sents.append(f"비교식 `{e_new}` 는 그대로이지만, 이 검사를 실행하는 상위 조건이 바뀌었습니다.")
    for x in changed_conds:
        e_old, e_new = cond_expr(x.o.text), cond_expr(x.n.text)
        d = expr_diff(_expr(x.o), _expr(x.n))
        rows.append({"item": "판정 조건", "before": f"`{e_old}`", "after": f"`{e_new}`" + (f" ({'; '.join(d[:2])})" if d else "")})
        behaviors.append((f"`{e_old}` 일 때 분기", f"`{e_new}` 일 때 분기"))
        sents.append(f"판정 조건이 `{e_old}` 에서 `{e_new}` 로 바뀌었습니다" + (f" ({'; '.join(d[:2])})." if d else "."))
    for x in added_conds:
        if any(gt["cond"] is x for gt in gating):
            continue
        e = cond_expr(x.n.text)
        rows.append({"item": "분기 조건", "before": "없음", "after": f"`{e}` 추가 — 실행 조건: {guard_str(guards(new, x.n.id))}"})
        sents.append(f"`{e}` 조건이 새로 추가되었습니다.")
    for x in removed_conds:
        e = cond_expr(x.n.text)
        rows.append({"item": "분기 조건", "before": f"`{e}` — 실행 조건: {guard_str(guards(old, x.n.id))}", "after": "삭제"})
        behaviors.append((f"`{e}` 를 만족할 때만 내부 처리 실행", "이 조건 검사 없이 처리"))
        sents.append(f"`{e}` 조건 검사가 삭제되었습니다.")
    for x in changed:
        e_o, e_n = _expr(x.o), _expr(x.n)
        if isinstance(e_o, c_ast.Assignment) and isinstance(e_n, c_ast.Assignment) and gen(e_o.lvalue) == gen(e_n.lvalue):
            rows.append({"item": f"`{gen(e_n.lvalue)}` 대입값", "before": f"`{gen(e_o.rvalue)}`", "after": f"`{gen(e_n.rvalue)}`"})
            sents.append(f"`{gen(e_n.lvalue)}` 에 대입하는 값이 `{_short(gen(e_o.rvalue), 40)}` 에서 `{_short(gen(e_n.rvalue), 40)}` 로 바뀌었습니다.")
        elif isinstance(e_o, c_ast.Assignment) and isinstance(e_n, c_ast.Assignment):
            rows.append({"item": "대입 대상", "before": f"`{gen(e_o.lvalue)}` ← `{_short(gen(e_o.rvalue), 40)}`",
                         "after": f"`{gen(e_n.lvalue)}` ← `{_short(gen(e_n.rvalue), 40)}`"})
            sents.append(f"대입 대상이 `{gen(e_o.lvalue)}` 에서 `{gen(e_n.lvalue)}` 로 바뀌었습니다.")
        else:
            d = expr_diff(e_o, e_n)
            rows.append({"item": "문장", "before": f"`{_short(x.o.text, 60)}`", "after": f"`{_short(x.n.text, 60)}`" + (f" ({'; '.join(d[:2])})" if d else "")})
    gated_add = {id(x) for gt in gating for x in gt["added"]}
    for x in added:
        gs = guards(new, x.n.id)
        rows.append({"item": f"`{_short(x.n.text, 50)}`", "before": "없음", "after": f"추가 — 실행 조건: {guard_str(gs)}"})
        cl = _calls(new, x.n)
        if id(x) in gated_add:
            continue
        if cl:
            sents.append(f"`{cl[0]}()` 호출이 추가되었습니다 (실행 조건: {guard_str(gs)}).")
        elif _writes(new, x.n):
            sents.append(f"`{_short(x.n.text, 50)}` 문장이 추가되었습니다 (실행 조건: {guard_str(gs)}).")
    for x in removed:
        gs = guards(old, x.n.id)
        rows.append({"item": f"`{_short(x.n.text, 50)}`", "before": f"실행 — 조건: {guard_str(gs)}", "after": "삭제"})
        cl = _calls(old, x.n)
        if cl:
            behaviors.append((f"`{cl[0]}()` 호출", "호출하지 않음"))
            sents.append(f"`{cl[0]}()` 호출이 삭제되었습니다.")
        else:
            sents.append(f"`{_short(x.n.text, 50)}` 문장이 삭제되었습니다.")
    for x in order_moves:
        if x.n.kind == "cond":
            continue
        og, ng = guards(old, x.o.id), guards(new, x.n.id)
        if og != ng:
            rows.append({"item": f"`{_short(x.n.text, 50)}` 실행 조건", "before": guard_str(og), "after": guard_str(ng)})
        else:
            rows.append({"item": f"`{_short(x.n.text, 50)}` 실행 순서", "before": f"{x.o.line}행 위치", "after": f"{x.n.line}행 위치 (순서 이동)"})
            behaviors.append((f"`{_short(x.n.text, 40)}` 를 원래 순서에서 실행", "다른 문장과의 실행 순서가 바뀜"))
    for x in g:
        if x.kind in ("func_added", "func_removed", "type_changed", "global_changed", "global_added", "global_removed"):
            rows.append({"item": {"func_added": "함수", "func_removed": "함수", "type_changed": "자료형", "global_changed": "초기값",
                                  "global_added": "전역 변수", "global_removed": "전역 변수"}[x.kind],
                         "before": _short(x.c.other_text, 60) if x.kind in ("global_changed", "type_changed") else
                         ("없음" if x.kind.endswith("added") else _short(x.c.text, 60)),
                         "after": _short(x.c.text, 60) if not x.kind.endswith("removed") else "삭제"})
            if x.kind == "func_added":
                sents.append(f"`{func}()` 함수가 새로 추가되었습니다.")
            elif x.kind == "func_removed":
                sents.append(f"`{func}()` 함수가 삭제되었습니다.")
    if behaviors:
        rows.append({"item": "달라지는 동작", "before": " / ".join(b for b, _ in behaviors[:2]),
                     "after": " / ".join(a for _, a in behaviors[:2]), "behavior": True})

    # 제목
    topic = _topic(u["type_label"])
    eff_all = L.effects(new, [x.n for x in g if x.version == "new"]) + L.effects(old, [x.n for x in g if x.version == "old"])
    if gating:
        gt = gating[0]
        title = (f"{_proc(gt['block'])}에 {'플래그 ' if gt['flag'] else ''}조건 추가 — "
                 f"{'·'.join(e.replace('`', '') for e in gt['states'][:3]) or '처리'} 갱신 경로 변경")
    elif changed_conds:
        x = changed_conds[0]
        d = [y for y in expr_diff(_expr(x.o), _expr(x.n)) if y.startswith(("상수", "연산자"))]
        title = f"{topic} 판정 기준 변경" + (f" ({d[0]})" if d else "")
    elif removed and any(_calls(old, x.n) for x in removed):
        c = next(_calls(old, x.n)[0] for x in removed if _calls(old, x.n))
        title = f"{L.call(c).replace('`', '')} 삭제 — {topic} 경로 변경"
    elif removed_conds:
        title = f"{topic} 검사 조건 삭제"
    elif added and any(_calls(new, x.n) for x in added):
        c = next(_calls(new, x.n)[0] for x in added if _calls(new, x.n))
        title = f"{L.call(c).replace('`', '')} 추가 — {topic} 경로 변경"
    elif changed and isinstance(_expr(changed[0].n), c_ast.Assignment):
        title = f"`{gen(_expr(changed[0].n).lvalue)}` 대입값 변경 — {topic}"
    elif order_moves:
        title = f"{topic} 실행 순서·조건 변경"
    elif any(x.kind == "func_added" for x in g):
        title = f"함수 `{func}()` 추가"
    elif any(x.kind == "func_removed" for x in g):
        title = f"함수 `{func}()` 삭제"
    elif any(x.kind == "type_changed" for x in g):
        x = next(x for x in g if x.kind == "type_changed")
        title = f"자료형 {x.c.text} 정의 변경"
    elif any(x.n.kind == "formal_in" for x in g) or any(x.n.kind == "return" for x in g):
        parts = []
        if any(x.n.kind == "formal_in" and x.kind == "added" for x in g):
            parts.append("인자 추가")
        if any(x.n.kind == "formal_in" and x.kind == "removed" for x in g):
            parts.append("인자 삭제")
        if any(x.n.kind == "return" for x in g):
            parts.append("반환값 계산 변경")
        title = f"{func}() {'·'.join(parts) or '인터페이스 변경'}"
    elif any(x.kind.startswith("global") for x in g):
        title = "전역 변수 " + ("추가" if any(x.kind == "global_added" for x in g) else "삭제" if any(x.kind == "global_removed" for x in g) else "초기값 변경")
    else:
        title = f"{topic} 변경 — {u['changes'][0]['phrase'] if u['changes'] else ''}"
    if not sents:
        sents = [u["summary"]]
    sents.sort(key=lambda t: 0 if "조건이 추가되었습니다" in t else 1 if t.startswith("비교식") else
               2 if "호출이 삭제" in t or "호출이 추가" in t else 3 if t.startswith("판정 조건") else 4)
    return {"rows": rows[:12], "more_rows": rows[12:], "sentences": sents[:3], "title": title.replace("`", ""),
            "gating": gating, "changed_conds": changed_conds, "removed_conds": removed_conds,
            "removed": removed, "added": added, "changed": changed, "moved": moved, "effects": eff_all}


# ---------------------------------------------------------------------------
# 영향 범위
# ---------------------------------------------------------------------------


def _edge(st):
    e = st.get("edge") or ["", ""]
    return (e[0] if e else "", e[1] if len(e) > 1 else "")


def _segments(path: list[dict]) -> list[dict]:
    segs: list[dict] = []
    for st in path:
        if st["kind"] in ("actual_out", "nop", "exit"):
            continue
        if segs and segs[-1]["func"] == st["func"]:
            segs[-1]["steps"].append(st)
        else:
            segs.append({"func": st["func"], "file": st["file"], "steps": [st], "in": _edge(st)})
    return segs


def _impact(u: dict, items: list[dict], extern_names: set[str]) -> dict:
    mine = []
    for i, it in enumerate(items):
        p = [st for st in (it.get("path") or []) if st["kind"] not in ("actual_out", "nop", "exit")]
        if not p:
            continue
        s0 = p[0]
        if s0["func"] == u["func"] and s0["file"] == u["file"] and u["lo"] - 1 <= s0["line"] <= u["hi"] + 1:
            mine.append((i, it, p))
    cross = [(i, it, p) for i, it, p in mine if len({st["func"] for st in p}) > 1]
    cross.sort(key=lambda t: (0 if t[2][-1]["kind"] == "extcall" else 1, -len({st["func"] for st in t[2]}), -len(t[2])))
    core = []
    chosen = []
    seen_end = set()
    covered: list[set] = []
    for i, it, p in cross:
        end = (p[-1]["func"], p[-1]["line"])
        steps = {(st["func"], st["line"]) for st in p}
        if end in seen_end or len(chosen) >= 2 or any(steps <= c for c in covered):
            continue
        seen_end.add(end)
        covered.append(steps)
        chosen.append((i, it, p))
    for k, (i, it, p) in enumerate(chosen):
        segs = _segments(p)
        stages = []
        for j, sg in enumerate(segs):
            nxt = segs[j + 1] if j + 1 < len(segs) else None
            out_var = nxt["in"][1] if nxt and nxt["in"][0] in ("memory", "data") else ""
            last = sg["steps"][-1]
            in_kind, in_var = sg["in"]
            if j == 0:
                stage = "변경 발생"
                basis = f"`{_short(last['text'], 60)}`" + (f" — `{out_var}` 갱신 조건·위치 변경" if out_var else "")
            elif nxt is None and last["kind"] == "extcall":
                stage = "외부 출력 후보"
                call = re.match(r"\s*([A-Za-z_]\w*)\s*\(", last["text"] or "")
                basis = (f"`{in_var}` 를 포함한 값을 " if in_var else "") + f"`{call.group(1) if call else _short(last['text'], 40)}()` 에 전달"
            elif nxt is None:
                stage = "영향 도달"
                basis = (f"`{in_var}` 를 읽어 " if in_var else "") + f"`{_short(last['text'], 60)}` 실행"
            else:
                stage = "상태 판단"
                basis = (f"`{in_var}` 를 읽어 " if in_var else "") + (f"`{out_var}` 설정" if out_var else f"`{_short(last['text'], 50)}` 실행")
            if j == 0:
                link = "변경 지점"
            elif in_kind == "memory":
                link = f"공유 변수 `{in_var}` 로 연결 (호출 관계 아님)"
            elif in_kind in ("call", "param"):
                link = "함수 호출로 연결" + (f" (`{in_var}`)" if in_var else "")
            elif in_kind == "return":
                link = "반환값으로 연결"
            else:
                link = f"{in_kind} `{in_var}`".strip()
            stages.append({"stage": stage, "func": sg["func"], "file": sg["file"], "line": sg["steps"][0]["line"],
                           "basis": basis, "link": link})
        core.append({"item": i, "stages": stages, "end_external": p[-1]["kind"] == "extcall"})
    outs = []
    for k in core:
        last = k["stages"][-1]
        if last["stage"] == "외부 출력 후보":
            m = re.search(r"`([A-Za-z_]\w*)\(\)` 에 전달", last["basis"])
            if m and m.group(1) not in outs:
                outs.append(m.group(1))
    core_outs = list(outs)
    for i, it, p in mine:
        if p[-1]["kind"] == "extcall":
            m = re.match(r"\s*([A-Za-z_]\w*)\s*\(", p[-1]["text"] or "")
            if m and m.group(1) not in outs:
                outs.append(m.group(1))
    funcs = sorted({st["func"] for _, _, p in mine for st in p} - {u["func"]})
    paths = [{"name": it.get("name", ""), "kind": it.get("how_kind", ""), "func": it.get("func_display", ""),
              "steps": [{"func": st["func"], "file": st["file"], "line": st["line"], "text": st.get("src") or st["text"],
                         "edge": list(st.get("edge") or [])} for st in p]} for _, it, p in mine[:40]]
    return {"core": core, "paths": paths, "items": [i for i, _, _ in mine], "n_paths": len(mine), "n_cross": len(cross),
            "funcs": funcs, "external": outs, "outputs": core_outs,
            "note": "정적 분석으로 연결된 영향 후보 경로이며, 실제 실행 순서와 출력 변화는 미확인입니다." if mine else ""}


# ---------------------------------------------------------------------------
# ③ 발견 사항 · ④ 추가 검증
# ---------------------------------------------------------------------------


def _cmp_status(code: str | None) -> str:
    return {"NEW_REGRESSION": "신규", "EXISTING": "기존", "FIXED": "해결", "PENDING": "판정 보류"}.get(code or "", "판정 보류")


def _judge_risk(r: dict) -> str:
    sym = r.get("symbolic") or {}
    if (r.get("klee") or {}).get("violated"):
        return "분석 모델 내 위반 확인"
    if r.get("state") == "violated" and (sym.get("counterexample") or sym.get("result") == "violated" or r.get("proof")):
        return "분석 모델 내 위반 확인"
    if r.get("state") == "violated":
        return "분석 모델 내 위반 확인"
    if r.get("status_code") == "FIXED" or r.get("state") == "holds":
        return "변경 사실 확인"
    return "위험 후보"


def _evidence_level(r: dict | None, imp: dict, sil_ran: bool) -> list[str]:
    ev = ["코드 변경 확인"]
    if imp["n_paths"] or (r and (r.get("taint") or {}).get("flow_detected")):
        ev.append("의존 경로 확인")
    if r is not None:
        sym = r.get("symbolic")
        if r.get("state") == "violated":
            ev.append("분석 모델 반례 있음")
        elif sym:
            ev.append("기호 실행 수행 — 위반 반례 없음" if r.get("state") == "holds" else "기호 실행 수행 — 판정 불가")
        else:
            ev.append("위반 반례 없음 (기호 실행 미실행)")
    ev.append("실제 재현 결과 있음" if sil_ran else "실제 재현 결과 없음")
    return ev


def _boundary(expr: str) -> tuple[str, list[str]] | None:
    m = re.search(r"([A-Za-z_][\w.\[\]>-]*)\s*(<=|>=|==|!=|<|>)\s*\(?\s*(\d+)[uUlL]*\s*\)?", expr or "")
    if not m:
        return None
    v, op, k = m.group(1), m.group(2), int(m.group(3))
    if op in ("==", "!="):
        vals = [str(k), str(1 - k) if k in (0, 1) else f"{k} 외 값"]
        if k in (0, 1):
            vals = ["0", "1"]
    else:
        vals = [str(x) for x in (k - 1, k, k + 1) if x >= 0]
    return v, vals


def _var_values(prog: Program, var: str) -> list[str]:
    vals = []
    for n in prog.nodes:
        e = _expr(n)
        if isinstance(e, c_ast.Assignment) and gen(e.lvalue) == var:
            r = gen(e.rvalue)
            if r not in vals:
                vals.append(r)
        elif n.kind == "ginit" and n.text.startswith(var + " ("):
            m = re.search(r"\(([^)]*)\)", n.text)
            if m and m.group(1) not in vals:
                vals.insert(0, m.group(1).replace("으로 초기화", "").strip())
    return vals


def build(old: Program, new: Program, units_out: dict, analysis: dict, review: dict | None, ctx: dict,
          patterns: dict | None = None) -> dict:
    """ctx: versions, file_changes, analyzed, scope, tools, sil, explore, parse_errors."""
    P = {k: re.compile(v, re.I) for k, v in {**DEFAULT_PATTERNS, **(patterns or {})}.items()}
    L = _Labeler(P)
    units = units_out["units"]
    risks = {r["id"]: r for r in analysis.get("risks", [])}
    items = analysis.get("items", [])
    extern_names = {e["name"] if isinstance(e, dict) else e for e in (analysis.get("excluded") or {}).get("extern_functions", [])}
    sil_ran = bool(ctx.get("sil") or ctx.get("explore"))
    review_by_unit = defaultdict(list)
    for f in (review or {}).get("findings", []):
        for u in units:
            if u["file"] == f["file"] and u["func"] == f["func"] and f["line"] and u["lo"] <= f["line"] <= u["hi"]:
                review_by_unit[u["id"]].append(f)
                break

    # 전역 변수 추가는 그 변수를 쓰는 변경에 붙인다 (따로 보면 의미가 없음)
    merged_into: dict[str, str] = {}
    ctl_by_unit: dict[str, list[dict]] = defaultdict(list)
    for u in units:
        if u["func"] != "(전역 초기화)" or not all(c["kind"] == "global_added" for c in u["changes"]):
            continue
        for c in u["changes"]:
            name = c["new"].split(" (")[0]
            init = re.search(r"\((.*)\)", c["new"])
            user = next((v for v in units if v is not u and v["func"] != "(전역 초기화)" and any(
                re.search(r"\b" + re.escape(name) + r"\b", (x["new"] or "") + (x["old"] or "")) for x in v["changes"])), None)
            if user is not None:
                ctl_by_unit[user["id"]].append({"name": name, "init": init.group(1).replace("으로 초기화", "") if init else ""})
                merged_into[u["id"]] = user["id"]

    changes, findings, tests = [], [], []
    fnum = 0
    for u in units:
        if u["id"] in merged_into:
            continue
        g = u.get("_g") or []
        A = _analyze_unit(u, g, new, old, L, ctl_by_unit.get(u["id"], []))
        imp = _impact(u, items, extern_names)
        cid = f"C{len(changes) + 1:02d}"
        old_lines = [x.o.line for x in g if x.o is not None] + [x.line for x in g if x.version == "old"]
        rv = review_by_unit.get(u["id"], [])
        flags = [f for f in rv if f["class"] not in ("변경 사실 확인",)]

        # ② 스크립트가 확인한 것
        checks = []

        def chk(item, status, confirmed, cannot):
            checks.append({"id": f"{cid}-K{len(checks) + 1}", "item": item, "status": status, "confirmed": confirmed, "cannot": cannot})

        kinds = []
        if ctl_by_unit.get(u["id"]):
            kinds.append("변수 추가")
        for lab, lst in (("조건문 추가", [x for x in g if x.kind == "added" and x.n.kind == "cond"]),
                         ("조건 변경", A["changed_conds"]), ("조건 삭제", A["removed_conds"]),
                         ("문장 추가", A["added"]), ("문장 삭제", A["removed"]), ("문장 변경", A["changed"]),
                         ("기존 문장 이동", A["moved"])):
            if lst:
                kinds.append(f"{lab} {len(lst)}개" if len(lst) > 1 else lab)
        chk("변경 전후 코드 비교", "분석 결과 있음", ", ".join(kinds) or u["detail_summary"], "변경이 요구사항에 맞는지")
        cond_facts = []
        for x in A["moved"]:
            if x.n.kind == "cond" and cond_expr(x.o.text) == cond_expr(x.n.text):
                cond_facts.append(f"`{cond_expr(x.n.text)}` 식은 동일하지만 감싸는 조건이 변경됨")
        for x in A["changed_conds"]:
            cond_facts.append(f"`{cond_expr(x.o.text)}` → `{cond_expr(x.n.text)}`")
        for gt in A["gating"]:
            cond_facts.append(f"상위 조건 `{gt['expr']}` 추가")
        for x in A["removed_conds"]:
            cond_facts.append(f"`{cond_expr(x.n.text)}` 삭제")
        if cond_facts:
            counterish = any(P["counter"].search(f) for f in cond_facts)
            chk("조건·분기 비교", "분석 결과 있음", "; ".join(cond_facts[:3]),
                "실제 재시도 횟수와 복구 동작" if counterish else "조건 경계에서의 실제 분기 결과")
        if imp["n_paths"]:
            if imp["core"]:
                st = imp["core"][0]["stages"]
                names = [re.findall(r"`([^`]+)`", x["basis"]) for x in st]
                first = names[0][-1] if names and names[0] else u["func"]
                mid = [n[-1] for n in names[1:-1] if n]
                last = names[-1][-1] if names[-1] else st[-1]["func"]
                confirmed = f"`{first}` 에서 " + "".join(f"`{m}` 를 거쳐 " for m in mid[:2]) + f"`{last}` 까지의 의존 관계"
            else:
                confirmed = f"`{u['func']}()` 안에서 변경 문장의 값·실행 여부를 쓰는 문장 {imp['n_paths']}곳"
            chk("영향 경로 추적", f"분석 결과 있음 (경로 {imp['n_paths']}개, 다른 함수로 {imp['n_cross']}개)", confirmed,
                "실제 송신값·송신 시점" if imp["outputs"] else "실제 실행 순서와 값")
        else:
            chk("영향 경로 추적", "실행 — 영향 경로 없음", "이번 분석 범위에서 다른 문장·함수로 이어지는 의존 경로 미탐지",
                "분석 범위 밖(다른 C 파일·비활성 #if) 영향")
        chk("자동 검토 항목 (25개)", f"실행 — 해당 지적 {len(flags)}건" if flags else "실행 — 해당 지적 없음",
            ", ".join(sorted({f['item'] for f in flags})) and "지적 항목: " + ", ".join(sorted({f['item'] for f in flags}))
            or "이번 분석 범위에서 25개 검토 규칙의 지적 미탐지", "지적 사항이 실제 결함인지")
        urisks = [risks[r] for r in u["risks"] + u["prop_risks"] if r in risks]
        if urisks:
            chk("보안·안전 규칙, Taint", "분석 결과 있음", "위험 후보 " + ", ".join(r["id"] for r in urisks),
                "실제 오류·공격 입력이 들어오는지")
        else:
            chk("보안·안전 규칙, Taint", "실행 — 위반 미탐지", "이번 분석 범위에서 해당 규칙의 위반 미탐지", "규칙에 없는 결함")
        if urisks:
            done = [r for r in urisks if r.get("symbolic")]
            notdone = [r for r in urisks if not r.get("symbolic")]
            if done:
                chk("기호 실행", "실행", "; ".join(f"{r['id']}: " + ("반례 발견" if r.get("state") == "violated" else
                                                                "위반 없음(속성 성립)" if r.get("state") == "holds" else "판정 불가")
                                                 for r in done), "분석 모델 밖의 실제 동작")
            if notdone:
                chk("기호 실행", "미실행", ", ".join(r["id"] for r in notdone) + " 의 기호 실행 기록 없음", "위반 입력이나 경로 존재 여부")
        else:
            chk("기호 실행", "해당 없음", "대상 위험 연산 없음", "-")
        chk("실제 실행 비교 (SIL·자동 탐색)", "실행" if sil_ran else "결과 미첨부",
            "SIL·자동 탐색 결과는 근거 상세 참고" if sil_ran else "이 report 에서 확인할 실행 증거 없음", "변경 전후 실제 동작 차이")

        # ③ 발견 사항: 위험 후보 + 검토 항목의 위험 후보를 같은 문장끼리 묶는다
        groups: dict = {}
        for r in urisks:
            key = r["line"]
            groups.setdefault(key, {"risks": [], "review": []})["risks"].append(r)
        for f in rv:
            if f["class"] not in ("위험 후보 탐지", "위반 경로 확인", "분석 불충분") or f["summary"].startswith("["):
                continue
            key = f["line"]
            hit = next((k for k in groups if k == key), None)
            groups.setdefault(hit if hit is not None else key, {"risks": [], "review": []})["review"].append(f)
        fids = []
        for key, grp in sorted(groups.items(), key=lambda kv: kv[0] or 0):
            r = grp["risks"][0] if grp["risks"] else None
            gt = next((gt for gt in A["gating"] if r is not None and any(x.n.line == r["line"] for x in gt["moved"] + gt["added"])), None)
            if gt is None and A["gating"] and r is not None and (r.get("change") or {}).get("kind") == "moved":
                gt = A["gating"][0]
            if r is not None:
                fid = r["id"]
            else:
                fnum += 1
                fid = f"F{fnum}"
            fkind = "gating" if gt is not None else ("threshold" if r is not None and (r.get("change") or {}).get("kind") == "changed"
                                                    else "removed" if r is not None and (r.get("change") or {}).get("kind") == "removed"
                                                    else "risk" if r is not None else "review")
            if gt is not None:
                title = f"새 {'플래그 ' if gt['flag'] else ''}조건에서 {gt['block']} 정책 확인 필요"
                fact = (f"`{cond_expr(r['expr'])}` 검사가 다른 상위 조건(`{gt['expr']}`) 아래로 이동함" if r is not None
                        else f"`{gt['expr']}` 조건이 기존 처리를 감쌈")
                situation = f"`{gt['expr']}` 조건이 거짓이어서 해당 블록이 실행되지 않을 때, 필요한 {_proc(gt['block'])}가 다른 경로에서 보장되지 않는 경우"
                remaining = "이 조건에서 처리를 생략하는 것이 요구사항에 맞는지"
            elif r is not None and (r.get("change") or {}).get("kind") == "changed" and r["kind"] == "check":
                ch = r["change"]
                title = f"{_topic(u['type_label'])} 판정 기준 변경 확인 필요"
                fact = f"`{cond_expr(ch.get('before', ''))}` → `{cond_expr(ch.get('after', ''))}`"
                situation = "경계값 근처 입력에서 분기 결과가 바뀌어 처리 여부가 의도와 다르게 되는 경우"
                remaining = "새 기준값이 요구사항과 일치하는지"
            elif r is not None and (r.get("change") or {}).get("kind") == "removed":
                title = f"{_topic(u['type_label'])} 검사 삭제로 보호 동작이 빠지지 않는지 확인 필요"
                fact = f"`{_short((r.get('change') or {}).get('before') or r['expr'], 60)}` 삭제"
                situation = "삭제된 검사가 막던 입력·상태가 그대로 처리되는 경우"
                remaining = "검사 삭제가 의도된 것인지, 다른 위치에서 같은 검사를 하는지"
            elif r is not None:
                title = f"{_topic(u['type_label'])}: `{_short(r['expr'], 50)}` 동작 확인 필요"
                fact = (r.get("change") or {}).get("detail") or r.get("evidence") or r.get("how", "")
                situation = re.sub(r"\s*\(요구사항 확인 필요\)", "", r.get("risk", "")) or "변경된 연산이 의도와 다른 값을 만드는 경우"
                remaining = r.get("verification") or "요구사항에 맞는 동작인지"
            else:
                f0 = grp["review"][0]
                title = _short(re.sub(r"`", "", f0["summary"]), 90) + " — 확인 필요"
                fact = f0["summary"]
                situation = f"검토 항목 {f0['item']} 이 지적한 상태가 실제 실행에서도 생기는 경우"
                remaining = f0.get("reviewer") or "요구사항에 맞는 동작인지"
            core = imp["core"][0]["stages"] if imp["core"] and fkind != "review" else []
            if core:
                st0 = core[0]["basis"]
                rest = [s for s in core[1:]]
                eff = (st0.split(" — ")[-1] if " — " in st0 else st0) + (
                    " 및 " + ", ".join(f"{s['func']}()" + ("의 외부 출력" if s["stage"] == "외부 출력 후보" else "") for s in rest)
                    + "에 영향 가능" if rest else "")
            else:
                eff = "변경 함수 내부 동작에 영향 가능" + (f" ({', '.join(e.replace('`', '') for e in A['effects'][:3])})" if A["effects"] else "")
            judgment = _judge_risk(r) if r is not None else (
                "분석 모델 내 위반 확인" if any(f["class"] == "위반 경로 확인" for f in grp["review"]) else
                "분석 불충분" if all(f["class"] == "분석 불충분" for f in grp["review"]) else "위험 후보")
            sources = [{"id": x["id"], "rule": x.get("kind_label", ""), "category": x.get("category", ""), "risk": x.get("risk", ""),
                        "how": x.get("how", ""), "verification_level": x.get("verification_level", ""),
                        "evidence": x.get("evidence", ""), "expr": x.get("expr", ""), "line": x.get("line"),
                        "status": x.get("status", ""), "baseline": (x.get("baseline") or {}).get("old_state_label", ""),
                        "symbolic": x.get("symbolic"), "taint": bool((x.get("taint") or {}).get("flow_detected")),
                        "bounds": (x.get("bounds") or {}).get("change", "")} for x in grp["risks"]]
            findings.append({
                "id": fid, "change": cid, "title": title, "fact": fact, "situation": situation, "impact": eff,
                "judgment": judgment, "comparison": _cmp_status(r.get("status_code")) if r is not None else "판정 보류",
                "evidence_level": _evidence_level(r, imp, sil_ran), "remaining": remaining,
                "merged": [x["id"] for x in grp["risks"][1:]] + [f"검토 {f['item']}" for f in grp["review"]],
                "sources": sources, "review": [{"item": f["item"], "class": f["class"], "summary": f["summary"],
                                                "reviewer": f.get("reviewer", "")} for f in grp["review"]],
                "line": key, "tests": [], "_kind": fkind, "_items": [f["item"] for f in grp["review"]],
            })
            fids.append(fid)
        fk = {f["id"]: f for f in findings if f["change"] == cid}

        def linked(kinds=(), items=()):
            return [i for i, f in fk.items() if f["_kind"] in kinds or set(f["_items"]) & set(items)]

        # ④ 추가 검증
        tl = []

        def test(title, cond, observe, criteria, links, kind="시험"):
            t = {"id": f"T{cid[1:]}-{len(tl) + 1}", "change": cid, "findings": links, "kind": kind, "title": title,
                 "condition": cond, "observe": observe, "criteria": criteria}
            tl.append(t)
            return t

        for gt in A["gating"]:
            gv = _boundary(gt["expr"])
            inner = [x for x in gt["moved"] if x.n.kind == "cond"]
            ib = _boundary(cond_expr(inner[0].n.text)) if inner else None
            cond = " × ".join(f"`{v}` {'/'.join(vals)}" for v, vals in [gv, ib] if v) if (gv or ib) else f"`{gt['expr']}` 참/거짓"
            obs = ", ".join(e for e in gt["effects"][:4]) or "블록 안 처리 실행 여부"
            test(f"{'플래그' if gt['flag'] else '조건'}별 {_proc(gt['block'])}", cond, obs,
                 f"승인된 {gt['block']} 정책과 일치 — {NO_REQ}", linked(("gating",)))
            var = gt.get("var")
            if var:
                vals = _var_values(new, var)
                if len(vals) >= 2:
                    seq = f"{vals[0]} → {vals[1]} → {vals[0]}"
                    back = any(v == vals[0] for v in vals[1:]) or len(vals) > 2
                    note = "" if back else f" (다시 {vals[0]} 으로 바뀌는 코드는 이번 분석 범위에서 찾지 못함 — 의도 확인)"
                    test(f"{'플래그' if gt['flag'] else var} 전이 후 처리", f"`{var}` {seq} 전이를 포함한 연속 실행{note}",
                         "처리 생략·재개 시점, 상태 잔류", f"필요한 처리가 누락되거나 중복되지 않음 — {NO_REQ}", linked(("gating",)))
        for x in A["changed_conds"]:
            b1 = _boundary(cond_expr(x.o.text))
            b2 = _boundary(cond_expr(x.n.text))
            vals = sorted({int(v) for b in (b1, b2) if b for v in b[1] if v.isdigit()})
            var = (b2 or b1 or ("", []))[0]
            cond = f"`{var}` {'/'.join(map(str, vals))}" if var and vals else f"`{cond_expr(x.n.text)}` 경계값"
            test("판정 경계 시험", cond, "분기 결과와 그 아래 상태 갱신", f"새 기준값이 요구사항과 일치 — {NO_REQ}",
                 linked(("threshold", "risk")))
        for x in A["removed"] + A["removed_conds"]:
            what = cond_expr(x.n.text) if x.n.kind == "cond" else _short(x.n.text, 50)
            test("삭제된 처리의 대체 경로 확인", f"변경 전 `{what}` 가 실행되던 입력·상태",
                 f"`{what}` 가 하던 처리가 다른 경로에서 수행되는지", f"처리 누락이 의도된 것 — {NO_REQ}", linked(("removed",)))
            if len(tl) > 6:
                break
        if imp["outputs"]:
            core = imp["core"][0]["stages"] if imp["core"] else []
            state = next((re.findall(r"`([^`]+)`", s["basis"]) for s in core if s["stage"] == "변경 발생"), [])
            field_ = next((re.findall(r"`([^`]+)`", s["basis"]) for s in core if s["stage"] == "상태 판단"), [])
            sv = state[-1] if state else "변경된 상태"
            fv = field_[-1] if field_ else "출력 값"
            test("외부 응답 확인", f"`{sv}` 설정·해제 조건", f"`{fv}` 값과 `{imp['outputs'][0]}()` 출력 시점",
                 f"메시지 사양과 일치 — {NO_REQ}", linked(("gating", "threshold", "removed", "risk")))
        for r in urisks:
            if r.get("verification") and not A["gating"]:
                test(f"{r['id']} 재현 시험", (r.get("bounds") or {}).get("change") or f"`{_short(r['expr'], 50)}` 경계 조건",
                     "해당 연산·분기의 결과", f"{_short(r['verification'], 120)} — {NO_REQ}", [r["id"]])
        writes = sorted({w for x in g if x.version == "new" for w in _writes(new, x.n)})
        test("변경 전후 회귀 비교", "같은 입력·초기 상태·빌드 설정",
             "내부 상태" + (f"({', '.join('`' + w + '`' for w in writes[:4])})" if writes else "") + "와 외부 출력"
             + (f"({', '.join('`' + e + '()`' for e in imp['external'][:2])})" if imp["external"] else "") + "의 차이",
             "승인된 동작 변경 범위와 일치 — 변경 의도 확인 필요", fids)
        # 판단 질문 (검토 항목에서 온 것)
        for q in u.get("checklist", []):
            if q["item"].startswith("V") or q["item"] == "-":
                continue
            test(q["area"], f"자동 검토 {q['item']} 지적: {q['basis']}", q["question"],
                 "요구사항·설계 근거와 대조해 판단 (근거를 판정 근거에 기록)", linked((), (q["item"],)), kind="판단")
        for f in findings:
            if f["change"] == cid:
                f["tests"] = [t["id"] for t in tl if f["id"] in t["findings"]]
        tests += tl

        js = [f["judgment"] for f in findings if f["change"] == cid]
        judgment = min(js, key=JUDGE.index) if js else "변경 사실 확인"
        cmp_ = [f["comparison"] for f in findings if f["change"] == cid]
        comparison = "신규" if "신규" in cmp_ else ("판정 보류" if "판정 보류" in cmp_ else (cmp_[0] if cmp_ else "-"))
        if judgment in ("실행 재현 확인", "분석 모델 내 위반 확인") or (judgment == "위험 후보" and (comparison == "신규" or imp["outputs"])):
            prio = "높음"
        elif judgment == "위험 후보" or imp["n_cross"] or flags:
            prio = "중간"
        else:
            prio = "낮음"
        changes.append({
            "id": cid, "unit": u["id"], "title": A["title"], "sentences": A["sentences"], "func": u["func"], "file": u["file"],
            "lo": u["lo"], "hi": u["hi"], "old_lo": min(old_lines) if old_lines else None, "old_hi": max(old_lines) if old_lines else None,
            "intent": "요구사항 확인 필요 (변경 이유·요구사항이 첨부되지 않음)", "type_label": u["type_label"],
            "compare": A["rows"], "compare_more": A["more_rows"], "impact": imp, "checks": checks,
            "findings": [f["id"] for f in findings if f["change"] == cid], "tests": [t["id"] for t in tl],
            "judgment": judgment, "comparison": comparison, "priority": prio,
            "review_flags": [{"item": f["item"], "class": f["class"], "summary": f["summary"]} for f in flags],
            "phrases": [c["phrase"] for c in u["changes"]], "merged_globals": ctl_by_unit.get(u["id"], []),
        })
    changes.sort(key=lambda c: (PRIO.index(c["priority"]), -len(c["findings"]), -c["impact"]["n_cross"], c["file"], c["lo"]))
    # 정렬 뒤 번호를 다시 매긴다 (C01 이 가장 먼저 볼 변경)
    remap = {c["id"]: f"C{i + 1:02d}" for i, c in enumerate(changes)}
    for c in changes:
        old_id = c["id"]
        c["id"] = remap[old_id]
        for k in c["checks"]:
            k["id"] = k["id"].replace(old_id + "-", c["id"] + "-")
    tmap = {}
    for t in tests:
        nid = remap[t["change"]]
        new_tid = f"T{nid[1:]}-{t['id'].split('-')[1]}"
        tmap[t["id"]] = new_tid
    for t in tests:
        t["id"] = tmap[t["id"]]
        t["change"] = remap[t["change"]]
    for c in changes:
        c["tests"] = [tmap[t] for t in c["tests"]]
    for f in findings:
        f["change"] = remap[f["change"]]
        f["tests"] = [tmap[t] for t in f["tests"]]
    findings.sort(key=lambda f: ([c["id"] for c in changes].index(f["change"]), JUDGE.index(f["judgment"])))
    for f in findings:
        f.pop("_kind", None)
        f.pop("_items", None)
    tests.sort(key=lambda t: (int(t["id"][1:].split("-")[0]), int(t["id"].split("-")[1])))
    return {"overview": _overview(ctx, changes, findings, tests, analysis, review), "changes": changes,
            "findings": findings, "tests": tests}


def _overview(ctx, changes, findings, tests, analysis, review) -> dict:
    fc = ctx.get("file_changes") or {}
    tools = ((analysis.get("security") or {}).get("tools") or {}) if analysis else {}
    risks = analysis.get("risks", []) if analysis else []
    pending = []
    j, cq = tools.get("joern") or {}, tools.get("codeql") or {}
    if j or cq:
        ran = [n for n, t in (("Joern", j), ("CodeQL", cq)) if t.get("ran")]
        if ran:
            pending.append({"item": "Joern·CodeQL", "status": f"{'·'.join(ran)} 실행", "done": True})
        else:
            pending.append({"item": "Joern·CodeQL", "status": "스크립트·질의 생성, 실행하지 않음", "done": False})
    k = tools.get("klee")
    if k is not None:
        pending.append({"item": "KLEE", "status": f"하네스 {k.get('harnesses', 0)}개 생성, 실행 {k.get('ran', 0)}개",
                        "done": bool(k.get("harnesses")) and k.get("ran", 0) >= k.get("harnesses", 0)})
    sil, ex = ctx.get("sil"), ctx.get("explore")
    if sil or ex:
        parts = []
        if sil:
            parts.append(f"SIL 케이스 {sil['cases']}개 (차이 {sil['diff_cases']}개)")
        if ex:
            parts.append(f"자동 탐색 케이스 {ex['cases']}개 (차이 {ex['diff_cases']}개)")
        pending.append({"item": "SIL·자동 탐색", "status": ", ".join(parts), "done": True})
    else:
        pending.append({"item": "SIL·자동 탐색", "status": "결과 미첨부 — 실제 실행 증거 없음", "done": False})
    if risks:
        nd = sum(1 for r in risks if r.get("symbolic"))
        pending.append({"item": "기호 실행", "status": f"위험 후보 {len(risks)}개 중 {nd}개 실행, {len(risks) - nd}개 미실행",
                        "done": nd == len(risks)})
    pe = (analysis.get("excluded") or {}).get("parse_errors") or {} if analysis else {}
    if pe:
        pending.append({"item": "파싱 실패", "status": f"{len(pe)}개 파일 분석 제외 (근거 상세 › 분석 설정)", "done": False})
    pending.append({"item": "요구사항·변경 의도", "status": "미첨부 — 판정 기준의 기대값 미확정", "done": False})
    flags = [f for f in (review or {}).get("findings", []) if f["class"] != "변경 사실 확인"]
    scope = ctx.get("scope") or ""
    return {
        "versions": ctx.get("versions"), "time": ctx.get("time"),
        "files": {k2: len(fc.get(k2, [])) for k2 in ("modified", "added", "removed")},
        "c_files": {"modified": sum(1 for f in fc.get("modified", []) if f.endswith(".c")),
                    "headers": sum(1 for f in fc.get("modified", []) if f.endswith(".h"))},
        "analyzed": ctx.get("analyzed") or {}, "scope": scope, "scope_reason": ctx.get("scope_reason") or [],
        "major": [c["id"] for c in changes if c["priority"] != "낮음"][:5] or [c["id"] for c in changes[:3]],
        "pending": pending,
        "counts": {"changes": len(changes), "findings": len(findings), "review_flags": len(flags),
                   "impact_paths": len(analysis.get("items", [])) if analysis else 0,
                   "tests": sum(1 for t in tests if t["kind"] == "시험"), "questions": sum(1 for t in tests if t["kind"] == "판단"),
                   "behavior_changes": sum(1 for c in changes if any(r.get("behavior") for r in c["compare"]))},
    }


# ---------------------------------------------------------------------------
# 코드 차이 (변경 전후 나란히)
# ---------------------------------------------------------------------------


def side_by_side(old_lines: list[str], new_lines: list[str], new_lo: int, new_hi: int,
                 old_lo: int | None = None, old_hi: int | None = None, ctx: int = 3, limit: int = 400) -> list[list]:
    """[[구분, 변경 전 줄번호, 변경 전 내용, 변경 후 줄번호, 변경 후 내용]] — 구분: same/ind(들여쓰기만 다름)/del/add/chg/gap."""
    sm = difflib.SequenceMatcher(None, [x.rstrip() for x in old_lines], [x.rstrip() for x in new_lines], autojunk=False)
    rows: list[list] = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            for k in range(i2 - i1):
                rows.append(["same", i1 + k + 1, old_lines[i1 + k], j1 + k + 1, new_lines[j1 + k]])
        else:
            # 바뀐 구간 안에서는 들여쓰기만 달라진 같은 줄(다른 조건 아래로 이동)을 짝지어 나란히 놓는다
            a = [x.strip() for x in old_lines[i1:i2]]
            b = [x.strip() for x in new_lines[j1:j2]]
            for t2, a1, a2, b1, b2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
                if t2 == "equal":
                    for k in range(a2 - a1):
                        rows.append(["ind", i1 + a1 + k + 1, old_lines[i1 + a1 + k], j1 + b1 + k + 1, new_lines[j1 + b1 + k]])
                    continue
                n = max(a2 - a1, b2 - b1)
                for k in range(n):
                    o = i1 + a1 + k if a1 + k < a2 else None
                    nn = j1 + b1 + k if b1 + k < b2 else None
                    kind = "chg" if o is not None and nn is not None else ("del" if o is not None else "add")
                    rows.append([kind, o + 1 if o is not None else "", old_lines[o] if o is not None else "",
                                 nn + 1 if nn is not None else "", new_lines[nn] if nn is not None else ""])
    lo, hi = max(1, new_lo - ctx), new_hi + ctx
    olo = (old_lo - ctx) if old_lo else None
    ohi = (old_hi + ctx) if old_hi else None

    def keep(r):
        if r[3] != "" and lo <= r[3] <= hi:
            return True
        return r[1] != "" and olo is not None and olo <= r[1] <= ohi
    out, last = [], None
    for i, r in enumerate(rows):
        if keep(r):
            if last is not None and i != last + 1:
                out.append(["gap", "", "", "", ""])
            out.append(r)
            last = i
        if len(out) >= limit:
            break
    return [[k, a, b.rstrip("\n"), c, d.rstrip("\n")] for k, a, b, c, d in out]
