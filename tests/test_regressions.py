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


if __name__ == "__main__":
    unittest.main()
