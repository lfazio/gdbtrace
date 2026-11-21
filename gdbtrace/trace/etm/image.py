"""Image du code (segments chargés de l'ELF) et classification des instructions Thumb-2.

La reconstruction du flot ETM a besoin, pour chaque instruction : sa taille, si c'est un
branchement direct (cible connue d'après l'opcode) ou indirect (cible donnée par la trace),
et si elle est conditionnelle. Le désassemblage est fait par capstone, avec un cache.
"""
from __future__ import annotations

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


class CodeImage:
    """Mémoire en lecture seule des segments PT_LOAD exécutables (et données) de l'ELF."""

    def __init__(self, elf_path: str):
        if capstone is None:
            raise RuntimeError("le module capstone est requis : pip install capstone")
        self.path = elf_path
        self.segments: list[tuple[int, bytes]] = []
        with open(elf_path, "rb") as fh:
            elf = ELFFile(fh)
            for seg in elf.iter_segments():
                if seg["p_type"] == "PT_LOAD" and seg["p_filesz"]:
                    self.segments.append((seg["p_vaddr"], seg.data()))
        self.segments.sort()
        self.cs = capstone.Cs(capstone.CS_ARCH_ARM, capstone.CS_MODE_THUMB | capstone.CS_MODE_MCLASS)
        self.cs.detail = True
        self._cache: dict[int, Instr | None] = {}

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
        if addr in self._cache:
            return self._cache[addr]
        code = self.read(addr, 4) or self.read(addr, 2)
        info = None
        if code is not None:
            for ins in self.cs.disasm(code, addr, 1):
                info = _classify(ins)
        self._cache[addr] = info
        return info


def _classify(ins) -> Instr:
    """Classement d'après les groupes capstone (pas d'après le mnémonique : `bic` commence
    aussi par « b »).
    - direct : groupe `branch_relative` (B, B<c>, BL, CBZ, CBNZ), cible = opérande immédiat ;
    - indirect : groupe `jump`/`call` ou écriture de PC sans être relatif (BX, BLX Rm,
      POP {…, PC}, LDR PC, TBB/TBH, MOV PC…) : la cible n'est connue que par la trace."""
    groups = set(ins.groups)
    mnem = ins.mnemonic.split(".")[0]
    ops = ins.operands
    writes_pc = cs_arm.ARM_REG_PC in ins.regs_write or any(
        op.type == cs_arm.ARM_OP_REG and op.reg == cs_arm.ARM_REG_PC and op.access & capstone.CS_AC_WRITE
        for op in ops)
    call = capstone.CS_GRP_CALL in groups
    cond = ins.cc not in (cs_arm.ARM_CC_AL, cs_arm.ARM_CC_INVALID) or mnem in ("cbz", "cbnz")
    kind, target, ret = NORMAL, None, False
    if capstone.CS_GRP_BRANCH_RELATIVE in groups:
        imm = [op.imm for op in ops if op.type == cs_arm.ARM_OP_IMM]
        kind, target = DIRECT, imm[-1] & 0xFFFFFFFF
    elif capstone.CS_GRP_JUMP in groups or call or writes_pc:
        kind = INDIRECT
        if mnem == "bx" and ops and ops[0].type == cs_arm.ARM_OP_REG and ops[0].reg == cs_arm.ARM_REG_LR:
            ret = True
        elif mnem in ("pop", "ldm", "ldmia") and writes_pc:
            ret = True
    return Instr(ins.address, ins.size, kind, target, cond, call, ret,
                 f"{ins.mnemonic} {ins.op_str}".strip())
