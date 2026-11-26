"""Compteurs de performance du cœur (BMC : benchmark counters), d'un arrêt à l'autre.

- **DWT** (Cortex-M3, M4, M7 ; ARMv7-M et ARMv8-M sans PMU) : CYCCNT (32 bits) et cinq
  compteurs de 8 bits — CPICNT (cycles supplémentaires des instructions multicycles), EXCCNT
  (surcoût des exceptions), SLEEPCNT (cycles en sommeil), LSUCNT (cycles d'attente des
  chargements et rangements), FOLDCNT (instructions repliées, en zéro cycle). Un compteur de
  8 bits fait le tour en 256 événements : ses tours sont rattrapés par les paquets « event
  counter » de l'ITM (`EventCounterTally`) quand une capture SWO tourne pendant toute
  l'exécution ; sinon l'écart n'est connu que modulo 256, exact si CYCCNT a avancé de moins
  de 256. Instructions exécutées (formule d'ARM) : ΔCYC − ΔCPI − ΔEXC − ΔSLEEP − ΔLSU + ΔFOLD.
- **PMU de l'ARMv8.1-M** (Cortex-M55, M85 ; base 0xE0003000, manuel 101051) : huit compteurs
  de 16 bits, chaînés ici par paires en 32 bits (le compteur impair compte les débordements du
  pair, événement CHAIN) : INST_RETIRED, L1D_CACHE_REFILL, L1I_CACHE_REFILL, EXC_TAKEN ; plus
  PMU_CCNTR (alias de DWT_CYCCNT). PMU et compteurs de profil du DWT s'excluent : la PMU, si
  elle existe, est préférée.

Rien n'est remis à zéro sur la cible (le chrono lit DWT_CYCCNT) : on lit au départ (resume
hook, cible encore arrêtée) et à l'arrêt visible, et on fait la différence. Désactivés par
défaut : activer les compteurs du DWT fait aussi émettre leurs paquets de débordement sur le
SWO (débit) ; `enable()` les arme. Absents sur un ARMv6-M et sous QEMU (DWT non émulé : relu
à 0) : dit, jamais inventé.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Callable

DEMCR = 0xE000EDFC
TRCENA = 1 << 24
DWT_CTRL = 0xE0001000                 # puis CYCCNT, CPICNT, EXCCNT, SLEEPCNT, LSUCNT, FOLDCNT
CYCCNTENA = 1 << 0
EVTENA = {"CPI": 17, "EXC": 18, "SLEEP": 19, "LSU": 20, "FOLD": 21}
NOPRFCNT, NOCYCCNT = 1 << 24, 1 << 25
DWT_NAMES = ("CPI", "EXC", "SLEEP", "LSU", "FOLD")

PMU_EVCNTR = 0xE0003000              # 8 × 16 bits ; PMU_CCNTR à +0x7C
PMU_CCNTR = 0xE000307C
PMU_EVTYPER = 0xE0003400
PMU_CNTENSET = 0xE0003C00
PMU_CNTENCLR = 0xE0003C20
PMU_TYPE = 0xE0003E00
PMU_CTRL = 0xE0003E04
CHAIN = 0x001E
PMU_EVENTS = (("INST_RETIRED", 0x0008, "instructions exécutées"),
              ("L1D_CACHE_REFILL", 0x0003, "remplissages du cache de données"),
              ("L1I_CACHE_REFILL", 0x0001, "remplissages du cache d'instructions"),
              ("EXC_TAKEN", 0x0009, "exceptions prises"))

LABELS = {"CYC": "cycles", "CPI": "cycles d'attente des instructions multicycles",
          "EXC": "surcoût des exceptions", "SLEEP": "cycles en sommeil",
          "LSU": "cycles d'attente des chargements et rangements",
          "FOLD": "instructions repliées (zéro cycle)"}
ARMV6M = {0xC20, 0xC60}
INVALIDATING = {"connected", "programmed", "reset"}


@dataclass
class CounterValue:
    name: str
    label: str
    delta: int | None
    exact: bool = True
    note: str = ""


@dataclass
class CounterMeasure:
    source: str                        # « DWT » ou « PMU »
    values: list[CounterValue] = field(default_factory=list)
    instructions: int | None = None
    note: str = ""

    def get(self, name: str) -> CounterValue | None:
        return next((v for v in self.values if v.name == name), None)

    @property
    def cycles(self) -> int | None:
        c = self.get("CYC")
        return c.delta if c else None

    @property
    def ipc(self) -> float | None:
        if self.instructions is None or not self.cycles:
            return None
        return self.instructions / self.cycles

    def as_dict(self) -> dict:
        return {"source": self.source, "note": self.note, "instructions": self.instructions,
                "ipc": self.ipc, "compteurs": {v.name: {"valeur": v.delta, "exact": v.exact,
                                                        "sens": v.label, "note": v.note}
                                               for v in self.values}}


class EventCounterTally:
    """Abonné ITM : débordements des compteurs de 8 bits du DWT (paquets « event counter »)
    depuis `mark()`."""

    def __init__(self):
        self._lock = threading.Lock()
        self.counts: dict[str, int] = {}

    def mark(self) -> None:
        with self._lock:
            self.counts = {}

    def __call__(self, pkts) -> None:
        from ..trace.itm import EventCounter
        with self._lock:
            for p in pkts:
                if isinstance(p, EventCounter):
                    for name in p.overflows:
                        self.counts[name] = self.counts.get(name, 0) + 1

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return dict(self.counts)


def _u32s(data: bytes) -> list[int]:
    return [int.from_bytes(data[i:i + 4], "little") for i in range(0, len(data), 4)]


class Counters:
    """Compteurs d'une session : `enable()` les arme, `start()` est un resume hook,
    `stop()` mesure à l'arrêt visible (une fois par `Session.stop_count`)."""

    def __init__(self, session, core: Callable[[], int | None] | None = None,
                 tally: Callable[[], EventCounterTally | None] | None = None,
                 capturing: Callable[[], bool] | None = None):
        self.s = session
        self.core = core or (lambda: None)
        self.tally = tally or (lambda: None)
        self.capturing = capturing or (lambda: False)
        self.enabled = False
        self.kind: str | None = None          # DWT, PMU, ou None (absents)
        self.reason = ""                      # pourquoi absents
        self._start: dict | None = None
        self._counted = -1
        self.last: CounterMeasure | None = None
        self.totals: dict[str, int] = {}
        self.runs = 0

    # ------------------------------------------------------------------ armement
    def enable(self, on: bool = True) -> str:
        """Arme (ou désarme) les compteurs, cible arrêtée ; rend ce qui a été fait, en clair."""
        if not on:
            self.enabled, self._start = False, None
            if self.kind == "DWT":
                try:
                    ctrl = self.s.read_u32(DWT_CTRL)
                    self.s.write_u32(DWT_CTRL, ctrl & ~sum(1 << b for b in EVTENA.values()))
                except Exception:  # noqa: BLE001 - déconnecté : rien à défaire
                    pass
            return "compteurs désarmés"
        core = self.core()
        if core in ARMV6M:
            self.kind, self.reason = None, "Cortex-M0/M0+ (ARMv6-M) : pas de compteurs de profil"
            return self.reason
        if getattr(getattr(self.s, "target", None), "native", False):
            self.kind, self.reason = None, "programme de l'hôte : pas de compteurs Cortex-M"
            return self.reason
        self.s.write_u32(DEMCR, self.s.read_u32(DEMCR) | TRCENA)
        if self._has_pmu():
            self._arm_pmu()
            self.kind, self.reason = "PMU", ""
        else:
            ctrl = self.s.read_u32(DWT_CTRL)
            want = CYCCNTENA | (0 if ctrl & NOPRFCNT else sum(1 << b for b in EVTENA.values()))
            if ctrl & NOCYCCNT:
                self.kind, self.reason = None, "DWT sans compteur de cycles (NOCYCCNT)"
                return self.reason
            self.s.write_u32(DWT_CTRL, ctrl | want)
            if not self.s.read_u32(DWT_CTRL) & CYCCNTENA:
                self.kind = None
                self.reason = "DWT absent ou non émulé (QEMU) : compteurs relus à 0"
                return self.reason
            self.kind = "DWT"
            self.reason = "" if not ctrl & NOPRFCNT else "DWT sans compteurs de profil (NOPRFCNT) : cycles seuls"
        self.enabled = True
        self._start = None
        return f"compteurs armés ({self.kind}) : mesure à partir de la prochaine exécution"

    def _has_pmu(self) -> bool:
        try:
            t = self.s.read_u32(PMU_TYPE)
        except Exception:  # noqa: BLE001 - zone absente
            return False
        return t != 0 and t != 0xFFFFFFFF and (t & 0xFF) >= 2 * len(PMU_EVENTS)

    def _arm_pmu(self) -> None:
        s = self.s
        for k, (_name, event, _label) in enumerate(PMU_EVENTS):
            s.write_u32(PMU_EVTYPER + 8 * k, event)               # compteur pair : l'événement
            s.write_u32(PMU_EVTYPER + 8 * k + 4, CHAIN)           # impair : ses débordements
        s.write_u32(PMU_CNTENSET, (1 << 31) | ((1 << (2 * len(PMU_EVENTS))) - 1))
        s.write_u32(PMU_CTRL, s.read_u32(PMU_CTRL) | 1)           # E, sans remise à zéro

    # ------------------------------------------------------------------ lectures
    def _read(self) -> dict:
        s = self.s
        if self.kind == "PMU":
            ev = s.read_memory(PMU_EVCNTR, 4 * 2 * len(PMU_EVENTS))
            raw = [v & 0xFFFF for v in _u32s(ev)]
            out = {name: raw[2 * k] | raw[2 * k + 1] << 16 for k, (name, _e, _l) in enumerate(PMU_EVENTS)}
            out["CYC"] = s.read_u32(PMU_CCNTR)
            return out
        words = _u32s(s.read_memory(DWT_CTRL, 28))
        out = {"CYC": words[1]}
        for k, name in enumerate(DWT_NAMES):
            out[name] = words[2 + k] & 0xFF
        return out

    def start(self) -> None:
        """Resume hook : valeurs au départ (une lecture)."""
        self._start = None
        if not self.enabled or self.s.state != "stopped":
            return
        try:
            self._start = {"values": self._read(), "capture": self.capturing()}
        except Exception:  # noqa: BLE001 - lecture impossible : pas de mesure
            self._start = None
            return
        t = self.tally()
        if t is not None:
            t.mark()

    def stop(self) -> CounterMeasure | None:
        """Mesure de l'exécution qui vient de finir (cible arrêtée), ou None."""
        if not self.enabled or self.s.state != "stopped":
            return None
        if self._counted == self.s.stop_count:
            return self.last
        self._counted = self.s.stop_count
        start, self._start = self._start, None
        reason = (getattr(self.s, "stop_reason", None) or {}).get("reason", "")
        if start is None or reason in INVALIDATING:
            self.last = None
            return None
        end = self._read()
        m = self._measure(start, end)
        self.last = m
        self.runs += 1
        for v in m.values:
            if v.delta is not None and v.exact:
                self.totals[v.name] = self.totals.get(v.name, 0) + v.delta
        if m.instructions is not None:
            self.totals["INSTR"] = self.totals.get("INSTR", 0) + m.instructions
        return m

    def _measure(self, start: dict, end: dict) -> CounterMeasure:
        a = start["values"]
        cyc = (end["CYC"] - a["CYC"]) & 0xFFFFFFFF
        if self.kind == "PMU":
            m = CounterMeasure("PMU", [CounterValue("CYC", LABELS["CYC"], cyc)])
            for name, _event, label in PMU_EVENTS:
                m.values.append(CounterValue(name, label, (end[name] - a[name]) & 0xFFFFFFFF))
            m.instructions = m.get("INST_RETIRED").delta
            return m
        m = CounterMeasure("DWT", [CounterValue("CYC", LABELS["CYC"], cyc)])
        tally = self.tally()
        overflow = tally.snapshot() if tally is not None and start["capture"] and self.capturing() else None
        if self.reason:                        # NOPRFCNT : cycles seuls
            m.note = self.reason
            return m
        all_exact = True
        for name in DWT_NAMES:
            d = (end[name] - a[name]) & 0xFF
            if overflow is not None:
                d += 256 * overflow.get(name, 0)
                v = CounterValue(name, LABELS[name], d)
            elif cyc < 256:
                v = CounterValue(name, LABELS[name], d)
            else:
                v = CounterValue(name, LABELS[name], d, exact=False,
                                 note="modulo 256 (pas de capture SWO pour compter les tours)")
                all_exact = False
            m.values.append(v)
        if all_exact:
            g = {v.name: v.delta for v in m.values}
            m.instructions = g["CYC"] - g["CPI"] - g["EXC"] - g["SLEEP"] - g["LSU"] + g["FOLD"]
        else:
            m.note = ("compteurs de 8 bits connus modulo 256 : lancer une capture SWO (paquets de "
                      "débordement de l'ITM) pour une longue exécution")
        return m

    def reset(self) -> None:
        self.totals, self.runs = {}, 0
