import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import detect_config as dc  # noqa: E402
import folder_impact as fi  # noqa: E402

DATA = ROOT / "tests" / "data" / "autocfg"


def by_item(r, item):
    return [d for d in r["decisions"] if d["item"] == item]


class DetectConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_build_file_evidence(self):
        r = dc.detect(DATA / "old", DATA / "new", out_dir=self.tmp, quiet=True, jobs=2)
        self.assertEqual(r["compiler"], "GHS")
        self.assertEqual(by_item(r, "컴파일러")[0]["confidence"], "높음")
        self.assertIn("__ghs__", r["defines"])
        self.assertIn("CPU_BOARD_REVA", r["defines"])
        board = [d for d in by_item(r, "매크로") if "CPU_BOARD_REVA" in d["value"]][0]
        self.assertEqual(board["confidence"], "높음")
        self.assertIn("CPU_BOARD_REVC", board["alternatives"])
        self.assertEqual(r["include_dirs"][0], "Bsw/Compiler/GHS")
        self.assertIn("*SAMPLECODE*", r["exclude"])
        self.assertEqual(r["sample_failed"], 0)
        self.assertIn("APP_VAR", Path(r["memclass_header"]).read_text(encoding="utf-8"))

    def test_without_build_files_marks_low_confidence(self):
        for v in ("old", "new"):
            shutil.copytree(DATA / v, self.tmp / v)
            (self.tmp / v / "Build" / "app.gpj").unlink()
        r = dc.detect(self.tmp / "old", self.tmp / "new", out_dir=self.tmp, quiet=True, jobs=2)
        self.assertEqual(by_item(r, "컴파일러")[0]["confidence"], "낮음")
        self.assertIn("TASKING", by_item(r, "컴파일러")[0]["alternatives"])
        board = [d for d in by_item(r, "매크로") if "CPU_BOARD" in d["value"]][0]
        self.assertEqual(board["confidence"], "낮음")
        self.assertEqual(r["sample_failed"], 0)

    def test_error_chain_candidates(self):
        p = self.tmp / "b.h"
        p.write_text("#if defined(A_X)\n#define Q 1\n#elif defined(B_Y) && defined(C)\n#define Q 2\n#else\n#error \"no\"\n#endif\n")
        chain = dc.error_chain(p, 6)
        self.assertEqual(dc.chain_candidates(chain), ["A_X", "B_Y", "C"])

    def test_folder_impact_auto_config(self):
        out = self.tmp / "out"
        rc = fi.main([str(DATA / "old"), str(DATA / "new"), "-o", str(out), "-q", "--auto-config"])
        self.assertEqual(rc, 0)
        s = json.loads((out / "summary.json").read_text(encoding="utf-8"))
        self.assertEqual(s["analysis"]["excluded"]["parse_errors"], {})
        self.assertTrue(s["analysis"]["changes"])
        self.assertTrue(s["auto_config"]["decisions"])
        self.assertIn("자동 탐지한 빌드 설정", (out / "summary.md").read_text(encoding="utf-8"))
        self.assertIn("자동 탐지한 빌드 설정", (out / "report.html").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
