import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import folder_impact as fi  # noqa: E402
from c_impact import symexec  # noqa: E402
from c_impact.review import expr_diff, scan_directives  # noqa: E402
from pycparser import CParser  # noqa: E402

DATA = ROOT / "tests" / "data" / "review"


def expr(text: str):
    return CParser().parse(f"int f(void) {{ return {text}; }}").ext[0].body.block_items[0].expr


class ExprDiffTests(unittest.TestCase):
    def test_operator_constant_and_call(self):
        self.assertEqual(expr_diff(expr("a > 10"), expr("a >= 10")), ["연산자 > → >="])
        self.assertEqual(expr_diff(expr("a > 10U"), expr("a > 20U")), ["상수 10U → 20U"])
        self.assertIn("호출 대상 f() → g()", expr_diff(expr("f(x)"), expr("g(x)")))
        self.assertIn("인자 수 1 → 2", expr_diff(expr("f(x)"), expr("f(x, y)")))

    def test_evaluation_order_and_precedence(self):
        d = expr_diff(expr("a && f()"), expr("f() && a"))
        self.assertTrue(d and "평가 순서" in d[0] and "호출 포함" in d[0])
        d = expr_diff(expr("a + b * c"), expr("(a + b) * c"))
        self.assertTrue(any("연산 순서" in x for x in d))

    def test_cast(self):
        self.assertEqual(expr_diff(expr("x"), expr("(unsigned char) x")), ["형변환 (unsigned char) 추가"])


class DirectiveTests(unittest.TestCase):
    def test_scan(self):
        d = scan_directives("#define A 1\n#define F(x) ((x)+1)\n#if (A == 1)\nint x;\n#else\nint y;\n#endif\n#include \"h.h\"\n")
        self.assertEqual(d["defines"]["A"], "1")
        self.assertIn("F(x)", d["defines"])
        self.assertIn("#if (A == 1)", d["conds"])
        self.assertEqual(d["includes"], ['"h.h"'])
        self.assertTrue(any(s == 3 for s, e, lab in d["regions"]))


class ReviewChecklistTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        cls.out = cls.tmp / "out"
        rc = fi.main([str(DATA / "old"), str(DATA / "new"), "-o", str(cls.out), "-q"])
        assert rc == 0
        cls.summary = json.loads((cls.out / "summary.json").read_text(encoding="utf-8"))
        cls.rv = cls.summary["review"]
        cls.items = {it["id"]: it for it in cls.rv["items"]}

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def find(self, item, text):
        return [f for f in self.items[item]["findings"] if text in f["summary"]]

    def test_all_items_checked_with_notes(self):
        ids = [it["id"] for it in self.rv["items"]]
        self.assertEqual(len(ids), 25)
        for it in self.rv["items"]:
            self.assertTrue(it["checked"])
            self.assertTrue(it["notes"], it["id"])
            self.assertIn(it["overall"], ["위반 경로 확인", "위험 후보 탐지", "검증자 확인 필요", "분석 불충분", "변경 사실 확인", "해당 변경 없음"])

    def test_change_and_semantic_difference(self):
        self.assertTrue(self.find("1-2", "조건문·상수 수정"))
        self.assertTrue(self.find("1-3", "상수 50U → 80U"))
        self.assertTrue(self.find("1-3", "인자 수 2 → 3"))

    def test_reorder_is_move_not_delete(self):
        self.assertFalse(self.find("7-2", "삭제"))
        self.assertTrue(self.find("3-1", "실행 순서 이동"))
        self.assertTrue(self.find("7-1", "상태 갱신 순서"))

    def test_state_error_counter_timer(self):
        self.assertEqual(self.find("3-3", "도달 불가능 상태 후보")[0]["class"], "위험 후보 탐지")
        self.assertEqual(self.find("4-2", "Dem_SetEventStatus")[0]["class"], "위험 후보 탐지")
        self.assertTrue(self.find("4-3", "s_retryCnt >= 5U"))
        self.assertTrue(self.find("7-2", "s_timer_tm > 80U"))

    def test_parameters_interfaces_and_preprocessor(self):
        p = self.find("5-3", "g_limit")[0]
        self.assertIn("영향 분기 2곳", p["summary"])
        self.assertTrue(self.find("6-2", "Check(): 인자 수 2 → 3"))
        self.assertTrue(self.find("6-2", "ST_WAIT"))
        self.assertTrue(self.find("8-1", "RETRY_MAX 3U → 5U"))
        self.assertTrue(self.find("8-2", "APP_DIAG_ENABLE"))
        self.assertTrue(self.find("8-4", "조건부 컴파일 블록 안"))
        self.assertTrue([f for f in self.items["8-4"]["findings"] if f["class"] == "분석 불충분"])

    def test_concurrency_uses_isr_model(self):
        self.assertFalse([f for f in self.items["7-3"]["findings"] if f["class"] == "분석 불충분"])

    @unittest.skipUnless(symexec.available(), "z3 없음")
    def test_else_if_chain_only(self):
        # 서로 배타적인 else-if 사슬(s_state 값)은 동시 성립 후보가 아니고, 연속된 별개의 if 는 사슬로 보지 않는다
        self.assertEqual(self.items["4-4"]["findings"], [])

    def test_reports_three_part_structure(self):
        md = (self.out / "summary.md").read_text(encoding="utf-8")
        for t in ("## 2. 파일별 변경 검토", "**① 무엇이 변경되었는가**", "**② 스크립트가 무엇을 확인했는가**",
                  "**③ 어떤 문제가 발견되었는가**", "**④ 검증자가 추가로 확인할 사항**", "## 3. 검토 항목 체크리스트", "| 8-4 |"):
            self.assertIn(t, md)
        html = (self.out / "report.html").read_text(encoding="utf-8")
        for t in ("📁 파일별 변경 검토", "⚠️ 검토 필요 항목", "🔗 변경 영향 및 의존성", "🔍 분석 근거 및 범위",
                  "① 무엇이 변경되었는가", "④ 검증자가 추가로 확인할 사항", "부분 분석", "CSV 내보내기"):
            self.assertIn(t, html)

    def test_change_units(self):
        un = self.summary["units"]
        units = un["units"]
        err = [u for u in units if u["func"] == "App_Main" and "Dem_SetEventStatus() 호출 삭제" in u["summary"]]
        self.assertEqual(len(err), 1)
        u = err[0]
        self.assertTrue(u["summary"].startswith("오류 처리 로직 변경"))
        self.assertIn("판정 기준 변경", u["summary"])
        self.assertEqual(u["level"], "orange")
        items = [q["item"] for q in u["checklist"]]
        self.assertEqual(len(items), len(set(items)))  # 항목당 하나
        self.assertTrue(any(i["title"].startswith("오류 처리·복구 호출 삭제") for i in u["issues"]))
        # 별개의 제어 블록은 다른 변경점으로 나뉜다
        self.assertGreaterEqual(len([x for x in units if x["func"] == "App_Main"]), 3)
        self.assertFalse([c for x in units for c in x["changes"] if "인자3" in c["phrase"]])
        self.assertEqual(un["files"][0]["file"], "app.c")
        fn = next(f for f in un["functions"] if f["func"] == "App_Main")
        self.assertIn(["Dem_SetEventStatus", ""], fn["call_pairs"])


if __name__ == "__main__":
    unittest.main()
