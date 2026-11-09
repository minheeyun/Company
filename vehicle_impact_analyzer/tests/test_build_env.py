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
    with open(log, "w") as f:
        f.write(LOG.replace("{root}", root))
    return root, log


def test_build_log_and_translation():
    root, log = _project()
    db = parse_build_log(open(log).read(), root)
    assert [os.path.basename(e["file"]) for e in db] == ["adc.c", "main.c"]
    assert db[0]["directory"] == root and detect_profile(db[0]["arguments"]) == "tasking"
    tr = translate(db[0]["arguments"], root, db[0]["file"], "tasking")
    assert "-std=c99" in tr.args and "-DECU_A=1" in tr.args and "-Dtrace" in tr.args
    assert f"-I{os.path.join(root, 'inc')}" in tr.args
    assert ["-include", os.path.join(root, "inc/cfg.h")] == tr.args[tr.args.index("-include"):tr.args.index("-include") + 2]
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
