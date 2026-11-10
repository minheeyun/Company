"""공변경(co-change) 마이닝: 과거 변경 이력에서 함께 바뀌어 온 파일·함수 쌍 (Zimmermann et al., ROSE).

정적 분석으로 보이지 않는 결합(ARXML·캘리브레이션·설정 파일과 코드, 같은 요구사항을 구현하는 파일들)을
찾는 보조 근거이자, 평가 단계의 정답 근사·비교 기준선(baseline)으로 쓴다.

입력
  * git 저장소: history: {git: <저장소 경로>, subdir: <분석 루트의 저장소 내 경로>, max_commits: 500}
  * JSON:       history: {file: change_history.json}  형식 [{"id": "...", "files": [...], "functions": [...]}]
규칙 X -> Y: support = X와 Y가 함께 바뀐 커밋 수, confidence = support / X가 바뀐 커밋 수.
대규모 일괄 변경(포맷팅 등) 커밋은 max_files_per_commit으로 제외한다.
"""
from __future__ import annotations

import json
import os
import subprocess
from collections import Counter, defaultdict
from typing import Dict, Iterable, List, Optional, Set, Tuple

from . import portable as pt


class CoChange:
    def __init__(self, transactions: List[Set[str]], source: str):
        self.transactions = [t for t in transactions if t]
        self.source = source
        self.count: Counter = Counter()
        self.pair: Dict[str, Counter] = defaultdict(Counter)
        for t in self.transactions:
            for x in t:
                self.count[x] += 1
                for y in t:
                    if x != y:
                        self.pair[x][y] += 1

    def confidence(self, x: str, y: str) -> Tuple[float, int]:
        n = self.count.get(x, 0)
        s = self.pair.get(x, {}).get(y, 0)
        return (s / n if n else 0.0), s

    def rules_from(self, items: Iterable[str], min_support: int = 2, min_conf: float = 0.3, top: int = 10) -> List[dict]:
        """items가 바뀌었을 때 함께 바뀌었던 대상(ROSE식 추천)."""
        items = set(items)
        best: Dict[str, Tuple[float, int, str]] = {}
        for x in items:
            for y, s in self.pair.get(x, {}).items():
                if y in items or s < min_support:
                    continue
                c = s / self.count[x]
                if c >= min_conf and (y not in best or c > best[y][0]):
                    best[y] = (c, s, x)
        out = [{"entity": y, "confidence": round(c, 2), "support": s, "because_of": x}
               for y, (c, s, x) in best.items()]
        out.sort(key=lambda r: (-r["confidence"], -r["support"], r["entity"]))
        return out[:top]

    def summary(self) -> dict:
        return {"source": self.source, "transactions": len(self.transactions), "entities": len(self.count)}


def _git_transactions(repo: str, subdir: str = "", max_commits: int = 500, max_files: int = 30) -> List[Set[str]]:
    try:
        out = pt.run_text(["git", "-c", "core.quotepath=off", "-C", repo, "log", f"-n{max_commits}", "--no-merges",
                           "--name-only", "--pretty=format:@@%H"], timeout=120).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    txs: List[Set[str]] = []
    cur: Set[str] = set()
    subdir = (subdir or "").replace("\\", "/")
    prefix = subdir.strip("/") + "/" if subdir else ""
    for line in out.splitlines():
        if line.startswith("@@"):
            if cur:
                txs.append(cur)
            cur = set()
            continue
        line = line.strip()
        if not line or (prefix and not line.startswith(prefix)):
            continue
        cur.add(line[len(prefix):])
    if cur:
        txs.append(cur)
    return [t for t in txs if len(t) <= max_files]


def load_cochange(history: Optional[dict]) -> Optional[CoChange]:
    if not history:
        return None
    max_files = int(history.get("max_files_per_commit", 30))
    if history.get("file") and os.path.isfile(history["file"]):
        data = json.loads(pt.read_text_any(history["file"]))
        txs = []
        for c in data:
            t = {str(x).replace("\\", "/") for x in c.get("files", [])}
            t |= {f"fn:{x}" for x in c.get("functions", [])}
            if len(t) <= max_files * 2:
                txs.append(t)
        return CoChange(txs, os.path.basename(history["file"]))
    if history.get("git"):
        txs = _git_transactions(history["git"], history.get("subdir", ""), int(history.get("max_commits", 500)),
                                max_files)
        return CoChange(txs, f"git:{history['git']}") if txs else None
    return None
