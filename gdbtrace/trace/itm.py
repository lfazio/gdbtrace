"""Décodeur du protocole ITM/DWT (ARMv7-M, annexe « ITM and DWT Packet Protocol »).

Le flux SWO d'un Cortex-M (formateur TPIU désactivé) est une suite de paquets
auto-délimités : synchro, overflow, horodatages, extension, et paquets « source » :
- instrumentation : écritures du logiciel dans ITM_STIMx (printf ITM, valeurs) ;
- matériel (DWT) : compteurs d'événements, trace des exceptions, échantillons de PC,
  data trace des comparateurs (watchpoints).

`ItmDecoder.feed()` est incrémental : un paquet coupé entre deux lectures est complété
à la lecture suivante. Un octet impossible à interpréter est compté et sauté (les paquets
étant auto-délimités, le décodage se recale de lui-même).
"""
from __future__ import annotations

from dataclasses import dataclass, field

# ====================================================================== paquets


@dataclass
class Packet:
    #: horodatage local cumulé (unités du timestamp ITM) au moment du décodage
    ts: int = field(default=0, kw_only=True)


@dataclass
class Sync(Packet):
    pass


@dataclass
class Overflow(Packet):
    """Le FIFO de l'ITM a débordé : des paquets ont été perdus côté cible."""


@dataclass
class LocalTimestamp(Packet):
    delta: int = 0
    #: TC : 0 = synchrone, 1 = horodatage retardé, 2 = paquet retardé, 3 = les deux
    tc: int = 0


@dataclass
class GlobalTimestamp(Packet):
    value: int = 0
    high: bool = False          # GTS2 (bits de poids fort) ou GTS1


@dataclass
class Extension(Packet):
    page: int = 0               # page des ports de stimulus (ports 32*page..)
    hardware: bool = False


@dataclass
class Instrumentation(Packet):
    port: int = 0
    value: int = 0
    size: int = 1               # octets : 1, 2 ou 4

    @property
    def data(self) -> bytes:
        return self.value.to_bytes(self.size, "little")


@dataclass
class EventCounter(Packet):
    flags: int = 0              # bits : CPI, Exc, Sleep, LSU, Fold, Cyc

    NAMES = ("CPI", "EXC", "SLEEP", "LSU", "FOLD", "CYC")

    @property
    def overflows(self) -> list[str]:
        return [n for i, n in enumerate(self.NAMES) if self.flags & (1 << i)]


@dataclass
class ExceptionTrace(Packet):
    number: int = 0
    event: str = "entry"        # entry, exit, return


@dataclass
class PcSample(Packet):
    pc: int | None = None       # None : cœur en sommeil (WFI/WFE)


@dataclass
class DataTrace(Packet):
    comparator: int = 0
    kind: str = "pc"            # pc, addr, read, write
    value: int = 0
    size: int = 4


@dataclass
class HardwarePacket(Packet):
    """Paquet matériel d'un discriminateur inconnu/réservé."""
    id: int = 0
    value: int = 0
    size: int = 1


EXCEPTION_EVENTS = {1: "entry", 2: "exit", 3: "return"}
_SIZES = {1: 1, 2: 2, 3: 4}


# ====================================================================== décodeur
class ItmDecoder:
    MAX_ZEROS = 64

    def __init__(self):
        self.buf = bytearray()
        self.ts = 0                 # horodatage local cumulé
        self.page = 0               # page de ports (paquet d'extension)
        self.errors = 0
        self.synced = False
        self.packets = 0

    def feed(self, data: bytes) -> list[Packet]:
        self.buf += data
        out: list[Packet] = []
        i = 0
        while i < len(self.buf):
            pkt, used = self._parse(i)
            if used == 0:
                break               # paquet incomplet : attendre la suite
            i += used
            if pkt is not None:
                self.packets += 1
                out.append(pkt)
        del self.buf[:i]
        return out

    # ------------------------------------------------------------------ analyse
    def _parse(self, i: int) -> tuple[Packet | None, int]:
        b = self.buf
        h = b[i]
        if h == 0x00:
            return self._sync(i)
        if h == 0x70:
            return Overflow(ts=self.ts), 1
        if h & 0x0F == 0x00:
            return self._timestamp(i)
        if h == 0x94 or h == 0xB4:
            return self._global_ts(i)
        if h & 0x0B == 0x08:
            return self._extension(i)
        if h & 0x03:
            return self._source(i)
        return self._error(1)

    def _error(self, n: int) -> tuple[None, int]:
        self.errors += 1
        return None, n

    def _sync(self, i: int) -> tuple[Packet | None, int]:
        b = self.buf
        j = i
        while j < len(b) and b[j] == 0x00:
            j += 1
        if j == len(b):
            if j - i > self.MAX_ZEROS:      # flux de zéros : on n'accumule pas sans fin
                return None, j - i - 5
            return None, 0
        zeros = j - i
        if b[j] == 0x80 and zeros >= 5:     # ≥ 47 bits à 0 puis un 1
            self.synced = True
            return Sync(ts=self.ts), zeros + 1
        return self._error(zeros)

    def _continuation(self, i: int, max_bytes: int) -> tuple[int, int] | None:
        """Octets de charge utile avec bit de continuation (bit 7) : (valeur, nombre)."""
        b = self.buf
        value, n = 0, 0
        while True:
            if i + n >= len(b):
                return None
            c = b[i + n]
            value |= (c & 0x7F) << (7 * n)
            n += 1
            if not c & 0x80:
                return value, n
            if n >= max_bytes:
                return value, -n            # trop long : invalide

    def _timestamp(self, i: int) -> tuple[Packet | None, int]:
        h = self.buf[i]
        if not h & 0x80:                    # LTS format 2 : valeur 1..6 dans l'en-tête
            delta = (h >> 4) & 0x7
            self.ts += delta
            return LocalTimestamp(delta=delta, tc=0, ts=self.ts), 1
        if h & 0xC0 == 0xC0:                # LTS format 1 : 1 à 4 octets de charge
            r = self._continuation(i + 1, 4)
            if r is None:
                return None, 0
            delta, n = r
            if n < 0:
                return self._error(1)
            self.ts += delta
            return LocalTimestamp(delta=delta, tc=(h >> 4) & 0x3, ts=self.ts), 1 + n
        return self._error(1)               # 0x80, 0x90, 0xA0, 0xB0 : réservés

    def _global_ts(self, i: int) -> tuple[Packet | None, int]:
        high = self.buf[i] == 0xB4
        r = self._continuation(i + 1, 6 if high else 4)
        if r is None:
            return None, 0
        value, n = r
        if n < 0:
            return self._error(1)
        return GlobalTimestamp(value=value, high=high, ts=self.ts), 1 + n

    def _extension(self, i: int) -> tuple[Packet | None, int]:
        h = self.buf[i]
        page, n = (h >> 4) & 0x7, 0
        if h & 0x80:
            r = self._continuation(i + 1, 4)
            if r is None:
                return None, 0
            more, n = r
            if n < 0:
                return self._error(1)
            page |= more << 3
        hardware = bool(h & 0x04)
        if not hardware:
            self.page = page
        return Extension(page=page, hardware=hardware, ts=self.ts), 1 + n

    def _source(self, i: int) -> tuple[Packet | None, int]:
        h = self.buf[i]
        size = _SIZES[h & 0x03]
        if i + 1 + size > len(self.buf):
            return None, 0
        value = int.from_bytes(self.buf[i + 1:i + 1 + size], "little")
        a = h >> 3
        if not h & 0x04:
            return Instrumentation(port=a + 32 * self.page, value=value, size=size, ts=self.ts), 1 + size
        return self._hardware(a, value, size), 1 + size

    def _hardware(self, ident: int, value: int, size: int) -> Packet:
        if ident == 0:
            return EventCounter(flags=value & 0x3F, ts=self.ts)
        if ident == 1:
            return ExceptionTrace(number=value & 0x1FF,
                                  event=EXCEPTION_EVENTS.get((value >> 12) & 0x3, "?"), ts=self.ts)
        if ident == 2:
            return PcSample(pc=value if size == 4 else None, ts=self.ts)
        if 8 <= ident <= 23:
            comparator = (ident >> 1) & 0x3
            if ident < 16:
                kind = "addr" if ident & 1 else "pc"
            else:
                kind = "write" if ident & 1 else "read"
            return DataTrace(comparator=comparator, kind=kind, value=value, size=size, ts=self.ts)
        return HardwarePacket(id=ident, value=value, size=size, ts=self.ts)


# ====================================================================== texte des ports
class ItmTextSink:
    """Reconstitue le texte écrit sur les ports d'instrumentation.

    Les ports de `text_ports` sont décodés en UTF-8 (printf ITM : un octet par écriture,
    ou 2/4 caractères par écriture 16/32 bits) ; les autres sont rendus en valeurs hexa.
    `on_line(port, ligne, ts)` est appelé à chaque fin de ligne.
    """

    def __init__(self, text_ports: set[int] | None = None, on_line=None, max_chars: int = 200_000):
        self.text_ports = {0} if text_ports is None else set(text_ports)
        self.on_line = on_line
        self.max_chars = max_chars
        self.partial: dict[int, bytearray] = {}
        self.history: dict[int, list[str]] = {}

    def add(self, pkt: Packet) -> None:
        if not isinstance(pkt, Instrumentation):
            return
        if pkt.port in self.text_ports:
            buf = self.partial.setdefault(pkt.port, bytearray())
            for byte in pkt.data:
                if byte == 0:
                    continue
                if byte == 0x0A:
                    self._line(pkt.port, buf.decode("utf-8", "replace").rstrip("\r"), pkt.ts)
                    buf.clear()
                else:
                    buf.append(byte)
        else:
            self._line(pkt.port, f"0x{pkt.value:0{pkt.size * 2}x}", pkt.ts)

    def _line(self, port: int, text: str, ts: int) -> None:
        lines = self.history.setdefault(port, [])
        lines.append(text)
        if len(lines) > 10_000:
            del lines[:1_000]
        if self.on_line:
            self.on_line(port, text, ts)

    def text(self, port: int = 0) -> str:
        """Lignes complètes reçues sur un port, plus la ligne en cours."""
        done = "\n".join(self.history.get(port, []))
        cur = self.partial.get(port, bytearray()).decode("utf-8", "replace")
        return done + ("\n" if done and cur else "") + cur


# ====================================================================== exceptions Cortex-M
CORE_EXCEPTIONS = {1: "Reset", 2: "NMI", 3: "HardFault", 4: "MemManage", 5: "BusFault",
                   6: "UsageFault", 11: "SVCall", 12: "DebugMonitor", 14: "PendSV", 15: "SysTick"}


def exception_name(number: int) -> str:
    if number == 0:
        return "Thread"
    if number in CORE_EXCEPTIONS:
        return CORE_EXCEPTIONS[number]
    return f"IRQ{number - 16}" if number >= 16 else f"Exception {number}"
