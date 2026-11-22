"""Reconstruction du flot d'exécution à partir des paquets ETMv4 (profil M) et du code.

Un atome ETMv4 ne désigne plus une instruction mais un **point de passage** (élément P0 :
branchement ou ISB). Pour chaque atome, on parcourt le code depuis l'adresse courante
jusqu'au prochain point de passage : les instructions rencontrées ont été exécutées, et
l'atome dit si le point de passage a été pris (E) ou non (N).

Exceptions : le paquet Exception est suivi de l'**adresse de retour préférée**, qui borne
les instructions exécutées avant l'interruption depuis le dernier point de passage, puis de
l'adresse du handler. Retour d'exception (0x07) : élément P0 en ARMv7-M ; l'adresse de
reprise suit.

Spéculation (comme OpenCSD) : les éléments arrivent dans une file et ne sont traités qu'une
fois validés : par un paquet Commit, ou implicitement dès que la profondeur dépasse
TRCIDR8.MAXSPEC (1 sur un ETM-M7). Cancel retire les plus récents, Mispredict inverse le
dernier atome ; Overflow et Discard vident la file. `flush()` valide le reste (fin de trace).

Après une synchronisation, Trace Info indique combien d'éléments émis **avant** elle sont
encore spéculatifs (champ SPEC) : invisibles pour le décodeur, ils sont pourtant comptés par
les Commit et Cancel qui suivent. Un Commit les consomme en premier (sans effet), un Cancel
en dernier. (OpenCSD 1.4.1 ne les compte pas pour Commit : sur une vraie trace d'ETM-M7, il
déborde à chaque synchronisation.)

Le résultat a la même forme que le chemin tiré du journal QEMU : Step / ExcEntry / ExcReturn,
avec des cycles **estimés** par le modèle Cortex-M (`cycles.py`).
"""
from __future__ import annotations

from dataclasses import dataclass

from .cycles import EXC_ENTRY, EXC_EXIT, TAIL_CHAIN, cycles_of
from .etm4 import (Address, ASync, Atoms, Cancel, Commit, Context, Discard, Etm4Decoder,
                   Etm4Packet, ExceptionPacket, ExceptionReturn, Mispredict, Overflow, TraceInfo,
                   TraceOn)
from .image import DIRECT, INDIRECT, CodeImage
from .qemu_log import Event, ExcEntry, ExcReturn, Step

#: instructions parcourues au plus sans point de passage (au-delà : désynchronisation)
MAX_RUN = 1 << 16


@dataclass
class Flow4Stats:
    instructions: int = 0
    not_executed: int = 0        # atomes N (branchement non pris)
    lost_atoms: int = 0          # atomes reçus sans adresse ni contexte connus
    unknown_code: int = 0        # adresse hors de l'image (code non chargé, désynchro)
    cycles: int = 0              # total des cycles estimés
    overflows: int = 0           # débordements de la FIFO de l'ETM
    errors: int = 0              # séquences impossibles (exception sans adresse...)


class Etm4Flow:
    def __init__(self, image: CodeImage, max_spec: int = 1):
        self.image = image
        self.max_spec = max_spec
        self.cycle_accurate = False
        self.stats = Flow4Stats()
        self._reset()

    @property
    def timing(self) -> str:
        return "estimé (modèle de cycles)"

    def _reset(self) -> None:
        self.queue: list[list] = []          # éléments en attente, le plus ancien en tête
        self.depth = 0                       # éléments P0 non validés (invisibles compris)
        self.unseen = 0                      # spéculatifs émis avant la synchro (Trace Info)
        self.fresh = True                    # pas encore de Trace Info depuis la synchro
        self.exc_waits_addr = False          # exception reçue, son adresse pas encore
        self.addr: int | None = None         # instruction courante (None : adresse attendue)
        self.need_ctx = True
        self.handler: tuple[int, bool, int] | None = None   # exception validée, handler attendu
        self.exc_depth = 0
        self.last_was_return = False

    # ------------------------------------------------------------------ paquets
    def feed(self, packets: list[Etm4Packet]) -> list[Event]:
        out: list[Event] = []
        for p in packets:
            commit, cancel, mispredict, discard = 0, 0, False, False
            if isinstance(p, ASync):
                if p.resync:
                    self._reset()
                continue
            if isinstance(p, TraceInfo):
                if self.fresh:                     # les suivantes n'apprennent rien
                    self.unseen = p.spec
                    self.depth += p.spec
                    self.fresh = False
                continue
            if isinstance(p, Atoms):
                self._push_atoms(p.atoms)
            elif isinstance(p, Address):
                self.queue.append(["addr", p.addr, p.context])
                if self.exc_waits_addr:            # l'exception devient un élément P0 complet
                    self.depth += 1
                    self.exc_waits_addr = False
            elif isinstance(p, Context):
                self.queue.append(["ctx", p.updated])
            elif isinstance(p, ExceptionPacket):
                self.queue.append(["exc", p])
                self.exc_waits_addr = True
            elif isinstance(p, ExceptionReturn):
                self.queue.append(["eret"])
                self.depth += 1
            elif isinstance(p, TraceOn):
                self.queue.append(["on"])
            elif isinstance(p, Commit):
                commit = p.count
            elif isinstance(p, Cancel):
                if p.atoms:
                    self._push_atoms(p.atoms)
                cancel, mispredict = p.count, p.mispredict
            elif isinstance(p, Mispredict):
                if p.atoms:
                    self._push_atoms(p.atoms)
                mispredict = True
            elif isinstance(p, (Overflow, Discard)):
                discard = True
                if isinstance(p, Overflow):
                    self.stats.overflows += 1
            else:
                continue                           # cycles, horodatage, événements
            if self.depth > self.max_spec:         # validation implicite au-delà de MAXSPEC
                commit = max(commit, self.depth - self.max_spec)
            # même ordre de résolution qu'OpenCSD
            if commit:
                self._commit(commit, out)
            if cancel:
                self._cancel(cancel)
            if mispredict:
                self._mispredict()
            if discard:
                self._reset()
        return out

    def flush(self) -> list[Event]:
        """Fin de trace : valide tous les éléments encore spéculatifs."""
        out: list[Event] = []
        self._commit(1 << 30, out)
        return out

    def _push_atoms(self, atoms: str) -> None:
        self.queue.append(["atom", list(atoms)])
        self.depth += len(atoms)

    # ------------------------------------------------------------------ résolution
    def _commit(self, n: int, out: list[Event]) -> None:
        k = min(n, self.unseen)                     # d'abord ceux d'avant la synchro
        self.unseen -= k
        self.depth -= k
        n -= k
        while n > 0 and self.queue:
            e = self.queue[0]
            kind = e[0]
            if kind == "atom":
                while e[1] and n > 0:
                    self._atom(e[1].pop(0), out)
                    n -= 1
                    self.depth -= 1
                if not e[1]:
                    self.queue.pop(0)
                continue
            if kind == "exc" and len(self.queue) < 2:
                break                               # adresse de retour pas encore reçue
            self.queue.pop(0)
            if kind == "addr":
                self._address(e[1], e[2], out)
            elif kind == "ctx":
                if e[1]:
                    self.need_ctx = False
            elif kind == "exc":
                self._exception(e[1], out)
                n -= 1
                self.depth -= 1
            elif kind == "eret":
                c = EXC_EXIT
                self.stats.cycles += c
                out.append(ExcReturn(cycles=c))
                self.exc_depth = max(0, self.exc_depth - 1)
                self.last_was_return = False
                n -= 1
                self.depth -= 1
        self.depth = max(0, self.depth)

    def _cancel(self, n: int) -> None:
        """Retire les `n` éléments P0 les plus récents, et les adresses et contextes reçus
        après eux, comme OpenCSD (cycles et horodatages, qu'il conserve, ne sont pas en file)."""
        while n > 0 and self.queue:
            e = self.queue[-1]
            if e[0] == "atom":
                k = min(n, len(e[1]))
                del e[1][len(e[1]) - k:]
                n -= k
                self.depth -= k
                if not e[1]:
                    self.queue.pop()
            elif e[0] in ("exc", "eret"):
                self.queue.pop()
                n -= 1
                self.depth -= 1
            else:
                self.queue.pop()
        k = min(n, self.unseen)                     # puis ceux d'avant la synchro
        self.unseen -= k
        self.depth = max(0, self.depth - k)

    def _mispredict(self) -> None:
        """Le dernier atome était mal prédit : on l'inverse, et les adresses reçues depuis
        ne valent plus."""
        for k in range(len(self.queue) - 1, -1, -1):
            e = self.queue[k]
            if e[0] == "atom" and e[1]:
                e[1][-1] = "N" if e[1][-1] == "E" else "E"
                return
            if e[0] == "addr":
                del self.queue[k]
        if not self.unseen:                         # sinon : un élément d'avant la synchro
            self.stats.errors += 1

    # ------------------------------------------------------------------ éléments
    def _address(self, addr: int, context: bool, out: list[Event]) -> None:
        if self.handler is not None:               # adresse du handler de l'exception
            number, tail, c = self.handler
            self.handler = None
            self.stats.cycles += c
            out.append(ExcEntry(number, addr, tailchain=tail, cycles=c))
        self.addr = addr
        if context:
            self.need_ctx = False

    def _walk(self, out: list[Event], stop: int | None = None) -> object | None:
        """Parcourt le code depuis l'adresse courante : jusqu'au prochain point de passage
        (retourné, non émis), ou jusqu'à `stop` exclu (retourne True). None : désynchro.

        Vers `stop` (adresse de retour d'une exception), un point de passage rencontré en
        route est une incohérence : il aurait eu son atome. Rien n'est alors émis."""
        steps: list[Step] = []
        addr = self.addr
        while True:
            if stop is not None and addr == stop:
                break
            ins = self.image.instr(addr)
            if ins is None or len(steps) >= MAX_RUN:
                self.stats.unknown_code += 1
                self.addr = None
                return None
            if ins.waypoint:
                if stop is None:
                    break
                self.stats.errors += 1
                self.addr = None
                return None
            steps.append(Step(addr, cycles=cycles_of(ins)))
            addr += ins.size
        out += steps
        self.stats.instructions += len(steps)
        self.stats.cycles += sum(s.cycles for s in steps)
        self.addr = addr
        return True if stop is not None else ins

    def _atom(self, atom: str, out: list[Event]) -> None:
        if self.need_ctx or self.addr is None:
            self.stats.lost_atoms += 1
            return
        ins = self._walk(out)
        if ins is None:
            return
        executed = atom == "E"
        taken = executed and ins.kind in (DIRECT, INDIRECT)
        c = cycles_of(ins, executed, taken)
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
                nxt = None                          # adresse dans un prochain paquet
        self.addr = nxt

    def _exception(self, pkt: ExceptionPacket, out: list[Event]) -> None:
        nxt = self.queue[0]
        if nxt[0] == "ctx":                         # contexte éventuel avant l'adresse
            self.queue.pop(0)
            nxt = self.queue[0] if self.queue else None
        if nxt is None or nxt[0] != "addr":
            self.stats.errors += 1
            self.addr = None
            return
        self.queue.pop(0)
        ret = nxt[1]
        if pkt.addr_interp == 0b10 or self.addr is None:
            # l'adresse de retour est aussi la cible du branchement précédent : aucune
            # instruction exécutée depuis
            self.addr = ret
        elif self._walk(out, stop=ret) is None:
            self.addr = ret
        number = pkt.number
        if number is None:                          # Debug halt, Lazy FP push, Lockup
            self.addr = ret
            return
        tail = self.exc_depth > 0 and self.last_was_return
        self.handler = (number, tail, TAIL_CHAIN if tail else EXC_ENTRY)
        if not tail:
            self.exc_depth += 1
        self.last_was_return = False
        self.addr = None                            # l'adresse du handler suit


def decode(data: bytes, image: CodeImage, max_spec: int = 1,
           flush: bool = True) -> tuple[list[Event], Etm4Decoder, Etm4Flow]:
    pk = Etm4Decoder(max_spec=max_spec)
    fl = Etm4Flow(image, max_spec)
    events = fl.feed(pk.feed(data))
    if flush:
        events += fl.flush()
    return events, pk, fl
