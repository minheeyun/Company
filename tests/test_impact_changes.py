"""2단계: 변경점 탐지/분류 테스트."""

import unittest

from cimpact_util import HAVE_CLANG, SKIP_MSG, Fixture, load

HDR = """
    typedef unsigned char U8;
    typedef unsigned short U16;
    #define THR 50U
    typedef struct { U8 a; U8 b; } S_t;
    typedef enum { ST_IDLE, ST_RUN, ST_ERR } St_t;
    void ext_out(U8 v);
"""
BASE = """
    #include "h.h"
    U8 g_a;
    U8 g_b = 3U;
    St_t g_state;
    int f_bound(U8 x) { if (x > THR) { return 1; } return 0; }
    int f_logic(U8 x, U8 y) { if ((x > 1U) && (y > 2U)) { return 1; } return 0; }
    int f_term(U8 x, U8 y) { if (x > 1U) { return 1; } return 0; }
    int f_termrm(U8 x, U8 y) { if ((x > 1U) && (y > 2U)) { return 1; } return 0; }
    int f_thr(U8 x) { if (x > 10U) { return 1; } return 0; }
    void f_assign(U8 x) { g_a = x * 2U; }
    void f_calladd(U8 x) { g_a = x; }
    void f_callrm(U8 x) { ext_out(x); g_a = x; }
    void f_args(U8 x) { ext_out(x); }
    int f_ret(U8 x) { return x + 1; }
    void f_sig(U8 x) { g_a = x; }
    void f_removed(void) { g_a = 0U; }
    void f_fmt(U8 x) { g_a = x; }
    void f_order(U8 x) { if (x > 10U) { g_a = 1U; } else if (x > 5U) { g_a = 2U; } else { g_a = 3U; } }
    void f_else(U8 x) { if (x > 1U) { g_a = 1U; } else { g_a = 0U; } }
    void f_case(St_t s) { switch (s) { case ST_IDLE: g_a = 0U; break; default: break; } }
    void f_loop(U8 n) { U8 i; for (i = 0U; i < n; i++) { ext_out(i); } }
    void f_neg(U8 x) { if (x == 0U) { g_a = 1U; } }
    void f_prec(U8 x, U8 y, U8 z) { if ((x > 1U && y > 1U) || z > 1U) { g_a = 1U; } }
    void f_local(void) { U8 t = 1U; g_a = t; }
    void f_fsm(void) { switch (g_state) { case ST_IDLE: g_state = ST_RUN; break; case ST_RUN: g_state = ST_ERR; break; default: g_state = ST_IDLE; break; } }
"""
CURR = """
    #include "h.h"
    U8 g_a;
    U8 g_b = 4U;
    St_t g_state;
    int f_bound(U8 x) { if (x >= THR) { return 1; } return 0; }
    int f_logic(U8 x, U8 y) { if ((x > 1U) || (y > 2U)) { return 1; } return 0; }
    int f_term(U8 x, U8 y) { if ((x > 1U) && (y != 0U)) { return 1; } return 0; }
    int f_termrm(U8 x, U8 y) { if (x > 1U) { return 1; } return 0; }
    int f_thr(U8 x) { if (x > 20U) { return 1; } return 0; }
    void f_assign(U8 x) { g_a = x * 3U; }
    void f_calladd(U8 x) { ext_out(x); g_a = x; }
    void f_callrm(U8 x) { g_a = x; }
    void f_args(U8 x) { ext_out(x + 1U); }
    int f_ret(U8 x) { return x + 2; }
    void f_sig(U16 x) { g_a = (U8)x; }
    void f_new(void) { g_a = 9U; }

    /* 주석 추가 + 포맷 변경 */
    void f_fmt( U8 x )
    {
        g_a = x;   /* same */
    }
    void f_order(U8 x) { if (x > 5U) { g_a = 2U; } else if (x > 10U) { g_a = 1U; } else { g_a = 3U; } }
    void f_else(U8 x) { if (x > 1U) { g_a = 1U; } }
    void f_case(St_t s) { switch (s) { case ST_IDLE: g_a = 0U; break; case ST_RUN: g_a = 1U; break; default: break; } }
    void f_loop(U8 n) { U8 i; for (i = 0U; i <= n; i++) { ext_out(i); } }
    void f_neg(U8 x) { if (!(x == 0U)) { g_a = 1U; } }
    void f_prec(U8 x, U8 y, U8 z) { if (x > 1U && (y > 1U || z > 1U)) { g_a = 1U; } }
    void f_local(void) { U16 t = 1U; g_a = (U8)t; }
    void f_fsm(void) { switch (g_state) { case ST_IDLE: g_state = ST_RUN; break; case ST_RUN: g_state = ST_IDLE; break; default: g_state = ST_IDLE; break; } }
"""
HDR2 = HDR.replace("#define THR 50U", "#define THR 60U").replace("U8 a; U8 b;", "U8 a; U16 b;").replace(
    "ST_RUN, ST_ERR", "ST_RUN = 5, ST_ERR")


@unittest.skipUnless(HAVE_CLANG, SKIP_MSG)
class ChangeDetectTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from c_impact.change_detect import detect_changes
        from c_impact.config import AnalysisConfig
        from c_impact.roles import Roles
        cls.fx = Fixture({"inc/h.h": HDR, "src/m.c": BASE}, {"inc/h.h": HDR2, "src/m.c": CURR})
        cfg = AnalysisConfig()
        b, c = load(cls.fx.base), load(cls.fx.curr)
        cls.cs = detect_changes(b, c, Roles(b, cfg), Roles(c, cfg), cfg)

    @classmethod
    def tearDownClass(cls):
        cls.fx.close()

    def of(self, fn):
        return [c for c in self.cs.changes if c.get("function") == fn]

    def types(self, fn):
        return {t for c in self.of(fn) for t in c["types"]}

    def test_boundary_operator(self):
        ch = self.of("f_bound")
        self.assertEqual(len(ch), 1)
        self.assertIn("BOUNDARY_OPERATOR", ch[0]["types"])
        self.assertEqual(ch[0]["detail"]["operators"][0], {"from": ">", "to": ">=", "at_from": "x > THR",
                                                           "at_to": "x >= THR"})
        # 매크로 값 변화(50→60)는 문장 변경이 아니라 매크로 변경점으로만 보고
        self.assertNotIn("THRESHOLD_CHANGED", ch[0]["types"])
        self.assertEqual(ch[0]["base"]["line"], 5)
        self.assertIn("x > THR", ch[0]["base"]["code"])
        self.assertIn("x >= THR", ch[0]["curr"]["code"])

    def test_logical_and_terms(self):
        self.assertIn("LOGICAL_OPERATOR", self.types("f_logic"))
        self.assertIn("CONDITION_TERM_ADDED", self.types("f_term"))
        self.assertIn("CONDITION_TERM_REMOVED", self.types("f_termrm"))
        self.assertIn("NEGATION_CHANGED", self.types("f_neg"))
        self.assertIn("PRECEDENCE_CHANGED", self.types("f_prec"))

    def test_threshold_constant(self):
        ch = self.of("f_thr")[0]
        self.assertIn("THRESHOLD_CHANGED", ch["types"])
        self.assertEqual(ch["detail"]["thresholds"], [{"var": "x", "from": 10, "to": 20, "op": ">"}])

    def test_assignment_call_return(self):
        self.assertEqual(self.types("f_assign"), {"CONSTANT_CHANGED", "ASSIGNMENT_CHANGED"})
        self.assertIn("CALL_ADDED", self.types("f_calladd"))
        self.assertIn("CALL_REMOVED", self.types("f_callrm"))
        self.assertIn("CALL_ARGS_CHANGED", self.types("f_args"))
        self.assertIn("RETURN_CHANGED", self.types("f_ret"))

    def test_function_level(self):
        self.assertEqual(self.types("f_removed"), {"FUNCTION_REMOVED"})
        self.assertEqual(self.types("f_new"), {"FUNCTION_ADDED"})
        self.assertTrue({"SIGNATURE_CHANGED", "PARAM_CHANGED"} <= self.types("f_sig"))
        self.assertIn("LOCAL_VAR_CHANGED", self.types("f_local"))

    def test_format_only_is_not_semantic(self):
        self.assertEqual(self.of("f_fmt"), [])
        self.assertTrue(any(n["type"] == "FORMAT_ONLY" and n.get("function") == "f_fmt" for n in self.cs.non_semantic))
        self.assertFalse(any("UNMAPPED_CHANGE" in c["types"] for c in self.cs.changes))

    def test_control_structure_changes(self):
        order = self.of("f_order")
        self.assertEqual(len(order), 1)  # 순서만 바뀐 분기는 하나의 변경점
        self.assertIn("BRANCH_ORDER_CHANGED", order[0]["types"])
        self.assertIn("ELSE_REMOVED", self.types("f_else"))
        case = self.of("f_case")
        self.assertEqual(len(case), 1)  # case 라벨과 본문은 묶어서 하나
        self.assertIn("CASE_ADDED", case[0]["types"])
        self.assertEqual(len(case[0]["curr_stmts"]), 3)
        self.assertTrue({"LOOP_CONDITION_CHANGED", "BOUNDARY_OPERATOR"} <= self.types("f_loop"))

    def test_state_transition(self):
        summary = next(c for c in self.of("f_fsm") if c.get("summary_only"))
        self.assertEqual(summary["detail"]["transitions_removed"], [["ST_RUN", "ST_ERR"]])
        self.assertEqual(summary["detail"]["transitions_added"], [["ST_RUN", "ST_IDLE"]])
        self.assertEqual(summary["category"], "STATE")

    def test_globals_macros_types(self):
        g = next(c for c in self.cs.changes if c.get("variable") == "G:g_b")
        self.assertEqual(g["types"], ["GLOBAL_INIT_CHANGED"])
        self.assertEqual((g["detail"]["init_from"], g["detail"]["init_to"]), ("3U", "4U"))
        m = next(c for c in self.cs.changes if c.get("macro_name") == "THR")
        self.assertEqual(m["detail"], {"from": "50U", "to": "60U"})
        self.assertTrue(any(c.get("type_name") == "S_t" and "STRUCT_CHANGED" in c["types"] for c in self.cs.changes))
        self.assertTrue(any(c.get("type_name") == "St_t" and "ENUM_CHANGED" in c["types"] for c in self.cs.changes))

    def test_every_change_has_location_and_labels(self):
        for c in self.cs.changes:
            self.assertTrue(c.get("base") or c.get("curr"), c)
            self.assertEqual(len(c["labels"]), len(c["types"]))
            loc = c.get("curr") or c.get("base")
            self.assertTrue(loc["file"] and loc["line"])


@unittest.skipUnless(HAVE_CLANG, SKIP_MSG)
class MovedAndInactiveTests(unittest.TestCase):
    def test_line_shift_and_inactive_code(self):
        from c_impact.change_detect import detect_changes
        from c_impact.config import AnalysisConfig
        from c_impact.roles import Roles
        base = {"a.c": """
            int g;
            void f(void) { g = 1; }
            void h(void) {
            #ifdef OPT
                g = 2;
            #endif
            }
        """}
        curr = {"a.c": """
            int g;


            void f(void) { g = 1; }
            void h(void) {
            #ifdef OPT
                g = 3;
            #endif
            }
        """}
        fx = Fixture(base, curr)
        try:
            cfg = AnalysisConfig()
            b, c = load(fx.base), load(fx.curr)
            cs = detect_changes(b, c, Roles(b, cfg), Roles(c, cfg), cfg)
            self.assertTrue(any(n["type"] == "FUNCTION_MOVED" and n["function"] == "f" for n in cs.non_semantic))
            self.assertEqual([c["types"] for c in cs.changes], [["INACTIVE_CODE_CHANGED"]])
            # 매크로를 정의한 빌드 설정에서는 실제 문장 변경
            b2, c2 = load(fx.base, defines=["OPT"]), load(fx.curr, defines=["OPT"])
            cs2 = detect_changes(b2, c2, Roles(b2, cfg), Roles(c2, cfg), cfg)
            self.assertEqual(cs2.changes[0]["function"], "h")
            self.assertIn("CONSTANT_CHANGED", cs2.changes[0]["types"])
        finally:
            fx.close()


if __name__ == "__main__":
    unittest.main()
