"""Captures MTB : sonde réelle (tampon lu par gdb à chaque arrêt) et MTB simulée sous QEMU.

Dans les deux cas, les paquets décodés (`trace/mtb.py`) deviennent des événements `Step` /
`ExcEntry` / `ExcReturn` ajoutés à la même trace d'instructions que l'ETM (`EtmTrace`) :
Trace.List, flot d'appels, couverture et Timing fonctionnent tels quels. La MTB ne compte
pas les cycles : les durées viennent du modèle de cycles (`etm/cycles.py`), « estimées ».
"""
from __future__ import annotations

from .etm.cycles import CORTEX_M0P, annotate
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
    décodée et remise à zéro (chaque exécution n'est ajoutée qu'une fois). En AUTOHALT, la MTB
    arrête elle-même le cœur quand le tampon est presque plein : `halted` le signale après
    `collect()`, le gestionnaire relance alors la cible sans que l'arrêt soit visible."""

    def __init__(self, session, image, base: int, buffer: int, size: int, autohalt: bool = False):
        self.s = session
        self.base = base
        self.autohalt = autohalt
        self.decoder = MtbDecoder(image)
        self.state: MtbState = configure(session.write_u32, session.read_u32, base, buffer, size,
                                         autohalt=autohalt)
        self.last_pc: int | None = session.register("pc")
        self.last_packets: list = []
        self.wraps = 0
        self.autohalts = 0           # arrêts du cœur demandés par la MTB (tampon plein)
        self.halted = False          # le dernier arrêt venait de la MTB
        self.model = CORTEX_M0P      # la MTB est celle d'un Cortex-M0+

    def retarget(self, image) -> None:
        self.decoder = MtbDecoder(image)

    def collect(self) -> list:
        packets, st = read_packets(self.s.read_u32, self.s.read_memory, self.base)
        pc = self.s.register("pc")
        self.halted = st.halt_requested
        self.autohalts += self.halted
        self.wraps += st.wrapped
        events = self.decoder.decode(packets, end_pc=pc, start_pc=None if st.wrapped else self.last_pc)
        restart(self.s.write_u32, self.base, st)
        self.last_pc, self.last_packets = pc, packets
        return annotate(events, self.decoder.image, self.model)

    def peek(self) -> list:
        """Paquets du tampon, sans le remettre à zéro."""
        return read_packets(self.s.read_u32, self.s.read_memory, self.base)[0]


class QemuMtbSim(QemuEtmSim):
    """MTB simulée : le chemin d'exécution réel de QEMU (journal de chaque exécution) est
    converti en paquets MTB, dont on ne garde que ce que contiendrait un tampon de `size`
    octets (0 : tout, comme une MTB vidée au fil de l'eau), puis décodé comme une vraie
    lecture de tampon."""

    def __init__(self, qmp, image, etm_trace, size: int = DEFAULT_SIZE, autohalt: bool = False,
                 period: float = 0.1):
        super().__init__(qmp, image, etm_trace)
        self.size = size
        self.decoder = MtbDecoder(image)
        self.last_pc: int | None = None
        self.last_packets: list = []
        self.wraps = 0
        # AUTOHALT simulé : QEMU est arrêté toutes les `period` s par QMP `stop` (SIGINT non
        # demandé pour gdb, comme l'EDBGRQ de la MTB) ; chaque tronçon est gardé en entier
        self.autohalt = autohalt
        self.period = period
        self.autohalts = 0
        self.logged = 0              # instructions exécutées d'après QEMU (vérité terrain)
        self.halted = False
        self._stopping = False
        self._halter = None
        self._closing = False
        self._pending: list[tuple[str, int | None]] = []   # tronçons à convertir (AUTOHALT)

    def start_halter(self, session) -> None:
        """Fil qui simule le seuil AUTOHALT : arrête QEMU régulièrement pendant les exécutions."""
        import threading
        import time

        def loop():
            while not self._closing:
                time.sleep(self.period)
                if (session.state == "running" and not session.internal_stop
                        and self.path is not None and not self._stopping):
                    self._stopping = True
                    try:
                        self.qmp.hmp("stop")
                    except Exception:  # noqa: BLE001 - QEMU fermé
                        self._stopping = False
        self._halter = threading.Thread(target=loop, name="mtb-autohalt", daemon=True)
        self._halter.start()

    def take_halt(self) -> bool:
        """Le dernier arrêt venait-il du seuil simulé ? (remis à zéro)"""
        self.halted, self._stopping = self._stopping, False
        self.autohalts += self.halted
        return self.halted

    def stash_run(self, end_pc: int | None) -> None:
        """Arrêt interne : le journal du tronçon est mis de côté (opération immédiate) ; sa
        conversion, gourmande en calcul, attend l'arrêt visible, sinon elle figerait
        l'interface pendant la course (sur carte, vider la MTB ne lit que quelques Ko)."""
        path = self._close_log()
        if path is not None:
            self._pending.append((path, end_pc))

    def end_run(self, end_pc: int | None = None) -> int:
        """Arrêt visible : tronçons mis de côté, dans l'ordre, puis l'exécution en cours."""
        done = 0
        pending, self._pending = self._pending, []
        for path, pc in pending:
            done += self._convert(self._read_log(path), pc)
        return done + super().end_run(end_pc)

    def clear_halt(self) -> None:
        """Arrêt visible (Break, point d'arrêt) : un `stop` QMP qui l'a croisé est oublié,
        sinon le fil n'en enverrait plus aucun."""
        self._stopping = False

    def close(self) -> None:
        self._closing = True
        super().close()

    def _convert(self, events: list, end_pc: int | None) -> int:
        from .etm.qemu_log import Step
        self.logged += sum(isinstance(e, Step) for e in events)
        packets = encode(events, self.image, start=self.runs == 1, resume_pc=self.last_pc,
                         end_pc=end_pc)
        kept = last(packets, self.size) if self.size and not self.autohalt else packets
        wrapped = len(kept) < len(packets)
        self.wraps += wrapped
        errors = self.decoder.stats.errors
        decoded = self.decoder.decode(kept, end_pc=end_pc,
                                      start_pc=None if wrapped else self.last_pc)
        self.etm.feed_events(annotate(decoded, self.image, self.model),
                             errors=self.decoder.stats.errors - errors)
        self.last_pc, self.last_packets = end_pc, kept
        return 8 * len(kept)

    def peek(self) -> list:
        return list(self.last_packets)


def describe(base: int, st: MtbState) -> str:
    text = (f"MTB à {base:#010x} : tampon {st.start:#010x}, {st.size} octets "
            f"({st.size // 8} paquets), {'active' if st.enabled else 'inactive'}")
    if st.autohalt:
        per_halt = (st.watermark - (st.start - st.sram)) // 8
        text += (f", AUTOHALT au seuil {st.sram + st.watermark:#010x} : un arrêt interne tous les "
                 f"{per_halt} branchements")
    return text
