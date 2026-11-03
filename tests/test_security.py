import shutil
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import folder_impact as fi  # noqa: E402
from c_impact import symexec  # noqa: E402
from c_impact.analysis import load_programs, run_analysis  # noqa: E402
from c_impact.frontend import BuildConfig  # noqa: E402
from c_impact.rules import Rules, load_rules  # noqa: E402

DIAG = """
typedef unsigned char uint8_t; typedef unsigned int uint32_t;
void *memcpy(void *d, const void *s, unsigned int n);
int process_diag(const uint8_t *rx, uint32_t len)
{
    uint8_t buffer[8] = {0};
    if (len <= {LIMIT}U)
    {
        memcpy(buffer, rx, len);
        return buffer[0];
    }
    return -1;
}
"""


def analyze(files_old: dict, files_new: dict, **kw) -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        for ver, files in (("old", files_old), ("new", files_new)):
            for rel, text in files.items():
                p = root / ver / rel
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(textwrap.dedent(text), encoding="utf-8")
        cfg = BuildConfig()
        old, new = load_programs(root / "old", root / "new", cfg)
        kw.setdefault("cwe", True)
        return run_analysis(old, new, cfg, **kw)


needs_z3 = unittest.skipUnless(symexec.available(), "z3-solver 가 설치되어 있지 않음")


class DiagExampleTests(unittest.TestCase):
    @needs_z3
    def test_relaxed_length_check_is_new_cwe787_with_counterexample(self):
        r = analyze({"diag.c": DIAG.replace("{LIMIT}", "8")}, {"diag.c": DIAG.replace("{LIMIT}", "64")})
        f = next(x for x in r["risks"] if x["kind"] == "length")
        self.assertEqual((f["level"], f["cwe"], f["status_code"], f["state"]), ("High", "CWE-787", "NEW_REGRESSION", "violated"))
        self.assertTrue(f["taint"]["flow_detected"])
        self.assertEqual(f["taint"]["source"], "DIAG_INPUT")
        self.assertIn("len", f["taint"]["source_label"])
        self.assertEqual(f["symbolic"]["result"], "violated")
        self.assertTrue(9 <= f["symbolic"]["counterexample"]["len"] <= 64)
        self.assertEqual(f["baseline"]["old_state"], "holds")
        self.assertEqual(f["change"]["before"], "if (len <= 8U)")
        self.assertEqual(f["change"]["after"], "if (len <= 64U)")
        self.assertIn("재현 입력", f["verification"])
        self.assertEqual(r["security"]["summary"]["by_status"]["신규 발생"], 1)

    def test_safe_change_reports_nothing(self):
        old = DIAG.replace("{LIMIT}", "4")
        new = DIAG.replace("{LIMIT}", "8")
        r = analyze({"diag.c": old}, {"diag.c": new})
        self.assertEqual([x for x in r["risks"] if x["kind"] == "length"], [])

    @needs_z3
    def test_tightened_check_is_fixed(self):
        r = analyze({"diag.c": DIAG.replace("{LIMIT}", "64")}, {"diag.c": DIAG.replace("{LIMIT}", "8")})
        f = next(x for x in r["risks"] if x["kind"] == "length")
        self.assertEqual(f["status_code"], "FIXED")

    @needs_z3
    def test_existing_overflow_stays_existing(self):
        old = DIAG.replace("{LIMIT}", "64")
        new = old.replace("    if (len <= 64U)", "    len = len & 0xFFU;\n    if (len <= 64U)")
        r = analyze({"diag.c": old}, {"diag.c": new})
        f = next(x for x in r["risks"] if x["kind"] == "length")
        self.assertEqual(f["status_code"], "EXISTING")


class TaintSourceTests(unittest.TestCase):
    SRC = """
    typedef unsigned char U8;
    U8 g_tab[10]; U8 g_out;
    unsigned char Rte_Read_Speed(U8 *v);
    void Task(void) {
        U8 v = 0U;
        (void)Rte_Read_Speed(&v);
        if (v < {LIM}U) { g_out = g_tab[v]; }
    }
    """

    @needs_z3
    def test_out_argument_source_reaches_index(self):
        r = analyze({"a.c": self.SRC.replace("{LIM}", "10")}, {"a.c": self.SRC.replace("{LIM}", "11")})
        f = next(x for x in r["risks"] if x["kind"] == "index")
        self.assertEqual(f["cwe"], "CWE-125")
        self.assertTrue(f["taint"]["flow_detected"])
        self.assertEqual(f["taint"]["source"], "COM_INPUT")
        self.assertEqual(f["symbolic"]["counterexample"].get("Rte_Read_Speed() 반환", 0) is not None, True)
        self.assertEqual(f["status_code"], "NEW_REGRESSION")
        self.assertEqual(f["level"], "High")


class SecurityCheckTests(unittest.TestCase):
    SRC = """
    typedef unsigned char U8;
    U8 g_secLevel; U8 g_unlocked;
    void Diag_Unlock(void) {
        if (g_secLevel == 3U) { g_unlocked = 1U; }
    }
    """

    def test_changed_security_level_check(self):
        new = self.SRC.replace("g_secLevel == 3U", "g_secLevel >= 1U")
        r = analyze({"a.c": self.SRC}, {"a.c": new})
        f = next(x for x in r["risks"] if x["category"] == "보안 조건 변경")
        self.assertEqual(f["cwe"], "CWE-862")
        self.assertEqual(f["status_code"], "PENDING")
        self.assertIn("변경", f["kind_label"])

    def test_removed_security_check_is_new_high(self):
        new = self.SRC.replace("if (g_secLevel == 3U) { g_unlocked = 1U; }", "g_unlocked = 1U;")
        r = analyze({"a.c": self.SRC}, {"a.c": new})
        f = next(x for x in r["risks"] if x["category"] == "보안 조건 변경")
        self.assertEqual((f["cwe"], f["status_code"], f["level"]), ("CWE-862", "NEW_REGRESSION", "High"))


class SymbolicTests(unittest.TestCase):
    @needs_z3
    def test_path_sensitive_counterexample(self):
        src = """
        typedef unsigned char U8;
        U8 arr[10]; U8 g;
        void F(U8 a) {
            if (a > 5U) {
                if (a < {HI}U) { arr[a] = g; }
            }
        }
        """
        r = analyze({"a.c": src.replace("{HI}", "10")}, {"a.c": src.replace("{HI}", "12")})
        f = next(x for x in r["risks"] if x["kind"] == "index")
        self.assertEqual(f["cwe"], "CWE-787")
        self.assertIn(f["symbolic"]["counterexample"]["a"], (10, 11))
        self.assertEqual(f["status_code"], "NEW_REGRESSION")

    @needs_z3
    def test_symbolic_proves_safety_through_assignment(self):
        src = """
        typedef unsigned char U8;
        U8 arr[10]; U8 g; U8 h;
        void F(U8 a) {
            U8 i = a % {M}U;
            arr[i] = g;
        }
        """
        r = analyze({"a.c": src.replace("{M}", "10")}, {"a.c": src.replace("{M}", "10") + "void G(void) { h = 2U; }\n"})
        self.assertEqual([x for x in r["risks"] if x["kind"] == "index"], [])
        r = analyze({"a.c": src.replace("{M}", "10")}, {"a.c": src.replace("{M}", "11")})
        f = next(x for x in r["risks"] if x["kind"] == "index")
        self.assertEqual(f["symbolic"]["result"], "violated")
        self.assertEqual(f["symbolic"]["counterexample"]["a"] % 11, 10)

    def test_without_z3_falls_back(self):
        saved = symexec.z3
        try:
            symexec.z3 = None
            r = analyze({"diag.c": DIAG.replace("{LIMIT}", "8")}, {"diag.c": DIAG.replace("{LIMIT}", "64")})
        finally:
            symexec.z3 = saved
        f = next(x for x in r["risks"] if x["kind"] == "length")
        self.assertEqual(f["state"], "violated")  # 바뀐 조건 구간 증명
        self.assertIsNone(f["symbolic"])
        self.assertIn("z3", r["security"]["engine"]["symbolic"])


class RulesTests(unittest.TestCase):
    def test_yaml_rules_load(self):
        import importlib.util
        if importlib.util.find_spec("yaml") is None:
            self.skipTest("PyYAML 없음")
        r = load_rules(str(ROOT / "config" / "security_rules.yaml"))
        self.assertTrue(r.param_sources("process_diag"))
        self.assertEqual(r.length_rule("memcpy")[1:], (3, 1))
        self.assertTrue(r.out_arg_sources("Rte_Read_Speed"))

    def test_custom_source_rule(self):
        src = """
        typedef unsigned char U8;
        U8 arr[4]; U8 g;
        void MyHandler(U8 idx) { if (idx < {N}U) { g = arr[idx]; } }
        """
        rules = Rules({"sources": [{"name": "MY_IN", "functions": ["MyHandler"]}],
                       "sinks": [{"name": "A", "type": "array_index"}]})
        r = analyze({"a.c": src.replace("{N}", "4")}, {"a.c": src.replace("{N}", "8")}, rules=rules)
        f = next(x for x in r["risks"] if x["kind"] == "index")
        self.assertEqual(f["taint"]["source"], "MY_IN")


class FolderOutputsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        for ver, lim in (("before", "8"), ("after", "64")):
            (self.tmp / ver).mkdir()
            (self.tmp / ver / "diag.c").write_text(DIAG.replace("{LIMIT}", lim), encoding="utf-8")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_security_artifacts(self):
        out = self.tmp / "out"
        rc = fi.main([str(self.tmp / "before"), str(self.tmp / "after"), "-o", str(out), "-q"])
        self.assertEqual(rc, 0)
        sd = out / "security"
        for p in ("vulnerability_report.json", "cpg.json", "joern/taint.sc", "codeql/ChangeTaint.ql", "codeql/qlpack.yml"):
            self.assertTrue((sd / p).is_file(), p)
        import json
        rep = json.loads((sd / "vulnerability_report.json").read_text(encoding="utf-8"))
        self.assertNotIn("cwe", rep["findings"][0])  # 기본은 CWE 분류를 표시하지 않음
        self.assertEqual(rep["findings"][0]["kind"], "length")
        cpg = json.loads((sd / "cpg.json").read_text(encoding="utf-8"))
        types = {e[2] for e in cpg["edges"]}
        self.assertTrue({"CFG", "CDG", "REACHING_DEF"} <= types)
        if symexec.available():
            self.assertTrue((sd / "klee" / "V1" / "verify.c").is_file())
            self.assertIn("klee_assert", (sd / "klee" / "V1" / "cia_diag_inst.c").read_text(encoding="utf-8"))
        md = (out / "summary.md").read_text(encoding="utf-8")
        self.assertNotIn("CWE", md)
        self.assertIn("신규 발생", md)
        html = (out / "report.html").read_text(encoding="utf-8")
        self.assertNotIn("CWE-787", html)

    def test_cwe_option_shows_classification(self):
        out = self.tmp / "out2"
        fi.main([str(self.tmp / "before"), str(self.tmp / "after"), "-o", str(out), "-q", "--cwe"])
        self.assertIn("CWE-787", (out / "summary.md").read_text(encoding="utf-8"))
        self.assertIn("CWE-787", (out / "report.html").read_text(encoding="utf-8"))


class ChangeComparisonTests(unittest.TestCase):
    """Source–Sink 경로 변화, 바뀐 조건의 영향, 버퍼·입력 범위 변화, 변경 전후 안전 속성 검증."""
    BASE = DIAG.replace("{LIMIT}", "8")

    def one(self, old, new):
        r = analyze({"d.c": old}, {"d.c": new})
        return next(x for x in r["risks"] if x["kind"] == "length")

    @needs_z3
    def test_relaxed_condition_range_and_safety_change(self):
        f = self.one(self.BASE, DIAG.replace("{LIMIT}", "64"))
        self.assertEqual(f["how"], "실행 조건 변경")
        self.assertEqual(f["taint"]["change"], "기존 경로 유지")
        self.assertEqual(f["bounds"]["change"], "버퍼 8바이트 · 허용 len ≤ 8 → len ≤ 64")
        self.assertEqual(f["baseline"]["old_state"], "holds")
        self.assertEqual(f["symbolic"]["result"], "violated")

    @needs_z3
    def test_buffer_shrink_without_initializer(self):
        old = self.BASE.replace("uint8_t buffer[8] = {0};", "uint8_t buffer[8];")
        f = self.one(old, old.replace("buffer[8];", "buffer[4];"))
        self.assertEqual(f["status_code"], "NEW_REGRESSION")
        self.assertEqual(f["how"], "자료형·크기 변경")
        self.assertIn("버퍼 8바이트 → 버퍼 4바이트", f["bounds"]["change"])

    @needs_z3
    def test_new_source_to_sink_path(self):
        new = DIAG.replace("{LIMIT}", "64")
        old = new.replace("memcpy(buffer, rx, len);", "memcpy(buffer, rx, 4U);")
        f = self.one(old, new)
        self.assertEqual(f["taint"]["change"], "신규 경로 (변경 전에는 외부 입력이 닿지 않음)")
        self.assertEqual(f["baseline"]["old_state"], "holds")
        self.assertIn("상수 4 → len ≤ 64", f["bounds"]["change"])
        self.assertEqual(f["status_code"], "NEW_REGRESSION")


class ReviewClassificationTests(unittest.TestCase):
    """① 직접·전파 구분 ② 기존 위험 분리 ③ 의존성 변화와 구분 ④ 검증 수준."""
    SRC = """
    typedef unsigned char uint8_t; typedef unsigned int uint32_t;
    void *memcpy(void *d, const void *s, unsigned int n);
    void Log_Write(uint8_t v);
    uint8_t g_idx; uint8_t g_tab[10]; uint32_t g_out;
    int process_diag(const uint8_t *rx, uint32_t len)
    {
        uint8_t buffer[8] = {0};
        if (len <= {LIMIT}U)
        {
            memcpy(buffer, rx, len);
            g_idx = buffer[0];{EXTRA}
            return buffer[0];
        }
        return -1;
    }
    void Other(void) { g_out = g_tab[g_idx]; }
    """

    def run_case(self, extra=""):
        old = self.SRC.replace("{LIMIT}", "8").replace("{EXTRA}", "")
        new = self.SRC.replace("{LIMIT}", "64").replace("{EXTRA}", extra)
        return analyze({"d.c": old}, {"d.c": new})

    @needs_z3
    def test_direct_and_propagated_and_existing(self):
        r = self.run_case()
        by = {(x["func"], x["kind"]): x for x in r["risks"]}
        d = by[("process_diag", "length")]
        self.assertEqual((d["relation"], d["status_code"], d["verification_level"], d["kind_of_result"]),
                         ("직접", "NEW_REGRESSION", "기호 실행 반례 발견", "보안 위험"))
        p = by[("Other", "index")]
        self.assertEqual(p["relation"], "전파")
        self.assertEqual(p["status_code"], "EXISTING")  # 변경 전에도 g_idx 는 0..255 → 기존 위험
        self.assertLess(r["risks"].index(d), r["risks"].index(p))

    def test_call_change_alone_is_not_a_risk(self):
        r = self.run_case("\n            Log_Write(buffer[0]);")
        self.assertFalse([x for x in r["risks"] if x["kind"] in ("fptr",) or "Log_Write" in x["expr"]])

    def test_verification_levels(self):
        saved = symexec.z3
        try:
            symexec.z3 = None
            r = self.run_case()
        finally:
            symexec.z3 = saved
        d = next(x for x in r["risks"] if x["kind"] == "length")
        self.assertEqual(d["verification_level"], "정적 규칙 위반")
        src = """
        typedef unsigned char U8;
        U8 g_tab[10]; U8 g_out;
        unsigned char Rte_Read_Idx(U8 *v);
        void Task(void) { U8 v = 0U; (void)Rte_Read_Idx(&v); g_out = g_tab[v % {M}U]; }
        """
        r = analyze({"a.c": src.replace("{M}", "10")}, {"a.c": src.replace("{M}", "12")})
        f = next(x for x in r["risks"] if x["kind"] == "index")
        self.assertTrue(f["verification_flags"]["taint"])
        self.assertIn(f["verification_level"], ("기호 실행 반례 발견", "정적 규칙 위반") if symexec.available() else ("Taint 경로 발견",))


if __name__ == "__main__":
    unittest.main()
