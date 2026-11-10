"""Windows 이식성 시험: Windows 경로·명령줄·인코딩·줄바꿈·fork 없는 실행 비교를 어느 OS에서나 재현한다."""
import ntpath
import os
import shutil
import subprocess
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helpers import graph_of  # noqa: E402
from vfia import clang_api as ca  # noqa: E402
from vfia import portable as pt  # noqa: E402
from vfia.build_env import detect_profile, parse_build_log  # noqa: E402
from vfia.config import load_structured  # noqa: E402
from vfia.model import EK, NK, AnalysisGraph  # noqa: E402


def test_rel_posix_windows_paths():
    nt = ntpath
    assert pt.rel_posix(r"C:\Proj\V1\src\a.c", "c:/proj/v1", pm=nt) == "src/a.c"       # 대소문자·구분자 무시
    assert pt.rel_posix(r"C:\Proj\V1\src\..\inc\b.h", r"C:\Proj\V1", pm=nt) == "inc/b.h"
    assert pt.rel_posix(r"C:\Proj\V1", r"C:\Proj\V1\\", pm=nt) == "."
    assert pt.rel_posix(r"D:\Proj\V1\src\a.c", r"C:\Proj\V1", pm=nt) is None          # 다른 드라이브
    assert pt.rel_posix(r"C:\Proj\V10\a.c", r"C:\Proj\V1", pm=nt) is None             # 접두어만 같은 형제 폴더
    assert pt.rel_posix("/a/B/c.c", "/a/b", pm=pt.posixpath) is None                    # POSIX는 대소문자 구분
    assert pt.basename_any(r"C:\TASKING\bin\ctc.EXE") == "ctc.EXE"
    assert pt.is_abs_any(r"C:\x\y.c") and pt.is_abs_any(r"\\server\share\a.c") and not pt.is_abs_any("src/a.c")


def test_split_windows_command_line():
    cmd = r'C:\TASKING\ctc.exe -I"C:\Program Files\inc" "C:\src dir\a.c" -DMSG=\"hi\" -IC:\x\ --iso=99'
    assert pt.split_windows(cmd) == [r"C:\TASKING\ctc.exe", r"-IC:\Program Files\inc", r"C:\src dir\a.c",
                                     '-DMSG="hi"', "-IC:\\x\\", "--iso=99"]
    assert pt.split_command(cmd) == pt.split_windows(cmd)            # Windows 경로가 보이면 Windows 규칙
    assert pt.split_command("gcc -DX='a b' src/a.c", windows=False) == ["gcc", "-DX=a b", "src/a.c"]


def test_windows_build_log_parsing():
    root = tempfile.mkdtemp(prefix="vfia_win_")
    rsp = os.path.join(root, "opts.rsp")
    with open(rsp, "wb") as f:   # 응답 파일: UTF-16(BOM) + Windows 따옴표
        f.write('-I"C:\\Program Files\\TASKING\\include" -DECU_A=1\r\n'.encode("utf-16"))
    log = ("cd /d C:\\work\\ecu && C:\\TASKING\\bin\\ctc.EXE --iso=99 @" + rsp + " C:\\work\\ecu\\src\\a.c\r\n"
           "CD /D \"C:\\work\\ecu 2\" && cctc.exe -IC:\\work\\inc src\\b.c\r\n")
    db = parse_build_log(log, root)
    assert len(db) == 2
    a, b = db
    assert a["file"] == r"C:\work\ecu\src\a.c" and a["directory"] == r"C:\work\ecu"
    assert r"-IC:\Program Files\TASKING\include" in a["arguments"] and "-DECU_A=1" in a["arguments"]
    assert detect_profile(a["arguments"]) == "tasking" and b["compiler_profile"] == "tasking"
    assert b["directory"] == r"C:\work\ecu 2"


def test_text_inputs_with_bom_utf16_and_cp949():
    d = tempfile.mkdtemp(prefix="vfia_enc_")
    cases = {"bom.yaml": "\ufeffproject: 토크\nlist: [1, 2]\n".encode("utf-8"),
             "u16.yaml": "project: 토크\nlist: [1, 2]\n".encode("utf-16"),
             "ansi.yaml": "project: 토크\r\nlist: [1, 2]\r\n".encode("cp949"),
             "bom.json": "\ufeff{\"project\": \"토크\", \"list\": [1, 2]}".encode("utf-8")}
    for name, data in cases.items():
        p = os.path.join(d, name)
        with open(p, "wb") as f:
            f.write(data)
        assert load_structured(p) == {"project": "토크", "list": [1, 2]}, name


def test_fallback_builtin_headers_parse_for_several_targets():
    import clang.cindex as ci
    ca.init_libclang()
    src = ("#include <stdint.h>\n#include <stdbool.h>\n#include <stddef.h>\n#include <limits.h>\n"
           "_Static_assert(sizeof(int16_t) == 2 && UINT32_MAX == 4294967295U && INT8_MIN == -128, \"w\");\n"
           "_Static_assert(CHAR_BIT == 8 && SIZE_MAX == (size_t)-1, \"l\");\n"
           "bool f(uint8_t x) { return x > UINT8_C(10) && true; }\n")
    d = tempfile.mkdtemp(prefix="vfia_hdr_")
    p = os.path.join(d, "t.c")
    with open(p, "w", encoding="utf-8") as f:
        f.write(src)
    idx = ci.Index.create()
    for target in ("x86_64-pc-windows-msvc", "arm-none-eabi", "powerpc-none-eabi", "x86_64-pc-linux-gnu"):
        tu = idx.parse(p, args=[f"--target={target}", "-std=c11", "-nostdinc", "-isystem", ca.BUILTIN_FALLBACK])
        errs = [x.spelling for x in tu.diagnostics if x.severity >= ci.Diagnostic.Error]
        assert not errs, (target, errs)
    info = ca.resource_include_info()
    assert os.path.isfile(os.path.join(info["path"], "stdbool.h"))


def test_crlf_sources_give_same_graph():
    src = ("#include <stdint.h>\n/* 한글 주석 */\nint16_t g_x;\n"
           "int16_t f(int16_t a, int16_t b)\n{\n    if (a > 30 &&\n        b < 40) {\n        g_x = a;\n"
           "        return (int16_t)(a + b);\n    }\n    return 0;\n}\n")
    g1, _ = graph_of({"src/a.c": src})
    g2, _ = graph_of({"src/a.c": src.replace("\n", "\r\n")})
    e1 = sorted((u, v, d["kind"]) for u, v, d in g1.g.edges(data=True))
    e2 = sorted((u, v, d["kind"]) for u, v, d in g2.g.edges(data=True))
    assert sorted(g1.g.nodes) == sorted(g2.g.nodes) and e1 == e2
    def attrs(g):
        return sorted((n, g.node(n).get("line"), g.node(n).get("text")) for n in g.nodes_of(NK.STMT))
    assert attrs(g1) == attrs(g2) and len(attrs(g1)) >= 4


def test_graph_order_is_canonical_after_save_load():
    def build(order):
        g = AnalysisGraph()
        for n in order:
            g.add_node(n, NK.FUNCTION, name=n)
        for a, b in [(order[0], order[1]), (order[2], order[0]), (order[1], order[2])]:
            g.add_edge(a, b, EK.CALLS)
        return g
    d = tempfile.mkdtemp(prefix="vfia_ord_")
    p1, p2 = os.path.join(d, "a.db"), os.path.join(d, "b.db")
    build(["fn:a", "fn:b", "fn:c"]).save(p1)
    build(["fn:c", "fn:a", "fn:b"]).save(p2)
    g1, g2 = AnalysisGraph.load(p1), AnalysisGraph.load(p2)
    assert list(g1.g.nodes) == list(g2.g.nodes) == ["fn:a", "fn:b", "fn:c"]


@pytest.mark.skipif(not (shutil.which("gcc") or shutil.which("cc")), reason="호스트 C 컴파일러 없음")
def test_exec_harness_without_fork(monkeypatch):
    """fork가 없는 Windows 방식(벡터별 프로세스)으로도 전역 상태가 벡터마다 초기화되는지."""
    from vfia.behavior_checker import run_harness
    d = tempfile.mkdtemp(prefix="vfia_h_")
    c = os.path.join(d, "h.c")
    with open(c, "w", encoding="utf-8") as f:
        f.write("#include <stdio.h>\nstatic long long acc;\nint main(void){long long x;"
                "if(scanf(\"%lld\",&x)==1){acc+=x;printf(\"%lld \\n\",acc);}return 0;}\n")
    exe = os.path.join(d, "h" + pt.EXE_SUFFIX)
    cc = shutil.which("gcc") or shutil.which("cc")
    assert subprocess.run([cc, c, "-o", exe]).returncode == 0
    assert run_harness(exe, ["5", "7", "-3"], per_process=True) == ["5", "7", "-3"]   # 누적되지 않음
