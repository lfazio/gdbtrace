"""Décodeur de paquets ETMv3 (profil Cortex-M : ETM-M3 / ETM-M4, ETMv3.5).

Encodages vérifiés contre OpenCSD (décodeur de référence d'ARM/Linaro) :
- A-sync : ≥ 5 × 0x00 puis 0x80 ; I-sync : 0x08, info, adresse 32 bits (bit 0 = Thumb) ;
- P-header (hors mode cycle-accurate) : format 1 `1NEEEE00` (E atomes « exécuté » puis
  0 ou 1 atome « non exécuté »), format 2 `1000FF10` (deux atomes) ;
  **un atome = une instruction** (E : exécutée / condition vraie, N : condition fausse) ;
- adresse de branchement (bit 0 = 1), encodage « alternatif » des ETM-M : octets 2 à 4 sans
  continuation portant 6 bits et le drapeau « info d'exception » (bit 6), 5e octet : 4 bits
  d'adresse + ISA ; adresse compressée par rapport à la dernière adresse émise ;
- 0x76 sortie d'exception, 0x7E entrée (marqueur), 0x0C trigger, 0x66 ignore,
  0x42/0x46 timestamp, 0x04 cycle count, 0x6E context ID.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class EtmPacket:
    pass


@dataclass
class ASync(EtmPacket):
    pass


@dataclass
class ISync(EtmPacket):
    addr: int = 0
    reason: int = 0          # 0 périodique, 1 trace activée, 2 reprise après overflow, 3 sortie de debug
    thumb: bool = True
    cycles: int = 0          # I-sync avec cycle count (0x70, mode cycle-accurate)


@dataclass
class Atoms(EtmPacket):
    atoms: str = ""          # « E » exécutée, « N » non exécutée, dans l'ordre d'exécution
    cycles: tuple = ()       # mode cycle-accurate : cycles de chaque atome


@dataclass
class Wait(EtmPacket):
    """Cycles écoulés sans instruction (mode cycle-accurate : P-header format 3 sans atome,
    ou paquet de cycle count)."""
    cycles: int = 0


@dataclass
class Branch(EtmPacket):
    addr: int = 0            # adresse complète (fusionnée avec la précédente)
    bits: int = 0            # bits d'adresse portés par le paquet
    exception: int | None = None
    cancel: bool = False     # la dernière instruction tracée a été annulée


@dataclass
class ExceptionExit(EtmPacket):
    pass


@dataclass
class ExceptionEntry(EtmPacket):
    pass


@dataclass
class Trigger(EtmPacket):
    pass


@dataclass
class Timestamp(EtmPacket):
    value: int = 0


@dataclass
class Other(EtmPacket):
    kind: str = ""
    value: int = 0


class Etm3Decoder:
    """Décodeur incrémental. `ctxid_bytes` : octets de context ID (0 sur Cortex-M)."""

    def __init__(self, ctxid_bytes: int = 0, cycle_accurate: bool = False):
        self.ctxid_bytes = ctxid_bytes
        self.cycle_accurate = cycle_accurate   # doit refléter ETMCR bit 12 de la cible
        self.buf = bytearray()
        self.synced = False
        self.last_addr = 0          # base de la compression d'adresse
        self.errors = 0
        self.dropped = 0            # octets ignorés avant la première A-sync

    def feed(self, data: bytes) -> list[EtmPacket]:
        self.buf += data
        out: list[EtmPacket] = []
        i = 0
        while i < len(self.buf):
            if not self.synced:
                used = self._seek_sync(i)
                if used == 0:
                    break
                i += used
                continue
            pkt, used = self._parse(i)
            if used == 0:
                break
            i += used
            if pkt is not None:
                out.append(pkt)
        del self.buf[:i]
        return out

    # ------------------------------------------------------------------ synchro
    def _zeros_then_80(self, i: int) -> int | None:
        """Longueur d'une A-sync commençant en i, 0 si absente, None si incomplète."""
        b = self.buf
        j = i
        while j < len(b) and b[j] == 0:
            j += 1
        if j == len(b):
            return None
        return j - i + 1 if b[j] == 0x80 and j - i >= 5 else 0

    def _seek_sync(self, i: int) -> int:
        b = self.buf
        k = i
        while k < len(b):
            if b[k] == 0:
                n = self._zeros_then_80(k)
                if n is None:
                    self.dropped += k - i
                    return k - i if k > i else 0
                if n:
                    self.dropped += k - i
                    self.synced = True
                    return k - i + n
            k += 1
        self.dropped += k - i
        return k - i

    # ------------------------------------------------------------------ paquets
    def _parse(self, i: int) -> tuple[EtmPacket | None, int]:
        b = self.buf
        h = b[i]
        if h & 0x01:
            return self._branch(i)
        if h & 0x81 == 0x80:
            if self.cycle_accurate:
                pkt = self._pheader_ca(h)
                return (pkt, 1) if pkt is not None else self._error(1)
            return self._pheader(h), 1
        if h == 0x00:
            n = self._zeros_then_80(i)
            if n is None:
                return None, 0
            if n:
                return ASync(), n
            return self._error(1)
        if h == 0x08:
            return self._isync(i, 1)
        if h == 0x70:                       # I-sync avec cycle count (mode cycle-accurate)
            r = self._cont(i + 1, 5)
            if r is None:
                return None, 0
            pkt, used = self._isync(i, 1 + r[1])
            if pkt is not None:
                pkt.cycles = r[0]
            return pkt, used
        if h == 0x04:
            r = self._cont(i + 1, 5)
            return (None, 0) if r is None else (Wait(r[0]), 1 + r[1])
        if h == 0x0C:
            return Trigger(), 1
        if h == 0x66:
            return None, 1                  # ignore
        if h == 0x76:
            return ExceptionExit(), 1
        if h == 0x7E:
            return ExceptionEntry(), 1
        if h == 0x6E:
            n = self.ctxid_bytes
            if i + 1 + n > len(b):
                return None, 0
            return Other("context-id", int.from_bytes(b[i + 1:i + 1 + n], "little")), 1 + n
        if h == 0x3C:
            if i + 2 > len(b):
                return None, 0
            return Other("vmid", b[i + 1]), 2
        if h & 0xFB == 0x42:
            r = self._cont(i + 1, 9)
            return (None, 0) if r is None else (Timestamp(r[0]), 1 + r[1])
        return self._error(1)               # données (non supportées sur ETM-M) ou réservé

    def _error(self, n: int) -> tuple[None, int]:
        self.errors += 1
        return None, n

    def _cont(self, i: int, max_bytes: int) -> tuple[int, int] | None:
        b = self.buf
        value, n = 0, 0
        while True:
            if i + n >= len(b):
                return None
            c = b[i + n]
            value |= (c & 0x7F) << (7 * n)
            n += 1
            if not c & 0x80 or n >= max_bytes:
                return value, n

    @staticmethod
    def _pheader(h: int) -> Atoms:
        if h & 0x03 == 0x00:                # format 1 : E × EEEE puis N × (0|1)
            return Atoms("E" * ((h >> 2) & 0xF) + "N" * ((h >> 6) & 1))
        if h & 0xF3 == 0x82:                # format 2 : deux atomes, bit 3 = 1er, bit 2 = 2e
            return Atoms(("N" if h & 0x8 else "E") + ("N" if h & 0x4 else "E"))
        return Atoms("")

    @staticmethod
    def _pheader_ca(h: int) -> EtmPacket | None:
        """P-headers du mode cycle-accurate (lecture identique à OpenCSD)."""
        code = h & 0xA3
        if code == 0x80:                    # format 1 : E (bits 4:2) puis N, 1 cycle par atome
            e, n = (h >> 2) & 0x7, (h >> 6) & 1
            if e + n == 0:
                return None
            return Atoms("E" * e + "N" * n, (1,) * (e + n))
        if code == 0x82:
            if h & 0x10:                    # format 4 : un atome, 0 cycle
                return Atoms("N" if h & 0x4 else "E", (0,))
            return Atoms(("N" if h & 0x8 else "E") + ("N" if h & 0x4 else "E"), (1, 0))   # format 2
        if code == 0xA0:                    # format 3 : 1 à 8 cycles, puis un atome E optionnel
            cycles = ((h >> 2) & 0x7) + 1
            return Atoms("E", (cycles,)) if h & 0x40 else Wait(cycles)
        return None

    def _isync(self, i: int, hdr_len: int) -> tuple[EtmPacket | None, int]:
        b = self.buf
        k = i + hdr_len + self.ctxid_bytes
        if k + 5 > len(b):
            return None, 0
        info = b[k]
        addr = int.from_bytes(b[k + 1:k + 5], "little")
        used = k + 5 - i
        if info & 0x80:                     # adresse LSiP compressée qui suit (rare)
            n = 0
            while True:
                if k + 5 + n >= len(b):
                    return None, 0
                c = b[k + 5 + n]
                n += 1
                if not c & 0x80 or n >= 5:
                    break
            used += n
        self.last_addr = addr & ~1
        return ISync(addr & ~1, (info >> 5) & 0x3, bool(addr & 1)), used

    def _branch(self, i: int) -> tuple[EtmPacket | None, int]:
        b = self.buf
        # --- délimitation (comme OpenCSD, encodage alternatif)
        n = 0
        need_exc = False
        while True:
            if i + n >= len(b):
                return None, 0
            c = b[i + n]
            n += 1
            if need_exc:
                if not c & 0x80:
                    break
                continue
            if n == 5:
                if c & 0xC0 == 0x40:
                    need_exc = True
                    continue
                break
            if not c & 0x80:
                if c & 0xC0 == 0x40:
                    need_exc = True
                    continue
                break
        pkt = b[i:i + n]
        # --- extraction de l'adresse
        value, bits, k = 0, 0, 0
        exc_follows = False
        cont = True
        while cont and k < 4:
            c = pkt[k]
            cont = bool(c & 0x80)
            if k == 0:
                value |= ((c >> 1) & 0x3F)
                bits += 6
                if not cont and c & 0x40:
                    exc_follows = True
            elif not cont:                  # dernier octet compressé (encodage alternatif)
                exc_follows = bool(c & 0x40)
                value |= (c & 0x3F) << bits
                bits += 6
            else:
                value |= (c & 0x7F) << bits
                bits += 7
            k += 1
        if cont:                            # 5e octet : 4 bits d'adresse + ISA
            c = pkt[k]
            k += 1
            exc_follows = bool(c & 0x40)
            value |= (c & 0x0F) << bits
            bits += 4
        bits += 1                           # alignement Thumb (adresse sur 2 octets)
        value <<= 1
        mask = (1 << bits) - 1
        addr = ((self.last_addr & ~mask) | (value & mask)) & 0xFFFFFFFF
        self.last_addr = addr
        exception, cancel = None, False
        if exc_follows and k < len(pkt):
            e0 = pkt[k]
            k += 1
            number = (e0 >> 1) & 0xF
            cancel = bool(e0 & 0x20)
            if e0 & 0x80 and k < len(pkt):
                e1 = pkt[k]
                k += 1
                if not e1 & 0x40:           # octet 1 (v7-M) : bits [8:4] du numéro
                    number |= (e1 & 0x1F) << 4
            exception = number
        return Branch(addr, bits, exception, cancel), n
