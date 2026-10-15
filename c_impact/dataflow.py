"""3단계(b): Data Flow / Def-Use — 도달 정의, 정의-사용 체인, 상향 노출 사용, 포인터 별칭(points-to).

- 함수 내부: CFG 위의 도달 정의 분석으로 def→use 간선을 만든다.
  ENTRY 는 매개변수·전역·역참조 대상의 '진입 시점 값'을 정의한다(상향 노출 사용 판정).
- 강한 정의(변수/필드 직접 대입)만 이전 정의를 제거(kill)한다. 포인터·배열 원소 대입은 약한 정의.
- 포인터 별칭: p = &x, p = arr, p = q(복사) 를 함수 단위·전역 단위로 수집(흐름 비민감).
  대상이 하나로 확정되면 confirmed, 여럿이거나 출처가 불명확하면 potential.
"""

from __future__ import annotations

from .ast_extract import iter_stmts, kbase, key_parts
from .cfg import CFG, ENTRY
from .roles import iter_expr


def overlap(d: str, u: str) -> bool:
    return d == u or u.startswith(d + ".") or d.startswith(u + ".")


def root_key(k: str) -> str:
    """'*P:p.f' → '*P:p', 'G:x.f' → 'G:x'"""
    star = k.startswith("*")
    return ("*" if star else "") + kbase(k)


def strong_defs(st) -> set[str]:
    out = set()
    if st["kind"] == "decl" and st.get("decl") and not st.get("static_local"):
        out.add(st["decl"])
    e = st.get("expr")
    if e is None:
        return out
    for x in iter_expr(e):
        lhs = None
        if x.get("k") == "assign":
            lhs = x["l"]
        elif x.get("k") == "un" and x["op"] in ("++", "--"):
            lhs = x["e"]
        if lhs is None:
            continue
        k = _direct_key(lhs)
        if k:
            out.add(k)
    return out


def _direct_key(e):
    while e.get("k") == "cast":
        e = e["e"]
    if e.get("k") == "var":
        return e.get("key")
    if e.get("k") == "member" and not e.get("arrow"):
        b = _direct_key(e["base"])
        return f"{b}.{e['field']}" if b else None
    return None


class FunctionDF:
    def __init__(self, fn: dict, cfg: CFG | None = None):
        self.fn = fn
        self.cfg = cfg or CFG(fn)
        self.defs: dict[int, list[str]] = {}
        self.uses: dict[int, list[str]] = {}
        self.strong: dict[int, set[str]] = {}
        self.du: dict[int, list[tuple[int, str]]] = {}
        self.ud: dict[int, list[tuple[int, str]]] = {}
        self.upward: dict[str, list[int]] = {}  # 진입 시점 값을 읽는 사용 (키 → 노드)
        self._run()

    def _run(self):
        cfg = self.cfg
        entry_keys = set(p["key"] for p in self.fn["params"])
        for n, st in cfg.stmts.items():
            self.defs[n] = list(st.get("defs", []))
            self.uses[n] = [u for u in st.get("uses", []) if not u.startswith("&")]
            self.strong[n] = strong_defs(st)
            for u in self.uses[n]:
                entry_keys.add(root_key(u))
            for d in self.defs[n]:
                if d.startswith(("G:", "*")):
                    entry_keys.add(root_key(d))
        self.defs[ENTRY] = sorted(entry_keys)
        self.uses[ENTRY] = []
        self.strong[ENTRY] = set(entry_keys)

        nodes = list(cfg.nodes)
        IN = {n: set() for n in nodes}
        OUT = {n: set() for n in nodes}
        gen = {n: {(n, k) for k in self.defs.get(n, [])} for n in nodes}
        work = list(nodes)
        inq = set(nodes)
        while work:
            n = work.pop(0)
            inq.discard(n)
            new_in = set()
            for p, _ in cfg.pred.get(n, []):
                new_in |= OUT[p]
            IN[n] = new_in
            kill = self.strong.get(n, set())
            out = {f for f in new_in if f[1] not in kill} | gen[n]
            if out != OUT[n]:
                OUT[n] = out
                for s, _ in cfg.succ.get(n, []):
                    if s not in inq:
                        work.append(s)
                        inq.add(s)
        for u in nodes:
            for k in self.uses.get(u, []):
                for d, dk in IN[u]:
                    if overlap(dk, k):
                        self.du.setdefault(d, []).append((u, k))
                        self.ud.setdefault(u, []).append((d, dk))
                        if d == ENTRY:
                            self.upward.setdefault(root_key(k), []).append(u)
        self.IN = IN

    def uses_of(self, node, key):
        """node 의 정의 key 가 도달하는 사용 노드."""
        return [u for u, k in self.du.get(node, []) if overlap(key, k)]

    def upward_uses(self, key):
        """진입 시점의 key 값을 읽는 노드(다른 함수/이전 호출에서 쓴 값을 읽는 위치)."""
        out = []
        rk = root_key(key)
        for k, nodes in self.upward.items():
            if overlap(rk, k) or overlap(k, rk):
                for n in nodes:
                    if any(overlap(key, u) for u in self.uses.get(n, [])):
                        out.append(n)
        return sorted(set(out))


class PointsTo:
    """흐름 비민감 points-to. 키는 (스코프, 포인터 키) — 전역 포인터는 스코프 None."""

    def __init__(self, project):
        self.p = project
        self.pts: dict[tuple, set] = {}
        self.unknown: set[tuple] = set()
        self._build()

    def _scope(self, fid, key):
        return (None, key) if key.startswith("G:") else (fid, key)

    def _build(self):
        p = self.p
        copies = []
        for gid, g in p.globals.items():
            for t in g.get("addr_init", []):
                self.pts.setdefault((None, g["key"]), set()).add((None, t) if t.startswith("G:") else (None, t))
        for fid, fn in p.functions.items():
            assign_count: dict[str, int] = {}
            ptr_defs: dict[str, int] = {}
            for st in iter_stmts(fn["body"]):
                for lhs, rhs in st.get("ptr_assign", []):
                    assign_count[lhs] = assign_count.get(lhs, 0) + 1
                    if rhs.startswith("fn:"):
                        continue
                    if rhs.startswith("copy:"):
                        copies.append((self._scope(fid, lhs), self._scope(fid, rhs[5:])))
                    else:
                        self.pts.setdefault(self._scope(fid, lhs), set()).add(self._scope(fid, rhs))
                for d in st.get("defs", []):
                    if not d.startswith("*") and not key_parts(d)[2]:
                        ptr_defs[d] = ptr_defs.get(d, 0) + 1
            for k, n in ptr_defs.items():
                if n > assign_count.get(k, 0):
                    self.unknown.add(self._scope(fid, k))
            # 매개변수: 호출 지점의 &x 전달(문맥 비민감 합집합)
            for call in fn["calls"]:
                callee = call.get("callee")
                if not callee or callee not in p.functions:
                    continue
                cfn = p.functions[callee]
                for i, addr in enumerate(call.get("arg_addr", [])):
                    if addr and i < len(cfn["params"]):
                        self.pts.setdefault((callee, cfn["params"][i]["key"]), set()).add(self._scope(fid, addr))
                for i, ptr in enumerate(call.get("arg_ptr", [])):
                    if ptr and i < len(cfn["params"]):
                        copies.append(((callee, cfn["params"][i]["key"]), self._scope(fid, ptr)))
        for _ in range(5):  # 복사 전파(고정점 근사)
            changed = False
            for dst, src in copies:
                s = self.pts.get(src, set())
                d = self.pts.setdefault(dst, set())
                if not s <= d:
                    d |= s
                    changed = True
                if src in self.unknown and dst not in self.unknown:
                    self.unknown.add(dst)
                    changed = True
            if not changed:
                break

    def resolve(self, fid, key) -> tuple[list[tuple], bool]:
        """'*ptr.f' 형태 키 → [(스코프, 대상키.f)], 확정 여부."""
        if not key.startswith("*"):
            return [], True
        inner = key[1:]
        _, base, suffix = key_parts(inner)
        sc = self._scope(fid, base)
        tg = self.pts.get(sc, set())
        out = [(s, t + suffix) for s, t in sorted(tg, key=str)]
        certain = len(out) == 1 and sc not in self.unknown
        return out, certain

    def to_dict(self):
        return {f"{k[0] or '<global>'}|{k[1]}": sorted(f"{s or '<global>'}|{t}" for s, t in v)
                for k, v in self.pts.items()}
