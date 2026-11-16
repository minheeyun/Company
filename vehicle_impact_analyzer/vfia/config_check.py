"""설정 점검: 분석을 시작하기 전에 경로·compile DB·차량 모델 파일을 확인하고, 고치는 방법을 알려 준다.

예제 설정을 복사한 뒤 경로를 고치지 않으면 '번역 단위 0개'로 조용히 진행되거나 중간에 파일을 못 찾아 멈춘다.
여기서 미리 잡아 한 번에 보여 준다.
  오류  분석할 수 없음(없는 폴더·파일, compile DB의 소스가 root 밖, 차량 모델 파일 없음 ...)
  주의  분석은 되지만 결과가 부정확하거나 일부 기능이 빠짐(compile DB 없음, 기능 정의 없음, 예제 값 ...)
"""
from __future__ import annotations

import json
import os
from typing import List, Optional, Tuple

from . import portable as pt
from .config import AnalysisConfig, load_structured


class ConfigError(Exception):
    """설정 때문에 분석을 진행할 수 없음(메시지에 고치는 방법 포함)."""


def _db_files(path: str) -> Tuple[List[str], Optional[str]]:
    """compile DB의 소스 파일 절대 경로 목록(읽기 실패 시 오류 메시지)."""
    try:
        db = json.loads(pt.read_text_any(path))
    except (OSError, ValueError) as e:
        return [], f"compile DB를 읽을 수 없음: {path} ({e})"
    if not isinstance(db, list):
        return [], f"compile DB 형식이 아님(JSON 배열이어야 함): {path}"
    base = os.path.dirname(os.path.abspath(path))
    out = []
    for e in db:
        if not isinstance(e, dict) or "file" not in e:
            continue
        d = e.get("directory") or base
        if not os.path.isabs(d):
            d = os.path.normpath(os.path.join(base, d))
        f = e["file"]
        out.append(os.path.normpath(f if os.path.isabs(f) else os.path.join(d, f)))
    return out, None


def _check_sources(cfg: AnalysisConfig, name: str, v, cfg_name: str, errors: List[str], warns: List[str]) -> None:
    if not os.path.isdir(v.root):
        errors.append(f"[{name}] 소스 폴더가 없음: {v.root}\n"
                      f"    → {cfg_name}의 versions.{name}.root에 그 버전 소스의 최상위 폴더를 적으세요"
                      f"(예: S:/project/26A10). 상대 경로는 설정 파일 폴더 기준입니다.")
        return
    if v.compile_commands:
        if not os.path.isfile(v.compile_commands):
            if cfg.build.build_log and os.path.isfile(cfg.build.build_log):
                warns.append(f"[{name}] compile DB 파일이 없어 build.build_log({cfg.build.build_log})에서 추출합니다. "
                             "두 버전이 같은 로그를 쓰게 되므로 버전별로 tools/make_compile_db.py를 실행하는 편이 정확합니다.")
            else:
                errors.append(f"[{name}] compile DB 파일이 없음: {v.compile_commands}\n"
                              f"    → tools/make_compile_db.py로 만들거나(빌드 로그 → compile_commands.json), "
                              f"versions.{name}.compile_commands 경로를 고치세요.")
                return
        else:
            files, err = _db_files(v.compile_commands)
            if err:
                errors.append(f"[{name}] {err}")
                return
            inside = [f for f in files if pt.rel_posix(f, v.root) is not None]
            if not files:
                errors.append(f"[{name}] compile DB에 컴파일 명령이 없음: {v.compile_commands}")
            elif not inside:
                errors.append(f"[{name}] compile DB의 소스 {len(files)}개가 모두 root 밖에 있음(예: {files[0]}).\n"
                              f"    → versions.{name}.root({v.root})를 소스 최상위 폴더로 고치세요. "
                              f"compile DB를 만들 때 쓴 --root와 같아야 합니다.")
            else:
                missing = [f for f in inside if not os.path.isfile(f)]
                if len(missing) == len(inside):
                    errors.append(f"[{name}] compile DB의 소스 파일이 디스크에 없음(예: {missing[0]}). "
                                  "소스를 옮겼다면 옮긴 위치에서 빌드 로그·compile DB를 다시 만드세요.")
                elif missing:
                    warns.append(f"[{name}] compile DB의 소스 {len(missing)}개가 디스크에 없음(예: {missing[0]}) — 빠진 채 분석")
                if len(inside) < len(files):
                    warns.append(f"[{name}] root 밖 소스 {len(files) - len(inside)}개는 분석에서 제외(예: "
                                 f"{next(f for f in files if pt.rel_posix(f, v.root) is None)})")
    else:
        srcs = v.sources or ["**/*.c"]
        import glob
        n = sum(len(glob.glob(os.path.join(v.root, p), recursive=True)) for p in srcs)
        if n == 0:
            errors.append(f"[{name}] compile DB가 지정되지 않았고 {v.root} 아래에 .c 파일도 없음.")
        else:
            warns.append(f"[{name}] compile DB가 없어 .c {n}개를 같은 옵션({' '.join(v.args) or '없음'})으로 파싱합니다"
                         "(include 경로·매크로가 실제 빌드와 다를 수 있음). tools/make_compile_db.py 사용 권장.")


def check_config(cfg: AnalysisConfig, versions: Optional[List[str]] = None) -> Tuple[List[str], List[str]]:
    """(오류 목록, 주의 목록). versions를 주면 그 버전만 점검."""
    errors: List[str] = []
    warns: List[str] = []
    cfg_name = os.path.basename(cfg.path)
    names = versions or list(cfg.versions)
    seen_vm = {}   # 여러 버전이 같이 쓰는 차량 모델 파일은 한 번만 점검(기능 수는 재사용)
    if not cfg.versions:
        errors.append(f"{cfg_name}에 versions가 없음: versions.before / versions.after에 변경 전·후 소스 폴더를 지정하세요.")
    for name in names:
        if name not in cfg.versions:
            errors.append(f"{cfg_name}에 '{name}' 버전이 없음(있는 버전: {', '.join(cfg.versions) or '없음'}). "
                          f"--before/--after 이름이나 versions 아래 이름을 맞추세요.")
            continue
        v = cfg.versions[name]
        _check_sources(cfg, name, v, cfg_name, errors, warns)
        # 차량 모델
        vm_files = cfg.vehicle_model_for(name) or []
        if not vm_files:
            warns.append(f"[{name}] vehicle_model이 없음: 기능·관찰 대상이 없어 '어떤 기능에 영향'은 판정하지 못하고 "
                         "영향 범위와 그래프만 만듭니다.")
        n_feat = 0
        for p in vm_files:
            if p in seen_vm:
                n_feat += seen_vm[p]
                continue
            seen_vm[p] = 0
            if not os.path.isfile(p):
                errors.append(f"[{name}] 차량 모델 파일이 없음: {p}\n"
                              f"    → versions.{name}.vehicle_model(또는 vehicle_model)에서 이 파일을 빼거나 만드세요"
                              "(예제의 execution_v1/v2.yaml은 예제 전용).")
                continue
            try:
                raw = load_structured(p) or {}
            except Exception as e:  # YAML 문법 오류 등
                errors.append(f"[{name}] 차량 모델 파일을 읽을 수 없음: {p} ({e})")
                continue
            seen_vm[p] = len(raw.get("features") or [])
            n_feat += seen_vm[p]
            base = os.path.dirname(os.path.abspath(p))
            for key in ("a2l", "arxml"):
                for x in raw.get(key) or []:
                    xp = x if os.path.isabs(x) else os.path.join(base, x)
                    if not os.path.isfile(xp):
                        errors.append(f"[{name}] {os.path.basename(p)}의 {key} 파일이 없음: {xp} → 목록에서 빼거나 경로를 고치세요.")
        if vm_files and n_feat == 0:
            warns.append(f"[{name}] 차량 모델에 features가 없음: 기능 영향 판정 없이 영향 범위와 그래프만 만듭니다.")
    # 선택 입력
    if cfg.review_file and not os.path.isfile(cfg.review_file):
        warns.append(f"review_file이 없음(무시): {cfg.review_file}")
    for k, p in (cfg.traces or {}).items():
        if p and not os.path.isfile(p):
            warns.append(f"traces.{k} 파일이 없음(무시): {p}")
    hist = cfg.history or {}
    if hist.get("file") and not os.path.isfile(hist["file"]):
        warns.append(f"history.file이 없음(공변경 근거 생략): {hist['file']}")
    if hist.get("git") and not os.path.isdir(hist["git"]):
        warns.append(f"history.git 폴더가 없음(공변경 근거 생략): {hist['git']}")
    if cfg.ground_truth and not os.path.isfile(cfg.ground_truth):
        warns.append(f"ground_truth가 없음: 평가(evaluate.py) 단계는 건너뜁니다 ({cfg.ground_truth})")
    if "VARIANT_SPORT" in (cfg.build.name or "") or cfg.project == "torque_ecu":
        if not os.path.abspath(cfg.path).replace("\\", "/").endswith("examples/torque_ecu/analysis.yaml"):
            warns.append("예제 설정 값(project: torque_ecu, build.name ...)이 남아 있음: 보고서에 그대로 표시됩니다. "
                         "tools/new_project.py로 새 설정을 만들면 깨끗하게 시작할 수 있습니다.")
    return _merge_versions(errors), _merge_versions(warns)


def _merge_versions(msgs: List[str]) -> List[str]:
    """'[before] X'와 '[after] X'처럼 버전만 다른 같은 메시지는 '[before, after] X' 하나로."""
    import re
    out: List[str] = []
    tags: dict = {}
    for m in msgs:
        mm = re.match(r"^\[([^\]]+)\] (.*)$", m, re.S)
        if not mm:
            out.append(m)
            continue
        body = mm.group(2)
        if body in tags:
            tags[body].append(mm.group(1))
        else:
            tags[body] = [mm.group(1)]
            out.append(body)
    return [f"[{', '.join(tags[m])}] {m}" if m in tags else m for m in out]


def report(errors: List[str], warns: List[str], log=print) -> None:
    for w in warns:
        log(f"[설정 주의] {w}")
    if errors:
        log("")
        log(f"설정 오류 {len(errors)}건 — 분석을 시작하지 않았습니다:")
        for i, e in enumerate(errors, 1):
            log(f"  {i}. {e}")


def ensure_config_ok(cfg: AnalysisConfig, versions: Optional[List[str]] = None, log=print) -> bool:
    errors, warns = check_config(cfg, versions)
    report(errors, warns, log)
    return not errors


def load_config_checked(path: str):
    """설정 파일을 읽는다. 없거나 문법 오류면 ConfigError(고치는 방법 포함)."""
    from .config import load_config
    if not os.path.isfile(path):
        raise ConfigError(f"설정 파일이 없음: {os.path.abspath(path)}\n"
                          "    → 경로를 확인하거나 tools/new_project.py로 새 설정을 만드세요.")
    try:
        return load_config(path)
    except KeyError as e:
        raise ConfigError(f"설정 파일 {path}에 필요한 항목이 없음: {e} (예: versions.<버전>.root)") from e
    except ValueError as e:
        raise ConfigError(f"설정 파일 {path}을 읽을 수 없음: {e}") from e
    except Exception as e:
        if type(e).__module__.startswith("yaml"):
            raise ConfigError(f"설정 파일 {path}의 YAML 문법 오류: {e}\n"
                              "    → 들여쓰기(공백 2칸)와 '키: 값' 형식, 경로의 \\ 대신 / 사용을 확인하세요.") from e
        raise


def gate(cfg: AnalysisConfig, versions: Optional[List[str]] = None, quiet: bool = False) -> bool:
    """실행 전 점검. 오류가 있으면 출력하고 False. 주의는 quiet이거나 상위(run_all)가 이미 보였으면 생략."""
    import sys
    errors, warns = check_config(cfg, versions)
    if not quiet and os.environ.get("VFIA_CONFIG_CHECKED") != "1":
        for w in warns:
            print(f"[설정 주의] {w}")
    if errors:
        print(f"\n설정 오류 {len(errors)}건 — 분석을 시작하지 않았습니다 ({cfg.path}):", file=sys.stderr)
        for i, e in enumerate(errors, 1):
            print(f"  {i}. {e}", file=sys.stderr)
        return False
    return True


def run_main(main) -> None:
    """스크립트 진입점: 설정 문제(ConfigError)는 Traceback 대신 안내문으로 끝낸다."""
    import sys
    try:
        sys.exit(main())
    except ConfigError as e:
        print(f"\n설정 오류: {e}", file=sys.stderr)
        sys.exit(2)
