"""Trace d'instructions décodée en continu : historique, couverture, flot d'appels.

- historique : les N dernières instructions et exceptions (Trace.List : « ce qui s'est passé
  avant l'arrêt ») ;
- couverture : ensemble des instructions exécutées au moins une fois, ramené aux fonctions
  et aux lignes source ;
- flot d'appels : appels, retours, exceptions, reconstruits depuis l'historique.
"""
from __future__ import annotations

import bisect
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
    def __init__(self, image: CodeImage, symbols=None, history: int = 200_000):
        self.image = image
        self.symbols = symbols
        self.packets = Etm3Decoder()
        self.flow = FlowDecoder(image)
        self.history: deque[Event] = deque(maxlen=history)
        self.executed: set[int] = set()
        self.bytes = 0
        self.generation = 0          # incrémenté à chaque ajout (rafraîchissement des vues)
        self._lock = threading.Lock()
        self._func_instrs: dict[str, list[int]] = {}

    def feed(self, data: bytes) -> int:
        """Ajoute des octets de trace ETM ; retourne le nombre d'instructions décodées."""
        with self._lock:
            self.bytes += len(data)
            events = self.flow.feed(self.packets.feed(data))
            self.history.extend(events)
            n = 0
            for e in events:
                if isinstance(e, Step):
                    self.executed.add(e.addr)
                    n += 1
            self.generation += 1
            return n

    def reset(self) -> None:
        with self._lock:
            self.packets = Etm3Decoder()
            self.flow = FlowDecoder(self.image)
            self.history.clear()
            self.executed.clear()
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
                "code inconnu": fs.unknown_code}

    # ------------------------------------------------------------------ couverture
    def _instructions_of(self, func) -> list[int]:
        cached = self._func_instrs.get(func.name)
        if cached is None:
            cached, addr = [], func.start
            while addr < func.end:
                if self.symbols.is_data(addr):      # literal pool : fin du code utile
                    addr += 2
                    continue
                ins = self.image.instr(addr)
                if ins is None:
                    break
                cached.append(addr)
                addr += ins.size
            self._func_instrs[func.name] = cached
        return cached

    def coverage(self) -> list[FuncCoverage]:
        """Couverture par fonction (instructions exécutées / total), fonctions de l'ELF."""
        if self.symbols is None:
            return []
        with self._lock:
            executed = set(self.executed)
        by_start: dict[int, list] = {}
        for f in self.symbols.functions:          # alias (même adresse) : une seule ligne
            if f.size:
                by_start.setdefault(f.start, []).append(f)
        out = []
        for funcs in by_start.values():
            f = max(funcs, key=lambda x: x.size)
            instrs = self._instructions_of(f)
            if instrs:
                name = " = ".join(sorted({x.name for x in funcs}, key=lambda n: (n.startswith("_"), n)))
                out.append(FuncCoverage(name, sum(a in executed for a in instrs), len(instrs)))
        return sorted(out, key=lambda c: (-c.percent, c.name))

    def line_coverage(self, path: str) -> dict[int, bool]:
        """{ligne: exécutée ?} pour les lignes de `path` qui portent du code."""
        if self.symbols is None:
            return {}
        with self._lock:
            executed = set(self.executed)
        lines: dict[int, bool] = {}
        for e in self.symbols.lines:
            if e.file != path:
                continue
            f = self.symbols.function_at(e.address)
            if f is None:
                continue
            # toutes les instructions de la plage d'adresses de cette entrée de ligne
            hit = any(a in executed for a in self._range_of(e.address))
            lines[e.line] = lines.get(e.line, False) or hit
        return lines

    def _range_of(self, addr: int) -> list[int]:
        """Instructions de la plage [addr, entrée de ligne suivante)."""
        sym = self.symbols
        idx = bisect.bisect_right(sym._line_addrs, addr)
        end = sym.lines[idx].address if idx < len(sym.lines) else addr + 2
        out = []
        while addr < end:
            ins = self.image.instr(addr)
            if ins is None:
                break
            out.append(addr)
            addr += ins.size
        return out

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
