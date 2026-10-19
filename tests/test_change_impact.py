import json
import platform
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import change_impact as cli  # noqa: E402
from c_impact import diff as d  # noqa: E402
from c_impact.analysis import load_programs, run_analysis  # noqa: E402
from c_impact.frontend import BuildConfig, load_compile_commands  # noqa: E402
from c_impact.sil import expand_cases, run_sil  # noqa: E402
from pycparser import c_parser  # noqa: E402

EXAMPLE = ROOT / "examples" / "impact"
HAS_GCC = shutil.which("gcc") is not None


def analyze(old: Path, new: Path, **kw) -> dict:
    cfg = BuildConfig()
    o, n = load_programs(old, new, cfg)
    return run_analysis(o, n, cfg, **kw)


def items(r: dict) -> dict[tuple[str, str], dict]:
    return {(it["func_display"], it["name"]): it for it in r["items"]}


class Snippet(unittest.TestCase):
    """변경 전/후 C 코드를 임시 폴더에 써서 분석한다."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def run_pair(self, old_src: str, new_src: str, name="m.c", **kw) -> dict:
        for ver, src in (("old", old_src), ("new", new_src)):
            p = self.root / ver
            p.mkdir(exist_ok=True)
            (p / name).write_text(textwrap.dedent(src), encoding="utf-8")
        return analyze(self.root / "old", self.root / "new", **kw)


class ExampleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.r = analyze(EXAMPLE / "old", EXAMPLE / "new")
        cls.items = items(cls.r)

    def test_header_macro_change_is_detected_after_preprocessing(self):
        ch = [c for c in self.r["changes"] if c["func"] == "UpdateTimeout"]
        self.assertEqual(len(ch), 1)
        self.assertIn("100U", ch[0]["old"])
        self.assertIn("200U", ch[0]["new"])
        self.assertIn("TIMEOUT_TICKS", ch[0]["src"])
        self.assertEqual(ch[0]["interval"]["diff"], [[100, 199]])
        self.assertEqual(ch[0]["interval"]["domain"], [0, 65535])

    def test_impact_through_shared_variable_and_condition(self):
        self.assertEqual(self.items[("UpdateTimeout", "g_timeout")]["how_kind"], "직접 변경")
        latched = self.items[("UpdateDiagnostic", "g_faultLatched")]
        self.assertEqual(latched["how_kind"], "실행 여부")
        self.assertIn("`if (g_timeout != 0U)` 참", latched["conditions"])
        self.assertTrue(any("100 ≤ tick ≤ 199" in c for c in latched["conditions"]))
        ret = self.items[("ReadStatus", "ReadStatus() 반환값")]
        self.assertEqual(ret["how_kind"], "값")
        funcs = [s["func"] for s in ret["path"] if s["kind"] != "actual_out"]
        self.assertEqual(funcs, ["UpdateTimeout", "UpdateDiagnostic", "UpdateDiagnostic", "ReadStatus"])

    def test_struct_field_through_pointer_is_field_sensitive(self):
        self.assertIn(("Debounce", "g_sensor.counter"), self.items)
        self.assertIn(("Sensor_MainFunction", "g_sensor.valid"), self.items)
        names = {k[1] for k in self.items}
        self.assertNotIn("Sensor_GetRaw() 반환값", names)  # g_sensor.raw 만 읽음
        self.assertNotIn("g_unrelated", names)

    def test_external_call_and_callback_entry(self):
        self.assertIn(("Sensor_Report", "Dem_SetEventStatus() 호출 (외부)"), self.items)
        isr = next(e for e in self.r["entries"] if e["entry"] == "Os_Tick_ISR")
        self.assertTrue(isr["isr"])
        self.assertIn("UpdateDiagnostic", isr["affected_functions"])  # 함수 포인터 테이블로 호출

    def test_sil_template_orders_producers_first(self):
        t = self.r["sil_template"]
        loop = t["steps"][0]["steps"]
        self.assertEqual([s["call"] for s in loop], ["Sensor_MainFunction", "UpdateTimeout", "Os_Tick_ISR"])
        self.assertEqual(t["params"]["tick"], [99, 100, 149, 199, 200])
        self.assertEqual(t["steps"][-1], {"call": "ReadStatus", "args": [], "record": True})


class AnalysisTests(Snippet):
    def test_context_sensitive_return(self):
        src = """
        unsigned g_a, g_b;
        static unsigned Sat(unsigned v) { return v > 10u ? 10u : v; }
        void A(unsigned x) { g_a = Sat(x + {K}u); }
        void B(unsigned y) { g_b = Sat(y); }
        """
        r = self.run_pair(src.replace("{K}", "1"), src.replace("{K}", "2"))
        names = {k[1] for k in items(r)}
        self.assertIn("g_a", names)
        self.assertNotIn("g_b", names)

    def test_context_insensitive_option_overapproximates(self):
        src = """
        unsigned g_a, g_b;
        static unsigned Sat(unsigned v) { return v > 10u ? 10u : v; }
        void A(unsigned x) { g_a = Sat(x + {K}u); }
        void B(unsigned y) { g_b = Sat(y); }
        """
        r = self.run_pair(src.replace("{K}", "1"), src.replace("{K}", "2"), k=0)
        self.assertIn("g_b", {k[1] for k in items(r)})

    def test_overwritten_local_is_not_impacted(self):
        r = self.run_pair(
            "int g; void f(int a) { int x = a + 1; x = 0; g = x; }",
            "int g; void f(int a) { int x = a + 2; x = 0; g = x; }",
        )
        self.assertNotIn(("f", "g"), items(r))
        self.assertEqual(len(r["changes"]), 1)

    def test_early_return_creates_control_dependence(self):
        r = self.run_pair(
            "int g; void f(int a) { if (a > 5) { return; } g = 1; }",
            "int g; void f(int a) { if (a > 6) { return; } g = 1; }",
        )
        it = items(r)[("f", "g")]
        self.assertEqual(it["how_kind"], "실행 여부")
        self.assertEqual(r["changes"][0]["interval"]["diff"], [[6, 6]])

    def test_dead_code_under_constant_condition(self):
        r = self.run_pair(
            "#define FEAT 0\nint g; void f(void) { if (FEAT) { g = 1; } }",
            "#define FEAT 0\nint g; void f(void) { if (FEAT) { g = 2; } }",
        )
        self.assertEqual(r["items"], [])

    def test_out_parameter_and_alias(self):
        src = """
        typedef struct { int a; int b; } S;
        S g_s; int g_x; int g_y;
        static void set(int *p, int v) { *p = v; }
        void f(void) { set(&g_s.a, {V}); }
        void r1(void) { g_x = g_s.a; }
        void r2(void) { g_y = g_s.b; }
        """
        r = self.run_pair(src.replace("{V}", "1"), src.replace("{V}", "2"))
        names = {k[1] for k in items(r)}
        self.assertIn("g_s.a", names)
        self.assertIn("g_x", names)
        self.assertNotIn("g_y", names)

    def test_union_members_alias(self):
        src = """
        typedef union { unsigned w; unsigned char b[4]; } U;
        U g_u; unsigned char g_o;
        void w(void) { g_u.w = {V}u; }
        void r(void) { g_o = g_u.b[0]; }
        """
        r = self.run_pair(src.replace("{V}", "1"), src.replace("{V}", "2"))
        self.assertIn(("r", "g_o"), items(r))
        self.assertEqual(r["excluded"]["unresolved_pointers"], [])

    def test_function_pointer_call(self):
        src = """
        int g;
        static void cb(int v) { g = v; }
        static void (*hook)(int) = cb;
        void f(int a) { hook(a + {K}); }
        """
        r = self.run_pair(src.replace("{K}", "1"), src.replace("{K}", "2"))
        self.assertIn(("cb", "g"), items(r))

    def test_global_type_and_initializer_change(self):
        r = self.run_pair(
            "unsigned char g_cnt = 0; int g_o; void f(void) { g_cnt++; g_o = g_cnt; }",
            "unsigned short g_cnt = 5; int g_o; void f(void) { g_cnt++; g_o = g_cnt; }",
        )
        kinds = {c["kind"] for c in r["changes"]}
        self.assertIn("type_changed", kinds)
        self.assertIn("global_changed", kinds)
        self.assertIn(("f", "g_o"), items(r))

    def test_hardware_register_write_is_output(self):
        src = "#define REG (*(volatile unsigned *)0x40001000u)\nvoid f(unsigned a) { if (a > {K}u) { REG = 1u; } }"
        r = self.run_pair(src.replace("{K}", "3"), src.replace("{K}", "4"))
        self.assertIn(("f", "*(0x40001000)"), items(r))
        self.assertEqual(r["sil_template"]["memory_map"], [["0x40001000", "0x1000"]])

    def test_deleted_statement_is_traced_in_old_version(self):
        r = self.run_pair(
            "int g, h; void f(void) { g = 1; } void k(void) { h = g; }",
            "int g, h; void f(void) { } void k(void) { h = g; }",
        )
        self.assertEqual(r["changes"][0]["kind"], "removed")
        self.assertEqual(items(r)[("k", "h")]["versions"], ["old"])

    def test_parse_error_is_reported_not_fatal(self):
        r = self.run_pair("int g; void f(void) { g = 1; }", "int g; void f(void) { g = ; }")
        self.assertTrue(r["excluded"]["parse_errors"])


class IntervalTests(unittest.TestCase):
    def pred(self, text):
        ast = c_parser.CParser().parse(f"int f(int x){{ return {text}; }}")
        return d._pred(ast.ext[0].body.block_items[0].expr)

    def test_basic_and_flipped(self):
        self.assertEqual(self.pred("x < 5")[2], [(-d.INF, 4)])
        self.assertEqual(self.pred("10 <= x")[2], [(10, d.INF)])
        self.assertEqual(self.pred("x != 3")[2], [(-d.INF, 2), (4, d.INF)])

    def test_logical(self):
        self.assertEqual(self.pred("x > 2 && x < 8")[2], [(3, 7)])
        self.assertEqual(self.pred("!(x >= 5)")[2], [(-d.INF, 4)])
        self.assertEqual(self.pred("x < 0 || x > 9")[2], [(-d.INF, -1), (10, d.INF)])

    def test_boundaries(self):
        self.assertEqual(d.boundary_values([(100, 199)], (0, 65535)), [99, 100, 149, 199, 200])
        self.assertEqual(d.fmt_interval("t", (100, 199)), "100 ≤ t ≤ 199")


class CompileCommandsTests(unittest.TestCase):
    def test_paths_are_rebased_to_root_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cc = [{"directory": str(root / "build"), "file": str(root / "src" / "a.c"),
                   "command": f"gcc -Iinc -I{root}/cfg -DVARIANT=2 -O2 -c {root}/src/a.c"}]
            (root / "cc.json").write_text(json.dumps(cc), encoding="utf-8")
            flags = load_compile_commands(root / "cc.json", root)
            self.assertEqual(flags["src/a.c"], ["-I", "{ROOT}/build/inc", "-I", "{ROOT}/cfg", "-DVARIANT=2"])


@unittest.skipUnless(HAS_GCC, "gcc 필요")
class SilTests(unittest.TestCase):
    def test_example_scenario_confirms_differences(self):
        scn = json.loads((EXAMPLE / "scenario.json").read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as tmp:
            s = run_sil(EXAMPLE / "old", EXAMPLE / "new", scn, Path(tmp), BuildConfig())
            self.assertEqual(len(s["cases"]), 8)
            self.assertEqual(s["stubs"]["new"], ["Dem_SetEventStatus"])
            obs = s["observables"]
            self.assertEqual(obs["g_timeout"]["diff_cases"], 4)  # tick ∈ {100, 199} × raw 2종
            self.assertEqual(obs["g_sensor.counter"]["example"]["step_label"], "Sensor_MainFunction(0)")
            self.assertEqual(obs["call:Dem_SetEventStatus"]["example"]["new"], "16,1")
            same = [c for c in s["cases"] if c["name"].startswith("tick=99, raw=[0")]
            self.assertEqual(same[0]["status"], "same")
            self.assertTrue((Path(tmp) / "sil_report.md").is_file())

    def test_cases_expand_cartesian(self):
        self.assertEqual(len(expand_cases({"params": {"a": [1, 2], "b": [[1, 2], [3]]}})), 4)

    @unittest.skipUnless(platform.system() == "Linux", "mmap 고정 주소는 Linux 기준")
    def test_hw_register_with_memory_map(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for ver, k in (("old", "3"), ("new", "4")):
                (root / ver).mkdir()
                (root / ver / "m.c").write_text(
                    "#define REG (*(volatile unsigned *)0x40001000u)\n"
                    f"void f(unsigned a) {{ REG = (a > {k}u) ? 1u : 2u; }}\n", encoding="utf-8")
            scn = {"sources": ["m.c"], "memory_map": [["0x40001000", "0x1000"]],
                   "observe": [{"expr": "*(volatile unsigned *)0x40001000", "name": "REG"}],
                   "params": {"a": [3, 4, 5]}, "steps": [{"call": "f", "args": ["${a}"]}]}
            s = run_sil(root / "old", root / "new", scn, root / "out", BuildConfig())
            self.assertEqual([c["status"] for c in s["cases"]], ["same", "diff", "same"])


class CliTests(unittest.TestCase):
    def test_analyze_writes_reports(self):
        with tempfile.TemporaryDirectory() as tmp:
            rc = cli.main(["analyze", "--old", str(EXAMPLE / "old"), "--new", str(EXAMPLE / "new"),
                           "-o", tmp, "-q", "-j", "1"])
            self.assertEqual(rc, 0)
            md = (Path(tmp) / "impact_report.md").read_text(encoding="utf-8")
            self.assertIn("## 2. 영향받는 함수·변수", md)
            self.assertIn("| `UpdateDiagnostic` | `g_faultLatched` |", md)
            self.assertTrue((Path(tmp) / "impact.dot").read_text(encoding="utf-8").startswith("digraph"))
            data = json.loads((Path(tmp) / "impact.json").read_text(encoding="utf-8"))
            self.assertEqual(data["stats"]["changes"], 5)

    def test_fail_on_impact_exit_code(self):
        with tempfile.TemporaryDirectory() as tmp:
            rc = cli.main(["analyze", "--old", str(EXAMPLE / "old"), "--new", str(EXAMPLE / "new"),
                           "-o", tmp, "-q", "-j", "1", "--fail-on-impact"])
            self.assertEqual(rc, 3)

    @unittest.skipUnless(shutil.which("git"), "git 필요")
    def test_git_revisions(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            git = ["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t"]
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            (repo / "m.c").write_text("int g, h; void f(int a) { g = a > 1; } void k(void) { h = g; }\n")
            subprocess.run(git + ["add", "."], check=True)
            subprocess.run(git + ["commit", "-qm", "v1"], check=True)
            (repo / "m.c").write_text("int g, h; void f(int a) { g = a > 2; } void k(void) { h = g; }\n")
            out = Path(tmp) / "out"
            rc = cli.main(["analyze", "--git-repo", str(repo), "--old-rev", "HEAD", "-o", str(out), "-q", "-j", "1"])
            self.assertEqual(rc, 0)
            data = json.loads((out / "impact.json").read_text(encoding="utf-8"))
            self.assertIn(("k", "h"), {(i["func_display"], i["name"]) for i in data["items"]})


if __name__ == "__main__":
    unittest.main()
