"""Temps par tâche RTOS mesuré par l'ITM (vue Temps des tâches).

Le firmware écrit l'adresse du TCB qui prend le processeur sur un port ITM à chaque
changement de tâche (FreeRTOS : `traceTASK_SWITCHED_IN()`, voir examples/freertos). Avec
l'horodatage ITM (cycles du cœur), chaque changement ferme la tranche de la tâche sortante :
on obtient, par tâche, le nombre d'activations, le temps brut, le temps net (sans les
exceptions survenues pendant ses tranches, d'après la trace des exceptions du DWT), la plus
longue tranche, et la chronologie des tranches (diagramme de Gantt).

Mesure exacte, sans échantillonnage, et qui sait *quelle* tâche tourne (ce que la trace
d'instructions seule ne dit pas). Les noms sont relus dans les TCB (disposition FreeRTOS)
au moment de l'affichage, jamais depuis le fil de la capture.

Sans ITM (Cortex-M0/M0+) ou sans capture SWO, le même crochet écrit aussi chaque changement
dans un anneau en RAM, `gdbtrace_task_log` (horodatage en cycles d'après SysTick et le compte
des ticks), que `TaskLogReader` lit par le canal mémoire, cible en marche, et à l'arrêt. Pas
de trace des exceptions par ce chemin : le temps net y est le temps brut.
"""
from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass, field
from typing import Callable

from .distribution import Reservoir, distribution
from .itm import ExceptionTrace, Instrumentation, LocalTimestamp, Packet

TASK_PORT = 31
TASK_LOG_SYMBOL = "gdbtrace_task_log"
TASK_LOG_MAGIC = 0x47544C31          # « GTL1 »
_HEADER = 12                         # magic, size, head


@dataclass
class TaskSlice:
    tcb: int
    start: int          # cycles (horodatage ITM)
    end: int
    isr: int = 0        # cycles passés en exception pendant la tranche


@dataclass
class TaskTime:
    name: str
    tcb: int
    activations: int = 0
    gross: int = 0      # cycles, de l'entrée à la sortie de chaque tranche
    isr: int = 0        # cycles d'exceptions survenues pendant ses tranches
    max_slice: int = 0  # plus longue tranche (brute)

    @property
    def net(self) -> int:
        return self.gross - self.isr

    @property
    def avg_slice(self) -> float:
        return self.gross / self.activations if self.activations else 0.0


@dataclass
class _Acc:
    activations: int = 0
    gross: int = 0
    isr: int = 0
    max_slice: int = 0
    slices: list = field(default_factory=list)
    #: durée brute de chaque tranche terminée (Trace.STATistic.DISTribution), et son rang
    durations: Reservoir = field(default_factory=Reservoir)


class TaskTraceSink:
    """Abonné ITM : changements de tâche (port `port`, valeur 32 bits = adresse du TCB) et
    trace des exceptions → tranches et temps par tâche."""

    def __init__(self, port: int = TASK_PORT, max_slices: int = 50_000):
        self.port = port
        self.namer: Callable[[int], str | None] | None = None
        self.names: dict[int, str] = {}
        self.slices: deque[TaskSlice] = deque(maxlen=max_slices)
        self.switches = 0
        self.timestamps = False             # le flux porte des horodatages ITM
        self.generation = 0
        self._lock = threading.Lock()
        self._acc: dict[int, _Acc] = {}
        self._cur: TaskSlice | None = None
        self._exc: list[tuple[int, int]] = []    # exceptions en cours : (numéro, ts d'entrée)
        self._exc_total = 0
        self._first: int | None = None
        self._last = 0

    def reset(self) -> None:
        with self._lock:
            self.slices.clear()
            self._acc.clear()
            self._cur, self._exc, self._exc_total = None, [], 0
            self._first, self._last = None, 0
            self.switches = 0
            self.generation += 1

    def __call__(self, pkts: list[Packet]) -> None:
        with self._lock:
            for p in pkts:
                if isinstance(p, LocalTimestamp):
                    self.timestamps = True
                elif isinstance(p, Instrumentation) and p.port == self.port and p.size == 4:
                    self._switch(p.value, p.ts)
                elif isinstance(p, ExceptionTrace) and p.number:
                    self._exception(p)

    def feed(self, entries: list[tuple[int, int]]) -> None:
        """Changements de tâche venus du journal en RAM : (horodatage en cycles, TCB)."""
        if not entries:
            return
        with self._lock:
            self.timestamps = True
            for ts, tcb in entries:
                self._switch(tcb, ts)

    def _switch(self, tcb: int, ts: int) -> None:
        if self._first is None:
            self._first = ts
        self._last = max(self._last, ts)
        cur = self._cur
        if cur is not None:
            cur.end = ts
            a = self._acc.setdefault(cur.tcb, _Acc())
            dur = max(0, ts - cur.start)
            a.gross += dur
            a.isr += min(cur.isr, dur)
            a.max_slice = max(a.max_slice, dur)
            a.durations.add(dur, self.switches)
            self.slices.append(cur)
        self._acc.setdefault(tcb, _Acc()).activations += 1
        self._cur = TaskSlice(tcb, ts, ts)
        self.switches += 1
        self.generation += 1

    def _exception(self, p: ExceptionTrace) -> None:
        if p.event == "entry":
            self._exc.append((p.number, p.ts))
        elif p.event == "exit":
            while self._exc:                    # dépile jusqu'à elle (paquets perdus tolérés)
                num, ts0 = self._exc.pop()
                if num == p.number:
                    if not self._exc:           # exception de premier niveau : temps volé
                        dt = max(0, p.ts - ts0)
                        self._exc_total += dt
                        if self._cur is not None:
                            self._cur.isr += dt
                    break
        self._last = max(self._last, p.ts)

    # ------------------------------------------------------------------ résultats
    def name(self, tcb: int) -> str:
        """Nom de la tâche (relu dans son TCB une seule fois), sinon l'adresse du TCB."""
        if tcb not in self.names and self.namer is not None:
            try:
                n = self.namer(tcb)
            except Exception:  # noqa: BLE001 - lecture impossible maintenant : réessayée
                n = None
            if n:
                self.names[tcb] = n
        return self.names.get(tcb, f"TCB {tcb:#010x}")

    @property
    def span(self) -> int:
        """Cycles couverts par la mesure (du premier changement au dernier paquet)."""
        return max(0, self._last - self._first) if self._first is not None else 0

    def rows(self) -> list[TaskTime]:
        """Temps par tâche, la plus gourmande d'abord (tranche en cours comprise)."""
        with self._lock:
            acc = {k: _Acc(a.activations, a.gross, a.isr, a.max_slice) for k, a in self._acc.items()}
            cur = self._cur
            if cur is not None and self._last > cur.start:
                a = acc.setdefault(cur.tcb, _Acc())
                dur = self._last - cur.start
                a.gross += dur
                a.isr += min(cur.isr, dur)
                a.max_slice = max(a.max_slice, dur)
        out = [TaskTime(self.name(tcb), tcb, a.activations, a.gross, a.isr, a.max_slice)
               for tcb, a in acc.items()]
        return sorted(out, key=lambda t: (-t.gross, t.name))

    def distribution(self, task, bins: int = 20, log: bool | None = None, scale: float = 1.0,
                     unit: str = "cycles"):
        """Distribution des durées des tranches terminées d'une tâche (nom ou TCB) ; la
        position d'une tranche est son rang parmi les changements de tâche."""
        with self._lock:
            tcbs = [t for t in self._acc if task == t or self.names.get(t) == task]
            if not tcbs:
                tcbs = [t for t in self._acc if self.name(t) == task]
            if not tcbs:
                raise KeyError(f"tâche inconnue : {task}")
            res = self._acc[tcbs[0]].durations
        return distribution(self.name(tcbs[0]), res, bins, log, scale, unit)

    def task_names(self) -> list[str]:
        with self._lock:
            tcbs = list(self._acc)
        return [self.name(t) for t in tcbs]

    @property
    def exception_cycles(self) -> int:
        return self._exc_total

    def timeline(self, n: int | None = None) -> list[TaskSlice]:
        with self._lock:
            out = list(self.slices)
            if self._cur is not None and self._last > self._cur.start:
                out.append(TaskSlice(self._cur.tcb, self._cur.start, self._last, self._cur.isr))
        return out[-n:] if n else out


class TaskLogReader:
    """Lecteur de l'anneau `gdbtrace_task_log` du firmware : en-tête (marqueur, taille, nombre
    d'entrées écrites), puis entrées {horodatage 32 bits, TCB}. `poll(read)` rend les seules
    entrées nouvelles, horodatages déroulés (un tour de 2³² cycles entre deux lectures au
    plus). Un anneau dépassé entre deux lectures perd ses plus vieilles entrées (`lost`) ; un
    nombre d'entrées qui recule signale une cible redémarrée (`restarted`)."""

    def __init__(self, addr: int):
        self.addr = addr
        self.head = 0                # entrées déjà lues
        self.lost = 0
        self.read_entries = 0
        self.restarted = False
        self._last: int | None = None
        self._epoch = 0

    def poll(self, read: Callable[[int, int], bytes]) -> list[tuple[int, int]]:
        self.restarted = False
        hdr = read(self.addr, _HEADER)
        magic, size, head = (int.from_bytes(hdr[i:i + 4], "little") for i in (0, 4, 8))
        if magic != TASK_LOG_MAGIC or not size:
            return []                # ordonnanceur pas encore démarré
        if head < self.head:         # cible redémarrée : la mesure repart
            self.head, self._last, self._epoch = 0, None, 0
            self.restarted = True
        start = max(self.head, head - size)
        self.lost += start - self.head
        if head == start:
            self.head = head
            return []
        raw = self._entries(read, size, start, head)
        again = int.from_bytes(read(self.addr + 8, 4), "little")
        valid = max(start, again - size)          # écrasées pendant la lecture : perdues
        self.lost += valid - start
        raw = raw[8 * (valid - start):]           # en octets : 8 par entrée
        out = []
        for i in range(0, len(raw), 8):
            ts = int.from_bytes(raw[i:i + 4], "little")
            tcb = int.from_bytes(raw[i + 4:i + 8], "little")
            if self._last is not None and ts < self._last:
                self._epoch += 1                  # l'horodatage 32 bits a fait un tour
            self._last = ts
            out.append((ts + (self._epoch << 32), tcb))
        self.head = head
        self.read_entries += len(out)
        return out

    def _entries(self, read, size: int, start: int, head: int) -> bytes:
        """Entrées [start, head) de l'anneau, en une ou deux lectures."""
        base = self.addr + _HEADER
        a, b = start % size, head % size
        n = head - start
        if a + n <= size:
            return read(base + 8 * a, 8 * n)
        return read(base + 8 * a, 8 * (size - a)) + read(base, 8 * b)


def freertos_namer(layout, read: Callable[[int, int], bytes]) -> Callable[[int], str | None]:
    """Nom d'une tâche FreeRTOS depuis l'adresse de son TCB (`read` : gdb ou canal live)."""
    def namer(tcb: int) -> str | None:
        raw = read(tcb + layout.tcb_name, layout.name_len)
        name = raw.split(b"\0", 1)[0].decode("latin-1")
        return name or None
    return namer
