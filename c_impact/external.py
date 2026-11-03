"""외부 도구 연동: Joern(CPG·데이터 흐름), CodeQL(Taint 질의), KLEE(기호 실행 재현).

내장 엔진(cpg · taint · symexec)만으로 분석이 끝나며, 이 모듈은 같은 규칙으로 각 도구의 입력을 만들어
교차 검증에 쓰게 한다. 도구가 설치되어 있고 옵션(--joern, --codeql, --klee)을 주면 실행까지 하고 결과를 읽는다.

  joern/taint.sc          Joern 스크립트 (importCode → Source/Sink → reachableByFlows)
  codeql/                 qlpack.yml + ChangeTaint.ql (TaintTracking::Global, path-problem)
  klee/<ID>/              verify.c(하네스) + 안전 속성(klee_assert)을 넣은 소스 사본 + run_klee.sh / .ps1
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

from pycparser import c_ast

from .cexpr import gen

# ---------------------------------------------------------------------------
# 공통
# ---------------------------------------------------------------------------


def glob_to_regex(patterns) -> str:
    """glob 목록 → Java/QL 정규식 (Joern·CodeQL 공용)."""
    parts = []
    for p in patterns or []:
        parts.append("".join(".*" if ch == "*" else "." if ch == "?" else re.escape(ch) for ch in p))
    return "|".join(parts) or "^$"


def _run(cmd, cwd=None, timeout=1800) -> tuple[int, str]:
    try:
        r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout,
                           encoding="utf-8", errors="replace")
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except (OSError, subprocess.TimeoutExpired) as e:
        return -1, str(e)


def _length_sinks(rules):
    return [(funcs, arg) for _, funcs, arg, _ in rules.length_calls]


# ---------------------------------------------------------------------------
# Joern
# ---------------------------------------------------------------------------


def joern_script(root: Path, rules, project: str = "cia-after") -> str:
    param_funcs = [p for s in rules.sources for p in (s.get("functions") or [])]
    out_args = [(o.get("functions"), o.get("args") or [-1]) for s in rules.sources for o in (s.get("out_args") or [])]
    lines = [
        "// folder_impact.py 가 만든 Joern 스크립트: 외부 입력(Source) → 위험 연산(Sink) 데이터 흐름",
        "// 실행: joern --script taint.sc",
        f'importCode(inputPath="{root.as_posix()}", projectName="{project}")',
        "",
        f'val srcParams = cpg.method.name("{glob_to_regex(param_funcs)}").parameter',
    ]
    if out_args:
        lines.append("val srcOut = " + " ++ ".join(
            f'cpg.call.name("{glob_to_regex(f)}").argument' for f, _ in out_args))
    else:
        lines.append("val srcOut = Iterator.empty")
    sink_parts = []
    for funcs, arg in _length_sinks(rules):
        if arg > 0:
            sink_parts.append(f'cpg.call.name("{glob_to_regex(funcs)}").argument({arg})')
        else:
            sink_parts.append(f'cpg.call.name("{glob_to_regex(funcs)}").argument')
    sink_parts.append('cpg.call.name("<operator>.indirectIndexAccess").argument(2)')
    sink_parts.append('cpg.call.name("<operator>.(division|modulo)").argument(2)')
    lines += [
        "val sinks = " + " ++ ".join(sink_parts),
        "val flows = sinks.reachableByFlows(srcParams ++ srcOut).l",
        'println(s"CIA_FLOW_COUNT ${flows.size}")',
        "flows.foreach { f =>",
        '  val e = f.elements',
        '  println("CIA_FLOW " + e.map(x => s"${x.location.filename}:${x.location.lineNumber.getOrElse(0)}").mkString(" -> "))',
        "}",
    ]
    return "\n".join(lines) + "\n"


def run_joern(out: Path, root: Path, rules, execute: bool) -> dict:
    d = out / "joern"
    d.mkdir(parents=True, exist_ok=True)
    (d / "taint.sc").write_text(joern_script(root, rules), encoding="utf-8")
    info = {"script": str(d / "taint.sc"), "ran": False, "flows": []}
    exe = shutil.which("joern")
    if not execute:
        info["note"] = "스크립트만 생성 (--joern 으로 실행)"
        return info
    if not exe:
        info["note"] = "joern 이 PATH 에 없어 실행하지 않음"
        return info
    code, text = _run([exe, "--script", str(d / "taint.sc")], cwd=d, timeout=3600)
    (d / "joern_output.txt").write_text(text, encoding="utf-8")
    info.update(ran=True, returncode=code, output=str(d / "joern_output.txt"))
    for line in text.splitlines():
        if line.startswith("CIA_FLOW "):
            info["flows"].append(line[len("CIA_FLOW "):].split(" -> "))
    return info


# ---------------------------------------------------------------------------
# CodeQL
# ---------------------------------------------------------------------------


def codeql_query(rules) -> str:
    src = []
    for s in rules.sources:
        if s.get("functions"):
            src.append(f'exists(Function f | f.getName().regexpMatch("{glob_to_regex(s["functions"])}") and '
                       "source.asParameter() = f.getAParameter())")
        for o in s.get("out_args") or []:
            for a in o.get("args") or [-1]:
                idx = "c.getNumberOfArguments() - 1" if a == -1 else str(a - 1)
                src.append(f'exists(FunctionCall c | c.getTarget().getName().regexpMatch("{glob_to_regex(o.get("functions"))}") '
                           f"and source.asDefiningArgument() = c.getArgument({idx}))")
        if s.get("returns"):
            src.append(f'exists(FunctionCall c | c.getTarget().getName().regexpMatch("{glob_to_regex(s["returns"])}") '
                       "and source.asExpr() = c)")
    snk = []
    for funcs, arg in _length_sinks(rules):
        idx = "c.getNumberOfArguments() - 1" if arg == -1 else str(arg - 1)
        snk.append(f'exists(FunctionCall c | c.getTarget().getName().regexpMatch("{glob_to_regex(funcs)}") '
                   f"and sink.asExpr() = c.getArgument({idx}))")
    snk.append("exists(ArrayExpr a | sink.asExpr() = a.getArrayOffset())")
    snk.append("exists(DivExpr d | sink.asExpr() = d.getRightOperand())")
    snk.append("exists(RemExpr d | sink.asExpr() = d.getRightOperand())")
    return "\n".join([
        "/**",
        " * @name External input reaches dangerous operation (folder_impact rules)",
        " * @kind path-problem",
        " * @problem.severity warning",
        " * @id cpp/cia-external-input-to-sink",
        " */",
        "",
        "import cpp",
        "import semmle.code.cpp.dataflow.new.TaintTracking",
        "",
        "module CiaConfig implements DataFlow::ConfigSig {",
        "  predicate isSource(DataFlow::Node source) {",
        "    " + ("\n    or ".join(src) if src else "none()"),
        "  }",
        "",
        "  predicate isSink(DataFlow::Node sink) {",
        "    " + "\n    or ".join(snk),
        "  }",
        "}",
        "",
        "module CiaFlow = TaintTracking::Global<CiaConfig>;",
        "import CiaFlow::PathGraph",
        "",
        "from CiaFlow::PathNode source, CiaFlow::PathNode sink",
        "where CiaFlow::flowPath(source, sink)",
        'select sink.getNode(), source, sink, "External input reaches a dangerous operation; verify bounds."',
        "",
    ])


def run_codeql(out: Path, root: Path, rules, execute: bool, build_cmd: str | None) -> dict:
    d = out / "codeql"
    d.mkdir(parents=True, exist_ok=True)
    (d / "qlpack.yml").write_text("name: cia/change-taint\nversion: 0.0.1\ndependencies:\n  codeql/cpp-all: \"*\"\n",
                                  encoding="utf-8")
    (d / "ChangeTaint.ql").write_text(codeql_query(rules), encoding="utf-8")
    info = {"query": str(d / "ChangeTaint.ql"), "ran": False, "results": []}
    exe = shutil.which("codeql")
    cmds = [
        f'codeql pack install "{d}"',
        f'codeql database create "{d / "db-after"}" --language=c-cpp --source-root "{root}" --command="{build_cmd or "<빌드 명령>"}"',
        f'codeql database analyze "{d / "db-after"}" "{d / "ChangeTaint.ql"}" --format=sarif-latest --output="{d / "taint.sarif"}"',
    ]
    (d / "run_codeql.txt").write_text("\n".join(cmds) + "\n", encoding="utf-8")
    if not execute:
        info["note"] = "질의만 생성 (--codeql --codeql-build \"빌드 명령\" 으로 실행)"
        return info
    if not exe or not build_cmd:
        info["note"] = "codeql 이 PATH 에 없거나 --codeql-build 가 없어 실행하지 않음"
        return info
    log = []
    for args in ([exe, "pack", "install", str(d)],
                 [exe, "database", "create", str(d / "db-after"), "--overwrite", "--language=c-cpp",
                  f"--source-root={root}", f"--command={build_cmd}"],
                 [exe, "database", "analyze", str(d / "db-after"), str(d / "ChangeTaint.ql"),
                  "--format=sarif-latest", f"--output={d / 'taint.sarif'}"]):
        code, text = _run(args, cwd=root, timeout=7200)
        log.append(f"$ {' '.join(args)}\n{text}")
        if code != 0:
            info["note"] = f"실패: {' '.join(args[:3])} (codeql/log.txt)"
            break
    (d / "log.txt").write_text("\n".join(log), encoding="utf-8")
    info["ran"] = True
    sarif = d / "taint.sarif"
    if sarif.is_file():
        info["results"] = parse_sarif(sarif)
        info["sarif"] = str(sarif)
    return info


def parse_sarif(path: Path) -> list[dict]:
    out = []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return out
    for run in data.get("runs", []):
        for r in run.get("results", []):
            for loc in r.get("locations", [])[:1]:
                pl = loc.get("physicalLocation", {})
                out.append({"file": pl.get("artifactLocation", {}).get("uri", ""),
                            "line": pl.get("region", {}).get("startLine", 0),
                            "message": r.get("message", {}).get("text", "")})
    return out


# ---------------------------------------------------------------------------
# KLEE
# ---------------------------------------------------------------------------

_ASSERT = {
    "index": lambda op, sk: f"({op}) >= 0 && ({op}) < {sk.dim}",
    "length": lambda op, sk: f"({op}) <= {sk.dim}",
    "div": lambda op, sk: f"({op}) != 0",
    "shift": lambda op, sk: f"({op}) >= 0 && ({op}) < {sk.width or 32}",
}


def klee_harness(prog, nid: int, sk, fid: str, out: Path, cfg_includes: list[str], defines: list[str]) -> dict | None:
    """위험 연산 하나에 대한 KLEE 하네스. 지원 종류: index, length, div, shift."""
    if sk.kind not in _ASSERT or (sk.kind in ("index", "length") and sk.dim is None):
        return None
    n = prog.nodes[nid]
    f = prog.funcs.get(n.func)
    if f is None or not n.file or not Path(n.file).is_file():
        return None
    d = out / "klee" / fid
    d.mkdir(parents=True, exist_ok=True)
    src_lines = Path(n.file).read_text(encoding="utf-8", errors="replace").splitlines()
    prop = _ASSERT[sk.kind](gen(sk.operand), sk)
    idx = max(0, n.line - 1)
    indent = re.match(r"\s*", src_lines[idx]).group(0) if idx < len(src_lines) else ""
    inst = ["#include <klee/klee.h>", f'#line 1 "{Path(n.file).name}"'] + src_lines[:idx] + \
        [f"{indent}klee_assert({prop}); /* CIA: 안전 속성 ({sk.kind}) */", f'#line {n.line} "{Path(n.file).name}"'] + src_lines[idx:]
    inst_name = f"cia_{Path(n.file).stem}_inst.c"
    (d / inst_name).write_text("\n".join(inst) + "\n", encoding="utf-8")
    decls, calls = [], []
    ftype = f.decl.type
    params = ftype.args.params if isinstance(ftype, c_ast.FuncDecl) and ftype.args else []
    for i, p in enumerate(params):
        if not isinstance(p, c_ast.Decl) or p.name is None:
            continue
        name = p.name
        is_ptr = f.env.is_pointer(p.type) or f.env.is_array(p.type)
        decl = gen(p).replace("const ", "")
        if is_ptr:
            decls.append(f"    static unsigned char cia_buf_{name}[256];")
            decls.append(f'    klee_make_symbolic(cia_buf_{name}, sizeof(cia_buf_{name}), "{name}_data");')
            decls.append(f"    {decl} = (void *)cia_buf_{name};")
        else:
            decls.append(f"    {decl};")
            decls.append(f'    klee_make_symbolic(&{name}, sizeof({name}), "{name}");')
        calls.append(name)
    harness = [
        "/* folder_impact.py 가 만든 KLEE 하네스: 인자를 기호 변수로 두고 위험 연산의 안전 속성을 검사한다. */",
        "#include <klee/klee.h>",
        f'#include "{inst_name}"',
        "",
        "int main(void)",
        "{",
        *decls,
        f"    (void){f.name}({', '.join(calls)});",
        "    return 0;",
        "}",
        "",
    ]
    (d / "verify.c").write_text("\n".join(harness), encoding="utf-8")
    inc = " ".join(f'-I"{x}"' for x in cfg_includes)
    dfn = " ".join(f"-D{x}" for x in defines)
    cmd_bc = f'clang -emit-llvm -c -g -O0 -Xclang -disable-O0-optnone -I"$KLEE_INCLUDE" {inc} {dfn} verify.c -o verify.bc'
    (d / "run_klee.sh").write_text(
        "#!/bin/sh\n# KLEE_INCLUDE 에 klee/klee.h 가 있는 폴더를 지정하세요 (예: /usr/local/include)\n"
        f"cd \"$(dirname \"$0\")\"\n{cmd_bc}\nklee --exit-on-error-type=Assert verify.bc\n"
        "for f in klee-last/*.assert.err; do [ -e \"$f\" ] && ktest-tool \"${f%.assert.err}.ktest\"; done\n",
        encoding="utf-8")
    return {"dir": str(d), "harness": str(d / "verify.c"), "property": prop, "function": f.name}


def run_klee(info: dict, include_dir: str | None) -> dict:
    d = Path(info["dir"])
    clang, klee = shutil.which("clang"), shutil.which("klee")
    if not clang or not klee:
        info["note"] = "clang·klee 가 PATH 에 없어 실행하지 않음 (run_klee.sh 로 직접 실행)"
        return info
    inc = [f"-I{include_dir}"] if include_dir else []
    code, text = _run([clang, "-emit-llvm", "-c", "-g", "-O0", "-Xclang", "-disable-O0-optnone", *inc,
                       "verify.c", "-o", "verify.bc"], cwd=d, timeout=600)
    if code != 0:
        info.update(ran=False, note="clang 컴파일 실패 (klee_build.log)")
        (d / "klee_build.log").write_text(text, encoding="utf-8")
        return info
    code, text = _run([klee, "--exit-on-error-type=Assert", "--max-time=120s", "verify.bc"], cwd=d, timeout=900)
    (d / "klee_output.txt").write_text(text, encoding="utf-8")
    errs = sorted((d / "klee-last").glob("*.assert.err")) if (d / "klee-last").exists() else []
    info.update(ran=True, violated=bool(errs) or "ASSERTION FAIL" in text)
    if errs:
        kt = shutil.which("ktest-tool")
        if kt:
            _, ktext = _run([kt, str(errs[0]).replace(".assert.err", ".ktest")], cwd=d, timeout=60)
            info["ktest"] = ktext
    return info
