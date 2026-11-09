"""변경 유형 확장, 전처리 전/후 대응, 심벌 통합, RTE 매크로 연결 시험."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from helpers import graph_of  # noqa: E402
from vfia.change_detector import detect_changes  # noqa: E402
from vfia.model import EK, NK  # noqa: E402
from vfia.vehicle_context import VehicleModel, apply_vehicle_model  # noqa: E402


def _diff(old_files, new_files):
    go, io = graph_of(old_files)
    gn, i_n = graph_of(new_files)
    res, cd = detect_changes(go, gn)
    return res, go, gn


def _types(res):
    return {c["type"]: c for c in res["changes"]}


def test_integer_switch_array_and_linkage_changes():
    old = r"""
    #include <stdint.h>
    static int16_t g_tab[4];
    static int helper(int a) { return a + 1; }
    int f(int m, int i) {
        uint16_t d = 5;
        int r = 0;
        switch (m) { case 1: r = 1; break; case 2: r = 2; break; default: r = 0; }
        if (d - 10 > 0) r += 1;
        return r + g_tab[i] + helper(m);
    }
    """
    new = r"""
    #include <stdint.h>
    static int16_t g_tab[8];
    int helper(int a) { return a + 1; }
    int f(int m, int i) {
        int16_t d = 5;
        int r = 0;
        switch (m) { case 1: r = 1; case 3: r = 2; break; default: r = 0; }
        if (d - 10 > 0) r += 1;
        return r + g_tab[i + 1] + helper(m);
    }
    """
    res, go, gn = _diff({"a.c": old}, {"a.c": new})
    t = _types(res)
    it = t["integer_type_change"]
    assert "unsigned -> signed" in it["summary"]
    # 정수 부호 변경: 변수 d의 모든 사용처(조건식 포함)가 영향 추적 시작점
    assert any("d - 10" in (gn.node(a).get("text") or "") for a in it["new"]["anchors"])
    assert "fallthrough_change" in t and "switch_case_change" in t
    assert t["array_size_change"]["summary"].startswith("g_tab")
    assert "array_index_change" in t
    assert "static -> extern" in t["linkage_change"]["summary"]


def test_text_coverage_inactive_and_comment_hunks():
    old = "int x;\n#ifdef OTHER_VARIANT\nint y = 1;\n#endif\n/* note */\nint z;\nint f(void) { return x; }\n"
    new = "int x;\n#ifdef OTHER_VARIANT\nint y = 2;\n#endif\n/* changed note */\nint z;\nint f(void) { return x + 1; }\n"
    res, go, gn = _diff({"a.c": old}, {"a.c": new})
    classes = {h["class"] for h in res["text_coverage"]}
    assert {"inactive_in_this_config", "comment_or_whitespace", "mapped"} <= classes
    assert "unmapped" not in classes
    inact = [c for c in res["changes"] if c["type"] == "inactive_code_change"]
    assert inact and "재분석" in inact[0]["summary"]


def test_symbol_integration_and_resources():
    files = {"a.h": "extern unsigned short g_v;\nint get(int a);\n",
             "a.c": '#include "a.h"\nunsigned int g_v;\nint get(int a) { return a; }\n',
             "b.c": '#include "a.h"\nstatic int s_c;\nint use(void) { char buf[16]; return get(g_v) + s_c + buf[0]; }\n',
             "c.c": "static int s_c;\nint other(void) { return s_c; }\n"}
    g, idx = graph_of(files)
    si = g.meta["summary"]["symbol_integration"]
    assert any(m["symbol"] == "gv:g_v" for m in si["type_mismatches"])     # extern 선언과 정의 자료형 불일치
    assert any(d["name"] == "s_c" for d in si["same_name_statics"])         # 파일별 static은 별도 심벌
    from vfia.resources import function_frames
    assert function_frames(g)["fn:use"]["stack"] >= 16


def test_rte_function_like_macro_becomes_signal_access():
    src = r"""
    int Rte_Buf_TqLimitActive;
    #define Rte_IWrite_RE_TqTask_TqLimitActive_Value(v) (Rte_Buf_TqLimitActive = (v))
    void Tq(int lim) { Rte_IWrite_RE_TqTask_TqLimitActive_Value(lim < 100); }
    """
    g, _ = graph_of({"a.c": src})
    vm = VehicleModel(tasks=[{"name": "RE_TqTask", "entry": "Tq", "kind": "runnable"}])
    apply_vehicle_model(g, vm)
    sid = "sig:TqLimitActive_Value"
    assert g.has_node(sid) and "implicit" in g.node(sid)["access"]
    writers = g.pred(sid, [EK.SIGNAL_WRITE])
    assert writers and g.kind(writers[0]) == NK.STMT and g.node(writers[0]).get("macro_calls")
