import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import auto_include as ai  # noqa: E402

FILES = {
    "ext/Base/Platform_Types.h": "typedef unsigned char uint8;\n",
    "ext/MCAL/TC3xx/Mcal/Std_Types.h": '#include "Platform_Types.h"\n#include "Compiler.h"\ntypedef uint8 Std_ReturnType;\n',
    "ext/MCAL/TC3xx/Port/inc/Port.h": '#include "Std_Types.h"\nvoid Port_Init(void);\n',
    "ext/MCAL/TC2xx/Port/inc/Port.h": '#include "Std_Types.h"\nvoid Port_Init(void);\n',
    "ext/MCAL/test_stub/Std_Types.h": "typedef int broken_stub;\n",
    "ext/Compiler/include/Compiler.h": "#define FUNC(r, m) r\n",
    "ext/MCAL/TC3xx/Dio/inc/Mcal/Dio.h": "void Dio_Write(uint8 v);\n",
}
APP = ('#include "Port.h"\n#include "Mcal/Dio.h"\n#include "App.h"\nuint8 g_x;\n'
       "void App(uint8 a) { if (a > LIMIT) { g_x = 1u; Dio_Write(g_x); } }\n")


@unittest.skipUnless(shutil.which("gcc"), "gcc 필요")
class AutoIncludeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.b = Path(self.tmp.name)
        files = dict(FILES)
        for v, limit in (("old", 10), ("new", 20)):
            files[f"proj/{v}/inc/App.h"] = f"#define LIMIT {limit}u\n"
            files[f"proj/{v}/src/app.c"] = APP
            files[f"proj/{v}/src/vendor.c"] = '#include "Missing_Vendor.h"\nint z;\n'
            files[f"proj/{v}/src/cc.c"] = '#ifndef __TASKING__\n#error "Compiler not supported"\n#endif\nint q;\n'
        for rel, text in files.items():
            p = self.b / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text, encoding="utf-8")
        self.out = self.b / "result"

    def tearDown(self):
        self.tmp.cleanup()

    def run_tool(self, *extra) -> tuple[int, dict]:
        rc = ai.main([str(self.b / "proj/old"), str(self.b / "proj/new"), "--search", str(self.b / "ext"),
                      "-o", str(self.out), "-j", "2", "-q", *extra])
        data = json.loads((self.out / "include_search" / "auto_include.json").read_text(encoding="utf-8"))
        return rc, data

    def test_finds_nested_and_subpath_headers_consistently(self):
        rc, r = self.run_tool("--no-run")
        self.assertEqual(rc, 0)
        rel = [Path(d).relative_to(self.b / "ext").as_posix() for d in r["include_dirs"]]
        self.assertEqual(sorted(rel), ["Base", "Compiler/include", "MCAL/TC3xx/Dio/inc", "MCAL/TC3xx/Mcal",
                                       "MCAL/TC3xx/Port/inc"])  # TC2xx·test_stub 이 아니라 같은 패키지로 통일
        self.assertEqual(r["found"]["Mcal/Dio.h"]["dir"], str(self.b / "ext/MCAL/TC3xx/Dio/inc"))
        self.assertEqual(list(r["unresolved"]), ["Missing_Vendor.h"])
        self.assertEqual(len(r["other_errors"]), 1)
        self.assertEqual(r["ok"], 2)
        self.assertTrue((self.out / "include_search" / "run_folder_impact.ps1").is_file())

    def test_prefer_overrides(self):
        _, r = self.run_tool("--no-run", "--prefer", "*TC2xx*")
        self.assertEqual(r["found"]["Port.h"]["dir"], str(self.b / "ext/MCAL/TC2xx/Port/inc"))

    def test_runs_folder_impact_with_found_dirs(self):
        rc, r = self.run_tool("-D", "__TASKING__")
        self.assertEqual(rc, 0)
        self.assertEqual(r["other_errors"], {})  # #error 는 -D 로 해결
        s = json.loads((self.out / "summary.json").read_text(encoding="utf-8"))
        a = s["analysis"]
        self.assertEqual(sorted(a["excluded"]["parse_errors"]), ["(변경 전) src/vendor.c", "(변경 후) src/vendor.c"])
        ch = next(c for c in a["changes"] if c["func"] == "App")
        self.assertEqual(ch["interval"]["diff"], [[11, 20]])
        self.assertIn(("App", "Dio_Write() 호출 (외부)"), {(i["func_display"], i["name"]) for i in a["items"]})


class IndexTests(unittest.TestCase):
    def test_subpath_and_parent_includes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "a/inc/Mcal").mkdir(parents=True)
            (root / "a/inc/Mcal/Dio.h").write_text("")
            (root / "b/Dio.h").parent.mkdir(parents=True)
            (root / "b/Dio.h").write_text("")
            idx = ai.HeaderIndex([root], skip=[])
            self.assertEqual(idx.candidates("Mcal/Dio.h"), [str(root / "a/inc")])
            self.assertEqual(sorted(idx.candidates("Dio.h")), sorted([str(root / "a/inc/Mcal"), str(root / "b")]))
            self.assertEqual(idx.candidates("../Mcal/Dio.h"), [str(root / "a/inc")])

    def test_missing_header_regex_ignores_missing_source(self):
        self.assertIsNone(ai.MISSING_RE.search("cc1: fatal error: x.c: No such file or directory"))
        m = ai.MISSING_RE.search(r"S:\a\b.c:3:10: fatal error: Mcal/Port.h: No such file or directory")
        self.assertEqual(m.group(1), "Mcal/Port.h")


if __name__ == "__main__":
    unittest.main()
