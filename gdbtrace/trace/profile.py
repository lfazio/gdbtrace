"""Profilage statistique (PERF) : où le CPU passe-t-il son temps ?

Chaque échantillon est une valeur de PC prise pendant que la cible tourne, sans
instrumenter le code. Trois sources, du plus au moins précis :
- paquets « PC sample » du DWT reçus par SWO (`PcSample`, trace/itm.py) ;
- lecture de DWT_PCSR par l'AHB-AP (OpenOCD, cœur non arrêté) ;
- sous QEMU, le PC de la vCPU lu par le moniteur.
Les échantillons sont agrégés par fonction, par ligne et par tâche RTOS.
"""
from __future__ import annotations

import threading
import time
from collections import Counter
from dataclasses import dataclass
from typing import Callable

UNKNOWN = "<hors symboles>"
SLEEP = "<sommeil>"


@dataclass
class Row:
    name: str
    samples: int
    percent: float


@dataclass
class LineRow:
    file: str
    line: int
    samples: int
    percent: float                 # % des échantillons de la fonction


class Profile:
    """Agrégats thread-safe (l'échantillonneur écrit, la GUI lit)."""

    def __init__(self, symbols=None):
        self.symbols = symbols
        self._lock = threading.Lock()
        self._cache: dict[int, tuple[str, tuple[str, int] | None]] = {}
        self.reset()

    def reset(self) -> None:
        with self._lock:
            self.total = 0
            self.functions: Counter[str] = Counter()
            self.lines: dict[str, Counter[tuple[str, int]]] = {}
            self.tasks: Counter[str] = Counter()
            self.first: float | None = None
            self.last: float | None = None

    def _resolve(self, pc: int) -> tuple[str, tuple[str, int] | None]:
        hit = self._cache.get(pc)
        if hit is not None:
            return hit
        name, line = UNKNOWN, None
        if self.symbols is not None:
            f = self.symbols.function_at(pc)
            if f is not None:
                name = f.name
                le = self.symbols.line_at(pc)
                if le is not None:
                    line = (le.file, le.line)
        if len(self._cache) < 100_000:
            self._cache[pc] = (name, line)
        return name, line

    def add(self, pc: int | None, task: str | None = None) -> None:
        """`pc=None` : cœur en sommeil (paquet SWO « sleep »)."""
        if pc is None:
            name, line = SLEEP, None
        else:
            name, line = self._resolve(pc & ~1)
        now = time.monotonic()
        with self._lock:
            self.total += 1
            self.functions[name] += 1
            if line is not None:
                self.lines.setdefault(name, Counter())[line] += 1
            if task is not None:
                self.tasks[task] += 1
            if self.first is None:
                self.first = now
            self.last = now

    # ------------------------------------------------------------------ lecture
    def top_functions(self, n: int | None = None) -> list[Row]:
        with self._lock:
            total = self.total or 1
            return [Row(k, v, 100.0 * v / total) for k, v in self.functions.most_common(n)]

    def lines_of(self, func: str) -> list[LineRow]:
        with self._lock:
            c = self.lines.get(func, Counter())
            total = sum(c.values()) or 1
            return [LineRow(f, ln, v, 100.0 * v / total) for (f, ln), v in c.most_common()]

    def task_rows(self) -> list[Row]:
        with self._lock:
            total = sum(self.tasks.values()) or 1
            return [Row(k, v, 100.0 * v / total) for k, v in self.tasks.most_common()]

    @property
    def rate(self) -> float:
        """Échantillons par seconde (moyenne depuis le premier)."""
        with self._lock:
            if self.first is None or self.last is None or self.last <= self.first:
                return 0.0
            return (self.total - 1) / (self.last - self.first)

    def report(self, n: int = 15) -> str:
        rows = self.top_functions(n)
        out = [f"{self.total} échantillons, {self.rate:.0f}/s"]
        for r in rows:
            bar = "█" * int(r.percent / 2.5)
            out.append(f"  {r.percent:5.1f} %  {r.samples:>7}  {r.name:<32} {bar}")
        return "\n".join(out)


class PcSampler:
    """Thread qui échantillonne le PC par le canal live tant que la cible tourne.

    `task_of(read)` (optionnel) donne la tâche RTOS courante ; elle est lue avant et après
    le PC et l'échantillon n'est attribué que si les deux lectures concordent (sinon un
    changement de contexte a eu lieu entre-temps : tâche inconnue, « ? »).
    """

    def __init__(self, live, profile: Profile, is_running: Callable[[], bool],
                 task_of: Callable[[Callable[[int, int], bytes]], str | None] | None = None,
                 on_error: Callable[[str], None] | None = None, max_rate: float | None = None):
        self.live = live
        self.profile = profile
        self.is_running = is_running
        self.task_of = task_of
        self.on_error = on_error or (lambda msg: None)
        self.interval = 1.0 / max_rate if max_rate else 0.0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.errors = 0

    @property
    def active(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if not self.active:
            self._stop.clear()
            self._thread = threading.Thread(target=self._loop, name="pc-sampler", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
        self._thread = None

    def _loop(self) -> None:
        reported = False
        while not self._stop.is_set():
            if not self.is_running():
                self._stop.wait(0.02)
                continue
            try:
                task = None
                if self.task_of is not None:
                    before = self.task_of(self.live.read)
                    pc = self.live.sample_pc()
                    after = self.task_of(self.live.read)
                    task = before if before == after else "?"
                else:
                    pc = self.live.sample_pc()
            except Exception as exc:  # noqa: BLE001
                self.errors += 1
                if not reported:
                    self.on_error(f"échantillonnage du PC : {exc}")
                    reported = True
                self._stop.wait(0.2)
                continue
            if pc is not None:
                self.profile.add(pc, task)
            if self.interval:
                self._stop.wait(self.interval)
