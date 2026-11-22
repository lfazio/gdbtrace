"""Décodeur de paquets ETMv4 (profil M : ETM-M7, ETMv4.0).

Encodages vérifiés contre OpenCSD (décodeur de référence d'ARM/Linaro) :
- A-sync : 11 × 0x00 puis 0x80 ; Trace Info 0x01 (sections INFO, KEY, SPEC, CYCT : champs à
  continuation, 7 bits par octet) ; Trace On 0x04 ;
- atomes, formats 1 à 6 (en-têtes 0xC0–0xFF) : **un atome par élément P0** — branchement
  (E : pris, N : non pris) ou ISB —, et non plus par instruction comme en ETMv3 : les
  instructions entre deux points de passage se déduisent du code ;
- adresses (Thumb : IS1) : courte 0x96 (bits [7:1] ou [15:1]), longue 32 bits 0x9B, avec
  contexte 0x83, correspondance exacte 0x90–0x92 avec la pile des 3 dernières adresses ;
  une adresse courte complète la plus récente ;
- exception 0x06 + 1 ou 2 octets (TYPE sur 10 bits, interprétation de l'adresse E1:E0),
  suivie de l'adresse de retour préférée ; retour d'exception 0x07 (élément P0 en ARMv7-M) ;
- spéculation : Commit 0x2D, Cancel 0x2E/0x2F/0x34–0x3F, Mispredict 0x30–0x33 ; cycles
  (F1 0x0E/0x0F, F2 0x0C/0x0D, F3 0x10–0x1F), Timestamp 0x02/0x03, Overflow (0x00 0x05),
  Discard (0x00 0x03), événements 0x71–0x7F.
"""
from __future__ import annotations

from dataclasses import dataclass, field

ASYNC = b"\x00" * 11 + b"\x80"


# ====================================================================== numéros d'exception
def exception_type(number: int) -> int:
    """Numéro d'exception Cortex-M → champ TYPE du paquet Exception (profil M). Les numéros
    1 à 23 (système, IRQ0 à IRQ7) sont codés tels quels ; 0x18 à 0x1A désignent Debug halt,
    Lazy FP push et Lockup ; IRQ8 et au-delà commencent à 0x208 (0x200 + n° d'IRQ)."""
    return number if number < 24 else 0x200 + number - 16


def exception_number(type_: int) -> int | None:
    """Champ TYPE → numéro d'exception, None pour les événements sans vecteur (Debug halt,
    Lazy FP push, Lockup) et les valeurs réservées."""
    if 0 < type_ < 0x18:
        return type_
    if 0x208 <= type_ <= 0x3EF:
        return type_ - 0x200 + 16
    return None


# ====================================================================== paquets
@dataclass
class Etm4Packet:
    pass


@dataclass
class ASync(Etm4Packet):
    resync: bool = False        # synchronisation (re)gagnée : tout l'état antérieur est perdu


@dataclass
class TraceInfo(Etm4Packet):
    info: int = 0               # bit 0 : comptage de cycles actif
    key: int = 0
    spec: int = 0               # profondeur de spéculation courante
    cc_threshold: int = 0


@dataclass
class TraceOn(Etm4Packet):
    """Reprise de la trace après une interruption (filtrage, débordement...)."""


@dataclass
class Address(Etm4Packet):
    addr: int
    isa: int = 1                # 1 : Thumb (IS1)
    context: bool = False       # adresse avec contexte : le contexte est (re)fixé


@dataclass
class Context(Etm4Packet):
    updated: bool


@dataclass
class Atoms(Etm4Packet):
    atoms: str                  # « E » / « N », du plus ancien au plus récent


@dataclass
class ExceptionPacket(Etm4Packet):
    type: int
    addr_interp: int = 1        # E1:E0 ; 0b10 : l'adresse est aussi la cible du branchement précédent
    fault_pending: bool = False

    @property
    def number(self) -> int | None:
        return exception_number(self.type)


@dataclass
class ExceptionReturn(Etm4Packet):
    pass


@dataclass
class Commit(Etm4Packet):
    count: int


@dataclass
class Cancel(Etm4Packet):
    count: int
    mispredict: bool = False
    atoms: str = ""


@dataclass
class Mispredict(Etm4Packet):
    atoms: str = ""


@dataclass
class CycleCount(Etm4Packet):
    cycles: int | None          # None : inconnu (après un débordement)
    commit: int | None = None


@dataclass
class Timestamp(Etm4Packet):
    value: int
    cycles: int | None = None


@dataclass
class Overflow(Etm4Packet):
    pass


@dataclass
class Discard(Etm4Packet):
    pass


@dataclass
class Event(Etm4Packet):
    value: int


# ====================================================================== atomes
_F4 = (0xE, 0x0, 0xA, 0x5)                       # bit 0 = atome le plus ancien, 1 = E
_F5 = {5: (0x1E, 5), 1: (0x00, 5), 2: (0x0A, 5), 3: (0x15, 5)}


def _pattern(bits: int, n: int) -> str:
    return "".join("E" if bits >> k & 1 else "N" for k in range(n))


def atoms_of(h: int) -> str | None:
    """Atomes d'un en-tête 0xC0–0xFF, None si le motif est invalide."""
    if 0xF6 <= h <= 0xF7:
        return _pattern(h & 1, 1)                                   # format 1
    if 0xD8 <= h <= 0xDB:
        return _pattern(h & 3, 2)                                   # format 2
    if h >= 0xF8:
        return _pattern(h & 7, 3)                                   # format 3
    if 0xDC <= h <= 0xDF:
        return _pattern(_F4[h & 3], 4)                              # format 4
    if 0xD5 <= h <= 0xD7 or h == 0xF5:                              # format 5
        p = _F5.get(((h & 0x20) >> 3) | (h & 3))
        return _pattern(*p) if p else None
    if h & 0x1F > 20:
        return None
    return "E" * ((h & 0x1F) + 3) + ("N" if h & 0x20 else "E")      # format 6


def _spec_atoms(h: int) -> str:
    """Atomes des paquets Mispredict et Cancel format 2 (bits [1:0])."""
    return ("", "E", "EE", "N")[h & 3]


# ====================================================================== décodeur
class _Incomplete(RuntimeError):
    pass


class Etm4Decoder:
    """Décodeur incrémental. `max_spec` : TRCIDR8.MAXSPEC de la cible (1 sur un ETM-M7) ;
    `commit_opt1` : TRCIDR0.COMMOPT (les paquets de cycles ne portent alors pas de commit)."""

    def __init__(self, max_spec: int = 1, commit_opt1: bool = False):
        self.max_spec = max_spec
        self.commit_opt1 = commit_opt1
        self.buf = bytearray()
        self.synced = False
        self.addrs = [0, 0, 0]          # pile des adresses, la plus récente en tête
        self.cc_threshold = 0
        self.ts = 0
        self.errors = 0
        self.dropped = 0                # octets ignorés en attendant une A-sync

    def feed(self, data: bytes) -> list[Etm4Packet]:
        self.buf += data
        out: list[Etm4Packet] = []
        i = 0
        while i < len(self.buf):
            if not self.synced:
                used = self._seek_sync(i)
                if used == 0:
                    break
                i += used
                if self.synced:
                    out.append(ASync(resync=True))
                continue
            try:
                pkt, used = self._parse(i)
            except _Incomplete:
                break
            i += used
            if pkt is not None:
                out.append(pkt)
        del self.buf[:i]
        return out

    # ------------------------------------------------------------------ synchro
    def _async_len(self, i: int) -> int | None:
        """Longueur d'une A-sync commençant en i, 0 si absente, None si incomplète."""
        b, j = self.buf, i
        while j < len(b) and b[j] == 0:
            j += 1
        if j == len(b):
            return None
        return j - i + 1 if b[j] == 0x80 and j - i >= 11 else 0

    def _seek_sync(self, i: int) -> int:
        b, k = self.buf, i
        while k < len(b):
            if b[k] == 0:
                n = self._async_len(k)
                if n is None:
                    self.dropped += k - i
                    return k - i
                if n:
                    self.dropped += k - i
                    self._resync()
                    return k - i + n
            k += 1
        self.dropped += k - i
        return k - i

    def _resync(self) -> None:
        self.synced = True

    def _lost(self) -> None:
        """Erreur de format : on se resynchronise sur la prochaine A-sync."""
        self.errors += 1
        self.synced = False

    # ------------------------------------------------------------------ outils
    def _byte(self, i: int) -> int:
        if i >= len(self.buf):
            raise _Incomplete
        return self.buf[i]

    def _cont(self, i: int, max_bytes: int = 5) -> tuple[int, int]:
        """Champ à continuation (7 bits par octet, bit 7 = suite) : (valeur, longueur)."""
        value = 0
        for k in range(max_bytes):
            b = self._byte(i + k)
            value |= (b & 0x7F) << (7 * k)
            if not b & 0x80:
                return value, k + 1
        return value, max_bytes

    def _push(self, addr: int) -> None:
        self.addrs = [addr & 0xFFFFFFFF, self.addrs[0], self.addrs[1]]

    # ------------------------------------------------------------------ paquets
    def _parse(self, i: int) -> tuple[Etm4Packet | None, int]:
        h = self._byte(i)
        if h == 0x00:
            return self._extension(i)
        if h == 0x01:
            return self._trace_info(i)
        if h in (0x02, 0x03):
            return self._timestamp(i)
        if h == 0x04:
            return TraceOn(), 1
        if h == 0x06:
            return self._exception(i)
        if h == 0x07:
            return ExceptionReturn(), 1
        if 0x0C <= h <= 0x1F:
            return self._cycle_count(i, h)
        if h == 0x2D:
            n, used = self._cont(i + 1)
            return Commit(n), 1 + used
        if h in (0x2E, 0x2F):
            n, used = self._cont(i + 1)
            return Cancel(n, mispredict=h == 0x2F), 1 + used
        if 0x30 <= h <= 0x33:
            return Mispredict(_spec_atoms(h)), 1
        if 0x34 <= h <= 0x37:
            return Cancel(1, mispredict=True, atoms=_spec_atoms(h)), 1
        if 0x38 <= h <= 0x3F:
            return Cancel(((h >> 1) & 3) + 2, mispredict=True, atoms="E" if h & 1 else ""), 1
        if 0x71 <= h <= 0x7F:
            return Event(h & 0x0F), 1
        if h in (0x80, 0x81):
            return self._context(i, h)
        if h in (0x82, 0x83):
            return self._long_address(i, h & 1, context=True)
        if 0x90 <= h <= 0x92:
            k = h & 3
            addr = self.addrs[k]
            self._push(addr)
            return Address(addr), 1
        if h in (0x95, 0x96):
            return self._short_address(i, h - 0x95)
        if h in (0x9A, 0x9B):
            return self._long_address(i, h - 0x9A)
        if h >= 0xC0:
            atoms = atoms_of(h)
            if atoms is None:
                self._lost()
                return None, 1
            return Atoms(atoms), 1
        # adresses 64 bits, données, instructions conditionnelles, Q : absents du profil M
        self._lost()
        return None, 1

    def _extension(self, i: int) -> tuple[Etm4Packet | None, int]:
        nxt = self._byte(i + 1)
        if nxt == 0x03:
            self.addrs = [0, 0, 0]
            return Discard(), 2
        if nxt == 0x05:
            return Overflow(), 2
        if nxt == 0x00:
            n = self._async_len(i)
            if n is None:
                raise _Incomplete
            if n:
                return ASync(), n
        self._lost()
        return None, 1

    def _trace_info(self, i: int) -> tuple[Etm4Packet, int]:
        present = self._byte(i + 1) & 0x1F
        _ctl, used = self._cont(i + 1)
        j = i + 1 + used
        vals = {}
        for bit, name in ((1, "info"), (2, "key"), (4, "spec"), (8, "cc_threshold"), (16, "window")):
            if present & bit:
                vals[name], n = self._cont(j)
                j += n
        vals.pop("window", None)
        pkt = TraceInfo(**vals)
        self.cc_threshold = pkt.cc_threshold
        self.addrs = [0, 0, 0]            # Trace Info remet la pile d'adresses à zéro
        return pkt, j - i

    def _timestamp(self, i: int) -> tuple[Etm4Packet, int]:
        h = self.buf[i]
        j, value, bits = i + 1, 0, 0
        for k in range(9):
            b = self._byte(j)
            j += 1
            if k == 8:
                value |= b << 56
                bits = 64
                break
            value |= (b & 0x7F) << (7 * k)
            bits += 7
            if not b & 0x80:
                break
        mask = (1 << bits) - 1
        self.ts = (self.ts & ~mask) | (value & mask)
        cycles = None
        if h & 1:
            cycles, n = self._cont(j, 3)
            j += n
        return Timestamp(self.ts, cycles), j - i

    def _exception(self, i: int) -> tuple[Etm4Packet, int]:
        b1 = self._byte(i + 1)
        type_ = (b1 >> 1) & 0x1F
        interp = ((b1 & 0x40) >> 5) | (b1 & 1)
        fault = False
        used = 2
        if b1 & 0x80:
            b2 = self._byte(i + 2)
            type_ |= (b2 & 0x1F) << 5
            fault = bool(b2 & 0x20)
            used = 3
        return ExceptionPacket(type_, interp, fault), used

    def _cycle_count(self, i: int, h: int) -> tuple[Etm4Packet, int]:
        if h >= 0x10:                                                   # format 3
            commit = None if self.commit_opt1 else ((h >> 2) & 3) + 1
            return CycleCount(self.cc_threshold + (h & 3), commit), 1
        if h >= 0x0E:                                                   # format 1
            j, commit, cycles = i + 1, None, None
            if not self.commit_opt1:
                commit, n = self._cont(j)
                j += n
            if not h & 1:
                count, n = self._cont(j, 3)
                j += n
                cycles = count + self.cc_threshold
            return CycleCount(cycles, commit), j - i
        b1 = self._byte(i + 1)                                          # format 2
        offset = self.max_spec - 15 if h & 1 else 1
        return CycleCount(self.cc_threshold + (b1 & 0x0F), ((b1 >> 4) & 0x0F) + offset), 2

    def _context(self, i: int, h: int) -> tuple[Etm4Packet, int]:
        if not h & 1:
            return Context(updated=False), 1
        info = self._byte(i + 1)
        if info & 0xC0:                   # VMID / Context ID : absents sur Cortex-M
            self._lost()
            return None, 2
        return Context(updated=True), 2

    def _short_address(self, i: int, is1: int) -> tuple[Etm4Packet, int]:
        shift = 1 if is1 else 2
        b1 = self._byte(i + 1)
        value, bits, used = (b1 & 0x7F) << shift, 7 + shift, 2
        if b1 & 0x80:
            value |= self._byte(i + 2) << (7 + shift)
            bits, used = bits + 8, 3
        mask = (1 << bits) - 1
        addr = (self.addrs[0] & ~mask) | (value & mask)
        self._push(addr)
        return Address(addr, is1), used

    def _long_address(self, i: int, is1: int, context: bool = False) -> tuple[Etm4Packet | None, int]:
        b = [self._byte(i + 1 + k) for k in range(4)]
        if is1:
            addr = (b[0] & 0x7F) << 1 | b[1] << 8
        else:
            addr = (b[0] & 0x7F) << 2 | (b[1] & 0x7F) << 9
        addr |= b[2] << 16 | b[3] << 24
        used = 5
        if context:
            info = self._byte(i + 5)
            used = 6
            if info & 0xC0:
                self._lost()
                return None, used
        self._push(addr)
        return Address(addr, is1, context=context), used
