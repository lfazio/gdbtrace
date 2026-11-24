"""Modèle de cycles approximatif Cortex-M3/M4 (d'après les tables de temps des TRM).

QEMU n'a aucune notion de cycles : sous QEMU, et pour une trace matérielle non
cycle-accurate, la durée des instructions est **estimée** avec ce modèle. Sur matériel en
mode cycle-accurate, les cycles **mesurés** par l'ETM le remplacent. L'interface indique
toujours laquelle des deux sources est utilisée.

Hypothèses : mémoire sans état d'attente, P = 2 cycles de remplissage du pipeline après un
branchement pris, division au coût moyen, pas de pipelining LDR/LDR ni de repliement d'IT.
"""
from __future__ import annotations

import re
from dataclasses import replace

from .image import DIRECT, INDIRECT, Instr
from .qemu_log import Event, ExcEntry, ExcReturn, Step, TraceGap

P = 2                  # remplissage du pipeline après un branchement pris
EXC_ENTRY = 12         # latence d'entrée d'exception (empilement)
EXC_EXIT = 10          # sortie d'exception (dépilement)
TAIL_CHAIN = 6         # enchaînement d'exceptions sans dépilement
DIV = 7                # SDIV/UDIV : 2 à 12 cycles selon les opérandes, valeur moyenne

_REGLIST = re.compile(r"\{([^}]*)\}")


def _base(ins: Instr) -> str:
    """Mnémonique de base, sans condition (`popne` → `pop`) ni « .w »."""
    return ins.op or ins.text.split()[0].split(".")[0]


def _nregs(ins: Instr) -> int:
    m = _REGLIST.search(ins.text)
    return len([r for r in m.group(1).split(",") if r.strip()]) if m else 1


def cycles_of(ins: Instr, executed: bool = True, taken: bool = False) -> int:
    """Cycles d'une instruction. `executed` : condition vraie (atome E) ;
    `taken` : branchement effectivement pris (flot non séquentiel)."""
    if not executed:
        return 1
    m = _base(ins)
    if m in ("pop", "ldm", "ldmia", "ldmdb", "ldmfd"):
        return 1 + _nregs(ins) + (P if taken else 0)
    if m in ("push", "stm", "stmia", "stmdb", "stmea"):
        return 1 + _nregs(ins)
    if m in ("tbb", "tbh"):
        return 2 + P
    if ins.kind in (DIRECT, INDIRECT):
        if m.startswith("ldr"):            # LDR PC, ...
            return 2 + P
        return 1 + P if taken else 1
    if m in ("ldrd",):
        return 3
    if m.startswith("ldr") or m.startswith("ldrex"):
        return 2
    if m in ("strd",):
        return 2
    if m in ("sdiv", "udiv"):
        return DIV
    if m in ("mla", "mls"):
        return 2
    return 1                                # ALU, STR, MUL, IT, NOP...


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


def annotate(events: list[Event], image) -> list[Event]:
    """Vérité terrain temporelle : chaque événement reçoit ses cycles selon le modèle."""
    succ = successors(events)
    out: list[Event] = []
    for i, ev in enumerate(events):
        if isinstance(ev, Step):
            ins = image.instr(ev.addr)
            executed, taken = outcome(ins, ev.addr, succ.get(i))
            out.append(replace(ev, cycles=cycles_of(ins, executed, taken)))
        elif isinstance(ev, ExcEntry):
            out.append(replace(ev, cycles=TAIL_CHAIN if ev.tailchain else EXC_ENTRY))
        elif isinstance(ev, ExcReturn):
            out.append(replace(ev, cycles=EXC_EXIT))
        else:
            out.append(ev)                          # TraceGap : durée inconnue
    return out
