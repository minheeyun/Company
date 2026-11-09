"""핵심 알고리즘 단위 시험: CFG/CDG, Reaching Definitions, AST 차이, HRB 슬라이싱, 기호 실행."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import z3  # noqa: E402

from helpers import graph_of  # noqa: E402
from vfia.ast_diff import diff  # noqa: E402
from vfia.change_detector import detect_changes  # noqa: E402
from vfia.model import EK, NK  # noqa: E402
from vfia.slicing import backward_slice, forward_slice  # noqa: E402
from vfia.symexec import Program, SymExec  # noqa: E402


def stmt(g, func, text):
    for n in g.nodes_of(NK.STMT):
        d = g.node(n)
        if d.get("func") == func and text in (d.get("text") or ""):
            return n
    raise AssertionError(f"{func}: '{text}' 문장 없음")


SRC = r"""
int g_out;
int f(int a, int c) {
    int x = 1;
    if (c > 3) {
        x = 2;
        if (a) return 0;
    }
    while (a < 10) {
        if (a == 5) break;
        a++;
    }
    g_out = x + a;
    return x;
}
"""


def test_control_dependence_and_reaching_defs():
    g, _ = graph_of({"a.c": SRC})
    f = "fn:f"
    cond = stmt(g, f, "c > 3")
    x2 = stmt(g, f, "x = 2")
    ret0 = stmt(g, f, "return 0")
    out = stmt(g, f, "g_out = x + a")
    # 제어 의존: x=2 는 (c>3)의 T 분기에 의존
    assert any(s == cond and d["label"] == "T" for s, _, d in g.in_edges(x2, [EK.CONTROL]))
    # NTSCD: 루프가 끝나지 않을 수 있으므로 g_out은 루프 탈출 조건(a<10 F, a==5 T)에 제어 의존하고,
    # 루프 조건은 조기 return 조건(if (a) F)에 제어 의존한다(전이적으로 연결).
    ctrls = {(g.node(s).get("text"), d["label"]) for s, _, d in g.in_edges(out, [EK.CONTROL])}
    assert ("a < 10", "F") in ctrls and ("a == 5", "T") in ctrls
    loop_ctrls = {(g.node(s).get("text"), d["label"]) for s, _, d in g.in_edges(stmt(g, f, "a < 10"), [EK.CONTROL])}
    assert ("a", "F") in loop_ctrls
    # 데이터 의존: g_out = x + a 에 x=1, x=2 두 정의가 모두 도달
    defs = {s for s, _, d in g.in_edges(out, [EK.DATA]) if d["label"] == "x"}
    assert stmt(g, f, "int x = 1") in defs and x2 in defs
    # 반복문: a++ 정의가 루프 조건에 도달(고정점)
    inc = stmt(g, f, "a++")
    loopc = stmt(g, f, "a < 10")
    assert any(s == inc for s, _, _ in g.in_edges(loopc, [EK.DATA]))
    assert ret0 in g.g


def test_short_circuit_decomposition():
    g, _ = graph_of({"a.c": "int h(void); int k(int a){ if (a && h()) return 1; return 0; }"})
    a_cond = stmt(g, "fn:k", "a")
    cs = [n for n in g.nodes_of(NK.CALL_SITE) if g.node(n).get("func") == "fn:k"][0]
    # h() 호출은 a가 참일 때만 실행: 호출 정점이 a 조건에 제어 의존
    assert any(s == a_cond for s, _, _ in g.in_edges(cs, [EK.CONTROL]))


def test_ast_diff_threshold_and_operator():
    a, _ = graph_of({"a.c": "int f(int s, int ok){ if (ok && s >= 30) return 80; return 100; }"})
    b, _ = graph_of({"a.c": "int f(int s, int ok){ if (ok && s > 40) return 80; return 100; }"})
    _, _, m, acts = diff(a.asts["fn:f"], b.asts["fn:f"])
    ups = sorted(x.detail for x in acts if x.op == "update")
    assert ups == ["30 -> 40", ">= -> >"]
    res, _ = detect_changes(a, b)
    types = {c["type"] for c in res["changes"]}
    assert "comparison_operator_change" in types


def test_hrb_context_sensitive_slice():
    src = r"""
    int id(int v) { return v; }
    int out_a, out_b;
    void A(int x) { out_a = id(x + 1); }
    void B(int y) { out_b = id(y); }
    """
    g, _ = graph_of({"a.c": src})
    seed = stmt(g, "fn:A", "out_a = id(x + 1)")
    # x+1 인자(actual-in)에서 시작한 전방 슬라이스는 B의 out_b로 새어 나가면 안 된다
    ai = [n for n in g.nodes_of(NK.ACTUAL_IN) if g.node(n).get("func") == "fn:A"][0]
    fs = forward_slice(g, [ai])
    assert "loc:gv:out_a" in fs
    assert "loc:gv:out_b" not in fs
    bs = backward_slice(g, ["loc:gv:out_b"])
    assert stmt(g, "fn:B", "out_b = id(y)") in bs
    assert seed not in bs


def test_function_pointer_table_resolution():
    src = r"""
    int g1, g2;
    static void cb1(void) { g1 = 1; }
    static void cb2(void) { g2 = 1; }
    typedef struct { int id; void (*cb)(void); } E;
    static const E tbl[2] = { {0, cb1}, {1, cb2} };
    void run(int i) { tbl[i].cb(); }
    """
    g, _ = graph_of({"a.c": src})
    callees = {d for _, d, e in g.out_edges("fn:run", [EK.CALLS])}
    assert {"fn:a.c:cb1", "fn:a.c:cb2"} <= callees


def _prog(src):
    g, _ = graph_of({"a.c": src})
    return Program(g.asts, "p")


def test_symexec_c_integer_semantics():
    p = _prog(r"""
    unsigned char add(unsigned char a, unsigned char b) { return a + b; }
    int cmp(int a, unsigned int b) { return a < b; }
    """)
    ex = SymExec(p)
    r = ex.run("fn:add", [z3.BitVecVal(200, 8), z3.BitVecVal(100, 8)])
    assert z3.simplify(r).as_long() == 44            # 300 mod 256
    ex = SymExec(p)
    r = ex.run("fn:cmp", [z3.BitVecVal(-1 % 2**32, 32), z3.BitVecVal(1, 32)])
    assert z3.simplify(r).as_long() == 0             # -1 은 unsigned로 변환되어 1보다 큼


def test_symexec_xor_region_example():
    old = _prog("int L(unsigned short s, _Bool ok){ if (ok && s >= 30U) return 80; return 100; }")
    new = _prog("int L(unsigned short s, _Bool ok){ if (ok && s >= 40U) return 80; return 100; }")
    s_ = z3.BitVec("s", 16)
    ok = z3.BitVec("ok", 8)
    ro = SymExec(old).run("fn:L", [s_, ok])
    rn = SymExec(new).run("fn:L", [s_, ok])
    sol = z3.Solver()
    sol.add(z3.ULE(ok, 1), ro != rn)
    assert sol.check() == z3.sat
    sol.add(z3.Or(z3.ULT(s_, 30), z3.UGE(s_, 40), ok == 0))
    assert sol.check() == z3.unsat                    # 차이는 30 <= s < 40, ok=1 에서만


def test_symexec_loop_and_state():
    p = _prog(r"""
    static unsigned char cnt;
    unsigned char tick(int on) { if (on) { if (cnt < 255U) cnt++; } else { cnt = 0; } return cnt; }
    int sum(int n) { int s = 0; for (int i = 0; i < n; i++) { s += i; } return s; }
    """)
    ex = SymExec(p)
    for _ in range(3):
        r = ex.run("fn:tick", [z3.BitVecVal(1, 32)])
    assert z3.simplify(r).as_long() == 3
    ex = SymExec(p, loop_bound=8)
    r = ex.run("fn:sum", [z3.BitVecVal(4, 32)])
    assert z3.simplify(r).as_long() == 6
    sol = z3.Solver()
    sol.add(ex.unwind)
    assert sol.check() == z3.unsat                    # n=4는 전개 한도 안


def test_ntscd_busy_wait_and_infinite_task_loop():
    src = r"""
    volatile int ready; int x, y;
    void wait_then_set(void) { while (!ready) { } x = 1; }
    void task_main(void) { for (;;) { y = y + 1; } }
    """
    g, _ = graph_of({"a.c": src})
    xs = stmt(g, "fn:wait_then_set", "x = 1")
    ctrls = {g.node(s).get("text") for s, _, _ in g.in_edges(xs, [EK.CONTROL])}
    assert "!ready" in ctrls or "ready" in ctrls      # 대기 루프가 끝나야 실행(비종료 민감)
    ys = stmt(g, "fn:task_main", "y = y + 1")
    # 종료하지 않는 태스크 본체는 분기가 없으므로 진입에만 의존
    assert {s for s, _, _ in g.in_edges(ys, [EK.CONTROL])} == {"fn:task_main#entry"}


def test_context_sensitive_chop_excludes_unrealizable_nodes():
    src = r"""
    int id(int v) { return v; }
    int out_a, out_b;
    void A(int x) { out_a = id(x + 1); }
    void B(int y) { out_b = id(y); }
    """
    from vfia.slicing import chop, naive_chop
    g, _ = graph_of({"a.c": src})
    ai = [n for n in g.nodes_of(NK.ACTUAL_IN) if g.node(n).get("func") == "fn:A"][0]
    c = chop(g, [ai], ["loc:gv:out_a"])
    assert "loc:gv:out_a" in c.reached_targets()
    assert stmt(g, "fn:id", "return v") in c.nodes          # 짝이 맞는 호출 내부(same-level chop)
    assert not any(g.func_of(n) == "fn:B" for n in c.nodes)
    assert c.nodes <= naive_chop(g, [ai], ["loc:gv:out_a"])
    assert chop(g, [ai], ["loc:gv:out_b"]).reached_targets() == []


def test_harmonic_one_vs_rest_matches_networkx_argmax_and_is_multilabel():
    import networkx as nx
    from vfia.feature_mapper import harmonic_scores
    G = nx.Graph()
    G.add_weighted_edges_from([("a", "x", 1), ("x", "y", 1), ("y", "b", 1), ("x", "s", 1), ("s", "y", 1)])
    fa = harmonic_scores(G, {"a"}, {"b"})
    fb = harmonic_scores(G, {"b"}, {"a"})
    assert fa["x"] > 0.5 > fa["y"] and abs(fa["s"] - 0.5) < 1e-4      # s는 두 기능에 같은 정도로 걸침
    assert abs(fa["s"] + fb["s"] - 1.0) < 1e-4
    try:
        import scipy  # noqa: F401
    except ImportError:
        return
    for n in G:
        G.nodes[n].pop("label", None)
    G.nodes["a"]["label"], G.nodes["b"]["label"] = "A", "B"
    pred = dict(zip(G.nodes, nx.node_classification.harmonic_function(G)))
    for n in ("x", "y"):
        assert pred[n] == ("A" if fa[n] > 0.5 else "B")


def test_cochange_rules():
    from vfia.cochange import CoChange
    cc = CoChange([{"a.c", "b.c"}, {"a.c", "b.c", "x.arxml"}, {"a.c", "c.c"}, {"b.c"}], "t")
    c, s = cc.confidence("a.c", "b.c")
    assert s == 2 and abs(c - 2 / 3) < 1e-9
    assert cc.rules_from(["a.c"], min_support=2)[0]["entity"] == "b.c"


def test_interval_analysis_widening_and_dead_branch():
    from vfia.intervals import IntervalAnalysis, interval_report
    src = r"""
    int f(unsigned char u) {
        int i, s = 0;
        for (i = 0; i < 10; i++) { s = s + 1; }
        if (i > 20) { return -1; }          /* 루프 뒤 i == 10 -> 항상 거짓 */
        if (u > 300) { return -2; }         /* uint8 범위 -> 항상 거짓 */
        if (u > 100) { return 1; }
        return s;
    }
    """
    g, _ = graph_of({"a.c": src})
    res = IntervalAnalysis("fn:f", g.asts["fn:f"]).run()
    by_text = {g.node(n).get("text"): v for n, v in res.items() if g.has_node(n)}
    assert by_text["u > 300"]["can_true"] is False
    assert by_text["u > 100"]["can_true"] and by_text["u > 100"]["can_false"]
    assert by_text["i > 20"]["can_true"] is False            # widening 후 narrowing 또는 루프 탈출 정제로 i ∈ [10, ..]
    dead = {d["text"] for d in interval_report(g)["static_branches"]}
    assert {"u > 300", "i > 20"} <= dead


def test_condition_boxes_exact_example_and_sentence():
    import z3
    from vfia.conditions import InputVar, condition_text, derive_boxes, explain_box, feature_sentence
    req, spd, ok = z3.BitVec("req", 16), z3.BitVec("spd", 16), z3.BitVec("ok", 8)
    def lim(t):
        return z3.If(z3.And(ok != 0, z3.UGE(spd, t)), z3.BitVecVal(80, 16), z3.BitVecVal(100, 16))
    o = z3.If(req < lim(30), req, lim(30))
    n = z3.If(req < lim(40), req, lim(40))
    vs = [InputVar(req, "request_pct", True, 0, 100), InputVar(spd, "speed_kph", False, 0, 300),
          InputVar(ok, "sensor_ok", False, 0, 1)]
    base = [req >= 0, req <= 100, z3.ULE(spd, 300), z3.ULE(ok, 1)]
    r = derive_boxes(base, o != n, vs)
    assert r["exact"] and condition_text(r) == "request_pct ≥ 81 ∧ 30 ≤ speed_kph ≤ 39 ∧ sensor_ok = 1"
    e = explain_box(base, o != n, r["boxes"][0], vs, [o], [n], True)
    assert e["old"] == "80" and e["new"] == "request_pct" and e["delta"] == [1, 20] and e["direction"] == "증가"
    assert "반환값이 변경 전 80 → 변경 후 request_pct" in feature_sentence("F", "f() 반환값", "c", e, False)
