import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import cpp_filter  # noqa: E402
import detect_config as dc  # noqa: E402
import folder_impact as fi  # noqa: E402
from c_impact import parsediag  # noqa: E402
from pycparser import c_parser  # noqa: E402

DATA = ROOT / "tests" / "data" / "autocfg"
DATA2 = ROOT / "tests" / "data" / "autocfg2"


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


    def test_build_file_in_parent_folder(self):
        proj = self.tmp / "proj"
        for v in ("old", "new"):
            shutil.copytree(DATA / v, proj / v)
            (proj / v / "Build" / "app.gpj").unlink()
        (proj / "Build").mkdir()
        shutil.copy(DATA / "new" / "Build" / "app.gpj", proj / "Build" / "app.gpj")
        r = dc.detect(proj / "old", proj / "new", out_dir=self.tmp, quiet=True, jobs=2)
        self.assertEqual(r["compiler"], "GHS")
        self.assertEqual(by_item(r, "컴파일러")[0]["confidence"], "높음")
        self.assertIn("CPU_BOARD_REVA", r["defines"])

    def test_keywords_signatures_and_remaining_causes(self):
        r = dc.detect(DATA2 / "old", DATA2 / "new", out_dir=self.tmp, quiet=True, jobs=2)
        comp = by_item(r, "컴파일러")[0]
        self.assertEqual((r["compiler"], comp["confidence"]), ("GHS", "중간"))  # 시험 결과가 같고 #pragma ghs 단서
        self.assertIn("__interrupt=", r["defines"])
        self.assertIn("__far=", r["defines"])
        self.assertEqual(r["scope_files"], 6)  # 변경된 .c 3개 × 변경 전/후
        rem = r["remaining"]
        self.assertEqual([(g["cause"], g["detail"], g["count"]) for g in rem], [("정의되지 않은 타입", "Ext_CounterType", 2)])
        self.assertEqual(rem[0]["defined_in"], ["Cfg/Ext_Types.h"])

    def test_compiler_without_effect_is_not_a_warning(self):
        for v in ("old", "new"):
            shutil.copytree(DATA2 / v, self.tmp / v)
            (self.tmp / v / "Bsw" / "Compiler.h").write_text(
                "#ifndef COMPILER_H\n#define COMPILER_H\n#define FUNC(rt, mc) rt\n#define VAR(t, mc) t\n#endif\n")
        r = dc.detect(self.tmp / "old", self.tmp / "new", out_dir=self.tmp, quiet=True, jobs=2)
        comp = by_item(r, "컴파일러")[0]
        self.assertEqual(comp["confidence"], "참고")
        self.assertIn("영향이 없음", comp["evidence"])
        self.assertFalse([d for d in r["decisions"] if d["confidence"] == "낮음"])

    def test_search_outside_project(self):
        ext = self.tmp / "ext" / "Rte"
        ext.mkdir(parents=True)
        (ext / "Rte_Ext.h").write_text("typedef unsigned long Ext_CounterType;\n")
        for v in ("old", "new"):
            shutil.copytree(DATA2 / v, self.tmp / v)
            f = self.tmp / v / "AP" / "Function" / "Ap_Ext.c"
            f.write_text('#include "Rte_Ext.h"\n' + f.read_text())
        r = dc.detect(self.tmp / "old", self.tmp / "new", out_dir=self.tmp, quiet=True, jobs=2)
        self.assertTrue(by_item(r, "헤더 없음"))
        r = dc.detect(self.tmp / "old", self.tmp / "new", out_dir=self.tmp, quiet=True, jobs=2, search=[str(self.tmp / "ext")])
        self.assertIn(str(ext), r["include_dirs"])
        self.assertEqual(r["sample_failed"], 0)

    def test_folder_impact_reports_parse_causes(self):
        out = self.tmp / "out"
        rc = fi.main([str(DATA2 / "old"), str(DATA2 / "new"), "-o", str(out), "-q", "--auto-config"])
        self.assertEqual(rc, 0)
        s = json.loads((out / "summary.json").read_text(encoding="utf-8"))
        self.assertEqual(len(s["analysis"]["excluded"]["parse_errors"]), 2)  # 키워드 문제 파일은 분석됨
        self.assertEqual(s["parse_diag"][0]["cause"], "정의되지 않은 타입")
        md = (out / "summary.md").read_text(encoding="utf-8")
        self.assertIn("**파싱 실패 원인**", md)
        self.assertIn("Cfg/Ext_Types.h", md)
        self.assertIn("정의되지 않은 타입", (out / "report.html").read_text(encoding="utf-8"))


class ParseDiagTests(unittest.TestCase):
    def cls(self, src):
        text = '# 1 "/x/f.c"\n' + src + "\n"
        try:
            c_parser.CParser().parse(text, "/x/f.c")
        except Exception as e:  # noqa: BLE001
            return parsediag.classify("파싱 실패: " + str(e), text)
        return None

    def test_keywords(self):
        self.assertEqual(self.cls("__interrupt void isr(void){}")["fix"], "__interrupt=")
        self.assertEqual(self.cls("__interrupt(5) void isr(void){}")["fix"], "__interrupt(...)=")
        self.assertEqual(self.cls("static __no_init int y;")["fix"], "__no_init=")
        c = self.cls("void f(void){ __DI(); __far int *z = 0; }")
        self.assertEqual(c["detail"], "__far")  # 문장으로 쓴 __DI() 는 고르지 않음

    def test_undefined_type_and_errors(self):
        c = self.cls("void f(void){ uint16 z = 1; }")
        self.assertEqual((c["cause"], c["detail"]), ("정의되지 않은 타입", "uint16"))
        self.assertEqual(parsediag.classify("a.c:3:10: fatal error: Rte_X.h: No such file or directory")["detail"], "Rte_X.h")
        self.assertEqual(parsediag.classify('/p/b.h:6:2: error: #error "Undefined Revision"')["cause"], "#error")

    def test_iar_at_placement(self):
        self.assertEqual(cpp_filter.clean('int x @ 0x1000;\nchar *s = "a@b";\nint y @ ".noinit" = 1;\n'),
                         'int x ;\nchar *s = "a@b";\nint y  = 1;\n')


if __name__ == "__main__":
    unittest.main()
