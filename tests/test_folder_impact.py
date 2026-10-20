import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import folder_impact as fi  # noqa: E402

EXAMPLE = ROOT / "examples" / "impact"


def write(root: Path, files: dict[str, str]):
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(text.encode("utf-8"))


BEFORE = {
    "cfg/Cfg.h": "#define LIMIT 10u\n",
    "inc/Types.h": '#include <stdint.h>\n#include "Cfg.h"\nextern uint8_t g_level;\nvoid Drv_Set(uint8_t v);\n',
    "src/drv/drv.c": '#include "Types.h"\nuint8_t g_reg;\nvoid Drv_Set(uint8_t v) { g_reg = v; }\n',
    "src/app/app.c": ('#include "Types.h"\nuint8_t g_level;\nstatic uint8_t s_log;\n'
                      "void App_Main(uint8_t in) { if (in > LIMIT) { g_level = 1u; } else { g_level = 0u; } }\n"
                      "void App_Out(void) { s_log = g_level; }\n"),
    "src/app/old_only.c": "int g_legacy; void Legacy(void) { g_legacy = 1; }\n",
    "src/crlf.c": "int x;\r\n",
    "src/other.c": "int g_other; void Other(void) { g_other = 2; }\n",
    "test/test_app.c": '#include "Types.h"\nvoid Test(void) { App_Main(3); }\n',
}


class FolderImpactTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.before, self.after, self.out = self.root / "before", self.root / "after", self.root / "out"
        write(self.before, BEFORE)
        after = dict(BEFORE)
        after["cfg/Cfg.h"] = "#define LIMIT 20u\n"
        after["src/crlf.c"] = "int x;\n"
        after["src/app/app.c"] = after["src/app/app.c"].replace("s_log = g_level; }", "s_log = g_level; Drv_Set(g_level); }")
        del after["src/app/old_only.c"]
        after["src/app/new_only.c"] = '#include "Types.h"\nuint8_t g_new; void New_Fn(void) { g_new = g_level; }\n'
        write(self.after, after)

    def tearDown(self):
        self.tmp.cleanup()

    def run_tool(self, *extra) -> dict:
        rc = fi.main([str(self.before), str(self.after), "-o", str(self.out), "-q", "-j", "1", *extra])
        self.assertEqual(rc, 0)
        return json.loads((self.out / "summary.json").read_text(encoding="utf-8"))

    def test_file_changes_ignore_crlf_and_excluded(self):
        s = self.run_tool("--exclude", "test/*")
        ch = s["file_changes"]
        self.assertEqual(ch["modified"], ["cfg/Cfg.h", "src/app/app.c"])
        self.assertEqual(ch["added"], ["src/app/new_only.c"])
        self.assertEqual(ch["removed"], ["src/app/old_only.c"])
        self.assertIn("src/crlf.c", ch["unchanged"])
        self.assertNotIn("test/test_app.c", ch["unchanged"])
        self.assertEqual(s["auto_include"], [".", "cfg", "inc"])

    def test_header_change_traced_with_auto_include(self):
        s = self.run_tool("--exclude", "test/*")
        a = s["analysis"]
        self.assertEqual(a["excluded"]["parse_errors"], {})
        ch = next(c for c in a["changes"] if c["func"] == "App_Main")
        self.assertEqual(ch["interval"]["diff"], [[11, 20]])
        names = {(i["func_display"], i["name"]) for i in a["items"]}
        self.assertIn(("App_Main", "g_level"), names)
        self.assertIn(("App_Out", "s_log"), names)
        self.assertIn(("Drv_Set", "g_reg"), names)
        self.assertNotIn(("Other", "g_other"), names)
        md = (self.out / "summary.md").read_text(encoding="utf-8")
        self.assertIn("| src/app/app.c:5 | `App_Out` | 추가 | `Drv_Set(g_level)` |", md)
        self.assertTrue((self.out / "impact" / "impact_report.md").is_file())
        self.assertTrue((self.out / "deps" / "deps_access_new.csv").is_file())

    def test_dependency_diff(self):
        d = self.run_tool("--exclude", "test/*")["deps_diff"]
        rows = {r["func"]: r for r in d["functions"]}
        self.assertEqual(rows["App_Out"]["calls_added"], ["Drv_Set"])
        self.assertEqual(rows["Legacy"]["status"], "삭제")
        self.assertEqual(rows["New_Fn"]["reads"], ["g_level"])
        self.assertEqual(d["vars_added"], ["g_new"])
        self.assertEqual(d["vars_removed"], ["g_legacy"])

    def test_scope_changed_limits_files(self):
        s = self.run_tool("--exclude", "test/*", "--scope", "changed")
        self.assertEqual(s["analyzed"], {"old": 3, "new": 3})  # app.c, drv.c(헤더 경유) + old_only/new_only
        self.assertTrue(any("--scope changed" in w for w in s["warnings"]))

    def test_no_change(self):
        rc = fi.main([str(self.before), str(self.before), "-o", str(self.out), "-q"])
        self.assertEqual(rc, 0)
        s = json.loads((self.out / "summary.json").read_text(encoding="utf-8"))
        self.assertNotIn("analysis", s)

    def test_bad_folder(self):
        self.assertEqual(fi.main([str(self.root / "nope"), str(self.after), "-o", str(self.out)]), 1)

    def test_includers(self):
        files = fi.list_c_files(self.before, ["test/*"])
        self.assertEqual(fi.includers_of(files, {"cfg/Cfg.h"}), {"inc/Types.h", "src/drv/drv.c", "src/app/app.c"})


@unittest.skipUnless(shutil.which("gcc"), "gcc 필요")
class FolderExploreTests(unittest.TestCase):
    def test_explore_on_example(self):
        with tempfile.TemporaryDirectory() as tmp:
            rc = fi.main([str(EXAMPLE / "old"), str(EXAMPLE / "new"), "-o", tmp, "-q", "-j", "1",
                          "--explore", "--max-cases", "120"])
            self.assertEqual(rc, 0)
            s = json.loads((Path(tmp) / "summary.json").read_text(encoding="utf-8"))
            self.assertGreater(s["explore"]["diff_cases"], 0)
            self.assertIn("g_timeout", s["explore"]["witnesses_input_only"])
            statuses = {i["name"]: i.get("status") for i in s["analysis"]["items"]}
            self.assertEqual(statuses["g_timeout"], "실행으로 차이 확인")
            self.assertTrue((Path(tmp) / "explore" / "explore_report.md").is_file())


if __name__ == "__main__":
    unittest.main()
