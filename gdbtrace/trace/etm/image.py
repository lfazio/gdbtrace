"""Image du code (segments chargés de l'ELF) et classification des instructions Thumb-2.

La reconstruction du flot ETM a besoin, pour chaque instruction : sa taille, si c'est un
branchement direct (cible connue d'après l'opcode) ou indirect (cible donnée par la trace),
et si elle est conditionnelle. Le désassemblage est fait par capstone, avec un cache.

Blocs IT (Thumb-2) : la condition d'une instruction placée après un `it` n'est pas dans son
propre encodage (`bxeq lr` s'encode comme `bx lr`). Décodée seule, elle paraît
inconditionnelle. Chaque fonction est donc balayée d'un seul tenant (capstone suit l'état
IT), une fois, à la première demande d'une de ses adresses, d'après la table des symboles
(fonctions, zones de données `$d`) ; les instructions couvertes par un IT gardent leur vraie
condition.
"""
from __future__ import annotations

import bisect
import threading
from dataclasses import dataclass

from elftools.elf.elffile import ELFFile

try:
    import capstone
    from capstone import arm as cs_arm
except ImportError:  # pragma: no cover - dépendance optionnelle (pip install capstone)
    capstone = None


NORMAL, DIRECT, INDIRECT = "normal", "direct", "indirect"


@dataclass(frozen=True)
class Instr:
    addr: int
    size: int
    kind: str                     # normal, direct, indirect
    target: int | None            # cible d'un branchement direct
    cond: bool                    # exécution conditionnelle (B<c>, CBZ...)
    call: bool                    # BL / BLX (appel de fonction)
    ret: bool                     # BX LR, POP {..., PC}... (retour probable)
    text: str                     # désassemblage
    op: str = ""                  # mnémonique de base, sans condition ni « .w » (bx, pop, ldr…)
    isb: bool = False             # ISB : point de passage (élément P0) de l'ETMv4

    @property
    def waypoint(self) -> bool:
        """Élément P0 de l'ETMv4 : tout branchement, et ISB (comme OpenCSD : DSB, DMB, WFI et
        WFE n'en sont pas sur un ETMv4.0)."""
        return self.kind != NORMAL or self.isb


class CodeImage:
    """Mémoire en lecture seule des segments PT_LOAD exécutables (et données) de l'ELF."""

    def __init__(self, elf_path: str):
        if capstone is None:
            raise RuntimeError("le module capstone est requis : pip install capstone")
        self.path = elf_path
        self.segments: list[tuple[int, bytes]] = []
        ends: dict[int, int] = {}                 # fonctions : début → fin (alias fusionnés)
        marks: list[tuple[int, str]] = []         # symboles de correspondance : t, a ou d
        with open(elf_path, "rb") as fh:
            elf = ELFFile(fh)
            for seg in elf.iter_segments():
                if seg["p_type"] == "PT_LOAD" and seg["p_filesz"]:
                    self.segments.append((seg["p_vaddr"], seg.data()))
            symtab = elf.get_section_by_name(".symtab")
            for sym in symtab.iter_symbols() if symtab is not None else ():
                addr, name = sym["st_value"] & ~1, sym.name
                if sym["st_info"]["type"] == "STT_FUNC" and sym["st_size"]:
                    ends[addr] = max(ends.get(addr, 0), addr + sym["st_size"])
                elif name[:2] in ("$t", "$a", "$d") and (len(name) == 2 or name[2] == "."):
                    marks.append((addr, name[1]))
        self.segments.sort()
        self._init_disasm(ends, marks)

    @classmethod
    def from_segments(cls, segments: list[tuple[int, bytes]], name: str = "mémoire") -> "CodeImage":
        """Image faite de copies de la mémoire de la cible [(adresse, octets)] : décoder une
        trace quand l'ELF ne correspond pas au code exécuté (autre build flashé, code généré
        à l'exécution). Sans table des symboles, pas de balayage des blocs IT : chaque
        instruction est décodée seule (une instruction conditionnée par un IT paraît
        inconditionnelle)."""
        if capstone is None:
            raise RuntimeError("le module capstone est requis : pip install capstone")
        img = cls.__new__(cls)
        img.path = name
        img.segments = sorted((base, bytes(data)) for base, data in segments)
        img._init_disasm({}, [])
        return img

    def _init_disasm(self, ends: dict[int, int], marks: list[tuple[int, str]]) -> None:
        self.cs = capstone.Cs(capstone.CS_ARCH_ARM, capstone.CS_MODE_THUMB | capstone.CS_MODE_MCLASS)
        self.cs.detail = True
        self._cache: dict[int, Instr | None] = {}
        self._funcs = sorted(ends.items())
        self._func_starts = [a for a, _ in self._funcs]
        marks.sort()
        self._marks = marks
        self._mark_addrs = [a for a, _ in marks]
        self._swept: set[int] = set()
        self._it: dict[int, Instr] = {}           # instructions couvertes par un IT
        # décodage (fil ETM) et vues (thread GUI, précalcul de la couverture) se partagent
        # l'image : capstone n'est pas réentrant, et un balayage en cours n'est pas fini
        self._lock = threading.Lock()

    def read(self, addr: int, n: int) -> bytes | None:
        for base, data in self.segments:
            if base <= addr and addr + n <= base + len(data):
                return data[addr - base:addr - base + n]
        return None

    def contains(self, addr: int) -> bool:
        return self.read(addr, 2) is not None

    def instr(self, addr: int) -> Instr | None:
        """Instruction à `addr` (bit Thumb ignoré), ou None si hors image / indécodable."""
        addr &= ~1
        try:
            return self._cache[addr]
        except KeyError:
            pass
        with self._lock:
            if addr in self._cache:
                return self._cache[addr]
            self._sweep_function(addr)
            info = self._it.get(addr)
            if info is None:
                code = self.read(addr, 4) or self.read(addr, 2)
                if code is not None:
                    for ins in self.cs.disasm(code, addr, 1):
                        info = _classify(ins)
            self._cache[addr] = info
            return info

    def _sweep_function(self, addr: int) -> None:
        """Balaie une fois la fonction qui contient `addr` : condition réelle des
        instructions placées dans un bloc IT."""
        i = bisect.bisect_right(self._func_starts, addr) - 1
        if i < 0:
            return
        start, end = self._funcs[i]
        if not start <= addr < end or start in self._swept:
            return
        self._swept.add(start)
        for a, b in self._code_runs(start, end):
            code = self.read(a, b - a)
            if not code:
                continue
            left = 0
            for ins in self.cs.disasm(code, a):
                if left:
                    self._it[ins.address] = _classify(ins)
                left = len(ins.mnemonic) - 1 if ins.id == cs_arm.ARM_INS_IT else max(0, left - 1)

    def _code_runs(self, start: int, end: int) -> list[tuple[int, int]]:
        """Plages de code de [start, end) : sans les zones de données (`$d` … `$t`)."""
        j = bisect.bisect_right(self._mark_addrs, start) - 1
        data = j >= 0 and self._marks[j][1] == "d"
        runs, cur = [], start
        for a, kind in self._marks[j + 1:]:
            if a >= end:
                break
            if kind == "d" and not data:
                if a > cur:
                    runs.append((cur, a))
                data = True
            elif kind != "d" and data:
                cur, data = a, False
        if not data and end > cur:
            runs.append((cur, end))
        return runs


def _classify(ins) -> Instr:
    """Classement d'après les groupes capstone (pas d'après le mnémonique : `bic` commence
    aussi par « b »).
    - direct : groupe `branch_relative` (B, B<c>, BL, CBZ, CBNZ), cible = opérande immédiat ;
    - indirect : groupe `jump`/`call` ou écriture de PC sans être relatif (BX, BLX Rm,
      POP {…, PC}, LDR PC, TBB/TBH, MOV PC…) : la cible n'est connue que par la trace."""
    groups = set(ins.groups)
    op = ins.insn_name() or ins.mnemonic.split(".")[0]   # sans condition : `bxeq` → `bx`
    ops = ins.operands
    writes_pc = cs_arm.ARM_REG_PC in ins.regs_write or any(
        op.type == cs_arm.ARM_OP_REG and op.reg == cs_arm.ARM_REG_PC and op.access & capstone.CS_AC_WRITE
        for op in ops)
    call = capstone.CS_GRP_CALL in groups
    cond = ins.cc not in (cs_arm.ARM_CC_AL, cs_arm.ARM_CC_INVALID) or op in ("cbz", "cbnz")
    kind, target, ret = NORMAL, None, False
    if capstone.CS_GRP_BRANCH_RELATIVE in groups:
        imm = [op.imm for op in ops if op.type == cs_arm.ARM_OP_IMM]
        kind, target = DIRECT, imm[-1] & 0xFFFFFFFF
    elif capstone.CS_GRP_JUMP in groups or call or writes_pc:
        kind = INDIRECT
        if op == "bx" and ops and ops[0].type == cs_arm.ARM_OP_REG and ops[0].reg == cs_arm.ARM_REG_LR:
            ret = True
        elif op in ("pop", "ldm", "ldmia") and writes_pc:
            ret = True
    return Instr(ins.address, ins.size, kind, target, cond, call, ret,
                 f"{ins.mnemonic} {ins.op_str}".strip(), op, isb=op == "isb")
