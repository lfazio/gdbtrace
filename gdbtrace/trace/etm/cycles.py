"""Modèles de cycles approximatifs, par cœur (d'après les tables de temps des TRM d'ARM).

QEMU n'a aucune notion de cycles : sous QEMU, et pour une trace matérielle non
cycle-accurate, la durée des instructions est **estimée** avec ce modèle. Sur matériel en
mode cycle-accurate, les cycles **mesurés** par l'ETM le remplacent. L'interface indique
toujours laquelle des deux sources est utilisée, et quel modèle.

- Cortex-M3/M4 (`CORTEX_M3`, aussi pour M7 et M55) : P = 2 cycles de remplissage du pipeline
  après un branchement pris, division au coût moyen, pas de pipelining LDR/LDR ni de
  repliement d'IT.
- Cortex-M0 (`CORTEX_M0`, ARM DDI 0432C, table 3-1) : B pris 3, BL 4, BX/BLX 3, LDR/STR 2,
  POP {…, PC} 4+N, MRS/MSR et barrières 4. Entrée d'exception : 16 cycles (fiche produit
  Cortex-M0 d'ARM ; le TRM ne la donne pas).
- Cortex-M0+ (`CORTEX_M0P`, ARM DDI 0484C, table 3-1) : pipeline à deux étages, B pris 2,
  BL 3, BX/BLX 2, LDR/STR 2 (1 par le port d'E/S à un cycle, non modélisé), POP {…, PC} 3+N,
  MRS/MSR et barrières 3. Entrée d'exception : 15 cycles (« worst case interrupt latency »).
- Multiplieur rapide (MULS en 1 cycle, celui des STM32F0 et G0) ; sortie d'exception et
  enchaînement des Cortex-M0/M0+ : non documentés, estimés (dépilement comme l'empilement).

Hypothèse commune : mémoire sans état d'attente.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, replace

from .image import DIRECT, INDIRECT, Instr
from .qemu_log import Event, ExcEntry, ExcReturn, Step, TraceGap

P = 2                  # remplissage du pipeline après un branchement pris
EXC_ENTRY = 12         # latence d'entrée d'exception (empilement)
EXC_EXIT = 10          # sortie d'exception (dépilement)
TAIL_CHAIN = 6         # enchaînement d'exceptions sans dépilement
DIV = 7                # SDIV/UDIV : 2 à 12 cycles selon les opérandes, valeur moyenne

_REGLIST = re.compile(r"\{([^}]*)\}")


@dataclass(frozen=True)
class CycleModel:
    """Coûts d'un cœur (cycles, mémoire sans état d'attente)."""
    name: str
    taken: int          # branchement pris : B, B<cc>, BX, BLX, MOV/ADD PC
    bl: int             # BL
    store: int          # STR, STRB, STRH
    pop_pc: int         # supplément d'un POP / LDM qui charge PC
    sys: int            # MRS, MSR, ISB, DSB, DMB
    exc_entry: int      # latence d'entrée d'exception (empilement)
    exc_exit: int       # sortie d'exception (dépilement)
    tail_chain: int     # enchaînement d'exceptions sans dépilement


CORTEX_M3 = CycleModel("Cortex-M3/M4", taken=1 + P, bl=1 + P, store=1, pop_pc=P, sys=1,
                       exc_entry=EXC_ENTRY, exc_exit=EXC_EXIT, tail_chain=TAIL_CHAIN)
CORTEX_M0 = CycleModel("Cortex-M0", taken=3, bl=4, store=2, pop_pc=3, sys=4,
                       exc_entry=16, exc_exit=16, tail_chain=TAIL_CHAIN)
CORTEX_M0P = CycleModel("Cortex-M0+", taken=2, bl=3, store=2, pop_pc=2, sys=3,
                        exc_entry=15, exc_exit=15, tail_chain=TAIL_CHAIN)
MODELS = {0xC20: CORTEX_M0, 0xC60: CORTEX_M0P}


def model_for(core: int | None, default: CycleModel = CORTEX_M3) -> CycleModel:
    """Modèle du cœur détecté (CPUID.PARTNO), sinon `default`."""
    return MODELS.get(core, default) if core is not None else default


def _base(ins: Instr) -> str:
    """Mnémonique de base, sans condition (`popne` → `pop`) ni « .w »."""
    return ins.op or ins.text.split()[0].split(".")[0]


def _nregs(ins: Instr) -> int:
    m = _REGLIST.search(ins.text)
    return len([r for r in m.group(1).split(",") if r.strip()]) if m else 1


def cycles_of(ins: Instr, executed: bool = True, taken: bool = False,
              model: CycleModel = CORTEX_M3) -> int:
    """Cycles d'une instruction selon `model`. `executed` : condition vraie (atome E) ;
    `taken` : branchement effectivement pris (flot non séquentiel)."""
    if not executed:
        return 1
    m = _base(ins)
    if m in ("pop", "ldm", "ldmia", "ldmdb", "ldmfd"):
        return 1 + _nregs(ins) + (model.pop_pc if taken else 0)
    if m in ("push", "stm", "stmia", "stmdb", "stmea"):
        return 1 + _nregs(ins)
    if m in ("tbb", "tbh"):
        return 2 + P
    if ins.kind in (DIRECT, INDIRECT):
        if m.startswith("ldr"):            # LDR PC, ...
            return 2 + P
        if m == "bl":
            return model.bl
        return model.taken if taken else 1
    if m in ("ldrd",):
        return 3
    if m.startswith("ldr") or m.startswith("ldrex"):
        return 2
    if m in ("strd",):
        return 2
    if m in ("str", "strb", "strh"):
        return model.store
    if m in ("mrs", "msr", "isb", "dsb", "dmb"):
        return model.sys
    if m in ("sdiv", "udiv"):
        return DIV
    if m in ("mla", "mls"):
        return 2
    return 1                                # ALU, MUL, IT, NOP...


def successors(evs: list[Event]) -> dict[int, int]:
    """Pour chaque instruction (indice de Step) : adresse de l'instruction suivante dans le
    même contexte (après le retour de l'exception qui l'interrompt, le cas échéant)."""
    succ: dict[int, int] = {}
    last: int | None = None
    stack: list[int | None] = []
    for i, ev in enumerate(evs):
        if isinstance(ev, Step):
            if last is not None:
                succ[last] = ev.addr
            last = i
        elif isinstance(ev, ExcEntry):
            if not ev.tailchain:
                stack.append(last)
            last = None
        elif isinstance(ev, ExcReturn):
            last = stack.pop() if stack else None
        elif isinstance(ev, TraceGap):              # trace interrompue : contexte perdu
            if last is not None and ev.next_addr is not None:
                succ[last] = ev.next_addr
            last, stack = None, []
    return succ


def outcome(ins: Instr, addr: int, nxt: int | None) -> tuple[bool, bool]:
    """(exécutée, branchement pris) d'une instruction d'après l'adresse suivante."""
    seq = addr + ins.size
    if ins.kind == DIRECT:
        if not ins.cond:
            # Toujours pris. On ne se fie pas au successeur : sous un RTOS, une exception
            # juste après peut changer de tâche, et « l'instruction suivante dans le même
            # contexte » appartient alors à une autre tâche.
            return True, True
        taken = nxt == ins.target if nxt is not None else False
        return taken, taken
    if ins.kind == INDIRECT:
        if ins.cond and nxt == seq:
            return False, False
        return True, True
    return True, False


def annotate(events: list[Event], image, model: CycleModel = CORTEX_M3) -> list[Event]:
    """Vérité terrain temporelle : chaque événement reçoit ses cycles selon le modèle."""
    succ = successors(events)
    out: list[Event] = []
    for i, ev in enumerate(events):
        if isinstance(ev, Step):
            ins = image.instr(ev.addr)
            executed, taken = outcome(ins, ev.addr, succ.get(i))
            out.append(replace(ev, cycles=cycles_of(ins, executed, taken, model)))
        elif isinstance(ev, ExcEntry):
            out.append(replace(ev, cycles=model.tail_chain if ev.tailchain else model.exc_entry))
        elif isinstance(ev, ExcReturn):
            out.append(replace(ev, cycles=model.exc_exit))
        else:
            out.append(ev)                          # TraceGap : durée inconnue
    return out
