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
    if not os.path.isfile(a.log):
        print(f"빌드 로그 파일이 없음: {a.log}", file=sys.stderr)
        return 2
    root = os.path.abspath(a.root)
    db = parse_build_log(pt.read_text_any(a.log), root, a.compiler, a.compiler_regex)
    # -o에 폴더를 주면 그 안에 compile_commands.json을 만든다(없는 폴더는 새로 만듦)
    out = a.out
    if os.path.isdir(out) or out.endswith(("\\", "/")):
        out = os.path.join(out, "compile_commands.json")
    if not db:
        print("컴파일 명령 0개 — 파일을 쓰지 않음(기존 파일 유지)\n  컴파일러 호출 줄을 찾지 못함: 로그에 컴파일 명령 전체"
              "(예: ctc ... src\\a.c)가 찍혀 있는지(상세 출력 빌드), --compiler 값이 맞는지, 사내 래퍼를 쓰면 "
              "--compiler-regex로 지정했는지 확인하세요.", file=sys.stderr)
        return 1
    try:
        os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
        write_compile_db(db, out)
    except PermissionError as e:
        print(f"쓰기 권한이 없음: {out}\n  ({e})\n  -o에 쓸 수 있는 폴더의 파일 경로를 주거나, 같은 파일이 다른 프로그램에서 "
              "열려 있지 않은지 확인하세요.", file=sys.stderr)
        return 2
    print(f"컴파일 명령 {len(db)}개 -> {out}")
    if db:
        outside = [e["file"] for e in db if pt.rel_posix(e["file"], root) is None]
        dirs = sorted({e["directory"] for e in db})
        print(f"  소스 파일 {len({e['file'] for e in db})}개, 작업 디렉터리 {len(dirs)}개(예: {dirs[0]})")
        if outside:
            print(f"  주의: --root({root}) 밖의 파일 {len(outside)}개는 분석에서 제외됨(예: {outside[0]}). "
                  "소스 최상위 폴더를 --root와 설정의 versions.<버전>.root로 지정하세요.")
    if a.show_translation:
        for e in db:
            tr = translate(e["arguments"], e["directory"], e["file"], e["compiler_profile"])
            print(f"- {pt.rel_or_abs(e['file'], a.root)} [{e['compiler_profile']}]")
            print(f"    clang: {' '.join(tr.args)}")
            print(f"    제거: {' '.join(tr.dropped)}")
    return 0 if db else 1


if __name__ == "__main__":
    sys.exit(main())
