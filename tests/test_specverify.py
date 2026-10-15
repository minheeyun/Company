import json
import shutil
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from specverify import cfront as C  # noqa: E402
from specverify import checks as K  # noqa: E402
from specverify import cli  # noqa: E402
from specverify import evidence as EV  # noqa: E402
from specverify import report as R  # noqa: E402
from specverify import schemas as SC  # noqa: E402
from specverify import stages as ST  # noqa: E402
from specverify.env import Environment, load_env  # noqa: E402
from specverify.llm import ClaudeLLM, LLMError  # noqa: E402
from specverify.spec import load_spec  # noqa: E402

EX = ROOT / "examples" / "specverify"
SRC = str(EX / "src")
ENV = str(EX / "env.json")
SPEC = str(EX / "spec_blv.md")


def rules(cands):
    return {(c.rule, c.file, c.line): c for c in cands}


class _Tmp(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="specverify_test_"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def model_of(self, code: str, env: Environment | None = None, name="t.c"):
        d = self.tmp / f"src_{len(list(self.tmp.iterdir()))}"
        d.mkdir()
        (d / name).write_text(textwrap.dedent(code), encoding="utf-8")
        return C.build_model([str(d)], env or Environment())


class FrontEndTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.m = C.build_model([SRC], load_env(ENV))

    def test_parses_all_files_without_issues(self):
        self.assertEqual(self.m.issues, [])
        self.assertEqual({f.name for f in self.m.functions.values()},
                         {"Blv_ConvertToMv", "Blv_Filter", "BlvDiag_Init", "BlvDiag_MainFunction", "Adc_ConvEndIsr"})
        fn = self.m.find_function("BlvDiag_MainFunction")
        self.assertEqual((fn.file, fn.start, fn.end), ("Blv_Diag.c", 91, 182))

    def test_macros_typedefs_enums(self):
        self.assertEqual(self.m.macros["BLV_START_MASK_CNT"].numeric, 300)
        self.assertEqual(self.m.macros["BLV_INVALID_RAW"].numeric, 0xFFFF)
        self.assertEqual(self.m.macros["BLV_USE_FILTER"].numeric, 1)  # STD_ON → (1U)
        v = self.m.variables["Blv_Diag.c::s_u8FaultCnt"]
        self.assertEqual((v.type.bits, v.type.signed, v.type.int_range()), (8, False, (0, 255)))
        self.assertEqual(self.m.enums["Blv_StateType"].members["BLV_STATE_SUSPEND"], 2)

    def test_inactive_conditional_code_is_detected(self):
        sf = self.m.files["Blv_Diag.c"]
        self.assertTrue(sf.is_inactive(74))   # #else 쪽 Blv_Filter
        self.assertFalse(sf.is_inactive(49))  # 활성 Blv_Filter
        reg = [r for r in self.m.regions if r.file == "Blv_Diag.c" and r.directive.startswith("#else")]
        self.assertEqual(reg[0].active, False)

    def test_transitions_and_guards(self):
        tr = {(tuple(t.from_states), t.to_state) for t in self.m.transitions}
        self.assertIn((("BLV_STATE_NORMAL",), "BLV_STATE_FAULT"), tr)
        self.assertIn((("BLV_STATE_FAULT",), "BLV_STATE_NORMAL"), tr)
        a = next(a for a in self.m.find_function("BlvDiag_MainFunction").assigns if a.line == 141)
        kinds = [(g.line, g.negated, g.kind) for g in a.guards]
        self.assertIn((116, True, "early-exit"), kinds)   # 유예 조기 반환을 통과한 경로
        self.assertIn((139, False, "if"), kinds)

    def test_context_map_follows_call_graph(self):
        cm = self.m.context_map()
        self.assertEqual(cm["Blv_Diag.c::Blv_Filter"], ["OsTask_10ms"])
        self.assertEqual(cm["Adc_ConvEndIsr"], ["ADC_ConvEnd_ISR"])

    def test_index_mentions_expanded_conditions(self):
        idx = C.render_index(self.m)
        self.assertIn("s_u8StartMaskCnt < 300U", idx)
        self.assertIn("비활성", idx)


class ExampleCheckTests(unittest.TestCase):
    def test_expected_candidates_with_env(self):
        m = C.build_model([SRC], load_env(ENV))
        got = rules(K.run_checks(m))
        self.assertEqual(got[("CONST_COMPARE_RANGE", "Blv_Diag.c", 116)].certainty, "CERTAIN")
        self.assertIn("44", got[("CONST_TRUNCATION", "Blv_Diag.c", 83)].message)
        self.assertEqual(got[("INEFFECTIVE_GUARD", "Blv_Diag.c", 118)].certainty, "PROBABLE")
        wrap = got[("COUNTER_WRAP", "Blv_Diag.c", 141)]
        self.assertEqual(wrap.severity, "HIGH")  # 판정(L157)에 쓰이는 카운터
        self.assertIn(("ARITH_OVERFLOW", "Blv_Diag.c", 38), got)  # int 16bit 대상
        self.assertIn(("SHARED_RCW", "Blv_Diag.c", 122), got)
        self.assertIn(("TORN_ACCESS", "Blv_Diag.c", 126), got)
        self.assertIn(("STATE_NEVER_ENTERED", "Blv_Diag.c", 10), got)
        # 필터의 인덱스/나눗셈은 가드·순환 처리로 보호되므로 오탐이 없어야 함
        self.assertFalse([k for k in got if k[0] in ("INDEX_OOB", "DIV_BY_ZERO")])

    def test_without_env_concurrency_is_review_only(self):
        m = C.build_model([SRC], Environment())
        got = rules(K.run_checks(m))
        self.assertFalse([k for k in got if k[0] in ("SHARED_RCW", "SHARED_RMW", "TORN_ACCESS")])
        unv = [c for k, c in got.items() if k[0] == "SHARED_UNVERIFIED"]
        self.assertTrue(unv and all(c.certainty == "REVIEW" for c in unv))
        self.assertNotIn(("ARITH_OVERFLOW", "Blv_Diag.c", 38), got)  # 기본 int 32bit에서는 넘치지 않음

    @unittest.skipUnless(shutil.which("gcc"), "gcc 필요")
    def test_gcc_confirms_certain_candidates(self):
        m, cands, notes = ST.static_analysis([SRC], load_env(ENV))
        got = rules(cands)
        self.assertTrue(any("type-limits" in t for t in got[("CONST_COMPARE_RANGE", "Blv_Diag.c", 116)].tool_evidence))
        self.assertTrue(got[("CONST_TRUNCATION", "Blv_Diag.c", 83)].tool_evidence)  # 매크로 정의 위치 → 사용 위치로 이동
        self.assertFalse([c for c in cands if "undeclared" in c.message])  # 스텁 헤더 오류는 제외


class RuleTests(_Tmp):
    def rules_of(self, code, env=None):
        return {c.rule for c in K.run_checks(self.model_of(code, env))}, K.run_checks(self.model_of(code, env))

    def test_guarded_counters_are_not_reported(self):
        r, _ = self.rules_of("""
            typedef unsigned char uint8;
            static uint8 a, b, c, d;
            void f(void) {
                if (a < 255U) { a++; }
                if (b != 255U) { b++; }
                if (c >= 10U) { return; }
                c++;
                d++;
                if (d >= 4U) { d = 0U; }
            }
        """)
        self.assertNotIn("COUNTER_WRAP", r)
        self.assertNotIn("INEFFECTIVE_GUARD", r)

    def test_unsigned_decrement_without_lower_bound(self):
        r, cands = self.rules_of("""
            typedef unsigned short uint16;
            static uint16 t;
            void f(void) { t--; }
            void g(void) { if (t > 0U) { t--; } }
        """)
        wraps = [c for c in cands if c.rule == "COUNTER_WRAP"]
        self.assertEqual([c.function for c in wraps], ["f"])

    def test_index_off_by_one_and_constant_oob(self):
        _, cands = self.rules_of("""
            #define N 4U
            static int a[N];
            void f(void) {
                unsigned char i;
                for (i = 0U; i <= N; i++) { a[i] = 0; }
                a[5] = 1;
                for (i = 0U; i < N; i++) { a[i] = 2; }
            }
        """)
        oob = {c.line: c.certainty for c in cands if c.rule == "INDEX_OOB"}
        self.assertEqual(oob, {6: "PROBABLE", 7: "CERTAIN"})

    def test_division_guard(self):
        _, cands = self.rules_of("""
            int f(int n, int d) { if (d != 0) { return n / d; } return 0; }
            int g(int n, int d) { return n / d; }
            int h(int n) { return n / 0; }
        """)
        divs = {c.function: c.certainty for c in cands if c.rule == "DIV_BY_ZERO"}
        self.assertEqual(divs, {"g": "POSSIBLE", "h": "CERTAIN"})

    def test_uninitialized_locals(self):
        _, cands = self.rules_of("""
            int f(int c) { int x; return x + c; }
            int g(int c) { int y; if (c) { y = 1; } else { y = 2; } return y; }
            int h(int c) { int z; if (c) { z = 1; } return z; }
        """)
        un = {c.function: c.certainty for c in cands if c.rule == "UNINIT_LOCAL"}
        self.assertEqual(un, {"f": "PROBABLE", "h": "POSSIBLE"})

    def test_null_deref_of_pointer_param(self):
        _, cands = self.rules_of("""
            typedef struct { int v; } S;
            int f(S *p) { return p->v; }
            int g(S *p) { if (p != 0) { return p->v; } return 0; }
        """)
        self.assertEqual([c.function for c in cands if c.rule == "NULL_DEREF"], ["f"])

    def test_compare_out_of_range_and_trivial(self):
        _, cands = self.rules_of("""
            typedef unsigned char uint8;
            static uint8 x;
            int f(void) { return (x > 255U) + (x >= 0U) + (x == 256); }
        """)
        sev = sorted(c.severity for c in cands if c.rule == "CONST_COMPARE_RANGE")
        self.assertEqual(sev, ["HIGH", "HIGH", "LOW"])

    def test_isr_rmw_with_protection_is_not_reported(self):
        env = Environment()
        env.contexts = [SimpleNamespace(name="T", kind="task", entries=["task"], period_ms=10, priority=1,
                                        preemptive=True, concurrent=True, notes=""),
                        SimpleNamespace(name="I", kind="isr", entries=["isr"], period_ms=None, priority=9,
                                        preemptive=True, concurrent=True, notes="")]
        env.protect_enter, env.protect_exit = ["SuspendAllInterrupts"], ["ResumeAllInterrupts"]
        _, cands = self.rules_of("""
            static volatile unsigned char cnt;
            void isr(void) { cnt = 0U; }
            void task(void) { SuspendAllInterrupts(); cnt++; ResumeAllInterrupts(); }
            void task2(void) { cnt++; }
        """, env)
        # task2는 어떤 컨텍스트에서도 호출되지 않으므로 대상 아님, task는 보호 구간 → 후보 없음
        self.assertFalse([c for c in cands if c.rule == "SHARED_RMW"])

    def test_unprotected_task_rmw_is_reported(self):
        env = Environment()
        env.contexts = [SimpleNamespace(name="T", kind="task", entries=["task"], period_ms=10, priority=1,
                                        preemptive=True, concurrent=True, notes=""),
                        SimpleNamespace(name="I", kind="isr", entries=["isr"], period_ms=None, priority=9,
                                        preemptive=True, concurrent=True, notes="")]
        _, cands = self.rules_of("""
            static volatile unsigned char cnt;
            void isr(void) { cnt++; }
            void task(void) { cnt += 2U; }
        """, env)
        self.assertEqual([(c.rule, c.function) for c in cands if c.rule == "SHARED_RMW"], [("SHARED_RMW", "task")])

    def test_compiler_extensions_and_missing_headers(self):
        m = self.model_of("""
            #include "Missing_Driver.h"
            #include <stdint.h>
            #pragma section ".text_fast"
            static uint8_t s;
            FUNC(void, APP_CODE) App_Run(P2VAR(uint8, AUTOMATIC, APP_VAR) p) {
                __asm("nop");
                *p = s;
            }
            __interrupt void Isr(void) __attribute__((interrupt)) { s++; }
        """)
        self.assertEqual([i for i in m.issues if i.severity == "error"], [])
        self.assertIsNotNone(m.find_function("App_Run"))
        self.assertIsNotNone(m.find_function("Isr"))


class SpecAndEvidenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.doc = load_spec(SPEC)
        cls.m = C.build_model([SRC], load_env(ENV))

    def test_spec_ids_and_multiline_quote(self):
        self.assertEqual(self.doc.ids[0], ("SWR-BLV-001", 21))
        self.assertEqual(len(self.doc.ids), 10)
        hit = self.doc.find_quote("진단은 IgnSts가 ON일 때 수행한다. IgnSts가 START인 동안에는")
        self.assertEqual(hit, (22, 23))
        self.assertIsNone(self.doc.find_quote("사양서에 없는 문장이다"))

    def test_requirement_verification(self):
        reqs = {"requirements": [
            {"id": "SWR-BLV-002", "id_origin": "SPEC", "source": {"line_start": 10, "line_end": 10,
                                                                   "quote": "배터리 전압이 9000mV 이하이면 저전압 조건으로 판단한다."}},
            {"id": "SWR-BLV-099", "id_origin": "SPEC", "source": {"line_start": 1, "line_end": 1, "quote": "지어낸 문장"}}]}
        EV.verify_requirements(self.doc, reqs)
        a, b = reqs["requirements"]
        self.assertEqual(a["source"]["line_start"], 26)
        self.assertIn("보정", a["source_check"]["note"])
        self.assertFalse(b["source_check"]["quote_found"])
        self.assertEqual(b["id_origin"], "GENERATED")

    def test_code_ref_verification(self):
        ok = EV.verify_code_ref(self.m, {"file": "src/Blv_Diag.c", "function": "BlvDiag_MainFunction", "line": 137,
                                         "symbol": "BLV_LOW_MV"})
        self.assertTrue(ok["check"]["verified"])
        self.assertEqual(ok["file"], "Blv_Diag.c")
        bad_sym = EV.verify_code_ref(self.m, {"file": "Blv_Diag.c", "function": "BlvDiag_MainFunction", "line": 100,
                                              "symbol": "BLV_LOW_MV"})
        self.assertFalse(bad_sym["check"]["verified"])
        bad_fn = EV.verify_code_ref(self.m, {"file": "Blv_Diag.c", "function": "BlvDiag_Init", "line": 137, "symbol": ""})
        self.assertFalse(bad_fn["check"]["verified"])
        self.assertIn("BlvDiag_MainFunction", bad_fn["check"]["note"])
        no_file = EV.verify_code_ref(self.m, {"file": "nope.c", "function": "", "line": 1, "symbol": ""})
        self.assertFalse(no_file["check"]["verified"])

    def _finding(self, **kw):
        f = {"id": "X-1", "category": "RUNTIME_ERROR", "verdict": "CONFIRMED_DEFECT", "req_ids": [], "basis": [
            {"kind": "FACT", "statement": "s", "evidence": "e"}], "static_candidates": [],
             "code_refs": [{"file": "Blv_Diag.c", "function": "BlvDiag_MainFunction", "line": 141, "symbol": "s_u8FaultCnt"}]}
        f.update(kw)
        return f

    def test_guardrails(self):
        no_env = C.build_model([SRC], Environment())
        cands = K.run_checks(self.m)
        certain = next(c for c in cands if c.certainty == "CERTAIN")
        fs = [
            self._finding(id="rt"),                                                     # 결정적 근거 없음 → 강등
            self._finding(id="rt_ok", static_candidates=[certain.id]),                  # CERTAIN 후보 연결 → 유지
            self._finding(id="gap", category="SPEC_GAP"),                               # 사양 공백 → 잠재 위험 상한
            self._finding(id="miss", category="MISSING_IMPLEMENTATION", req_ids=["R1"], code_refs=[]),
            self._finding(id="badref", category="LOGIC_ERROR", code_refs=[{"file": "Blv_Diag.c", "function": "f", "line": 9999,
                                                                           "symbol": ""}]),
            self._finding(id="nofact", category="LOGIC_ERROR", basis=[{"kind": "INFERENCE", "statement": "", "evidence": ""}]),
        ]
        EV.apply_guardrails(self.m, fs, {"traces": [{"req_id": "R1", "status": "TRACE_NOT_FOUND"}]}, cands)
        v = {f["id"]: f["verdict"] for f in fs}
        self.assertEqual(v, {"rt": "PROBABLE_DEFECT", "rt_ok": "CONFIRMED_DEFECT", "gap": "POTENTIAL_RISK",
                             "miss": "NEEDS_REVIEW", "badref": "NEEDS_REVIEW", "nofact": "PROBABLE_DEFECT"})
        self.assertTrue(all(f.get("guardrail_notes") for f in fs if f["id"] != "rt_ok"))
        conc = [self._finding(id="c", category="CONCURRENCY", static_candidates=[certain.id])]
        EV.apply_guardrails(no_env, conc, None, cands)
        self.assertEqual(conc[0]["verdict"], "NEEDS_REVIEW")


class FakeStream:
    def __init__(self, msg, calls, kw):
        self.msg, self.calls, self.kw = msg, calls, kw

    def __enter__(self):
        self.calls.append(self.kw)
        return self

    def __exit__(self, *a):
        return False

    def get_final_message(self):
        return self.msg


def fake_client(text, stop="end_turn"):
    msg = SimpleNamespace(content=[SimpleNamespace(type="text", text=text)], stop_reason=stop, model="claude-opus-5-5",
                          usage=SimpleNamespace(input_tokens=10, output_tokens=5, cache_read_input_tokens=0,
                                                cache_creation_input_tokens=0))
    calls = []
    client = SimpleNamespace(beta=SimpleNamespace(messages=SimpleNamespace(stream=lambda **kw: FakeStream(msg, calls, kw))))
    return client, calls


class LLMTests(unittest.TestCase):
    def test_request_shape_and_parse(self):
        client, calls = fake_client('{"traces": []}')
        llm = ClaudeLLM(client=client)
        out = llm.call("t", "sys", [{"type": "text", "text": "x"}], SC.TRACES)
        self.assertEqual(out, {"traces": []})
        kw = calls[0]
        self.assertEqual(kw["model"], "claude-opus-5-5")
        self.assertEqual(kw["thinking"], {"type": "adaptive"})
        self.assertEqual(kw["output_config"]["format"]["type"], "json_schema")
        self.assertIs(kw["output_config"]["format"]["schema"], SC.TRACES)
        self.assertEqual(kw["fallbacks"], "default")
        self.assertEqual(kw["system"][0]["cache_control"], {"type": "ephemeral"})
        self.assertEqual(llm.usage[0]["input_tokens"], 10)

    def test_truncated_and_refused(self):
        for stop in ("max_tokens", "refusal"):
            client, _ = fake_client('{"tr', stop)
            with self.assertRaises(LLMError):
                ClaudeLLM(client=client, fallback=False).call("t", "s", [], SC.TRACES)

    def test_schemas_are_structured_output_compatible(self):
        def walk(s):
            if s.get("type") == "object":
                self.assertIs(s.get("additionalProperties"), False)
                self.assertEqual(set(s["required"]), set(s["properties"]))
                for v in s["properties"].values():
                    walk(v)
            if s.get("type") == "array":
                walk(s["items"])
        for sch in (SC.REQUIREMENTS, SC.TRACES, SC.CONSISTENCY, SC.GAPS, SC.DEFECTS):
            walk(sch)


class PipelineTests(_Tmp):
    def test_trace_hints_convert_time_to_counts(self):
        m = C.build_model([SRC], load_env(ENV))
        run = ST.Run(load_spec(SPEC), m, load_env(ENV), [], [], self.tmp)
        req = {"timers_counters": [{"value": "2", "unit": "s", "behavior": "", "quote": "2초 동안 연속으로"}],
               "outputs": [{"name": "BlvWarnLamp"}]}
        hints = "\n".join(ST.trace_hints(run, req))
        self.assertIn("BLV_CONFIRM_CNT=200", hints)
        self.assertIn("Blv_Diag.c:157", hints)
        self.assertIn("BlvWarnLamp", hints)

    def test_manual_mode_end_to_end_with_sample_responses(self):
        llm_dir = self.tmp / "llm"
        shutil.copytree(EX / "sample_llm", llm_dir, ignore=shutil.ignore_patterns("*.prompt.md"))
        out = self.tmp / "out"
        rc = cli.main(["--spec", SPEC, "--src", SRC, "--env", ENV, "--manual", str(llm_dir), "-o", str(out)])
        self.assertEqual(rc, 0)
        rep = json.loads((out / "report.json").read_text(encoding="utf-8"))
        ids = {f["id"]: f for f in rep["findings"]}
        self.assertEqual(len([i for i in ids if i.startswith("C-")]), 9)
        self.assertEqual(ids["C-007"]["verdict"], "CONFIRMED_DEFECT")
        self.assertIn("D-001", ids["C-004"]["related"])
        # 모든 근거 위치가 원본과 대조됨
        refs = [r for f in rep["findings"] for r in f["code_refs"]]
        self.assertTrue(refs and all(r["check"]["verified"] for r in refs))
        self.assertTrue(all(r["source_check"]["quote_found"] for r in rep["requirements"]["requirements"]))
        for name in ("report.md", "report.html", "test_cases.csv"):
            self.assertTrue((out / name).is_file(), name)
        md = (out / "report.md").read_text(encoding="utf-8")
        for k in ("1. **요구사항**", "2. **코드 위치**", "5. **발생 조건**", "7. **판단 근거**", "10. **테스트 케이스**"):
            self.assertIn(k, md)
        # 보고서만 재생성
        rc = cli.main(["--report-only", str(out / "report.json"), "-o", str(self.tmp / "re")])
        self.assertEqual(rc, 0)
        self.assertTrue((self.tmp / "re" / "report.html").is_file())

    def test_manual_mode_waits_for_missing_response(self):
        out = self.tmp / "out"
        rc = cli.main(["--spec", SPEC, "--src", SRC, "--manual", str(self.tmp / "llm"), "-o", str(out), "--no-gcc"])
        self.assertEqual(rc, 2)
        self.assertTrue((self.tmp / "llm" / "stage1_requirements.prompt.md").is_file())

    def test_static_only_report(self):
        out = self.tmp / "out"
        rc = cli.main(["--src", SRC, "--env", ENV, "--static-only", "-o", str(out), "--no-gcc"])
        self.assertEqual(rc, 0)
        rep = json.loads((out / "report.json").read_text(encoding="utf-8"))
        self.assertNotIn("findings", rep)
        self.assertTrue(rep["candidates"])
        self.assertIn("CERTAIN", (out / "report.md").read_text(encoding="utf-8"))
        html_text = (out / "report.html").read_text(encoding="utf-8")
        self.assertIn("정적 분석 후보", html_text)
        R.to_html(rep)  # 재렌더 가능


if __name__ == "__main__":
    unittest.main()
