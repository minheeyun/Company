"""SIL 차등 실행: 변경 전/후 소스를 같은 시나리오(입력·초기 상태·호출 순서)로 실행해 비교한다.

하네스는 대상 .c 파일을 직접 #include 하므로 static 변수·함수도 관찰·호출할 수 있다.
본문이 없는 외부 함수/변수는 링크 오류에서 이름을 찾아 자동으로 스텁을 만든다.
스텁은 호출 인자를 트레이스에 남기므로(외부 출력), 반환값은 cia_ret_<이름> 변수로 정한다.

시나리오(JSON) 예:
{
  "sources": ["timeout.c"],              # 버전 루트 기준 상대 경로
  "include_dirs": ["."], "defines": ["UNIT_TEST=1"], "cflags": ["-std=c99"],
  "observe": ["g_timeout", {"expr": "g_ratio", "fmt": "f"}],
  "params": {"tick": [99, 100, 199, 200], "raw": [[1001, 1001, 0]]},   # 리스트 안의 리스트 = 주기별 시퀀스
  "init": {"g_faultLatched": 0},
  "memory_map": [["0x40000000", "0x10000"]],   # 하드웨어 레지스터 주소를 호스트 메모리로 매핑 (Linux)
  "steps": [
    {"repeat": 3, "steps": [
      {"call": "UpdateTimeout", "args": ["${tick}"]},
      {"call": "UpdateDiagnostic"},
      {"set": {"g_x": 1}}, {"stub_return": {"Rte_Read_X": 5}}, {"stub_out": {"Rte_Read_X": {"0": 150}}},
      {"code": "/* 임의 C 문장 */"}
    ]},
    {"call": "ReadStatus", "record": true}
  ]
}
"""

from __future__ import annotations

import copy
import itertools
import json
import re
import shlex
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from pycparser import c_ast, c_generator

from .frontend import ROOT_TOKEN, BuildConfig, parse_tu

MAX_CASES = 2000
_UNDEF_RE = re.compile(r"undefined (?:reference to|symbol:?)\s*[`'\"]?_?([A-Za-z_]\w*)")
_PH_RE = re.compile(r"\$\{(\w+)\}")


class SilError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# 시나리오
# ---------------------------------------------------------------------------


def load_scenario(path: str | Path) -> dict:
    scn = json.loads(Path(path).read_text(encoding="utf-8"))
    if not scn.get("sources"):
        raise SilError("시나리오에 sources(대상 .c 파일 목록)가 필요합니다.")
    return scn


def expand_cases(scn: dict) -> list[tuple[str, dict]]:
    if scn.get("cases"):  # 명시적 케이스 목록 (explore 가 만든 시나리오 등)
        if len(scn["cases"]) > MAX_CASES:
            raise SilError(f"케이스가 {MAX_CASES}개를 넘습니다.")
        return [(c.get("name") or case_label(c["bindings"]), c["bindings"]) for c in scn["cases"]]
    params = scn.get("params") or {}
    if not params:
        return [("default", {})]
    names = list(params)
    combos = list(itertools.islice(itertools.product(*(params[n] for n in names)), MAX_CASES + 1))
    if len(combos) > MAX_CASES:
        raise SilError(f"케이스가 {MAX_CASES}개를 넘습니다. params 값 수를 줄이세요.")
    cases = []
    for combo in combos:
        b = dict(zip(names, combo))
        cases.append((case_label(b), b))
    return cases


def case_label(b: dict) -> str:
    return ", ".join(f"{k}={_fmt_val(v)}" for k, v in b.items()) or "default"


def _fmt_val(v) -> str:
    if isinstance(v, list):
        return "[" + ",".join(str(x) for x in v) + "]"
    return str(v)


# ---------------------------------------------------------------------------
# 하네스 생성
# ---------------------------------------------------------------------------


def _c_str(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ") + '"'


class _Emitter:
    def __init__(self, scn: dict, bindings: dict, case: int = 0):
        self.scn = scn
        self.b = bindings
        self.case = case
        self.lines: list[str] = []
        self.loop_depth = 0
        self.seq_decls: list[str] = []

    def sub(self, v) -> str:
        if isinstance(v, (int, float)):
            return repr(v)
        if not isinstance(v, str):
            raise SilError(f"지원하지 않는 값: {v!r}")

        def rep(m):
            name = m.group(1)
            if name not in self.b:
                raise SilError(f"params 에 없는 자리표시자: ${{{name}}}")
            val = self.b[name]
            if isinstance(val, list):  # 주기별 시퀀스: 가장 안쪽 repeat 반복 번호로 고른다
                arr = f"cia_seq{self.case}_{name}"
                decl = f"static const long long {arr}[] = {{{', '.join(str(x) for x in val)}}};"
                if decl not in self.seq_decls:
                    self.seq_decls.append(decl)
                idx = f"cia_i{self.loop_depth - 1}" if self.loop_depth else "0"
                return f"{arr}[({idx}) < {len(val)} ? ({idx}) : {len(val) - 1}]"
            return str(val)

        return _PH_RE.sub(rep, v)

    def emit_label(self, text: str):
        """단계 이름을 출력한다. 시퀀스 자리표시자는 실행 시점의 값으로 찍는다."""
        exprs = []

        def rep(m):
            v = self.b.get(m.group(1))
            if isinstance(v, list):
                exprs.append(self.sub(m.group(0)))
                return "%lld"
            return (m.group(0) if v is None else str(v)).replace("%", "%%")

        fmt = _PH_RE.sub(rep, text.replace("%", "%%"))
        args = "".join(f", (long long)({e})" for e in exprs)
        self.emit(f'printf("%d\\t#\\t{_c_str(fmt)[1:-1]}\\n", cia_step{args});')

    def emit(self, s: str):
        self.lines.append("    " * (self.loop_depth + 2) + s)

    def steps(self, steps: list):
        for st in steps:
            if "call" in st:
                fn = st["call"]
                args = ", ".join(self.sub(a) for a in st.get("args", []))
                label = st.get("label") or f"{fn}({', '.join(str(a) for a in st.get('args', []))})"
                self.emit("cia_step++;")
                self.emit_label(label)
                if st.get("record"):
                    if st.get("fmt") == "f":
                        self.emit(f'{{ double cia_r = (double){fn}({args}); printf("%d\\t%s\\t%.9g\\n", cia_step, "{fn}()", cia_r); }}')
                    else:
                        self.emit(f'{{ long long cia_r = (long long){fn}({args}); printf("%d\\t%s\\t%lld\\n", cia_step, "{fn}()", cia_r); }}')
                else:
                    self.emit(f"{fn}({args});")
                self.emit("cia_observe();")
            elif "set" in st:
                for k, v in st["set"].items():
                    self.emit(f"{k} = ({self.sub(v)});")
            elif "stub_return" in st:
                for k, v in st["stub_return"].items():
                    self.emit(f"cia_ret_{k} = ({self.sub(v)});")
            elif "stub_out" in st:
                for k, outs in st["stub_out"].items():
                    for idx, v in outs.items():
                        self.emit(f"cia_out_{k}_{idx} = ({self.sub(v)});")
            elif "repeat" in st:
                n = int(self.sub(st["repeat"])) if isinstance(st["repeat"], str) else int(st["repeat"])
                var = f"cia_i{self.loop_depth}"
                self.emit(f"for (int {var} = 0; {var} < {n}; {var}++) {{")
                self.loop_depth += 1
                self.steps(st.get("steps", []))
                self.loop_depth -= 1
                self.emit("}")
            elif "code" in st:
                self.emit(self.sub(st["code"]))
            else:
                raise SilError(f"알 수 없는 step: {st}")


def _observe_code(scn: dict) -> str:
    out = ["static void cia_observe(void)", "{"]
    for ob in scn.get("observe", []):
        if isinstance(ob, str):
            ob = {"expr": ob}
        expr, name, fmt = ob["expr"], ob.get("name") or ob["expr"], ob.get("fmt", "d")
        if fmt == "f":
            out.append(f'    printf("%d\\t%s\\t%.9g\\n", cia_step, {_c_str(name)}, (double)({expr}));')
        elif fmt == "x":
            out.append(f'    printf("%d\\t%s\\t0x%llX\\n", cia_step, {_c_str(name)}, (unsigned long long)({expr}));')
        elif fmt == "u":
            out.append(f'    printf("%d\\t%s\\t%llu\\n", cia_step, {_c_str(name)}, (unsigned long long)({expr}));')
        else:
            out.append(f'    printf("%d\\t%s\\t%lld\\n", cia_step, {_c_str(name)}, (long long)({expr}));')
    out.append("}")
    return "\n".join(out)


def generate_harness(scn: dict, root: Path, cases: list[tuple[str, dict]], stubs: str = "") -> str:
    parts = [
        "/* change_impact.py sil 이 생성한 하네스 - 직접 수정하지 마세요 */",
        "#include <stdio.h>",
        "#include <stdlib.h>",
        "#include <string.h>",
        "static int cia_step;",
    ]
    for src in scn["sources"]:
        parts.append(f'#include "{(root / src).resolve().as_posix()}"')
    parts.append(stubs)
    parts.append("static void cia_label(const char *s) { printf(\"%d\\t#\\t%s\\n\", cia_step, s); }")
    parts.append(_observe_code(scn))
    bodies, seqs = [], []
    for i, (_, b) in enumerate(cases):
        em = _Emitter(scn, b, i)
        for k, v in (scn.get("init") or {}).items():
            em.emit(f"{k} = ({em.sub(v)});")
        em.emit('cia_label("init");')
        em.emit("cia_observe();")
        em.steps(scn.get("steps", []))
        seqs.extend(d for d in em.seq_decls if d not in seqs)
        bodies.append(f"    case {i}: {{\n" + "\n".join(em.lines) + "\n        break;\n    }")
    parts.extend(seqs)
    maps = ""
    if scn.get("memory_map"):
        # 하드웨어 레지스터 주소를 호스트 메모리로 매핑해 레지스터 접근 코드가 SIL 에서 동작하도록 한다
        parts.insert(1, "#include <sys/mman.h>")
        for addr, size in scn["memory_map"]:
            maps += (f"    if (mmap((void *)(unsigned long){addr}, (unsigned long){size}, PROT_READ | PROT_WRITE, "
                     f"MAP_PRIVATE | MAP_ANONYMOUS | MAP_FIXED, -1, 0) == MAP_FAILED) "
                     f"{{ perror(\"mmap {addr}\"); return 3; }}\n")
    parts.append("int main(int argc, char **argv)\n{\n    int cia_case = argc > 1 ? atoi(argv[1]) : 0;\n"
                 "    setvbuf(stdout, NULL, _IOLBF, 0);\n" + maps + "    switch (cia_case) {\n" + "\n".join(bodies) +
                 "\n    default:\n        return 2;\n    }\n    return 0;\n}\n")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# 스텁
# ---------------------------------------------------------------------------


@dataclass
class _Decls:
    funcs: dict[str, tuple[c_ast.Decl, object]] = field(default_factory=dict)
    vars: dict[str, tuple[c_ast.Decl, object]] = field(default_factory=dict)


def _collect_decls(root: Path, sources: list[str], cfg: BuildConfig) -> _Decls:
    out = _Decls()
    for src in sources:
        tu = parse_tu(root, src, cfg)
        if tu.ast is None:
            continue
        for ext in tu.ast.ext:
            if isinstance(ext, c_ast.Decl) and ext.name:
                if isinstance(ext.type, c_ast.FuncDecl):
                    out.funcs.setdefault(ext.name, (ext, tu.env))
                elif "extern" in (ext.storage or []):
                    out.vars.setdefault(ext.name, (ext, tu.env))
    return out


def _innermost(t):
    while not isinstance(t, c_ast.TypeDecl):
        t = t.type
    return t


def _typename(t) -> str:
    t = copy.deepcopy(t)
    _innermost(t).declname = None
    return c_generator.CGenerator().visit(c_ast.Typename(None, [], None, t))


def make_stub(name: str, decls: _Decls) -> str:
    if name in decls.vars:
        d, env = decls.vars[name]
        d = copy.deepcopy(d)
        d.storage = [s for s in (d.storage or []) if s != "extern"]
        t = d.type
        while isinstance(t, (c_ast.ArrayDecl, c_ast.PtrDecl)):
            if isinstance(t, c_ast.ArrayDecl) and t.dim is None:
                t.dim = c_ast.Constant("int", "256")
            t = t.type
        if isinstance(t, c_ast.TypeDecl):
            t.quals = [q for q in t.quals if q != "const"]
        return f"/* 스텁 변수 */\n{c_generator.CGenerator().visit(d)};"
    if name not in decls.funcs:
        return (f"/* 스텁 함수 (원형 없음) */\nlong long cia_ret_{name};\n"
                f"int {name}() {{ printf(\"%d\\tcall:{name}\\t\\n\", cia_step); return (int)cia_ret_{name}; }}")
    d, env = decls.funcs[name]
    d = copy.deepcopy(d)
    d.storage = []
    d.funcspec = []
    fdecl = d.type
    params = fdecl.args.params if fdecl.args else []
    fmt, vals, outs, out_vars = [], [], [], []
    for i, p in enumerate(params):
        if isinstance(p, c_ast.EllipsisParam):
            continue
        if isinstance(p, c_ast.Typename):
            if env.is_void(p.type):
                continue
            p = c_ast.Decl(None, [], [], [], [], p.type, None, None)
            params[i] = p
        if p.name is None:
            p.name = f"cia_a{i}"
            _innermost(p.type).declname = p.name
        if env.is_float(p.type):
            fmt.append("%g")
            vals.append(f"(double){p.name}")
        elif env.is_scalar(p.type):
            fmt.append("%lld")
            vals.append(f"(long long){p.name}")
        else:
            fmt.append("<ptr>" if env.is_pointer(p.type) or env.is_array(p.type) else "<obj>")
            core = env.core(p.type)
            if isinstance(core, c_ast.PtrDecl) and env.is_scalar(core.type) and "const" not in (getattr(core.type, "quals", None) or []):
                # 출력 포인터 인자: 시나리오의 stub_out(기본 0)으로 채운다
                out_vars.append(f"long long cia_out_{name}_{i};")
                outs.append(f"if ({p.name} != 0) {{ *{p.name} = ({_typename(core.type)})cia_out_{name}_{i}; }}")
    sig = c_generator.CGenerator().visit(d)
    log = f'printf("%d\\tcall:{name}\\t{",".join(fmt)}\\n", cia_step{"".join(", " + v for v in vals)});'
    ret_t = fdecl.type
    if env.is_void(ret_t):
        ret = ""
    elif env.is_float(ret_t):
        ret = f" return ({_typename(ret_t)})cia_ret_{name};"
    elif env.is_scalar(ret_t):
        ret = f" return ({_typename(ret_t)})cia_ret_{name};"
    elif env.is_pointer(ret_t):
        ret = f" return ({_typename(ret_t)})0;"
    else:
        tn = _typename(ret_t)
        ret = f" {{ {tn} cia_r; memset(&cia_r, 0, sizeof cia_r); return cia_r; }}"
    var = f"double cia_ret_{name};" if env.is_float(ret_t) else f"long long cia_ret_{name};"
    body = "\n    ".join([log] + outs) + ret
    return f"/* 스텁 함수 */\n{var}\n" + "\n".join(out_vars) + f"\n{sig}\n{{\n    {body}\n}}"


# ---------------------------------------------------------------------------
# 빌드·실행·비교
# ---------------------------------------------------------------------------


def _compile_cmd(scn: dict, root: Path, cfg: BuildConfig, harness: Path, exe: Path) -> list[str]:
    cmd = shlex.split(scn.get("compiler", "gcc")) + ["-w", "-O0", "-g", "-fno-strict-aliasing"]
    cmd += scn.get("cflags", ["-std=gnu99"])
    for d in scn.get("include_dirs", []):
        cmd += ["-I", str((root / d).resolve())]
    for d in cfg.include_dirs:
        cmd += ["-I", d.replace(ROOT_TOKEN, str(root))]
    for src in scn["sources"]:
        for x in cfg.per_file.get(src, []):
            cmd.append(x.replace(ROOT_TOKEN, str(root)))
        cmd += ["-I", str((root / src).resolve().parent)]
    for d in list(cfg.defines) + list(scn.get("defines", [])):
        cmd.append("-D" + d)
    cmd += [str(harness), "-o", str(exe)]
    cmd += scn.get("ldflags", [])
    return cmd


def _scenario_stub_names(steps: list) -> list[str]:
    """시나리오가 stub_return/stub_out 으로 값을 정하는 함수 (처음부터 스텁을 만든다)."""
    names: list[str] = []
    for st in steps:
        for key in ("stub_return", "stub_out"):
            for n in st.get(key, {}):
                if n not in names:
                    names.append(n)
        for n in _scenario_stub_names(st.get("steps", [])):
            if n not in names:
                names.append(n)
    return names


def build_version(scn: dict, root: Path, cfg: BuildConfig, work: Path, cases) -> tuple[Path, list[str], str]:
    work.mkdir(parents=True, exist_ok=True)
    harness = work / "harness.c"
    exe = work / "harness"
    stub_names: list[str] = _scenario_stub_names(scn.get("steps", []))
    decls = _collect_decls(root, scn["sources"], cfg) if stub_names else None
    stubs = "\n\n".join(make_stub(n, decls) for n in stub_names)
    for _ in range(4):
        harness.write_text(generate_harness(scn, root, cases, stubs), encoding="utf-8")
        cmd = _compile_cmd(scn, root, cfg, harness, exe)
        r = subprocess.run(cmd, capture_output=True, text=True)
        (work / "build.log").write_text(" ".join(cmd) + "\n\n" + r.stdout + r.stderr, encoding="utf-8")
        if r.returncode == 0:
            return exe, stub_names, " ".join(cmd)
        missing = sorted(set(_UNDEF_RE.findall(r.stderr)) - set(stub_names))
        if not missing or scn.get("auto_stub", True) is False:
            raise SilError(f"하네스 빌드 실패 ({work}):\n{r.stderr.strip()[:3000]}")
        if decls is None:
            decls = _collect_decls(root, scn["sources"], cfg)
        stub_names += missing
        stubs = "\n\n".join(make_stub(n, decls) for n in stub_names)
    raise SilError(f"스텁을 만들어도 링크되지 않습니다 ({work}). build.log 를 확인하세요.")


def run_case(exe: Path, idx: int, timeout: float) -> dict:
    try:
        r = subprocess.run([str(exe), str(idx)], capture_output=True, text=True, timeout=timeout)
        out, rc, err = r.stdout, r.returncode, r.stderr[-500:]
    except subprocess.TimeoutExpired as e:
        out = (e.stdout or b"").decode(errors="replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
        rc, err = "timeout", f"{timeout}s 초과"
    labels: dict[int, str] = {}
    values: dict[tuple[int, str], str] = {}
    for line in out.splitlines():
        parts = line.split("\t", 2)
        if len(parts) != 3 or not parts[0].lstrip("-").isdigit():
            continue
        step, name, val = int(parts[0]), parts[1], parts[2]
        if name == "#":
            labels[step] = val
        elif (step, name) in values:
            values[(step, name)] += ";" + val
        else:
            values[(step, name)] = val
    return {"labels": labels, "values": values, "rc": rc, "stderr": err}


def compare_traces(a: dict, b: dict) -> list[dict]:
    diffs = []
    for key in sorted(set(a["values"]) | set(b["values"]), key=lambda k: (k[0], k[1])):
        va, vb = a["values"].get(key), b["values"].get(key)
        if va != vb:
            step, name = key
            diffs.append({"step": step, "label": name, "old": "(없음)" if va is None else va,
                          "new": "(없음)" if vb is None else vb,
                          "step_label": b["labels"].get(step) or a["labels"].get(step, "")})
    return diffs


def run_sil(old_root: Path, new_root: Path, scn: dict, out_dir: Path, cfg: BuildConfig,
            timeout: float = 10.0) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    cases = expand_cases(scn)
    builds = {}
    for label, root in (("old", old_root), ("new", new_root)):
        exe, stubs, cmd = build_version(scn, Path(root).resolve(), cfg, out_dir / label, cases)
        builds[label] = {"exe": exe, "stubs": stubs, "cmd": cmd}
    results = []
    observables: dict[str, dict] = {}
    for i, (name, b) in enumerate(cases):
        ta = run_case(builds["old"]["exe"], i, timeout)
        tb = run_case(builds["new"]["exe"], i, timeout)
        diffs = compare_traces(ta, tb)
        status = "diff" if diffs else "same"
        if ta["rc"] != 0 or tb["rc"] != 0:
            status = "error" if ta["rc"] == tb["rc"] and not diffs else "diff"
        case = {"name": name, "bindings": b, "status": status, "old_rc": ta["rc"], "new_rc": tb["rc"],
                "first_step": diffs[0]["step"] if diffs else None,
                "first_step_label": diffs[0]["step_label"] if diffs else None,
                "diffs": diffs[:200], "diff_count": len(diffs)}
        if ta["rc"] != tb["rc"]:
            case["note"] = f"종료 코드 다름: 변경 전 {_rc(ta['rc'])} / 변경 후 {_rc(tb['rc'])}"
        elif ta["rc"] != 0:
            case["note"] = f"양쪽 모두 비정상 종료: {_rc(ta['rc'])}"
        results.append(case)
        labels_seen = {k[1] for k in list(ta["values"]) + list(tb["values"])}
        for lab in labels_seen:
            ob = observables.setdefault(lab, {"diff_cases": 0, "cases": 0, "example": None})
            ob["cases"] += 1
        first_by_label = {}
        for d in diffs:
            first_by_label.setdefault(d["label"], d)
        for lab, d in first_by_label.items():
            ob = observables.setdefault(lab, {"diff_cases": 0, "cases": 0, "example": None})
            ob["diff_cases"] += 1
            if ob["example"] is None:
                ob["example"] = {"case": name, **d}
    summary = {
        "cases": results,
        "observables": observables,
        "stubs": {k: v["stubs"] for k, v in builds.items()},
        "build": {k: v["cmd"] for k, v in builds.items()},
        "scenario": scn,
        "diff_cases": sum(1 for c in results if c["status"] == "diff"),
    }
    (out_dir / "sil_result.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str),
                                             encoding="utf-8")
    (out_dir / "sil_report.md").write_text(render_sil_md(summary), encoding="utf-8")
    return summary


def _rc(rc) -> str:
    if rc == -11:
        return "-11 (SIGSEGV: 잘못된 메모리 접근, 하드웨어 주소라면 memory_map 지정)"
    if isinstance(rc, int) and rc < 0:
        return f"{rc} (시그널)"
    return str(rc)


def render_sil_md(s: dict) -> str:
    out = ["# SIL 차등 실행 결과", ""]
    n = len(s["cases"])
    out.append(f"- 케이스 {n}개 중 **차이 발생 {s['diff_cases']}개**")
    for v in ("old", "new"):
        if s["stubs"].get(v):
            out.append(f"- 자동 스텁({'변경 전' if v == 'old' else '변경 후'}): {', '.join(s['stubs'][v])}")
    out += ["", "## 관찰 대상별 요약", "", "| 관찰 대상 | 차이 케이스 | 첫 예시 (케이스 / 단계 / 변경 전 → 변경 후) |", "|---|---|---|"]
    for lab, ob in sorted(s["observables"].items()):
        ex = ob["example"]
        exs = (f"{ex['case']} / #{ex['step']} {ex['step_label']} / `{ex['old']}` → `{ex['new']}`" if ex else "-")
        out.append(f"| `{lab}` | {ob['diff_cases']}/{ob['cases']} | {exs} |")
    out += ["", "## 케이스별 결과", "", "| 케이스 | 결과 | 최초 차이 단계 | 차이 수 |", "|---|---|---|---|"]
    for c in s["cases"]:
        st = {"diff": "**차이**", "same": "동일", "error": "실행 오류"}[c["status"]]
        first = f"#{c['first_step']} {c['first_step_label']}" if c["first_step"] is not None else "-"
        note = f" ({c['note']})" if c.get("note") else ""
        out.append(f"| {c['name']} | {st}{note} | {first} | {c['diff_count']} |")
    shown = [c for c in s["cases"] if c["diffs"]][:20]
    if shown:
        out += ["", "## 차이 상세 (최대 20개 케이스, 케이스당 30줄, 차이 값이 바뀐 단계만)", ""]
        for c in shown:
            out += [f"### {c['name']}", "", "| 단계 | 호출 | 관찰 대상 | 변경 전 | 변경 후 |", "|---|---|---|---|---|"]
            last: dict[str, tuple] = {}
            rows = 0
            for d in c["diffs"]:
                pair = (d["old"], d["new"], d["step"] - 1)
                prev = last.get(d["label"])
                last[d["label"]] = (d["old"], d["new"], d["step"])
                if prev is not None and prev[:2] == pair[:2] and prev[2] == pair[2]:
                    continue  # 직전 단계와 같은 차이가 이어지는 중
                out.append(f"| {d['step']} | {d['step_label']} | `{d['label']}` | {d['old']} | {d['new']} |")
                rows += 1
                if rows >= 30:
                    break
            out.append("")
    out += ["", "> 차이가 검출되지 않았다는 결과는 이 시나리오의 입력·순서에 한한 것입니다. "
            "모든 실행에서 같다는 뜻이 아닙니다. 실제 인터럽트 지연·하드웨어 타이밍은 SIL로 확정할 수 없습니다.", ""]
    return "\n".join(out)
