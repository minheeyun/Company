"""파싱 실패 원인 분류.

전처리(gcc -E)·파싱(pycparser) 오류 메시지를 원인별로 나누고, 고칠 수 있는 것은 고칠 방법(-D 등)을 돌려준다.
  - 헤더 없음          fatal error: X.h: No such file or directory
  - #error             #if 사슬에서 고를 매크로가 정의되지 않음
  - 컴파일러 확장 키워드 __interrupt, __far, __no_init 처럼 C 표준에 없는 키워드 → -D 이름= 으로 지우면 파싱된다
  - 정의되지 않은 타입  uint16 x; 에서 uint16 의 typedef 가 없음 (헤더·설정 문제)
  - 구문 오류 / 전처리 오류  그 밖의 경우 (why_parse.py 로 추적)
detect_config.py(자동 탐지)와 folder_impact.py(보고서의 '파싱 실패 원인' 표)가 함께 쓴다.
"""

from __future__ import annotations

import os
import re
from collections import defaultdict
from pathlib import Path

ERROR_RE = re.compile(r"^(.*?):(\d+):(?:\d+:)?\s*error:\s*#error\s*(.*)$", re.M)
MISSING_RE = re.compile(r":\d+:\d+: fatal error: (.+?): No such file or directory")
PP_ERR_RE = re.compile(r"^(.*?):(\d+):(?:\d+:)?\s*(?:fatal )?error:\s*(.*)$", re.M)
PARSE_RE = re.compile(r"^(?:파싱 실패: )?(.+?):(\d+):(\d+): (.*)$", re.M)
MARKER_RE = re.compile(r'^#\s*(?:line\s+)?(\d+)\s+"((?:[^"\\]|\\.)*)"')
IDENT_RE = re.compile(r"\b[A-Za-z_]\w*\b")

# pycparser 가 아는 예약 이름 (지우면 안 됨)
RESERVED_OK = {
    "_Bool", "_Complex", "_Imaginary", "_Static_assert", "_Alignas", "_Alignof", "_Atomic", "_Noreturn",
    "_Thread_local", "_Pragma", "__func__", "__FILE__", "__LINE__", "__DATE__", "__TIME__", "__STDC__",
    "__STDC_VERSION__", "__builtin_va_list", "__builtin_offsetof", "__int128",
}
# 예약 이름이 아니어도 임베디드 컴파일러가 키워드로 쓰는 이름
KNOWN_EXT = {"interrupt", "far", "near", "huge", "sfr", "sfrb", "sfrw", "sbit", "bit", "code", "idata", "xdata",
             "pdata", "bdata", "data", "reentrant", "_interrupt", "_far", "_near"}
C_WORDS = {
    "auto", "break", "case", "char", "const", "continue", "default", "do", "double", "else", "enum", "extern",
    "float", "for", "goto", "if", "inline", "int", "long", "register", "restrict", "return", "short", "signed",
    "sizeof", "static", "struct", "switch", "typedef", "union", "unsigned", "void", "volatile", "while",
} | RESERVED_OK

CAUSE_ORDER = ["헤더 없음", "#error", "컴파일러 확장 키워드", "정의되지 않은 타입", "구문 오류", "전처리 오류", "기타"]


def _norm(p: str) -> str:
    return os.path.normcase(os.path.normpath(p.replace("\\\\", "\\")))


def pp_line(text: str, path: str, line: int) -> str:
    """전처리 결과에서 원본 (path, line) 에 해당하는 줄."""
    want, cur_file, cur = _norm(path), None, 0
    base = os.path.basename(want)
    for raw in text.splitlines():
        m = MARKER_RE.match(raw)
        if m:
            cur, cur_file = int(m.group(1)), _norm(m.group(2))
            continue
        if cur == line and cur_file and (cur_file == want or os.path.basename(cur_file) == base):
            return raw
        cur += 1
    return ""


def source_line(path: str, line: int) -> str:
    try:
        lines = Path(path.replace("\\\\", "\\")).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    return lines[line - 1] if 0 < line <= len(lines) else ""


def _is_ext(tok: str) -> bool:
    if tok in RESERVED_OK:
        return False
    return bool(re.match(r"^(__[A-Za-z0-9]|_[A-Z])", tok)) or tok in KNOWN_EXT


def keyword_fix(line_text: str, col: int, expanded: bool = True) -> tuple[str, str] | None:
    """오류 줄에서 지우면 파싱될 컴파일러 확장 키워드 (이름, -D 값).

    expanded=False(원본 줄)면 매크로 이름일 수 있으므로 __ 로 시작하는 이름만 본다.
    오류 위치 바로 앞(또는 그 위치)의 이름을 고르고, 문장 맨 앞의 함수 호출(__DI(); 등)은 정상이므로 고르지 않는다.
    """
    if not line_text:
        return None
    cands = []
    for m in IDENT_RE.finditer(line_text):
        tok = m.group(0)
        if not _is_ext(tok) or (not expanded and not tok.startswith("__")):
            continue
        rest = line_text[m.end():]
        fn = rest.lstrip().startswith("(")
        if fn and re.match(r"^\s*\([^()]*\)\s*;", rest) and re.search(r"(^|[{};])\s*$", line_text[:m.start()]):
            continue  # __DI(); — 문장으로 쓰인 내장 함수 호출
        cands.append((m.start(), tok, f"{tok}(...)=" if fn else f"{tok}="))
    if not cands:
        return None
    if col:  # pycparser 의 열은 1부터
        before = [c for c in cands if c[0] <= col]
        pick = max(before, key=lambda c: c[0]) if before else min(cands, key=lambda c: c[0])
    else:
        pick = cands[0]
    return pick[1], pick[2]


def undefined_type(msg: str, line_text: str, pp_text: str | None = None) -> str | None:
    """`uint16 z;` 처럼 typedef 가 없는 타입 이름."""
    if not line_text:
        return None
    m = re.search(r"before: (\S+)", msg)
    if m:
        pat = r"\b([A-Za-z_]\w*)\s*\**\s*" + re.escape(m.group(1)) + r"\b"
    else:
        pat = r"(?:^|[;{}(,]|\b(?:static|extern|const|volatile|register)\b)\s*([A-Za-z_]\w*)\s+\**\s*[A-Za-z_]\w*\s*[;=\[,)]"
    for m2 in re.finditer(pat, line_text):
        t = m2.group(1)
        if t in C_WORDS or _is_ext(t):
            continue
        if pp_text and re.search(r"\btypedef\b[^;]*\b" + re.escape(t) + r"\s*;", pp_text):
            continue
        return t
    return None


def classify(err: str, pp_text: str | None = None) -> dict:
    """오류 하나 → {cause, detail, where, fix(정의 -D 값 또는 None)}."""
    err = err or ""
    m = ERROR_RE.search(err)
    if m:
        return {"cause": "#error", "detail": m.group(3).strip().strip('"'), "where": f"{Path(m.group(1)).name}:{m.group(2)}",
                "fix": None, "path": m.group(1), "line": int(m.group(2))}
    m = MISSING_RE.search(err)
    if m:
        return {"cause": "헤더 없음", "detail": m.group(1).strip().strip('"<>'), "where": "", "fix": None}
    m = PARSE_RE.search(err) if "파싱 실패" in err or pp_text is not None else None
    if m and "error:" not in m.group(4):
        path, line, col, msg = m.group(1), int(m.group(2)), int(m.group(3)), m.group(4).strip()
        where = f"{Path(path).name}:{line}"
        if "Illegal character '@'" in msg:
            return {"cause": "컴파일러 확장 키워드", "detail": "@ (변수 주소 지정)", "where": where, "fix": None,
                    "line_text": source_line(path, line).strip()}
        lt = pp_line(pp_text, path, line) if pp_text else ""
        expanded = bool(lt)
        if not lt:
            lt = source_line(path, line)
        kf = keyword_fix(lt, col if expanded else 0, expanded)
        if kf:
            return {"cause": "컴파일러 확장 키워드", "detail": kf[0], "where": where, "fix": kf[1], "line_text": lt.strip()}
        ut = undefined_type(msg, lt, pp_text)
        if ut:
            return {"cause": "정의되지 않은 타입", "detail": ut, "where": where, "fix": None, "line_text": lt.strip()}
        return {"cause": "구문 오류", "detail": msg[:120], "where": where, "fix": None, "line_text": lt.strip()}
    m = PP_ERR_RE.search(err)
    if m:
        return {"cause": "전처리 오류", "detail": m.group(3).strip()[:120], "where": f"{Path(m.group(1)).name}:{m.group(2)}", "fix": None}
    first = next((ln for ln in err.splitlines() if ln.strip() and not ln.startswith("전처리 실패")), err[:120])
    return {"cause": "기타", "detail": first.strip()[:120], "where": "", "fix": None}


ADVICE = {
    "헤더 없음": "프로젝트 폴더에 없는 헤더입니다. 프로젝트 밖(MCAL·BSW·컴파일러 설치 폴더)에 있으면 --search \"폴더\" 로 찾을 위치를 주고, "
               "빌드 때 생성되는 파일이면 생성된 폴더를 -I 로 지정하세요.",
    "#error": "#if 조건에 필요한 매크로가 정의되지 않았습니다. 실제 빌드의 -D 값을 지정하세요 (--auto-config 가 #if 사슬에서 후보를 고릅니다).",
    "컴파일러 확장 키워드": "C 표준에 없는 컴파일러 키워드입니다. --auto-config 가 -D 이름= 으로 지웁니다. 남아 있으면 --strip 이름 으로 지정하세요.",
    "정의되지 않은 타입": "그 타입의 typedef 가 include 되지 않았습니다. 정의한 헤더의 폴더를 -I 로 맨 앞에 지정하거나 그 헤더를 고르는 -D 를 확인하세요.",
    "구문 오류": "파서가 이해하지 못한 구문입니다. python why_parse.py 프로젝트 파일 ... 로 원인 줄을 확인하세요.",
    "전처리 오류": "전처리기(gcc -E)가 처리하지 못했습니다. 오류 줄의 #if 식·#include 형식을 확인하세요.",
    "기타": "python why_parse.py 로 원인을 확인하세요.",
}


def find_typedef(name: str, headers: dict[str, Path], limit: int = 5) -> list[str]:
    pat = re.compile(r"\btypedef\b[^;]*\b" + re.escape(name) + r"\s*(\[[^\]]*\])?\s*;", re.S)
    hits = []
    for rel, p in headers.items():
        if not rel.lower().endswith(".h"):
            continue
        try:
            if pat.search(p.read_text(encoding="utf-8", errors="replace")):
                hits.append(rel)
        except OSError:
            continue
        if len(hits) >= limit:
            break
    return hits


def summarize(parse_errors: dict[str, str], headers: dict[str, Path] | None = None) -> list[dict]:
    """{파일: 오류} → 원인별 묶음 [{cause, detail, count, files, where, fix, advice, line_text, defined_in}] (많은 순)."""
    groups: dict[tuple[str, str], dict] = {}
    for f, err in parse_errors.items():
        c = classify(err)
        key = (c["cause"], c["detail"])
        g = groups.setdefault(key, {**c, "count": 0, "files": [], "advice": ADVICE.get(c["cause"], "")})
        g["count"] += 1
        g["files"].append(f)
    out = sorted(groups.values(), key=lambda g: (-g["count"], CAUSE_ORDER.index(g["cause"]) if g["cause"] in CAUSE_ORDER else 99))
    if headers:
        for g in out:
            if g["cause"] == "정의되지 않은 타입":
                g["defined_in"] = find_typedef(g["detail"], headers)
    return out


def by_cause(groups: list[dict]) -> dict[str, int]:
    d: dict[str, int] = defaultdict(int)
    for g in groups:
        d[g["cause"]] += g["count"]
    return dict(d)
