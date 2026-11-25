"""Chrono : temps du cœur entre la dernière reprise visible et l'arrêt visible (RunTime).

Mesuré par la cible elle-même, sans trace :

- **SysTick** (Cortex-M0/M0+, qui n'ont pas de compteur de cycles ; QEMU) : compteur
  décroissant de `SYST_RVR + 1` cycles, et le compte de ses tours que tient le logiciel —
  `xTickCount` (+ `xPendedTicks`) de FreeRTOS, `uwTick` de la HAL STM32. Durée =
  Δticks × période + (compteur au départ − compteur à l'arrêt) ; un tick en attente
  (`ICSR.PENDSTSET` : le compteur est repassé par zéro, l'interruption n'est pas encore
  prise) compte. Sans compteur de ticks, la durée n'est connue qu'à une période près.
- **`DWT_CYCCNT`** (Cortex-M3 et au-delà, sur carte) : compteur de cycles 32 bits, activé à
  la première mesure ; le compte des ticks, s'il existe, rattrape ses tours (2³² cycles).

Le cœur arrêté par le débogueur, ni SysTick ni CYCCNT n'avancent : les arrêts ne sont pas
comptés. Les arrêts internes (breakpoints à action, AUTOHALT de la MTB) ne relancent pas la
mesure : `resume_internal` ne passe pas par les resume hooks. Un reset, une programmation ou
une connexion l'invalident.

Lire `SYST_CSR` efface son drapeau COUNTFLAG pour le logiciel sur certaines sondes : un
firmware qui l'attend (attente active sans interruption) peut en manquer un.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

SYST_CSR = 0xE000E010             # CSR, RVR, CVR contigus
ICSR = 0xE000ED04
PENDSTSET = 1 << 26
DEMCR = 0xE000EDFC
DWT_CTRL = 0xE0001000
DWT_CYCCNT = 0xE0001004
ARMV6M = {0xC20, 0xC60}
#: compteurs de ticks connus : (variable, variable de ticks en attente ou None)
TICK_COUNTERS = (("xTickCount", ("xPendedTicks", "uxPendedTicks")), ("uwTick", ()))
INVALIDATING = {"connected", "programmed", "reset"}


@dataclass
class Measure:
    """Une mesure : `cycles` (None si inconnue), d'où elle vient, et pourquoi elle est
    approximative (`note`)."""
    cycles: int | None
    source: str
    note: str = ""

    def seconds(self, cpu_hz: int) -> float | None:
        return self.cycles / cpu_hz if self.cycles is not None and cpu_hz else None


@dataclass
class _Start:
    source: str
    counter: int
    ticks: int | None
    period: int | None


def format_duration(seconds: float | None) -> str:
    """4,98 ms, 312 µs, 2,41 s (virgule décimale)."""
    if seconds is None:
        return "?"
    for unit, scale in (("s", 1.0), ("ms", 1e-3), ("µs", 1e-6)):
        if seconds >= scale or unit == "µs":
            v = seconds / scale
            text = f"{v:.3g}" if v < 1000 else f"{v:.0f}"
            return f"{text.replace('.', ',')} {unit}"
    return "?"


class Chrono:
    """Chrono d'une session : `start()` est un resume hook (cible encore arrêtée), `stop()`
    lit l'arrivée à l'arrêt visible (une fois par arrêt, `Session.stop_count`)."""

    def __init__(self, session, core: Callable[[], int | None] | None = None):
        self.s = session
        self.core = core or (lambda: None)
        self._start: _Start | None = None
        self._ticks: list[tuple[int, list[int]]] | None = None   # adresses (compteur, attente)
        self._counted = -1               # stop_count de la dernière mesure
        self.last: Measure | None = None
        self.total = 0                   # cycles cumulés depuis la remise à zéro
        self.runs = 0
        self.enabled = True

    # ------------------------------------------------------------------ source
    @property
    def simulated(self) -> bool:
        from .target import QemuTarget
        return isinstance(getattr(self.s, "target", None), QemuTarget)

    def source(self) -> str | None:
        target = getattr(self.s, "target", None)
        if target is not None and getattr(target, "native", False):
            return None                  # programme de l'hôte : ni SysTick ni DWT
        core = self.core()
        if self.simulated or core is None or core in ARMV6M:
            return "systick"
        return "cyccnt"

    def _tick_addrs(self) -> list[tuple[int, list[int]]]:
        """Adresses du compteur de ticks (et des ticks en attente), lues une fois par gdb."""
        if self._ticks is None:
            self._ticks = []
            for name, pending in TICK_COUNTERS:
                try:
                    addr = self.s.address_of_expr(name)
                except Exception:  # noqa: BLE001 - symbole absent : compteur suivant
                    continue
                extra = []
                for p in pending:
                    try:
                        extra.append(self.s.address_of_expr(p))
                        break
                    except Exception:  # noqa: BLE001
                        continue
                self._ticks.append((addr, extra))
                break
        return self._ticks

    def _read_ticks(self, pending_irq: bool) -> int | None:
        addrs = self._tick_addrs()
        if not addrs:
            return None
        addr, extra = addrs[0]
        ticks = self.s.read_u32(addr) + sum(self.s.read_u32(a) for a in extra)
        return ticks + (1 if pending_irq else 0)

    def _systick(self) -> tuple[int, int, int | None]:
        """(compteur, période, ticks) ; ValueError si SysTick est arrêté."""
        raw = self.s.read_memory(SYST_CSR, 12)
        csr, rvr, cvr = (int.from_bytes(raw[i:i + 4], "little") for i in (0, 4, 8))
        if not csr & 1:
            raise ValueError("SysTick arrêté")
        pending = bool(self.s.read_u32(ICSR) & PENDSTSET)
        return cvr & 0xFFFFFF, (rvr & 0xFFFFFF) + 1, self._read_ticks(pending)

    # ------------------------------------------------------------------ mesure
    def start(self) -> None:
        """Resume hook : lit le départ (cible arrêtée). Ne lève jamais."""
        self._start = None
        src = self.source() if self.enabled else None
        if src is None or getattr(self.s, "state", "stopped") != "stopped":
            return
        try:
            if src == "cyccnt":
                demcr = self.s.read_u32(DEMCR)
                if not demcr & 1 << 24:
                    self.s.write_u32(DEMCR, demcr | 1 << 24)
                ctrl = self.s.read_u32(DWT_CTRL)
                if not ctrl & 1:
                    self.s.write_u32(DWT_CTRL, ctrl | 1)
                period = ticks = None
                try:
                    _cvr, period, ticks = self._systick()
                except Exception:  # noqa: BLE001 - pas de SysTick : tours de CYCCNT non rattrapés
                    pass
                self._start = _Start(src, self.s.read_u32(DWT_CYCCNT), ticks, period)
            else:
                cvr, period, ticks = self._systick()
                self._start = _Start(src, cvr, ticks, period)
        except Exception:  # noqa: BLE001 - registres illisibles : pas de mesure
            self._start = None

    def invalidate(self) -> None:
        """Reset, programmation, connexion : le départ ne vaut plus rien."""
        self._start = None

    def stop(self) -> Measure | None:
        """Arrêt visible : la mesure depuis la dernière reprise (lue une seule fois par arrêt),
        ajoutée au cumul. None sans départ valable."""
        count = getattr(self.s, "stop_count", 0)
        if count == self._counted:
            return self.last
        self._counted = count
        reason = (getattr(self.s, "stop_reason", None) or {}).get("reason")
        st, self._start = self._start, None
        if st is None or reason in INVALIDATING:
            return None
        try:
            m = self._measure(st)
        except Exception as exc:  # noqa: BLE001 - registres illisibles
            m = Measure(None, st.source, f"lecture impossible : {exc}")
        self.last = m
        if m.cycles is not None:
            self.total += m.cycles
            self.runs += 1
        return m

    def _measure(self, st: _Start) -> Measure:
        label = "SysTick (QEMU : temps simulé)" if self.simulated and st.source == "systick" \
            else {"systick": "SysTick", "cyccnt": "DWT_CYCCNT"}[st.source]
        if st.source == "cyccnt":
            cyc = (self.s.read_u32(DWT_CYCCNT) - st.counter) & 0xFFFFFFFF
            if st.ticks is not None and st.period:
                try:
                    _cvr, _p, ticks = self._systick()
                except Exception:  # noqa: BLE001
                    ticks = None
                if ticks is not None:       # tours de 2³² cycles d'après les ticks
                    coarse = (ticks - st.ticks) * st.period
                    cyc += round((coarse - cyc) / 2 ** 32) * 2 ** 32 if coarse > 2 ** 31 else 0
            return Measure(cyc, label)
        cvr, period, ticks = self._systick()
        if period != st.period:
            return Measure(None, label, "période de SysTick changée pendant la marche")
        if ticks is not None and st.ticks is not None:
            return Measure((ticks - st.ticks) * period + st.counter - cvr, label)
        return Measure((st.counter - cvr) % period, label,
                       "à une période de SysTick près (pas de compteur de ticks : xTickCount, uwTick)")

    def reset(self) -> None:
        """Cumul remis à zéro (Run › Remettre le chrono à zéro)."""
        self.total = 0
        self.runs = 0
