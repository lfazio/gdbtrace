"""Programmation de l'ETM d'un Cortex-M3/M4 (ETMv3.5) et du TPIU en mode parallèle.

Ce que GdbTrace fait pour `ETM.ON` (`--etm-config`) : déverrouiller l'ETM,
le programmer pour tracer tout le code (événement « toujours vrai », aucune plage
d'exclusion), lui donner un ID de trace, puis régler le TPIU : port parallèle de 1, 2 ou
4 bits, formateur actif (indispensable pour séparer ETM et ITM sur le même port).
Sur STM32, les broches TRACECLK/TRACED0-3 sont activées par DBGMCU_CR (option `stm32`).
"""
from __future__ import annotations

from dataclasses import dataclass

from ..config import RegWrite

DEMCR = 0xE000EDFC
ETM_BASE = 0xE0041000
ETMCR = ETM_BASE + 0x000
ETMTEEVR = ETM_BASE + 0x020
ETMTECR1 = ETM_BASE + 0x024
ETMTRACEIDR = ETM_BASE + 0x200
ETMLAR = ETM_BASE + 0xFB0
TPIU_CSPSR = 0xE0040004
TPIU_ACPR = 0xE0040010
TPIU_SPPR = 0xE00400F0
TPIU_FFCR = 0xE0040304
DBGMCU_CR = 0xE0042004

UNLOCK = 0xC5ACCE55
ETMCR_POWERDOWN = 1 << 0
ETMCR_STALL = 1 << 7
ETMCR_BRANCH_OUTPUT = 1 << 8
ETMCR_PROGRAMMING = 1 << 10
ETMCR_CYCLE_ACCURATE = 1 << 12
ETMCR_TIMESTAMP = 1 << 28
EVENT_ALWAYS = 0x6F            # ressource « toujours vrai » (fonction A)
TECR1_EXCLUDE_ALL_OFF = 1 << 24   # mode exclusion sans plage : tout est tracé


@dataclass
class EtmConfig:
    port_width: int = 4          # bits du port de trace parallèle : 1, 2 ou 4
    trace_id: int = 2            # ID de flux dans les trames TPIU (ITM : 1)
    stall: bool = False          # bloquer le cœur plutôt que perdre de la trace
    cycle_accurate: bool = False # cycles par instruction (analyse temporelle) ; ~8× plus de trace
    timestamps: bool = False
    stm32: bool = False          # activer les broches de trace via DBGMCU_CR

    @property
    def etmcr(self) -> int:
        v = 0
        if self.stall:
            v |= ETMCR_STALL
        if self.timestamps:
            v |= ETMCR_TIMESTAMP
        if self.cycle_accurate:
            v |= ETMCR_CYCLE_ACCURATE
        return v

    def register_writes(self) -> list[RegWrite]:
        if self.port_width not in (1, 2, 4):
            raise ValueError("largeur de port : 1, 2 ou 4 bits")
        writes = [RegWrite("DEMCR.TRCENA", DEMCR, 1 << 24, mask=1 << 24)]
        if self.stm32:      # TRACE_IOEN + TRACE_MODE (01 : 1 bit, 10 : 2 bits, 11 : 4 bits)
            mode = {1: 1, 2: 2, 4: 3}[self.port_width]
            writes.append(RegWrite("DBGMCU_CR.TRACE", DBGMCU_CR, 1 << 5 | mode << 6, mask=0xE0))
        writes += [
            RegWrite("TPIU_CSPSR", TPIU_CSPSR, 1 << (self.port_width - 1)),
            RegWrite("TPIU_ACPR", TPIU_ACPR, 0),
            RegWrite("TPIU_SPPR", TPIU_SPPR, 0),              # port parallèle synchrone
            RegWrite("TPIU_FFCR", TPIU_FFCR, 0x102),          # formateur actif (EnFCont)
            RegWrite("ETMLAR", ETMLAR, UNLOCK),
            RegWrite("ETMCR.prog", ETMCR, ETMCR_PROGRAMMING),  # sous tension, en programmation
            RegWrite("ETMTRACEIDR", ETMTRACEIDR, self.trace_id & 0x7F),
            RegWrite("ETMTEEVR", ETMTEEVR, EVENT_ALWAYS),
            RegWrite("ETMTECR1", ETMTECR1, TECR1_EXCLUDE_ALL_OFF),
            RegWrite("ETMCR", ETMCR, self.etmcr),             # fin de programmation : trace active
        ]
        return writes

    def apply(self, session) -> list[tuple[str, int]]:
        done = []
        for w in self.register_writes():
            value = w.value
            if w.mask is not None:
                value = (session.read_u32(w.addr) & ~w.mask) | (w.value & w.mask)
            session.write_u32(w.addr, value)
            done.append((w.name, value))
        return done

    def summary(self) -> str:
        return (f"ETM : ID {self.trace_id}, port {self.port_width} bits, formateur TPIU actif"
                + (", horodatage" if self.timestamps else "") + (", stall" if self.stall else "")
                + (", cycle-accurate" if self.cycle_accurate else "")
                + (", broches STM32" if self.stm32 else ""))
