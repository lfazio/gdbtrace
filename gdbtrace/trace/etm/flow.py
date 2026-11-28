"""Reconstruction du flot d'exécution à partir des paquets ETMv3 et de l'image du code.

Pour chaque atome, l'instruction à l'adresse courante est lue dans l'ELF : sa taille et son
type donnent l'adresse suivante (branchement direct pris → cible de l'opcode ; branchement
indirect exécuté → adresse attendue dans le prochain paquet). Le résultat a la même forme
que le chemin tiré du journal QEMU (`qemu_log`) : Step / ExcEntry / ExcReturn.

Temps (`cycles` de chaque événement) :
- trace cycle-accurate : cycles **mesurés** ; les cycles sans instruction (format 3 sans
  atome, paquets de cycles) s'ajoutent à l'événement suivant (latence d'exception…) ;
- sinon : cycles **estimés** par le modèle Cortex-M3/M4 (`cycles.py`).

Filtre par le DWT (ETMv3 des M3/M4) : l'événement TraceEnable est l'égalité de PC d'un
comparateur du DWT, vraie seulement aux cycles où une instruction du bloc s'exécute ; après
un branchement (cycles de remplissage du pipeline), la première instruction échappe à la
trace, puis un I-sync « trace activée » reprend à la suivante (la reprise peut tomber juste
après le bloc : l'événement est en retard d'une instruction). `heal_ranges` (les blocs
tracés) : un tel trou est comblé quand l'image dit sans ambiguïté ce qui s'est exécuté entre
les deux — depuis l'adresse attendue, des instructions du bloc, ordinaires et
inconditionnelles, jusqu'à l'adresse de reprise. Sinon (exécution hors du bloc, branchement
indirect ou conditionnel) : trou.

Instruction annulée : une adresse de branchement d'exception dont le bit Cancel est à 1 dit
que la dernière instruction tracée n'a pas été exécutée (interrompue : le cœur la reprendra
au retour). La dernière instruction d'un atome est donc retenue jusqu'au paquet suivant
(`_held`), qui l'émet ou l'annule ; `flush()` l'émet en fin de flux (arrêt de la cible).
Constaté sur STM32F446 : un SysTick sur un `str` du bloc filtré, retiré par OpenCSD.
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
    healed: int = 0              # trous comblés (instructions du bloc perdues par le filtre)
    overflows: int = 0           # reprises après débordement de la FIFO de l'ETM (I-sync 2)
    cancelled: int = 0           # instructions annulées par une exception (bit Cancel)


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
        #: blocs tracés par un filtre du DWT ; None : aucun trou n'est comblé
        self.heal_ranges: list[tuple[int, int]] | None = None
        #: dernière instruction d'un atome, émise au paquet suivant sauf s'il l'annule
        self._held: Step | None = None

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
            if isinstance(p, Branch) and p.cancel and self._held is not None:
                self._cancel()
            elif not isinstance(p, Atoms):
                self._release(out)
            if isinstance(p, ISync):
                # trace réactivée (filtre) ou reprise après débordement : un trou dans le chemin
                if self.synced and p.reason in (1, 2):
                    if p.reason == 2:
                        self.stats.overflows += 1
                    if p.reason == 1 and self._heal(p.addr, out):
                        self.stats.healed += 1
                    else:
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

    def flush(self) -> list[Event]:
        """Fin du flux (arrêt de la cible, fin d'une capture) : la dernière instruction,
        retenue jusqu'au paquet suivant, est émise."""
        out: list[Event] = []
        self._release(out)
        return out

    def _release(self, out: list[Event]) -> None:
        if self._held is not None:
            out.append(self._held)
            self._held = None

    def _cancel(self) -> None:
        """La dernière instruction tracée, annulée par l'exception qui suit, n'est pas
        émise ; ses cycles mesurés reviennent à l'événement suivant (l'entrée d'exception)."""
        s, self._held = self._held, None
        self.stats.instructions -= 1
        self.stats.cycles -= s.cycles
        if not s.executed:
            self.stats.not_executed -= 1
        if self.cycle_accurate:
            self.pending += s.cycles
        self.stats.cancelled += 1
        self.last_was_return = False

    MAX_HEAL = 8

    def _heal(self, resume: int, out: list[Event]) -> bool:
        """Trou du filtre du DWT comblé (voir l'en-tête) : vrai si les instructions entre
        l'adresse attendue et `resume` sont connues ; elles sont alors ajoutées à `out`."""
        if not self.heal_ranges or self.addr is None:
            return False
        inside = lambda a: any(lo <= a < hi for lo, hi in self.heal_ranges)  # noqa: E731
        missing, a = [], self.addr
        while a != resume:
            ins = self.image.instr(a)
            if (ins is None or len(missing) == self.MAX_HEAL or not inside(a) or ins.cond
                    or ins.kind == INDIRECT or (ins.kind == DIRECT and ins.call)):
                return False
            missing.append(ins)
            a = ins.target if ins.kind == DIRECT else a + ins.size
        for ins in missing:                     # exécutées : cycles du modèle
            taken = ins.kind == DIRECT
            c = cycles_of(ins, True, taken)
            out.append(Step(ins.addr, cycles=c, executed=True))
            self.stats.instructions += 1
            self.stats.cycles += c
            self.last_was_return = False
        return True

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
        self._release(out)
        self._held = Step(self.addr, cycles=c, executed=executed)
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
    events += fl.flush()
    return events, pk, fl
