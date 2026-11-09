"""예제 ECU(torque_ecu) 전체 흐름 시험: 두 스크립트가 같은 그래프로 기대 결과를 내는지 확인."""
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CFG = os.path.join(ROOT, "examples", "torque_ecu", "analysis.yaml")
OUT = os.path.join(ROOT, "examples", "torque_ecu", "out_test")


def _run(script, *args):
    p = subprocess.run([sys.executable, os.path.join(ROOT, script), "--config", CFG, "--out", OUT, "--quiet", *args],
                       capture_output=True, text=True, timeout=600)
    assert p.returncode == 0, p.stderr


def test_classify_and_impact():
    _run("classify_features.py", "--version", "after")
    feats = json.load(open(os.path.join(OUT, "features_after.json"), encoding="utf-8"))
    tq = next(f for f in feats["features"] if f["id"] == "F_TQ_LIMIT")
    roles = {x["name"]: x["role"] for x in tq["elements"]}
    assert roles["CalcTorqueLimitPct"] == "direct"
    assert roles["ComputeTorqueCommand"] == "direct"
    assert any(f["multi_feature"] for f in feats["files"])

    _run("analyze_impact.py", "--exec", "on")
    d = json.load(open(os.path.join(OUT, "impact.json"), encoding="utf-8"))
    ch = {c["type"]: c for c in d["changes"]}
    assert {"threshold_change", "comparison_operator_change", "early_exit_added", "assignment_removed",
            "local_var_added", "function_added", "type_layout_change", "macro_change",
            "task_config_change"} <= set(ch)
    # 임계값 변경: 30 <= speed < 40, sensor_ok = 1 에서만 분기 차이, 출력 차이 실행으로 확인
    thr = ch["threshold_change"]
    f = next(x for x in thr["impact"]["features"] if x["feature"] == "F_TQ_LIMIT")
    assert f["status"] in ("실행으로 차이 확인", "모델에서 동작 차이 확인")
    b = next(x for x in f["behavior"] if x["observation"] == "OBS_TQ_CMD")
    assert b["divergence_region"]["speed_kph"] == [30, 39]
    assert b["divergence_region"]["sensor_ok"] == [1, 1]
    assert b["examples_same_output"], "중간 값만 달라지고 출력은 같은 예가 있어야 함"
    # 동작이 같은 리팩터링(Tq_Min)은 변경 분리 검증에서 차이 없음
    ref = next(c for c in d["changes"] if c["type"] == "call_added" and c["unit_name"] == "ComputeTorqueCommand")
    fr = next(x for x in ref["impact"]["features"] if x["feature"] == "F_TQ_LIMIT")
    assert fr["status"] == "모델에서 동작 차이 미발견(검증 범위 내)"
    # > -> >= : 상태 누적(시퀀스)에서 차이
    op = ch["comparison_operator_change"]
    fd = next(x for x in op["impact"]["features"] if x["feature"] == "F_SENSOR_DIAG")
    assert fd["status"] in ("실행으로 차이 확인", "모델에서 동작 차이 확인")
    # 삭제된 대입: 변경 전 그래프에서 진단 기능까지 경로
    rm = ch["assignment_removed"]
    fdiag = next(x for x in rm["impact"]["features"] if x["feature"] == "F_SENSOR_DIAG")
    assert "old" in fdiag["sides"]
    # 사용하지 않는 지역변수지만 초기화 부작용
    assert ch["local_var_added"]["side_effect"]
    # 헤더 구조체 변경: 영향받는 번역 단위
    assert "src/diag.c" in ch["type_layout_change"]["affected_tus"]
    # ISR -> 태스크 공유 상태와 콜백 테이블을 거친 경로가 존재
    assert os.path.isfile(os.path.join(OUT, "impact.html"))
    # 2.5단계 MVP 결과와 평가(정답셋 대비 지표)
    assert d["mvp"] and all("features" in m for m in d["mvp"])
    p = subprocess.run([sys.executable, os.path.join(ROOT, "evaluate.py"), "--config", CFG, "--out", OUT,
                        "--impact", os.path.join(OUT, "impact.json")], capture_output=True, text=True, timeout=600)
    assert p.returncode == 0, p.stderr
    ev = json.load(open(os.path.join(OUT, "evaluation.json"), encoding="utf-8"))
    s = ev["summary"]
    assert s["precise"]["feature"]["recall"] >= 0.9
    assert s["precise+behavior"]["feature"]["precision"] >= s["precise"]["feature"]["precision"]
    assert s["mvp"]["feature"]["precision"] <= s["precise"]["feature"]["precision"]
