"""Trace ETM écrite en RAM par l'ETR (CoreSight TMC), lue à chaque arrêt.

Sur un STM32N6 (Cortex-M55), l'ETM n'a pas de sortie série : funnel → ETF → réplicateur →
ETR, qui écrit les trames du formateur (16 octets, ID de flux 2 pour l'ETM) dans un tampon
circulaire en RAM système. Comme la MTB : programmé cible arrêtée ; à chaque arrêt, l'ETM est
coupé (il vide sa FIFO), l'ETF et l'ETR sont vidés (flush, arrêt sur flush), le tampon lu par
gdb ; à la reprise suivante, les puits repartent du début du tampon et l'ETM est rallumé (il
commence par une synchronisation : chaque exécution se décode seule). Un tampon plein a
tourné (STS.Full) : seules ses dernières trames restent, et le décodage commence par un trou.

Le tampon est réservé par le firmware (symbole `__etr_buffer__`, aligné sur 4 Ko) ou donné
par `--etr-buffer ADRESSE:TAILLE`. Les horloges de trace et les attributs du maître AXI de
l'ETR (`chips.py`) sont réécrits à chaque reprise : un reset système les remet à zéro.
"""
from __future__ import annotations

from .chips import TraceChip
from .tpiu import TpiuDeformatter

# registres du TMC (ETF, ETR)
RSZ, STS, RRD, RRP, RWP, TRG, CTL, MODE = 0x004, 0x00C, 0x010, 0x014, 0x018, 0x01C, 0x020, 0x028
BUFWM, RRPHI, RWPHI, AXICTL, DBALO, DBAHI = 0x034, 0x038, 0x03C, 0x110, 0x118, 0x11C
FFSR, FFCR, LAR = 0x300, 0x304, 0xFB0
STS_FULL, STS_READY, STS_MEMERR = 1 << 0, 1 << 2, 1 << 5
MODE_CB, MODE_HWFIFO = 0, 2
FFCR_ENFT, FFCR_ENTI, FFCR_FONFLIN, FFCR_FLUSHMAN, FFCR_STOPONFL = 1, 1 << 1, 1 << 4, 1 << 6, 1 << 12
AXICTL_PRIV, AXICTL_BURST16 = 1 << 0, 0xF << 8
UNLOCK = 0xC5ACCE55
# ETMv4
ETM_BASE = 0xE0041000
TRCPRGCTLR, TRCSTATR = ETM_BASE + 0x004, ETM_BASE + 0x00C

#: symboles du tampon réservé par le firmware
SYMBOLS = ("__etr_buffer__", "etr_buffer", "__ETR_BUFFER")


def buffer_from_symbols(symbols) -> tuple[int, int] | None:
    """(adresse, taille) du tampon de l'ETR réservé par le firmware, d'après ses symboles."""
    objects = getattr(symbols, "objects", {}) or {}
    for name in SYMBOLS:
        if name in objects and objects[name][1]:
            return objects[name]
    return None


class EtrCapture:
    """ETR d'une vraie cible, en tampon circulaire de `size` octets à `buffer`."""

    def __init__(self, session, chip: TraceChip, buffer: int, size: int, trace_id: int = 2):
        if chip.etr is None:
            raise ValueError(f"{chip.name} : pas d'ETR")
        if buffer % 8 or size % 16 or size < 1024:
            raise ValueError("tampon de l'ETR : adresse alignée sur 8 octets, taille multiple de "
                             "16 octets, 1 Ko au moins")
        self.s = session
        self.chip = chip
        self.buffer, self.size = buffer, size
        self.trace_id = trace_id
        self.runs = 0                 # exécutions lues
        self.wraps = 0                # exécutions dont le tampon a tourné (début perdu)
        self.bytes = 0                # octets de trace ETM lus
        self.mem_errors = 0           # écritures de l'ETR refusées par le bus (STS.MemErr)
        self.last_raw = b""           # dernier tampon lu (trames), pour le diagnostic
        self.last_wrapped = False

    # ------------------------------------------------------------------ accès
    def _w(self, addr: int, value: int) -> None:
        self.s.write_u32(addr, value)

    def _r(self, addr: int) -> int:
        return self.s.read_u32(addr)

    def _wait(self, addr: int, mask: int, tries: int = 200) -> bool:
        for _ in range(tries):
            if self._r(addr) & mask:
                return True
        return False

    # ------------------------------------------------------------------ programmation
    def _chip_setup(self) -> list[str]:
        """Horloges de trace et maître AXI de l'ETR (perdus à chaque reset système)."""
        c, log = self.chip, []
        if c.dbgmcu_cr is not None:
            v = self._r(c.dbgmcu_cr) | c.dbgmcu_bits
            self._w(c.dbgmcu_cr, v)
            log.append(f"DBGMCU_CR = {v:#010x}")
        if c.etr_master is not None:
            addr, value = c.etr_master
            if self._r(addr) != value:
                self._w(addr, value)
            log.append(f"maître AXI de l'ETR ({addr:#010x}) = {value:#06x}")
        return log

    def setup(self) -> list[str]:
        """Programme la chaîne (cible arrêtée) et démarre les puits ; l'ETM est programmé à
        part (EtmConfig, port « tmc »). Retourne le journal."""
        c = self.chip
        log = self._chip_setup()
        for base in (c.funnel, c.etf, c.etr, c.replicator):
            if base is not None:
                self._w(base + LAR, UNLOCK)
        if c.funnel is not None:                # ETM seul (l'ITM sort par le SWO), maintien 3
            self._w(c.funnel, 3 << 8 | 1 << c.funnel_etm_port)
            log.append(f"funnel : entrée {c.funnel_etm_port} (ETM)")
        if c.replicator is not None:            # l'autre sortie (TPIU) ne doit rien retenir
            self._w(c.replicator + 4 * c.replicator_etr_port, 0)
            self._w(c.replicator + 4 * (1 - c.replicator_etr_port), 0xFF)
            log.append("réplicateur : tout vers l'ETR, rien vers le TPIU")
        self._start_sinks()
        log.append(f"ETR : tampon circulaire de {self.size >> 10} Ko à {self.buffer:#010x}"
                   + (", ETF en FIFO" if c.etf is not None else ""))
        return log

    def _start_sinks(self) -> None:
        """ETR (tampon vide) puis ETF, prêts à recevoir."""
        etr, etf = self.chip.etr, self.chip.etf
        self._w(etr + CTL, 0)
        self._wait(etr + STS, STS_READY)
        self._w(etr + RSZ, self.size // 4)
        self._w(etr + MODE, MODE_CB)
        self._w(etr + AXICTL, AXICTL_BURST16 | AXICTL_PRIV)
        self._w(etr + DBALO, self.buffer)
        self._w(etr + DBAHI, 0)
        self._w(etr + RWP, self.buffer)
        self._w(etr + RRP, self.buffer)
        self._w(etr + RWPHI, 0)
        self._w(etr + RRPHI, 0)
        self._w(etr + FFCR, FFCR_ENFT | FFCR_ENTI | FFCR_FONFLIN)
        self._w(etr + TRG, 0)
        self._w(etr + CTL, 1)
        if etf is not None:
            self._w(etf + CTL, 0)
            self._wait(etf + STS, STS_READY)
            self._w(etf + MODE, MODE_HWFIFO)
            self._w(etf + FFCR, FFCR_ENFT | FFCR_ENTI)
            self._w(etf + BUFWM, 0)
            self._w(etf + CTL, 1)

    def restart(self) -> None:
        """Avant une reprise : tampon vidé, puits relancés, ETM rallumé."""
        self._chip_setup()
        self._start_sinks()
        self._w(TRCPRGCTLR, 1)

    # ------------------------------------------------------------------ lecture
    def _flush(self, base: int) -> None:
        self._w(base + FFCR, self._r(base + FFCR) | FFCR_STOPONFL | FFCR_FLUSHMAN)
        self._wait(base + STS, STS_READY)

    def collect(self) -> bytes:
        """Cible arrêtée : ETM coupé, ETF et ETR vidés, tampon lu ; octets ETM (ID de flux
        `trace_id`) de la dernière exécution. `last_wrapped` : le tampon a tourné."""
        self._w(TRCPRGCTLR, 0)
        self._wait(TRCSTATR, 1)                 # IDLE : FIFO de l'ETM vidée
        if self.chip.etf is not None:
            self._flush(self.chip.etf)
        etr = self.chip.etr
        self._flush(etr)
        sts, rwp = self._r(etr + STS), self._r(etr + RWP)
        if sts & STS_MEMERR:
            self.mem_errors += 1
        end = self.buffer + self.size
        if not self.buffer <= rwp <= end:
            raise RuntimeError(f"ETR : pointeur d'écriture {rwp:#x} hors du tampon")
        wrapped = bool(sts & STS_FULL)
        if wrapped:
            raw = self.s.read_memory(rwp, end - rwp) + self.s.read_memory(self.buffer, rwp - self.buffer)
        else:
            raw = self.s.read_memory(self.buffer, rwp - self.buffer) if rwp > self.buffer else b""
        self.runs += 1
        self.wraps += wrapped
        self.last_raw, self.last_wrapped = raw, wrapped
        data = b"".join(chunk for sid, chunk in TpiuDeformatter(aligned=True).feed(raw)
                        if sid == self.trace_id)
        self.bytes += len(data)
        return data

    def describe(self) -> str:
        plural = "s" if self.wraps > 1 else ""
        wraps = f", {self.wraps} tampon{plural} plein{plural} (début perdu)" if self.wraps else ""
        err = f", {self.mem_errors} erreurs d'écriture en RAM" if self.mem_errors else ""
        return (f"ETR ({self.chip.name}, {self.size >> 10} Ko à {self.buffer:#010x}) : "
                f"{self.runs} exécution{'s' if self.runs > 1 else ''}, {self.bytes} octets ETM{wraps}{err}")
