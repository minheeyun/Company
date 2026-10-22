#!/usr/bin/env python3
"""AUTOSAR 메모리 클래스(memclass/ptrclass) 이름을 빈 매크로로 정의하는 헤더를 만든다.

GHS 등의 Compiler.h 는 `#define VAR(vartype, memclass) memclass vartype` 처럼 메모리 클래스 이름을 그대로 남긴다.
그 이름들(DET_VAR_NOINIT, OS_VAR, COM_VAR ...)은 보통 빌드 환경에서 생성되는 Compiler_Cfg.h 에서 빈 값으로
정의되는데, 비교용 폴더에 그 파일이 없으면 수백 개 파일이 파싱에 실패한다.

이 스크립트는 변경 전/후 폴더의 *.c / *.h 에서
  VAR, CONST, FUNC, P2VAR, P2CONST, CONSTP2VAR, CONSTP2CONST, P2FUNC, CONSTP2FUNC, FUNC_P2VAR, FUNC_P2CONST
의 memclass/ptrclass 자리에 쓰인 이름을 모아 `#ifndef X / #define X / #endif` 로 정의한 헤더를 만든다.
Compiler*.h 에서 직접 정의하는 이름(AUTOMATIC, TYPEDEF 등)은 중복 정의 검사(#error)에 걸리지 않도록 뺀다.
메모리 클래스는 메모리 배치(섹션)만 정하므로 비워도 변경 영향 분석 결과에는 영향이 없다.

사용 예)
  python memclass_shim.py "S:\\...\\26A10" "S:\\...\\26A22" -o C:\\Users\\admin\\Company\\cia_memclass.h
  python folder_impact.py ... --cpp "python C:\\Users\\admin\\Company\\cpp_filter.py -include C:\\Users\\admin\\Company\\cia_memclass.h"
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import folder_impact as fi

# 매크로 이름 → memclass/ptrclass 인자 위치 (0부터)
POSITIONS = {
    "VAR": (1,), "CONST": (1,), "FUNC": (1,),
    "P2VAR": (1, 2), "P2CONST": (1, 2), "CONSTP2VAR": (1, 2), "CONSTP2CONST": (1, 2),
    "P2FUNC": (1,), "CONSTP2FUNC": (1,),
    "FUNC_P2VAR": (1, 2), "FUNC_P2CONST": (1, 2),
}
CALL_RE = re.compile(r"\b(" + "|".join(sorted(POSITIONS, key=len, reverse=True)) + r")\s*\(")
IDENT_RE = re.compile(r"[A-Za-z_]\w*")
DEFINE_RE = re.compile(r"^\s*#\s*define\s+([A-Za-z_]\w*)", re.M)
C_WORDS = {"const", "volatile", "static", "extern", "void", "char", "short", "int", "long", "float", "double",
           "signed", "unsigned", "struct", "union", "enum", "inline", "register", "restrict"}


def split_args(text: str, start: int) -> tuple[list[str], int]:
    """start 는 '(' 다음 위치. 최상위 쉼표로 나눈 인자와 닫는 괄호 다음 위치."""
    depth, args, cur, i = 1, [], [], start
    while i < len(text) and depth:
        c = text[i]
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                break
        elif c == "," and depth == 1:
            args.append("".join(cur).strip())
            cur = []
            i += 1
            continue
        cur.append(c)
        i += 1
    args.append("".join(cur).strip())
    return args, i + 1


def strip_comments(text: str) -> str:
    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.S)
    return re.sub(r"//[^\n]*", " ", text)


def collect(roots: list[Path], exclude: list[str]) -> tuple[dict[str, str], set[str]]:
    """memclass 이름 → 처음 본 위치,  Compiler*.h 에서 정의하는 이름."""
    names: dict[str, str] = {}
    compiler_defined: set[str] = set()
    for root in roots:
        for rel, p in fi.list_c_files(root, exclude).items():
            try:
                raw = p.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if Path(rel).name.lower().startswith("compiler"):
                compiler_defined.update(DEFINE_RE.findall(raw))
            text = strip_comments(raw)
            for m in CALL_RE.finditer(text):
                if text[: m.start()].rstrip().endswith("define"):
                    continue
                line_start = text.rfind("\n", 0, m.start()) + 1
                if text[line_start:m.start()].lstrip().startswith("#"):
                    continue  # #define VAR(...) 같은 매크로 정의 자체
                args, _ = split_args(text, m.end())
                for pos in POSITIONS[m.group(1)]:
                    if pos < len(args) and IDENT_RE.fullmatch(args[pos]) and args[pos] not in C_WORDS:
                        names.setdefault(args[pos], rel)
    return names, compiler_defined


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="AUTOSAR 메모리 클래스 이름을 빈 매크로로 정의하는 헤더 생성")
    ap.add_argument("dirs", nargs="+", help="프로젝트 폴더 (변경 전, 변경 후)")
    ap.add_argument("-o", "--out", default="cia_memclass.h", help="만들 헤더 파일 (기본 cia_memclass.h)")
    ap.add_argument("--exclude", action="append", default=[], metavar="GLOB", help="제외할 파일·폴더 패턴")
    ap.add_argument("--keep", action="append", default=[], metavar="NAME", help="정의하지 않을 이름 (직접 정의되는 것)")
    args = ap.parse_args(argv)

    roots = [Path(d).resolve() for d in args.dirs]
    for r in roots:
        if not r.is_dir():
            print(f"오류: 폴더가 아닙니다: {r}", file=sys.stderr)
            return 1
    names, compiler_defined = collect(roots, args.exclude)
    skip = compiler_defined | set(args.keep) | set(POSITIONS)
    use = sorted(n for n in names if n not in skip)
    out = Path(args.out).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    lines = ["/* memclass_shim.py 가 만든 파일: AUTOSAR 메모리 클래스 이름을 빈 값으로 정의 (변경 영향 분석용) */",
             "#ifndef CIA_MEMCLASS_SHIM_H", "#define CIA_MEMCLASS_SHIM_H"]
    for n in use:
        lines += [f"#ifndef {n}", f"#define {n}", "#endif"]
    lines += ["#endif", ""]
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"메모리 클래스 이름 {len(use)}개 정의 (Compiler*.h 에서 직접 정의하는 {len(names) - len(use)}개 제외)")
    print(f"생성: {out}")
    if " " in str(out):
        print("⚠ 경로에 공백이 있으면 --cpp 에 넣을 수 없습니다. 공백 없는 위치로 -o 를 지정하세요.")
    here = Path(__file__).resolve().parent
    print("\nfolder_impact.py 에 붙일 옵션:")
    print(f'  --cpp "python {here / "cpp_filter.py"} -include {out}"')
    return 0


if __name__ == "__main__":
    sys.exit(main())
