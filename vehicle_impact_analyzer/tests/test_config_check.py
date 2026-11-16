"""설정 점검과 새 프로젝트 설정 만들기: 예제 설정을 복사만 한 경우 등을 실행 전에 잡아 안내하는지."""
import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from vfia import portable as pt  # noqa: E402
from vfia.config_check import ConfigError, check_config, load_config_checked  # noqa: E402

EX = os.path.join(ROOT, "examples", "torque_ecu")


def _new_project_module():
    import importlib.util
    spec = importlib.util.spec_from_file_location("new_project", os.path.join(ROOT, "tools", "new_project.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_copied_example_config_is_reported_before_running():
    d = tempfile.mkdtemp(prefix="vfia_cfg_")
    shutil.copy(os.path.join(EX, "analysis.yaml"), d)
    shutil.copy(os.path.join(EX, "vehicle_model.yaml"), d)
    cfg = load_config_checked(os.path.join(d, "analysis.yaml"))
    errors, warns = check_config(cfg)
    text = "\n".join(errors)
    assert "소스 폴더가 없음" in text and "versions.before.root" in text
    assert "execution_v2.yaml" in text and "a2l 파일이 없음" in text
    assert sum("a2l 파일이 없음" in e for e in errors) == 1          # 두 버전이 같이 쓰는 파일은 한 번만
    assert any("예제 설정 값" in w for w in warns)
    # 스크립트는 Traceback 없이 종료 코드 2와 안내문
    for script in ("run_all.py", "classify_features.py", "analyze_impact.py"):
        p = subprocess.run([sys.executable, os.path.join(ROOT, script), "--config", os.path.join(d, "analysis.yaml")],
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300,
                           env=pt.subprocess_env_utf8())
        assert p.returncode == 2, (script, p.stdout, p.stderr)
        assert "Traceback" not in p.stdout + p.stderr and "소스 폴더가 없음" in p.stdout + p.stderr, script


def test_missing_or_broken_config_file():
    d = tempfile.mkdtemp(prefix="vfia_cfg_")
    try:
        load_config_checked(os.path.join(d, "없음.yaml"))
        raise AssertionError("ConfigError 기대")
    except ConfigError as e:
        assert "설정 파일이 없음" in str(e)
    bad = os.path.join(d, "bad.yaml")
    with open(bad, "w", encoding="utf-8") as f:
        f.write("versions:\n  before:\n    root: [unclosed\n")
    try:
        load_config_checked(bad)
        raise AssertionError("ConfigError 기대")
    except ConfigError as e:
        assert "YAML" in str(e)


def test_compile_db_outside_root_is_an_error():
    d = tempfile.mkdtemp(prefix="vfia_cfg_")
    src = os.path.join(d, "src_root")
    os.makedirs(src)
    db = os.path.join(d, "compile_commands.json")
    with open(db, "w", encoding="utf-8") as f:
        json.dump([{"directory": os.path.join(EX, "v1"), "file": "src/torque.c", "arguments": ["gcc", "-c", "src/torque.c"]}], f)
    with open(os.path.join(d, "analysis.yaml"), "w", encoding="utf-8") as f:
        f.write(f"versions:\n  before:\n    root: '{src}'\n    compile_commands: '{db}'\n"
                f"  after:\n    root: '{src}'\n    compile_commands: '{db}'\n")
    errors, warns = check_config(load_config_checked(os.path.join(d, "analysis.yaml")))
    assert any("모두 root 밖" in e for e in errors)
    assert any("vehicle_model이 없음" in w for w in warns)


def test_new_project_from_compile_db_and_from_build_log(capsys):
    np_ = _new_project_module()
    d = tempfile.mkdtemp(prefix="vfia_np_")
    a = os.path.join(d, "a")
    assert np_.main(["--dir", a, "--compiler", "gcc", "--before-root", os.path.join(EX, "v1"),
                     "--before-db", os.path.join(EX, "v1", "compile_commands.json"), "--after-root", os.path.join(EX, "v2"),
                     "--after-db", os.path.join(EX, "v2")]) == 0           # 폴더를 주면 그 안의 compile_commands.json
    cfg = load_config_checked(os.path.join(a, "analysis.yaml"))
    errors, warns = check_config(cfg)
    assert not errors and any("features가 없음" in w for w in warns)
    assert "/" in cfg.versions["before"].root.replace("\\", "/")
    # 이미 있으면 --force 없이 덮어쓰지 않음
    try:
        np_.main(["--dir", a, "--before-root", os.path.join(EX, "v1"), "--before-db", os.path.join(EX, "v1"),
                  "--after-root", os.path.join(EX, "v2"), "--after-db", os.path.join(EX, "v2")])
        raise AssertionError("SystemExit 기대")
    except SystemExit as e:
        assert "--force" in str(e)
    # 빌드 로그에서 compile DB까지 만들기
    logs = {}
    for v in ("v1", "v2"):
        with open(os.path.join(EX, v, "compile_commands.json"), encoding="utf-8") as f:
            db = json.load(f)
        logs[v] = os.path.join(d, f"build_{v}.log")
        with open(logs[v], "w", encoding="utf-8") as f:
            f.write(f"make: Entering directory '{os.path.join(EX, v)}'\n" + "".join(" ".join(e["arguments"]) + "\n" for e in db))
    b = os.path.join(d, "b")
    assert np_.main(["--dir", b, "--compiler", "gcc", "--before-root", os.path.join(EX, "v1"), "--before-log", logs["v1"],
                     "--after-root", os.path.join(EX, "v2"), "--after-log", logs["v2"]]) == 0
    assert os.path.isfile(os.path.join(b, "before", "compile_commands.json"))
    errors, _ = check_config(load_config_checked(os.path.join(b, "analysis.yaml")))
    assert not errors
    out = capsys.readouterr().out
    assert "run_all.py --config" in out
