import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from c_impact.analysis import load_programs, run_analysis  # noqa: E402
from c_impact.frontend import BuildConfig  # noqa: E402


def analyze(files_old: dict, files_new: dict) -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        for ver, files in (("old", files_old), ("new", files_new)):
            for rel, text in files.items():
                p = root / ver / rel
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(textwrap.dedent(text), encoding="utf-8")
        cfg = BuildConfig()
        old, new = load_programs(root / "old", root / "new", cfg)
        return run_analysis(old, new, cfg)


class StaticPrototypeTests(unittest.TestCase):
    def test_static_prototype_then_definition_without_static(self):
        # AUTOSAR 콜아웃에 흔한 형태: static 원형 뒤에 static 없는 정의 (KeyError 회귀)
        src = """
        static int Helper(int x);
        int g;
        int Helper(int x) { return x + {K}; }
        void Api(int a) { g = Helper(a); }
        """
        r = analyze({"a.c": src.replace("{K}", "1")}, {"a.c": src.replace("{K}", "2")})
        self.assertIn(("Api", "g"), {(i["func_display"], i["name"]) for i in r["items"]})

    def test_same_static_name_in_two_files(self):
        a = "static int H(void) { return {K}; }\nint ga; void A(void) { ga = H(); }\n"
        b = "static int H(void) { return 7; }\nint gb; void B(void) { gb = H(); }\n"
        r = analyze({"a.c": a.replace("{K}", "1"), "b.c": b}, {"a.c": a.replace("{K}", "2"), "b.c": b})
        names = {(i["func_display"], i["name"]) for i in r["items"]}
        self.assertIn(("A", "ga"), names)
        self.assertNotIn(("B", "gb"), names)

    def test_duplicate_global_definition_is_warning_not_crash(self):
        files = {"b.c": "int Dup(void) { return 1; }\n", "c.c": "int Dup(void) { return 2; }\n",
                 "d.c": "int g; void F(void) { g = 1; }\n"}
        new = dict(files, **{"d.c": "int g; void F(void) { g = 2; }\n"})
        r = analyze(files, new)
        self.assertTrue(any("Dup" in w for w in r["excluded"]["warnings"]))
        self.assertIn(("F", "g"), {(i["func_display"], i["name"]) for i in r["items"]})


class ReadabilityTests(unittest.TestCase):
    def test_constant_cast_is_not_hw_register(self):
        src = "typedef unsigned int U32;\nU32 g;\nvoid F(void) { g = (U32)155U - (U32){K}U; }\n"
        r = analyze({"a.c": src.replace("{K}", "5")}, {"a.c": src.replace("{K}", "6")})
        self.assertFalse([i for i in r["items"] if i["kind"] == "hw"])
        self.assertIn(("F", "g"), {(i["func_display"], i["name"]) for i in r["items"]})

    def test_pointer_cast_is_still_hw_register(self):
        src = "typedef unsigned int U32;\nvoid F(void) { *(volatile U32 *)0x40001000 = {K}U; }\n"
        r = analyze({"a.c": src.replace("{K}", "1")}, {"a.c": src.replace("{K}", "2")})
        self.assertTrue([i for i in r["items"] if i["kind"] == "hw"])

    def test_initializer_diff_shows_only_changed_element(self):
        src = "typedef struct { int a; int b; } C;\nconst C tbl[3] = {{1, 2}, {3, {K}}, {5, 6}};\nint g;\nvoid F(int i) { g = tbl[i].b; }\n"
        r = analyze({"a.c": src.replace("{K}", "4")}, {"a.c": src.replace("{K}", "9")})
        ch = [c for c in r["changes"] if c["kind"] == "global_changed"]
        self.assertEqual(len(ch), 1)
        self.assertIn("[1].b", ch[0]["old"] + ch[0]["new"])
        self.assertNotIn("{5, 6}", ch[0]["old"] + ch[0]["new"])

    def test_struct_type_diff_lists_added_field(self):
        old = "typedef struct { int a; int b; } S;\nS g;\nint h;\nvoid F(void) { h = g.a; }\n"
        new = "typedef struct { int a; int b; int c; } S;\nS g;\nint h;\nvoid F(void) { h = g.a; }\n"
        r = analyze({"a.c": old}, {"a.c": new})
        details = " ".join(c["detail"] for c in r["changes"])
        self.assertIn("필드 추가", details)
        self.assertIn("c", details)

    def test_statement_moved_under_new_condition(self):
        old = "int g;\nint h;\nvoid F(int x) { if (x) { h = 2; } g = 1; }\n"
        new = "int g;\nint h;\nvoid F(int x) { if (x) { h = 2; g = 1; } }\n"
        r = analyze({"a.c": old}, {"a.c": new})
        self.assertEqual([c["kind"] for c in r["changes"] if c["func"] == "F"], ["moved"])


if __name__ == "__main__":
    unittest.main()
