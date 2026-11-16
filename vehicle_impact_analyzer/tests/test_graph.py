"""영향 그래프: 데이터(변경 영향·함수 의존·모듈 의존), 전후 비교 상태, 내보내기(DOT·GraphML), 보고서 탭."""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from vfia import portable as pt  # noqa: E402
from vfia.impact_graph import _json_safe, _script_safe, to_dot, to_graphml  # noqa: E402

CFG = os.path.join(ROOT, "examples", "torque_ecu", "analysis.yaml")


@pytest.fixture(scope="module")
def example_out():
    out = tempfile.mkdtemp(prefix="vfia_graph_")
    p = subprocess.run([sys.executable, os.path.join(ROOT, "analyze_impact.py"), "--config", CFG, "--out", out,
                        "--no-behavior", "--quiet"], capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=600, env=pt.subprocess_env_utf8())
    assert p.returncode == 0, p.stderr
    return out


def _load(out):
    with open(os.path.join(out, "impact_graph.json"), encoding="utf-8") as f:
        g = json.load(f)
    with open(os.path.join(out, "impact.json"), encoding="utf-8") as f:
        d = json.load(f)
    return g, d


def test_impact_graph_paths_and_diff_status(example_out):
    g, d = _load(example_out)
    by_type = {c["type"]: c["id"] for c in d["changes"]}
    assert set(g["impact"]) == {c["id"] for c in d["changes"]}
    # 임계값 변경: 변경 지점 → 변경 함수 → ... → 토크 명령 관찰(출력)까지 이어진 근거 경로
    thr = g["impact"][by_type["threshold_change"]]
    paths = [p for p in thr["paths"] if p["feature"] == "F_TQ_LIMIT"]
    assert paths and paths[0]["nodes"][0].startswith("chg:") and paths[0]["nodes"][-1] == "obs:OBS_TQ_CMD"
    assert paths[0]["nodes"][1] == "fn:CalcTorqueLimitPct"
    # 경로 끝의 관찰 대상은 그 경로의 기능 것만
    for p in thr["paths"]:
        tail = [x for x in p["nodes"] if x.startswith("obs:")]
        assert all(g["nodes"][x]["x"]["기능"] == p["feature"] for x in tail)
    # 변경 노드(단계 0)와 위험 후보(출력 경로 위)
    rows = {r[0]: r for r in thr["n"]}
    assert rows["fn:CalcTorqueLimitPct"][1] == 0.0 and rows["obs:OBS_TQ_CMD"][2] == 1 and rows["obs:OBS_TQ_CMD"][4] >= 1
    # 대입문 삭제: 프로그램 그래프에서 사라진 쓰기 연결은 'removed'
    rem = g["impact"][by_type["assignment_removed"]]["e"]
    assert any(e[0] == "fn:Tq_Task" and e[1] == "loc:gv:g_tq_state|last_limit" and e[8] == "removed" for e in rem)
    # 함수 추가: 새로 생긴 호출은 'added'
    add = g["impact"][by_type["function_added"]]["e"]
    assert any(e[1] == "fn:src/torque.c:Tq_Min" and e[2] == "call" and e[8] == "added" for e in add)
    # 공유 변수 연결은 'shared', 호출은 'call'로 구분
    kinds = {e[2] for cid in g["impact"] for e in g["impact"][cid]["e"]}
    assert {"call", "shared", "signal", "observe", "change"} <= kinds


def test_program_and_module_graphs(example_out):
    g, d = _load(example_out)
    centers = {c["id"]: c["centers"] for c in g["changes"]}
    # 설정·매크로·자료형 변경도 함수 의존 그래프의 중심을 가진다
    types = {c["id"]: c["type"] for c in d["changes"]}
    for cid, t in types.items():
        if t in ("task_config_change", "macro_change", "type_layout_change"):
            assert centers[cid], (cid, t)
    pn = set(g["program"]["n"])
    assert {"fn:CalcTorqueLimitPct", "fn:Tq_Task"} <= pn
    # 조건부 호출(제어 조건) 표시가 하나 이상 있다
    assert any(e[3] for e in g["program"]["e"] if e[2] == "call")
    mods = g["modules"]["file"]
    edges = {(e[0], e[1]): e for e in mods["e"]}
    # torque.c -> diag.c 공유 변수(last_limit)는 1 -> 0 으로 줄어 'changed'
    e = edges[("src/torque.c", "src/diag.c")]
    assert e[4] == 1 and e[5] == 0 and e[7] == "changed"
    assert {n[0] for n in mods["n"] if n[3]} >= {"src/torque.c", "src/speed.c", "src/diag.c"}


def test_export_files_parse(example_out):
    import networkx as nx
    ex = os.path.join(example_out, "graph_export")
    files = sorted(os.listdir(ex))
    for need in ("functions.dot", "functions.graphml", "modules_file.dot", "modules_folder.graphml", "impact_C001.dot"):
        assert need in files
    for f in files:
        if f.endswith(".graphml"):
            gg = nx.read_graphml(os.path.join(ex, f))
            assert gg.number_of_nodes() >= 1, f
    if shutil.which("dot"):
        for f in files:
            if f.endswith(".dot"):
                p = subprocess.run(["dot", "-Tsvg", os.path.join(ex, f), "-o", os.devnull], capture_output=True, text=True)
                assert p.returncode == 0, (f, p.stderr)


def test_report_has_offline_graph_tab(example_out):
    with open(os.path.join(example_out, "impact.html"), encoding="utf-8") as f:
        html = f.read()
    assert "id='tab-graph'" in html and "id='tabbtn-graph'" in html and "id='ig-data'" in html
    assert "Cytoscape.js" in html and "href='#graph:C001:impact'" in html
    # 인터넷 없이: 외부 스크립트·스타일 참조가 없다
    assert not re.search(r"<script[^>]+src=", html) and not re.search(r"<link[^>]+href=", html)
    # 데이터 JSON이 그대로 읽힌다
    m = re.search(r"<script id='ig-data' type='application/json'>(.*?)</script>", html, re.S)
    data = json.loads(m.group(1))
    assert data["changes"] and data["impact"]


def test_dot_and_graphml_escaping():
    import networkx as nx
    nodes = [{"id": 'fn:"a"\\b', "label": "한글 <x> & \"q\"", "kind": "function", "changed": True, "risk": 2, "status": "added"},
             {"id": "loc:gv:v|f", "label": "v.f", "kind": "variable", "changed": False, "risk": 0, "status": "same"}]
    edges = [{"source": 'fn:"a"\\b', "target": "loc:gv:v|f", "kind": "shared", "control": True, "label": "쓰기 \"x\"",
              "status": "removed", "count": 2}]
    d = tempfile.mkdtemp(prefix="vfia_exp_")
    gp = os.path.join(d, "t.graphml")
    with open(gp, "w", encoding="utf-8") as f:
        f.write(to_graphml("t", nodes, edges))
    gg = nx.read_graphml(gp)
    assert gg.nodes['fn:"a"\\b']["label"] == "한글 <x> & \"q\"" and gg.nodes['fn:"a"\\b']["changed"] is True
    assert list(gg.edges(data=True))[0][2]["status"] == "removed"
    dot = to_dot("t", nodes, edges)
    assert 'style="dashed"' in dot and "#c62828" in dot
    if shutil.which("dot"):
        p = subprocess.run(["dot", "-Tsvg"], input=dot, capture_output=True, text=True, encoding="utf-8")
        assert p.returncode == 0, p.stderr


def test_script_escaping():
    assert "</script" not in _script_safe("var a='</script>';").lower()
    assert json.loads(_json_safe(json.dumps({"a": "</script><!--"}))) == {"a": "</script><!--"}
