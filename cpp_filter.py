#!/usr/bin/env python3
"""gcc -E 로 전처리한 뒤, C 파서(pycparser)가 해석하지 못하는 컴파일러 확장을 지워서 내보내는 전처리기 대리 실행기.

folder_impact.py / auto_include.py / change_impact.py 의 --cpp 로 지정해서 쓴다 (기존 코드는 바꾸지 않는다).
지운 자리는 같은 수의 줄바꿈으로 채우므로 보고서의 줄 번호는 원본과 같다.

  - 인라인 어셈블리: __asm volatile ("..." ::: "memory"), asm goto (...), __asm__ __volatile__ (...)
    (괄호가 바로 붙은 __asm("...") 은 기본 매크로로도 지워지지만, volatile 이 끼면 매크로로는 못 지운다)
  - #pragma 줄과 _Pragma("...") : 구조체 안 등 파서가 받지 않는 위치에 나오는 경우가 있다 (분석과 무관)
  - 문자 인코딩 통일: 소스가 UTF-8·CP949 가 섞여 있어도, 호출한 Python 이 읽는 인코딩(로캘 기본값)으로 맞춘다
    (문자열·주석 안의 한글만 해당하므로 분석 결과에는 영향이 없다)

사용 예 (Windows, 경로에 공백이 없어야 함):
  python folder_impact.py 변경전 변경후 -o result --cpp "python C:\\Users\\admin\\Company\\cpp_filter.py" ...

gcc 대신 다른 전처리기를 쓰려면 환경 변수 CIA_GCC 로 지정한다 (예: set CIA_GCC=clang).
"""

from __future__ import annotations

import locale
import os
import re
import subprocess
import sys

ASM_RE = re.compile(r"\b(?:__asm__|__asm|asm)\b(?:\s*\b(?:volatile|__volatile__|goto|inline)\b)*\s*\(")
PRAGMA_OP_RE = re.compile(r"\b_Pragma\s*\(")
PRAGMA_LINE_RE = re.compile(r"^[ \t]*#[ \t]*pragma\b.*$", re.M)


def remove_balanced(text: str, start_re: re.Pattern) -> str:
    """start_re(여는 괄호까지)로 시작해 짝이 맞는 닫는 괄호까지를 지우고, 그 안의 줄바꿈 수만 남긴다."""
    out: list[str] = []
    i = 0
    n = len(text)
    while True:
        m = start_re.search(text, i)
        if m is None:
            out.append(text[i:])
            break
        k = m.end()
        depth = 1
        quote = None
        while k < n and depth:
            c = text[k]
            if quote:
                if c == "\\":
                    k += 2
                    continue
                if c == quote:
                    quote = None
            elif c in "\"'":
                quote = c
            elif c == "(":
                depth += 1
            elif c == ")":
                depth -= 1
            k += 1
        out.append(text[i:m.start()])
        out.append("\n" * text.count("\n", m.start(), k))
        i = k
    return "".join(out)


def clean(text: str) -> str:
    text = PRAGMA_LINE_RE.sub("", text)
    text = remove_balanced(text, PRAGMA_OP_RE)
    text = remove_balanced(text, ASM_RE)
    return text


def main(argv: list[str]) -> int:
    gcc = os.environ.get("CIA_GCC", "gcc")
    args = [a for a in argv if a != "-E"]
    try:
        r = subprocess.run([gcc, "-E"] + args, capture_output=True)
    except OSError as e:
        sys.stderr.write(f"cpp_filter: {gcc} 실행 실패: {e}\n")
        return 1
    sys.stderr.buffer.write(r.stderr)
    if r.returncode != 0:
        return r.returncode
    target = locale.getpreferredencoding(False) or "utf-8"
    text = "".join(_decode_line(line) for line in r.stdout.splitlines(keepends=True))
    sys.stdout.buffer.write(clean(text).encode(target, errors="replace"))
    return 0


def _decode_line(b: bytes) -> str:
    for enc in ("utf-8", "cp949"):
        try:
            return b.decode(enc)
        except UnicodeDecodeError:
            pass
    return b.decode("latin-1")


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
