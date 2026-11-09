"""공용 분석 그래프 모델.

기능 분류기와 변경 영향 분석기는 모두 이 AnalysisGraph를 입력으로 사용한다.
그래프 하나는 '하나의 소스 버전 + 하나의 빌드 설정'을 표현한다. 변경 전/후
그래프는 절대 합치지 않고, change_detector가 만든 대응 정보로만 연결한다.
"""
from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from typing import Dict, Iterable, Iterator, List, Optional, Set, Tuple

import networkx as nx


class NK:
    """노드 종류(Node Kind)."""
    FILE = "file"
    FUNCTION = "function"
    GLOBAL = "global"
    LOCAL = "local"
    PARAM = "param"
    RECORD = "record"
    FIELD = "field"
    TYPEDEF = "typedef"
    ENUM = "enum"
    ENUM_CONST = "enum_const"
    MACRO = "macro"
    # 함수 내부(PDG/SDG) 노드
    ENTRY = "entry"
    EXIT = "exit"
    STMT = "stmt"
    FORMAL_IN = "formal_in"
    FORMAL_OUT = "formal_out"
    CALL_SITE = "call_site"
    ACTUAL_IN = "actual_in"
    ACTUAL_OUT = "actual_out"
    LOCATION = "location"          # 전역 메모리 위치(필드 단위)
    # 차량 기능 연결 정보
    SIGNAL = "signal"
    TASK = "task"                  # task / isr / runnable / callback 실행 문맥
    FEATURE = "feature"
    REQUIREMENT = "requirement"
    OBSERVATION = "observation"
    CALIBRATION = "calibration"


class EK:
    """간선 종류(Edge Kind)."""
    # 구조
    CONTAINS = "contains"          # file->symbol, function->stmt
    INCLUDES = "includes"          # file->file
    CONFIG_DEPENDS = "config_depends"  # file->macro (#if 조건에 쓰인 매크로)
    # 함수/심벌 수준 관계(기능 분류용 요약 관계)
    CALLS = "calls"                # function->function
    READS = "reads"                # function->global
    WRITES = "writes"              # function->global
    USES_TYPE = "uses_type"        # function->record/typedef
    USES_FIELD = "uses_field"      # stmt/function->field
    USES_MACRO = "uses_macro"      # stmt/function/global->macro
    USES_ENUM = "uses_enum"        # stmt/function->enum_const
    ADDR_TAKEN = "addr_taken"      # function->function(주소가 사용된 함수)
    HAS_FIELD = "has_field"        # record->field
    # 실행 흐름
    CFG = "cfg"
    # 의존관계(SDG)
    DATA = "data"                  # 정의 -> 사용
    CONTROL = "control"            # 조건 -> 실행 여부가 결정되는 문장
    CALL = "call"                  # call_site -> callee entry
    PARAM_IN = "param_in"          # actual_in -> formal_in
    PARAM_OUT = "param_out"        # formal_out -> actual_out
    SUMMARY = "summary"            # actual_in -> actual_out (피호출 함수 요약)
    GLOBAL_WRITE = "global_write"  # stmt -> location
    GLOBAL_READ = "global_read"    # location -> stmt
    INTERFERENCE = "interference"  # 다른 실행 문맥(태스크/ISR)의 쓰기 stmt -> 읽기 stmt (간섭 의존)
    # 차량 기능 연결
    SIGNAL_WRITE = "signal_write"  # node -> signal
    SIGNAL_READ = "signal_read"    # signal -> node
    ACTIVATES = "activates"        # task -> function
    RUNS_IN = "runs_in"            # function -> task
    REQUIRES = "requires"          # feature -> requirement
    OBSERVES = "observes"          # observation -> 그래프 앵커
    HAS_OBSERVATION = "has_observation"  # feature -> observation
    CALIBRATES = "calibrates"      # calibration -> location


# 슬라이싱에 사용하는 간선 묶음. 목적에 맞는 간선과 방향만 사용한다.
DEPENDENCE_EDGES = frozenset({
    EK.DATA, EK.CONTROL, EK.CALL, EK.PARAM_IN, EK.PARAM_OUT, EK.SUMMARY,
    EK.GLOBAL_WRITE, EK.GLOBAL_READ, EK.SIGNAL_WRITE, EK.SIGNAL_READ, EK.CALIBRATES, EK.INTERFERENCE,
})
# 호출 문맥을 '내려가는' 간선(호출자 -> 피호출자)
DESCEND_EDGES = frozenset({EK.CALL, EK.PARAM_IN})
# 호출 문맥을 '올라가는' 간선(피호출자 -> 호출자)
ASCEND_EDGES = frozenset({EK.PARAM_OUT})

# 결과 상태(보고서 공통)
class Status:
    STATIC_CANDIDATE = "정적 영향 후보"
    MODEL_DIFF = "모델에서 동작 차이 확인"
    MODEL_NO_DIFF = "모델에서 동작 차이 미발견(검증 범위 내)"
    EXEC_DIFF = "실행으로 차이 확인"
    EXEC_NO_DIFF = "실행 비교에서 차이 미발견(시험 입력 범위 내)"
    INCONCLUSIVE = "분석 불완전으로 판정 유보"
    NO_PATH = "현재 분석 범위에서 영향 경로 미발견"


# 메모리 위치 문자열: "<심벌 키>|필드|하위필드". 키에는 파일 경로의 '.'이 들어갈 수 있어
# 필드 구분자로 '|'를 사용한다. 배열 원소는 배열 전체 위치로 합친다(array collapse).
FIELD_SEP = "|"


def loc_root(loc: str) -> str:
    return loc.split(FIELD_SEP, 1)[0]


def loc_field(loc: str, field: str) -> str:
    return f"{loc}{FIELD_SEP}{field}"


def loc_may_alias(a: str, b: str) -> bool:
    """필드 경로 기반 별칭 판정. 's'와 's|f'는 겹치고, 's|f'와 's|g'는 겹치지 않는다."""
    if a == b:
        return True
    return a.startswith(b + FIELD_SEP) or b.startswith(a + FIELD_SEP)


def loc_display(loc: str) -> str:
    """'gv:src/a.c:g_state|mode' -> 'g_state.mode'"""
    root, _, rest = loc.partition(FIELD_SEP)
    name = root.split(":")[-1]
    return name + ("." + rest.replace(FIELD_SEP, ".") if rest else "")


class AnalysisGraph:
    """networkx MultiDiGraph 위의 얇은 래퍼 + SQLite 저장."""

    def __init__(self, meta: Optional[dict] = None):
        self.g = nx.MultiDiGraph()
        self.meta: dict = meta or {}
        self.asts: Dict[str, dict] = {}       # unit key -> generic AST
        self.diagnostics: List[dict] = []
        self._by_kind: Dict[str, Set[str]] = defaultdict(set)
        self._edge_keys: Set[Tuple[str, str, str]] = set()

    # ---- 노드/간선 ----
    def add_node(self, nid: str, kind: str, **attrs) -> str:
        if nid in self.g:
            data = self.g.nodes[nid]
            if data.get("kind") is None:      # 간선 추가로 암묵적으로 만들어진 노드
                data["kind"] = kind
                self._by_kind[kind].add(nid)
            for k, v in attrs.items():
                if v is not None:
                    data[k] = v
            if data.get("kind") != kind and kind:
                # 같은 ID가 다른 종류로 재정의되면 진단으로 남긴다.
                self.diag("warning", "node_kind_conflict", f"{nid}: {data.get('kind')} vs {kind}")
            return nid
        self.g.add_node(nid, kind=kind, **{k: v for k, v in attrs.items() if v is not None})
        self._by_kind[kind].add(nid)
        return nid

    def add_edge(self, src: str, dst: str, kind: str, label: str = "", **attrs) -> None:
        key = f"{kind}|{label}"
        sig = (src, dst, key)
        if sig in self._edge_keys:
            return
        self._edge_keys.add(sig)
        self.g.add_edge(src, dst, key=key, kind=kind, label=label, **attrs)

    def remove_edge(self, src: str, dst: str, kind: str, label: str = "") -> bool:
        key = f"{kind}|{label}"
        if (src, dst, key) not in self._edge_keys:
            return False
        self._edge_keys.discard((src, dst, key))
        self.g.remove_edge(src, dst, key=key)
        return True

    def has_node(self, nid: str) -> bool:
        return nid in self.g

    def node(self, nid: str) -> dict:
        return self.g.nodes[nid]

    def kind(self, nid: str) -> Optional[str]:
        return self.g.nodes[nid].get("kind") if nid in self.g else None

    def nodes_of(self, *kinds: str) -> List[str]:
        out: List[str] = []
        for k in kinds:
            out.extend(sorted(self._by_kind.get(k, ())))
        return out

    def out_edges(self, nid: str, kinds: Optional[Iterable[str]] = None) -> Iterator[Tuple[str, str, dict]]:
        ks = set(kinds) if kinds is not None else None
        for _, dst, data in self.g.out_edges(nid, data=True):
            if ks is None or data["kind"] in ks:
                yield nid, dst, data

    def in_edges(self, nid: str, kinds: Optional[Iterable[str]] = None) -> Iterator[Tuple[str, str, dict]]:
        ks = set(kinds) if kinds is not None else None
        for src, _, data in self.g.in_edges(nid, data=True):
            if ks is None or data["kind"] in ks:
                yield src, nid, data

    def succ(self, nid: str, kinds: Optional[Iterable[str]] = None) -> List[str]:
        return [d for _, d, _ in self.out_edges(nid, kinds)]

    def pred(self, nid: str, kinds: Optional[Iterable[str]] = None) -> List[str]:
        return [s for s, _, _ in self.in_edges(nid, kinds)]

    def diag(self, severity: str, code: str, message: str, **extra) -> None:
        self.diagnostics.append({"severity": severity, "code": code, "message": message, **extra})

    # ---- 조회 보조 ----
    def functions_by_name(self, name: str) -> List[str]:
        return [n for n in self.nodes_of(NK.FUNCTION) if self.g.nodes[n].get("name") == name]

    def globals_by_name(self, name: str) -> List[str]:
        return [n for n in self.nodes_of(NK.GLOBAL) if self.g.nodes[n].get("name") == name]

    def func_of(self, nid: str) -> Optional[str]:
        return self.g.nodes[nid].get("func") if nid in self.g else None

    def describe(self, nid: str) -> str:
        if nid not in self.g:
            return nid
        d = self.g.nodes[nid]
        k = d.get("kind")
        if k in (NK.STMT, NK.CALL_SITE, NK.ACTUAL_IN, NK.ACTUAL_OUT):
            fn = d.get("func", "")
            return f"{d.get('file', '?')}:{d.get('line', '?')} [{fn.split(':')[-1]}] {d.get('text', '')}"
        if k in (NK.FORMAL_IN, NK.FORMAL_OUT, NK.ENTRY, NK.EXIT):
            return f"{d.get('func', '').split(':')[-1]}::{d.get('label', k)}"
        return f"{k}:{d.get('name', nid)}"

    def stats(self) -> dict:
        ek: Dict[str, int] = defaultdict(int)
        for _, _, data in self.g.edges(data=True):
            ek[data["kind"]] += 1
        return {
            "nodes": {k: len(v) for k, v in sorted(self._by_kind.items())},
            "edges": dict(sorted(ek.items())),
        }

    # ---- 저장/로딩 ----
    def save(self, path: str) -> None:
        con = sqlite3.connect(path)
        cur = con.cursor()
        for t in ("meta", "nodes", "edges", "asts", "diags"):
            cur.execute(f"DROP TABLE IF EXISTS {t}")
        cur.execute("CREATE TABLE meta (k TEXT PRIMARY KEY, v TEXT)")
        cur.execute("CREATE TABLE nodes (id TEXT PRIMARY KEY, kind TEXT, attrs TEXT)")
        cur.execute("CREATE TABLE edges (src TEXT, dst TEXT, kind TEXT, label TEXT, attrs TEXT)")
        cur.execute("CREATE TABLE asts (unit TEXT PRIMARY KEY, data TEXT)")
        cur.execute("CREATE TABLE diags (data TEXT)")
        cur.executemany("INSERT INTO meta VALUES (?, ?)",
                        [(k, json.dumps(v, ensure_ascii=False)) for k, v in self.meta.items()])
        rows = []
        for nid, data in self.g.nodes(data=True):
            attrs = {k: v for k, v in data.items() if k != "kind"}
            rows.append((nid, data.get("kind"), json.dumps(attrs, ensure_ascii=False)))
        cur.executemany("INSERT INTO nodes VALUES (?, ?, ?)", rows)
        erows = []
        for s, d, data in self.g.edges(data=True):
            attrs = {k: v for k, v in data.items() if k not in ("kind", "label")}
            erows.append((s, d, data["kind"], data.get("label", ""), json.dumps(attrs, ensure_ascii=False)))
        cur.executemany("INSERT INTO edges VALUES (?, ?, ?, ?, ?)", erows)
        cur.executemany("INSERT INTO asts VALUES (?, ?)",
                        [(k, json.dumps(v, ensure_ascii=False)) for k, v in self.asts.items()])
        cur.executemany("INSERT INTO diags VALUES (?)",
                        [(json.dumps(d, ensure_ascii=False),) for d in self.diagnostics])
        cur.execute("CREATE INDEX edges_src ON edges(src)")
        cur.execute("CREATE INDEX edges_dst ON edges(dst)")
        con.commit()
        con.close()

    @classmethod
    def load(cls, path: str) -> "AnalysisGraph":
        con = sqlite3.connect(path)
        cur = con.cursor()
        g = cls()
        for k, v in cur.execute("SELECT k, v FROM meta"):
            g.meta[k] = json.loads(v)
        for nid, kind, attrs in cur.execute("SELECT id, kind, attrs FROM nodes"):
            g.g.add_node(nid, kind=kind, **json.loads(attrs))
            g._by_kind[kind].add(nid)
        for s, d, kind, label, attrs in cur.execute("SELECT src, dst, kind, label, attrs FROM edges"):
            g.add_edge(s, d, kind, label, **json.loads(attrs))
        for unit, data in cur.execute("SELECT unit, data FROM asts"):
            g.asts[unit] = json.loads(data)
        for (data,) in cur.execute("SELECT data FROM diags"):
            g.diagnostics.append(json.loads(data))
        con.close()
        return g

    def export_graphml(self, path: str, kinds: Optional[Set[str]] = None) -> None:
        """시각화 도구(Gephi, yEd)용 내보내기. 속성은 문자열로 평탄화한다."""
        h = nx.MultiDiGraph()
        for nid, data in self.g.nodes(data=True):
            if kinds and data.get("kind") not in kinds:
                continue
            h.add_node(nid, **{k: (v if isinstance(v, (int, float, str)) else json.dumps(v, ensure_ascii=False))
                              for k, v in data.items()})
        for s, d, data in self.g.edges(data=True):
            if s in h and d in h:
                h.add_edge(s, d, kind=data["kind"], label=data.get("label", ""))
        nx.write_graphml(h, path)


def walk_ast(node: dict) -> Iterator[dict]:
    """일반화 AST의 전위 순회."""
    stack = [node]
    while stack:
        n = stack.pop()
        yield n
        ch = n.get("ch")
        if ch:
            stack.extend(reversed([c for c in ch if c is not None]))


def ast_index(root: dict) -> Dict[int, dict]:
    return {n["id"]: n for n in walk_ast(root) if "id" in n}


def ast_parents(root: dict) -> Dict[int, Optional[int]]:
    par: Dict[int, Optional[int]] = {root.get("id"): None}
    for n in walk_ast(root):
        for c in n.get("ch") or []:
            if c is not None:
                par[c["id"]] = n["id"]
    return par
