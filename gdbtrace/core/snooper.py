"""SNOOPer : variables échantillonnées à période fixe par le canal mémoire, cible en marche,
sans trace (Var.DRAW d'un Cortex-M0/M0+, de QEMU, d'une carte sans capture SWO).

Cible arrêtée, la disposition de chaque variable (adresse, taille, type) vient de gdb
(`trace/datatrace.variable_layout`). Ensuite un fil lit ses octets toutes les `period_ms`
millisecondes par le canal mémoire en direct (OpenOCD TCL, relais ST-LINK, QEMU QMP) et range
la valeur dans le même `DataTraceSink` que la trace de données du DWT : la vue Variables
tracées et `gdbtrace.var_samples()` les montrent de la même façon.

Chaque variable y est un `TracedVar` de numéro négatif (-1, -2…), jamais un comparateur du
DWT, d'accès « sample ». Horodatage : l'horloge de l'hôte, à la précision du fil
(la milliseconde), pas celle du cœur ; une période plus courte que la lecture d'une variable
par le canal (~1-2 ms) n'est pas tenue. Une valeur qui change et revient entre deux lectures
n'est pas vue : c'est un échantillonnage, pas une trace.
"""
from __future__ import annotations

import threading
from typing import Callable

from ..trace.datatrace import Sample, TracedVar

MIN_PERIOD_MS = 1


class Snooper:
    def __init__(self, sink, read: Callable[[int, int], bytes] | None,
                 is_running: Callable[[], bool], period_ms: int = 10):
        self.sink = sink
        self.read = read
        self.is_running = is_running
        self.period_ms = max(MIN_PERIOD_MS, int(period_ms))
        self.vars: dict[int, TracedVar] = {}
        self._next = -1
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.errors = 0
        self.last_error = ""
        self.reads = 0

    def add(self, expr: str, addr: int, size: int, layout) -> TracedVar:
        """Une variable de plus (ou la même, remplacée)."""
        self.remove(expr)
        with self._lock:
            var = TracedVar(expr, addr, size, self._next, access="sample", layout=layout)
            self._next -= 1
            self.vars[var.comparator] = var
        self.sink.add(var)
        self._ensure_thread()
        return var

    def remove(self, expr: str | None = None) -> list[str]:
        with self._lock:
            gone = [v for v in self.vars.values() if expr is None or v.expr == expr]
            for v in gone:
                del self.vars[v.comparator]
        for v in gone:
            self.sink.remove(v.comparator)
        return [f"{v.expr} n'est plus échantillonnée" for v in gone]

    def _ensure_thread(self) -> None:
        if self._thread is None or not self._thread.is_alive():
            self._stop.clear()
            self._thread = threading.Thread(target=self._loop, name="snooper", daemon=True)
            self._thread.start()

    def _loop(self) -> None:
        while not self._stop.wait(self.period_ms / 1000.0):
            if not self.vars or self.read is None:
                continue
            try:
                running = self.is_running()
            except Exception:  # noqa: BLE001
                running = False
            if not running:
                continue
            with self._lock:
                todo = list(self.vars.values())
            for var in todo:
                try:
                    data = self.read(var.addr, var.size)
                except Exception as exc:  # noqa: BLE001 - lecture ratée : comptée, on continue
                    self.errors += 1
                    self.last_error = str(exc)
                    continue
                self.reads += 1
                self.sink.add_sample(var, Sample(self.sink.host_time(), int.from_bytes(data, "little"),
                                                 var.size, "sample"))

    def close(self) -> None:
        self._stop.set()
        self.remove()
