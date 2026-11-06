"""프로젝트 변수 이름에서 이름 규칙(review_patterns)을 찾는다.

검토 항목과 변경 설명은 변수·함수 이름으로 역할을 짐작한다 (예: 이름에 cnt 가 있으면 카운터).
프로젝트마다 약어가 달라서 (u8s_UVStepAdjRetryCnt, u8g_UVStepAdjErrSta, u8s_uvchangeflag, ...)
값을 쓰는 변수 이름을 모아 접두어(u8s_, u16g_, s_, g_ …)를 떼고 조각(단어)으로 나눈 뒤,
알려진 약어 사전과 맞춰 역할별 조각을 찾는다. 기본 패턴이 이미 잡는 조각은 빼고, 새로 필요한 것만 제안한다.
어느 역할에도 맞지 않는 자주 나오는 조각은 '분류되지 않은 이름 조각'으로 보여 주어 검증자가 정하게 한다.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from pathlib import Path

ROLE_TOKENS = {
    "counter": ["cnt", "count", "counter", "ctr", "retry", "rtry", "rty", "deb", "debounce", "attempt", "try", "num"],
    "timer": ["tmr", "timer", "tm", "tick", "dly", "delay", "time", "timeout", "tout", "period", "prd", "wait"],
    "error_state": ["err", "error", "fault", "flt", "fail", "dtc", "diag", "abn", "abnormal", "ng", "dem", "malf"],
    "flag": ["flg", "flag", "req", "ena", "enable", "lock", "lck", "act", "trig", "ack"],
}
_KNOWN_LONG = sorted({t for ts in ROLE_TOKENS.values() for t in ts if len(t) >= 3}, key=len, reverse=True)
ROLE_KO = {"counter": "카운터", "timer": "타이머", "error_state": "오류 상태", "flag": "플래그"}
PREFIX_RE = re.compile(r"^(?:[a-z]{0,3}\d{0,2}[sglc]?_|[sgl]_|[a-z]{1,3}\d{1,2}[A-Z])")
ASSIGN_RE = re.compile(r"\b([A-Za-z_]\w*)\s*(?:\[[^\]\n]*\])?\s*(?:\.\s*\w+\s*)?(?:=(?!=)|\+\+|--|\+=|-=|\|=|&=)")
DECL_RE = re.compile(r"^\s*(?:static\s+|volatile\s+|extern\s+|const\s+)*[A-Za-z_]\w*(?:\s*\*)?\s+([A-Za-z_]\w*)\s*(?:\[[^\]\n]*\])?\s*[=;]", re.M)
C_WORDS = {"if", "else", "for", "while", "return", "switch", "case", "do", "sizeof", "int", "char", "void", "unsigned",
           "signed", "short", "long", "static", "const", "volatile", "extern", "struct", "union", "enum", "typedef"}


def split_name(name: str) -> list[str]:
    n = PREFIX_RE.sub("", name) or name
    parts = []
    for chunk in re.split(r"_+", n):
        parts += re.findall(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|\d+", chunk)
    parts = [p for p in parts if len(p) >= 2 and not p.isdigit()]
    # 소문자로 붙여 쓴 이름 (uvchangeflag): 알려진 약어가 앞·뒤에 붙어 있으면 그 조각도 센다
    extra = []
    for p in parts:
        if p.islower() and len(p) >= 6:
            for t in _KNOWN_LONG:
                if p != t and (p.endswith(t) or p.startswith(t)):
                    extra.append(t)
    return parts + extra


def token_regex(tok: str) -> str:
    """이름 안의 단어 경계(밑줄·대소문자 바뀜)에 맞는 조각만 찾는 정규식 (대소문자 무시 패턴 안에서도 동작).

    Flt → u8s_UVFltSta 는 맞고 u8s_DacFltrVal 은 아님. 소문자로 붙여 쓴 이름(uvchangeflag)의 끝 조각도 맞는다.
    """
    low, cap, up = tok.lower(), tok[:1].upper() + tok[1:].lower(), tok.upper()
    alts = [rf"{re.escape(cap)}(?![a-z])",                                  # UVFltSta, Flt_x
            rf"(?:^|(?<=[_0-9a-z])){re.escape(low)}(?=$|_|[A-Z0-9])",        # u8s_flt, uvchangeflag
            rf"(?:^|(?<=[_0-9a-z])){re.escape(up)}(?=$|_|[0-9]|[A-Z][a-z])"]  # FLT_STA, UV_FLTSta
    return "(?-i:" + "|".join(dict.fromkeys(alts)) + ")"


def collect(files: dict[str, Path], limit: int = 6000) -> Counter:
    names: Counter = Counter()
    for i, (rel, p) in enumerate(files.items()):
        if i >= limit:
            break
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        text = re.sub(r"/\*.*?\*/|//[^\n]*", " ", text, flags=re.S)
        for m in ASSIGN_RE.finditer(text):
            names[m.group(1)] += 1
        for m in DECL_RE.finditer(text):
            names[m.group(1)] += 1
    for w in list(names):
        if w in C_WORDS or w.isupper():
            del names[w]
    return names


def suggest(files: dict[str, Path], current: dict[str, str], min_count: int = 2) -> dict:
    """{'patterns': {역할: 정규식}, 'roles': {역할: [(조각, 이름 수, 예)]}, 'unclassified': [(조각, 수, 예)], 'names': 수}."""
    names = collect(files)
    tok_names: dict[str, set] = defaultdict(set)
    for n in names:
        for t in split_name(n):
            tok_names[t.lower()].add(n)
    cur = {k: re.compile(v, re.I) for k, v in current.items() if v}
    roles, patterns = {}, {}
    known = {t for ts in ROLE_TOKENS.values() for t in ts}
    for role, toks in ROLE_TOKENS.items():
        found = []
        for t in toks:
            ns = tok_names.get(t, set())
            if len(ns) < min_count:
                continue
            missed = [n for n in ns if role not in cur or not cur[role].search(n)]
            found.append((t, len(ns), sorted(ns)[:3], len(missed)))
        roles[role] = [(t, k, ex) for t, k, ex, _ in found]
        new = [t for t, k, ex, missed in found if missed]
        if new:
            patterns[role] = "|".join(token_regex(t) for t in new)
    unclassified = []
    for t, ns in sorted(tok_names.items(), key=lambda kv: -len(kv[1])):
        if t in known or len(ns) < max(3, min_count) or len(t) < 2:
            continue
        unclassified.append((t, len(ns), sorted(ns)[:3]))
        if len(unclassified) >= 30:
            break
    return {"patterns": patterns, "roles": roles, "unclassified": unclassified, "names": len(names)}
