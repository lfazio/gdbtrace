"""Encodeur ETMv4 (profil M, règles d'un ETM-M7) : chemin d'exécution → flux de trace.

Comme l'encodeur ETMv3, il fabrique depuis le chemin réel de QEMU la trace qu'émettrait le
matériel : tests du décodeur, comparaison avec OpenCSD. Règles appliquées :
- un atome par point de passage (branchement ou ISB), aucun pour les autres instructions ;
- adresse après un branchement indirect pris (courte, longue, ou correspondance exacte avec
  la pile des 3 dernières adresses) ;
- exception : paquet Exception, adresse de retour préférée (l'instruction que le code
  interrompu exécutera au retour), puis adresse du handler ; E1:E0 = 0b10 quand l'adresse de
  retour est aussi la cible du branchement précédent (adresse encore inconnue du décodeur) ;
- retour d'exception : atome du BX LR / POP {PC}, puis 0x07, puis l'adresse de reprise ;
- A-sync + Trace Info + adresse avec contexte au début, et périodiquement, mais seulement
  juste après un point de passage : sinon les instructions déjà parcourues depuis le dernier
  point de passage seraient perdues ;
- spéculation de profondeur 1 (TRCIDR8 d'un ETM-M7) : chaque élément P0 valide implicitement
  le précédent.

Séquences reprises d'une vraie trace d'ETM-M7 (STM32F746) :
- avant une exception, Commit des éléments en attente ; paquet Exception toujours sous sa
  forme longue (3 octets) ; Context (mode handler, info 0x03) après l'adresse du handler ;
- après un retour d'exception, l'adresse de reprise porte le contexte (mode repris).
"""
from __future__ import annotations

from typing import Iterable

from .cycles import outcome, successors
from .etm4 import ASYNC, exception_type
from .image import DIRECT, INDIRECT, CodeImage
from .qemu_log import Event, ExcEntry, ExcReturn, Step, TraceGap

TRACE_INFO = b"\x01\x00"          # aucune section : pas de comptage de cycles, spéculation 0
TRACE_ON = b"\x04"
MAX_SPEC = 1                      # TRCIDR8.MAXSPEC d'un ETM-M7
CTX_THREAD, CTX_HANDLER = 0x00, 0x03   # octet d'info du contexte (relevé sur un ETM-M7)


def atom_packets(atoms: list[str] | str) -> bytes:
    """Atomes (« E » / « N », du plus ancien au plus récent) → paquets : format 6 pour les
    suites d'au moins 3 E (jusqu'à 23, suivies d'un E ou d'un N), formats 3, 2, 1 sinon."""
    out = bytearray()
    a, i = atoms, 0
    while i < len(a):
        run = 0
        while i + run < len(a) and a[i + run] == "E" and run < 23:
            run += 1
        rest = len(a) - i
        if run >= 3 and rest > run:                     # format 6 : E^run puis E ou N
            out.append(0xC0 | (0x20 if a[i + run] == "N" else 0) | (run - 3))
            i += run + 1
        elif run >= 4:                                  # format 6 : E^(run-1) puis E
            out.append(0xC0 | (run - 4))
            i += run
        else:
            k = min(3, rest)                            # formats 3, 2, 1
            bits = sum(1 << j for j in range(k) if a[i + j] == "E")
            out.append((0xF8, 0xD8, 0xF6)[3 - k] | bits)
            i += k
    return bytes(out)


class Etm4Encoder:
    def __init__(self, image: CodeImage, sync_period: int = 1024):
        self.image = image
        self.sync_period = sync_period
        self.out = bytearray()
        self.atoms: list[str] = []
        self.stack = [0, 0, 0]            # pile d'adresses, comme celle du décodeur
        self.need_addr = False
        self.synced = False
        self.boundary = True              # instruction suivante = début d'une plage
        self.handler: int | None = None   # dernier handler entré (adresse de retour au besoin)
        self.depth = 0                    # éléments P0 non validés, vus du décodeur
        self.mode = CTX_THREAD            # contexte courant (thread / handler)
        self.ctx_pending = False          # la prochaine adresse doit porter le contexte
        self._since_sync = 0
        #: événements effectivement encodés (une exception dont l'adresse de retour est
        #: inconnue, en fin de chemin, arrête l'encodage)
        self.consumed = 0

    # ------------------------------------------------------------------ primitives
    def _emit(self, data: bytes) -> None:
        self.out += data
        self._since_sync += len(data)

    def _flush_atoms(self) -> None:
        if self.atoms:
            self._emit(atom_packets(self.atoms))
            self.atoms.clear()

    def _p0(self) -> None:
        """Un élément P0 de plus ; au-delà de MAXSPEC, le plus ancien est validé implicitement."""
        self.depth = min(self.depth + 1, MAX_SPEC)

    def _address(self, addr: int, context: bool = False) -> None:
        addr &= 0xFFFFFFFE
        s = self.stack
        if context:                                     # 0x83 : 32 bits, IS1, contexte
            self._emit(bytes([0x83, (addr >> 1) & 0x7F, (addr >> 8) & 0xFF,
                              (addr >> 16) & 0xFF, addr >> 24, self.mode]))
            self.ctx_pending = False
        elif addr in s:                                 # correspondance exacte
            self._emit(bytes([0x90 + s.index(addr)]))
        elif (addr ^ s[0]) >> 8 == 0:                   # courte, 1 octet : bits [7:1]
            self._emit(bytes([0x96, (addr >> 1) & 0x7F]))
        elif (addr ^ s[0]) >> 16 == 0:                  # courte, 2 octets : bits [15:1]
            self._emit(bytes([0x96, 0x80 | (addr >> 1) & 0x7F, (addr >> 8) & 0xFF]))
        else:                                           # longue, 32 bits
            self._emit(bytes([0x9B, (addr >> 1) & 0x7F, (addr >> 8) & 0xFF,
                              (addr >> 16) & 0xFF, addr >> 24]))
        self.stack = [addr, s[0], s[1]]

    def _sync(self, addr: int) -> None:
        self._flush_atoms()
        self._emit(ASYNC + TRACE_INFO + (b"" if self.synced else TRACE_ON))
        self.stack = [0, 0, 0]                          # Trace Info remet la pile à zéro
        self._address(addr, context=True)
        self.need_addr = False
        self.synced = True
        self._since_sync = 0

    def _exception(self, number: int, ret: int, handler: int) -> None:
        self._flush_atoms()
        if self.depth:                                  # tout ce qui précède est validé
            self._emit(bytes([0x2D, self.depth]))
            self.depth = 0
        t = exception_type(number)
        interp = 0b10 if self.need_addr else 0b01
        b1 = (t & 0x1F) << 1 | (interp & 1) | (interp >> 1) << 6
        self._emit(bytes([0x06, 0x80 | b1, (t >> 5) & 0x1F]))
        self._address(ret)
        self._address(handler)
        self._p0()
        if self.mode != CTX_HANDLER:                    # entrée en mode handler
            self._emit(bytes([0x81, CTX_HANDLER]))
            self.mode = CTX_HANDLER
        self.ctx_pending = False
        self.need_addr = False
        self.boundary = True
        self.handler = handler

    # ------------------------------------------------------------------ encodage
    def encode(self, events: Iterable[Event]) -> bytes:
        evs = list(events)
        succ = successors(evs)
        rets: list[int] = []              # adresses de retour des exceptions en cours
        ctx_stack: list[int | None] = []
        last: int | None = None           # dernière instruction du contexte courant
        self._last_step: int | None = None  # dernière instruction encodée, tous contextes
        for idx, ev in enumerate(evs):
            if isinstance(ev, Step):
                if not self.synced or (self._since_sync >= self.sync_period and self.boundary):
                    self._sync(ev.addr)
                elif self.need_addr:
                    self._flush_atoms()
                    self._address(ev.addr, context=self.ctx_pending)
                    self.need_addr = False
                self._instr(ev, succ.get(idx))
                last = self._last_step = idx
            elif isinstance(ev, ExcEntry) and self.synced:
                if ev.tailchain and rets:
                    ret = rets[-1]                     # même code interrompu
                else:
                    ret = self._return_address(ev, evs, last, succ)
                    if ret is None:
                        break
                    rets.append(ret)
                    ctx_stack.append(last)
                self._exception(ev.number, ret, ev.handler)
                last = None
            elif isinstance(ev, ExcReturn) and self.synced:
                self._flush_atoms()
                self._emit(b"\x07")
                self._p0()
                self.need_addr = True
                self.boundary = True
                if rets:
                    rets.pop()
                last = ctx_stack.pop() if ctx_stack else None
                self.mode = CTX_HANDLER if rets else CTX_THREAD     # mode repris
                self.ctx_pending = True
            elif isinstance(ev, TraceGap):
                # filtre : ViewInst inactif ; la reprise se fait par Trace On + adresse
                self._flush_atoms()
                self.synced = False
                self.need_addr = False
                rets, ctx_stack, last = [], [], None
            self.consumed = idx + 1
        self._flush_atoms()
        return bytes(self.out)

    def _return_address(self, ev: ExcEntry, evs: list[Event], last: int | None,
                        succ: dict[int, int]) -> int | None:
        """Adresse de retour préférée : l'instruction que le code interrompu aurait exécutée
        ensuite. Ce n'est pas forcément celle où l'exception revient : sous un RTOS, le
        retour peut aller à une autre tâche (le premier SVC de FreeRTOS ne revient jamais).

        QEMU la donne pour une interruption prise devant une instruction (`ev.ret`) ; sinon
        elle se déduit de la dernière instruction exécutée. Seule la cible d'un branchement
        indirect pris juste avant demande le chemin à venir ; None si elle est inconnue."""
        if last is None:                    # interrompu avant sa première instruction
            return ev.ret if ev.ret is not None else self.handler
        step = evs[last]
        ins = self.image.instr(step.addr)
        seq = step.addr + ins.size
        fresh = last == self._last_step and bool(self.atoms)   # son atome est encore en attente
        if ev.ret is not None:
            if ins.cond and ins.kind in (DIRECT, INDIRECT) and fresh:
                # le branchement conditionnel interrompu : son issue se lit dans l'adresse
                taken = ev.ret == ins.target if ins.kind == DIRECT else ev.ret != seq
                self.atoms[-1] = "E" if taken else "N"
                self.need_addr = ins.kind == INDIRECT and taken
            return ev.ret
        if not ins.waypoint or ins.isb:
            return seq
        if ins.kind == DIRECT and fresh:
            return ins.target if self.atoms[-1] == "E" else seq
        return succ.get(last)

    def _instr(self, ev: Step, nxt: int | None) -> None:
        ins = self.image.instr(ev.addr)
        if ins is None:
            raise ValueError(f"instruction hors image à {ev.addr:#x}")
        if not ins.waypoint:
            self.boundary = False
            return
        executed, _taken = outcome(ins, ev.addr, nxt)
        self.atoms.append("E" if executed else "N")
        self._p0()
        if ins.kind == INDIRECT and executed:
            self.need_addr = True
        self.boundary = True


def encode(events: Iterable[Event], image: CodeImage, sync_period: int = 1024) -> bytes:
    return Etm4Encoder(image, sync_period).encode(events)
