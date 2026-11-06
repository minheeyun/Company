"""변경점 검토 체크리스트: 변경사항·기능 로직, 초기화·오류 처리, 데이터·수치, 인터페이스, 실행 순서·동시성, 설정·컴파일 조건.

검토 항목마다 자동 분석으로 확인한 사실과, 검증자가 추가로 판단해야 할 내용을 함께 남긴다.
항목의 결과는 적합/부적합이 아니라 다음 다섯 가지로 구분한다.

  변경 사실 확인    코드 구조·조건·의존성의 변경이 확인됨
  위험 후보 탐지    정적 분석으로 잠재적인 취약·결함 경로가 식별됨
  위반 경로 확인    정의한 안전 속성을 위반하는 실행 경로 또는 반례가 확인됨
  검증자 확인 필요  요구사항·환경·시험 근거가 있어야 적합 여부를 판단할 수 있음
  분석 불충분      모델링 누락·도구 한계 등으로 판정할 수 없음

입력은 변경 전후 Program(전처리 후 AST·CFG·의존성), ChangeSet, 영향 분석·보안 분석 결과, 의존성 비교 결과,
그리고 변경 파일의 원문(#define·#if 같은 전처리 지시문은 AST 에 남지 않으므로 원문에서 본다)이다.
"""

from __future__ import annotations

import re
from collections import defaultdict
from pathlib import Path

from pycparser import c_ast

from .cexpr import const_eval, gen, norm
from .diff import predicate, fmt_set
from .program import Program, display_path

# --- 결과 분류 ---
CHANGE, CANDIDATE, VIOLATION, REVIEW, INSUFFICIENT = (
    "변경 사실 확인", "위험 후보 탐지", "위반 경로 확인", "검증자 확인 필요", "분석 불충분")
CLASSES = [VIOLATION, CANDIDATE, REVIEW, INSUFFICIENT, CHANGE]
CLASS_DESC = {
    CHANGE: "코드 구조·조건·의존성의 변경이 확인됨",
    CANDIDATE: "정적 분석으로 잠재적인 취약·결함 경로가 식별됨",
    VIOLATION: "정의한 안전 속성을 위반하는 실행 경로 또는 반례가 확인됨",
    REVIEW: "요구사항·환경·시험 근거가 있어야 적합 여부를 판단할 수 있음",
    INSUFFICIENT: "모델링 누락·도구 한계 등으로 판정할 수 없음",
}
NONE_FOUND = "해당 변경 없음"

# --- 검토 항목 정의: (번호, 영역, 자동 분석으로 확인할 내용, 검증자가 추가로 판단할 내용, 적용 알고리즘) ---
GROUPS = [
    ("1", "변경사항 및 기능 로직", "AST Diff · CFG 비교 · CPG · Control Dependence · Program Slicing"),
    ("2", "초기화·오류 처리·복구", "CFG · 데이터 흐름 분석 · Symbolic Execution · 상태 전이 분석"),
    ("3", "데이터·파라미터·수치 연산", "AST · Constant Propagation · Range Analysis · Symbolic Execution"),
    ("4", "인터페이스 및 의존성", "CPG · Call Graph · Def-Use Chain · Interprocedural Dataflow · Taint Analysis"),
    ("5", "실행 순서·타이머·동시성", "CFG · CPG · Control/Data Dependence · 정적 동시성 분석"),
    ("6", "설정·컴파일 조건", "Preprocessor · AST Diff · CFG 비교 · 빌드 설정별 분석"),
]
ITEMS = [
    ("1-2", "1", "함수·조건문·변수·상수·호출의 추가·수정·삭제", "변경사항 누락 여부"),
    ("1-3", "1", "변경 전후 조건식·연산·대입·호출의 의미적 차이 후보", "의도한 동작 변경인지"),
    ("3-1", "1", "기존에 가능했던 실행 경로의 추가·삭제·변경", "기존 기능을 보존해야 하는지"),
    ("3-2", "1", "조건 평가 순서·우선순위·분기 경로 및 조기 종료 변화", "기존 처리가 의도적으로 생략되었는지"),
    ("3-3", "1", "상태 변수의 대입·전이 조건·도달 불가능 경로·전이 충돌 후보", "상태 전이 요구사항을 만족하는지"),
    ("3-4", "1", "동일 조건·함수를 사용하는 다른 위치와 수정 누락 후보", "모든 사용 위치에 동일 변경이 필요한지"),
    ("3-5", "1", "삭제·이동·대체된 코드와 해당 코드에 의존하던 경로", "삭제된 기능이 여전히 필요한지"),
    ("4-1", "2", "초기값 변경, 초기화 누락 후보, 초기화 전 사용 경로", "실제 실행 순서에서 초기화가 보장되는지"),
    ("4-2", "2", "오류 처리·리셋·복구 함수의 호출 및 실행 조건 변화", "변경된 오류 반응이 적절한지"),
    ("4-3", "2", "재시도·실패 카운터의 증감·초기화·임계값 변경", "재시도 횟수와 복구 조건이 적절한지"),
    ("4-4", "2", "복수 오류 조건의 분기 우선순위와 동시 성립 가능성", "우선순위가 요구된 오류 처리 정책과 일치하는지"),
    ("5-1", "3", "경계 비교 연산자, 임계값, 배열 인덱스 범위 및 위험 경로", "변경된 경계값이 요구사항과 일치하는지"),
    ("5-2", "3", "자료형·형변환·연산 순서 변경, 오버플로·언더플로 가능성", "계산 방식과 반올림 정책이 적절한지"),
    ("5-3", "3", "변경된 파라미터의 모든 참조 위치와 영향을 받는 분기", "적용 대상과 변경 범위가 적절한지"),
    ("6-1", "4", "함수 호출, 전역 변수, 데이터 읽기·쓰기 의존성의 추가·삭제", "변화가 연관 기능에 미치는 실제 영향"),
    ("6-2", "4", "함수 인자·반환형·구조체·enum·매크로 정의의 변경 및 코드상 불일치", "인터페이스 사양과 일치하는지"),
    ("6-3", "4", "데이터의 정의·대입·전달·사용 경로와 초기화 전 사용 후보", "소비 시점에 유효한 데이터가 보장되는지"),
    ("6-4", "4", "동일한 값을 사용하는 복수 처리 경로와 조건·연산 차이", "경로 간 동일 동작이 요구되는지"),
    ("6-5", "4", "외부 함수·라이브러리·드라이버·헤더에 대한 의존성 변화", "외부 구성요소의 실제 동작에 문제가 없는지"),
    ("7-1", "5", "함수 호출 순서·상태 갱신·반복·종료 조건의 변경", "요구된 처리 시퀀스와 일치하는지"),
    ("7-2", "5", "타이머 시작·리셋·증감·만료 조건 및 기준값 변경", "실제 주기·시간 요구사항을 만족하는지"),
    ("7-3", "5", "공유 변수 접근, ISR·태스크 간 데이터 의존성, 경합 가능성 후보", "실제 실행 환경에서 경합이 발생하는지"),
    ("8-1", "6", "#define, #if, #ifdef의 변경 및 빌드별 활성화 상태", "실제 제품 설정과 일치하는지"),
    ("8-2", "6", "기능 스위치 제거·변경에 따른 컴파일 경로와 적용 범위 차이", "새로 활성화된 기능이 의도한 것인지"),
    ("8-4", "6", "조건부 컴파일에 따라 달라지는 초기화·실행·오류 처리 경로", "경로별 동작이 요구된 설정 정책과 일치하는지"),
]

# --- 이름 패턴 (규칙 파일의 review 절로 바꿀 수 있음) ---
DEFAULT_PATTERNS = {
    "error_call": r"(Det_Report|Dem_|ReportError|SetEventStatus|ErrorStatus|Reset|Recover|Error|Fault|Abort|Shutdown|"
                  r"SafeState|FailSafe|Failsafe|Mcu_PerformReset|WdgM_|Fail)",
    "counter": r"(cnt|count|counter|retry|fail|debounce|attempt)",
    "timer": r"(timer|tmr|tick|_tm\b|_tm[A-Z_\[]|time|delay|timeout|period)",
    "init_func": r"(_Init\b|Init$|_init\b|Initialize|Startup)",
    "critical": r"(SuspendAllInterrupts|SuspendOSInterrupts|DisableAllInterrupts|SchM_Enter|EnterCritical|"
                r"Os_Enter|GetResource|osEnterCritical|__disable_irq)",
    "feature_switch": r"(_ENABLE|_ENABLED|_SUPPORT|_USED|_API\b|_ACTIVE|FEATURE|_USE_|DEV_ERROR_DETECT|_ON\b|_OFF\b)",
    # 변수 역할 (변경 제목·설명·검증 항목에 쓰임)
    "error_state": r"(err|fault|fail|dtc|diag|abnormal)",
    "flag": r"(flag|flg|_f\b|enable|ena\b|req\b|lock)",
}


# ---------------------------------------------------------------------------
# 결과 모음
# ---------------------------------------------------------------------------


class _Collector:
    def __init__(self):
        self.findings: list[dict] = []
        self.notes: dict[str, list[str]] = defaultdict(list)  # 항목별 '무엇을 검토했는지'
        self.seen: set = set()

    def add(self, item: str, cls: str, summary: str, *, func: str = "", file: str = "", line: int = 0,
            evidence: list[str] | None = None, reviewer: str = "", refs: list[str] | None = None):
        key = (item, cls, func, file, line, summary)
        if key in self.seen:
            return
        self.seen.add(key)
        self.findings.append({"item": item, "class": cls, "summary": summary, "func": func, "file": file,
                              "line": line, "evidence": evidence or [], "reviewer": reviewer, "refs": refs or []})

    def note(self, item: str, text: str):
        if text not in self.notes[item]:
            self.notes[item].append(text)


# ---------------------------------------------------------------------------
# 공통 도구
# ---------------------------------------------------------------------------


def _rel(prog: Program, path: str) -> str:
    try:
        return Path(path).resolve().relative_to(prog.root.resolve()).as_posix()
    except (ValueError, OSError):
        return path or ""


def _expr(n):
    e = n.ast
    if n.kind == "stmt" and isinstance(e, c_ast.Decl):
        return e.init
    return e


def _has_call(e) -> bool:
    for x in _iter(e):
        if isinstance(x, c_ast.FuncCall):
            return True
    return False


def _iter(e):
    stack = [e]
    while stack:
        x = stack.pop()
        if x is None:
            continue
        yield x
        stack.extend(c for _, c in x.children())


def _leaves(e) -> list[str]:
    out = []

    def walk(x):
        if x is None:
            return
        if isinstance(x, (c_ast.ID, c_ast.Constant)):
            out.append(norm(x))
            return
        if isinstance(x, c_ast.FuncCall):
            out.append(norm(x.name))
        for _, c in x.children():
            walk(c)
    walk(e)
    return out


def _ops(e) -> list[str]:
    """식의 이항 연산자를 트리 구조 순서(전위)로."""
    out = []
    for x in _iter(e):
        if isinstance(x, c_ast.BinaryOp):
            out.append(x.op)
    return out


def expr_diff(a, b, depth: int = 0) -> list[str]:
    """두 식의 의미적 차이 설명 (연산자·상수·변수·호출·형변환·대입 대상·평가 순서)."""
    if a is None or b is None or depth > 12:
        return [] if a is b else [f"`{gen(a) if a is not None else '(없음)'}` → `{gen(b) if b is not None else '(없음)'}`"]
    if norm(a) == norm(b):
        return []
    if isinstance(a, c_ast.Cast) != isinstance(b, c_ast.Cast):
        if isinstance(a, c_ast.Cast) and norm(a.expr) == norm(b):
            return [f"형변환 ({gen(a.to_type)}) 제거"]
        if isinstance(b, c_ast.Cast) and norm(b.expr) == norm(a):
            return [f"형변환 ({gen(b.to_type)}) 추가"]
    if type(a) is not type(b):
        if _leaves(a) == _leaves(b):
            return [f"연산 순서·괄호 변경: `{gen(a)}` → `{gen(b)}`"]
        return [f"`{_short(gen(a))}` → `{_short(gen(b))}`"]
    out: list[str] = []
    if isinstance(a, c_ast.Constant):
        return [f"상수 {a.value} → {b.value}"]
    if isinstance(a, c_ast.ID):
        return [f"변수 {a.name} → {b.name}"]
    if isinstance(a, c_ast.BinaryOp):
        if _leaves(a) == _leaves(b) and sorted(_ops(a)) == sorted(_ops(b)) and _ops(a) != _ops(b) or \
                (_leaves(a) == _leaves(b) and _ops(a) == _ops(b) and a.op == b.op and
                 (norm(a.left) != norm(b.left) or norm(a.right) != norm(b.right)) and a.op not in ("&&", "||")):
            return [f"연산 순서·괄호 변경: `{_short(gen(a))}` → `{_short(gen(b))}`"]
        if a.op in ("&&", "||") and a.op == b.op and {norm(a.left), norm(a.right)} == {norm(b.left), norm(b.right)}:
            return [f"`{a.op}` 피연산자 평가 순서 변경" + (" (호출 포함: 단락 평가로 실행 여부가 달라질 수 있음)"
                                                     if _has_call(a) else "")]
        if a.op != b.op:
            if norm(a.left) == norm(b.left) and norm(a.right) == norm(b.right):
                return [f"연산자 {a.op} → {b.op}"]
            out.append(f"연산자 {a.op} → {b.op}")
        if _leaves(a) == _leaves(b) and (norm(a.left) != norm(b.left) or norm(a.right) != norm(b.right)) and a.op == b.op:
            return [f"연산 순서·괄호 변경: `{_short(gen(a))}` → `{_short(gen(b))}`"]
        return out + expr_diff(a.left, b.left, depth + 1) + expr_diff(a.right, b.right, depth + 1)
    if isinstance(a, c_ast.UnaryOp):
        if a.op != b.op:
            out.append(f"단항 연산자 {a.op} → {b.op}")
        return out + expr_diff(a.expr, b.expr, depth + 1)
    if isinstance(a, c_ast.Cast):
        if norm(a.to_type) != norm(b.to_type):
            out.append(f"형변환 ({gen(a.to_type)}) → ({gen(b.to_type)})")
        return out + expr_diff(a.expr, b.expr, depth + 1)
    if isinstance(a, c_ast.Assignment):
        if norm(a.lvalue) != norm(b.lvalue):
            out.append(f"대입 대상 {gen(a.lvalue)} → {gen(b.lvalue)}")
        if a.op != b.op:
            out.append(f"대입 연산자 {a.op} → {b.op}")
        return out + expr_diff(a.rvalue, b.rvalue, depth + 1)
    if isinstance(a, c_ast.FuncCall):
        if norm(a.name) != norm(b.name):
            out.append(f"호출 대상 {gen(a.name)}() → {gen(b.name)}()")
        aa = a.args.exprs if a.args else []
        ba = b.args.exprs if b.args else []
        if len(aa) != len(ba):
            out.append(f"인자 수 {len(aa)} → {len(ba)}")
        else:
            for i, (x, y) in enumerate(zip(aa, ba), 1):
                for d in expr_diff(x, y, depth + 1):
                    out.append(f"인자{i}: {d}")
        return out
    if isinstance(a, c_ast.StructRef):
        if a.field.name != b.field.name:
            out.append(f"필드 {a.field.name} → {b.field.name}")
        return out + expr_diff(a.name, b.name, depth + 1)
    if isinstance(a, c_ast.ArrayRef):
        return expr_diff(a.name, b.name, depth + 1) + [f"인덱스: {d}" for d in expr_diff(a.subscript, b.subscript, depth + 1)]
    if isinstance(a, c_ast.TernaryOp):
        return ([f"조건: {d}" for d in expr_diff(a.cond, b.cond, depth + 1)] + expr_diff(a.iftrue, b.iftrue, depth + 1)
                + expr_diff(a.iffalse, b.iffalse, depth + 1))
    return [f"`{_short(gen(a))}` → `{_short(gen(b))}`"]


def _short(s: str, n: int = 90) -> str:
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[: n - 1] + "…"


def _category(prog: Program, c) -> str:
    if c.kind in ("func_added", "func_removed"):
        return "함수"
    if c.kind in ("global_added", "global_removed", "global_changed", "type_changed"):
        return "변수"
    n = prog.nodes[c.node]
    if n.kind == "cond":
        return "조건문"
    if n.kind == "formal_in":
        return "함수 인자"
    if n.kind in ("call", "extcall", "actual_in", "actual_out"):
        return "호출"
    e = _expr(n)
    if n.kind == "stmt" and isinstance(n.ast, c_ast.Decl):
        return "변수"
    if e is not None and _has_call(e):
        return "호출"
    if n.kind == "return":
        return "반환"
    return "연산·대입"


def _persistent(prog: Program, key: str) -> bool:
    v = prog.vars.get(key)
    return bool(v and v.kind in ("global", "static", "static_local"))


def _var_name(prog: Program, key: str) -> str:
    v = prog.vars.get(key)
    return v.name if v else key


def _const_assign(prog: Program, n, enums):
    """상태 변수 대입: (변수 키, 상수 값) 또는 None."""
    e = n.ast
    if n.kind != "stmt" or not isinstance(e, c_ast.Assignment) or e.op != "=":
        return None
    v = const_eval(e.rvalue, enums)
    if v is None:
        return None
    try:
        ls = [p for p in prog.lvals(e.lvalue) if len(p) == 1]
    except Exception:
        return None
    if len(ls) != 1 or not _persistent(prog, ls[0][0]):
        return None
    return ls[0][0], v


def _compared_values(prog: Program, n, enums) -> list[tuple[str, int]]:
    """조건식에서 상수와 비교되는 영속 변수: [(변수 키, 상수)]. switch 는 case 값 전부."""
    out = []
    if n.kind != "cond" or n.ast is None:
        return out
    if n.sig.startswith("switch|") and n.cases:
        try:
            ls = [p for p in prog.lvals(n.ast) if len(p) == 1]
        except Exception:
            ls = []
        for p in ls:
            if _persistent(prog, p[0]):
                out += [(p[0], int(c)) for c in n.cases]
        return out
    for x in _iter(n.ast):
        if isinstance(x, c_ast.BinaryOp) and x.op in ("==", "!="):
            for var, cst in ((x.left, x.right), (x.right, x.left)):
                cv = const_eval(cst, enums)
                if cv is None:
                    continue
                try:
                    ls = [p for p in prog.lvals(var) if len(p) == 1]
                except Exception:
                    ls = []
                for p in ls:
                    if _persistent(prog, p[0]):
                        out.append((p[0], cv))
    return out


def _enums(prog: Program, n) -> dict:
    f = prog.funcs.get(n.func) if n.func else None
    return prog.enums.get(f.tu, {}) if f else {}


def _names_in(prog: Program, n) -> str:
    return " ".join(display_path(prog, p) for p in (n.defs | n.uses)) + " " + (n.text or "")


# ---------------------------------------------------------------------------
# 전처리 지시문 (원문)
# ---------------------------------------------------------------------------

_DIR = re.compile(r"^\s*#\s*(define|undef|if|ifdef|ifndef|elif|else|endif|include)\b(.*)$")


def _logical_lines(text: str):
    """줄 이어쓰기(\\)를 합친 (줄 번호, 내용)."""
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        start, cur = i + 1, lines[i]
        while cur.endswith("\\") and i + 1 < len(lines):
            i += 1
            cur = cur[:-1] + " " + lines[i]
        yield start, cur
        i += 1


def scan_directives(text: str) -> dict:
    defines: dict[str, str] = {}
    conds: list[str] = []
    includes: list[str] = []
    regions: list[tuple[int, int, str]] = []
    stack: list[tuple[int, str]] = []
    for ln, line in _logical_lines(re.sub(r"/\*.*?\*/", lambda m: " " * len(m.group(0).split("\n")[0]) + "\n" * m.group(0).count("\n"), text, flags=re.S)):
        line = re.sub(r"//.*$", "", line)
        m = _DIR.match(line)
        if not m:
            continue
        kind, rest = m.group(1), " ".join(m.group(2).split())
        if kind == "define":
            mm = re.match(r"([A-Za-z_]\w*(\([^)]*\))?)\s*(.*)$", rest)
            if mm:
                defines[mm.group(1)] = mm.group(3)
        elif kind == "include":
            includes.append(rest)
        elif kind in ("if", "ifdef", "ifndef"):
            label = f"#{kind} {rest}"
            conds.append(label)
            stack.append((ln, label))
        elif kind in ("elif", "else"):
            label = f"#{kind} {rest}".strip()
            if kind == "elif":
                conds.append(label)
            if stack:
                s, lab = stack.pop()
                regions.append((s, ln, lab))
                stack.append((ln, f"{lab} → {label}"))
        elif kind == "endif" and stack:
            s, lab = stack.pop()
            regions.append((s, ln, lab))
    return {"defines": defines, "conds": conds, "includes": includes, "regions": regions}


def _read(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


# ---------------------------------------------------------------------------
# 검토
# ---------------------------------------------------------------------------


def review(old: Program, new: Program, cs, result: dict, deps_diff: dict | None, file_changes: dict | None,
           build_defines: list[str] | None = None, patterns: dict | None = None) -> dict:
    """변경점 검토 체크리스트를 수행하고 항목별 결과를 돌려준다."""
    P = {k: re.compile(v, re.I) for k, v in {**DEFAULT_PATTERNS, **(patterns or {})}.items()}
    C = _Collector()

    def loc(prog, n):
        return prog.func_display(n.func or ""), _rel(prog, n.file), n.line

    changes = list(cs.changes)
    pairs = [c for c in changes if c.kind in ("changed", "moved", "global_changed") and c.version == "new"]
    new_nodes = {c.node for c in changes if c.version == "new"}
    changed_funcs_new = {c.func for c in changes if c.version == "new" and c.func in new.funcs}
    changed_funcs_old = {c.func for c in changes if c.version == "old" and c.func in old.funcs}

    # ---------------- 1-2 변경 항목 분류 ----------------
    cats = defaultdict(lambda: defaultdict(int))
    for c in changes:
        if c.kind in ("changed", "moved", "global_changed") and c.version == "old":
            continue
        prog = new if c.version == "new" else old
        cat = _category(prog, c)
        verb = {"changed": "수정", "moved": "이동", "added": "추가", "removed": "삭제", "func_added": "추가",
                "func_removed": "삭제", "global_added": "추가", "global_removed": "삭제", "global_changed": "수정",
                "type_changed": "수정"}.get(c.kind, c.kind)
        if c.kind == "changed" and c.other_node is not None:
            d = expr_diff(_expr(old.nodes[c.other_node]), _expr(new.nodes[c.node]))
            if d and all(x.startswith("상수") for x in d):
                cat = cat + "·상수" if cat == "조건문" else "상수"
        cats[cat][verb] += 1
        func = prog.func_display(c.func or "")
        C.add("1-2", CHANGE, f"{cat} {verb}: `{_short(c.text, 70)}`"
              if c.kind not in ("changed", "moved", "global_changed") else
              f"{cat} {verb}: `{_short(c.other_text, 50)}` → `{_short(c.text, 50)}`",
              func=func, file=_rel(prog, c.file), line=c.line, reviewer="변경 요청과 대조해 빠지거나 의도하지 않은 변경이 없는지 확인하세요.")
    for cat, d in cats.items():
        C.note("1-2", f"{cat}: " + ", ".join(f"{k} {v}" for k, v in d.items()))
    if file_changes:
        mod = [f for f in file_changes.get("modified", []) + file_changes.get("added", []) if f.endswith(".c")]
        with_changes = {_rel(new, c.file) for c in changes if c.version == "new"} | {_rel(old, c.file) for c in changes if c.version == "old"}
        quiet = [f for f in mod if f not in with_changes]
        if quiet:
            C.add("1-2", REVIEW, f"내용이 바뀐 .c 파일 {len(quiet)}개에서 코드 변경점이 나오지 않음 (주석·공백·비활성 #if 블록만 바뀌었을 수 있음)",
                  evidence=quiet[:10], reviewer="이 파일들의 변경이 현재 빌드 설정에서 비활성인 코드가 아닌지, 누락된 분석이 없는지 확인하세요.")
    C.note("1-2", f"변경 지점 {len(changes)}개를 함수·조건문·변수·상수·호출로 분류")

    # ---------------- 1-3 의미적 차이 ----------------
    for c in pairs:
        if c.kind != "changed" or c.other_node is None:
            continue
        o, n = old.nodes[c.other_node], new.nodes[c.node]
        d = expr_diff(_expr(o), _expr(n))
        if not d:
            continue
        f, file, line = loc(new, n)
        ev = d + ([c.detail] if c.detail else [])
        C.add("1-3", REVIEW, f"`{_short(o.text, 60)}` → `{_short(n.text, 60)}`: " + "; ".join(d[:3]), func=f, file=file, line=line,
              evidence=ev, reviewer="이 차이가 의도한 동작 변경인지 확인하세요" + (f" ({c.detail})" if c.detail else "") + ".")
    C.note("1-3", f"수정 문장 {sum(1 for c in pairs if c.kind == 'changed')}개의 식을 AST 로 비교 (연산자·상수·변수·호출·형변환·대입 대상·평가 순서)")

    # ---------------- 3-1 실행 경로 ----------------
    for c in changes:
        prog = new if c.version == "new" else old
        n = prog.nodes[c.node]
        if c.kind in ("added", "removed") and n.kind == "cond":
            f, file, line = loc(prog, n)
            cls = REVIEW if c.kind == "removed" else CHANGE
            C.add("3-1", cls, f"분기 {'추가' if c.kind == 'added' else '삭제'}: `{_short(n.text)}`", func=f, file=file, line=line,
                  reviewer=("삭제된 분기로 처리되던 경우가 이제 어떤 경로로 가는지, 기존 기능을 보존해야 하는지 확인하세요." if c.kind == "removed"
                            else "새 분기로 갈라지는 경우가 요구사항에 있는지 확인하세요."))
        elif c.kind == "moved" and c.version == "new":
            f, file, line = loc(prog, n)
            o = old.nodes[c.other_node] if c.other_node is not None else None
            same_ctl = o is not None and o.depth == n.depth
            C.add("3-1", REVIEW if not same_ctl else CHANGE,
                  f"문장 {'실행 순서 이동' if same_ctl else '다른 조건 아래로 이동'}: `{_short(n.text)}`", func=f, file=file, line=line,
                  reviewer=("앞뒤 문장과의 실행 순서가 바뀌어 같은 주기 안의 결과가 달라지지 않는지 확인하세요." if same_ctl else
                            "이동 전 조건에서 실행되던 경우에 이 문장이 더 이상 실행되지 않아도 되는지 확인하세요."))
        elif c.kind == "changed" and c.version == "new" and n.kind == "cond":
            f, file, line = loc(prog, n)
            C.add("3-1", CHANGE, f"분기 조건 변경: `{_short(c.other_text, 50)}` → `{_short(n.text, 50)}`", func=f, file=file, line=line,
                  evidence=[c.detail] if c.detail else [],
                  reviewer="판정이 달라지는 입력에서 이전 경로의 처리가 필요한지 확인하세요.")
    for fk in sorted(changed_funcs_new & set(old.funcs)):
        fo, fn = old.funcs[fk], new.funcs[fk]
        co = sum(1 for i in fo.nodes if old.nodes[i].kind == "cond" and not old.nodes[i].dead)
        cn = sum(1 for i in fn.nodes if new.nodes[i].kind == "cond" and not new.nodes[i].dead)
        ro, rn = len(fo.returns), len(fn.returns)
        if co != cn or ro != rn:
            C.note("3-1", f"{fn.name}: 분기 {co}→{cn}개, return {ro}→{rn}개")
    C.note("3-1", f"변경 함수 {len(changed_funcs_new | changed_funcs_old)}개의 분기·이동 문장을 CFG 기준으로 비교")

    # ---------------- 3-2 평가 순서·조기 종료 ----------------
    for c in pairs:
        if c.kind != "changed" or c.other_node is None:
            continue
        o, n = old.nodes[c.other_node], new.nodes[c.node]
        for d in expr_diff(_expr(o), _expr(n)):
            if "평가 순서" in d or "연산 순서" in d:
                f, file, line = loc(new, n)
                C.add("3-2", CANDIDATE if "호출 포함" in d else REVIEW, d, func=f, file=file, line=line,
                      evidence=[f"`{_short(o.text)}` → `{_short(n.text)}`"],
                      reviewer="평가 순서가 바뀌어 앞 조건이 거짓일 때 뒤 호출·연산이 생략되는지, 그 생략이 의도된 것인지 확인하세요.")
    for c in changes:
        prog = new if c.version == "new" else old
        n = prog.nodes[c.node]
        if n.kind == "return" and c.kind in ("added", "removed", "moved") and not (c.kind == "moved" and c.version == "old"):
            f, file, line = loc(prog, n)
            verb = {"added": "추가", "removed": "삭제", "moved": "위치 변경"}[c.kind]
            C.add("3-2", REVIEW, f"조기 종료(return) {verb}: `{_short(n.text)}`", func=f, file=file, line=line,
                  reviewer=("추가된 return 뒤의 처리가 그 경우에 의도적으로 생략되는지 확인하세요." if c.kind == "added"
                            else "삭제된 return 이후 처리가 이제 그 경우에도 실행되는데 의도한 것인지 확인하세요."))
    for fk in sorted(changed_funcs_new & set(old.funcs)):
        so = [norm(old.nodes[i].ast) for i in old.funcs[fk].nodes if old.nodes[i].kind == "cond" and old.nodes[i].sig.startswith("if|")]
        sn = [norm(new.nodes[i].ast) for i in new.funcs[fk].nodes if new.nodes[i].kind == "cond" and new.nodes[i].sig.startswith("if|")]
        common = [x for x in so if x in sn]
        if common and [x for x in sn if x in common] != common:
            fn = new.funcs[fk]
            C.add("3-2", CANDIDATE, f"{fn.name}: 같은 조건들의 판정 순서가 바뀜 (if/else if 우선순위 변경 후보)",
                  func=fn.name, file=_rel(new, fn.file), line=fn.line, evidence=[f"변경 전: {', '.join(common[:4])}"],
                  reviewer="조건이 동시에 성립할 때 어느 처리가 먼저 되어야 하는지 요구사항과 대조하세요.")
    C.note("3-2", "&&·|| 피연산자 순서, 괄호(연산 우선순위), if/else if 판정 순서, return 추가·삭제를 비교")

    # ---------------- 3-3 상태 변수 ----------------
    def state_model(prog):
        assigned, compared = defaultdict(set), defaultdict(set)
        for n in prog.nodes:
            if n.dead or not n.func:
                continue
            en = _enums(prog, n)
            a = _const_assign(prog, n, en)
            if a:
                assigned[a[0]].add(a[1])
            for k, v in _compared_values(prog, n, en):
                compared[k].add(v)
        return assigned, compared

    ao, co_ = state_model(old)
    an, cn_ = state_model(new)
    touched = set()
    for c in changes:
        prog = new if c.version == "new" else old
        for p in prog.nodes[c.node].defs | prog.nodes[c.node].uses:
            touched.add(_var_name(prog, p[0]))
    state_vars = {k for k in set(an) | set(cn_) if len(an.get(k, set()) | cn_.get(k, set())) >= 2}
    name_of_old = {_var_name(old, k): k for k in set(ao) | set(co_)}
    checked_states = 0
    for k in sorted(state_vars):
        name = _var_name(new, k)
        if name not in touched:
            continue
        checked_states += 1
        ok = name_of_old.get(name)
        a_new, c_new = an.get(k, set()), cn_.get(k, set())
        a_old, c_old = (ao.get(ok, set()), co_.get(ok, set())) if ok else (set(), set())
        v = new.vars.get(k)
        file = _rel(new, v.file) if v else ""
        if a_new != a_old:
            C.add("3-3", CHANGE, f"상태 변수 `{name}` 대입 값 {sorted(a_old)} → {sorted(a_new)}", func="(상태)", file=file,
                  reviewer="추가·삭제된 상태 값과 전이 조건이 상태 전이 요구사항(상태도)과 일치하는지 확인하세요.")
        unreach = sorted((c_new - a_new) - (c_old - a_old))
        if unreach and a_new:
            C.add("3-3", CANDIDATE, f"상태 변수 `{name}`: 비교는 하지만 어디서도 대입하지 않는 값 {unreach} (도달 불가능 상태 후보)",
                  func="(상태)", file=file, reviewer="해당 상태로 가는 전이가 빠졌는지, 다른 경로(초기값·외부 쓰기)로 설정되는지 확인하세요.")
        unhandled = sorted((a_new - c_new) - (a_old - c_old))
        if unhandled and c_new:
            C.add("3-3", CANDIDATE, f"상태 변수 `{name}`: 대입하지만 어디서도 판정하지 않는 값 {unhandled} (처리되지 않는 상태 후보)",
                  func="(상태)", file=file, reviewer="이 상태에서 해야 할 처리가 누락되지 않았는지 확인하세요.")
    for fk in sorted(changed_funcs_new):
        f = new.funcs[fk]
        groups = defaultdict(list)
        for i in f.nodes:
            n = new.nodes[i]
            if n.dead:
                continue
            a = _const_assign(new, n, _enums(new, n))
            if a and a[0] in state_vars:
                parents = tuple(sorted((p, str(lab)) for p, lab in new.cd_parents.get(i, ())))
                groups[(a[0], parents)].append((n, a[1]))
        for (k, _), lst in groups.items():
            vals = {v for _, v in lst}
            if len(vals) > 1 and any(n.id in new_nodes for n, _ in lst):
                n0 = lst[0][0]
                C.add("3-3", CANDIDATE, f"같은 실행 조건에서 상태 변수 `{_var_name(new, k)}` 를 {sorted(vals)} 로 여러 번 대입 (전이 충돌 후보: 마지막 값만 남음)",
                      func=f.name, file=_rel(new, n0.file), line=n0.line, reviewer="한 주기 안에서 상태가 덮어써지는 것이 의도된 것인지 확인하세요.")
        dead_new = {new.nodes[i].text for i in f.nodes if new.nodes[i].dead and new.nodes[i].visible}
        if fk in old.funcs:
            dead_old = {old.nodes[i].text for i in old.funcs[fk].nodes if old.nodes[i].dead and old.nodes[i].visible}
            for t in sorted(dead_new - dead_old)[:5]:
                C.add("3-3", CANDIDATE, f"새로 도달 불가능해진 문장: `{_short(t)}` (상수 조건)", func=f.name, file=_rel(new, f.file),
                      reviewer="상수 조건 때문에 실행되지 않게 된 코드가 의도된 것인지(기능 비활성화) 확인하세요.")
    C.note("3-3", f"상태 변수 후보 {len(state_vars)}개 중 변경과 관련된 {checked_states}개의 대입 값·판정 값·전이 충돌, 도달 불가능 문장을 검사")

    # ---------------- 3-4 다른 사용 위치 수정 누락 ----------------
    by_norm = defaultdict(list)
    for n in new.nodes:
        if n.dead or not n.visible or n.kind not in ("cond", "stmt"):
            continue
        e = _expr(n)
        if e is not None:
            by_norm[(n.kind, norm(e))].append(n)
    for c in pairs:
        if c.kind != "changed" or c.other_node is None:
            continue
        o, n = old.nodes[c.other_node], new.nodes[c.node]
        e = _expr(o)
        if e is None or len(norm(e)) < 8 or not isinstance(e, (c_ast.BinaryOp, c_ast.FuncCall, c_ast.Assignment, c_ast.UnaryOp)):
            continue
        others = [x for x in by_norm.get((o.kind, norm(e)), []) if x.id not in new_nodes]
        if others:
            f, file, line = loc(new, n)
            refs = [f"{new.func_display(x.func)} {_rel(new, x.file)}:{x.line}" for x in others[:10]]
            C.add("3-4", REVIEW, f"바뀐 `{_short(o.text, 60)}` 와 같은 식이 다른 {len(others)}곳에는 그대로 남음 (수정 누락 후보)",
                  func=f, file=file, line=line, refs=refs, evidence=refs[:5],
                  reviewer="같은 조건·계산을 쓰는 다른 위치에도 동일한 변경이 필요한지 확인하세요.")
        for d in expr_diff(_expr(o), _expr(n)):
            m = re.match(r"호출 대상 (\w+)\(\) → (\w+)\(\)", d)
            if m:
                still = [s for s in new.sites if s.name == m.group(1)]
                if still:
                    f, file, line = loc(new, n)
                    refs = [f"{new.func_display(s.caller)} {_rel(new, new.nodes[s.call_node].file)}:{new.nodes[s.call_node].line}" for s in still[:10]]
                    C.add("3-4", REVIEW, f"{m.group(1)}() 를 {m.group(2)}() 로 바꿨지만 다른 {len(still)}곳은 {m.group(1)}() 를 계속 호출",
                          func=f, file=file, line=line, refs=refs, evidence=refs[:5],
                          reviewer="다른 호출 위치도 같은 함수로 바꿔야 하는지 확인하세요.")
    C.note("3-4", "수정된 조건·식·호출 대상의 변경 전 형태가 다른 위치에 남아 있는지 전체 코드에서 검색")

    # ---------------- 3-5 삭제·이동·대체 ----------------
    new_var_by_name = {}
    for k, v in new.vars.items():
        new_var_by_name.setdefault((v.name, v.kind), k)
    for c in changes:
        if c.version != "old" or c.kind not in ("removed", "func_removed", "global_removed"):
            continue
        o = old.nodes[c.node]
        f, file, line = loc(old, o)
        if c.kind == "func_removed":
            callers = sorted({old.func_display(old.sites[s].caller) for s in old.callers.get(c.func, ())})
            C.add("3-5", REVIEW, f"함수 삭제: {old.func_display(c.func)}()" + (f" — 변경 전 호출 함수: {', '.join(callers[:6])}" if callers else ""),
                  func=f, file=file, line=line, refs=callers,
                  reviewer="삭제된 함수가 하던 기능이 다른 곳으로 옮겨졌는지, 여전히 필요한 기능은 아닌지 확인하세요.")
            continue
        for p in o.defs:
            if not _persistent(old, p[0]):
                continue
            name = _var_name(old, p[0])
            nk = new_var_by_name.get((name, old.vars[p[0]].kind))
            if nk is None:
                continue
            writers = [x for x, _ in new.def_index.get(nk, ()) if not new.nodes[x].dead and new.nodes[x].kind != "ginit"]
            readers = [x for x, _ in new.use_index.get(nk, ()) if not new.nodes[x].dead]
            if readers and not writers:
                C.add("3-5", CANDIDATE, f"삭제된 `{_short(o.text, 60)}` 가 `{name}` 의 마지막 갱신 위치였음 — 읽는 곳 {len(readers)}곳은 남음",
                      func=f, file=file, line=line,
                      refs=[f"{new.func_display(new.nodes[x].func)} {_rel(new, new.nodes[x].file)}:{new.nodes[x].line}" for x in readers[:8]],
                      reviewer=f"`{name}` 를 읽는 처리가 이제 초기값만 보게 되는데 의도된 것인지 확인하세요.")
            elif readers:
                C.add("3-5", REVIEW, f"삭제된 문장 `{_short(o.text, 60)}` 가 쓰던 `{name}` 를 다른 {len(readers)}곳에서 읽음",
                      func=f, file=file, line=line, reviewer="삭제로 이 값을 읽는 경로의 동작이 달라지는데 기능상 문제없는지 확인하세요.")
    for c in pairs:
        if c.kind == "changed" and c.other_node is not None:
            for d in expr_diff(_expr(old.nodes[c.other_node]), _expr(new.nodes[c.node])):
                if d.startswith("호출 대상") or d.startswith("대입 대상"):
                    f, file, line = loc(new, new.nodes[c.node])
                    C.add("3-5", REVIEW, f"대체: {d}", func=f, file=file, line=line,
                          reviewer="대체 전 코드에 의존하던 처리(부수 효과·반환값 사용)가 새 코드에서도 유지되는지 확인하세요.")
    C.note("3-5", "삭제된 함수의 호출 위치, 삭제된 문장이 쓰던 전역 값의 남은 읽기·쓰기, 호출·대입 대상 대체를 추적")

    # ---------------- 4-1 초기화 ----------------
    for c in changes:
        if c.kind in ("global_changed", "global_added", "global_removed") and not (c.kind == "global_changed" and c.version == "old"):
            prog = new if c.version == "new" else old
            n = prog.nodes[c.node]
            f, file, line = loc(prog, n)
            verb = {"global_changed": "초기값 변경", "global_added": "새 전역·정적 변수", "global_removed": "전역·정적 변수 삭제"}[c.kind]
            cls = REVIEW if c.kind == "global_changed" else CHANGE
            C.add("4-1", cls, f"{verb}: " + (f"`{_short(c.other_text, 50)}` → `{_short(c.text, 50)}`" if c.kind == "global_changed" else f"`{_short(c.text, 70)}`"),
                  func=f, file=file, line=line, evidence=[c.detail] if c.detail else [],
                  reviewer="바뀐 초기값이 첫 주기·리셋 직후 동작에 맞는지, 초기화 함수가 다시 덮어쓰지 않는지 확인하세요.")
    for note in getattr(cs, "notes", []) or []:
        if "초기화" in note or "필드" in note:
            C.add("4-1", CHANGE, _short(note, 160), reviewer="새 필드가 0 으로 시작해도 되는지 확인하세요.")
    rdd = defaultdict(set)
    for d, succ in new.dd.items():
        for m, lab in succ:
            rdd[m].add(lab.split(".")[0].split("[")[0])
    checked_uses = 0
    for fk in sorted(changed_funcs_new):
        f = new.funcs[fk]
        for i in f.nodes:
            n = new.nodes[i]
            if n.dead or i not in new_nodes:
                continue
            for u in n.uses:
                v = new.vars.get(u[0])
                if not v or v.kind != "local" or u[0] in new.use_index:
                    continue
                checked_uses += 1
                if v.name not in rdd.get(i, set()):
                    C.add("4-1", CANDIDATE, f"지역 변수 `{v.name}` 를 대입 전에 읽을 수 있는 경로 (초기화 전 사용 후보)",
                          func=f.name, file=_rel(new, n.file), line=n.line, evidence=[f"`{_short(n.text)}`"],
                          reviewer="모든 경로에서 이 지점 전에 값이 정해지는지 확인하세요.")
                    C.add("6-3", CANDIDATE, f"지역 변수 `{v.name}` 초기화 전 사용 후보", func=f.name, file=_rel(new, n.file), line=n.line,
                          reviewer="소비 시점에 유효한 값이 보장되는지 확인하세요.")
    for fk in sorted(changed_funcs_new):
        if P["init_func"].search(new.funcs[fk].name):
            fn = new.funcs[fk]
            C.add("4-1", REVIEW, f"초기화 함수 {fn.name}() 변경", func=fn.name, file=_rel(new, fn.file), line=fn.line,
                  reviewer="실제 실행 순서(부팅·리셋·모드 전환)에서 이 초기화가 사용 전에 호출되는지 확인하세요.")
    C.note("4-1", f"전역 초기값 변경, 새 구조체 필드, 초기화 함수 변경, 변경 문장의 지역 변수 읽기 {checked_uses}건의 도달 정의를 검사")

    # ---------------- 4-2 오류 처리·복구 ----------------
    seeds_new = set(cs.seeds_new)
    by_node_change = {c.node: c for c in changes if c.version == "new"}
    for c in changes:
        prog = new if c.version == "new" else old
        n = prog.nodes[c.node]
        names = [prog.sites[n.site].name] if n.site is not None and n.kind in ("call", "extcall", "actual_in") else \
            [prog.sites[prog.site_of[id(x)]].name for x in _iter(_expr(n)) if isinstance(x, c_ast.FuncCall) and id(x) in prog.site_of]
        hits = [x for x in names if P["error_call"].search(x)]
        if not hits or (c.version == "old" and c.kind in ("changed", "moved")):
            continue
        f, file, line = loc(prog, n)
        if c.kind == "removed":
            C.add("4-2", CANDIDATE, f"오류 처리·복구 호출 삭제: {', '.join(sorted(set(hits)))}()", func=f, file=file, line=line,
                  reviewer="이 오류가 더 이상 보고·복구되지 않아도 되는지(다른 곳에서 처리하는지) 확인하세요.")
        else:
            C.add("4-2", REVIEW, f"오류 처리·복구 호출 {('추가' if c.kind == 'added' else '변경')}: {', '.join(sorted(set(hits)))}()",
                  func=f, file=file, line=line, evidence=[f"`{_short(n.text)}`"],
                  reviewer="변경된 오류 반응(보고 시점·대상·복구 동작)이 요구된 오류 처리 정책과 맞는지 확인하세요.")
    for s in new.sites:
        if s.call_node < 0 or not P["error_call"].search(s.name):
            continue
        for p, lab in new.cd_parents.get(s.call_node, ()):
            if p in seeds_new and new.nodes[p].kind == "cond" and s.call_node not in new_nodes:
                ch = by_node_change.get(p)
                n = new.nodes[s.call_node]
                C.add("4-2", REVIEW, f"{s.name}() 의 실행 조건이 바뀜: `{_short(new.nodes[p].text)}`",
                      func=new.func_display(s.caller), file=_rel(new, n.file), line=n.line,
                      evidence=[ch.detail] if ch and ch.detail else [],
                      reviewer="오류 보고·리셋이 일어나는 조건이 바뀐 것이 의도된 것인지 확인하세요.")
    C.note("4-2", "오류 보고·리셋·복구 이름 패턴의 호출 추가·삭제·변경과, 바뀐 조건이 직접 제어하는 호출을 검사")

    # ---------------- 4-3 카운터 ----------------
    def counter_vars(prog, n):
        return sorted({display_path(prog, p) for p in (n.defs | n.uses) if P["counter"].search(_var_name(prog, p[0]))})

    for c in changes:
        if c.version == "old" and c.kind in ("changed", "moved", "global_changed"):
            continue
        prog = new if c.version == "new" else old
        n = prog.nodes[c.node]
        cv = counter_vars(prog, n)
        call_cnt = [x for x in _iter(_expr(n)) if isinstance(x, c_ast.FuncCall) and re.search(r"Count(Up|Down)|Inc|Dec", gen(x.name))]
        if not cv and not call_cnt:
            continue
        f, file, line = loc(prog, n)
        what = ", ".join(cv) or gen(call_cnt[0].name)
        verb = {"changed": "변경", "added": "추가", "removed": "삭제", "moved": "실행 조건 변경(이동)"}.get(c.kind, c.kind)
        if n.kind == "cond":
            C.add("4-3", REVIEW, f"카운터 판정 조건 {verb}: `{_short(c.other_text, 40) + '` → `' if c.kind == 'changed' else ''}{_short(n.text, 50)}`",
                  func=f, file=file, line=line, evidence=[c.detail] if c.detail else [],
                  reviewer="재시도·실패 판정 임계값과 횟수가 요구사항과 맞는지 확인하세요.")
        elif c.kind == "removed":
            C.add("4-3", CANDIDATE, f"카운터 갱신·초기화 삭제 ({what}): `{_short(n.text)}`", func=f, file=file, line=line,
                  reviewer="카운터가 더 이상 증가·감소·초기화되지 않아 임계값 판정이 영원히 성립(또는 불성립)하지 않는지 확인하세요.")
        else:
            C.add("4-3", REVIEW, f"카운터 갱신 {verb} ({what}): `{_short(n.text)}`", func=f, file=file, line=line,
                  reviewer="증감 단위·초기화 시점이 재시도·복구 조건과 맞는지 확인하세요.")
    C.note("4-3", "카운터 이름 패턴(cnt·count·retry·fail·debounce)의 증감·초기화·임계값 비교 변경을 검사")

    # ---------------- 4-4 복수 오류 조건 우선순위·동시 성립 ----------------
    chains_checked = 0
    for fk in sorted(changed_funcs_new):
        f = new.funcs[fk]
        for i in f.nodes:
            n = new.nodes[i]
            if n.kind != "cond" or not n.sig.startswith("if|") or n.dead:
                continue
            chain = [n]
            cur = n
            while True:
                # else if: 거짓 쪽 다음 조건이 앞 조건에 제어 의존할 때만 (연속된 별개의 if 는 사슬이 아님)
                nxt = [m for m, lab in f.succ.get(cur.id, []) if lab == "F" and new.nodes[m].kind == "cond"
                       and new.nodes[m].sig.startswith("if|") and (cur.id, "F") in new.cd_parents.get(m, ())]
                if not nxt or len(chain) > 12:
                    break
                cur = new.nodes[nxt[0]]
                chain.append(cur)
            if len(chain) < 2 or not any(x.id in new_nodes for x in chain):
                continue
            # 다른 사슬의 중간이면 건너뜀 (맨 앞 조건에서 시작한 사슬만)
            if any(p for p, lab in new.cd_parents.get(i, ()) if lab == "F" and new.nodes[p].kind == "cond"
                   and new.nodes[p].sig.startswith("if|") and i in [m for m, l2 in f.succ.get(p, []) if l2 == "F"]):
                continue
            chains_checked += 1
            errorish = any(P["error_call"].search(_names_in(new, x)) for x in chain)
            overl = _overlaps(new, f.key, chain)
            label = "오류 조건" if errorish else "조건"
            if overl is None:
                C.add("4-4", INSUFFICIENT, f"{label} 사슬 {len(chain)}개의 동시 성립 여부를 판정하지 못함 (z3 없음 또는 기호화 실패)",
                      func=f.name, file=_rel(new, n.file), line=n.line, reviewer="조건들이 동시에 성립할 수 있는지 직접 확인하세요.")
            for a, b in overl or []:
                C.add("4-4", REVIEW, f"{label} `{_short(a.text, 50)}` 과 `{_short(b.text, 50)}` 이 동시에 성립 가능 → 앞의 분기가 우선 처리됨",
                      func=f.name, file=_rel(new, a.file), line=a.line,
                      reviewer="두 조건이 동시에 성립할 때 앞 분기만 처리되는 우선순위가 요구된 오류 처리 정책과 일치하는지 확인하세요.")
    C.note("4-4", f"변경과 관련된 if/else if 사슬 {chains_checked}개에서 조건 쌍의 동시 성립 가능성을 기호 실행(z3)으로 검사")

    # ---------------- 5-1·5-2 경계·자료형 (보안 분석 결과 포함) ----------------
    for c in pairs:
        if c.kind != "changed" or c.other_node is None:
            continue
        o, n = old.nodes[c.other_node], new.nodes[c.node]
        d = expr_diff(_expr(o), _expr(n))
        f, file, line = loc(new, n)
        bound = [x for x in d if x.startswith("연산자") and any(op in x for op in ("<", ">", "="))] + \
                ([c.detail] if c.interval else [])
        if n.kind == "cond" and (bound or any(x.startswith("상수") for x in d)):
            C.add("5-1", REVIEW, f"경계·임계값 변경: `{_short(o.text, 50)}` → `{_short(n.text, 50)}`", func=f, file=file, line=line,
                  evidence=d + ([c.detail] if c.detail else []),
                  reviewer="변경된 경계값(판정이 달라지는 구간 포함)이 요구사항의 임계값·비교 기준(이상/초과)과 일치하는지 확인하세요.")
        tdiff = [x for x in d if x.startswith("형변환") or "연산 순서" in x or (x.startswith("연산자") and any(op in x for op in "+-*/%<>&|^"))]
        if tdiff:
            C.add("5-2", REVIEW, "; ".join(tdiff[:3]), func=f, file=file, line=line, evidence=[f"`{_short(o.text)}` → `{_short(n.text)}`"],
                  reviewer="계산 방식(정수 나눗셈·반올림·부호)과 연산 순서가 사양과 맞는지 확인하세요.")
    for c in changes:
        if c.kind == "type_changed":
            prog = new
            C.add("5-2", REVIEW, f"자료형 변경: {c.text} — {_short(c.detail, 120)}", func=prog.func_display(c.func or ""),
                  file=_rel(prog, c.file), line=c.line,
                  reviewer="바뀐 자료형의 범위·부호가 값의 실제 범위와 연산(넘침·잘림)에 맞는지 확인하세요.")
    for r in result.get("risks", []):
        if r.get("status_code") not in ("NEW_REGRESSION", "PENDING"):
            continue
        item = "5-1" if r["kind"] in ("index", "length") else "5-2" if r["kind"] in ("narrow", "shift", "div") else \
            "6-3" if r["kind"] in ("deref", "fptr") else "7-1" if r["kind"] == "loop" else None
        if r.get("category") == "보안 조건 변경":
            lab = r.get("kind_label", "")
            item = "5-1" if "입력 검증" in lab else "4-3" if "실패 횟수" in lab else "3-3" if "상태" in lab else "3-1"
        if item is None:
            continue
        cls = VIOLATION if r["state"] == "violated" else (REVIEW if r.get("category") == "보안 조건 변경" else CANDIDATE)
        sym = (r.get("symbolic") or {}).get("counterexample")
        ev = [r.get("evidence", "")] + (["반례: " + ", ".join(f"{k} = {v}" for k, v in sym.items())] if sym else []) + \
            ([f"범위: {r['bounds']['change']}"] if (r.get("bounds") or {}).get("change") else [])
        C.add(item, cls, f"[{r['id']}] {r['kind_label']}: `{_short(r['expr'], 70)}` — {r.get('verification_level', '')}",
              func=r["func"], file=r["file"], line=r["line"], evidence=[x for x in ev if x],
              reviewer=r.get("verification") or "")
    C.note("5-1", "조건식의 비교 연산자·상수 변경과 판정 차이 구간, 배열 인덱스·복사 길이의 범위(구간 분석·기호 실행)를 검사")
    C.note("5-2", "형변환·연산자·괄호 변경, 자료형 변경, 값 잘림·시프트·0 나누기 위험(범위 분석·기호 실행)을 검사")

    # ---------------- 5-3 파라미터 참조 위치 ----------------
    for c in changes:
        if c.kind != "global_changed" or c.version != "new":
            continue
        n = new.nodes[c.node]
        key = n.var
        if not key:
            continue
        uses = [x for x, _ in new.use_index.get(key, ()) if not new.nodes[x].dead]
        conds = [x for x in uses if new.nodes[x].kind == "cond"]
        funcs = sorted({new.func_display(new.nodes[x].func) for x in uses if new.nodes[x].func})
        refs = [f"{new.func_display(new.nodes[x].func)} {_rel(new, new.nodes[x].file)}:{new.nodes[x].line} `{_short(new.nodes[x].text, 50)}`" for x in conds[:10]]
        C.add("5-3", REVIEW, f"파라미터 `{_var_name(new, key)}` 변경 ({_short(c.detail, 40)}) — 참조 {len(uses)}곳 · 함수 {len(funcs)}개 · 영향 분기 {len(conds)}곳",
              func="(전역 초기화)", file=_rel(new, n.file), line=n.line, refs=refs, evidence=funcs[:8],
              reviewer="이 파라미터를 쓰는 모든 기능(목록)에 새 값이 적용되어도 되는지, 적용 범위가 의도와 같은지 확인하세요.")
    C.note("5-3", "초기값이 바뀐 전역·상수 테이블(파라미터)의 모든 참조 위치와 그 값을 판정하는 분기를 나열")

    # ---------------- 6-1 의존성 변화 ----------------
    for row in (deps_diff or {}).get("functions", []):
        if row["status"] == "변경":
            parts = []
            for k, ko in (("calls", "호출"), ("reads", "읽기"), ("writes", "쓰기")):
                if row.get(k + "_added"):
                    parts.append(f"{ko} +{', '.join(row[k + '_added'][:4])}")
                if row.get(k + "_removed"):
                    parts.append(f"{ko} −{', '.join(row[k + '_removed'][:4])}")
            C.add("6-1", CHANGE, f"{row['func']}: " + " · ".join(parts), func=row["func"], file=row.get("file", ""),
                  reviewer="추가·삭제된 호출과 데이터 읽기·쓰기가 연관 기능에 주는 실제 영향을 확인하세요 (이 변화 자체는 취약점이 아님).")
        else:
            C.add("6-1", CHANGE, f"{row['func']}: 함수 {row['status']}", func=row["func"], file=row.get("file", ""),
                  reviewer="함수 추가·삭제로 바뀌는 호출 관계가 연관 기능에 미치는 영향을 확인하세요.")
    C.note("6-1", f"변경 전후 함수별 호출·읽기·쓰기 집합 비교 ({len((deps_diff or {}).get('functions', []))}개 함수 변화)")

    # ---------------- 6-2 인터페이스 ----------------
    def sig(prog, f):
        ft = f.decl.type
        params = ft.args.params if isinstance(ft, c_ast.FuncDecl) and ft.args else []
        ps = []
        for p in params:
            if isinstance(p, (c_ast.Decl, c_ast.Typename)):
                ps.append(f.env.signature(p.type))
        return ps, f.env.signature(f.ret_type)

    for fk in sorted(set(old.funcs) & set(new.funcs)):
        po, ro = sig(old, old.funcs[fk])
        pn, rn = sig(new, new.funcs[fk])
        if po != pn or ro != rn:
            fn = new.funcs[fk]
            callers = sorted({new.func_display(new.sites[s].caller) for s in new.callers.get(fk, ())})
            what = []
            if len(po) != len(pn):
                what.append(f"인자 수 {len(po)} → {len(pn)}")
            elif po != pn:
                what.append("인자 자료형 변경")
            if ro != rn:
                what.append("반환형 변경")
            C.add("6-2", REVIEW, f"함수 인터페이스 변경 {fn.name}(): {', '.join(what)}", func=fn.name, file=_rel(new, fn.file), line=fn.line,
                  refs=callers, evidence=[f"호출 함수 {len(callers)}개: {', '.join(callers[:6])}"],
                  reviewer="인터페이스 사양(인자 의미·단위·범위, 반환값 의미)과 모든 호출 위치가 새 정의에 맞는지 확인하세요.")
    by_name = {}
    for x in new.funcs.values():
        if not x.static:
            by_name.setdefault(x.name, x)
    for name, (fd, env) in new.protos.items():
        f = by_name.get(name)
        if f is None or f.key not in changed_funcs_new:
            continue
        dp = fd.args.params if fd.args else []
        fp = f.decl.type.args.params if isinstance(f.decl.type, c_ast.FuncDecl) and f.decl.type.args else []
        def cnt(ps):
            return 0 if len(ps) == 1 and isinstance(ps[0], (c_ast.Typename, c_ast.Decl)) and gen(ps[0]).strip() == "void" else \
                len([p for p in ps if not isinstance(p, c_ast.EllipsisParam)])
        if cnt(dp) != cnt(fp):
            C.add("6-2", CANDIDATE, f"{name}(): 선언(원형)과 정의의 인자 수가 다름 ({cnt(dp)} vs {cnt(fp)})", func=name, file=_rel(new, f.file), line=f.line,
                  reviewer="헤더 선언과 정의를 일치시켜야 하는지 확인하세요.")
    for s in new.sites:
        if s.callees and s.caller in changed_funcs_new:
            for k in s.callees:
                f = new.funcs[k]
                fp = f.decl.type.args.params if isinstance(f.decl.type, c_ast.FuncDecl) and f.decl.type.args else []
                if any(isinstance(p, c_ast.EllipsisParam) for p in fp):
                    continue
                n_params = 0 if len(fp) == 1 and gen(fp[0]).strip() == "void" else len(fp)
                n_args = len(s.ast.args.exprs) if s.ast.args else 0
                if n_args != n_params:
                    n = new.nodes[s.call_node]
                    C.add("6-2", CANDIDATE, f"{s.name}() 호출 인자 {n_args}개 ↔ 정의 인자 {n_params}개", func=new.func_display(s.caller),
                          file=_rel(new, n.file), line=n.line, reviewer="호출과 정의의 인자 불일치를 확인하세요.")
    eo = {k: v for e in old.enums.values() for k, v in e.items()}
    en_ = {k: v for e in new.enums.values() for k, v in e.items()}
    for k in sorted(set(eo) | set(en_)):
        if eo.get(k) != en_.get(k):
            if k in eo and k in en_:
                C.add("6-2", REVIEW, f"enum 상수 `{k}` 값 {eo[k]} → {en_[k]}", func="(전역)",
                      reviewer="이 enum 값을 저장·전송하는 곳(NVM·통신·진단)의 사양과 맞는지 확인하세요.")
            else:
                C.add("6-2", CHANGE, f"enum 상수 `{k}` {'추가' if k in en_ else '삭제'}", func="(전역)",
                      reviewer="새·삭제된 enum 값을 처리하는 switch·조건이 모두 갱신되었는지 확인하세요.")
    for c in changes:
        if c.kind == "type_changed":
            C.add("6-2", REVIEW, f"구조체·자료형 정의 변경: {c.text} — {_short(c.detail, 120)}", func=new.func_display(c.func or ""),
                  file=_rel(new, c.file), line=c.line, reviewer="구조체 배치·크기가 바뀐 데이터를 주고받는 인터페이스(통신·NVM·다른 모듈) 사양과 맞는지 확인하세요.")
    for note in getattr(cs, "notes", []) or []:
        if "자료형 변경" in note:
            C.add("6-2", CHANGE, _short(note, 180), func="(전역)", reviewer="구조체 필드 추가가 이 구조체를 주고받는 인터페이스 사양에 반영되었는지 확인하세요.")
    C.note("6-2", "함수 원형(인자·반환형), 선언과 정의·호출 인자 수 불일치, 구조체·enum 정의 변경을 비교 (매크로 정의는 8-1)")

    # ---------------- 6-3 정의-사용 ----------------
    for c in changes:
        if c.version != "new" or c.kind not in ("changed", "added"):
            continue
        n = new.nodes[c.node]
        for p in n.defs:
            if not _persistent(new, p[0]):
                continue
            readers = [x for x, u in new.use_index.get(p[0], ()) if not new.nodes[x].dead and x != n.id]
            funcs = sorted({new.func_display(new.nodes[x].func) for x in readers if new.nodes[x].func})
            if not readers:
                continue
            f, file, line = loc(new, n)
            C.add("6-3", CHANGE, f"`{display_path(new, p)}` 정의 변경 → 사용 {len(readers)}곳 (함수 {', '.join(funcs[:5]) or '-'})",
                  func=f, file=file, line=line, refs=funcs,
                  reviewer="이 값을 읽는 시점(다른 태스크·다음 주기 포함)에 새 값이 유효한지 확인하세요.")
    for c in changes:
        if c.version != "new":
            continue
        n = new.nodes[c.node]
        for u in n.uses:
            v = new.vars.get(u[0])
            if not v or not _persistent(new, u[0]) or v.init is not None:
                continue
            writers = [x for x, _ in new.def_index.get(u[0], ()) if not new.nodes[x].dead and new.nodes[x].kind != "ginit"]
            if not writers and v.kind in ("static", "static_local"):
                f, file, line = loc(new, n)
                C.add("6-3", CANDIDATE, f"`{v.name}` 를 읽지만 어디서도 값을 쓰지 않음 (항상 0 후보)", func=f, file=file, line=line,
                      reviewer="이 값을 설정하는 처리가 빠지지 않았는지 확인하세요.")
    C.note("6-3", "변경 문장이 정의하는 전역 값의 사용 위치(Def-Use), 쓰는 곳이 없는 static 값 읽기, 초기화 전 사용(4-1)을 추적")

    # ---------------- 6-4 같은 값을 다른 기준으로 판단 ----------------
    preds = defaultdict(list)
    for n in new.nodes:
        if n.kind == "cond" and not n.dead and n.ast is not None:
            try:
                pr = predicate(n.ast, new)
            except Exception:
                pr = None
            if pr:
                preds[pr[0]].append((n, pr))
    for c in pairs:
        if c.kind != "changed" or new.nodes[c.node].kind != "cond":
            continue
        n = new.nodes[c.node]
        try:
            pr = predicate(n.ast, new)
        except Exception:
            pr = None
        if not pr:
            continue
        others = [(m, q) for m, q in preds.get(pr[0], []) if m.id != n.id and q[1] != pr[1]]
        if others:
            f, file, line = loc(new, n)
            refs = [f"{new.func_display(m.func)} {_rel(new, m.file)}:{m.line} `{fmt_set(pr[0], q[1], q[2])}`" for m, q in others[:8]]
            C.add("6-4", REVIEW, f"`{pr[0]}` 를 바뀐 기준 `{fmt_set(pr[0], pr[1], pr[2])}` 와 다른 기준으로 판단하는 위치 {len(others)}곳",
                  func=f, file=file, line=line, refs=refs, evidence=refs[:5],
                  reviewer="같은 값을 쓰는 경로들이 같은 기준으로 동작해야 하는지(의도된 차이인지) 확인하세요.")
    C.note("6-4", "바뀐 조건의 변수를 판단하는 다른 조건식의 기준 구간을 비교")

    # ---------------- 6-5 외부 의존성 ----------------
    ext = set(new.extern_funcs) | set(old.extern_funcs)
    for row in (deps_diff or {}).get("functions", []):
        add = [x for x in row.get("calls_added", []) if x in ext]
        rem = [x for x in row.get("calls_removed", []) if x in ext]
        if add or rem:
            C.add("6-5", REVIEW, f"{row['func']}: 외부 함수 " + " · ".join(p for p in ((f"+{', '.join(add)}" if add else ""), (f"−{', '.join(rem)}" if rem else "")) if p),
                  func=row["func"], file=row.get("file", ""),
                  reviewer="새로 쓰는 외부(드라이버·라이브러리·BSW) 함수의 실제 동작·호출 조건·반환값 의미를 확인하세요. 분석에서는 본문 없는 함수로 보수적으로 가정했습니다.")
    if file_changes:
        hdr = [f for f in file_changes.get("modified", []) + file_changes.get("added", []) + file_changes.get("removed", []) if f.endswith(".h")]
        if hdr:
            C.add("6-5", CHANGE, f"헤더 변경 {len(hdr)}개", evidence=hdr[:12], refs=hdr,
                  reviewer="바뀐 헤더를 include 하는 다른 모듈(분석 범위 밖 포함)이 새 정의와 맞는지 확인하세요.")
    C.note("6-5", "외부 함수 호출의 추가·삭제, 헤더 파일 변경, #include 변경(8-1)을 검사")

    # ---------------- 7-1 호출 순서·반복 ----------------
    for fk in sorted(changed_funcs_new & set(old.funcs)):
        def seq(prog, f):
            return [prog.sites[prog.nodes[i].site].name for i in f.nodes
                    if prog.nodes[i].kind in ("call", "extcall") and not prog.nodes[i].dead]
        so, sn = seq(old, old.funcs[fk]), seq(new, new.funcs[fk])
        common = [x for x in so if x in sn]
        fn = new.funcs[fk]
        if common and [x for x in sn if x in common] != common:
            C.add("7-1", REVIEW, f"{fn.name}(): 호출 순서 변경", func=fn.name, file=_rel(new, fn.file), line=fn.line,
                  evidence=[f"변경 전: {' → '.join(so[:8])}", f"변경 후: {' → '.join(sn[:8])}"],
                  reviewer="요구된 처리 시퀀스(초기화→처리→출력 등)와 새 호출 순서가 일치하는지 확인하세요.")
        def wseq(prog, f):
            out = []
            for i in f.nodes:
                n = prog.nodes[i]
                if n.kind == "stmt" and not n.dead:
                    out += [display_path(prog, p) for p in n.defs if _persistent(prog, p[0])]
            return out
        wo, wn = wseq(old, old.funcs[fk]), wseq(new, new.funcs[fk])
        wc = list(dict.fromkeys(x for x in wo if x in wn))
        if len(wc) > 1 and list(dict.fromkeys(x for x in wn if x in wc)) != wc:
            C.add("7-1", REVIEW, f"{fn.name}(): 상태 갱신 순서 변경", func=fn.name, file=_rel(new, fn.file), line=fn.line,
                  evidence=[f"변경 전: {' → '.join(wc[:6])}"], reviewer="상태 변수 갱신 순서가 바뀌어 같은 주기 안의 판단이 달라지지 않는지 확인하세요.")
    for c in changes:
        if c.version != "new" or c.kind not in ("changed", "added"):
            continue
        n = new.nodes[c.node]
        if n.kind == "cond" and n.sig.startswith(("while|", "for|", "dowhile|")):
            f, file, line = loc(new, n)
            C.add("7-1", REVIEW, f"반복·종료 조건 {('변경' if c.kind == 'changed' else '추가')}: `{_short(n.text)}`", func=f, file=file, line=line,
                  evidence=[c.detail] if c.detail else [], reviewer="반복 횟수·종료 조건이 처리 시간(주기) 안에 끝나는지 확인하세요.")
    C.note("7-1", "변경 함수의 호출 순서, 전역 상태 갱신 순서, 반복·종료 조건 변경을 비교")

    # ---------------- 7-2 타이머 ----------------
    for c in changes:
        if c.version == "old" and c.kind in ("changed", "moved", "global_changed"):
            continue
        prog = new if c.version == "new" else old
        n = prog.nodes[c.node]
        tv = sorted({display_path(prog, p) for p in (n.defs | n.uses) if P["timer"].search(_var_name(prog, p[0]))})
        if not tv:
            continue
        f, file, line = loc(prog, n)
        role = "만료·판정 조건" if n.kind == "cond" else ("시작·리셋·증감" if n.defs else "사용")
        cls = CANDIDATE if c.kind == "removed" and n.kind != "cond" else REVIEW
        C.add("7-2", cls, f"타이머 {role} {({'changed': '변경', 'added': '추가', 'removed': '삭제', 'moved': '이동', 'global_changed': '기준값 변경'}).get(c.kind, c.kind)} ({', '.join(tv[:3])}): `{_short(c.text if c.kind != 'removed' else n.text, 60)}`",
              func=f, file=file, line=line, evidence=[c.detail] if c.detail else [],
              reviewer=("타이머가 더 이상 리셋·증가되지 않아 만료가 항상(또는 전혀) 일어나지 않는지 확인하세요." if cls == CANDIDATE
                        else "기준값·증감 주기가 실제 호출 주기와 시간 요구사항(ms)을 만족하는지 확인하세요."))
    C.note("7-2", "타이머 이름 패턴(timer·tick·time·delay·timeout·_tm)의 시작·리셋·증감·만료 조건·기준값 변경을 검사")

    # ---------------- 7-3 동시성 ----------------
    shared = {s["var"]: s for s in result.get("shared", [])}
    has_isr = any(e.get("isr") for e in result.get("entries", []))
    if not has_isr:
        C.add("7-3", INSUFFICIENT, "ISR·태스크 실행 모델이 없음 (이름으로 추정되는 ISR 이 없고 --isr 지정도 없음)",
              reviewer="--isr·--entry 로 ISR·태스크 함수를 지정해 다시 분석하거나, 공유 변수 접근 규칙(보호 구간)을 직접 확인하세요.")
    crit = {f.key for f in new.funcs.values() if any(P["critical"].search(new.sites[new.nodes[i].site].name)
                                                     for i in f.nodes if new.nodes[i].kind in ("call", "extcall"))}
    for c in changes:
        if c.version != "new":
            continue
        n = new.nodes[c.node]
        if n.kind == "ginit":
            continue
        for p in n.defs | n.uses:
            name = display_path(new, p)
            s = shared.get(name) or shared.get(display_path(new, p[:1]))
            if not s:
                continue
            f, file, line = loc(new, n)
            write = p in n.defs
            protected = n.func in crit
            cls = CANDIDATE if (s["isr"] and write and not protected) else REVIEW
            C.add("7-3", cls, f"공유 변수 `{name}` {'쓰기' if write else '읽기'} 변경 — 접근 진입점: {', '.join(s['entries'][:4])}"
                  + (" (ISR 포함)" if s["isr"] else "") + (" · 보호 구간 호출 있음" if protected else ""),
                  func=f, file=file, line=line,
                  reviewer="ISR·다른 태스크와 동시에 접근할 때 값이 깨지지 않는지(원자성·보호 구간·읽기 순서) 실제 실행 환경에서 확인하세요.")
    C.note("7-3", f"여러 진입점이 접근하는 공유 변수 {len(shared)}개와 변경 문장의 읽기·쓰기, 보호 구간 호출 여부를 검사")

    # ---------------- 8-x 전처리 지시문 (원문) ----------------
    _review_preprocessor(C, old, new, file_changes, changes, build_defines or [], P)

    return _summarize(C)


def _overlaps(prog: Program, func_key: str, chain):
    """사슬의 조건 쌍 중 동시에 성립할 수 있는 것. z3 가 없으면 None."""
    from . import symexec
    if not symexec.available():
        return None
    try:
        ex = symexec._Exec(prog, func_key, 2000)
        st = symexec._State({}, [], False, {})
        exprs = [ex.truth(st, c.ast) for c in chain]
    except Exception:
        return None
    out = []
    z3 = symexec.z3
    for i in range(len(chain)):
        for j in range(i + 1, len(chain)):
            s = z3.Solver()
            s.set("timeout", 2000)
            s.add(*ex.base, *st.cons, exprs[i], exprs[j])
            if s.check() == z3.sat:
                out.append((chain[i], chain[j]))
    return out


def _review_preprocessor(C: _Collector, old: Program, new: Program, file_changes, changes, build_defines, P):
    files = []
    if file_changes:
        files = file_changes.get("modified", []) + file_changes.get("added", []) + file_changes.get("removed", [])
    any_dir = False
    any_region = False
    for rel in files:
        to = _read(old.root / rel) if (old.root / rel).is_file() else ""
        tn = _read(new.root / rel) if (new.root / rel).is_file() else ""
        do, dn = scan_directives(to), scan_directives(tn)
        for name in sorted(set(do["defines"]) | set(dn["defines"])):
            a, b = do["defines"].get(name), dn["defines"].get(name)
            if a == b:
                continue
            any_dir = True
            verb = "추가" if a is None else "삭제" if b is None else "변경"
            text = f"#define {name} " + (f"{a} → {b}" if verb == "변경" else (b if b is not None else a) or "")
            active = any(d.split("=")[0] == name.split("(")[0] for d in build_defines)
            C.add("8-1", REVIEW, f"매크로 {verb}: `{_short(text, 100)}`" + (" (이번 분석 빌드에서 -D 로도 지정됨)" if active else ""),
                  file=rel, reviewer="이 매크로 값이 실제 제품 설정(빌드 변형)과 일치하는지 확인하세요.")
            if "(" in name:
                C.add("6-2", REVIEW, f"함수형 매크로 {verb}: `{_short(text, 100)}`", file=rel,
                      reviewer="매크로를 쓰는 모든 위치에서 새 정의의 인자·평가 방식이 맞는지 확인하세요.")
            if P["feature_switch"].search(name) or (a or "").strip() in ("STD_ON", "STD_OFF", "TRUE", "FALSE", "0", "1") or \
                    (b or "").strip() in ("STD_ON", "STD_OFF", "TRUE", "FALSE", "0", "1"):
                C.add("8-2", REVIEW, f"기능 스위치 {verb}: `{_short(text, 100)}`", file=rel,
                      reviewer="스위치 변경으로 새로 켜지거나 꺼진 기능이 의도한 것인지, 적용 범위(빌드 변형)가 맞는지 확인하세요.")
        for label in sorted(set(do["conds"]) ^ set(dn["conds"])):
            any_dir = True
            C.add("8-1", REVIEW, f"조건부 컴파일 {'추가' if label in dn['conds'] else '삭제'}: `{_short(label, 100)}`", file=rel,
                  reviewer="이 조건이 각 빌드 설정에서 어떻게 평가되는지(활성 여부) 확인하세요.")
        for inc in sorted(set(do["includes"]) ^ set(dn["includes"])):
            C.add("6-5", CHANGE, f"#include {'추가' if inc in dn['includes'] else '삭제'}: {inc}", file=rel,
                  reviewer="추가·삭제된 헤더 의존성이 빌드 구성과 맞는지 확인하세요.")
        regions = dn["regions"]
        if not regions and not do["regions"]:
            continue
        any_region = True
        for c in changes:
            prog = new if c.version == "new" else old
            if _rel(prog, c.file) != rel:
                continue
            rg = regions if c.version == "new" else do["regions"]
            inside = [lab for s, e, lab in rg if s <= c.line <= e]
            if not inside:
                continue
            n = prog.nodes[c.node]
            kind = "오류 처리" if P["error_call"].search(n.text or "") else "초기화" if P["init_func"].search(prog.func_display(c.func) or "") else "실행"
            C.add("8-4", REVIEW, f"조건부 컴파일 블록 안의 {kind} 경로 변경: `{_short(n.text, 60)}`",
                  func=prog.func_display(c.func) if c.func else "", file=rel, line=c.line, evidence=inside[-2:],
                  reviewer="이 블록이 꺼진 빌드 설정에서의 동작도 요구된 정책과 맞는지 확인하세요.")
    if any_region or any_dir:
        C.add("8-4", INSUFFICIENT, "변경 파일에 조건부 컴파일 블록이 있음 — #else 등 이번 빌드에서 비활성인 경로는 AST 에 없어 분석하지 않음",
              reviewer="다른 빌드 설정(-D)으로 다시 분석하거나, 비활성 경로의 변경을 원문에서 직접 확인하세요.")
    C.note("8-1", f"변경 파일 {len(files)}개의 원문에서 #define·#if·#ifdef·#include 를 비교 (이번 빌드 -D: {', '.join(build_defines) or '없음'})")
    C.note("8-2", "기능 스위치(이름 패턴·STD_ON/OFF·0/1 값) 매크로의 추가·삭제·변경을 검사")
    C.note("8-4", "변경 문장이 #if/#ifdef 블록 안에 있는지 원문 줄 범위로 확인")
    if not any_dir:
        C.note("8-1", "매크로·조건부 컴파일 지시문 변경 없음")


def _summarize(C: _Collector) -> dict:
    items = []
    for num, grp, title, reviewer in ITEMS:
        fs = [f for f in C.findings if f["item"] == num]
        counts = {k: sum(1 for f in fs if f["class"] == k) for k in CLASSES}
        overall = next((k for k in CLASSES if counts[k]), NONE_FOUND)
        g = next(x for x in GROUPS if x[0] == grp)
        items.append({"id": num, "group": g[1], "algorithms": g[2], "title": title, "reviewer_focus": reviewer,
                      "checked": True, "overall": overall, "counts": counts, "notes": C.notes.get(num, []),
                      "findings": sorted(fs, key=lambda f: (CLASSES.index(f["class"]), f["file"], f["line"]))})
    total = {k: sum(1 for f in C.findings if f["class"] == k) for k in CLASSES}
    return {"items": items, "classes": [{"name": k, "desc": CLASS_DESC[k]} for k in CLASSES],
            "groups": [{"id": g[0], "name": g[1], "algorithms": g[2]} for g in GROUPS], "totals": total,
            "findings": C.findings}
