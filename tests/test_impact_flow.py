"""3단계(Call Graph, Def-Use, 별칭) / 4단계(CFG, 경로, 값 범위) 테스트."""

import unittest

from cimpact_util import HAVE_CLANG, SKIP_MSG, Fixture, load

SRC = {"m.c": """
    typedef unsigned char U8;
    typedef void (*Cb_t)(U8);
    U8 g_x; U8 g_y; U8 g_out;
    static Cb_t s_cb;
    void out(U8 v);
    static void on_evt(U8 v) { g_out = v; }
    static void on_err(U8 v) { g_out = (U8)(v + 1U); }
    static const Cb_t s_tbl[2] = { on_evt, on_err };
    void reg(Cb_t cb) { s_cb = cb; }
    void fire(U8 v) { if (s_cb != 0) { s_cb(v); } }
    void dispatch(U8 i) { s_tbl[i](i); }
    void setup(void) { reg(on_evt); }
    int fact(int n) { if (n <= 1) { return 1; } return n * fact(n - 1); }
    int ping(int n);
    int pong(int n) { return n > 0 ? ping(n - 1) : 0; }
    int ping(int n) { return n > 0 ? pong(n - 1) : 1; }
    void wr(U8 *p) { *p = 5U; }
    int f(U8 a) {
        U8 b = 0U;
        U8 c;
        if (a > 3U) { b = 1U; } else { b = 2U; }
        c = b;
        b = 7U;
        while (c < 10U) { c++; if (c == 5U) { break; } }
        switch (a) { case 1U: g_x = 1U; case 2U: g_x = 2U; break; default: g_y = 1U; }
        wr(&g_y);
        if (a > 200U) { if (a < 100U) { out(1U); } }
        return c;
    }
    void g(U8 cnt) {
    retry:
        cnt--;
        if (cnt > 0U) { goto retry; }
        for (;;) { if (cnt == 9U) { return; } }
        out(3U);
    }
    void h(U8 *p) { U8 *q = &g_x; *q = 1U; if (p != 0) { *p = 2U; } }
    void u8cmp(U8 k) { if (k >= 300U) { out(1U); } }
"""}


@unittest.skipUnless(HAVE_CLANG, SKIP_MSG)
class CallGraphTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from c_impact.callgraph import CallGraph
        from c_impact.config import AnalysisConfig
        cls.fx = Fixture(SRC)
        cls.p = load(cls.fx.base)
        cls.cg = CallGraph(cls.p, AnalysisConfig())

    @classmethod
    def tearDownClass(cls):
        cls.fx.close()

    def callees(self, f):
        return {(e["callee"], e["kind"], e["certainty"]) for e in self.cg.callees_of(f)}

    def test_callback_registered_through_parameter_is_potential_edge(self):
        self.assertEqual(self.callees("fire"), {("m.c::on_evt", "indirect", "potential")})
        e = self.cg.callees_of("fire")[0]
        self.assertIn("흘러드는", e["reason"])

    def test_function_pointer_table_candidates(self):
        self.assertEqual(self.callees("dispatch"), {("m.c::on_evt", "indirect", "potential"),
                                                     ("m.c::on_err", "indirect", "potential")})

    def test_recursion_and_cycles_terminate(self):
        cyc = self.cg.cycles()
        self.assertIn(["fact"], cyc)
        self.assertIn(["ping", "pong"], cyc)
        up = self.cg.upward("ping")
        self.assertIn("pong", up)
        chains = self.cg.chains_to_entries("ping")
        self.assertTrue(all(len(c["chain"]) == len(set(c["chain"])) for c in chains))

    def test_entry_points(self):
        self.assertIn("setup", self.cg.entry_points)
        self.assertEqual(self.cg.entry_points["m.c::on_err"]["kind"], "callback")
        self.assertNotIn("reg", self.cg.entry_points)


@unittest.skipUnless(HAVE_CLANG, SKIP_MSG)
class DataFlowCfgTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from c_impact.cfg import CFG
        from c_impact.dataflow import FunctionDF
        cls.fx = Fixture(SRC)
        cls.p = load(cls.fx.base)
        cls.g = CFG(cls.p.functions["f"])
        cls.df = FunctionDF(cls.p.functions["f"], cls.g)

    @classmethod
    def tearDownClass(cls):
        cls.fx.close()

    def node(self, text, g=None):
        g = g or self.g
        return next(n for n, d in g.nodes.items() if d["text"] == text)

    def test_reaching_definitions_kill(self):
        use = self.node("c = b")
        defs = {self.g.nodes[d]["text"] for d, k in self.df.ud[use]}
        self.assertEqual(defs, {"b = 1U", "b = 2U"})  # U8 b = 0U 는 두 분기에서 모두 재정의(kill)
        self.assertEqual(self.df.uses_of(self.node("b = 7U"), "L:b#0"), [])

    def test_upward_exposed_param_use(self):
        self.assertIn(self.node("if ( a > 3U )"), self.df.upward_uses("P:a"))

    def test_cfg_switch_fallthrough_and_break(self):
        sw = self.node("switch ( a )")
        labels = {lab for _, lab in self.g.succ[sw]}
        self.assertEqual(labels, {"case 1U", "case 2U", "default"})
        c1 = self.node("g_x = 1U")
        self.assertEqual([self.g.nodes[s]["text"] for s, _ in self.g.succ[c1]], ["case 2U :"])  # fall-through

    def test_control_dependence(self):
        cd = self.g.control_deps()
        inner = self.node("out ( 1U )")
        self.assertEqual([(self.g.nodes[b]["text"], lab) for b, lab in cd[inner]], [("if ( a < 100U )", "T")])
        self.assertIn(self.node("if ( a < 100U )"), self.g.dependents(self.node("if ( a > 200U )")))

    def test_paths_feasibility(self):
        paths, trunc = self.g.paths(500, 1)
        self.assertFalse(trunc)
        target = self.node("out ( 1U )")
        reach = [p for p in paths if target in [n for n, _ in p]]
        self.assertTrue(reach)
        self.assertTrue(all(self.g.feasible(p) is False for p in reach))  # a>200 ∧ a<100 모순
        feasible = [p for p in paths if self.g.feasible(p)]
        self.assertTrue(feasible)  # 반복문 확장(widening)으로 루프 뒤 경로는 실행 가능

    def test_path_limit(self):
        paths, trunc = self.g.paths(5, 1)
        self.assertEqual(len(paths), 5)
        self.assertTrue(trunc)

    def test_goto_infinite_loop_unreachable(self):
        from c_impact.cfg import CFG
        g = CFG(self.p.functions["g"])
        self.assertEqual([g.nodes[n]["text"] for n in g.unreachable_nodes()], ["out ( 3U )"])
        g.control_deps()  # 무한 루프가 있어도 종료

    def test_static_dead_branch_by_type_range(self):
        from c_impact.cfg import CFG
        g = CFG(self.p.functions["u8cmp"])
        res = g.static_branch_outcomes()
        self.assertEqual(len(res), 1)
        self.assertIs(res[0]["always"], False)

    def test_points_to(self):
        from c_impact.dataflow import PointsTo
        pts = PointsTo(self.p)
        self.assertEqual(pts.resolve("h", "*L:q#0"), ([(None, "G:g_x")], True))
        tg, certain = pts.resolve("wr", "*P:p")
        self.assertEqual(tg, [(None, "G:g_y")])
        self.assertEqual(pts.resolve("h", "*P:p"), ([], False))


class ValueRangeTests(unittest.TestCase):
    def test_refine_and_eval(self):
        from c_impact.valuerange import Env, eval_cond, expr_interval, refine, type_range
        u8 = {"kind": "int", "bits": 8, "signed": False}
        x = {"k": "var", "key": "G:x", "t": u8}

        def cmp(op, v):
            return {"k": "bin", "op": op, "l": x, "r": {"k": "const", "v": v}}
        self.assertEqual(type_range(u8), (0, 255))
        self.assertIs(eval_cond(Env(), cmp(">=", 300)), False)
        self.assertIs(eval_cond(Env(), cmp("<", 256)), True)
        self.assertIsNone(eval_cond(Env(), cmp(">", 10)))
        env = refine(Env(), cmp(">", 200), True)
        self.assertEqual(env.r["G:x"], (201, 255))
        self.assertIsNone(refine(env, cmp("<", 100), True))
        both = {"k": "bin", "op": "&&", "l": cmp(">", 5), "r": cmp("<", 3)}
        self.assertIs(eval_cond(Env(), both), False)
        mul = {"k": "bin", "op": "*", "l": x, "r": {"k": "const", "v": 150}}
        self.assertEqual(expr_interval({"k": "bin", "op": "/", "l": mul, "r": {"k": "const", "v": 4095}}), (0, 9))


if __name__ == "__main__":
    unittest.main()
