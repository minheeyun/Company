"""c_impact 테스트 공용 도우미: 임시 C 프로젝트 작성, 분석 실행."""

from __future__ import annotations

import os
import sys
import tempfile
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DEMO = ROOT / "examples" / "impact_demo"

try:
    from c_impact.clang_frontend import require_clang

    require_clang()
    HAVE_CLANG = True
except Exception:  # libclang 미설치 환경
    HAVE_CLANG = False

SKIP_MSG = "libclang 이 필요합니다 (pip install libclang)"


def write_tree(root, files: dict):
    for rel, text in files.items():
        p = Path(root) / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(textwrap.dedent(text).lstrip("\n"), encoding="utf-8")
    return str(root)


class Fixture:
    """base/curr 두 트리를 임시 디렉터리에 만든다."""

    def __init__(self, base: dict, curr: dict | None = None):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = write_tree(os.path.join(self.tmp.name, "base"), base)
        merged = dict(base)
        merged.update(curr or {})
        self.curr = write_tree(os.path.join(self.tmp.name, "curr"), merged)

    def close(self):
        self.tmp.cleanup()


def load(root, **kw):
    from c_impact.project import load_project
    return load_project(root, os.path.basename(root), **kw)


def analyze(base, curr, config=None, **opt):
    from c_impact.pipeline import Options, analyze as run
    return run(base, curr, config, Options(**opt))


def fn_levels(result, change_id=None):
    """{함수명: 수준} (change_id 가 있으면 해당 변경점만)"""
    if change_id:
        pc = next(p for p in result["impact"]["per_change"] if p["change"] == change_id)
        return {k.split("::")[-1]: v["level"] for k, v in pc["functions"].items()}
    return {f["name"]: f["level"] for f in result["impact"]["functions"]}
