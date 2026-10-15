"""외부 정적 분석 도구 결과를 수집해 후보의 근거로 붙인다 (선택).

- gcc 경고: -Wtype-limits(항상 참/거짓 비교), -Wconversion(값 손실), -Woverflow, -Wmaybe-uninitialized 등
  ※ 호스트 gcc는 호스트 자료형 크기(int 32bit 등)로 판단한다. 대상과 다르면 --tool-cc로 교차 컴파일러를 지정한다.
- gcc -fanalyzer (--gcc-analyzer): 경로 기반 분석 (느림)
- cppcheck (설치되어 있고 --cppcheck 지정 시)

같은 파일·라인(±1)의 후보에는 tool_evidence로 붙여 확실성을 보강하고,
어느 후보에도 대응하지 않는 경고는 TOOL_WARNING 후보로 추가한다.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from . import cfront as C
from .checks import RULES, Candidate


@dataclass
class ToolFinding:
    tool: str
    file: str
    line: int
    severity: str
    message: str
    flag: str = ""

    def render(self) -> str:
        return f"{self.tool}: {self.file}:{self.line}: {self.message}" + (f" [{self.flag}]" if self.flag else "")


GCC_WARN = ["-fsyntax-only", "-Wall", "-Wextra", "-Wconversion", "-Wtype-limits", "-Wshadow", "-Wswitch-enum",
            "-Wuninitialized", "-Wmaybe-uninitialized", "-Woverflow", "-Wdiv-by-zero", "-O1"]
_DIAG = re.compile(r"^(.*?):(\d+):(?:(\d+):)?\s*(warning|error|note):\s*(.*?)(?:\s+\[(-W[^\]]+|CWE-\d+)\])?$")
_NOISE = re.compile(r"-Wunused|-Wimplicit-function-declaration|-Wbuiltin-declaration|-Wmissing-|-Wint-conversion|"
                    r"-Wpedantic|-Wsign-conversion|-Wshadow")


def run_gcc(model: C.CodeModel, cc: str = "gcc", analyzer: bool = False) -> tuple[list[ToolFinding], str]:
    exe = shutil.which(cc)
    if not exe:
        return [], f"{cc} 없음"
    out: list[ToolFinding] = []
    errors = 0
    with tempfile.TemporaryDirectory(prefix="specverify_tool_") as tmp:
        flags, _ = C.compile_flags(model, Path(tmp))
        sign = ["-fsigned-char"] if model.env.target.char_signed else ["-funsigned-char"]
        extra = ["-fanalyzer"] if analyzer else []
        for p in model._paths:  # type: ignore[attr-defined]
            if p.suffix.lower() != ".c":
                continue
            r = subprocess.run([exe] + flags + sign + GCC_WARN + extra + [str(p)], capture_output=True, text=True,
                               errors="replace")
            last: ToolFinding | None = None
            for line in r.stderr.splitlines():
                m = _DIAG.match(line.strip())
                if not m:
                    continue
                f = model._display(m.group(1))  # type: ignore[attr-defined]
                if m.group(4) == "note":
                    # 매크로 정의 위치로 보고된 경고는 실제 사용 위치(바깥쪽 확장)로 옮긴다
                    if last is not None and "in expansion of macro" in m.group(5) and f in model.files:
                        last.file, last.line = f, int(m.group(2))
                    continue
                last = None
                flag = m.group(6) or ""
                if m.group(4) == "error":
                    errors += 1  # 스텁 헤더로 인한 미선언 식별자 등: 대상 빌드 환경 문제이므로 결과에서 제외
                    continue
                if _NOISE.search(flag) or "specverify_" in m.group(1) or f not in model.files:
                    continue
                if "-Wconversion" in flag and "from 'int'" in m.group(5).replace("‘", "'").replace("’", "'"):
                    calls = _implicit_calls(model, f, int(m.group(2)))
                    if calls:
                        continue  # 선언 없는 함수의 암시적 int 반환으로 인한 경고
                last = ToolFinding("gcc" + (" -fanalyzer" if "analyzer" in flag else ""), f, int(m.group(2)),
                                   m.group(4), m.group(5), flag)
                out.append(last)
    note = f"{Path(exe).name} (호스트 자료형 기준: 대상 int {model.env.target.int_bits}bit와 다를 수 있음)"
    if errors:
        note += f"; 스텁 헤더로 인한 컴파일 오류 {errors}건 제외"
    return _dedupe(out), note


def _implicit_calls(model: C.CodeModel, file: str, line: int) -> list[str]:
    fn = model.function_at(file, line)
    if not fn:
        return []
    return [c.callee for c in fn.calls if c.line == line
            and model.find_function(c.callee) is None and c.callee not in model.prototypes]


def run_cppcheck(model: C.CodeModel) -> tuple[list[ToolFinding], str]:
    exe = shutil.which("cppcheck")
    if not exe:
        return [], "cppcheck 없음"
    files = [str(p) for p in model._paths if p.suffix.lower() == ".c"]  # type: ignore[attr-defined]
    platform = {16: "unix32", 32: "unix32", 64: "unix64"}.get(model.env.target.int_bits, "native")
    cmd = [exe, "--enable=warning,style,portability", "--inline-suppr", "--quiet", f"--platform={platform}",
           "--template={file}:{line}:{severity}:{id}:{message}"] + files
    r = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
    out = []
    for line in r.stderr.splitlines():
        parts = line.split(":", 4)
        if len(parts) == 5 and parts[1].isdigit():
            f = model._display(parts[0])  # type: ignore[attr-defined]
            if f in model.files:
                out.append(ToolFinding("cppcheck", f, int(parts[1]), parts[2], parts[4], parts[3]))
    return _dedupe(out), "cppcheck"


def _dedupe(items: list[ToolFinding]) -> list[ToolFinding]:
    seen, out = set(), []
    for t in items:
        k = (t.tool, t.file, t.line, t.message)
        if k not in seen:
            seen.add(k)
            out.append(t)
    return out


def attach(cands: list[Candidate], findings: list[ToolFinding]) -> list[Candidate]:
    """도구 결과를 후보에 붙이고, 대응 후보가 없는 경고는 새 후보로 추가한다."""
    extra = []
    for t in findings:
        hit = [c for c in cands if c.file == t.file and c.line == t.line]
        if hit:
            for c in hit:
                c.tool_evidence.append(t.render())
            continue
        cat = "LOGIC_ERROR" if "type-limits" in t.flag or "switch" in t.flag else "RUNTIME_ERROR"
        extra.append(Candidate("", "TOOL_WARNING", cat, RULES["TOOL_WARNING"][1], t.file, "-", t.line,
                               "PROBABLE" if t.severity == "error" else "POSSIBLE", "LOW", t.message,
                               tool_evidence=[t.render()]))
    allc = cands + extra
    allc.sort(key=lambda c: (c.file, c.line, c.rule))
    for i, c in enumerate(allc, 1):
        c.id = f"SC-{i:03d}"
    return allc
