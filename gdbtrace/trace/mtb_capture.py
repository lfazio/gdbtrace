"""Captures MTB : sonde réelle (tampon lu par gdb à chaque arrêt) et MTB simulée sous QEMU.

Dans les deux cas, les paquets décodés (`trace/mtb.py`) deviennent des événements `Step` /
`ExcEntry` / `ExcReturn` ajoutés à la même trace d'instructions que l'ETM (`EtmTrace`) :
Trace.List, flot d'appels, couverture et Timing fonctionnent tels quels. La MTB ne compte
pas les cycles : les durées viennent du modèle de cycles (`etm/cycles.py`), « estimées ».
"""
from __future__ import annotations

from .etm.cycles import annotate
from .etm.qemu_sim import QemuEtmSim
from .mtb import MtbDecoder, MtbState, configure, encode, last, read_packets, restart

DEFAULT_SIZE = 1024


def buffer_from_symbols(symbols) -> tuple[int, int] | None:
    """(adresse, taille) du tampon réservé par le firmware, d'après ses symboles."""
    from .mtb import SYMBOLS
    objects = getattr(symbols, "objects", {}) or {}
    for name in SYMBOLS:
        if name in objects and objects[name][1]:
            return objects[name]
    return None


class MtbCapture:
    """MTB d'une vraie cible : programmée une fois (cible arrêtée), puis, à chaque arrêt, lue,
    décodée et remise à zéro (chaque exécution n'est ajoutée qu'une fois)."""

    def __init__(self, session, image, base: int, buffer: int, size: int):
        self.s = session
        self.base = base
        self.decoder = MtbDecoder(image)
        self.state: MtbState = configure(session.write_u32, session.read_u32, base, buffer, size)
        self.last_pc: int | None = session.register("pc")
        self.last_packets: list = []
        self.wraps = 0

    def retarget(self, image) -> None:
        self.decoder = MtbDecoder(image)

    def collect(self) -> list:
        packets, st = read_packets(self.s.read_u32, self.s.read_memory, self.base)
        pc = self.s.register("pc")
        self.wraps += st.wrapped
        events = self.decoder.decode(packets, end_pc=pc, start_pc=None if st.wrapped else self.last_pc)
        restart(self.s.write_u32, self.base, st)
        self.last_pc, self.last_packets = pc, packets
        return annotate(events, self.decoder.image)

    def peek(self) -> list:
        """Paquets du tampon, sans le remettre à zéro."""
        return read_packets(self.s.read_u32, self.s.read_memory, self.base)[0]


class QemuMtbSim(QemuEtmSim):
    """MTB simulée : le chemin d'exécution réel de QEMU (journal de chaque exécution) est
    converti en paquets MTB, dont on ne garde que ce que contiendrait un tampon de `size`
    octets (0 : tout, comme une MTB vidée au fil de l'eau), puis décodé comme une vraie
    lecture de tampon."""

    def __init__(self, qmp, image, etm_trace, size: int = DEFAULT_SIZE):
        super().__init__(qmp, image, etm_trace)
        self.size = size
        self.decoder = MtbDecoder(image)
        self.last_pc: int | None = None
        self.last_packets: list = []
        self.wraps = 0

    def _convert(self, events: list, end_pc: int | None) -> int:
        packets = encode(events, self.image, start=self.runs == 1)
        kept = last(packets, self.size) if self.size else packets
        wrapped = len(kept) < len(packets)
        self.wraps += wrapped
        errors = self.decoder.stats.errors
        decoded = self.decoder.decode(kept, end_pc=end_pc,
                                      start_pc=None if wrapped else self.last_pc)
        self.etm.feed_events(annotate(decoded, self.image), errors=self.decoder.stats.errors - errors)
        self.last_pc, self.last_packets = end_pc, kept
        return 8 * len(kept)

    def peek(self) -> list:
        return list(self.last_packets)


def describe(base: int, st: MtbState) -> str:
    return (f"MTB à {base:#010x} : tampon {st.start:#010x}, {st.size} octets "
            f"({st.size // 8} paquets), {'active' if st.enabled else 'inactive'}")
