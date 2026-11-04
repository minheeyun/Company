import json
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import folder_impact as fi  # noqa: E402

DATA = ROOT / "tests" / "data" / "story"


class StoryTests(unittest.TestCase):
    """report.html 의 변경 이야기: ① 변경 → ② 자동 분석 → ③ 발견 → ④ 추가 검증 (s_DacUVRefFaultStep 예)."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        cls.out = cls.tmp / "out"
        assert fi.main([str(DATA / "old"), str(DATA / "new"), "-o", str(cls.out), "-q"]) == 0
        cls.s = json.loads((cls.out / "summary.json").read_text(encoding="utf-8"))
        cls.st = cls.s["story"]
        cls.c = cls.st["changes"][0]

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_overview(self):
        ov = self.st["overview"]
        self.assertEqual(ov["versions"], {"old": "old", "new": "new"})
        self.assertEqual(ov["files"]["modified"], 1)
        self.assertEqual(ov["analyzed"], {"old": 1, "new": 1})
        pend = {p["item"]: p["status"] for p in ov["pending"]}
        self.assertEqual(pend["Joern·CodeQL"], "스크립트·질의 생성, 실행하지 않음")
        self.assertIn("결과 미첨부", pend["SIL·자동 탐색"])
        self.assertIn("요구사항·변경 의도", pend)
        self.assertEqual(ov["counts"]["findings"], len(self.st["findings"]))
        self.assertEqual(ov["major"][0], "C01")

    def test_global_flag_merged_into_change(self):
        # 전역 변수 추가는 따로 나오지 않고, 그 변수를 쓰는 변경의 '제어 변수' 로 들어간다
        self.assertEqual(len(self.st["changes"]), 1)
        self.assertEqual(self.c["merged_globals"][0]["name"], "u8s_uvchangeflag")

    def test_what_changed(self):
        c = self.c
        self.assertIn("플래그 조건 추가", c["title"])
        self.assertIn("오류 상태 갱신 경로 변경", c["title"])
        text = " ".join(c["sentences"])
        self.assertIn("`u8s_uvchangeflag == 0U` 조건이 추가되었습니다", text)
        self.assertIn("비교식 `u8s_UVStepAdjRetryCnt < 10` 는 그대로이지만", text)
        rows = {r["item"]: r for r in c["compare"]}
        self.assertEqual(rows["제어 변수"]["after"], "`u8s_uvchangeflag` 추가 (초기값 0)")
        self.assertEqual(rows["`u8s_UVStepAdjRetryCnt < 10` 비교"]["after"], "동일 (실행 위치만 이동)")
        entry = next(r for k, r in rows.items() if k.endswith("블록 진입 조건"))
        self.assertEqual(entry["before"], "`uv > 3U`")
        self.assertIn("`u8s_uvchangeflag == 0U` (추가)", entry["after"])
        self.assertTrue(rows["달라지는 동작"]["behavior"])
        self.assertEqual(c["intent"].split(" ")[0], "요구사항")
        self.assertTrue(any(r[0] == "add" and "u8s_uvchangeflag == 0U" in r[4] for r in c["code"]))

    def test_impact_stages_use_shared_variables(self):
        st = self.c["impact"]["core"][0]["stages"]
        self.assertEqual([s["stage"] for s in st], ["변경 발생", "상태 판단", "외부 출력 후보"])
        self.assertEqual([s["func"] for s in st],
                         ["s_DacUVRefFaultStep", "Runnable_TxConfirm_LSS_W_01_100ms", "s_P2CTX_LSS_W_01_100ms"])
        self.assertIn("LSS_UVDTChangeConfirm_W", st[1]["basis"])
        self.assertIn("Rte_Write_LSS_W_01", st[2]["basis"])
        self.assertIn("호출 관계 아님", st[1]["link"])  # 공유 변수 연결을 호출처럼 쓰지 않는다
        self.assertIn("미확인", self.c["impact"]["note"])

    def test_checks(self):
        k = {x["item"]: x for x in self.c["checks"]}
        self.assertIn("식은 동일하지만 감싸는 조건이 변경됨", k["조건·분기 비교"]["confirmed"])
        self.assertEqual(k["기호 실행"]["status"], "미실행")
        self.assertEqual(k["실제 실행 비교 (SIL·자동 탐색)"]["status"], "결과 미첨부")
        self.assertIn("Rte_Write_LSS_W_01", k["영향 경로 추적"]["confirmed"])
        self.assertTrue(all(x["cannot"] for x in self.c["checks"]))

    def test_finding_is_functional_and_keeps_rule(self):
        f = next(x for x in self.st["findings"] if x["id"] == "V1")
        self.assertIn("새 플래그 조건에서", f["title"])
        self.assertNotIn("보안 검사", f["title"])
        self.assertEqual((f["judgment"], f["comparison"]), ("위험 후보", "판정 보류"))
        self.assertIn("위반 반례 없음 (기호 실행 미실행)", f["evidence_level"])
        self.assertIn("실제 재현 결과 없음", f["evidence_level"])
        self.assertEqual(f["sources"][0]["rule"], "실패 횟수·잠금 시간 조건 위치 이동")
        self.assertTrue(f["tests"])

    def test_verification_items(self):
        ts = {t["id"]: t for t in self.st["tests"] if t["change"] == "C01"}
        t1 = ts["T01-1"]
        self.assertIn("0/1", t1["condition"])
        self.assertIn("9/10/11", t1["condition"])
        self.assertIn("기대값 미확정", t1["criteria"])
        self.assertEqual(t1["findings"], ["V1"])
        titles = [t["title"] for t in ts.values()]
        self.assertIn("플래그 전이 후 처리", titles)
        self.assertIn("외부 응답 확인", titles)
        self.assertIn("변경 전후 회귀 비교", titles)
        ext = next(t for t in ts.values() if t["title"] == "외부 응답 확인")
        self.assertIn("LSS_UVDTChangeConfirm_W", ext["observe"])

    def test_moved_counter_check_not_reported_as_deleted(self):
        flags = [f["summary"] for f in self.s["review"]["findings"] if f["item"] == "4-3"]
        self.assertFalse([x for x in flags if "삭제" in x])

    def test_html(self):
        html = (self.out / "report.html").read_text(encoding="utf-8")
        data = json.loads(re.search(r'<script type="application/json" id="data">(.*?)</script>', html, re.S).group(1))
        self.assertEqual(data["story"]["changes"][0]["id"], "C01")
        for t in ("검증 기록", "결과만 고르면 완료가 되지 않습니다", "판정 표시와 비교 상태의 의미"):
            self.assertIn(t, html)


if __name__ == "__main__":
    unittest.main()
