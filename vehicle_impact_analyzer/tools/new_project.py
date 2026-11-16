#!/usr/bin/env python3
"""내 ECU 프로젝트용 분석 설정(analysis.yaml, vehicle_model.yaml)을 새로 만든다.

  # compile DB가 이미 있을 때
  python tools/new_project.py --dir my_project --compiler tasking ^
      --before-root S:/proj/26A10 --before-db S:/proj/before/compile_commands.json ^
      --after-root  S:/proj/26A11 --after-db  S:/proj/after/compile_commands.json

  # 빌드 로그만 있을 때(compile DB를 <dir>/before, <dir>/after에 만들어 줌)
  python tools/new_project.py --dir my_project --compiler tasking ^
      --before-root S:/proj/26A10 --before-log S:/proj/26A10/.log/build.log ^
      --after-root  S:/proj/26A11 --after-log  S:/proj/26A11/.log/build.log

만든 뒤 설정을 점검해 결과를 보여 준다. 기존 파일은 --force일 때만 덮어쓰며 .bak으로 남긴다.
"""
import argparse
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from vfia import portable as pt  # noqa: E402
from vfia.build_env import PROFILES, parse_build_log, write_compile_db  # noqa: E402

ANALYSIS = """# 변경 영향 분석 설정 — tools/new_project.py로 생성
# 경로는 / 로 적는다(윈도우도 동일). 상대 경로는 이 파일이 있는 폴더 기준.
project: {project}
build:
  name: '{build_name}'        # 보고서에 표시할 빌드 설정 이름(차종·변형)
  compiler: {compiler}        # auto | tasking | ghs | diab | hightec | gcc | clang
  extra_args: []              # 분석에만 더할 옵션, 예: ['-DVARIANT_A']
  # target: arm-none-eabi     # 비Clang 컴파일러의 데이터 모델 대리 target(기본: 컴파일러 프로필 값)
  # system_includes: ['C:/TASKING/ctc/include']   # 툴체인 표준 헤더 폴더(필요할 때)
versions:
  before:                     # 변경 전
    root: '{before_root}'
    compile_commands: '{before_db}'
  after:                      # 변경 후
    root: '{after_root}'
    compile_commands: '{after_db}'
vehicle_model: [vehicle_model.yaml]   # 기능·관찰 대상·실행 문맥(버전마다 다르면 versions.<버전>.vehicle_model)
analysis:
  exec_compare: auto          # 호스트 C 컴파일러로 전후 비교 실행(없으면 자동 생략)
  # graph_cluster_threshold: 150      # 영향 그래프 묶음 표시 기준
  # mmio_map: {{PWM_DUTY_REG: 0xF0003000}}
output_dir: out
# review_file: feature_review.yaml    # 검토자가 기능 연결을 확정/기각한 기록(선택)
# ground_truth: ground_truth.yaml     # 평가용 정답셋(선택, 있으면 evaluate.py 실행)
# history: {{git: 'S:/proj/repo'}}       # 공변경 근거(선택)
"""

VEHICLE = """# 차량 기능 연결 정보 — 처음에는 비워 두고 실행해도 된다(영향 범위·영향 그래프는 만들어짐).
# 기능과 관찰 대상(기능 출력)을 적으면 '어떤 기능에 영향이 가는지'와 근거 경로, 동작 차이까지 판정한다.
ecu: {ecu}
features: []
# features:
#   - id: F_EXAMPLE                     # 기능 ID
#     name: 예시 기능
#     keywords: [torque, limit]         # 이름·주석에 자주 나오는 단어(기능 분류 보조)
#     seeds:
#       functions: [Example_MainFunction]   # 이 기능을 직접 구현하는 함수
#     observations:                      # 기능 출력: 아래 중 하나 이상
#       - {{id: OBS_EX_GLOBAL, kind: global, variable: g_example_out}}          # 전역 변수
#       - {{id: OBS_EX_RET, kind: function_return, function: Example_Calc}}     # 함수 반환값
#       - {{id: OBS_EX_SIG, kind: signal, signal: Example_Signal}}              # RTE/CAN 신호
execution:                             # 실행 문맥(선택): 태스크·ISR 진입 함수
  tasks: []
  # tasks:
  #   - {{name: Task_10ms, entry: Task_10ms, period_ms: 10, priority: 5}}
  # isrs:
  #   - {{name: ISR_CanRx, entry: Can_RxIsr, priority: 20}}
signals: []
# a2l: [calibration.a2l]               # 캘리브레이션 범위(선택)
"""


def _p(path: str) -> str:
    return os.path.abspath(path).replace("\\", "/")


def _make_db(log: str, root: str, compiler: str, out_dir: str) -> str:
    db = parse_build_log(pt.read_text_any(log), os.path.abspath(root), compiler)
    if not db:
        raise SystemExit(f"빌드 로그에서 컴파일 명령을 찾지 못함: {log}\n"
                         "  로그에 컴파일러 호출 줄 전체가 있는지, --compiler가 맞는지 확인하세요.")
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, "compile_commands.json")
    write_compile_db(db, path)
    print(f"compile DB 생성: {path} (컴파일 명령 {len(db)}개)")
    return path


def _write(path: str, text: str, force: bool) -> None:
    if os.path.exists(path):
        if not force:
            raise SystemExit(f"이미 있음: {path}\n  덮어쓰려면 --force(기존 파일은 .bak으로 보관)")
        shutil.copyfile(path, path + ".bak")
        print(f"기존 파일 보관: {path}.bak")
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    print(f"생성: {path}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="내 ECU 프로젝트용 분석 설정 만들기")
    ap.add_argument("--dir", required=True, help="설정을 만들 폴더(예: my_project)")
    ap.add_argument("--before-root", required=True, help="변경 전 소스 최상위 폴더")
    ap.add_argument("--after-root", required=True, help="변경 후 소스 최상위 폴더")
    ap.add_argument("--before-db", help="변경 전 compile_commands.json(이미 있을 때)")
    ap.add_argument("--after-db", help="변경 후 compile_commands.json(이미 있을 때)")
    ap.add_argument("--before-log", help="변경 전 빌드 로그(compile DB가 없을 때)")
    ap.add_argument("--after-log", help="변경 후 빌드 로그(compile DB가 없을 때)")
    ap.add_argument("--compiler", default="auto", choices=["auto"] + sorted(PROFILES))
    ap.add_argument("--project", default=None, help="보고서에 표시할 프로젝트 이름(기본: 폴더 이름)")
    ap.add_argument("--ecu", default="ECU")
    ap.add_argument("--force", action="store_true", help="기존 설정 파일 덮어쓰기(.bak 보관)")
    a = ap.parse_args(argv)
    pt.setup_console()
    for name in ("before_root", "after_root"):
        if not os.path.isdir(getattr(a, name)):
            raise SystemExit(f"--{name.replace('_', '-')} 폴더가 없음: {getattr(a, name)}")
    os.makedirs(a.dir, exist_ok=True)
    dbs = {}
    for side in ("before", "after"):
        db, log, root = getattr(a, f"{side}_db"), getattr(a, f"{side}_log"), getattr(a, f"{side}_root")
        if db:
            if os.path.isdir(db):
                db = os.path.join(db, "compile_commands.json")
            if not os.path.isfile(db):
                raise SystemExit(f"--{side}-db 파일이 없음: {db}")
        elif log:
            if not os.path.isfile(log):
                raise SystemExit(f"--{side}-log 파일이 없음: {log}")
            db = _make_db(log, root, a.compiler, os.path.join(a.dir, side))
        else:
            raise SystemExit(f"--{side}-db 또는 --{side}-log 중 하나가 필요합니다.")
        dbs[side] = db
    project = a.project or os.path.basename(os.path.abspath(a.dir))
    _write(os.path.join(a.dir, "analysis.yaml"), ANALYSIS.format(
        project=project, build_name=f"{project} 기본 빌드", compiler=a.compiler,
        before_root=_p(a.before_root), before_db=_p(dbs["before"]),
        after_root=_p(a.after_root), after_db=_p(dbs["after"])), a.force)
    vm = os.path.join(a.dir, "vehicle_model.yaml")
    if os.path.exists(vm) and not a.force:
        print(f"유지: {vm} (이미 있음)")
    else:
        _write(vm, VEHICLE.format(ecu=a.ecu), a.force)

    from vfia.config_check import check_config, load_config_checked
    cfg = load_config_checked(os.path.join(a.dir, "analysis.yaml"))
    errors, warns = check_config(cfg)
    print("\n설정 점검:")
    for w in warns:
        print(f"  [주의] {w}")
    for e in errors:
        print(f"  [오류] {e}")
    if errors:
        return 2
    print("  오류 없음.\n\n다음 실행:")
    print(f"  python run_all.py --config {os.path.join(a.dir, 'analysis.yaml')}")
    print("기능·관찰 대상을 vehicle_model.yaml에 적으면 '어떤 기능에 영향이 가는지'까지 판정합니다.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
