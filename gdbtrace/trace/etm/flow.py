"""Reconstruction du flot d'exécution à partir des paquets ETMv3 et de l'image du code.

Pour chaque atome, l'instruction à l'adresse courante est lue dans l'ELF : sa taille et son
type donnent l'adresse suivante (branchement direct pris → cible de l'opcode ; branchement
indirect exécuté → adresse attendue dans le prochain paquet). Le résultat a la même forme
que le chemin tiré du journal QEMU (`qemu_log`) : Step / ExcEntry / ExcReturn.
"""
from __future__ import annotations

from dataclasses import dataclass

from .etm3 import Atoms, Branch, Etm3Decoder, EtmPacket, ExceptionExit, ISync
from .image import DIRECT, INDIRECT, CodeImage
from .qemu_log import Event, ExcEntry, ExcReturn, Step


@dataclass
class FlowStats:
    instructions: int = 0
    not_executed: int = 0        # atomes N (condition fausse)
    lost_atoms: int = 0          # atomes reçus sans adresse connue
    unknown_code: int = 0        # adresse hors de l'image (code non chargé, désynchro)


class FlowDecoder:
    def __init__(self, image: CodeImage):
        self.image = image
        self.addr: int | None = None
        self.stats = FlowStats()

    def feed(self, packets: list[EtmPacket]) -> list[Event]:
        out: list[Event] = []
        for p in packets:
            if isinstance(p, ISync):
                self.addr = p.addr
            elif isinstance(p, Branch):
                if p.exception is not None:
                    out.append(ExcEntry(p.exception, p.addr))
                self.addr = p.addr
            elif isinstance(p, ExceptionExit):
                out.append(ExcReturn())
            elif isinstance(p, Atoms):
                for atom in p.atoms:
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
        out.append(Step(self.addr))
        self.stats.instructions += 1
        if atom == "N":
            self.stats.not_executed += 1
        nxt: int | None = self.addr + ins.size
        if atom == "E":
            if ins.kind == DIRECT:
                nxt = ins.target
            elif ins.kind == INDIRECT:
                nxt = None              # adresse dans le prochain paquet
        self.addr = nxt


def decode(data: bytes, image: CodeImage) -> tuple[list[Event], Etm3Decoder, FlowDecoder]:
    pk = Etm3Decoder()
    fl = FlowDecoder(image)
    events = fl.feed(pk.feed(data))
    return events, pk, fl
