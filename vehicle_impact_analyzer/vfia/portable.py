"""운영체제 차이(Windows / Linux)를 한곳에서 흡수하는 도우미.

분석 결과(노드 키, 보고서의 파일 경로, 캐시 해시)는 운영체제와 무관하게 같아야 하므로
프로젝트 안의 상대 경로는 항상 '/'로 구분해 저장한다. 실제 파일을 열 때만 native()로 바꾼다.

  * rel_posix   root 아래 경로 -> 'src/a.c' 형식(Windows는 대소문자·구분자 무시하고 비교)
  * native      'src/a.c' -> 운영체제 경로
  * split_command  컴파일 명령 문자열 -> 인자 목록(Windows 따옴표 규칙 / POSIX 셸 규칙)
  * run_text    하위 프로세스 출력을 UTF-8(실패 시 로캘 인코딩)로 해석. cp949 콘솔에서도 깨지지 않음
  * setup_console  인코딩할 수 없는 문자가 있어도 출력이 중단되지 않게 표준 출력 설정
"""
from __future__ import annotations

import locale
import ntpath
import os
import posixpath
import re
import shlex
import subprocess
import sys
from typing import List, Optional, Sequence

IS_WINDOWS = os.name == "nt"
EXE_SUFFIX = ".exe" if IS_WINDOWS else ""

_DRIVE_ABS = re.compile(r"^(?:[A-Za-z]:[\\/]|\\\\)")


def is_abs_any(p: str) -> bool:
    """현재 운영체제 기준 절대 경로이거나 Windows 절대 경로('C:\\..', '\\\\server\\..')인가."""
    return os.path.isabs(p) or bool(_DRIVE_ABS.match(p))


def basename_any(p: str) -> str:
    """'/'와 '\\'를 모두 구분자로 보는 basename(다른 OS에서 만든 빌드 로그·compile DB용)."""
    return re.split(r"[\\/]", p.rstrip("\\/"))[-1] if p else p


def to_posix(rel: str) -> str:
    return rel.replace("\\", "/") if IS_WINDOWS else rel


def rel_posix(path: str, root: str, pm=os.path) -> Optional[str]:
    """path가 root 아래(또는 root 자체)이면 '/'로 구분한 상대 경로, 아니면 None.

    Windows에서는 대소문자와 구분자 차이를 무시하고 비교하며, 드라이브가 달라도 예외 없이 None.
    pm은 시험용(ntpath/posixpath)이고 그 경우 path·root는 절대 경로여야 한다.
    """
    if not path:
        return None
    if pm is os.path:
        p = os.path.normpath(os.path.abspath(path))
        r = os.path.normpath(os.path.abspath(root))
    else:
        p, r = pm.normpath(path), pm.normpath(root)
    pc, rc = pm.normcase(p), pm.normcase(r)
    if pc == rc:
        return "."
    sep = "\\" if pm is ntpath else "/"
    rc_sep = rc if rc.endswith(sep) else rc + sep
    if not pc.startswith(rc_sep):
        return None
    rel = p[len(rc_sep):]
    return rel.replace("\\", "/") if pm is ntpath else rel


def rel_or_abs(path: str, root: str) -> str:
    """root 아래면 상대 경로('/'), 아니면 정규화한 절대 경로(보고용)."""
    r = rel_posix(path, root)
    return r if r is not None else to_posix(os.path.normpath(os.path.abspath(path)))


def native(root: str, rel: str) -> str:
    """'/'로 구분한 상대 경로를 root 기준 운영체제 경로로."""
    return os.path.join(root, *[x for x in rel.split("/") if x]) if rel and rel != "." else root


def same_path(a: str, b: str) -> bool:
    return os.path.normcase(os.path.normpath(os.path.abspath(a))) == \
        os.path.normcase(os.path.normpath(os.path.abspath(b)))


# ---------------------------------------------------------------------------
# 명령줄 분리
# ---------------------------------------------------------------------------
def split_windows(cmd: str) -> List[str]:
    """Windows 명령줄 규칙(CommandLineToArgvW)으로 분리. 역슬래시 경로를 그대로 보존한다."""
    args: List[str] = []
    buf: List[str] = []
    have = False
    in_q = False
    i, n = 0, len(cmd)
    while i < n:
        c = cmd[i]
        if c == "\\":
            j = i
            while j < n and cmd[j] == "\\":
                j += 1
            nb = j - i
            if j < n and cmd[j] == '"':
                buf.append("\\" * (nb // 2))
                have = True
                if nb % 2:
                    buf.append('"')
                    i = j + 1
                else:
                    i = j
                continue
            buf.append("\\" * nb)
            have = True
            i = j
            continue
        if c == '"':
            if in_q and i + 1 < n and cmd[i + 1] == '"':
                buf.append('"')
                i += 2
                continue
            in_q = not in_q
            have = True
            i += 1
            continue
        if c in " \t\r\n" and not in_q:
            if have:
                args.append("".join(buf))
                buf, have = [], False
            i += 1
            continue
        buf.append(c)
        have = True
        i += 1
    if have:
        args.append("".join(buf))
    return args


def looks_windows_command(cmd: str) -> bool:
    return bool(re.search(r'(?:^|[\s"=@])(?:[A-Za-z]:\\|\\\\)', cmd)) or bool(
        re.match(r'^\s*"?[^\s"]+\.exe\b', cmd, re.I))


def split_command(cmd: str, windows: Optional[bool] = None) -> List[str]:
    """컴파일 명령 문자열을 인자로 나눈다. windows=None이면 실행 OS와 명령 모양으로 판단."""
    if windows is None:
        windows = IS_WINDOWS or looks_windows_command(cmd)
    if windows:
        return split_windows(cmd)
    try:
        return shlex.split(cmd)
    except ValueError:
        return split_windows(cmd)


def decode_any(data: bytes) -> str:
    """인코딩을 모르는 바이트열 해석: BOM -> UTF-8 -> VFIA_INPUT_ENCODING -> 로캘 인코딩 -> cp949 -> cp1252.

    Windows 도구(메모장, 사내 툴, ECU 도구가 만든 A2L·빌드 로그)는 UTF-16, ANSI(cp949/cp1252)로도 저장한다.
    """
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16", errors="replace")
    if data.startswith(b"\xef\xbb\xbf"):
        return data[3:].decode("utf-8", errors="replace")
    encs = ["utf-8", os.environ.get("VFIA_INPUT_ENCODING"), locale.getpreferredencoding(False), "cp949"]
    for enc in encs:
        if not enc:
            continue
        try:
            return data.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("cp1252", errors="replace")


def read_text_any(path: str) -> str:
    """응답 파일·빌드 로그·설정처럼 인코딩을 모르는 텍스트 파일(decode_any 규칙)."""
    with open(path, "rb") as f:
        return decode_any(f.read())


# ---------------------------------------------------------------------------
# 하위 프로세스·콘솔
# ---------------------------------------------------------------------------
def _decode(b: Optional[bytes]) -> str:
    return decode_any(b) if b else ""


def run_text(cmd: Sequence[str], input: Optional[str] = None, timeout: Optional[float] = None,
             cwd: Optional[str] = None, env: Optional[dict] = None) -> subprocess.CompletedProcess:
    """subprocess.run(text=True)과 같지만 출력 해석을 UTF-8 우선으로 고정한다.

    text=True는 Windows에서 로캘 인코딩(cp949)으로 해석해 git·gcc의 UTF-8 출력이 깨지거나 예외가 난다.
    """
    p = subprocess.run(list(cmd), input=input.encode("utf-8") if input is not None else None,
                       capture_output=True, timeout=timeout, cwd=cwd, env=env)
    return subprocess.CompletedProcess(p.args, p.returncode, _decode(p.stdout), _decode(p.stderr))


def setup_console() -> None:
    """출력 장치가 표현할 수 없는 문자(∧, ≤ 등)가 있어도 예외 없이 '?'로 바꿔 계속 출력한다.

    Windows 콘솔 창은 유니코드를 그대로 표시하지만, 파일·파이프로 보내면 cp949로 인코딩되어
    UnicodeEncodeError가 날 수 있다.
    """
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(errors="replace")
        except Exception:
            pass


def reexec_with_fixed_hash_seed() -> None:
    """set·dict 순회 순서가 실행마다(그리고 운영체제마다) 달라지지 않도록 PYTHONHASHSEED=0으로 다시 실행한다.

    Python은 문자열 해시를 프로세스마다 무작위화하므로, 집합 순회 순서에 따라 SMT 질의 순서·증거 입력·
    Louvain 결과가 달라질 수 있다. 같은 입력에서 같은 결과를 내는 것(재현성)이 이 도구의 전제이므로
    스크립트 진입점에서 해시 시드를 고정한다. VFIA_NO_REEXEC=1이면 하지 않는다.
    """
    if os.environ.get("PYTHONHASHSEED") == "0" or os.environ.get("VFIA_NO_REEXEC") == "1":
        return
    env = dict(os.environ)
    env["PYTHONHASHSEED"] = "0"
    try:
        r = subprocess.run([sys.executable] + sys.argv, env=env)
    except KeyboardInterrupt:
        sys.exit(130)
    sys.exit(r.returncode)


def subprocess_env_utf8() -> dict:
    """자식 Python 프로세스가 UTF-8로 입출력하도록 하는 환경 변수(시험·하위 스크립트 실행용)."""
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    return env


__all__ = ["IS_WINDOWS", "EXE_SUFFIX", "is_abs_any", "basename_any", "to_posix", "rel_posix", "rel_or_abs",
           "native", "same_path", "split_windows", "split_command", "looks_windows_command", "read_text_any",
           "run_text", "setup_console", "subprocess_env_utf8", "reexec_with_fixed_hash_seed", "posixpath", "ntpath"]
