"""C 부분집합 기호 실행기(z3, 유계).

* C 정수 의미: 자료형별 비트폭, 부호(signed/unsigned 비교·나눗셈·시프트), clang이 넣은 암묵
  형변환(정수 승격·통상 산술 변환), 복합 대입의 계산형, _Bool 변환, 부호 없는 wrap-around.
* 실행 모델: 경로를 나열하지 않고 '보호 조건(guard)' 기반으로 모든 분기를 한 번에 인코딩한다.
  메모리 쓰기는 ite(guard, 새 값, 이전 값)으로 기록되므로 분기 병합이 필요 없다.
* 반복문은 loop_bound까지 전개하고, 더 반복할 수 있는 조건을 unwind 조건으로 기록한다.
  (전개 범위가 부족하면 전체 정확성을 증명한 것이 아니다. 보고서에 범위를 표시한다.)
* 함수 호출은 inline_depth까지 인라인. 본문 없는 외부 함수는 두 버전이 공유하는 미해석 함수
  (같은 입력 -> 같은 출력)로 모델링한다.
* 지원하지 않는 구문(goto, 부동소수점, 정수->포인터 변환 등)은 Unsupported를 발생시키며
  결과는 '분석 불완전으로 판정 유보'가 된다.
"""
from __future__ import annotations

from typing import Dict, List, Optional, Set, Tuple

import z3

from .model import FIELD_SEP


class Unsupported(Exception):
    pass


BV_IDX = 32


def bits_of(ty: Optional[dict]) -> int:
    if not ty:
        return 32
    k = ty.get("k")
    if k in ("i", "b"):
        return int(ty.get("b", 32))
    if k == "p":
        return 64
    raise Unsupported(f"정수가 아닌 자료형 {ty.get('n')}")


def is_signed(ty: Optional[dict]) -> bool:
    return bool(ty and ty.get("k") == "i" and ty.get("s"))


def bv(v: int, b: int) -> z3.BitVecRef:
    return z3.BitVecVal(v % (1 << b), b)


class PtrVal:
    """포인터 값: [(조건, 대상)] 대상 = ('loc', 위치, 인덱스|None) | ('fn', 함수키) | None(NULL)"""
    __slots__ = ("entries",)

    def __init__(self, entries):
        out = []
        for c, t in entries:
            if z3.is_false(c):
                continue
            out.append((c, t))
        self.entries = out

    @staticmethod
    def single(t) -> "PtrVal":
        return PtrVal([(z3.BoolVal(True), t)])


def merge(cond, a, b):
    if a is None:
        return b
    if b is None:
        return a
    if isinstance(a, PtrVal) or isinstance(b, PtrVal):
        if not isinstance(a, PtrVal) or not isinstance(b, PtrVal):
            raise Unsupported("포인터와 정수 값의 병합")
        ents = [(z3.And(cond, c), t) for c, t in a.entries] + [(z3.And(z3.Not(cond), c), t) for c, t in b.entries]
        return PtrVal(ents)
    if z3.is_bv(a) and z3.is_bv(b) and a.size() != b.size():
        w = max(a.size(), b.size())
        a = z3.ZeroExt(w - a.size(), a) if a.size() < w else a
        b = z3.ZeroExt(w - b.size(), b) if b.size() < w else b
    if z3.is_true(cond):
        return a
    if z3.is_false(cond):
        return b
    return z3.If(cond, a, b)


def resize(v: z3.BitVecRef, to: int, signed: bool) -> z3.BitVecRef:
    w = v.size()
    if w == to:
        return v
    if w > to:
        return z3.Extract(to - 1, 0, v)
    return z3.SignExt(to - w, v) if signed else z3.ZeroExt(to - w, v)


class Frame:
    def __init__(self, func: str, tag: str, depth: int):
        self.func = func
        self.tag = tag
        self.depth = depth
        self.ret = None
        self.returned = z3.BoolVal(False)
        self.brk: List = []
        self.cont: List = []
        self.switch: List = []


class Program:
    """한 버전(또는 혼합 버전)의 함수·전역·레코드 AST 묶음."""

    def __init__(self, asts: Dict[str, dict], name: str, external: Set[str] = None,
                 volatile_globals: Set[str] = None, calibrations: Dict[str, tuple] = None,
                 rte_map: Dict[str, dict] = None):
        self.name = name
        self.calibrations = calibrations or {}   # 전역 키 -> (min, max): 상수로 접지 않는 자유 입력
        self.rte_map = rte_map or {}             # RTE API 이름 -> {signal, access, direction}
        self.functions = {k: a for k, a in asts.items() if k.startswith("fn:") and a.get("k") == "Func"}
        self.globals = {k: a for k, a in asts.items() if k.startswith("gv:") and a.get("k") == "GlobalVar"}
        self.records = {a.get("v"): a for k, a in asts.items() if k.startswith("ty:") and a.get("k") == "Record"}
        self.external = external or set()
        self.volatile_globals = volatile_globals or set()

    def hybrid(self, other: "Program", replace: Set[str], name: str) -> "Program":
        asts = {}
        asts.update({k: v for k, v in self.globals.items()})
        asts.update({k: v for k, v in self.functions.items()})
        asts.update({f"ty:{k}": v for k, v in self.records.items()})
        for k, v in other.globals.items():
            asts.setdefault(k, v)
        for k, v in other.functions.items():
            if k in replace or k not in self.functions:
                asts[k] = v
        for k, v in other.records.items():
            asts.setdefault(f"ty:{k}", v)
        return Program(asts, name, self.external | other.external, self.volatile_globals | other.volatile_globals,
                       {**other.calibrations, **self.calibrations}, {**other.rte_map, **self.rte_map})


class SymExec:
    def __init__(self, prog: Program, loop_bound: int = 8, inline_depth: int = 8,
                 initial_state: str = "initializers", watch: Optional[Set[Tuple[str, int]]] = None,
                 shared_prefix: str = "", tag: str = "v"):
        self.p = prog
        self.K = loop_bound
        self.D = inline_depth
        self.init_mode = initial_state
        self.watch = watch or set()
        self.events: Dict[Tuple[str, int], List[Tuple[z3.BoolRef, object]]] = {}
        self.tag = tag
        self.mem: Dict[str, object] = {}
        self.unwind = z3.BoolVal(False)
        self.notes: Set[str] = set()
        self.frame_id = 0
        self.vol_count: Dict[str, int] = {}
        self.ext_count: Dict[str, int] = {}
        self.ext_calls: Dict[str, List[Tuple[z3.BoolRef, List]]] = {}
        self.step = 0
        self.frames: List[Frame] = []
        self.mmio_writes: Dict[str, List] = {}
        self.rte_pending: Dict[str, object] = {}
        self.rte_snapshot: Dict[str, object] = {}
        self.rte_env_count: Dict[Tuple[str, int], int] = {}

    # ---------------- 메모리 ----------------
    def fresh(self, name: str, ty: dict):
        k = ty.get("k")
        if k == "b":
            return z3.ZeroExt(bits_of(ty) - 1, z3.BitVec(name, 1))
        if k == "i":
            return z3.BitVec(name, bits_of(ty))
        if k == "a":
            el = ty.get("el") or {}
            if el.get("k") in ("i", "b"):
                return z3.Array(name, z3.BitVecSort(BV_IDX), z3.BitVecSort(bits_of(el)))
        raise Unsupported(f"기호 초기값을 만들 수 없는 자료형 {ty.get('n')}")

    def zero(self, ty: dict):
        k = ty.get("k")
        if k in ("i", "b"):
            return bv(0, bits_of(ty))
        if k == "p":
            return PtrVal.single(None)
        if k == "a":
            el = ty.get("el") or {}
            if el.get("k") in ("i", "b"):
                return z3.K(z3.BitVecSort(BV_IDX), bv(0, bits_of(el)))
            if el.get("k") == "p":
                return ("ptrarray", {})
        raise Unsupported(f"0 초기화할 수 없는 자료형 {ty.get('n')}")

    def leaves(self, ty: dict, prefix: str = "") -> List[Tuple[str, dict]]:
        """레코드를 필드 경로 단위로 평탄화. 배열 안 레코드는 필드별 배열로 분리."""
        k = ty.get("k")
        if k == "r":
            rec = self.p.records.get(ty.get("rec"))
            if not rec:
                raise Unsupported(f"레코드 정의 없음 {ty.get('rec')}")
            out = []
            for f in rec.get("ch", []):
                out += self.leaves(f.get("ty") or {}, f"{prefix}{FIELD_SEP}{f['v']}")
            return out
        if k == "a":
            el = ty.get("el") or {}
            if el.get("k") == "r":
                return [(p, {"k": "a", "el": t, "n": f"{t.get('n')}[]", "cnt": ty.get("cnt")})
                        for p, t in self.leaves(el, prefix)]
        return [(prefix, ty)]

    def ensure_global(self, key: str) -> None:
        if any(l == key or l.startswith(key + FIELD_SEP) for l in self.mem):
            return
        if key.startswith("gv:mmio:"):
            return
        g = self.p.globals.get(key)
        if g is not None and key in self.p.calibrations and (g.get("ty") or {}).get("k") in ("i", "b"):
            # 캘리브레이션: const/const volatile이어도 XCP 등으로 바뀌므로 상수로 접지 않고 공유 자유 입력
            self.mem[key] = self.fresh(f"cal:{key}", g.get("ty") or {})
            return
        if g is None:
            raise Unsupported(f"전역 변수 정의 없음 {key}")
        ty = g.get("ty") or {}
        leaves = self.leaves(ty)
        if self.init_mode == "symbolic" and not (ty.get("const")):
            for path, lty in leaves:
                self.mem[key + path] = self.fresh(f"init:{key}{path}", lty) if lty.get("k") != "p" else self.zero(lty)
            return
        for path, lty in leaves:
            self.mem[key + path] = self.zero(lty)
        if g.get("ch"):
            self.init_store([(z3.BoolVal(True), key, None)], g["ch"][0], ty, z3.BoolVal(True))

    def loc_of(self, n: dict) -> str:
        ref = n.get("ref")
        if not ref:
            raise Unsupported(f"참조 대상 불명 {n.get('v')}")
        if ref.startswith("gv:"):
            self.ensure_global(ref)
            return ref
        fr = self.frames[-1]
        return f"{ref}@{fr.tag}"

    def read(self, loc: str, idx, ty: dict):
        root = loc.split(FIELD_SEP, 1)[0]
        if root.startswith("gv:") and (root in self.p.volatile_globals or root.startswith("gv:mmio:")):
            # volatile(ISR·하드웨어 공유) 읽기는 읽을 때마다 새 환경 입력. 두 버전에서
            # 같은 단계·같은 순번의 읽기는 같은 기호를 사용한다.
            ck = (loc, self.step)
            c = self.vol_count.get(ck, 0)
            self.vol_count[ck] = c + 1
            if (ty or {}).get("k") == "b":
                return z3.ZeroExt(bits_of(ty) - 1, z3.BitVec(f"vol:{loc}@{self.step}#{c}", 1))
            if (ty or {}).get("k") == "i":
                return z3.BitVec(f"vol:{loc}@{self.step}#{c}", bits_of(ty))
        if loc not in self.mem:
            if root.startswith("gv:"):
                self.ensure_global(root)
            if loc not in self.mem:
                # 초기화되지 않은 지역 변수: 버전별 임의 값(같다고 가정하지 않음)
                self.notes.add(f"초기화되지 않은 값 읽기 가능: {loc.split('@')[0]}")
                self.mem[loc] = self.fresh(f"undef:{self.tag}:{loc}", ty) if (ty or {}).get("k") in ("i", "b", "a") \
                    else self.zero(ty)
        v = self.mem[loc]
        if idx is not None:
            if isinstance(v, tuple) and v[0] == "ptrarray":
                key = self._idx_const(idx)
                return v[1].get(key, PtrVal.single(None))
            if not z3.is_array(v):
                if z3.is_bv(idx) and z3.is_true(z3.simplify(idx == 0)):
                    return v
                raise Unsupported("배열이 아닌 위치의 인덱스 접근")
            return z3.Select(v, idx)
        return v

    @staticmethod
    def _idx_const(idx) -> int:
        s = z3.simplify(idx)
        if z3.is_bv_value(s):
            return s.as_long()
        raise Unsupported("포인터 배열의 기호 인덱스 접근")

    def write(self, loc: str, idx, val, guard) -> None:
        if loc.startswith("gv:mmio:"):
            # 레지스터 쓰기는 관찰 가능한 출력으로 기록(읽기는 매번 새 입력)
            self.mmio_writes.setdefault(loc, []).append((guard, val))
        if loc not in self.mem:
            root = loc.split(FIELD_SEP, 1)[0]
            if root.startswith("gv:"):
                self.ensure_global(root)
        old = self.mem.get(loc)
        if idx is not None:
            if isinstance(old, tuple) and old[0] == "ptrarray":
                key = self._idx_const(idx)
                old[1][key] = merge(guard, val, old[1].get(key, PtrVal.single(None)))
                return
            if old is None or not z3.is_array(old):
                if z3.is_true(z3.simplify(idx == 0)) and old is not None:
                    self.mem[loc] = merge(guard, val, old)
                    return
                raise Unsupported("배열이 아닌 위치의 인덱스 쓰기")
            if not z3.is_bv(val):
                raise Unsupported("배열 원소 자료형 불일치")
            el_w = old.range().size()
            val = resize(val, el_w, False)
            self.mem[loc] = z3.If(guard, z3.Store(old, idx, val), old) if not z3.is_true(guard) else z3.Store(old, idx, val)
            return
        if old is None:
            self.mem[loc] = val
        else:
            self.mem[loc] = merge(guard, val, old)

    # ---------------- 실행 ----------------
    def active(self, guard):
        fr = self.frames[-1]
        conds = [guard, z3.Not(fr.returned)]
        if fr.brk:
            conds.append(z3.Not(fr.brk[-1]))
        if fr.cont:
            conds.append(z3.Not(fr.cont[-1]))
        return z3.simplify(z3.And(*conds))

    def record(self, n: dict, guard, value) -> None:
        key = (self.frames[-1].func, n.get("id"))
        if key in self.watch:
            self.events.setdefault(key, []).append((guard, value))

    def call_function(self, fkey: str, args: List, guard) -> object:
        f = self.p.functions.get(fkey)
        if f is None:
            raise Unsupported(f"함수 본문 없음 {fkey}")
        if len(self.frames) >= self.D:
            raise Unsupported(f"인라인 깊이 한도({self.D}) 초과: {fkey}")
        self.frame_id += 1
        fr = Frame(fkey, f"{self.tag}{self.frame_id}", len(self.frames))
        self.frames.append(fr)
        try:
            for p, a in zip(f.get("params", []), args):
                loc = f"{p['key']}@{fr.tag}"
                self.mem[loc] = self.convert(a, None, p.get("ty") or {}) if a is not None else self.zero(p.get("ty") or {})
            if f.get("ch"):
                self.exec(f["ch"][0], guard)
            ret = fr.ret
            rty = f.get("ret") or {}
            if ret is None and rty.get("k") not in ("v", None):
                ret = self.fresh(f"noret:{self.tag}:{fkey}:{self.frame_id}", rty) if rty.get("k") in ("i", "b") else None
        finally:
            self.frames.pop()
        return ret

    def exec(self, s: dict, guard) -> None:
        k = s["k"]
        fr = self.frames[-1]
        if k == "Compound":
            for c in s.get("ch", []):
                self.exec(c, guard)
            return
        eff = self.active(guard)
        if z3.is_false(eff):
            return
        if k == "ExprStmt":
            v = self.ev(s["ch"][0], eff) if s.get("ch") else None
            self.record(s, eff, v)
        elif k == "DeclStmt":
            for v in s.get("ch", []):
                self.decl(v, eff)
        elif k == "Return":
            v = None
            if s.get("ch"):
                v = self.ev(s["ch"][0], eff)
                rty = self.p.functions[fr.func].get("ret") or {}
                v = self.convert(v, s["ch"][0].get("ty"), rty)
                fr.ret = merge(eff, v, fr.ret) if fr.ret is not None else v
            self.record(s, eff, v)
            fr.returned = z3.simplify(z3.Or(fr.returned, eff))
        elif k == "If":
            c = self.truth(self.ev(s["ch"][0], eff))
            self.exec(s["ch"][1], z3.And(eff, c))
            if len(s["ch"]) > 2:
                self.exec(s["ch"][2], z3.And(eff, z3.Not(c)))
        elif k in ("While", "Do", "For"):
            self.loop(s, eff)
        elif k == "Switch":
            self.switch(s, eff)
        elif k == "Break":
            if not fr.brk:
                raise Unsupported("루프/switch 밖 break")
            fr.brk[-1] = z3.simplify(z3.Or(fr.brk[-1], eff))
        elif k == "Continue":
            if not fr.cont:
                raise Unsupported("루프 밖 continue")
            fr.cont[-1] = z3.simplify(z3.Or(fr.cont[-1], eff))
        elif k == "Null":
            return
        elif k in ("Case", "Default"):
            if not fr.switch:
                raise Unsupported("switch 밖 case")
            sw = fr.switch[-1]
            if k == "Case":
                v = s.get("v")
                if isinstance(v, list):
                    lo, hi = v
                    w = sw["v"].size()
                    hit = z3.And(sw["v"] >= bv(lo, w), sw["v"] <= bv(hi, w))
                else:
                    hit = sw["v"] == bv(int(v), sw["v"].size())
            else:
                hit = sw["default"]
            sw["matched"] = z3.Or(sw["matched"], z3.And(guard, hit))
            if s.get("ch"):
                self.exec(s["ch"][0], z3.And(sw["guard"], sw["matched"]))
        else:
            raise Unsupported(f"지원하지 않는 문장 {k}")

    def decl(self, v: dict, eff) -> None:
        if v.get("static"):
            return
        loc = self.loc_of(v)
        ty = v.get("ty") or {}
        if v.get("ch"):
            init = v["ch"][0]
            if init.get("k") == "InitList" or ty.get("k") in ("r", "a"):
                for path, lty in self.leaves(ty):
                    self.write(loc + path, None, self.zero(lty), eff)
                self.init_store([(z3.BoolVal(True), loc, None)], init, ty, eff)
                return
            val = self.convert(self.ev(init, eff), init.get("ty"), ty)
            self.write(loc, None, val, eff)
            self.record(v, eff, val)
        else:
            if ty.get("k") in ("r", "a"):
                for path, lty in self.leaves(ty):
                    self.write(loc + path, None, self.fresh(f"undef:{self.tag}:{loc}{path}", lty)
                               if lty.get("k") in ("i", "b", "a") else self.zero(lty), eff)
            elif ty.get("k") in ("i", "b"):
                self.write(loc, None, z3.BitVec(f"undef:{self.tag}:{loc}", bits_of(ty)), eff)
            else:
                self.write(loc, None, self.zero(ty), eff)

    def init_store(self, lv, init: dict, ty: dict, guard) -> None:
        """초기화 목록(구조체·배열)을 필드/원소 단위로 저장."""
        if init.get("k") == "ICast" and init.get("ch"):
            inner = init["ch"][0]
            if inner.get("k") == "InitList":
                init = inner
        if init.get("k") != "InitList":
            if ty.get("k") == "r":
                src = self.lval(init, guard)
                for path, lty in self.leaves(ty):
                    val = self.load([(c, l + path, i) for c, l, i in src], lty)
                    self.store([(c, l + path, i) for c, l, i in lv], val, guard)
                return
            val = self.convert(self.ev(init, guard), init.get("ty"), ty)
            self.store(lv, val, guard)
            return
        k = ty.get("k")
        if k == "r":
            fields = init.get("fields") or []
            rec = self.p.records.get(ty.get("rec")) or {}
            ftys = {f["v"]: f.get("ty") or {} for f in rec.get("ch", [])}
            for i, c in enumerate(init.get("ch", [])):
                if i >= len(fields):
                    break
                fname = fields[i].split(".", 1)[1]
                self.init_store([(cc, l + FIELD_SEP + fname, ix) for cc, l, ix in lv], c, ftys.get(fname, c.get("ty") or {}), guard)
        elif k == "a":
            el = ty.get("el") or {}
            for i, c in enumerate(init.get("ch", [])):
                self.init_store([(cc, l, bv(i, BV_IDX)) for cc, l, _ in lv], c, el, guard)
        else:
            if init.get("ch"):
                self.init_store(lv, init["ch"][0], ty, guard)

    def loop(self, s: dict, eff) -> None:
        fr = self.frames[-1]
        k = s["k"]
        ch = s["ch"]
        if k == "For":
            has = s.get("has", {})
            parts = list(ch)
            init = parts.pop(0) if has.get("init") else None
            cond = parts.pop(0) if has.get("cond") else None
            inc = parts.pop(0) if has.get("inc") else None
            body = parts[0]
            if init is not None:
                self.exec(init, eff)
        elif k == "While":
            cond, body, inc = ch[0], ch[1], None
        else:
            body, cond, inc = ch[0], ch[1], None
        fr.brk.append(z3.BoolVal(False))
        for it in range(self.K + 1):
            fr.cont.append(z3.BoolVal(False))
            g = self.active(eff)
            if k != "Do" or it > 0:
                c = self.truth(self.ev(cond, g)) if cond is not None else z3.BoolVal(True)
            else:
                c = z3.BoolVal(True)
            if it == self.K:
                self.unwind = z3.simplify(z3.Or(self.unwind, z3.And(g, c)))
                fr.cont.pop()
                break
            self.exec(body, z3.And(g, c))
            # 이 반복에서 조건이 거짓이면 이후 반복은 실행되지 않음
            fr.brk[-1] = z3.simplify(z3.Or(fr.brk[-1], z3.And(g, z3.Not(c))))
            fr.cont.pop()
            if inc is not None:
                self.exec(inc, eff)
            if z3.is_false(self.active(eff)):
                break
        fr.brk.pop()

    def switch(self, s: dict, eff) -> None:
        fr = self.frames[-1]
        v = self.ev(s["ch"][0], eff)
        if not z3.is_bv(v):
            raise Unsupported("정수가 아닌 switch 조건")
        vals = []
        for n in _walk_cases(s["ch"][1]):
            if n["k"] == "Case":
                vals.append(n.get("v"))
        w = v.size()
        conds = []
        for x in vals:
            if isinstance(x, list):
                conds.append(z3.And(v >= bv(x[0], w), v <= bv(x[1], w)))
            elif x is not None:
                conds.append(v == bv(int(x), w))
        default = z3.Not(z3.Or(*conds)) if conds else z3.BoolVal(True)
        fr.brk.append(z3.BoolVal(False))
        fr.switch.append({"v": v, "matched": z3.BoolVal(False), "default": default, "guard": eff})
        body = s["ch"][1]
        items = body.get("ch", []) if body.get("k") == "Compound" else [body]
        for st in items:
            sw = fr.switch[-1]
            if st["k"] in ("Case", "Default"):
                self.exec(st, eff)
            else:
                self.exec(st, z3.And(eff, sw["matched"]))
        fr.switch.pop()
        fr.brk.pop()

    # ---------------- 식 ----------------
    def truth(self, v):
        if isinstance(v, PtrVal):
            return z3.Or(*[c for c, t in v.entries if t is not None]) if any(t is not None for _, t in v.entries) \
                else z3.BoolVal(False)
        if v is None:
            raise Unsupported("값이 없는 조건")
        return v != bv(0, v.size())

    def convert(self, v, from_ty: Optional[dict], to_ty: Optional[dict]):
        if to_ty is None:
            return v
        tk = to_ty.get("k")
        if tk == "v":
            return None
        if tk == "p":
            if isinstance(v, PtrVal):
                return v
            if v is not None and z3.is_bv(v):
                s = z3.simplify(v)
                if z3.is_bv_value(s) and s.as_long() == 0:
                    return PtrVal.single(None)
                if z3.is_bv_value(s):
                    # 정수 상수 주소 -> 메모리 맵 레지스터(graph_builder와 같은 키)
                    return PtrVal.single(("loc", f"gv:mmio:0x{s.as_long():08X}", None))
            raise Unsupported("정수 -> 포인터 변환(상수가 아닌 주소)")
        if tk == "b":
            if isinstance(v, PtrVal):
                return z3.If(self.truth(v), bv(1, 8), bv(0, 8))
            return z3.If(v != bv(0, v.size()), bv(1, bits_of(to_ty)), bv(0, bits_of(to_ty)))
        if tk == "i":
            if isinstance(v, PtrVal):
                raise Unsupported("포인터 -> 정수 변환")
            if v is None:
                raise Unsupported("void 값 사용")
            fs = is_signed(from_ty) if from_ty else False
            return resize(v, bits_of(to_ty), fs)
        if tk in ("r", "a", "f"):
            return v
        raise Unsupported(f"형변환 대상 {to_ty.get('n')}")

    def lval(self, n: dict, guard) -> List[Tuple[object, str, object]]:
        k = n["k"]
        if k in ("ICast", "Cast"):
            return self.lval(n["ch"][0], guard)
        if k == "Ref":
            if n.get("rk") in ("g", "l", "p"):
                return [(z3.BoolVal(True), self.loc_of(n), None)]
            raise Unsupported(f"lvalue가 아닌 참조 {n.get('v')}")
        if k == "Member":
            if n.get("arrow"):
                p = self.ev(n["ch"][0], guard)
                return [(c, l + FIELD_SEP + n["v"], i) for c, l, i in self.ptr_targets(p)]
            return [(c, l + FIELD_SEP + n["v"], i) for c, l, i in self.lval(n["ch"][0], guard)]
        if k == "Index":
            p = self.ev(n["ch"][0], guard)
            idx = self.ev(n["ch"][1], guard)
            idx = resize(idx, BV_IDX, is_signed(n["ch"][1].get("ty")))
            out = []
            for c, l, i in self.ptr_targets(p):
                out.append((c, l, idx if i is None else i + idx))
            return out
        if k == "Un" and n.get("v") == "*":
            return self.ptr_targets(self.ev(n["ch"][0], guard))
        raise Unsupported(f"lvalue 식 {k}")

    def ptr_targets(self, p) -> List[Tuple[object, str, object]]:
        if not isinstance(p, PtrVal):
            raise Unsupported("포인터가 아닌 값의 역참조")
        out = []
        for c, t in p.entries:
            if t is None:
                self.notes.add("NULL 역참조 가능 경로는 모델에서 제외")
                continue
            if t[0] == "fn":
                raise Unsupported("함수 포인터 역참조를 값으로 사용")
            out.append((c, t[1], t[2]))
        if not out:
            raise Unsupported("대상이 없는 포인터 역참조")
        return out

    def load(self, lv, ty: dict):
        val = None
        for c, l, i in reversed(lv):
            v = self.read(l, i, ty)
            val = v if val is None else merge(c, v, val)
        return val

    def store(self, lv, val, guard) -> None:
        for c, l, i in lv:
            self.write(l, i, val, z3.And(guard, c) if not z3.is_true(c) else guard)

    def ev(self, n: dict, guard):
        v = self._ev(n, guard)
        self.record(n, guard, v)
        return v

    def _ev(self, n: dict, guard):
        k = n["k"]
        ty = n.get("ty") or {}
        if k == "Lit":
            if n.get("v") is None or isinstance(n.get("v"), float):
                raise Unsupported("값을 알 수 없는 상수")
            return bv(int(n["v"]), bits_of(ty))
        if k in ("FLit",):
            raise Unsupported("부동소수점 연산")
        if k == "Str":
            raise Unsupported("문자열 리터럴")
        if k == "Ref":
            rk = n.get("rk")
            if rk == "f":
                return PtrVal.single(("fn", n["ref"]))
            if rk == "e":
                return bv(int(n.get("val") or 0), bits_of(ty))
            if ty.get("k") == "a":
                return PtrVal.single(("loc", self.loc_of(n), bv(0, BV_IDX)))
            if ty.get("k") == "r":
                return ("record", self.lval(n, guard), ty)
            return self.load(self.lval(n, guard), ty)
        if k in ("ICast", "Cast"):
            c = n["ch"][0]
            v = self.ev(c, guard)
            if isinstance(v, tuple) and v and v[0] == "record":
                return v
            if (ty.get("k") == "p" and (c.get("ty") or {}).get("k") == "f"):
                return v
            return self.convert(v, c.get("ty"), ty)
        if k in ("Member", "Index") or (k == "Un" and n.get("v") == "*"):
            if ty.get("k") == "a":
                lv = self.lval(n, guard)
                return PtrVal([(c, ("loc", l, i if i is not None else bv(0, BV_IDX))) for c, l, i in lv])
            if ty.get("k") == "f":
                return self.ev(n["ch"][0], guard)
            if ty.get("k") == "r":
                return ("record", self.lval(n, guard), ty)
            return self.load(self.lval(n, guard), ty)
        if k == "Un":
            return self.unary(n, guard)
        if k == "Bin":
            return self.binary(n, guard)
        if k == "Assign":
            lhs, rhs = n["ch"]
            lty = lhs.get("ty") or {}
            if lty.get("k") == "r":
                src = self.ev(rhs, guard)
                if not (isinstance(src, tuple) and src[0] == "record"):
                    raise Unsupported("구조체 대입 원본")
                dst = self.lval(lhs, guard)
                for path, t in self.leaves(lty):
                    val = self.load([(c, l + path, i) for c, l, i in src[1]], t)
                    self.store([(c, l + path, i) for c, l, i in dst], val, guard)
                return None
            v = self.ev(rhs, guard)
            v = self.convert(v, rhs.get("ty"), lty)
            self.store(self.lval(lhs, guard), v, guard)
            return v
        if k == "CAssign":
            return self.cassign(n, guard)
        if k == "Cond":
            c = self.truth(self.ev(n["ch"][0], guard))
            t = self.ev(n["ch"][1], z3.And(guard, c))
            e = self.ev(n["ch"][2], z3.And(guard, z3.Not(c)))
            return merge(c, t, e)
        if k == "Comma":
            self.ev(n["ch"][0], guard)
            return self.ev(n["ch"][1], guard)
        if k == "Call":
            return self.call(n, guard)
        raise Unsupported(f"지원하지 않는 식 {k} {n.get('v') or ''}")

    def unary(self, n: dict, guard):
        op = n.get("v")
        c = n["ch"][0]
        ty = n.get("ty") or {}
        if op == "&":
            s = c
            while s.get("k") == "ICast":
                s = s["ch"][0]
            if s.get("k") == "Ref" and s.get("rk") == "f":
                return PtrVal.single(("fn", s["ref"]))
            return PtrVal([(cc, ("loc", l, i)) for cc, l, i in self.lval(c, guard)])
        if op in ("pre++", "pre--", "post++", "post--"):
            lv = self.lval(c, guard)
            cty = c.get("ty") or {}
            old = self.load(lv, cty)
            if isinstance(old, PtrVal):
                raise Unsupported("포인터 증감")
            one = bv(1, old.size())
            new = old + one if "++" in op else old - one
            if cty.get("k") == "b":
                new = bv(1, 8) if "++" in op else z3.If(old == 0, bv(1, 8), bv(0, 8))
            self.store(lv, new, guard)
            return new if op.startswith("pre") else old
        v = self.ev(c, guard)
        if op == "!":
            return z3.If(self.truth(v), bv(0, bits_of(ty)), bv(1, bits_of(ty)))
        if isinstance(v, PtrVal):
            raise Unsupported(f"포인터에 대한 단항 {op}")
        if op == "-":
            return -v
        if op == "~":
            return ~v
        if op == "+":
            return v
        raise Unsupported(f"단항 연산 {op}")

    def binary(self, n: dict, guard):
        op = n.get("v")
        a_n, b_n = n["ch"][0], n["ch"][1]
        ty = n.get("ty") or {}
        if op in ("&&", "||"):
            a = self.truth(self.ev(a_n, guard))
            if op == "&&":
                b = self.truth(self.ev(b_n, z3.And(guard, a)))
                r = z3.And(a, b)
            else:
                b = self.truth(self.ev(b_n, z3.And(guard, z3.Not(a))))
                r = z3.Or(a, b)
            return z3.If(r, bv(1, bits_of(ty)), bv(0, bits_of(ty)))
        a = self.ev(a_n, guard)
        b = self.ev(b_n, guard)
        ta, tb = a_n.get("ty") or {}, b_n.get("ty") or {}
        if isinstance(a, PtrVal) or isinstance(b, PtrVal):
            return self.ptr_binary(op, a, b, ty)
        if op in ("<<", ">>"):
            b = resize(b, a.size(), False)
            if op == "<<":
                return a << b
            return a >> b if is_signed(ta) else z3.LShR(a, b)
        if a.size() != b.size():
            w = max(a.size(), b.size())
            a, b = resize(a, w, is_signed(ta)), resize(b, w, is_signed(tb))
        signed = is_signed(ta) and is_signed(tb)
        if op in ("<", "<=", ">", ">=", "==", "!="):
            if op == "==":
                r = a == b
            elif op == "!=":
                r = a != b
            elif signed:
                r = {"<": a < b, "<=": a <= b, ">": a > b, ">=": a >= b}[op]
            else:
                r = {"<": z3.ULT(a, b), "<=": z3.ULE(a, b), ">": z3.UGT(a, b), ">=": z3.UGE(a, b)}[op]
            return z3.If(r, bv(1, bits_of(ty)), bv(0, bits_of(ty)))
        sres = is_signed(ty) if ty.get("k") == "i" else signed
        if op == "+":
            r = a + b
        elif op == "-":
            r = a - b
        elif op == "*":
            r = a * b
        elif op == "/":
            self.notes.add("0으로 나누기 경로는 C에서 정의되지 않음(모델은 z3 의미 사용)")
            r = a / b if sres else z3.UDiv(a, b)
        elif op == "%":
            r = z3.SRem(a, b) if sres else z3.URem(a, b)
        elif op == "&":
            r = a & b
        elif op == "|":
            r = a | b
        elif op == "^":
            r = a ^ b
        else:
            raise Unsupported(f"이항 연산 {op}")
        return resize(r, bits_of(ty), sres) if ty.get("k") in ("i", "b") else r

    def ptr_binary(self, op, a, b, ty):
        if op in ("==", "!="):
            def as_ptr(x):
                if isinstance(x, PtrVal):
                    return x
                s = z3.simplify(x)
                if z3.is_bv_value(s) and s.as_long() == 0:
                    return PtrVal.single(None)
                raise Unsupported("포인터와 정수 비교")
            pa, pb = as_ptr(a), as_ptr(b)
            eqs = []
            for ca_, ta_ in pa.entries:
                for cb_, tb_ in pb.entries:
                    if ta_ is None and tb_ is None:
                        eqs.append(z3.And(ca_, cb_))
                    elif ta_ is not None and tb_ is not None and ta_[0] == tb_[0] and ta_[1] == tb_[1]:
                        ia, ib = (ta_[2] if len(ta_) > 2 else None), (tb_[2] if len(tb_) > 2 else None)
                        same = z3.BoolVal(True) if ia is None and ib is None else (
                            (ia if ia is not None else bv(0, BV_IDX)) == (ib if ib is not None else bv(0, BV_IDX)))
                        eqs.append(z3.And(ca_, cb_, same))
            r = z3.Or(*eqs) if eqs else z3.BoolVal(False)
            if op == "!=":
                r = z3.Not(r)
            return z3.If(r, bv(1, bits_of(ty)), bv(0, bits_of(ty)))
        if op in ("+", "-") and isinstance(a, PtrVal) and not isinstance(b, PtrVal):
            off = resize(b, BV_IDX, True)
            ents = []
            for c, t in a.entries:
                if t is None or t[0] != "loc":
                    raise Unsupported("NULL/함수 포인터 산술")
                i = t[2] if t[2] is not None else bv(0, BV_IDX)
                ents.append((c, ("loc", t[1], i + off if op == "+" else i - off)))
            return PtrVal(ents)
        raise Unsupported(f"포인터 연산 {op}")

    def cassign(self, n: dict, guard):
        op = n.get("v")[:-1]
        lhs, rhs = n["ch"]
        lty = lhs.get("ty") or {}
        lv = self.lval(lhs, guard)
        old = self.load(lv, lty)
        r = self.ev(rhs, guard)
        rty = rhs.get("ty") or {}
        if isinstance(old, PtrVal):
            res = self.ptr_binary(op, old, r, lty)
            self.store(lv, res, guard)
            return res
        ct = _common_type(lty, rty)
        a = resize(old, bits_of(ct), is_signed(lty))
        b = resize(r, bits_of(ct), is_signed(rty))
        s = is_signed(ct)
        if op in ("<<", ">>"):
            b = resize(r, a.size(), False)
            res = (a << b) if op == "<<" else ((a >> b) if is_signed(lty) else z3.LShR(a, b))
        else:
            res = {"+": lambda: a + b, "-": lambda: a - b, "*": lambda: a * b,
                   "/": lambda: (a / b if s else z3.UDiv(a, b)), "%": lambda: (z3.SRem(a, b) if s else z3.URem(a, b)),
                   "&": lambda: a & b, "|": lambda: a | b, "^": lambda: a ^ b}[op]()
        res = self.convert(res, ct, lty)
        self.store(lv, res, guard)
        return res

    def call(self, n: dict, guard):
        ch = n.get("ch", [])
        callee = ch[0] if ch else None
        args_n = ch[1:]
        direct = n.get("ref")
        if direct is None and callee is not None:
            s = callee
            while s.get("k") == "ICast":
                s = s["ch"][0]
            if s.get("k") == "Ref" and s.get("rk") == "f":
                direct = s["ref"]
        args = []
        for a in args_n:
            v = self.ev(a, guard)
            args.append(v)
        rty = n.get("ty") or {}
        if direct is not None:
            return self._call_target(direct, args, args_n, guard, rty, n)
        fp = self.ev(callee, guard)
        if not isinstance(fp, PtrVal):
            raise Unsupported("함수 포인터 값 해석 실패")
        result = None
        for c, t in fp.entries:
            if t is None or t[0] != "fn":
                continue
            r = self._call_target(t[1], args, args_n, z3.And(guard, c), rty, n)
            result = r if result is None else (merge(c, r, result) if r is not None else result)
        return result

    # ---------------- AUTOSAR RTE ----------------
    def _rte_value(self, sig: str, bits: int):
        """신호 버퍼 현재 값. 이 코드가 쓰지 않는 신호(다른 SWC·ECU 생산)는 단계마다 새 환경 입력."""
        key = f"rte:{sig}"
        produced = any(m["signal"] == sig and m["direction"] == "write" for m in self.p.rte_map.values())
        if key in self.mem:
            return self.mem[key]
        name = f"rte:{sig}@init" if produced else f"rte:{sig}@{self.step}"
        v = z3.BitVec(name, bits)
        self.mem[key] = v
        return v

    def begin_step(self) -> None:
        """runnable 시작: 암묵적 읽기(Rte_IRead)용 복사본을 만든다. 외부 생산 신호는 새 입력."""
        for api, m in self.p.rte_map.items():
            key = f"rte:{m['signal']}"
            produced = any(x["signal"] == m["signal"] and x["direction"] == "write" for x in self.p.rte_map.values())
            if not produced:
                self.mem.pop(key, None)
        self.rte_snapshot = {}
        self.rte_pending = {}

    def end_step(self) -> None:
        """runnable 종료: 암묵적 쓰기(Rte_IWrite)를 버퍼에 반영."""
        for key, v in self.rte_pending.items():
            self.mem[key] = v
        self.rte_pending = {}

    def _rte_call(self, name: str, m: dict, args, args_n, guard, rty):
        sig, implicit = m["signal"], m.get("access") == "implicit"
        key = f"rte:{sig}"
        if m["direction"] == "write":
            val = next((a for a in args if a is not None and not isinstance(a, PtrVal)), None)
            if val is None and args and isinstance(args[0], PtrVal):
                pt = (args_n[0].get("ty") or {}).get("pt") or {}
                val = self.load(self.ptr_targets(args[0]), pt)
            if val is None:
                raise Unsupported(f"RTE 쓰기 값 해석 실패 {name}")
            if implicit:
                cur = self.rte_pending.get(key, self.mem.get(key))
                self.rte_pending[key] = val if cur is None else merge(guard, val, cur)
            else:
                cur = self.mem.get(key)
                self.mem[key] = val if cur is None else merge(guard, val, cur)
            self.ext_calls.setdefault(name, []).append((guard, [val]))
            return bv(0, bits_of(rty)) if rty.get("k") in ("i", "b") else None
        # 읽기: 출력 포인터 인자 또는 반환값
        out_ptr = next(((a, an) for a, an in zip(args, args_n) if isinstance(a, PtrVal)), None)
        bits = bits_of(((out_ptr[1].get("ty") or {}).get("pt") or {}) if out_ptr else rty) if (out_ptr or rty.get("k") in ("i", "b")) else 32
        if implicit:
            if key not in self.rte_snapshot:
                self.rte_snapshot[key] = self._rte_value(sig, bits)
            val = self.rte_snapshot[key]
        else:
            val = self._rte_value(sig, bits)
        if z3.is_bv(val) and val.size() != bits:
            val = resize(val, bits, False)
        if out_ptr:
            self.store(self.ptr_targets(out_ptr[0]), val, guard)
            return bv(0, bits_of(rty)) if rty.get("k") in ("i", "b") else None
        return val

    def _mem_call(self, name: str, args, guard):
        """memcpy/memmove/memset: 객체 전체 단위로 모델링(크기 인자는 대상 객체 크기와 같다고 가정)."""
        dst = self.ptr_targets(args[0])
        if len(dst) != 1 or dst[0][2] not in (None,) and not z3.is_true(z3.simplify(dst[0][2] == 0)):
            raise Unsupported(f"{name}: 대상이 하나의 객체가 아님")
        dl = dst[0][1]
        self.notes.add(f"{name}()는 객체 전체 복사/초기화로 모델링(크기 인자 무시)")
        if name in ("memcpy", "memmove"):
            src = self.ptr_targets(args[1])
            if len(src) != 1:
                raise Unsupported(f"{name}: 원본이 하나의 객체가 아님")
            sl = src[0][1]
            for root in {sl.split(FIELD_SEP, 1)[0], dl.split(FIELD_SEP, 1)[0]}:
                if root.startswith("gv:"):
                    self.ensure_global(root)
            keys = [k for k in list(self.mem) if k == sl or k.startswith(sl + FIELD_SEP)]
            for k in keys:
                self.write(dl + k[len(sl):], None, self.mem[k], guard)
            return PtrVal.single(("loc", dl, None))
        val = args[1]
        sv = z3.simplify(val) if z3.is_bv(val) else None
        if sv is None or not z3.is_bv_value(sv) or sv.as_long() != 0:
            raise Unsupported("memset: 0이 아닌 값")
        root = dl.split(FIELD_SEP, 1)[0]
        if root.startswith("gv:"):
            self.ensure_global(root)
        for k in [k for k in list(self.mem) if k == dl or k.startswith(dl + FIELD_SEP)]:
            v = self.mem[k]
            if z3.is_array(v):
                self.write(k, None, z3.K(v.domain(), bv(0, v.range().size())), guard)
            elif z3.is_bv(v):
                self.write(k, None, bv(0, v.size()), guard)
        return PtrVal.single(("loc", dl, None))

    def _call_target(self, fkey: str, args, args_n, guard, rty, n):
        if fkey in self.p.functions:
            return self.call_function(fkey, args, guard)
        # 외부 함수: 공유 미해석 함수
        name = fkey.split(":")[-1]
        if name in self.p.rte_map:
            return self._rte_call(name, self.p.rte_map[name], args, args_n, guard, rty)
        if name in ("memcpy", "memmove", "memset") and args and isinstance(args[0], PtrVal):
            return self._mem_call(name, args, guard)
        cnt = self.ext_count.get(name, 0)
        self.ext_count[name] = cnt + 1
        self.ext_calls.setdefault(name, []).append((guard, args))
        scalar_args = [a for a in args if a is not None and not isinstance(a, PtrVal) and z3.is_bv(a)]
        key_args = scalar_args + [bv(self.step, 16), bv(cnt, 16)]
        for a, an in zip(args, args_n):
            aty = an.get("ty") or {}
            if isinstance(a, PtrVal) and not ((aty.get("pt") or {}).get("const")):
                pt = aty.get("pt") or {}
                if pt.get("k") in ("i", "b"):
                    w = bits_of(pt)
                    uf = z3.Function(f"ext_out:{name}", *[x.sort() for x in key_args], z3.BitVecSort(w))
                    self.store([(c, l, i) for c, l, i in self.ptr_targets(a)], uf(*key_args), guard)
                    self.notes.add(f"외부 함수 {name}의 출력 인자를 미해석 함수로 모델링")
        if rty.get("k") in ("i", "b"):
            uf = z3.Function(f"ext:{name}", *[x.sort() for x in key_args], z3.BitVecSort(bits_of(rty)))
            self.notes.add(f"외부 함수 {name}()를 공유 미해석 함수로 모델링")
            return uf(*key_args)
        if rty.get("k") in ("v", None):
            return None
        raise Unsupported(f"외부 함수 {name}의 반환 자료형 {rty.get('n')}")

    # ---------------- 진입 ----------------
    def run(self, fkey: str, args: List, guard=None):
        g = guard if guard is not None else z3.BoolVal(True)
        return self.call_function(fkey, args, g)


def _walk_cases(n: dict):
    stack = [n]
    while stack:
        x = stack.pop()
        if x.get("k") in ("Case", "Default"):
            yield x
        if x.get("k") == "Switch":
            continue
        for c in x.get("ch", []) or []:
            if c is not None:
                stack.append(c)


def _common_type(a: dict, b: dict) -> dict:
    """정수 승격 + 통상 산술 변환."""
    def promote(t):
        if t.get("k") == "b" or (t.get("k") == "i" and int(t.get("b", 32)) < 32):
            return {"k": "i", "b": 32, "s": True, "n": "int"}
        return t
    a, b = promote(a), promote(b)
    ba, bb = int(a.get("b", 32)), int(b.get("b", 32))
    sa, sb = bool(a.get("s")), bool(b.get("s"))
    if sa == sb:
        return a if ba >= bb else b
    u, s = (a, b) if not sa else (b, a)
    if int(u.get("b", 32)) >= int(s.get("b", 32)):
        return u
    return s
