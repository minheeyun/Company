"""보안 분석 규칙: Source(외부 입력) · Sink(위험 연산) · 보안 검사 조건.

규칙 파일은 YAML(PyYAML 필요) 또는 JSON 이다. 파일이 없으면 DEFAULT_RULES 를 쓴다.
함수 이름에는 glob(`Rte_Read_*`)을 쓸 수 있다. 인자 번호는 1부터 센다(-1 = 마지막 인자).

sources:
  - name: DIAG_INPUT
    functions: [process_diag]          # 이 함수들의 인자 전체를 외부 입력으로 본다 (args 로 좁힐 수 있음)
    args: [rx, len]                    # 선택: 인자 이름 또는 번호
  - name: CAN_INPUT
    out_args:                          # 호출한 뒤 출력 포인터 인자가 가리키는 값이 외부 입력
      - {functions: ["Rte_Read_*", "Rte_Receive_*"], args: [-1]}
    returns: ["Can_Read*"]             # 반환값이 외부 입력
sinks:
  - {name: MEMORY_COPY, functions: [memcpy, memmove], sensitive_argument: 3, dest_argument: 1}
  - {name: ARRAY_ACCESS, type: array_index}
security_checks:
  - {name: AUTHORIZATION, patterns: [SecurityLevel, Session], cwe: CWE-862}
"""

from __future__ import annotations

import fnmatch
import json
import re
from pathlib import Path

DEFAULT_RULES: dict = {
    "sources": [
        {"name": "DIAG_INPUT", "note": "UDS 진단 요청 데이터 (Dcm 콜아웃·DID 쓰기·루틴 제어)",
         "functions": ["*_WriteData", "*RoutineControl*", "Dcm_*Request*", "*_ProcessRequest", "process_diag",
                       "*SecurityAccess*CompareKey*", "*_CompareKey"]},
        {"name": "COM_INPUT", "note": "통신 수신 데이터 (CAN/LIN/Ethernet PDU)",
         "functions": ["*_RxIndication", "*_TpRxIndication", "*_CopyRxData", "*_StartOfReception"],
         "out_args": [{"functions": ["Rte_Read_*", "Rte_Receive_*", "Rte_DRead_*"], "args": [-1]},
                      {"functions": ["Com_ReceiveSignal", "Com_ReceiveSignalGroup", "Com_ReceiveShadowSignal"], "args": [2]},
                      {"functions": ["CanIf_ReadRxPduData", "PduR_*Receive*"], "args": [-1]}],
         "returns": ["Can_Read*", "Lin_Read*"]},
    ],
    "sinks": [
        {"name": "MEMORY_COPY", "functions": ["memcpy", "memmove", "*_memcpy", "*MemCopy", "*MemCpy", "Rte_MemCpy",
                                               "strncpy"], "sensitive_argument": 3, "dest_argument": 1},
        {"name": "MEMORY_WRITE", "functions": ["memset", "*_memset", "*MemSet", "*MemClear"],
         "sensitive_argument": -1, "dest_argument": 1},
        {"name": "ARRAY_ACCESS", "type": "array_index"},
        {"name": "POINTER_DEREF", "type": "pointer_deref"},
        {"name": "DIVISION", "type": "division"},
        {"name": "SHIFT", "type": "shift"},
        {"name": "NARROWING", "type": "narrowing"},
        {"name": "LOOP_BOUND", "type": "loop"},
        {"name": "FUNCTION_POINTER", "type": "function_pointer"},
    ],
    "security_checks": [
        {"name": "AUTHORIZATION", "cwe": "CWE-862", "label": "권한·세션 확인",
         "patterns": ["SecurityLevel", "SecLevel", "SecurityAccess", "Session", "SesCtrl", "Authentic", "Unlock",
                      "Privilege", "AccessRight", "Dcm_GetSecurity"]},
        {"name": "BRUTE_FORCE", "cwe": "CWE-307", "label": "실패 횟수·잠금 시간",
         "patterns": ["AttemptCnt", "AttemptCount", "FailCnt", "FailCount", "FailedAttempt", "DelayTime", "DelayTimer",
                      "LockTime", "LockTimer", "Lockout", "RetryCnt"]},
        {"name": "INTEGRITY", "cwe": "CWE-354", "label": "무결성·인증 검증 (OTA·보안 부팅·MAC)",
         "patterns": ["Crc", "Mac", "Cmac", "Signature", "Verify", "Hash", "Sha256", "Integrity", "SecureBoot",
                      "Freshness"]},
        {"name": "SECURITY_STATE", "cwe": "CWE-840", "label": "보안 상태 전이·NVM 보안 상태",
         "patterns": ["SecurityState", "SecState", "LockState", "Tamper", "ConfigurationLock", "DebugLock"]},
    ],
}

# 내장 위험 연산 종류와 규칙 type 이름
SINK_TYPES = {
    "array_index": "index", "pointer_deref": "deref", "division": "div", "shift": "shift",
    "narrowing": "narrow", "loop": "loop", "function_pointer": "fptr",
}


class Rules:
    def __init__(self, data: dict | None = None, path: str | None = None):
        data = data or DEFAULT_RULES
        self.path = path
        self.sources = data.get("sources") or []
        self.sinks = data.get("sinks") or []
        self.checks = data.get("security_checks") or []
        self.enabled_kinds = {SINK_TYPES[s["type"]] for s in self.sinks if s.get("type") in SINK_TYPES}
        self.length_calls = [(s.get("name", "MEMORY"), list(s.get("functions") or []), int(s.get("sensitive_argument", -1)),
                              int(s.get("dest_argument", 1)))
                             for s in self.sinks if s.get("functions")]
        if self.length_calls:
            self.enabled_kinds.add("length")
        self._check_res = [(c, re.compile("|".join(re.escape(p) for p in c.get("patterns") or ["^$"]), re.I))
                           for c in self.checks]

    # --- 이름 매칭 ---
    @staticmethod
    def match(name: str, patterns) -> bool:
        return any(fnmatch.fnmatchcase(name, p) for p in patterns or [])

    def length_rule(self, name: str):
        """복사·초기화 함수면 (규칙 이름, 길이 인자 번호, 목적지 인자 번호) — 1부터, -1 = 마지막."""
        for rname, funcs, arg, dest in self.length_calls:
            if self.match(name, funcs):
                return rname, arg, dest
        return None

    def param_sources(self, func_name: str):
        """함수 정의의 인자 중 외부 입력인 것: (Source 이름, 인자 이름·번호 목록 또는 None=전체)."""
        out = []
        for s in self.sources:
            if self.match(func_name, s.get("functions")):
                out.append((s.get("name", "SOURCE"), s.get("args")))
        return out

    def out_arg_sources(self, call_name: str):
        out = []
        for s in self.sources:
            for o in s.get("out_args") or []:
                if self.match(call_name, o.get("functions")):
                    out.append((s.get("name", "SOURCE"), o.get("args") or [-1]))
        return out

    def return_sources(self, call_name: str):
        return [s.get("name", "SOURCE") for s in self.sources if self.match(call_name, s.get("returns"))]

    def security_check(self, text: str):
        for c, rx in self._check_res:
            if rx.search(text):
                return c
        return None


def load_rules(path: str | None) -> Rules:
    if not path:
        return Rules()
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    if p.suffix.lower() in (".yaml", ".yml"):
        try:
            import yaml  # type: ignore
        except ImportError as e:
            raise ValueError("YAML 규칙 파일을 읽으려면 PyYAML 이 필요합니다: pip install pyyaml "
                             "(또는 같은 내용을 .json 으로 저장해 쓰세요)") from e
        data = yaml.safe_load(text) or {}
    else:
        data = json.loads(text)
    return Rules(data, str(p))
