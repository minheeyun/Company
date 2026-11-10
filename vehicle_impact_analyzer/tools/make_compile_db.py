#!/usr/bin/env python3
"""빌드 로그 -> compile_commands.json (Bear를 쓸 수 없는 TASKING/GHS/Diab/IDE 빌드용).

  python tools/make_compile_db.py --log build.log --root <소스 루트> --compiler tasking -o compile_commands.json

로그에서 'make: Entering directory', 'cd dir &&'를 따라 작업 디렉터리를 추적하고, 응답 파일(@file)을
펼친다. 결과는 원래 컴파일러 명령 그대로 저장하며, Clang 옵션 변환은 분석 시점(vfia.build_env)에 한다.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from vfia import portable as pt  # noqa: E402
from vfia.build_env import parse_build_log, translate, write_compile_db  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--log", required=True)
    ap.add_argument("--root", default=".", help="상대 경로 기준 디렉터리(로그에 디렉터리 정보가 없을 때)")
    ap.add_argument("--compiler", default="auto", help="auto|tasking|ghs|diab|hightec|gcc|clang")
    ap.add_argument("--compiler-regex", default=None, help="컴파일러 실행 파일을 찾는 정규식(사내 래퍼 등)")
    ap.add_argument("-o", "--out", default="compile_commands.json")
    ap.add_argument("--show-translation", action="store_true", help="Clang 변환 결과와 제거된 옵션 출력")
    a = ap.parse_args(argv)
    pt.setup_console()
    db = parse_build_log(pt.read_text_any(a.log), os.path.abspath(a.root), a.compiler, a.compiler_regex)
    write_compile_db(db, a.out)
    print(f"컴파일 명령 {len(db)}개 -> {a.out}")
    if a.show_translation:
        for e in db:
            tr = translate(e["arguments"], e["directory"], e["file"], e["compiler_profile"])
            print(f"- {pt.rel_or_abs(e['file'], a.root)} [{e['compiler_profile']}]")
            print(f"    clang: {' '.join(tr.args)}")
            print(f"    제거: {' '.join(tr.dropped)}")
    return 0 if db else 1


if __name__ == "__main__":
    sys.exit(main())
