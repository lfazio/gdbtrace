"""Reconstruction du flot d'exécution à partir des paquets ETMv3 et de l'image du code.

Pour chaque atome, l'instruction à l'adresse courante est lue dans l'ELF : sa taille et son
type donnent l'adresse suivante (branchement direct pris → cible de l'opcode ; branchement
indirect exécuté → adresse attendue dans le prochain paquet). Le résultat a la même forme
que le chemin tiré du journal QEMU (`qemu_log`) : Step / ExcEntry / ExcReturn.

Temps (`cycles` de chaque événement) :
- trace cycle-accurate : cycles **mesurés** ; les cycles sans instruction (format 3 sans
  atome, paquets de cycles) s'ajoutent à l'événement suivant (latence d'exception…) ;
- sinon : cycles **estimés** par le modèle Cortex-M3/M4 (`cycles.py`).
"""
from __future__ import annotations

from dataclasses import dataclass

from .cycles import EXC_ENTRY, EXC_EXIT, TAIL_CHAIN, cycles_of
from .etm3 import Atoms, Branch, Etm3Decoder, EtmPacket, ExceptionExit, ISync, Wait
from .image import DIRECT, INDIRECT, CodeImage
from .qemu_log import Event, ExcEntry, ExcReturn, Step, TraceGap


@dataclass
class FlowStats:
    instructions: int = 0
    not_executed: int = 0        # atomes N (condition fausse)
    lost_atoms: int = 0          # atomes reçus sans adresse connue
    unknown_code: int = 0        # adresse hors de l'image (code non chargé, désynchro)
    cycles: int = 0              # total des cycles (mesurés ou estimés)
    gaps: int = 0                # trous émis (TraceGap)


class FlowDecoder:
    def __init__(self, image: CodeImage, cycle_accurate: bool = False):
        self.image = image
        self.cycle_accurate = cycle_accurate
        self.addr: int | None = None
        self.pending = 0             # cycles mesurés pas encore attribués
        self.stats = FlowStats()
        self.exc_depth = 0           # exceptions en cours (imbrication)
        self.last_was_return = False # dernier événement : instruction de retour exécutée
        self.synced = False          # un premier I-sync a été reçu

    @property
    def timing(self) -> str:
        return "mesuré (ETM cycle-accurate)" if self.cycle_accurate else "estimé (modèle de cycles)"

    def _take(self, estimate: int) -> int:
        if not self.cycle_accurate:
            return estimate
        c, self.pending = self.pending, 0
        return c

    def feed(self, packets: list[EtmPacket]) -> list[Event]:
        out: list[Event] = []
        for p in packets:
            if isinstance(p, ISync):
                # trace réactivée (filtre) ou reprise après débordement : un trou dans le chemin
                if self.synced and p.reason in (1, 2):
                    out.append(TraceGap())
                    self.stats.gaps += 1
                    self.exc_depth, self.last_was_return = 0, False
                self.synced = True
                self.addr = p.addr
                self.pending += p.cycles
            elif isinstance(p, Wait):
                self.pending += p.cycles
            elif isinstance(p, Branch):
                if p.exception is not None:
                    # Enchaînement (tail-chaining) : nouvelle exception juste après le retour
                    # d'un handler, sans sortie d'exception (0x76) entre les deux.
                    tail = self.exc_depth > 0 and self.last_was_return
                    c = self._take(TAIL_CHAIN if tail else EXC_ENTRY)
                    self.stats.cycles += c
                    out.append(ExcEntry(p.exception, p.addr, tailchain=tail, cycles=c))
                    if not tail:
                        self.exc_depth += 1
                    self.last_was_return = False
                self.addr = p.addr
            elif isinstance(p, ExceptionExit):
                c = self._take(EXC_EXIT)
                self.stats.cycles += c
                out.append(ExcReturn(cycles=c))
                self.exc_depth = max(0, self.exc_depth - 1)
                self.last_was_return = False
            elif isinstance(p, Atoms):
                for k, atom in enumerate(p.atoms):
                    if self.cycle_accurate and k < len(p.cycles):
                        self.pending += p.cycles[k]
                    self._atom(atom, out)
        return out

    def _atom(self, atom: str, out: list[Event]) -> None:
        if self.addr is None:
            self.stats.lost_atoms += 1
            return
        ins = self.image.instr(self.addr)
        if ins is None:
            self.stats.unknown_code += 1
            self.addr = None
            return
        executed = atom == "E"
        taken = executed and ins.kind in (DIRECT, INDIRECT)
        c = self._take(cycles_of(ins, executed, taken))
        out.append(Step(self.addr, cycles=c, executed=executed))
        self.stats.instructions += 1
        self.stats.cycles += c
        if not executed:
            self.stats.not_executed += 1
        self.last_was_return = executed and ins.ret
        nxt: int | None = self.addr + ins.size
        if executed:
            if ins.kind == DIRECT:
                nxt = ins.target
            elif ins.kind == INDIRECT:
                nxt = None              # adresse dans le prochain paquet
        self.addr = nxt


def decode(data: bytes, image: CodeImage,
           cycle_accurate: bool = False) -> tuple[list[Event], Etm3Decoder, FlowDecoder]:
    pk = Etm3Decoder(cycle_accurate=cycle_accurate)
    fl = FlowDecoder(image, cycle_accurate)
    events = fl.feed(pk.feed(data))
    return events, pk, fl
