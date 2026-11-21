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

from .image import DIRECT, INDIRECT, CodeImage
from .qemu_log import Event, ExcEntry, ExcReturn, Step

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
    def __init__(self, image: CodeImage, sync_period: int = 1024):
        self.image = image
        self.sync_period = sync_period
        self.out = bytearray()
        self.atoms: list[str] = []
        self.last_addr = 0
        self.need_addr = False
        self.synced = False
        self._since_sync = 0

    # ------------------------------------------------------------------ primitives
    def _emit(self, data: bytes) -> None:
        self.out += data
        self._since_sync += len(data)

    def _flush_atoms(self) -> None:
        a = self.atoms
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
        a.clear()

    def _sync(self, addr: int, reason: int) -> None:
        self._flush_atoms()
        self._emit(ASYNC + isync_packet(addr, reason))
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
        succ = _successors(evs)
        for idx, ev in enumerate(evs):
            if isinstance(ev, Step):
                if not self.synced or self._since_sync >= self.sync_period:
                    self._sync(ev.addr, 1 if not self.synced else 0)
                elif self.need_addr:
                    self._branch(ev.addr)
                self._instr(ev.addr, succ.get(idx))
            elif isinstance(ev, ExcEntry):
                if self.synced:
                    self._branch(ev.handler, ev.number)
            elif isinstance(ev, ExcReturn):
                if self.synced:
                    self._flush_atoms()
                    self._emit(b"\x76")
                    self.need_addr = True
        self._flush_atoms()
        return bytes(self.out)

    def _instr(self, addr: int, nxt: int | None) -> None:
        ins = self.image.instr(addr)
        if ins is None:
            raise ValueError(f"instruction hors image à {addr:#x}")
        seq = addr + ins.size
        if ins.kind == DIRECT:
            taken = nxt == ins.target if nxt is not None else not ins.cond
            self.atoms.append("E" if taken or not ins.cond else "N")
        elif ins.kind == INDIRECT:
            if ins.cond and nxt == seq:
                self.atoms.append("N")
            else:
                self.atoms.append("E")
                self.need_addr = True
        else:
            self.atoms.append("E")


def _successors(evs: list[Event]) -> dict[int, int]:
    """Pour chaque instruction (indice de Step), l'adresse de l'instruction suivante dans le
    même contexte : la Step suivante, ou celle qui suit le retour de l'exception qui l'a
    interrompue (exceptions imbriquées et enchaînées gérées par une pile)."""
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
    return succ


def encode(events: Iterable[Event], image: CodeImage, sync_period: int = 1024) -> bytes:
    return Etm3Encoder(image, sync_period).encode(events)
