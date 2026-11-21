"""Programmation de la trace « légère » d'un Cortex-M3/M4/M7 : ITM, DWT, TPIU (SWO).

Ce que GdbTrace fait pour `ITM.ON` / `SWO.ON` (`--swo-config`) : activer la trace (DEMCR.TRCENA),
déverrouiller et activer l'ITM, choisir les ports de stimulus, régler l'échantillonnage du
PC et la trace des exceptions dans le DWT, puis le TPIU (vitesse SWO, NRZ, formateur
coupé). Les broches SWO propres au microcontrôleur (ex. DBGMCU_CR.TRACE_IOEN des STM32)
sont réglées par OpenOCD (`tpiu enable`) ou par le firmware.
"""
from __future__ import annotations

from dataclasses import dataclass, field

DEMCR = 0xE000EDFC
ITM_STIM = 0xE0000000
ITM_TER = 0xE0000E00
ITM_TPR = 0xE0000E40
ITM_TCR = 0xE0000E80
ITM_LAR = 0xE0000FB0
DWT_CTRL = 0xE0001000
TPIU_CSPSR = 0xE0040004
TPIU_ACPR = 0xE0040010
TPIU_SPPR = 0xE00400F0
TPIU_FFCR = 0xE0040304

UNLOCK = 0xC5ACCE55
DWT_MASK = 0x1 | 0x1E | 0x1E0 | 0x200 | 0xC00 | 0x1000 | 0x10000


@dataclass
class RegWrite:
    name: str
    addr: int
    value: int
    mask: int | None = None        # écriture partielle (lecture-modification-écriture)


@dataclass
class SwoConfig:
    cpu_hz: int                     # horloge du cœur (= TRACECLKIN du TPIU en SWO)
    swo_hz: int = 2_000_000         # débit SWO visé
    ports: int = 0x0000_0003        # ports de stimulus activés (bit n = port n)
    timestamps: bool = True
    pc_sampling_hz: int | None = 10_000    # None : pas d'échantillonnage du PC
    exceptions: bool = True
    manchester: bool = False
    trace_bus_id: int = 1
    notes: list[str] = field(default_factory=list, repr=False)

    # ------------------------------------------------------------------ calculs
    @property
    def prescaler(self) -> int:
        return max(0, round(self.cpu_hz / self.swo_hz) - 1)

    @property
    def actual_swo_hz(self) -> float:
        return self.cpu_hz / (self.prescaler + 1)

    def pc_sampling(self) -> tuple[int, int, float]:
        """(CYCTAP, POSTPRESET, fréquence réelle) au plus près de `pc_sampling_hz`.
        Un échantillon tous les (POSTPRESET + 1) × (CYCTAP ? 1024 : 64) cycles."""
        want = self.pc_sampling_hz or 1
        best = None
        for tap, div in ((0, 64), (1, 1024)):
            for preset in range(16):
                hz = self.cpu_hz / ((preset + 1) * div)
                err = abs(hz - want) / want
                if best is None or err < best[0]:
                    best = (err, tap, preset, hz)
        return best[1], best[2], best[3]

    # ------------------------------------------------------------------ registres
    def register_writes(self) -> list[RegWrite]:
        tcr = (self.trace_bus_id & 0x7F) << 16 | 1 << 3 | 1 << 2 | (1 << 1 if self.timestamps else 0) | 1
        dwt = 1 | 1 << 10                                   # CYCCNTENA, SYNCTAP = CYCCNT[24]
        if self.pc_sampling_hz:
            tap, preset, _ = self.pc_sampling()
            dwt |= preset << 1 | preset << 5 | tap << 9 | 1 << 12   # POSTPRESET, POSTINIT, PCSAMPLENA
        if self.exceptions:
            dwt |= 1 << 16                                  # EXCTRCENA
        return [
            RegWrite("DEMCR.TRCENA", DEMCR, 1 << 24, mask=1 << 24),
            RegWrite("TPIU_CSPSR", TPIU_CSPSR, 1),
            RegWrite("TPIU_ACPR", TPIU_ACPR, self.prescaler),
            RegWrite("TPIU_SPPR", TPIU_SPPR, 1 if self.manchester else 2),
            RegWrite("TPIU_FFCR", TPIU_FFCR, 0x100),           # formateur coupé
            RegWrite("ITM_LAR", ITM_LAR, UNLOCK),
            RegWrite("ITM_TCR", ITM_TCR, tcr),
            RegWrite("ITM_TER", ITM_TER, self.ports & 0xFFFFFFFF),
            RegWrite("ITM_TPR", ITM_TPR, 0),                   # ports accessibles hors mode privilégié
            RegWrite("DWT_CTRL", DWT_CTRL, dwt, mask=DWT_MASK),
        ]

    def apply(self, session) -> list[tuple[str, int, int]]:
        """Écrit les registres (cible arrêtée) ; retourne [(nom, écrit, relu)]."""
        done = []
        for w in self.register_writes():
            value = w.value
            if w.mask is not None:
                cur = session.read_u32(w.addr)
                value = (cur & ~w.mask) | (w.value & w.mask)
            session.write_u32(w.addr, value)
            done.append((w.name, value, session.read_u32(w.addr) if w.name != "ITM_LAR" else value))
        return done

    def summary(self) -> str:
        parts = [f"SWO {self.actual_swo_hz / 1e6:.3f} MHz (ACPR={self.prescaler})",
                 f"ports 0x{self.ports:08x}"]
        if self.pc_sampling_hz:
            tap, preset, hz = self.pc_sampling()
            parts.append(f"PC {hz:.0f} éch./s (CYCTAP={tap}, POSTPRESET={preset})")
        if self.exceptions:
            parts.append("exceptions")
        if self.timestamps:
            parts.append("horodatage")
        return ", ".join(parts)


def openocd_tpiu_commands(cfg: SwoConfig, tpiu: str, port: int) -> list[str]:
    """Commandes `monitor` pour qu'OpenOCD capture le SWO et le serve en TCP sur `port`."""
    proto = "manchester" if cfg.manchester else "uart"
    return [
        f"{tpiu} configure -protocol {proto} -traceclk {cfg.cpu_hz} -pin-freq {cfg.swo_hz} "
        f"-output :{port} -formatter 0",
        f"{tpiu} enable",
    ]


def openocd_tpiu_name(session) -> str:
    """Nom de l'objet TPIU déclaré par la configuration OpenOCD (ex. stm32f4x.tpiu)."""
    names = session.monitor("tpiu names").split()
    if not names:
        raise RuntimeError("aucun TPIU déclaré dans la configuration OpenOCD")
    return names[0]
