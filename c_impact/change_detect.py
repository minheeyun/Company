"""2단계: 변경점 탐지 — 텍스트 diff + AST 비교로 의미 있는 변경을 식별·분류한다.

- 함수: 추가/삭제/수정/이동/포맷만 변경, 시그니처(인자·반환형) 변경
- 함수 내부: 문장 시퀀스를 정규화 토큰으로 정렬(difflib)한 뒤 쌍별로 분류
  조건(경계 연산자, 관계/논리 연산자, 항 추가/삭제, 임계값, 부정, 우선순위), 반복 조건,
  분기 추가/삭제, 분기 순서, case 추가/삭제, 대입/상수, 호출 추가/삭제/인자, 반환, 지역변수,
  상태 전이, 타이머/카운터
- 전역변수(자료형/초기값/한정자), 매크로, typedef, 구조체, 열거형
- 주석/공백/포맷만 바뀐 부분과 비활성(#if) 영역 변경, 매핑되지 않은 변경 구분
"""

from __future__ import annotations

import difflib
import os

from .ast_extract import expr_text, iter_stmts
from .roles import comparisons, iter_expr, key_name, lvalue_key
from .textdiff import file_pairs, hunks, read_lines

COND_KINDS = ("if", "while", "do", "for", "switch")

LABELS = {
    "FUNCTION_ADDED": "함수 추가", "FUNCTION_REMOVED": "함수 삭제", "FUNCTION_MODIFIED": "함수 수정",
    "FUNCTION_MOVED": "함수 이동(의미 변경 없음)", "FORMAT_ONLY": "주석/공백/포맷 변경",
    "SIGNATURE_CHANGED": "함수 시그니처 변경", "PARAM_CHANGED": "함수 인자 변경", "RETURN_TYPE_CHANGED": "반환형 변경",
    "STORAGE_CHANGED": "링크(static) 변경",
    "GLOBAL_ADDED": "전역변수 추가", "GLOBAL_REMOVED": "전역변수 삭제", "GLOBAL_TYPE_CHANGED": "전역변수 자료형 변경",
    "GLOBAL_INIT_CHANGED": "전역변수 초기값 변경", "GLOBAL_QUALIFIER_CHANGED": "전역변수 한정자(volatile/const/static) 변경",
    "LOCAL_VAR_ADDED": "지역변수 추가", "LOCAL_VAR_REMOVED": "지역변수 삭제", "LOCAL_VAR_CHANGED": "지역변수 변경",
    "CONDITION_CHANGED": "조건문 변경", "BOUNDARY_OPERATOR": "경계 연산자 변경(> ↔ >= 등)",
    "RELATIONAL_OPERATOR": "관계 연산자 변경", "EQUALITY_OPERATOR": "동등 연산자 변경(== ↔ !=)",
    "LOGICAL_OPERATOR": "논리 연산자 변경(AND ↔ OR)", "CONDITION_TERM_ADDED": "조건 항 추가",
    "CONDITION_TERM_REMOVED": "조건 항 삭제", "THRESHOLD_CHANGED": "임계값/상수 변경",
    "CONDITION_OPERAND_CHANGED": "조건 피연산자 변경", "NEGATION_CHANGED": "조건 부정 변경",
    "PRECEDENCE_CHANGED": "조건 우선순위(괄호 구조) 변경", "ARITHMETIC_OPERATOR": "산술 연산자 변경",
    "LOOP_CONDITION_CHANGED": "반복문 진입/종료 조건 변경",
    "BRANCH_ADDED": "분기 추가", "BRANCH_REMOVED": "분기 삭제", "ELSE_ADDED": "else 경로 추가",
    "ELSE_REMOVED": "else 경로 삭제", "BRANCH_ORDER_CHANGED": "분기 순서/우선순위 변경",
    "CASE_ADDED": "switch case 추가", "CASE_REMOVED": "switch case 삭제", "CASE_VALUE_CHANGED": "case 값 변경",
    "ASSIGNMENT_ADDED": "대입문 추가", "ASSIGNMENT_REMOVED": "대입문 삭제", "ASSIGNMENT_CHANGED": "대입문/계산식 변경",
    "ASSIGNMENT_TARGET_CHANGED": "대입 대상 변경", "CONSTANT_CHANGED": "상수값 변경",
    "CALL_ADDED": "함수 호출 추가", "CALL_REMOVED": "함수 호출 삭제", "CALL_ARGS_CHANGED": "호출 인자 변경",
    "RETURN_CHANGED": "반환값 변경", "RETURN_ADDED": "return 추가", "RETURN_REMOVED": "return 삭제",
    "STATE_TRANSITION_CHANGED": "상태 전이 로직 변경", "TIMER_COUNTER_CHANGED": "타이머/카운터 로직 변경",
    "FAULT_LOGIC_CHANGED": "고장 감지/해제 로직 변경", "OUTPUT_LOGIC_CHANGED": "출력 로직 변경",
    "STATEMENT_ADDED": "문장 추가", "STATEMENT_REMOVED": "문장 삭제", "STATEMENT_CHANGED": "문장 변경",
    "JUMP_CHANGED": "break/continue/goto 변경",
    "MACRO_ADDED": "매크로 추가", "MACRO_REMOVED": "매크로 삭제", "MACRO_CHANGED": "매크로 정의 변경",
    "TYPEDEF_CHANGED": "typedef 변경", "TYPEDEF_ADDED": "typedef 추가", "TYPEDEF_REMOVED": "typedef 삭제",
    "STRUCT_CHANGED": "구조체 정의 변경", "ENUM_CHANGED": "열거형 정의 변경",
    "INACTIVE_CODE_CHANGED": "현재 빌드 설정에서 비활성(#if)인 코드 변경",
    "UNMAPPED_CHANGE": "AST 요소에 매핑되지 않은 변경", "UNPARSED_FILE_CHANGE": "파싱되지 않은 파일 변경",
}

CATEGORY = {
    "FUNCTION": "함수", "CONDITION": "조건", "VARIABLE": "변수", "STATE": "상태", "TIMER_COUNTER": "타이머/카운터",
    "CALL": "호출", "ASSIGNMENT": "대입/계산", "MACRO_TYPE": "매크로/자료형", "CONTROL": "제어 흐름",
    "FORMAT": "포맷", "UNKNOWN": "미분류",
}

_BOUNDARY = {frozenset((">", ">=")), frozenset(("<", "<="))}
_REL = {"<", ">", "<=", ">="}
_EQ = {"==", "!="}
_LOG = {"&&", "||"}


class ChangeSet:
    def __init__(self):
        self.changes: list[dict] = []
        self.files: list[dict] = []
        self.non_semantic: list[dict] = []
        self.functions: dict[str, dict] = {}  # fid -> 함수 단위 요약
        self.stmt_map: dict[str, dict] = {}  # fid -> {curr_sid: base_sid}
        self.warnings: list[str] = []

    def add(self, ch: dict) -> dict:
        ch["id"] = f"C{len(self.changes) + 1:03d}"
        ch.setdefault("semantic", True)
        ch["labels"] = [LABELS.get(t, t) for t in ch["types"]]
        self.changes.append(ch)
        return ch

    def to_dict(self):
        return {"files": self.files, "changes": self.changes, "non_semantic": self.non_semantic,
                "functions": self.functions, "warnings": self.warnings}


# ------------------------------------------------------------------------
def detect_changes(base, curr, base_roles, curr_roles, config, only_files: set[str] | None = None) -> ChangeSet:
    cs = ChangeSet()
    pairs = file_pairs(base.root, curr.root, config.exclude_globs)
    covered: dict[str, list] = {}  # rel -> [(side, l1, l2)] 의미 변경으로 설명된 줄
    for fp in pairs:
        if fp["status"] == "unchanged":
            continue
        if only_files and fp["file"] not in only_files:
            continue
        hk = hunks(fp["base"], fp["curr"])
        fp_out = {"file": fp["file"], "status": fp["status"], "hunks": len(hk),
                  "added_lines": sum(len(h["new"]) for h in hk), "removed_lines": sum(len(h["old"]) for h in hk),
                  "format_only_hunks": sum(1 for h in hk if h["format_only"]), "_hunks": hk}
        cs.files.append(fp_out)

    target_files = {f["file"] for f in cs.files}
    det = _Detector(base, curr, base_roles, curr_roles, cs, config, covered)
    det.functions(target_files)
    det.globals(target_files)
    det.macros_types(target_files)
    det.leftovers()
    for f in cs.files:
        f.pop("_hunks", None)
    return cs


class _Detector:
    def __init__(self, base, curr, br, cr, cs: ChangeSet, config, covered):
        self.b, self.c, self.br, self.cr, self.cs, self.cfg = base, curr, br, cr, cs, config
        self.covered = covered
        self._lines = {}

    # ------------------------------------------------------------ 보조
    def src(self, proj, rel, a, b) -> str:
        key = (proj.label, rel)
        if key not in self._lines:
            p = os.path.join(proj.root, rel)
            self._lines[key] = read_lines(p) if os.path.exists(p) else []
        lines = self._lines[key]
        return "\n".join(lines[max(a - 1, 0):max(b, a)])

    def cover(self, rel, side, a, b):
        self.covered.setdefault(rel, []).append((side, a, b))

    def loc(self, proj, fn_or_file, st=None, a=None, b=None):
        rel = fn_or_file["file"] if isinstance(fn_or_file, dict) else fn_or_file
        if st is not None:
            a = st["line"]
            b = st.get("hdr_end", st["end"]) if st["kind"] in COND_KINDS else st["end"]
        side = "base" if proj is self.b else "curr"
        self.cover(rel, side, a, b)
        return {"file": rel, "line": a, "end": b, "code": self.src(proj, rel, a, b)}

    # ---------------------------------------------------------- 함수
    def functions(self, target_files):
        bf, cf_ = self.b.functions, self.c.functions
        removed = [f for f in bf if f not in cf_ and bf[f]["file"] in target_files]
        added = [f for f in cf_ if f not in bf and cf_[f]["file"] in target_files]
        # static 함수가 다른 파일로 이동한 경우(이름·본문 동일)
        for r in list(removed):
            for a in list(added):
                if bf[r]["name"] == cf_[a]["name"] and bf[r]["norm_hash"] == cf_[a]["norm_hash"]:
                    self.cs.non_semantic.append({"type": "FUNCTION_MOVED", "function": a, "from": bf[r]["file"],
                                                 "to": cf_[a]["file"], "note": "static 함수 파일 이동, 본문 동일"})
                    self.cs.add({"types": ["FUNCTION_MOVED", "STORAGE_CHANGED"], "category": "FUNCTION",
                                 "function": a, "base_function": r,
                                 "base": self.loc(self.b, bf[r], a=bf[r]["line"], b=bf[r]["end_line"]),
                                 "curr": self.loc(self.c, cf_[a], a=cf_[a]["line"], b=cf_[a]["end_line"]),
                                 "description": f"static 함수 {bf[r]['name']} 가 {bf[r]['file']} → {cf_[a]['file']} 로 이동"
                                                " (심볼 ID 변경, 호출자 연결 확인 필요)"})
                    removed.remove(r)
                    added.remove(a)
                    break
        for fid in removed:
            fn = bf[fid]
            self.cs.add({"types": ["FUNCTION_REMOVED"], "category": "FUNCTION", "function": fid,
                         "base": self.loc(self.b, fn, a=fn["line"], b=fn["end_line"]), "curr": None,
                         "description": f"함수 {fn['name']} 삭제"})
            self.cs.functions[fid] = {"status": "removed"}
        for fid in added:
            fn = cf_[fid]
            self.cs.add({"types": ["FUNCTION_ADDED"], "category": "FUNCTION", "function": fid, "base": None,
                         "curr": self.loc(self.c, fn, a=fn["line"], b=fn["end_line"]),
                         "description": f"함수 {fn['name']} 추가"})
            self.cs.functions[fid] = {"status": "added"}
        for fid in sorted(set(bf) & set(cf_)):
            fb, fc = bf[fid], cf_[fid]
            if fb["file"] not in target_files and fc["file"] not in target_files:
                continue
            if fb["norm_hash"] == fc["norm_hash"]:
                if fb["raw_hash"] != fc["raw_hash"] and (
                        self._has_skipped(self.b, fb) or self._has_skipped(self.c, fc)):
                    continue  # 비활성(#if) 영역이 바뀐 경우: leftovers 에서 INACTIVE_CODE_CHANGED 로 보고
                if fb["raw_hash"] != fc["raw_hash"]:
                    self.cs.non_semantic.append({"type": "FORMAT_ONLY", "function": fid, "file": fc["file"],
                                                 "line": fc["line"],
                                                 "note": "주석/공백/포맷만 변경 (토큰 시퀀스 동일)"})
                    self.cover(fc["file"], "curr", fc["line"], fc["end_line"])
                    self.cover(fb["file"], "base", fb["line"], fb["end_line"])
                elif fb["line"] != fc["line"] or fb["file"] != fc["file"]:
                    self.cs.non_semantic.append({"type": "FUNCTION_MOVED", "function": fid,
                                                 "from": f"{fb['file']}:{fb['line']}", "to": f"{fc['file']}:{fc['line']}",
                                                 "note": "위치(라인)만 이동"})
                continue
            self._function_body(fid, fb, fc)

    def _cover_active(self, proj, fn, side):
        skip = [(a, b) for f, a, b in proj.skipped if f == fn["file"]]
        start = fn["line"]
        for a, b in sorted(skip):
            if b < fn["line"] or a > fn["end_line"]:
                continue
            if a > start:
                self.cover(fn["file"], side, start, a - 1)
            start = max(start, b + 1)
        if start <= fn["end_line"]:
            self.cover(fn["file"], side, start, fn["end_line"])

    @staticmethod
    def _has_skipped(proj, fn):
        return any(f == fn["file"] and a <= fn["end_line"] and b >= fn["line"] for f, a, b in proj.skipped)

    def _function_body(self, fid, fb, fc):
        n_before = len(self.cs.changes)
        # 함수 내부의 줄 변경은 아래 문장 단위 분류(또는 포맷 변경)로 모두 설명된다(비활성 #if 구간 제외)
        self._cover_active(self.b, fb, "base")
        self._cover_active(self.c, fc, "curr")
        summary = {"status": "modified", "changes": []}
        self.cs.functions[fid] = summary
        name = fc["name"]
        # 시그니처
        sig_types = []
        if fb["ret"]["spelling"] != fc["ret"]["spelling"]:
            sig_types.append("RETURN_TYPE_CHANGED")
        if [p["type"]["spelling"] for p in fb["params"]] != [p["type"]["spelling"] for p in fc["params"]] or \
                [p["name"] for p in fb["params"]] != [p["name"] for p in fc["params"]]:
            sig_types.append("PARAM_CHANGED")
        if fb["static"] != fc["static"]:
            sig_types.append("STORAGE_CHANGED")
        if sig_types:
            self.cs.add({"types": ["SIGNATURE_CHANGED"] + sig_types, "category": "FUNCTION", "function": fid,
                         "base": self.loc(self.b, fb, a=fb["line"], b=fb["line"]),
                         "curr": self.loc(self.c, fc, a=fc["line"], b=fc["line"]),
                         "description": f"{name}: {fb['sig']} → {fc['sig']}",
                         "detail": {"from": fb["sig"], "to": fc["sig"]}})
        if fb["file"] != fc["file"]:
            self.cs.non_semantic.append({"type": "FUNCTION_MOVED", "function": fid, "from": fb["file"],
                                         "to": fc["file"], "note": "다른 파일로 이동 + 본문 변경"})
        # 지역변수 (이름 기준)
        lb = {v["name"]: v for v in fb["locals"].values()}
        lc = {v["name"]: v for v in fc["locals"].values()}
        # 문장 정렬
        sb = [st for st in iter_stmts(fb["body"])]
        sc = [st for st in iter_stmts(fc["body"])]
        sig_b = [_sig(st) for st in sb]
        sig_c = [_sig(st) for st in sc]
        sm = difflib.SequenceMatcher(None, sig_b, sig_c, autojunk=False)
        smap = {}
        for tag, i1, i2, j1, j2 in sm.get_opcodes():
            if tag == "equal":
                for k in range(i2 - i1):
                    smap[sc[j1 + k]["id"]] = sb[i1 + k]["id"]
                    # else 존재 여부 변화는 시그니처에 없으므로 별도 확인
                    self._else_change(fid, fb, fc, sb[i1 + k], sc[j1 + k])
                continue
            olds = sb[i1:i2]
            news = sc[j1:j2]
            paired, rem, add = _pair(olds, news)
            for o, n in paired:
                smap[n["id"]] = o["id"]
                self._modified(fid, fb, fc, o, n)
            for grp in _runs(rem, sb):
                self._group(fid, fb, fc, grp, True, lb, lc)
            for grp in _runs(add, sc):
                self._group(fid, fb, fc, grp, False, lb, lc)
        self.cs.stmt_map[fid] = smap
        self._branch_order(fid, fb, fc)
        self._state_transitions(fid, fb, fc)
        new = self.cs.changes[n_before:]
        summary["changes"] = [c["id"] for c in new]
        if not new:
            # 토큰은 다르지만 문장 단위 차이가 없는 경우(선언부 속성 등)
            self.cs.add({"types": ["FUNCTION_MODIFIED"], "category": "FUNCTION", "function": fid,
                         "base": self.loc(self.b, fb, a=fb["line"], b=fb["end_line"]),
                         "curr": self.loc(self.c, fc, a=fc["line"], b=fc["end_line"]),
                         "description": f"{name}: 문장 단위로 분류되지 않은 토큰 변경"})
            summary["changes"] = [self.cs.changes[-1]["id"]]

    def _else_change(self, fid, fb, fc, o, n):
        if o["kind"] != "if":
            return
        ob, nb = o.get("else") is not None, n.get("else") is not None
        if ob == nb:
            return
        t = "ELSE_ADDED" if nb else "ELSE_REMOVED"
        self.cs.add({"types": [t, "BRANCH_ADDED" if nb else "BRANCH_REMOVED"], "category": "CONTROL",
                     "function": fid, "base": self.loc(self.b, fb, o), "curr": self.loc(self.c, fc, n),
                     "base_stmt": o["id"], "curr_stmt": n["id"],
                     "description": f"{fc['name']}: '{n['text']}' 의 else 경로 {'추가' if nb else '삭제'}"})

    # ------------------------------------------------- 문장 쌍 분류
    def _modified(self, fid, fb, fc, o, n):
        types, detail = [], {}
        cat = "ASSIGNMENT"
        if o["kind"] in COND_KINDS and n["kind"] == o["kind"]:
            ct, detail = compare_conditions(o.get("cond"), n.get("cond"))
            types = ["CONDITION_CHANGED"] + ct
            if o["kind"] in ("while", "do", "for"):
                types.append("LOOP_CONDITION_CHANGED")
                if o["kind"] == "for" and o["text"] != n["text"] and not ct:
                    types.append("STATEMENT_CHANGED")
            cat = "CONDITION"
        elif o["kind"] == "case" and n["kind"] == "case":
            types = ["CASE_VALUE_CHANGED"]
            detail = {"from": o.get("value_text"), "to": n.get("value_text")}
            cat = "CONDITION"
        else:
            types, detail, cat = self._compare_simple(fid, fb, fc, o, n)
        types += self._role_types(fid, o, n)
        cat = _category(types, cat)
        self.cs.add({"types": _uniq(types), "category": cat, "function": fid,
                     "base": self.loc(self.b, fb, o), "curr": self.loc(self.c, fc, n),
                     "base_stmt": o["id"], "curr_stmt": n["id"], "detail": detail,
                     "description": _describe(fc["name"], types, o, n, detail)})

    def _compare_simple(self, fid, fb, fc, o, n):
        types, detail = [], {}
        cat = "ASSIGNMENT"
        calls_o = _call_names(fb, o)
        calls_n = _call_names(fc, n)
        if calls_o != calls_n:
            add = sorted(set(calls_n) - set(calls_o))
            rem = sorted(set(calls_o) - set(calls_n))
            if add:
                types.append("CALL_ADDED")
            if rem:
                types.append("CALL_REMOVED")
            detail["calls_added"], detail["calls_removed"] = add, rem
            cat = "CALL"
        elif calls_o:
            ao = [fb["calls"][c]["args"] for c in o.get("calls", [])]
            an = [fc["calls"][c]["args"] for c in n.get("calls", [])]
            if ao != an:
                types.append("CALL_ARGS_CHANGED")
                detail["args_from"], detail["args_to"] = ao, an
                cat = "CALL"
        if o["kind"] == "return" or n["kind"] == "return":
            if o["kind"] == n["kind"]:
                types.append("RETURN_CHANGED")
            else:
                types.append("RETURN_ADDED" if n["kind"] == "return" else "RETURN_REMOVED")
            cat = "ASSIGNMENT" if cat != "CALL" else cat
        if o["kind"] == "decl" or n["kind"] == "decl":
            types.append("LOCAL_VAR_CHANGED")
            cat = "VARIABLE"
        do = [key_name(k) for k in o.get("defs", [])]
        dn = [key_name(k) for k in n.get("defs", [])]
        eo, en = o.get("expr"), n.get("expr")
        if do or dn:
            if do != dn:
                types.append("ASSIGNMENT_TARGET_CHANGED")
            co = [x.get("v") for x in iter_expr(eo) if x.get("k") == "const"]
            cn = [x.get("v") for x in iter_expr(en) if x.get("k") == "const"]
            if co != cn and len(co) == len(cn):
                types.append("CONSTANT_CHANGED")
                detail["const_from"], detail["const_to"] = co, cn
            ops_o = [x["op"] for x in iter_expr(eo) if x.get("k") in ("bin", "assign", "un")]
            ops_n = [x["op"] for x in iter_expr(en) if x.get("k") in ("bin", "assign", "un")]
            if ops_o != ops_n:
                types.append("ARITHMETIC_OPERATOR")
                detail["ops_from"], detail["ops_to"] = ops_o, ops_n
            if "CALL_ADDED" not in types and "CALL_REMOVED" not in types:
                types.append("ASSIGNMENT_CHANGED")
        if o["kind"] in ("break", "continue", "goto") or n["kind"] in ("break", "continue", "goto"):
            types.append("JUMP_CHANGED")
            cat = "CONTROL"
        if not types:
            types.append("STATEMENT_CHANGED")
        detail.setdefault("from", o["text"])
        detail.setdefault("to", n["text"])
        return types, detail, cat

    def _role_types(self, fid, o, n):
        r = set()
        if o:
            r |= set(self.br.stmt_roles(fid, o))
        if n:
            r |= set(self.cr.stmt_roles(fid, n))
        out = []
        st = n or o
        if st["kind"] in COND_KINDS:
            for side, proj_roles in ((o, self.br), (n, self.cr)):
                if not side:
                    continue
                for key in ("then", "else", "body"):
                    for s2 in iter_stmts(side.get(key) or []):
                        rr = proj_roles.stmt_roles(fid, s2)
                        if "STATE" in rr and any(proj_roles.is_state(d) for d in s2.get("defs", [])):
                            r.add("STATE")
                        if "FAULT" in rr and s2.get("defs"):
                            r.add("FAULT")
                        if "OUTPUT" in rr or proj_roles.calls_output(fid, s2):
                            r.add("OUTPUT")
        defs = (o or {}).get("defs", []) + (n or {}).get("defs", [])
        if "STATE" in r and (defs or st["kind"] in ("switch", "case") or st["kind"] in COND_KINDS):
            out.append("STATE_TRANSITION_CHANGED")
        if "TIMER_COUNTER" in r:
            out.append("TIMER_COUNTER_CHANGED")
        if "FAULT" in r:
            out.append("FAULT_LOGIC_CHANGED")
        if "OUTPUT" in r:
            out.append("OUTPUT_LOGIC_CHANGED")
        return out

    def _single_types(self, fid, fn, st, removed, lb, lc):
        k = st["kind"]
        types = []
        cat = "ASSIGNMENT"
        if k in COND_KINDS:
            types.append("BRANCH_REMOVED" if removed else "BRANCH_ADDED")
            if k in ("while", "do", "for"):
                types.append("LOOP_CONDITION_CHANGED")
            cat = "CONTROL"
        elif k in ("case", "default"):
            types.append("CASE_REMOVED" if removed else "CASE_ADDED")
            cat = "CONTROL"
        elif k == "decl":
            name = fn["locals"].get(st.get("decl"), {}).get("name") or key_name(st.get("decl") or "")
            other = lc if removed else lb
            types.append("LOCAL_VAR_REMOVED" if removed and name not in other else
                         "LOCAL_VAR_ADDED" if not removed and name not in other else "LOCAL_VAR_CHANGED")
            cat = "VARIABLE"
        elif k == "return":
            types.append("RETURN_REMOVED" if removed else "RETURN_ADDED")
        elif k in ("break", "continue", "goto", "label"):
            types.append("JUMP_CHANGED")
            cat = "CONTROL"
        if st.get("calls"):
            types.append("CALL_REMOVED" if removed else "CALL_ADDED")
            cat = "CALL" if cat == "ASSIGNMENT" else cat
        if st.get("defs") and k == "expr":
            types.append("ASSIGNMENT_REMOVED" if removed else "ASSIGNMENT_ADDED")
        if not types:
            types.append("STATEMENT_REMOVED" if removed else "STATEMENT_ADDED")
        roles = (self.br if removed else self.cr).stmt_roles(fid, st)
        if "STATE" in roles and (st.get("defs") or k in ("switch", "case")):
            types.append("STATE_TRANSITION_CHANGED")
        if "TIMER_COUNTER" in roles:
            types.append("TIMER_COUNTER_CHANGED")
        if "FAULT" in roles:
            types.append("FAULT_LOGIC_CHANGED")
        if "OUTPUT" in roles:
            types.append("OUTPUT_LOGIC_CHANGED")
        return types, cat

    def _group(self, fid, fb, fc, sts, removed, lb, lc):
        """연속으로 추가/삭제된 문장(중첩 문장, case 블록 포함)을 하나의 변경점으로 기록."""
        fn = fb if removed else fc
        proj = self.b if removed else self.c
        types, cats = [], []
        for st in sts:
            t, c = self._single_types(fid, fn, st, removed, lb, lc)
            types += t
            cats.append(c)
        cat = _category(types, cats[0])
        head = sts[0]
        a = min(st["line"] for st in sts)
        b = max(st.get("hdr_end", st["end"]) if st["kind"] in COND_KINDS and st is not head else st["end"]
                for st in sts)
        loc = self.loc(proj, fn, a=a, b=b)
        detail = {}
        calls = sorted({n for st in sts for n in _call_names(fn, st)})
        if calls:
            detail["calls"] = calls
        if removed:
            detail["removed_defs"] = sorted({d for st in sts for d in st.get("defs", [])})
        what = head["text"] if len(sts) == 1 else f"{head['text']} … (문장 {len(sts)}개)"
        ch = {"types": _uniq(types), "category": cat, "function": fid, "detail": detail,
              "base": loc if removed else None, "curr": None if removed else loc,
              "description": f"{fc['name']}: '{what}' {'삭제' if removed else '추가'}"}
        if removed:
            ch["base_stmt"] = head["id"]
            ch["base_stmts"] = [st["id"] for st in sts]
            ch["anchor_curr_stmt"] = self._anchor(fb, fc, sts[-1])
        else:
            ch["curr_stmt"] = head["id"]
            ch["curr_stmts"] = [st["id"] for st in sts]
        self.cs.add(ch)

    def _anchor(self, fb, fc, st):
        """삭제된 문장의 영향이 시작되는 현재 버전 위치(바로 다음 문장 또는 둘러싼 문장)."""
        lst = list(iter_stmts(fb["body"]))
        idx = next((i for i, s in enumerate(lst) if s["id"] == st["id"]), None)
        curr = list(iter_stmts(fc["body"]))
        sig_c = {}
        for s in curr:
            sig_c.setdefault(_sig(s), s["id"])
        if idx is not None:
            for s in lst[idx + 1:]:
                if _sig(s) in sig_c:
                    return sig_c[_sig(s)]
        return curr[-1]["id"] if curr else None

    def _branch_order(self, fid, fb, fc):
        cb = _else_if_chains(fb["body"])
        cc = _else_if_chains(fc["body"])
        for chain_c in cc:
            texts_c = [s["text"] for s in chain_c]
            for chain_b in cb:
                texts_b = [s["text"] for s in chain_b]
                if len(texts_b) == len(texts_c) and sorted(texts_b) == sorted(texts_c) and texts_b != texts_c:
                    self._absorb_moved(fid, chain_b, chain_c)
                    self.cs.add({"types": ["BRANCH_ORDER_CHANGED", "CONDITION_CHANGED"], "category": "CONDITION",
                                 "function": fid,
                                 "base": self.loc(self.b, fb, a=chain_b[0]["line"], b=chain_b[-1]["line"]),
                                 "curr": self.loc(self.c, fc, a=chain_c[0]["line"], b=chain_c[-1]["line"]),
                                 "curr_stmt": chain_c[0]["id"],
                                 "detail": {"order_from": texts_b, "order_to": texts_c},
                                 "description": f"{fc['name']}: else-if 체인의 평가 순서 변경"})

    def _absorb_moved(self, fid, chain_b, chain_c):
        """순서만 바뀐 else-if 체인 구성 문장의 추가/삭제 변경점은 '분기 순서 변경' 하나로 대체한다."""
        sig_b = {_sig(s) for s in iter_stmts(chain_b)}
        sig_c = {_sig(s) for s in iter_stmts(chain_c)}
        ids_b = {s["id"] for s in iter_stmts(chain_b)}
        ids_c = {s["id"] for s in iter_stmts(chain_c)}
        sb = {s["id"]: s for s in iter_stmts(chain_b)}
        sc = {s["id"]: s for s in iter_stmts(chain_c)}
        keep = []
        for ch in self.cs.changes:
            if ch.get("function") == fid:
                cs_ = ch.get("curr_stmts") or ([ch["curr_stmt"]] if ch.get("curr_stmt") is not None
                                               and ch.get("base_stmt") is None else [])
                bs_ = ch.get("base_stmts") or ([ch["base_stmt"]] if ch.get("base_stmt") is not None
                                               and ch.get("curr_stmt") is None else [])
                if cs_ and all(i in ids_c and _sig(sc[i]) in sig_b for i in cs_):
                    continue
                if bs_ and all(i in ids_b and _sig(sb[i]) in sig_c for i in bs_):
                    continue
            keep.append(ch)
        self.cs.changes[:] = keep
        for i, ch in enumerate(self.cs.changes):
            ch["id"] = f"C{i + 1:03d}"

    def _state_transitions(self, fid, fb, fc):
        tb = state_transitions(fb, self.br)
        tc = state_transitions(fc, self.cr)
        for var in sorted(set(tb) | set(tc)):
            a, b = tb.get(var, set()), tc.get(var, set())
            if a == b:
                continue
            added = sorted(b - a, key=str)
            removed = sorted(a - b, key=str)
            if not added and not removed:
                continue
            fn_detail = {"variable": key_name(var), "transitions_added": [list(x) for x in added],
                         "transitions_removed": [list(x) for x in removed]}
            self.cs.add({"types": ["STATE_TRANSITION_CHANGED"], "category": "STATE", "function": fid,
                         "base": self.loc(self.b, fb, a=fb["line"], b=fb["line"]),
                         "curr": self.loc(self.c, fc, a=fc["line"], b=fc["line"]), "detail": fn_detail,
                         "summary_only": True,
                         "description": f"{fc['name']}: 상태 변수 {key_name(var)} 전이 집합 변경 "
                                        f"(추가 {len(added)}, 삭제 {len(removed)})"})

    # ----------------------------------------------------------- 전역
    def globals(self, target_files):
        gb, gc = self.b.globals, self.c.globals
        for gid in sorted(set(gb) | set(gc)):
            o, n = gb.get(gid), gc.get(gid)
            f = (n or o)["file"]
            if f not in target_files:
                continue
            if o and not n:
                self.cs.add({"types": ["GLOBAL_REMOVED"], "category": "VARIABLE", "variable": o["key"],
                             "base": self.loc(self.b, f, a=o["line"], b=o["line"]), "curr": None,
                             "description": f"전역변수 {o['name']} 삭제"})
                continue
            if n and not o:
                self.cs.add({"types": ["GLOBAL_ADDED"], "category": "VARIABLE", "variable": n["key"], "base": None,
                             "curr": self.loc(self.c, f, a=n["line"], b=n["line"]),
                             "description": f"전역변수 {n['name']} 추가"})
                continue
            types, detail = [], {}
            if o["type"]["spelling"] != n["type"]["spelling"] or o["type"].get("bits") != n["type"].get("bits"):
                types.append("GLOBAL_TYPE_CHANGED")
                detail["type_from"], detail["type_to"] = o["type"]["spelling"], n["type"]["spelling"]
                if o["type"].get("len") != n["type"].get("len"):
                    detail["len_from"], detail["len_to"] = o["type"].get("len"), n["type"].get("len")
            if (o.get("init") or "") != (n.get("init") or ""):
                types.append("GLOBAL_INIT_CHANGED")
                detail["init_from"], detail["init_to"] = o.get("init"), n.get("init")
            if o["type"].get("volatile") != n["type"].get("volatile") or o["type"].get("const") != n["type"].get(
                    "const") or o["static"] != n["static"]:
                types.append("GLOBAL_QUALIFIER_CHANGED")
            if not types:
                continue
            if self.cr.is_state(n["key"]):
                types.append("STATE_TRANSITION_CHANGED")
            if self.cr.is_counter(n["key"]):
                types.append("TIMER_COUNTER_CHANGED")
            self.cs.add({"types": types, "category": "VARIABLE", "variable": n["key"], "detail": detail,
                         "function": n["id"].rsplit("::", 1)[0] if n.get("static_local") else None,
                         "base": self.loc(self.b, f, a=o["line"], b=o["line"]),
                         "curr": self.loc(self.c, f, a=n["line"], b=n["line"]),
                         "description": f"전역변수 {n['name']}: " + ", ".join(LABELS[t] for t in types)})

    # --------------------------------------------- 매크로 / 자료형
    def macros_types(self, target_files):
        mb, mc = self.b.macros, self.c.macros
        for key in sorted(set(mb) | set(mc)):
            o, n = mb.get(key), mc.get(key)
            f = (n or o)["file"]
            if f not in target_files:
                continue
            if o and n and o["body"] == n["body"] and o["params"] == n["params"]:
                continue
            t = "MACRO_CHANGED" if o and n else ("MACRO_ADDED" if n else "MACRO_REMOVED")
            m = n or o
            self.cs.add({"types": [t], "category": "MACRO_TYPE", "macro": key, "macro_name": m["name"],
                         "base": self.loc(self.b, f, a=o["line"], b=o["line"]) if o else None,
                         "curr": self.loc(self.c, f, a=n["line"], b=n["line"]) if n else None,
                         "detail": {"from": o and o["body"], "to": n and n["body"]},
                         "description": f"매크로 {m['name']}: '{o and o['body']}' → '{n and n['body']}'"})
        for kind, attr, cmp_key in (("TYPEDEF", "typedefs", "canon"), ("STRUCT", "records", "fields"),
                                    ("ENUM", "enums", "constants")):
            db, dc = getattr(self.b, attr), getattr(self.c, attr)
            for name in sorted(set(db) | set(dc)):
                o, n = db.get(name), dc.get(name)
                f = (n or o)["file"]
                if f not in target_files:
                    continue
                if o and n and o[cmp_key] == n[cmp_key]:
                    continue
                if kind == "TYPEDEF" and (not o or not n):
                    t = "TYPEDEF_ADDED" if n else "TYPEDEF_REMOVED"
                else:
                    t = f"{kind}_CHANGED"
                self.cs.add({"types": [t], "category": "MACRO_TYPE", "type_name": name, "type_kind": kind,
                             "base": self.loc(self.b, f, a=o["line"], b=o["line"]) if o else None,
                             "curr": self.loc(self.c, f, a=n["line"], b=n["line"]) if n else None,
                             "detail": {"from": o and o[cmp_key], "to": n and n[cmp_key]},
                             "description": f"{kind.lower()} {name} 정의 변경"})

    # ------------------------------------------------------- 나머지
    def leftovers(self):
        """의미 변경으로 설명되지 않은 diff 줄: 포맷 변경 / 비활성 코드 / 미매핑."""
        skipped = {"base": self.b.skipped, "curr": self.c.skipped}
        unparsed = set(self.c.unparsed_headers) | set(self.b.unparsed_headers)
        for f in self.cs.files:
            rel = f["file"]
            cov = self.covered.get(rel, [])
            for h in f["_hunks"]:
                if h["format_only"]:
                    self.cs.non_semantic.append({"type": "FORMAT_ONLY", "file": rel, "line": h["curr_start"],
                                                 "note": "주석/공백만 다른 hunk"})
                    continue
                spans = []
                if h["old"]:
                    spans.append(("base", h["base_start"], h["base_end"]))
                if h["new"]:
                    spans.append(("curr", h["curr_start"], h["curr_end"]))
                uncovered = []
                for side, a, b in spans:
                    for ln in range(a, b + 1):
                        if not any(s == side and x <= ln <= y for s, x, y in cov):
                            uncovered.append((side, ln))
                if not uncovered:
                    continue
                # 주석/공백 줄만 남았으면 무시
                lines_text = {("base", h["base_start"] + i): t for i, t in enumerate(h["old"])}
                lines_text.update({("curr", h["curr_start"] + i): t for i, t in enumerate(h["new"])})
                meaningful = [(s, ln) for s, ln in uncovered if _meaningful(lines_text.get((s, ln), ""))]
                if not meaningful:
                    continue
                inactive = [(s, ln) for s, ln in meaningful
                            if any(fr == rel and a <= ln <= b for fr, a, b in skipped[s])]
                pp = [(s, ln) for s, ln in meaningful if lines_text.get((s, ln), "").lstrip().startswith("#")]
                rest = [x for x in meaningful if x not in inactive and x not in pp]
                if inactive:
                    self.cs.add({"types": ["INACTIVE_CODE_CHANGED"], "category": "UNKNOWN", "file": rel,
                                 "base": {"file": rel, "line": h["base_start"], "end": h["base_end"],
                                          "code": "\n".join(h["old"])} if h["old"] else None,
                                 "curr": {"file": rel, "line": h["curr_start"], "end": h["curr_end"],
                                          "code": "\n".join(h["new"])} if h["new"] else None,
                                 "description": f"{rel}: 현재 빌드 설정({self.cfg.build_config_name})에서 "
                                                "전처리기로 제외된 코드가 변경됨 — 다른 빌드 설정에서 영향 가능"})
                if rest or (pp and not inactive):
                    t = "UNPARSED_FILE_CHANGE" if rel in unparsed else "UNMAPPED_CHANGE"
                    self.cs.add({"types": [t], "category": "UNKNOWN", "file": rel,
                                 "base": {"file": rel, "line": h["base_start"], "end": h["base_end"],
                                          "code": "\n".join(h["old"])} if h["old"] else None,
                                 "curr": {"file": rel, "line": h["curr_start"], "end": h["curr_end"],
                                          "code": "\n".join(h["new"])} if h["new"] else None,
                                 "description": f"{rel}: AST 요소(함수/전역/매크로/자료형)로 설명되지 않는 변경 — 수동 확인 필요"})


# ------------------------------------------------------------------------
def _meaningful(line: str) -> bool:
    from .textdiff import squash
    return bool(squash(line))


def _uniq(xs):
    out = []
    for x in xs:
        if x not in out:
            out.append(x)
    return out


def _category(types, default):
    if "STATE_TRANSITION_CHANGED" in types:
        return "STATE"
    if "TIMER_COUNTER_CHANGED" in types:
        return "TIMER_COUNTER"
    if "CONDITION_CHANGED" in types or "BRANCH_ORDER_CHANGED" in types:
        return "CONDITION"
    return default


def _sig(st):
    return (st["kind"], st["text"])


def _pair(olds, news):
    """replace 블록 안에서 종류가 같고 텍스트가 가장 비슷한 문장끼리 짝짓는다."""
    paired, used = [], set()
    for o in olds:
        best, best_r = None, 0.0
        for j, n in enumerate(news):
            if j in used:
                continue
            same = o["kind"] == n["kind"] or {o["kind"], n["kind"]} <= {"expr", "decl", "return"}
            if not same:
                continue
            r = difflib.SequenceMatcher(None, o["text"].split(), n["text"].split()).ratio()
            if o["kind"] == n["kind"]:
                r += 0.25
            if r > best_r:
                best, best_r = j, r
        if best is not None and best_r >= 0.5:
            used.add(best)
            paired.append((o, news[best]))
    rem = [o for o in olds if all(o is not p[0] for p in paired)]
    add = [n for j, n in enumerate(news) if j not in used]
    return paired, rem, add


def _runs(items, seq):
    """seq(전위 순서) 에서 연속된 항목끼리 묶는다."""
    pos = {id(st): i for i, st in enumerate(seq)}
    out = []
    for st in sorted(items, key=lambda x: pos[id(x)]):
        if out and pos[id(st)] == pos[id(out[-1][-1])] + 1:
            out[-1].append(st)
        else:
            out.append([st])
    return out


def _call_names(fn, st):
    return sorted(fn["calls"][c].get("name") or "?" for c in st.get("calls", []))


def _else_if_chains(body):
    out = []
    for st in iter_stmts(body):
        if st["kind"] == "if" and st.get("else_if") and not st.get("_in_chain"):
            chain = [st]
            cur = st
            while cur.get("else_if") and cur.get("else"):
                nxt = cur["else"][0]
                nxt["_in_chain"] = True
                chain.append(nxt)
                cur = nxt
            if len(chain) >= 2:
                out.append(chain)
    for st in iter_stmts(body):
        st.pop("_in_chain", None)
    return out


# ------------------------------------------------------------- 조건 비교
def _logic_terms(e, op=None):
    """&&/|| 체인을 항 목록으로 평탄화."""
    if e and e.get("k") == "bin" and e["op"] in _LOG and (op is None or e["op"] == op):
        return _logic_terms(e["l"], e["op"]) + _logic_terms(e["r"], e["op"])
    return [e]


def _shape(e):
    if not e:
        return None
    k = e.get("k")
    if k == "bin":
        cls = "R" if e["op"] in _REL else "E" if e["op"] in _EQ else "L" if e["op"] in _LOG else "A"
        return (cls, _shape(e["l"]), _shape(e["r"]))
    if k == "un":
        return ("U" + e["op"], _shape(e["e"]))
    if k == "cast":
        return _shape(e["e"])
    if k == "const":
        return "C"
    return "V"


def compare_conditions(eo, en) -> tuple[list[str], dict]:
    types, detail = [], {"from": expr_text(eo), "to": expr_text(en)}
    if eo is None or en is None:
        return (["CONDITION_TERM_ADDED"] if en else ["CONDITION_TERM_REMOVED"]), detail
    detail["comparisons_from"] = _cmp_summary(eo)
    detail["comparisons_to"] = _cmp_summary(en)
    if _shape(eo) == _shape(en):
        _walk_same(eo, en, types, detail)
    else:
        no = _strip_not(eo)
        nn = _strip_not(en)
        if expr_text(no[0]) == expr_text(nn[0]) and no[1] != nn[1]:
            types.append("NEGATION_CHANGED")
        else:
            to = _atoms(eo)
            tn = _atoms(en)
            so = {expr_text(t) for t in to}
            sn = {expr_text(t) for t in tn}
            ops_o = sorted(x["op"] for x in iter_expr(eo) if x.get("k") == "bin" and x["op"] in _LOG)
            ops_n = sorted(x["op"] for x in iter_expr(en) if x.get("k") == "bin" and x["op"] in _LOG)
            if so == sn:
                types.append("LOGICAL_OPERATOR" if ops_o != ops_n else "PRECEDENCE_CHANGED")
            elif sn > so:
                types.append("CONDITION_TERM_ADDED")
                detail["terms_added"] = sorted(sn - so)
            elif so > sn:
                types.append("CONDITION_TERM_REMOVED")
                detail["terms_removed"] = sorted(so - sn)
            else:
                if len(to) == len(tn) and ops_o != ops_n:
                    types.append("LOGICAL_OPERATOR")
                if len(tn) > len(to):
                    types.append("CONDITION_TERM_ADDED")
                elif len(tn) < len(to):
                    types.append("CONDITION_TERM_REMOVED")
                detail["terms_added"] = sorted(sn - so)
                detail["terms_removed"] = sorted(so - sn)
                # 같은 위치의 원자 조건끼리 다시 비교(연산자/임계값 변화)
                for x, y in zip(to, tn):
                    if _shape(x) == _shape(y) and expr_text(x) != expr_text(y):
                        _walk_same(x, y, types, detail)
    # 상수 변화(구조가 달라도 비교식 단위로 확인)
    _threshold_by_var(detail, types)
    return _uniq(types), detail


def _atoms(e):
    """&&/|| 로 연결된 원자 조건 목록(연산자 종류와 무관하게 평탄화)."""
    if e and e.get("k") == "bin" and e["op"] in _LOG:
        return _atoms(e["l"]) + _atoms(e["r"])
    return [e]


def _strip_not(e):
    neg = False
    while e and e.get("k") == "un" and e["op"] == "!":
        neg = not neg
        e = e["e"]
    return e, neg


def _cmp_summary(e):
    out = []
    for c in comparisons(e):
        lk = lvalue_key(c["lhs"])
        out.append({"var": key_name(lk) if lk else expr_text(c["lhs"]), "key": lk, "op": c["op"],
                    "value": c["const"], "const_text": c["const_text"], "type": c["lhs"].get("t", {})})
    return out


def _threshold_by_var(detail, types):
    by_o = {(c["var"], c["op"]): c for c in detail.get("comparisons_from", [])}
    for c in detail.get("comparisons_to", []):
        o = by_o.get((c["var"], c["op"]))
        if o and o["value"] != c["value"] and o.get("const_text") != c.get("const_text"):
            types.append("THRESHOLD_CHANGED")
            detail.setdefault("thresholds", []).append({"var": c["var"], "from": o["value"], "to": c["value"],
                                                        "op": c["op"]})


def _walk_same(a, b, types, detail):
    ka = a.get("k")
    if ka == "bin":
        if a["op"] != b["op"]:
            pair = frozenset((a["op"], b["op"]))
            if pair in _BOUNDARY:
                types.append("BOUNDARY_OPERATOR")
            elif a["op"] in _REL and b["op"] in _REL:
                types.append("RELATIONAL_OPERATOR")
            elif a["op"] in _EQ and b["op"] in _EQ:
                types.append("EQUALITY_OPERATOR")
            elif a["op"] in _LOG and b["op"] in _LOG:
                types.append("LOGICAL_OPERATOR")
            else:
                types.append("ARITHMETIC_OPERATOR")
            detail.setdefault("operators", []).append({"from": a["op"], "to": b["op"], "at_from": expr_text(a),
                                                       "at_to": expr_text(b)})
        _walk_same(a["l"], b["l"], types, detail)
        _walk_same(a["r"], b["r"], types, detail)
    elif ka == "un":
        _walk_same(a["e"], b["e"], types, detail)
    elif ka == "cast":
        _walk_same(a["e"], b["e"], types, detail)
    elif ka == "const":
        if a.get("text") == b.get("text") and str(a.get("text") or "")[:1].isalpha():
            return  # 같은 매크로/열거 상수 이름 — 값 변화는 매크로 변경점으로 따로 보고
        if a.get("v") != b.get("v") or a.get("text") != b.get("text"):
            types.append("THRESHOLD_CHANGED")
            detail.setdefault("constants", []).append({"from": a.get("v"), "to": b.get("v"),
                                                       "text_from": a.get("text"), "text_to": b.get("text")})
    elif expr_text(a) != expr_text(b):
        types.append("CONDITION_OPERAND_CHANGED")
        detail.setdefault("operands", []).append({"from": expr_text(a), "to": expr_text(b)})


# --------------------------------------------------------- 상태 전이
def state_transitions(fn, roles) -> dict[str, set]:
    """switch(state) { case S: ... state = T; } 구조에서 (S → T) 전이 집합 추출."""
    out: dict[str, set] = {}
    for st in iter_stmts(fn["body"]):
        if st["kind"] != "switch" or not st.get("cond"):
            continue
        sk = lvalue_key(st["cond"])
        if not sk or not roles.is_state(sk):
            continue
        cur_labels = []
        for s2 in st.get("body", []):
            if s2["kind"] in ("case", "default"):
                if cur_labels and cur_labels[-1] == "__body__":
                    cur_labels = []
                cur_labels = [x for x in cur_labels if x != "__body__"]
                cur_labels.append(s2.get("value_text") if s2["kind"] == "case" else "default")
                continue
            if cur_labels and cur_labels[-1] != "__body__":
                cur_labels.append("__body__")
            for s3 in iter_stmts([s2]):
                if sk in s3.get("defs", []) and s3.get("expr") and s3["expr"].get("k") == "assign":
                    tgt = expr_text(s3["expr"]["r"])
                    for lab in cur_labels:
                        if lab != "__body__":
                            out.setdefault(sk, set()).add((lab, tgt))
    # switch 구조가 없는 상태 변수: 조건부 대입을 전이로 기록
    for st in iter_stmts(fn["body"]):
        if st["kind"] == "if":
            if any(d in out for s3 in iter_stmts(st.get("then", [])) for d in s3.get("defs", [])):
                continue
            for s3 in iter_stmts(st.get("then", [])):
                for d in s3.get("defs", []):
                    if roles.is_state(d) and s3.get("expr") and s3["expr"].get("k") == "assign" and \
                            s3["expr"]["l"].get("k") in ("var", "member"):
                        out.setdefault(d, set()).add(("(조건부)", expr_text(s3["expr"]["r"])))
    return out


def _describe(fname, types, o, n, detail) -> str:
    main = LABELS.get(types[0], types[0]) if types else "변경"
    extra = []
    for op in detail.get("operators", [])[:2]:
        extra.append(f"연산자 {op['from']} → {op['to']}")
    for th in detail.get("thresholds", [])[:2]:
        extra.append(f"{th['var']} 임계값 {th['from']} → {th['to']}")
    for c in detail.get("constants", [])[:2]:
        if not detail.get("thresholds"):
            extra.append(f"상수 {c['text_from']} → {c['text_to']}")
    if detail.get("const_from") is not None and detail.get("const_to") is not None:
        extra.append(f"상수 {detail['const_from']} → {detail['const_to']}")
    if detail.get("calls_added"):
        extra.append("호출 추가 " + ", ".join(detail["calls_added"]))
    if detail.get("calls_removed"):
        extra.append("호출 삭제 " + ", ".join(detail["calls_removed"]))
    if detail.get("args_to") is not None:
        extra.append(f"인자 {detail.get('args_from')} → {detail['args_to']}")
    s = f"{fname}: {main} — '{o['text']}' → '{n['text']}'"
    if extra:
        s += " (" + "; ".join(extra) + ")"
    return s
