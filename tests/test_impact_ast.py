"""1단계: AST 기반 코드 구조 분석 테스트."""

import unittest

from cimpact_util import DEMO, HAVE_CLANG, SKIP_MSG, Fixture, load

SRC = {
    "inc/t.h": """
        typedef unsigned char U8;
        typedef struct { U8 mode; U8 cnt; } Ctx_t;
        typedef void (*Cb_t)(void);
        #define LIMIT (10U * 2U)
        extern U8 g_out;
        void ext_drv(U8 v);
    """,
    "src/a.c": """
        #include "t.h"
        U8 g_out;
        static Ctx_t s_ctx;
        static int helper(int v) { return v + 1; }
        static void cb1(void) { g_out = 1U; }
        static const Cb_t s_tbl[1] = { cb1 };
        int A(U8 x) {
            U8 i = x;
            Ctx_t *p = &s_ctx;
            static U8 s_calls;
            { U8 i = 3U; g_out = i; }
            if (i > LIMIT) { p->mode = 1U; }
            s_calls++;
            s_tbl[0]();
        #if 0
            ext_drv(9U);
        #endif
            return helper(i);
        }
    """,
    "src/b.c": """
        #include "t.h"
        static int helper(int v) { return v * 2; }
        void B(void) { ext_drv((U8)helper(LIMIT)); }
    """,
}


@unittest.skipUnless(HAVE_CLANG, SKIP_MSG)
class AstTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fx = Fixture(SRC)
        cls.p = load(cls.fx.base)

    @classmethod
    def tearDownClass(cls):
        cls.fx.close()

    def stmts(self, fid):
        return list(self.p.stmt_index(fid).values())

    def test_static_functions_with_same_name_are_distinct_symbols(self):
        self.assertIn("src/a.c::helper", self.p.functions)
        self.assertIn("src/b.c::helper", self.p.functions)
        self.assertIn("A", self.p.functions)  # 외부 연결은 이름 그대로
        a_calls = [c["callee"] for c in self.p.functions["A"]["calls"] if not c["indirect"]]
        b_calls = [c["callee"] for c in self.p.functions["B"]["calls"]]
        self.assertIn("src/a.c::helper", a_calls)
        self.assertIn("src/b.c::helper", b_calls)
        self.assertNotIn("src/a.c::helper", b_calls)

    def test_shadowed_locals_get_different_ids(self):
        names = sorted(k for k, v in self.p.functions["A"]["locals"].items() if v["name"] == "i")
        self.assertEqual(names, ["L:i#0", "L:i#1"])
        st = next(s for s in self.stmts("A") if s["text"].startswith("g_out = i"))
        self.assertEqual(st["uses"], ["L:i#1"])  # 내부 블록의 i 를 참조
        cond = next(s for s in self.stmts("A") if s["kind"] == "if")
        self.assertIn("L:i#0", cond["uses"])

    def test_static_globals_and_static_locals(self):
        self.assertIn("src/a.c::s_ctx", self.p.globals)
        self.assertIn("A::s_calls", self.p.globals)
        inc = next(s for s in self.stmts("A") if s["text"] == "s_calls ++")
        self.assertEqual(inc["rmw"], ["G:A::s_calls"])

    def test_pointer_field_access_and_alias(self):
        st = next(s for s in self.stmts("A") if "mode" in s["text"])
        self.assertEqual(st["defs"], ["*L:p#0.mode"])
        self.assertIn("F:Ctx_t.mode", st["fdefs"])
        decl = next(s for s in self.stmts("A") if s.get("decl") == "L:p#0")
        self.assertIn(("L:p#0", "G:src/a.c::s_ctx"), [tuple(x) for x in decl["ptr_assign"]])

    def test_condition_expression_tree_with_macro_value(self):
        cond = next(s for s in self.stmts("A") if s["kind"] == "if")["cond"]
        self.assertEqual(cond["k"], "bin")
        self.assertEqual(cond["op"], ">")
        self.assertEqual(cond["r"]["k"], "const")
        self.assertEqual(cond["r"]["v"], 20)          # 매크로 전개 값
        self.assertEqual(cond["r"]["text"], "LIMIT")  # 원문 매크로 이름 보존
        self.assertEqual(cond["l"]["t"]["bits"], 8)

    def test_macro_definitions_and_uses(self):
        self.assertIn("inc/t.h::LIMIT", self.p.macros)
        uses = [m for m in self.p.functions["A"]["macro_uses"] if m["name"] == "LIMIT"]
        self.assertTrue(uses and uses[0]["stmt"] is not None)
        self.assertEqual(uses[0]["def"], "inc/t.h::LIMIT")

    def test_function_pointer_table_and_indirect_call(self):
        self.assertIn({"target": "G:src/a.c::s_tbl", "func": "src/a.c::cb1", "line": 6},
                      self.p.global_fp_bindings)
        ind = [c for c in self.p.functions["A"]["calls"] if c["indirect"]]
        self.assertEqual(len(ind), 1)
        self.assertEqual(ind[0]["fp_key"], "G:src/a.c::s_tbl")

    def test_external_function_and_inactive_region(self):
        call = next(c for c in self.p.functions["B"]["calls"] if c["name"] == "ext_drv")
        self.assertTrue(call["external"])
        self.assertTrue(any(f == "src/a.c" for f, _, _ in self.p.skipped))
        self.assertFalse(any(c["name"] == "ext_drv" for c in self.p.functions["A"]["calls"]))

    def test_typedefs_records(self):
        self.assertEqual(self.p.typedefs["U8"]["canon"], "unsigned char")
        self.assertEqual([f[0] for f in self.p.records["Ctx_t"]["fields"]], ["mode", "cnt"])


@unittest.skipUnless(HAVE_CLANG, SKIP_MSG)
class AstParseFailureTests(unittest.TestCase):
    def test_parse_error_is_reported_not_hidden(self):
        fx = Fixture({"bad.c": "int f(void) { return undefined_symbol + ; }\n", "ok.c": "int g(void) { return 1; }\n"})
        try:
            p = load(fx.base)
            self.assertEqual([f["file"] for f in p.failures], ["bad.c"])
            self.assertIn("g", p.functions)
        finally:
            fx.close()

    def test_demo_project_parses(self):
        p = load(str(DEMO / "current"))
        self.assertEqual(p.failures, [])
        self.assertGreaterEqual(len(p.functions), 25)


if __name__ == "__main__":
    unittest.main()
