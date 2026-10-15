"""5~6단계: 검증용 예제 프로젝트의 시나리오별 기대 영향 범위(examples/impact_demo/expected_impact.json) 자동 검증
+ 보고서/CLI/Git/패치/캐시 입력 경로 테스트."""

import csv
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

from cimpact_util import DEMO, HAVE_CLANG, ROOT, SKIP_MSG, analyze, fn_levels

EXPECTED = json.loads((DEMO / "expected_impact.json").read_text(encoding="utf-8"))
RESULT = None


def setUpModule():
    global RESULT
    if not HAVE_CLANG:
        return
    from c_impact.config import AnalysisConfig
    cfg = AnalysisConfig.load(DEMO / "analysis_config.json")
    RESULT = analyze(str(DEMO / "baseline"), str(DEMO / "current"), cfg,
                     compdb=str(DEMO / "compile_commands.json"), compdb_root=str(DEMO / "baseline"))


def find_change(sel):
    for c in RESULT["changes"]:
        if sel["type"] not in c["types"]:
            continue
        if "function" in sel and (c.get("function") or "").split("::")[-1] != sel["function"]:
            continue
        if "variable" in sel and (c.get("variable") or "").split("::")[-1] != sel["variable"]:
            continue
        if "macro" in sel and c.get("macro_name") != sel["macro"]:
            continue
        if c.get("summary_only") and sel["type"] != "STATE_TRANSITION_CHANGED":
            continue
        return c
    raise AssertionError(f"변경점 없음: {sel}")


def per_change(cid):
    return next(p for p in RESULT["impact"]["per_change"] if p["change"] == cid)


@unittest.skipUnless(HAVE_CLANG, SKIP_MSG)
class ScenarioTests(unittest.TestCase):
    """expected_impact.json 의 시나리오마다 실제 분석 결과가 기대 관계를 탐지하는지 확인."""

    def test_scenarios(self):
        for sc in EXPECTED["scenarios"]:
            with self.subTest(sc["id"] + " " + sc["title"]):
                ch = find_change(sc["change"])
                if ch.get("summary_only"):
                    det = ch["detail"]
                    self.assertEqual(det["transitions_removed"], sc["transitions_removed"])
                    self.assertEqual(det["transitions_added"], sc["transitions_added"])
                    ch = next(c for c in RESULT["changes"] if c.get("function") == ch["function"]
                              and "STATE_TRANSITION_CHANGED" in c["types"] and not c.get("summary_only"))
                pc = per_change(ch["id"])
                lv = {k.split("::")[-1]: v["level"] for k, v in pc["functions"].items()}
                edges = {k.split("::")[-1]: set(v["edges"]) for k, v in pc["functions"].items()}
                for f in sc.get("direct", []):
                    self.assertEqual(lv.get(f), "DIRECT", f"{f} DIRECT 기대, 실제 {lv.get(f)}")
                for f in sc.get("indirect", []):
                    self.assertEqual(lv.get(f), "INDIRECT", f"{f} INDIRECT 기대, 실제 {lv.get(f)}")
                for f in sc.get("potential", []):
                    self.assertEqual(lv.get(f), "POTENTIAL", f"{f} POTENTIAL 기대, 실제 {lv.get(f)}")
                for f, es in sc.get("edges", {}).items():
                    self.assertTrue(set(es) <= edges.get(f, set()), f"{f}: {es} ⊄ {edges.get(f)}")
                for f in sc.get("not_impacted", []):
                    self.assertNotIn(f, lv, f"{f} 는 영향 없음이어야 함")
                for f in sc.get("call_context", []):
                    self.assertTrue(pc["functions"][f]["call_context_only"], f)
                if sc.get("exact"):
                    self.assertEqual(set(lv), set(sc["direct"]) | set(sc.get("indirect", [])))
                outs = {o["callee"] for o in pc["external_outputs"]}
                for o in sc.get("outputs", []):
                    self.assertIn(o, outs)
                for a, b in sc.get("no_call_relation", []):
                    self.assertFalse(self._calls_transitively(a, b))
                    self.assertFalse(self._calls_transitively(b, a))
                for v in sc.get("variables", []):
                    self.assertTrue(any(x["name"] == v for x in RESULT["impact"]["variables"]), v)
                for d in sc.get("defects", []):
                    self.assertIn(d, {x["type"] for x in RESULT["defects"]})
                for d in sc.get("defects_confirmed", []):
                    self.assertTrue(any(x["type"] == d and x["status"] == "CONFIRMED_BY_STATIC_ANALYSIS"
                                        for x in RESULT["defects"]), d)
                if "boundary_values_changed" in sc:
                    t = next(t for t in RESULT["tests"] if t["kind"] == "BOUNDARY" and ch["id"] in t["related_changes"])
                    changed = sorted(r["value"] for r in t["code_behavior"] if r["changed"])
                    self.assertEqual(changed, sc["boundary_values_changed"])
                if "min_callers" in sc:
                    chains = RESULT["call_chains"][ch["function"]]
                    self.assertGreaterEqual(len({c["chain"][-2] for c in chains}), sc["min_callers"])
                    self.assertTrue(set(sc["entry_points"]) <= {c["chain"][0] for c in chains})

    def _calls_transitively(self, a, b):
        cg = RESULT["callgraph"]["edges"]
        seen, stack = set(), [a]
        while stack:
            f = stack.pop()
            for e in cg:
                if e["caller"] == f and e["callee"] not in seen:
                    if e["callee"] == b:
                        return True
                    seen.add(e["callee"])
                    stack.append(e["callee"])
        return False

    def test_format_only_and_inactive(self):
        for fn in EXPECTED["format_only"]:
            self.assertFalse(any(c.get("function") == fn for c in RESULT["changes"]))
            self.assertTrue(any(n.get("function") == fn and n["type"] == "FORMAT_ONLY" for n in RESULT["non_semantic"]))
        ina = [c for c in RESULT["changes"] if "INACTIVE_CODE_CHANGED" in c["types"]]
        self.assertEqual([c["file"] for c in ina], [EXPECTED["inactive_code_file"]])
        lv = fn_levels(RESULT, ina[0]["id"])
        self.assertEqual(set(lv.values()), {"POTENTIAL"})

    def test_no_impact_confirmed(self):
        names = {n["name"] for n in RESULT["impact"]["no_impact"]}
        self.assertTrue(set(EXPECTED["no_impact_confirmed"]) <= names)
        levels = fn_levels(RESULT)
        self.assertTrue(names.isdisjoint(levels))

    def test_concurrency(self):
        conc = [d for d in RESULT["defects"] if d["type"] == "SHARED_VARIABLE_RACE"]
        for v in EXPECTED["concurrency_shared"]:
            self.assertTrue(any(v in d["description"] for d in conc), v)

    def test_impact_table_rows(self):
        row = next(r for r in RESULT["impact_table"] if r["function_name"] == "Output_UpdateGauge")
        for k in ("location", "change_category", "function_name", "file", "path_type", "level", "evidence", "priority"):
            self.assertTrue(row[k], k)
        self.assertIn("Sensor_Task", row["evidence"])
        self.assertIn(row["priority"], ("High", "Medium", "Low"))

    def test_regression_tests_have_required_fields(self):
        req = {"purpose", "target_functions", "inputs", "preconditions", "scenario", "expected", "reason",
               "related_changes", "impact_path"}
        self.assertTrue(RESULT["tests"])
        for t in RESULT["tests"]:
            self.assertTrue(req <= set(t), t["id"])
        # 요구사항에 기대 동작이 있는 경우만 예상 결과를 채운다
        t = next(t for t in RESULT["tests"] if t["kind"] == "BOUNDARY" and "Ctrl_CheckOverheat" in t["target_functions"])
        self.assertEqual(t["expected_source"], "requirement")
        self.assertIn("REQ-HTR-010", [r["id"] for r in t["requirements"]])
        self.assertIn("UT-CTRL-001", [r["id"] for r in t["existing_tests"]])
        t2 = next(t for t in RESULT["tests"] if t["kind"] == "BOUNDARY" and "Output_Update" in t["target_functions"])
        self.assertEqual(t2["expected_source"], "unknown")  # REQ-LMP-030 은 기대 동작 미기재
        self.assertIn("사양 확인 필요", t2["expected"])

    def test_completeness_flags(self):
        self.assertEqual(RESULT["analysis_mode"], "compdb")
        self.assertTrue(RESULT["completeness"]["complete"])

    def test_graph_marks_uncertain_edges(self):
        pc = per_change(find_change({"function": "Evt_OnSensorFail", "type": "CALL_ARGS_CHANGED"})["id"])
        unc = [e for e in pc["graph"]["edges"] if not e["certain"]]
        self.assertTrue(any(e["to"] == "fn:Event_Dispatch" for e in unc))


@unittest.skipUnless(HAVE_CLANG, SKIP_MSG)
class LimitedModeTests(unittest.TestCase):
    def test_without_compdb_is_marked_incomplete(self):
        r = analyze(str(DEMO / "baseline"), str(DEMO / "current"))
        self.assertEqual(r["analysis_mode"], "limited")
        self.assertFalse(r["completeness"]["complete"])
        self.assertTrue(any("제한 모드" in n for n in r["completeness"]["notes"]))
        self.assertTrue(any("태스크/ISR" in x for x in r["uncertainty"]["limitations"]))


@unittest.skipUnless(HAVE_CLANG, SKIP_MSG)
class OutputAndCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def run_cli(self, *args):
        env = dict(os.environ, PYTHONPATH=str(ROOT))
        r = subprocess.run([sys.executable, "-m", "c_impact", *args], capture_output=True, text=True, env=env,
                           cwd=str(ROOT))
        self.assertEqual(r.returncode, 0, r.stderr[-3000:])
        return r

    def test_reports_written(self):
        from c_impact.reports import write_all
        files = write_all(RESULT, self.tmp.name)
        data = json.loads(open(files["json"], encoding="utf-8").read())
        self.assertEqual(data["summary"], json.loads(json.dumps(RESULT["summary"])))
        html = open(files["html"], encoding="utf-8").read()
        for h in ("1. 변경 내용 요약", "2. 직접 영향 함수", "3. 간접 영향 함수", "4. 영향 전파 경로",
                  "5. 변경으로 달라질 수 있는 동작", "6. 잠재 결함 및 위험", "7. 분석 근거 및 불확실성",
                  "8. 검증이 필요한 함수와 테스트 케이스", "9. 추가 수동 리뷰가 필요한 항목", "<svg"):
            self.assertIn(h, html)
        with open(files["impact_table_csv"], encoding="utf-8-sig") as f:
            rows = list(csv.reader(f))
        self.assertEqual(rows[0][:4], ["변경 ID", "변경 위치", "변경 유형", "영향 함수"])
        self.assertEqual(len(rows) - 1, len(RESULT["impact_table"]))
        dot = open(files["dot"], encoding="utf-8").read()
        self.assertIn("style=dashed", dot)  # 불확실 의존 구분

    def test_cli_dirs_mode_and_cache(self):
        out = os.path.join(self.tmp.name, "o")
        args = ["analyze", "--base-dir", str(DEMO / "baseline"), "--curr-dir", str(DEMO / "current"),
                "--config", str(DEMO / "analysis_config.json"), "-o", out, "-q"]
        self.run_cli(*args)
        self.assertTrue(os.path.exists(os.path.join(out, "impact_report.html")))
        r1 = json.load(open(os.path.join(out, "impact_result.json"), encoding="utf-8"))
        # 모든 .c 가 변경된 app_config.h 를 포함하므로 첫 실행에서는 Current 캐시 적중 없음(헤더 해시로 무효화)
        self.assertEqual(r1["projects"]["current"]["stats"]["cache_hits"], 0)
        self.run_cli(*args)
        r2 = json.load(open(os.path.join(out, "impact_result.json"), encoding="utf-8"))
        self.assertEqual(r2["projects"]["baseline"]["stats"]["cache_misses"], 0)
        self.assertEqual(r2["projects"]["current"]["stats"]["cache_misses"], 0)
        self.assertEqual(r2["projects"]["current"]["stats"]["cache_hits"], 9)
        self.assertEqual(r2["summary"], r1["summary"])

    def test_cli_stage_commands(self):
        r = self.run_cli("ast", str(DEMO / "current"), "--function", "Fault_Monitor")
        self.assertIn("s_overTempCnt ++", r.stdout)
        r = self.run_cli("changes", "--base-dir", str(DEMO / "baseline"), "--curr-dir", str(DEMO / "current"))
        self.assertIn("BOUNDARY", r.stdout.upper().replace("경계 연산자", "BOUNDARY"))
        r = self.run_cli("callgraph", str(DEMO / "current"), "--from", "Util_ScaleTemp")
        self.assertIn("Task_100ms", r.stdout)
        dot = os.path.join(self.tmp.name, "cfg.dot")
        r = self.run_cli("cfg", str(DEMO / "current"), "Ctrl_Main10ms", "--dot", dot)
        self.assertIn("digraph", open(dot, encoding="utf-8").read())

    def test_git_commit_and_working_tree_modes(self):
        if not shutil.which("git"):
            self.skipTest("git 없음")
        repo = os.path.join(self.tmp.name, "repo")
        shutil.copytree(DEMO / "baseline", repo)
        g = ["git", "-C", repo, "-c", "user.email=t@example.com", "-c", "user.name=t"]
        subprocess.run(["git", "init", "-q", repo], check=True)
        subprocess.run(g + ["add", "-A"], check=True)
        subprocess.run(g + ["commit", "-qm", "base"], check=True)
        # copy2 는 mtime 을 보존해 git 이 같은 크기의 변경을 놓칠 수 있으므로 copy 사용
        shutil.copytree(DEMO / "current", repo, dirs_exist_ok=True, copy_function=shutil.copy)
        # 작업 트리 변경 분석
        out1 = os.path.join(self.tmp.name, "wt")
        self.run_cli("analyze", "--repo", repo, "--base-rev", "HEAD", "--working-tree", "-o", out1, "-q", "--no-cache")
        r1 = json.load(open(os.path.join(out1, "impact_result.json"), encoding="utf-8"))
        self.assertEqual(r1["inputs"]["head_rev"], "WORKING_TREE")
        self.assertIn("src/fault.c", r1["inputs"]["git_changed_files"])
        # 커밋 간 비교
        subprocess.run(g + ["add", "-A"], check=True)
        subprocess.run(g + ["commit", "-qm", "curr"], check=True)
        out2 = os.path.join(self.tmp.name, "cm")
        self.run_cli("analyze", "--repo", repo, "--base-rev", "HEAD~1", "--head-rev", "HEAD", "-o", out2, "-q",
                     "--no-cache")
        r2 = json.load(open(os.path.join(out2, "impact_result.json"), encoding="utf-8"))
        self.assertEqual([c["types"] for c in r2["changes"]], [c["types"] for c in r1["changes"]])
        self.assertEqual(r2["summary"]["functions_by_level"], r1["summary"]["functions_by_level"])
        # 원본 저장소는 변경되지 않음
        st = subprocess.run(["git", "-C", repo, "status", "--porcelain"], capture_output=True, text=True).stdout
        self.assertEqual(st.strip(), "")

    def test_patch_mode(self):
        from c_impact.textdiff import unified
        patch = os.path.join(self.tmp.name, "change.diff")
        with open(patch, "w", encoding="utf-8") as f:
            f.write(unified(str(DEMO / "baseline/src/control.c"), str(DEMO / "current/src/control.c"),
                            "src/control.c") + "\n")
        out = os.path.join(self.tmp.name, "p")
        self.run_cli("analyze", "--base-dir", str(DEMO / "baseline"), "--patch", patch, "-o", out, "-q", "--no-cache")
        r = json.load(open(os.path.join(out, "impact_result.json"), encoding="utf-8"))
        self.assertEqual(r["inputs"]["patch_files"], ["src/control.c"])
        self.assertEqual({c.get("function") for c in r["changes"]}, {"Ctrl_CheckOverheat", "Ctrl_Main10ms"})


if __name__ == "__main__":
    unittest.main()
