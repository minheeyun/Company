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

STORY = ROOT / "tests" / "data" / "story"
AUTOCFG = ROOT / "tests" / "data" / "autocfg"


class OutputDirTests(unittest.TestCase):
    """모든 산출물은 변경 후(NEW) 폴더의 바로 상위 폴더에 만들고, 결과 폴더는 소스로 읽지 않는다."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def copy(self, src):
        for v in ("old", "new"):
            shutil.copytree(src / v, self.tmp / v)
        return self.tmp / "old", self.tmp / "new"

    def test_default_and_relative_out_are_next_to_new(self):
        old, new = self.copy(STORY)
        self.assertEqual(fi.main([str(old), str(new), "-q"]), 0)
        res = self.tmp / "CIA_Result_new"  # NEW 의 바로 상위 폴더
        for f in ("report.html", "summary.md", "summary.json", ".cia_output"):
            self.assertTrue((res / f).is_file(), f)
        self.assertTrue((res / "impact" / "impact_report.md").is_file())
        self.assertTrue((res / "security").is_dir())
        self.assertFalse((new / "CIA_Result").exists())
        self.assertEqual(fi.main([str(old), str(new), "-o", "result_26A22", "-q"]), 0)
        self.assertTrue((self.tmp / "result_26A22" / "report.html").is_file())
        absolute = self.tmp / "elsewhere" / "r"
        self.assertEqual(fi.main([str(old), str(new), "-o", str(absolute), "-q"]), 0)
        self.assertTrue((absolute / "report.html").is_file())

    def test_outputs_are_not_read_as_sources(self):
        # 결과 폴더를 분석 대상 폴더 안에 두더라도(절대 경로 -o) 그 안의 파일은 비교·파싱하지 않는다
        old, new = self.copy(AUTOCFG)
        res = new / "inner_result"
        self.assertEqual(fi.main([str(old), str(new), "-o", str(res), "-q", "--auto-config"]), 0)
        self.assertTrue((res / "cia_memclass_auto.h").is_file())
        (res / "stray.c").write_text("int broken(\n")
        self.assertEqual(fi.main([str(old), str(new), "-o", str(res), "-q", "--auto-config"]), 0)
        s = json.loads((res / "summary.json").read_text(encoding="utf-8"))
        ch = s["file_changes"]
        self.assertFalse([f for k in ("added", "modified", "removed") for f in ch[k] if f.startswith("inner_result/")])
        self.assertEqual(s["analysis"]["excluded"]["parse_errors"], {})
        self.assertNotIn("inner_result/stray.c", fi.list_c_files(new, []))

    def test_detect_config_cli_writes_next_to_new(self):
        old, new = self.copy(AUTOCFG)
        self.assertEqual(dc.main([str(old), str(new), "-j", "2"]), 0)
        res = self.tmp / "CIA_Result_new"
        for f in ("cia_config.json", "run_auto.ps1", "cia_memclass_auto.h", ".cia_output"):
            self.assertTrue((res / f).is_file(), f)
        ps1 = (res / "run_auto.ps1").read_text(encoding="utf-8")
        self.assertIn(f'-o "{res}"', ps1)


if __name__ == "__main__":
    unittest.main()
