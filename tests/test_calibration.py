import json
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import calibrate  # noqa: E402
import folder_impact as fi  # noqa: E402
from c_impact import names, project  # noqa: E402
from c_impact.review import DEFAULT_PATTERNS  # noqa: E402

STORY = ROOT / "tests" / "data" / "story"


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        for v in ("old", "new"):
            shutil.copytree(STORY / v, self.tmp / "LSS" / v)
        self.old, self.new = self.tmp / "LSS" / "old", self.tmp / "LSS" / "new"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_fi(self, *extra):
        self.assertEqual(fi.main([str(self.old), str(self.new), "-q", *extra]), 0)
        res = self.tmp / "LSS" / "CIA_Result_new"
        return res, json.loads((res / "summary.json").read_text(encoding="utf-8"))


class ProjectConfigTests(Base):
    def test_no_config_writes_template_run_log_and_timing(self):
        res, s = self.run_fi()
        self.assertTrue((res / "cia_project.suggested.yaml").is_file())
        log = (res / "run_log.txt").read_text(encoding="utf-8")
        for t in ("[단계별 시간]", "전처리·파싱", "[파싱 실패] 0개", "[결과] 변경 1"):
            self.assertIn(t, log)
        self.assertTrue(s["timing"])
        self.assertEqual(s["story"]["overview"]["sample"], ["C01"])
        tpl = project.load(res / "cia_project.suggested.yaml")  # 만든 시작 파일이 그대로 읽혀야 한다
        self.assertEqual(tpl["priority_rules"][0]["priority"], "높음")

    def test_config_next_to_new_is_applied(self):
        (self.tmp / "LSS" / "cia_project.yaml").write_text(project.to_yaml({
            "call_verbs": {"Adjust": "기준값 보정"},
            "priority_rules": [{"priority": "낮음", "funcs": ["^s_Dac"], "reason": "보정 테스트"}],
            "suppress": {"review_items": ["6-3"], "risk_rules": []},
            "tests": {"questions": False},
        }), encoding="utf-8")
        res, s = self.run_fi()
        self.assertFalse((res / "cia_project.suggested.yaml").exists())
        self.assertTrue(s["project_config"].endswith("cia_project.yaml"))
        c = s["story"]["changes"][0]
        self.assertIn("기준값 보정", c["title"])
        self.assertEqual((c["priority"], c["priority_reason"]), ("낮음", "보정 테스트"))
        self.assertNotIn("F1", c["findings"])  # 6-3 지적에서 온 발견은 빠지고
        self.assertTrue(any(x["id"] == "6-3" for x in c["suppressed"]))  # 근거 상세에 남는다
        self.assertFalse([t for t in s["story"]["tests"] if t["kind"] == "판단"])

    def test_bad_pattern_is_reported(self):
        (self.tmp / "LSS" / "cia_project.yaml").write_text('review_patterns:\n  counter: "(cnt"\n', encoding="utf-8")
        self.assertEqual(fi.main([str(self.old), str(self.new), "-q"]), 1)

    def test_priority_rules(self):
        f = {"judgment": "위험 후보", "comparison": "판정 보류", "external_output": True, "cross_function": True,
             "review_flags": True, "file": "a.c", "func": "f"}
        self.assertEqual(project.priority_of(None, f)[0], "높음")
        self.assertEqual(project.priority_of(None, {**f, "external_output": False})[0], "중간")
        self.assertEqual(project.priority_of([{"priority": "낮음", "files": ["*.c"]}], f)[0], "낮음")


class NameRuleTests(unittest.TestCase):
    def test_project_abbreviations(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            (tmp / "a.c").write_text("static int u8s_UVFltSta;\nstatic int u16g_WaitDly;\nstatic int u8s_DacFltrVal;\n"
                                     "void f(void){ u8s_UVFltSta = 1; u16g_WaitDly++; u8s_HeatFltCnt = 0; u8s_DacFltrVal = 2; }\n")
            r = names.suggest({"a.c": tmp / "a.c"}, DEFAULT_PATTERNS, min_count=1)
            self.assertIn("error_state", r["patterns"])  # Flt (기본 패턴에 없음)
            rx = re.compile(r["patterns"]["error_state"], re.I)
            self.assertTrue(rx.search("u8s_UVFltSta"))
            self.assertFalse(rx.search("u8s_DacFltrVal"))  # Fltr(필터)는 아님
            self.assertIn("timer", r["patterns"])  # Dly
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_merge_add_and_replace(self):
        m = project.merge_patterns({"counter": "cnt"}, None, {"review_patterns": {"counter": "ctr"}})
        self.assertTrue(re.search(m["counter"], "x_ctr") and re.search(m["counter"], "x_cnt"))
        m = project.merge_patterns({"counter": "cnt"}, None, {"pattern_mode": "replace", "review_patterns": {"counter": "ctr"}})
        self.assertIsNone(re.search(m["counter"], "x_cnt"))


class CalibrateTests(unittest.TestCase):
    def fb(self, changes, missed=()):
        return {"format": "cia-feedback/1", "versions": {"old": "26A10", "new": "26A22"}, "changes": changes, "missed": list(missed),
                "sample": [c["id"] for c in changes], "new_dir": "", "project_config": ""}

    def ch(self, i, prio, reason, verdict_p, findings=(), cross=0, ext=False, judgment="위험 후보"):
        return {"id": f"C{i:02d}", "title": "t", "func": f"f{i}", "file": "a.c", "line": 1, "type_label": "오류 처리 — 조건",
                "priority": prio, "priority_reason": reason, "judgment": judgment, "comparison": "판정 보류",
                "external_output": ext, "cross_function": cross, "review_items": [],
                "feedback": {"title": "정확", "priority": verdict_p, "impact": "정확"}, "findings": list(findings)}

    def test_suppress_and_priority_suggestions(self):
        f = lambda i, v: {"id": f"V{i}", "title": "x", "judgment": "위험 후보", "comparison": "판정 보류",  # noqa: E731
                          "rules": ["입력 검증 삭제"], "review_items": [], "feedback": {"verdict": v, "note": "의도된 변경"}}
        reason = project.DEFAULT_PRIORITY_RULES[2]["reason"]
        changes = [self.ch(1, "높음", reason, "낮춰야 함", [f(1, "오탐 (불필요한 경고)")]),
                   self.ch(2, "높음", reason, "낮춰야 함", [f(2, "오탐 (불필요한 경고)")]),
                   self.ch(3, "낮음", "", "높여야 함", cross=2, judgment="변경 사실 확인"),
                   self.ch(4, "낮음", "", "높여야 함", cross=1, judgment="변경 사실 확인")]
        tmp = Path(tempfile.mkdtemp())
        try:
            p = tmp / "fb.json"
            p.write_text(json.dumps(self.fb(changes, [{"kind": "영향 누락", "change": "C01", "where": "", "desc": "Task 순서"}]),
                                    ensure_ascii=False), encoding="utf-8")
            self.assertEqual(calibrate.main([str(p), "-o", str(tmp)]), 0)
            cfg = project.load(tmp / "cia_project.calibrated.yaml")
            self.assertEqual(cfg["suppress"]["risk_rules"], ["입력 검증 삭제"])
            pr = cfg["priority_rules"]
            self.assertEqual(pr[0]["priority"], "높음")  # '높여야 함' 2건 공통 특징 → 맨 앞 규칙
            self.assertTrue(pr[0]["cross_function"])
            demoted = next(r for r in pr if r.get("reason", "").startswith(reason))
            self.assertEqual(demoted["priority"], "중간")
            md = (tmp / "calibration_report.md").read_text(encoding="utf-8")
            for t in ("일치율 0%", "입력 검증 삭제", "영향 누락 1건", "전체 발견 2건"):
                self.assertIn(t, md)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_rejects_other_json(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            (tmp / "x.json").write_text("{}")
            self.assertEqual(calibrate.main([str(tmp / "x.json")]), 1)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class CrashLogTests(Base):
    def test_crash_is_logged(self):
        from c_impact import review
        orig = review.review
        review.review = lambda *a, **k: (_ for _ in ()).throw(KeyError("테스트용"))
        try:
            with self.assertRaises(KeyError):
                fi.main([str(self.old), str(self.new), "-q"])
            log = (self.tmp / "LSS" / "CIA_Result_new" / "run_log.txt").read_text(encoding="utf-8")
            self.assertIn("[오류]", log)
            self.assertIn("KeyError", log)
            self.assertIn("전처리·파싱", log)  # 어디까지 진행됐는지
        finally:
            review.review = orig


if __name__ == "__main__":
    unittest.main()
