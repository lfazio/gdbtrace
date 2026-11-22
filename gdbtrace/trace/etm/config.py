"""Programmation de l'ETM d'un Cortex-M3/M4 (ETMv3.5) ou d'un Cortex-M7 (ETMv4.0) et du
TPIU (port parallèle ou SWO).

Ce que GdbTrace fait pour `ETM.ON` (`--etm-config`) : déverrouiller l'ETM,
le programmer pour tracer tout le code (événement « toujours vrai », aucune plage
d'exclusion), lui donner un ID de trace, puis régler le TPIU : port parallèle de 1, 2 ou
4 bits, formateur actif (indispensable pour séparer ETM et ITM sur le même port).
Sur STM32, les broches TRACECLK/TRACED0-3 sont activées par DBGMCU_CR (option `stm32`).

Par SWO (`port="swo"`), sans sonde de trace : le même flux, formaté par le TPIU, sort sur
la broche SWO, capturée par la sonde de débogage (ST-Link, J-Link...) via OpenOCD. Le débit
est faible : l'ETMv3 produit ~0,76 bit par instruction (mesuré sur les démos), ~1 bit sur
le fil avec le formateur et le codage UART ; au-delà de ce que le SWO écoule, la FIFO de
l'ETM déborde et la trace a des trous (resynchronisés). `summary()` donne la fréquence de
cœur supportable sans perte.

ETMv4 (ETM-M7) : registres TRC* à la même adresse. Arrêt (TRCPRGCTLR = 0) et attente de
TRCSTATR.IDLE, configuration — trace de tout le code (ViewInst « toujours vrai », démarrée,
sans plage ni filtrage par niveau), ID de trace —, puis remise en marche. `stall` demande à
l'ETM de ralentir le cœur plutôt que de déborder (TRCSTALLCTLR.ISTALL).
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
ETMIDR = ETM_BASE + 0x1E4     # ETMv3 : ETMIDR ; ETMv4 (Cortex-M7...) : TRCIDR1, même champ
BITS_PER_INSTRUCTION = 0.76   # ETMv3 sans cycle-accurate (mesuré : 0,65 à 0,76)
CYCLES_PER_INSTRUCTION = 1.6  # Cortex-M3/M4, modèle sur les démos

# ETMv4 (ETM-M7)
TRCPRGCTLR = ETM_BASE + 0x004
TRCSTATR = ETM_BASE + 0x00C
TRCCONFIGR = ETM_BASE + 0x010
TRCEVENTCTL0R = ETM_BASE + 0x020
TRCEVENTCTL1R = ETM_BASE + 0x024
TRCSTALLCTLR = ETM_BASE + 0x02C
TRCTSCTLR = ETM_BASE + 0x030
TRCCCCTLR = ETM_BASE + 0x038
TRCBBCTLR = ETM_BASE + 0x03C
TRCTRACEIDR = ETM_BASE + 0x040
TRCVICTLR = ETM_BASE + 0x080
TRCVIIECTLR = ETM_BASE + 0x084
TRCVISSCTLR = ETM_BASE + 0x088
TRCLAR = ETM_BASE + 0xFB0
TRCSTATR_IDLE = 1 << 0
TRCCONFIGR_RES1 = 1 << 0
VICTLR_ALWAYS = 0x01 | 1 << 9        # EVENT = ressource 1 (toujours vrai), SSSTATUS : démarrée
STALL_ISTALL = 1 << 8 | 0xC          # ralentir le cœur, seuil haut (LEVEL)
BITS_PER_INSTRUCTION_V4 = 0.5        # ETMv4 : 0,24 à 0,48 mesuré sur les démos (un atome par branchement)

UNLOCK = 0xC5ACCE55
ETMCR_POWERDOWN = 1 << 0
ETMCR_STALL = 1 << 7
ETMCR_BRANCH_OUTPUT = 1 << 8
ETMCR_PROGRAMMING = 1 << 10
ETMCR_CYCLE_ACCURATE = 1 << 12
ETMCR_TIMESTAMP = 1 << 28
EVENT_ALWAYS = 0x6F            # ressource « toujours vrai » (fonction A)
TECR1_EXCLUDE_ALL_OFF = 1 << 24   # mode exclusion sans plage : tout est tracé


def etm_version(read_u32) -> int:
    """Version majeure de l'architecture ETM (ETMIDR[11:8] : 2 = ETMv3, 4 = ETMv4), 0 si absent."""
    try:
        return (read_u32(ETMIDR) >> 8) & 0xF
    except Exception:  # noqa: BLE001
        return 0


def swo_core_limit_hz(swo_hz: float, version: int = 3) -> float:
    """Fréquence de cœur dont l'ETM tient sans perte sur un SWO NRZ de `swo_hz` bit/s :
    10 bits par octet (UART), 15 octets utiles par trame TPIU de 16."""
    bits = BITS_PER_INSTRUCTION_V4 if version == 4 else BITS_PER_INSTRUCTION
    return swo_hz * 0.8 * 15 / 16 / bits * CYCLES_PER_INSTRUCTION


@dataclass
class EtmConfig:
    port_width: int = 4          # bits du port de trace parallèle : 1, 2 ou 4
    trace_id: int = 2            # ID de flux dans les trames TPIU (ITM : 1)
    stall: bool = False          # bloquer le cœur plutôt que perdre de la trace
    cycle_accurate: bool = False # cycles par instruction (analyse temporelle) ; ~8× plus de trace
    timestamps: bool = False
    stm32: bool = False          # activer les broches de trace via DBGMCU_CR
    port: str = "parallel"       # "parallel" : port de trace ; "swo" : broche SWO, formatée
    cpu_hz: int = 0              # SWO : horloge du cœur (TRACECLKIN)
    swo_hz: int = 2_000_000      # SWO : débit visé
    manchester: bool = False     # SWO : codage Manchester au lieu de NRZ (UART)
    version: int = 3             # architecture ETM : 3 (M3/M4) ou 4 (M7) ; apply() la détecte

    @property
    def prescaler(self) -> int:
        return max(0, round(self.cpu_hz / self.swo_hz) - 1)

    @property
    def actual_swo_hz(self) -> float:
        return self.cpu_hz / (self.prescaler + 1)

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
        if self.port not in ("parallel", "swo"):
            raise ValueError("port de trace : parallel ou swo")
        swo = self.port == "swo"
        if swo and self.cpu_hz <= 0:
            raise ValueError("ETM par SWO : préciser la fréquence du cœur (--cpu-hz)")
        if not swo and self.port_width not in (1, 2, 4):
            raise ValueError("largeur de port : 1, 2 ou 4 bits")
        writes = [RegWrite("DEMCR.TRCENA", DEMCR, 1 << 24, mask=1 << 24)]
        if self.stm32:      # TRACE_IOEN + TRACE_MODE (00 : SWO ; 01 : 1 bit, 10 : 2, 11 : 4)
            mode = 0 if swo else {1: 1, 2: 2, 4: 3}[self.port_width]
            writes.append(RegWrite("DBGMCU_CR.TRACE", DBGMCU_CR, 1 << 5 | mode << 6, mask=0xE0))
        if swo:
            writes += [
                RegWrite("TPIU_CSPSR", TPIU_CSPSR, 1),
                RegWrite("TPIU_ACPR", TPIU_ACPR, self.prescaler),
                RegWrite("TPIU_SPPR", TPIU_SPPR, 1 if self.manchester else 2),   # SWO
            ]
        else:
            writes += [
                RegWrite("TPIU_CSPSR", TPIU_CSPSR, 1 << (self.port_width - 1)),
                RegWrite("TPIU_ACPR", TPIU_ACPR, 0),
                RegWrite("TPIU_SPPR", TPIU_SPPR, 0),          # port parallèle synchrone
            ]
        writes.append(RegWrite("TPIU_FFCR", TPIU_FFCR, 0x102))  # formateur actif (EnFCont)
        if self.version == 4:
            return writes + self._etm4_writes()
        writes += [
            RegWrite("ETMLAR", ETMLAR, UNLOCK),
            RegWrite("ETMCR.prog", ETMCR, ETMCR_PROGRAMMING),  # sous tension, en programmation
            RegWrite("ETMTRACEIDR", ETMTRACEIDR, self.trace_id & 0x7F),
            RegWrite("ETMTEEVR", ETMTEEVR, EVENT_ALWAYS),
            RegWrite("ETMTECR1", ETMTECR1, TECR1_EXCLUDE_ALL_OFF),
            RegWrite("ETMCR", ETMCR, self.etmcr),             # fin de programmation : trace active
        ]
        return writes

    def _etm4_writes(self) -> list[RegWrite]:
        if self.cycle_accurate or self.timestamps:
            raise ValueError("ETMv4 : cycle-accurate et horodatage pas encore décodés par GdbTrace")
        return [
            RegWrite("TRCLAR", TRCLAR, UNLOCK),
            RegWrite("TRCPRGCTLR.off", TRCPRGCTLR, 0),       # arrêt ; apply() attend IDLE
            RegWrite("TRCCONFIGR", TRCCONFIGR, TRCCONFIGR_RES1),
            RegWrite("TRCEVENTCTL0R", TRCEVENTCTL0R, 0),
            RegWrite("TRCEVENTCTL1R", TRCEVENTCTL1R, 0),
            RegWrite("TRCSTALLCTLR", TRCSTALLCTLR, STALL_ISTALL if self.stall else 0),
            RegWrite("TRCTSCTLR", TRCTSCTLR, 0),
            RegWrite("TRCCCCTLR", TRCCCCTLR, 0),
            RegWrite("TRCBBCTLR", TRCBBCTLR, 0),
            RegWrite("TRCTRACEIDR", TRCTRACEIDR, self.trace_id & 0x7F),
            RegWrite("TRCVICTLR", TRCVICTLR, VICTLR_ALWAYS),
            RegWrite("TRCVIIECTLR", TRCVIIECTLR, 0),         # aucune plage : tout le code
            RegWrite("TRCVISSCTLR", TRCVISSCTLR, 0),
            RegWrite("TRCPRGCTLR", TRCPRGCTLR, 1),           # remise en marche
        ]

    def apply(self, session) -> list[tuple[str, int]]:
        """Écrit les registres (cible arrêtée) ; détecte d'abord l'architecture de l'ETM."""
        v = etm_version(session.read_u32)
        if v in (3, 4):
            self.version = v
        done = []
        for w in self.register_writes():
            value = w.value
            if w.mask is not None:
                value = (session.read_u32(w.addr) & ~w.mask) | (w.value & w.mask)
            session.write_u32(w.addr, value)
            done.append((w.name, value))
            if w.name == "TRCPRGCTLR.off":
                for _ in range(100):                          # l'ETMv4 doit être au repos
                    if session.read_u32(TRCSTATR) & TRCSTATR_IDLE:
                        break
                else:
                    raise RuntimeError("ETMv4 : TRCSTATR.IDLE jamais atteint")
        return done

    def summary(self) -> str:
        arch = f"ETMv{self.version}"
        if self.port == "swo":
            limit = swo_core_limit_hz(self.actual_swo_hz, self.version)
            warn = (f" ; cœur à {self.cpu_hz / 1e6:.0f} MHz > {limit / 1e6:.1f} MHz : la trace "
                    "aura des trous (couverture cumulée : borne basse ; Timing : inutilisable)"
                    if self.cpu_hz > limit else "")
            return (f"ETM par SWO ({arch}) : ID {self.trace_id}, {self.actual_swo_hz / 1e6:.3f} Mbit/s "
                    f"(ACPR={self.prescaler}, {'Manchester' if self.manchester else 'NRZ'}), "
                    f"formateur TPIU actif ; sans perte jusqu'à un cœur à ~{limit / 1e6:.1f} MHz"
                    + warn + (", stall" if self.stall else "")
                    + (", broches STM32" if self.stm32 else ""))
        return (f"ETM ({arch}) : ID {self.trace_id}, port {self.port_width} bits, formateur TPIU actif"
                + (", horodatage" if self.timestamps else "") + (", stall" if self.stall else "")
                + (", cycle-accurate" if self.cycle_accurate else "")
                + (", broches STM32" if self.stm32 else ""))
