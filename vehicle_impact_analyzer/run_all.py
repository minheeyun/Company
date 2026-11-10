#!/usr/bin/env python3
"""기능 분류 -> 변경 영향 분석 -> 평가를 한 번에 실행한다(Windows / Linux 공통).

  python run_all.py --config examples/torque_ecu/analysis.yaml
  python run_all.py --config my_project/analysis.yaml --exec off --skip-eval

각 단계는 별도 프로세스로 실행하며(해시 시드 고정, UTF-8 입출력), 한 단계가 실패하면 멈춘다.
산출물은 설정의 output_dir(기본 out/)에 생긴다.
"""
import argparse
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from vfia import portable as pt  # noqa: E402
from vfia.config import load_config  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--config", required=True)
    ap.add_argument("--before", default="before")
    ap.add_argument("--after", default="after")
    ap.add_argument("--out", default=None)
    ap.add_argument("--exec", dest="exec_mode", default=None, choices=["auto", "on", "off"])
    ap.add_argument("--level", default="full", choices=["full", "function"])
    ap.add_argument("--no-behavior", action="store_true")
    ap.add_argument("--rebuild", action="store_true")
    ap.add_argument("--strict", action="store_true")
    ap.add_argument("--skip-eval", action="store_true", help="정답셋이 없으면 평가 단계를 건너뛴다(자동)")
    a = ap.parse_args(argv)
    pt.setup_console()
    cfg = load_config(a.config)
    out = a.out or cfg.output_dir
    common = ["--config", a.config] + (["--out", a.out] if a.out else [])
    impact = ["--before", a.before, "--after", a.after, "--level", a.level]
    impact += (["--exec", a.exec_mode] if a.exec_mode else []) + (["--no-behavior"] if a.no_behavior else [])
    impact += (["--rebuild"] if a.rebuild else []) + (["--strict"] if a.strict else [])
    steps = [("기능 분류", "classify_features.py", ["--version", a.after] + (["--rebuild"] if a.rebuild else [])),
             ("변경 영향 분석", "analyze_impact.py", impact)]
    if not a.skip_eval and cfg.ground_truth and os.path.isfile(cfg.ground_truth) and a.level == "full":
        steps.append(("평가", "evaluate.py", ["--before", a.before, "--after", a.after, "--impact",
                                             os.path.join(out, "impact.json")]))
    env = pt.subprocess_env_utf8()
    env["PYTHONHASHSEED"] = "0"
    for i, (name, script, args) in enumerate(steps, 1):
        print(f"\n=== [{i}/{len(steps)}] {name}: {script} ===", flush=True)
        t0 = time.time()
        r = subprocess.run([sys.executable, os.path.join(HERE, script)] + common + args, env=env)
        if r.returncode != 0:
            print(f"\n[{name}] 실패(종료 코드 {r.returncode}). 위 메시지를 확인하세요.")
            return r.returncode
        print(f"--- {name} 완료 ({time.time() - t0:.1f}초)")
    print("\n산출물:")
    for f in (f"features_{a.after}.html", "impact.html", "evaluation.md", "impact_mvp.json"):
        p = os.path.join(out, f)
        if os.path.isfile(p):
            print(f"  {p}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
