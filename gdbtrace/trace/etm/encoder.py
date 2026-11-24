"""Encodeur ETMv3 (règles d'un ETM-M3/M4) : chemin d'exécution → flux de trace.

Sert à fabriquer une trace réaliste depuis le chemin réel exécuté par QEMU (qui n'émule
pas d'ETM) : tests du décodeur, comparaison avec OpenCSD, et trace « ETM simulé » de
l'interface. Règles appliquées, comme le matériel :
- un atome par instruction : N pour un branchement conditionnel non pris, E sinon ;
- aucune adresse pour un branchement direct (le décodeur la lit dans l'opcode) ;
- paquet d'adresse après un branchement indirect exécuté (adresse compressée par rapport à
  la dernière émise), et à chaque entrée d'exception (avec le numéro d'exception) ;
- retour d'exception : 0x76 puis adresse de reprise ;
- A-sync + I-sync au début et périodiquement.
"""
from __future__ import annotations

from typing import Iterable

from .cycles import outcome, successors
from .image import INDIRECT, CodeImage
from .qemu_log import Event, ExcEntry, ExcReturn, Step, TraceGap

ASYNC = b"\x00\x00\x00\x00\x00\x80"


def branch_packet(addr: int, last: int, exception: int | None = None) -> bytes:
    """Paquet d'adresse de branchement (Thumb, encodage alternatif)."""
    value, prev = (addr >> 1) & 0x7FFFFFFF, (last >> 1) & 0x7FFFFFFF
    sizes = {1: 6, 2: 12, 3: 19, 4: 26, 5: 31}
    n = 5
    if exception is None:
        for k in (1, 2, 3, 4):
            if value >> sizes[k] == prev >> sizes[k]:
                n = k
                break
        if n == 1 and value & 0x20:
            n = 2       # 1 octet avec bit 6 à 1 : lu comme « info d'exception » (OpenCSD)
    out = bytearray([0x01 | (value & 0x3F) << 1 | (0x80 if n > 1 else 0)])
    shift = 6
    for k in range(1, n):
        if n == 5 and k == 4:               # 5e octet : 4 bits + ISA Thumb (0b0x01xxxx)
            out.append(0x10 | ((value >> shift) & 0x0F) | (0x40 if exception is not None else 0))
        elif k == n - 1:                    # dernier octet compressé : 6 bits
            out.append((value >> shift) & 0x3F)
        else:
            out.append(0x80 | ((value >> shift) & 0x7F))
            shift += 7
    if exception is not None:
        if exception >> 4:
            out += bytes([0x80 | (exception & 0xF) << 1, (exception >> 4) & 0x1F])
        else:
            out.append((exception & 0xF) << 1)
    return bytes(out)


def isync_packet(addr: int, reason: int = 0) -> bytes:
    return bytes([0x08, (reason & 3) << 5]) + ((addr & ~1) | 1).to_bytes(4, "little")


class Etm3Encoder:
    """`cycle_accurate=True` : mode cycle-accurate (ETMCR bit 12) ; chaque atome porte les
    cycles de son instruction (`Step.cycles`, voir `cycles.annotate`) et les latences
    d'exception sont émises en cycles sans instruction."""

    def __init__(self, image: CodeImage, sync_period: int = 1024, cycle_accurate: bool = False):
        self.image = image
        self.sync_period = sync_period
        self.cycle_accurate = cycle_accurate
        self.out = bytearray()
        self.atoms: list[tuple[str, int]] = []     # (atome, cycles)
        self.last_addr = 0
        self.need_addr = False
        self.synced = False
        self._since_sync = 0

    # ------------------------------------------------------------------ primitives
    def _emit(self, data: bytes) -> None:
        self.out += data
        self._since_sync += len(data)

    def _flush_atoms(self) -> None:
        if self.cycle_accurate:
            for atom, cycles in self.atoms:
                self._ca_atom(atom, cycles)
            self.atoms.clear()
            return
        a = [atom for atom, _ in self.atoms]
        i = 0
        while i < len(a):
            if a[i] == "N" and i + 1 < len(a):          # format 2 : deux atomes dont un N
                self._emit(bytes([0x82 | (0x8 if a[i] == "N" else 0) | (0x4 if a[i + 1] == "N" else 0)]))
                i += 2
                continue
            e = 0
            while i < len(a) and a[i] == "E" and e < 15:
                e += 1
                i += 1
            n = 0
            if i < len(a) and a[i] == "N":
                n, i = 1, i + 1
            self._emit(bytes([0x80 | e << 2 | n << 6]))
        self.atoms.clear()

    # --- mode cycle-accurate (P-headers CA de l'ETMv3)
    def _wait(self, cycles: int) -> None:
        """Cycles sans instruction : format 3 sans atome, 1 à 8 cycles par octet."""
        while cycles > 0:
            k = min(8, cycles)
            self._emit(bytes([0xA0 | (k - 1) << 2]))
            cycles -= k

    def _ca_atom(self, atom: str, cycles: int) -> None:
        if cycles == 0:                                  # format 4 : un atome, 0 cycle
            self._emit(bytes([0x92 | (0x04 if atom == "N" else 0)]))
            return
        if atom == "E":                                  # format 3 : 1 à 8 cycles puis E
            self._wait(cycles - min(cycles, 8))
            self._emit(bytes([0xA0 | (min(cycles, 8) - 1) << 2 | 0x40]))
        else:                                            # format 1 CA : N sur 1 cycle
            self._wait(cycles - 1)
            self._emit(b"\xC0")

    def _sync(self, addr: int, reason: int) -> None:
        self._flush_atoms()
        isync = isync_packet(addr, reason)
        if self.cycle_accurate:                          # I-sync avec cycle count (0x70)
            isync = b"\x70\x00" + isync[1:]
        self._emit(ASYNC + isync)
        self.last_addr = addr
        self.need_addr = False
        self.synced = True
        self._since_sync = 0

    def _branch(self, addr: int, exception: int | None = None) -> None:
        self._flush_atoms()
        self._emit(branch_packet(addr, self.last_addr, exception))
        self.last_addr = addr
        self.need_addr = False

    # ------------------------------------------------------------------ encodage
    def encode(self, events: Iterable[Event]) -> bytes:
        evs = list(events)
        succ = successors(evs)
        for idx, ev in enumerate(evs):
            if isinstance(ev, Step):
                if not self.synced or self._since_sync >= self.sync_period:
                    self._sync(ev.addr, 1 if not self.synced else 0)
                elif self.need_addr:
                    self._branch(ev.addr)
                self._instr(ev, succ.get(idx))
            elif isinstance(ev, ExcEntry):
                if self.synced:
                    self._flush_atoms()
                    if self.cycle_accurate:
                        self._wait(ev.cycles)            # latence d'entrée
                    self._branch(ev.handler, ev.number)
            elif isinstance(ev, ExcReturn):
                if self.synced:
                    self._flush_atoms()
                    if self.cycle_accurate:
                        self._wait(ev.cycles)            # latence de sortie
                    self._emit(b"\x76")
                    self.need_addr = True
            elif isinstance(ev, TraceGap):
                # filtre : TraceEnable inactif ; la reprise se fait par I-sync « trace activée »
                self._flush_atoms()
                self.synced = False
                self.need_addr = False
        self._flush_atoms()
        return bytes(self.out)

    def _instr(self, ev: Step, nxt: int | None) -> None:
        ins = self.image.instr(ev.addr)
        if ins is None:
            raise ValueError(f"instruction hors image à {ev.addr:#x}")
        executed, taken = outcome(ins, ev.addr, nxt)
        self.atoms.append(("E" if executed else "N", ev.cycles))
        if ins.kind == INDIRECT and executed:
            self.need_addr = True


def encode(events: Iterable[Event], image: CodeImage, sync_period: int = 1024,
           cycle_accurate: bool = False) -> bytes:
    return Etm3Encoder(image, sync_period, cycle_accurate).encode(events)
