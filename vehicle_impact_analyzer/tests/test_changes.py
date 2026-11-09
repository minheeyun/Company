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


def test_interference_locks_init_filter_and_mmio():
    src = r"""
    #define ADC_RESULT (*(volatile unsigned int *)0xF0001000u)
    #define PWM_DUTY   (*(volatile unsigned int *)0xF0003000u)
    void SuspendAllInterrupts(void); void ResumeAllInterrupts(void);
    volatile unsigned short g_raw; unsigned long long g_wide; int g_cfg; int g_out;
    void Isr(void) { g_raw = (unsigned short)ADC_RESULT; g_wide = 1ULL; }
    void Init(void) { g_cfg = 3; int x = g_out; (void)x; }
    void Task(void) {
        unsigned short r;
        SuspendAllInterrupts();
        r = g_raw;
        ResumeAllInterrupts();
        g_out = r + g_cfg + (int)g_wide;
        PWM_DUTY = (unsigned int)g_out;
    }
    """
    g, _ = graph_of({"a.c": src})
    vm = VehicleModel(tasks=[{"name": "ISR_Adc", "entry": "Isr", "kind": "isr", "priority": 30},
                             {"name": "Init", "entry": "Init", "kind": "init", "priority": 1},
                             {"name": "Task_10ms", "entry": "Task", "kind": "task", "priority": 5}])
    apply_vehicle_model(g, vm)
    inter = [(g.node(s).get("text"), g.node(d).get("text"), e) for s, d, e in g.g.edges(data=True)
             if e["kind"] == EK.INTERFERENCE]
    raw = [e for s, d, e in inter if "g_raw" in s and "r = g_raw" in d]
    # ISR이 쓰고 태스크가 인터럽트 잠금 안에서 읽으므로 보호됨(ISR은 잠금 구간에 끼어들 수 없음)
    assert raw and raw[0]["preemptive"] and raw[0]["protected"]
    reader = next(n for n in g.nodes_of(NK.STMT) if g.node(n).get("text") == "r = g_raw")
    assert g.node(reader).get("critical")                                # 읽기는 인터럽트 잠금 안
    wide = [e for s, d, e in inter if "g_wide" in s]
    assert wide and wide[0]["atomic"] is False                          # 64비트 > 원자 폭 32비트
    # 주기 태스크의 쓰기(g_out)는 먼저 끝나는 Init의 읽기에 도달할 수 없음 -> 전역 읽기 간선 제거
    filtered = g.meta["interference"]["filtered_flows"]
    assert any(f["location"] == "g_out" for f in filtered)
    init_read = next(n for n in g.nodes_of(NK.STMT) if "int x = g_out" in (g.node(n).get("text") or ""))
    assert not g.pred(init_read, [EK.GLOBAL_READ])
    # MMIO: 레지스터 주소가 이름 있는 입력·관찰 위치가 됨
    mm = {g.node(k).get("name"): k for k in g.nodes_of(NK.GLOBAL) if g.node(k).get("mmio")}
    assert {"ADC_RESULT", "PWM_DUTY"} <= set(mm)
    assert g.pred(f"loc:{mm['PWM_DUTY']}", [EK.GLOBAL_WRITE])


def test_symexec_rte_implicit_calibration_mmio_memcpy():
    import z3
    from vfia.symexec import Program, SymExec
    src = r"""
    typedef struct { int a; int b; } S;
    S g_src, g_dst;
    const volatile int Cal_Gain = 2;
    int Rte_IRead_RE_X_Req_Value(void); void Rte_IWrite_RE_X_Cmd_Value(int v);
    void *memcpy(void *d, const void *s, unsigned long n);
    #define REG (*(volatile unsigned int *)0xF0002000u)
    int step(void) {
        int r1 = Rte_IRead_RE_X_Req_Value();
        Rte_IWrite_RE_X_Cmd_Value(r1 * Cal_Gain);
        int r2 = Rte_IRead_RE_X_Req_Value();      /* 같은 runnable 안: 시작 시 복사본 */
        memcpy(&g_dst, &g_src, sizeof(S));
        REG = (unsigned int)r2;
        return r1 - r2 + g_dst.b;
    }
    """
    g, _ = graph_of({"a.c": src})
    rmap = {"Rte_IRead_RE_X_Req_Value": {"signal": "Req_Value", "access": "implicit", "direction": "read"},
            "Rte_IWrite_RE_X_Cmd_Value": {"signal": "Cmd_Value", "access": "implicit", "direction": "write"}}
    p = Program(g.asts, "p", calibrations={"gv:Cal_Gain": (1, 4)}, rte_map=rmap)
    ex = SymExec(p)
    ex.begin_step()
    r = ex.run("fn:step", [])
    assert "rte:Cmd_Value" not in ex.mem          # 암묵적 쓰기는 runnable 종료 후 반영
    ex.end_step()
    assert "rte:Cmd_Value" in ex.mem and "cal:gv:Cal_Gain" in str(ex.mem["rte:Cmd_Value"])
    s = z3.Solver()
    s.add(r != ex.mem["gv:g_src|b"])
    assert s.check() == z3.unsat                    # r1 == r2, memcpy로 g_dst.b == g_src.b
    assert "gv:mmio:0xF0002000" in ex.mmio_writes
