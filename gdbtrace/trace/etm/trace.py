"""Trace d'instructions décodée en continu : historique, couverture, flot d'appels.

- historique : les N dernières instructions et exceptions (Trace.List : « ce qui s'est passé
  avant l'arrêt ») ;
- couverture : base de couverture (`trace/coverage.py`) alimentée en continu : instructions,
  branches prises / non prises, ramenées aux lignes et aux fonctions ;
- flot d'appels : appels, retours, exceptions, reconstruits depuis l'historique.
"""
from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass

from .etm3 import Etm3Decoder
from .flow import FlowDecoder
from .image import CodeImage
from .qemu_log import Event, ExcEntry, ExcReturn, Step


@dataclass
class FuncCoverage:
    name: str
    executed: int
    total: int

    @property
    def percent(self) -> float:
        return 100.0 * self.executed / self.total if self.total else 0.0


@dataclass
class FlowRow:
    index: int          # position (négative : depuis la fin de l'historique)
    depth: int
    kind: str           # call, return, exception, exc-return, start
    func: str
    addr: int


class EtmTrace:
    def __init__(self, image: CodeImage, symbols=None, history: int = 200_000,
                 cycle_accurate: bool = False):
        self.image = image
        self.symbols = symbols
        self.cycle_accurate = cycle_accurate
        #: trace produite par l'ETM simulé : ses cycles viennent du modèle, pas du matériel
        self.simulated = False
        self.packets = Etm3Decoder(cycle_accurate=cycle_accurate)
        self.flow = FlowDecoder(image, cycle_accurate)
        self.history: deque[Event] = deque(maxlen=history)
        from ..coverage import CoverageDB
        self.cov = CoverageDB(image, symbols)
        self.bytes = 0
        self.generation = 0          # incrémenté à chaque ajout (rafraîchissement des vues)
        self._lock = threading.Lock()

    def feed(self, data: bytes) -> int:
        """Ajoute des octets de trace ETM ; retourne le nombre d'instructions décodées."""
        with self._lock:
            self.bytes += len(data)
            events = self.flow.feed(self.packets.feed(data))
            self.history.extend(events)
            self.cov.add(events)
            self.generation += 1
            return sum(1 for e in events if isinstance(e, Step))

    def reset(self) -> None:
        with self._lock:
            self.packets = Etm3Decoder(cycle_accurate=self.cycle_accurate)
            self.flow = FlowDecoder(self.image, self.cycle_accurate)
            self.history.clear()
            self.bytes = 0
            self.generation += 1

    def last(self, n: int) -> list[Event]:
        with self._lock:
            if n >= len(self.history):
                return list(self.history)
            return list(self.history)[-n:]

    @property
    def stats(self) -> dict[str, int]:
        fs = self.flow.stats
        return {"octets": self.bytes, "instructions": fs.instructions, "non exécutées": fs.not_executed,
                "erreurs": self.packets.errors, "atomes perdus": fs.lost_atoms,
                "code inconnu": fs.unknown_code, "cycles": fs.cycles}

    @property
    def timing(self) -> str:
        """Origine des durées : mesurées (cycle-accurate) ou estimées (modèle)."""
        if self.simulated:
            return "estimé (modèle de cycles, ETM simulé)"
        return self.flow.timing

    # ------------------------------------------------------------------ couverture
    @property
    def executed(self) -> set[int]:
        return self.cov.executed()

    def coverage(self) -> list[FuncCoverage]:
        """Couverture par fonction (instructions couvertes / total), fonctions de l'ELF."""
        if self.symbols is None:
            return []
        out = [FuncCoverage(f.name, f.covered, f.instrs) for f in self.cov.functions()]
        return sorted(out, key=lambda c: (-c.percent, c.name))

    def line_coverage(self, path: str) -> dict[int, bool]:
        """{ligne: au moins une instruction couverte ?} pour les lignes de code de `path`."""
        if self.symbols is None:
            return {}
        return {ln: c.covered > 0 for ln, c in self.cov.lines(path).items()}

    # ------------------------------------------------------------------ flot d'appels
    def call_flow(self, n: int = 20_000) -> list[FlowRow]:
        """Transitions de fonction des `n` derniers événements (appels, retours, exceptions)."""
        events = self.last(n)
        rows: list[FlowRow] = []
        depth = 0
        cur_func = None
        pending_call = pending_ret = False
        base = -len(events)
        for i, e in enumerate(events):
            if isinstance(e, ExcEntry):
                depth += 1
                rows.append(FlowRow(base + i, depth, "exception", self._name(e.handler), e.handler))
                cur_func, pending_call, pending_ret = self._name(e.handler), False, False
                continue
            if isinstance(e, ExcReturn):
                depth = max(0, depth - 1)
                pending_call, pending_ret = False, False
                cur_func = None
                rows.append(FlowRow(base + i, depth, "exc-return", "", 0))
                continue
            name = self._name(e.addr)
            if name != cur_func:
                if pending_call:
                    depth += 1
                    kind = "call"
                elif pending_ret:
                    depth = max(0, depth - 1)
                    kind = "return"
                else:
                    kind = "start" if cur_func is None else "branch"
                rows.append(FlowRow(base + i, depth, kind, name, e.addr))
                cur_func = name
            ins = self.image.instr(e.addr)
            pending_call = bool(ins and ins.call)
            pending_ret = bool(ins and ins.ret)
        return rows

    def _name(self, addr: int) -> str:
        if self.symbols is None:
            return f"{addr:#x}"
        f = self.symbols.function_at(addr)
        return f.name if f else f"{addr:#x}"
