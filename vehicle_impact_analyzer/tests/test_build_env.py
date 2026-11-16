"""차량 ECU 빌드 환경 재현: 빌드 로그 파싱, 옵션 변환, 확장 키워드 호환 헤더, 파싱 건전성."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from helpers import make_project  # noqa: E402
from vfia.build_env import detect_profile, parse_build_log, translate  # noqa: E402
from vfia.config import BuildConfig, VersionSpec  # noqa: E402
from vfia.graph_builder import build_graph  # noqa: E402
from vfia.model import NK  # noqa: E402
from vfia.project_indexer import ProjectIndexer  # noqa: E402

LOG = r"""
make[1]: Entering directory '{root}'
ctc --iso=99 -Ctc39xb --include-directory=inc --define=ECU_A=1 -Dtrace -O2 --core=tc1.6.2 -H inc/cfg.h -o obj/adc.src src/adc.c
"C:/Tools/ctc/bin/ctc.exe" -Iinc -DECU_A=1 -o obj/main.src src/main.c
echo done
"""

SRC_ADC = r"""
#include "adc.h"
#ifdef __TASKING__
#pragma section farbss "adc_ram"
#endif
static __far uint16 s_raw;
uint16 g_adc_mv;
void __interrupt(5) Adc_Isr(void) { s_raw = (uint16)__mfcr(0xFE1C); __nop(); }
void Adc_Main(void) { g_adc_mv = (uint16)(s_raw * ECU_A); }
"""
SRC_MAIN = r"""
#include "adc.h"
void __vendor_magic Weird(void) { g_adc_mv = 0; }
int Main_Ok(void) { return CFG_VALUE; }
"""


def _project():
    root = make_project({"inc/adc.h": "typedef unsigned short uint16; extern uint16 g_adc_mv;\nvoid Adc_Main(void);\n",
                         "inc/cfg.h": "#define CFG_VALUE 3\n", "src/adc.c": SRC_ADC, "src/main.c": SRC_MAIN})
    log = os.path.join(root, "build.log")
    with open(log, "w", encoding="utf-8") as f:
        f.write(LOG.replace("{root}", root))
    return root, log


def test_build_log_and_translation():
    root, log = _project()
    db = parse_build_log(open(log, encoding="utf-8").read(), root)
    assert [os.path.basename(e["file"]) for e in db] == ["adc.c", "main.c"]
    assert db[0]["directory"] == root and detect_profile(db[0]["arguments"]) == "tasking"
    tr = translate(db[0]["arguments"], root, db[0]["file"], "tasking")
    assert "-std=c99" in tr.args and "-DECU_A=1" in tr.args and "-Dtrace" in tr.args
    assert f"-I{os.path.join(root, 'inc')}" in tr.args
    assert ["-include", os.path.normpath(os.path.join(root, "inc/cfg.h"))] == tr.args[tr.args.index("-include"):tr.args.index("-include") + 2]
    assert "-Ctc39xb" in tr.dropped and "--core=tc1.6.2" in tr.dropped


def test_tasking_project_parses_with_compat_and_reports_health():
    root, log = _project()
    v = VersionSpec(name="t", root=root)
    b = BuildConfig(compiler="tasking", build_log=log)
    idx = ProjectIndexer(v, b, log=lambda *a: None).run()
    fns = {u["name"] for u in idx.units.values() if u["kind"] == "function"}
    assert {"Adc_Isr", "Adc_Main", "Main_Ok"} <= fns          # __interrupt/__far/__mfcr 처리
    ph = idx.parse_health
    assert ph["compiler_profiles"] == ["tasking"]
    assert ph["success_rate"] < 1.0                              # main.c는 알 수 없는 확장으로 오류
    missing = {m["function"] for m in ph["missing_function_candidates"]}
    assert "Weird" in missing                                    # 조용히 빠진 함수 탐지
    partial = {p["function"] for p in ph["partial_ast_functions"]}
    assert "Main_Ok" in partial                                  # CFG_VALUE 미정의 -> 본문 일부가 빠진 함수
    g = build_graph(idx)
    cs = [n for n in g.nodes_of(NK.CALL_SITE) if g.node(n).get("func") == "fn:Adc_Isr"]
    assert any("__mfcr" in " ".join(g.node(c).get("callees") or []) for c in cs)   # intrinsic은 외부 호출로 남김


def test_make_compile_db_output_folder_and_messages(capsys):
    """-o에 폴더를 주면 그 안에 compile_commands.json을 만들고(없으면 폴더 생성), 결과 요약을 출력한다."""
    import importlib.util
    root, log = _project()
    spec = importlib.util.spec_from_file_location("mcd", os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "tools", "make_compile_db.py"))
    mcd = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mcd)
    out_dir = os.path.join(root, "before")
    os.makedirs(out_dir)
    assert mcd.main(["--log", log, "--root", root, "--compiler", "tasking", "-o", out_dir]) == 0
    assert os.path.isfile(os.path.join(out_dir, "compile_commands.json"))
    new_dir = os.path.join(root, "nested", "after") + os.sep
    assert mcd.main(["--log", log, "--root", root, "-o", new_dir]) == 0
    assert os.path.isfile(os.path.join(new_dir, "compile_commands.json"))
    text = capsys.readouterr().out
    assert "컴파일 명령 2개" in text and "소스 파일 2개" in text
    assert mcd.main(["--log", os.path.join(root, "없음.log"), "--root", root]) == 2


def _ide_project():
    """이클립스 기반 TASKING IDE 형태: <root>/Debug에서 '../src/..'로 컴파일, include는 -f 옵션 파일."""
    root = make_project({"src/app/torque.c": '#include "x.h"\nint g;\nint f(int a) { if (a > LIMIT) { g = a; } return g; }\n',
                         "include/x.h": "#define LIMIT 30\n", "src/app/b.c": '#include "y.h"\nint b(void){return Y;}\n',
                         "inc dir/y.h": "#define Y 1\n", "Debug/makefile": "all:\n"})
    with open(os.path.join(root, "Debug", "inc.opt"), "w", encoding="utf-8") as f:
        f.write(f'-I"{root}/include"\n-I"../src/app"\n')
    return root


def test_ide_style_tasking_log_with_option_file_and_build_folder():
    root = _ide_project()
    log = ("18:22:15 **** Build of configuration Debug for project LSS ****\nmake -j8 all\n"
           "Building file: ../src/app/torque.c\nInvoking: TASKING C/C++ Compiler\n"
           '"/opt/TASKING/TriCore v6.3r1/ctc/bin/cctc" -cs --dep-file="src/app/.torque.o.d" -D__CPU__=tc39xb '
           '"-finc.opt" --iso=99 -O0 -Ctc39xb -o "src/app/torque.src" "../src/app/torque.c" && \\\n'
           '"/opt/TASKING/TriCore v6.3r1/ctc/bin/astc" -Og -o "src/app/torque.o" "src/app/torque.src"\n'
           "Finished building: ../src/app/torque.c\n")
    db = parse_build_log(log, root, "tasking")
    assert len(db) == 1
    e = db[0]
    assert e["file"] == os.path.join(root, "src", "app", "torque.c") and e["directory"] == os.path.join(root, "Debug")
    tr = translate(e["arguments"], e["directory"], e["file"], "tasking")
    assert f"-I{os.path.join(root, 'include')}" in tr.args and f"-I{os.path.join(root, 'src', 'app')}" in tr.args
    assert "-D__CPU__=tc39xb" in tr.args and "-std=c99" in tr.args


def test_prefixed_log_lines_and_diagnostics_and_scan():
    from vfia.build_env import diagnose_build_log, scan_sources
    root = _ide_project()
    log = ('[2026-10-08 18:22:15] [INFO] Compile: cctc.exe --iso=99 -I"inc dir" -Iinclude src/app/b.c -o out/b.o\n'
           "[2026-10-08 18:22:16] [INFO] Link done\n")
    db = parse_build_log(log, root)
    assert [os.path.basename(x["file"]) for x in db] == ["b.c"] and db[0]["compiler_profile"] == "tasking"
    quiet = "Compiling torque.c ...\nCompiling b.c ...\nLinking LSS.elf ...\n"
    assert parse_build_log(quiet, root) == []
    diag = "\n".join(diagnose_build_log(quiet))
    assert "Compiling torque.c" in diag and "compiling(" not in diag
    sc = scan_sources(root, "tasking")
    assert sorted(os.path.basename(x["file"]) for x in sc) == ["b.c", "torque.c"]
    args = sc[0]["arguments"]
    assert f"-I{os.path.join(root, 'inc dir')}" in args and not any("Debug" in a for a in args)


def test_new_project_rejects_log_folder_as_root_and_scan_fallback(capsys):
    import importlib.util
    spec = importlib.util.spec_from_file_location("new_project", os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "tools", "new_project.py"))
    np_ = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(np_)
    root = _ide_project()
    os.makedirs(os.path.join(root, ".log"))
    quiet = os.path.join(root, ".log", "build.log")
    with open(quiet, "w", encoding="utf-8") as f:
        f.write("Compiling torque.c ...\n")
    d = os.path.join(root, "cfg")
    try:
        np_.main(["--dir", d, "--before-root", root, "--before-log", quiet, "--after-root", os.path.join(root, ".log"),
                  "--after-log", quiet])
        raise AssertionError("SystemExit 기대")
    except SystemExit as e:
        assert "소스 폴더가 아닌 것 같음" in str(e)
    try:
        np_.main(["--dir", d, "--before-root", root, "--before-log", quiet, "--after-root", root, "--after-log", quiet])
        raise AssertionError("SystemExit 기대")
    except SystemExit as e:
        assert "--scan" in str(e) and "Compiling torque.c" in str(e)
    assert np_.main(["--dir", d, "--compiler", "tasking", "--before-root", root, "--before-log", quiet,
                     "--after-root", root, "--after-log", quiet, "--scan"]) == 0
    assert os.path.isfile(os.path.join(d, "before", "compile_commands.json"))
