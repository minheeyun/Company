#!/usr/bin/env python3
"""실행 환경 점검(Windows / Linux / macOS).

  python tools/check_env.py

필수 항목(Python 버전, 패키지, libclang 로딩, 컴파일러 내장 헤더)과 선택 항목(전후 비교 실행용 C 컴파일러,
CBMC, git)을 확인하고, 빠진 것이 있으면 설치 명령을 알려 준다. 필수 항목이 모두 갖춰지면 종료 코드 0.
"""
import importlib
import os
import platform
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from vfia import portable as pt  # noqa: E402

OK, WARN, FAIL = "[정상]", "[주의]", "[실패]"


def _ver(mod) -> str:
    try:
        from importlib import metadata
        name = {"clang": "libclang", "z3": "z3-solver", "yaml": "PyYAML"}.get(mod.__name__, mod.__name__)
        return metadata.version(name)
    except Exception:
        v = getattr(mod, "__version__", None)
        return v if isinstance(v, str) else "?"


def main() -> int:
    pt.setup_console()
    win = pt.IS_WINDOWS
    hard_fail = False
    print(f"운영체제: {platform.system()} {platform.release()} ({platform.machine()}), Python {platform.python_version()}")
    print(f"표준 출력 인코딩: {sys.stdout.encoding}, 파일 기본 인코딩: {__import__('locale').getpreferredencoding(False)}"
          " (분석기는 입력·출력 파일을 UTF-8로 다루고 BOM·UTF-16·cp949 입력도 읽는다)")
    print()

    # 1. Python
    if sys.version_info < (3, 8):
        print(f"{FAIL} Python 3.8 이상이 필요합니다(현재 {platform.python_version()}).")
        hard_fail = True
    else:
        print(f"{OK} Python {platform.python_version()}")

    # 2. 패키지
    missing = []
    for mod, pip_name in (("clang.cindex", "libclang"), ("z3", "z3-solver"), ("networkx", "networkx"),
                          ("yaml", "PyYAML")):
        try:
            m = importlib.import_module(mod)
            top = importlib.import_module(mod.split(".")[0])
            print(f"{OK} {pip_name} {_ver(top)}")
            del m
        except Exception as e:
            print(f"{FAIL} {pip_name} 가져오기 실패: {e}")
            missing.append(pip_name)
    if missing:
        hard_fail = True
        print("      설치: python -m pip install -r requirements.txt")

    # 3. libclang과 내장 헤더
    if "libclang" not in missing:
        try:
            from vfia import clang_api as ca
            ca.init_libclang()
            print(f"{OK} libclang 로딩: {ca.libclang_version() or '버전 미상'} ({ca._libclang_path() or '?'})")
            info = ca.resource_include_info()
            if info["fallback"]:
                print(f"{WARN} Clang 내장 헤더(resource-dir)를 찾지 못해 vfia 대체 헤더를 사용합니다: {info['path']}")
                print("      stdint.h·stdbool.h·stddef.h·limits.h·stdarg.h·float.h는 동작합니다. 분석 대상이 stdio.h·string.h 같은")
                print("      C 라이브러리 헤더를 쓰거나 결과를 Clang 정식 헤더와 맞추려면 LLVM을 설치하세요:")
                print("        Windows: winget install --id LLVM.LLVM -e   (설치 후 새 터미널)" if win else
                      "        Linux: sudo apt-get install clang   /   macOS: brew install llvm")
                print("      또는 VFIA_CLANG_RESOURCE_DIR 환경 변수에 'lib/clang/<버전>' 경로를 지정")
            else:
                print(f"{OK} Clang 내장 헤더: {info['path']} ({info['source']})")
        except Exception as e:
            hard_fail = True
            print(f"{FAIL} libclang을 불러오지 못했습니다: {e}")
            print("      pip 휠을 다시 설치하거나(python -m pip install --force-reinstall libclang),")
            print("      LLVM의 libclang 경로를 VFIA_LIBCLANG 환경 변수로 지정하세요"
                  + (" (예: C:\\Program Files\\LLVM\\bin\\libclang.dll)" if win else " (예: /usr/lib/llvm-18/lib/libclang.so)"))

    # 4. 선택: 전후 비교 실행용 C 컴파일러
    from vfia.behavior_checker import exec_per_process, host_compiler
    from vfia.config import AnalysisOptions
    cc = host_compiler(AnalysisOptions())
    if cc:
        mode = "입력 벡터마다 프로세스 실행(fork 없음)" if exec_per_process() else "fork"
        print(f"{OK} 전후 비교 실행용 C 컴파일러: {cc} — 실행 방식: {mode}")
        if win and os.path.basename(cc).lower().startswith("clang"):
            print("      Windows의 clang은 링크에 Visual Studio Build Tools가 필요합니다. 빌드 실패 시 MinGW-w64 gcc를 설치하거나")
            print("      analysis.exec_compiler로 컴파일러를 지정하세요.")
    else:
        print(f"{WARN} 전후 비교 실행용 C 컴파일러 없음 — 실행 비교는 건너뛰고 기호 실행(z3) 결과만 사용합니다.")
        print("      " + ("Windows: winget install --id BrechtSanders.WinLibs.POSIX.UCRT -e   (MinGW-w64 gcc, 설치 후 새 터미널)"
                         if win else "Linux: sudo apt-get install gcc"))

    # 5. 선택: CBMC, git
    if shutil.which("goto-cc") and shutil.which("goto-diff"):
        print(f"{OK} CBMC goto-diff 기준선 사용 가능")
    else:
        print(f"{WARN} CBMC(goto-cc/goto-diff) 없음 — evaluate.py의 goto-diff 기준선 비교만 빠집니다.")
    git = shutil.which("git")
    if git:
        try:
            v = pt.run_text([git, "--version"], timeout=10).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            v = git
        print(f"{OK} {v} (history.git 공변경 마이닝에 사용)")
    else:
        print(f"{WARN} git 없음 — history.git 설정(공변경 마이닝)은 쓸 수 없고 JSON 이력만 사용 가능합니다.")

    # 6. 실제 파싱 시험
    if not hard_fail:
        import tempfile
        import clang.cindex as ci
        from vfia import clang_api as ca
        d = tempfile.mkdtemp(prefix="vfia_chk_")
        p = os.path.join(d, "t.c")
        with open(p, "w", encoding="utf-8") as f:
            f.write("#include <stdint.h>\n#include <stdbool.h>\nbool f(uint8_t x) { return x > 3u; }\n")
        tu = ci.Index.create().parse(p, args=["-std=c99", "-isystem", ca.detect_resource_include()])
        errs = [x.spelling for x in tu.diagnostics if x.severity >= ci.Diagnostic.Error]
        if errs:
            hard_fail = True
            print(f"{FAIL} 시험 파싱 오류: {errs[:3]}")
        else:
            print(f"{OK} 시험 파싱(stdint.h, stdbool.h)")
    print()
    print("필수 항목이 모두 정상입니다. 예제 실행: python run_all.py --config examples/torque_ecu/analysis.yaml"
          if not hard_fail else "필수 항목에 문제가 있습니다. 위 안내를 따라 설치한 뒤 다시 실행하세요.")
    return 1 if hard_fail else 0


if __name__ == "__main__":
    sys.exit(main())
