"""테스트용: 인라인 C 소스로 분석 그래프를 만든다."""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from vfia.config import AnalysisOptions, BuildConfig, VersionSpec  # noqa: E402
from vfia.graph_builder import build_graph  # noqa: E402
from vfia.project_indexer import ProjectIndexer  # noqa: E402


def make_project(files: dict, root: str = None) -> str:
    root = root or tempfile.mkdtemp(prefix="vfia_t_")
    for rel, text in files.items():
        p = os.path.join(root, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(text)
    return root


def graph_of(files: dict, name: str = "v", args=None):
    root = make_project(files)
    v = VersionSpec(name=name, root=root, sources=["**/*.c"], args=args or ["-I."])
    idx = ProjectIndexer(v, BuildConfig(), log=lambda *a: None).run()
    return build_graph(idx, AnalysisOptions(), log=lambda *a: None), idx
