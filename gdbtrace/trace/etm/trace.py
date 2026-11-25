"""Trace d'instructions décodée en continu : historique, couverture, flot d'appels.

- historique : les N dernières instructions et exceptions (Trace.List : « ce qui s'est passé
  avant l'arrêt ») ;
- couverture : base de couverture (`trace/coverage.py`) alimentée en continu : instructions,
  branches prises / non prises, ramenées aux lignes et aux fonctions ;
- flot d'appels : appels, retours, exceptions, reconstruits depuis l'historique.

`version` : architecture de l'ETM de la cible, 3 (Cortex-M3/M4, ETMv3.5) ou 4 (Cortex-M7,
ETMv4.0). En ETMv4, le dernier élément P0 reste spéculatif tant que le suivant n'est pas
arrivé : `flush()` le valide (à l'arrêt de la cible, sinon la dernière instruction tracée
manquerait).
"""
from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass

from .etm3 import Etm3Decoder
from .flow import FlowDecoder
from .image import CodeImage
from .qemu_log import Event, ExcEntry, ExcReturn, Step, TraceGap


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
    kind: str           # call, return, exception, exc-return, start, gap (trace interrompue)
    func: str
    addr: int


class EtmTrace:
    def __init__(self, image: CodeImage, symbols=None, history: int = 200_000,
                 cycle_accurate: bool = False, version: int = 3, max_spec: int = 1):
        self.image = image
        self.symbols = symbols
        self.version = 4 if version == 4 else 3
        #: ETMv4 : profondeur de spéculation de l'ETM (TRCIDR8.MAXSPEC ; M7 : 1, M55 : 0)
        self.max_spec = max_spec
        self.cycle_accurate = cycle_accurate and self.version == 3   # ETMv4 : pas encore
        #: trace produite par l'ETM simulé : ses cycles viennent du modèle, pas du matériel
        self.simulated = False
        self.packets, self.flow = self._decoders()
        self.history: deque[Event] = deque(maxlen=history)
        from ..coverage import CoverageDB
        self.cov = CoverageDB(image, symbols)
        self.bytes = 0
        self.generation = 0          # incrémenté à chaque ajout (rafraîchissement des vues)
        #: origine des durées quand la trace ne vient pas de l'ETM (ex. MTB : cycles du modèle)
        self.timing_label: str | None = None
        self.fed_instructions = 0    # instructions venues d'une autre source (MTB)
        self.fed_errors = 0
        self.gaps = 0                # interruptions de la trace (filtre, débordement)
        #: cible relancée depuis le dernier arrêt : la resynchronisation qui ouvre la nouvelle
        #: exécution (I-sync, Trace On) n'est pas un trou, rien n'ayant tourné entre les deux
        self._resumed = False
        self._lock = threading.Lock()

    def _decoders(self):
        if self.version == 4:
            from .etm4 import Etm4Decoder
            from .flow4 import Etm4Flow
            return Etm4Decoder(max_spec=self.max_spec), Etm4Flow(self.image, max_spec=self.max_spec)
        return Etm3Decoder(cycle_accurate=self.cycle_accurate), FlowDecoder(self.image, self.cycle_accurate)

    def set_max_spec(self, max_spec: int) -> None:
        """ETMv4 : profondeur de spéculation de la cible ; décodeurs refaits si elle change
        (avant toute trace : ce qui serait en cours de décodage est abandonné)."""
        with self._lock:
            if max_spec != self.max_spec:
                self.max_spec = max_spec
                self.packets, self.flow = self._decoders()

    def resumed(self) -> None:
        """La cible repart après un arrêt (appelé avant chaque reprise)."""
        self._resumed = True

    def _clean(self, events: list[Event], gaps: int) -> list[Event]:
        """Retire le « trou » qui ne fait que marquer la reprise après un arrêt (ceux d'avant
        la première instruction qui la suit) et compte les autres. `gaps` : trous dans
        `events`, compte du décodeur. Les événements ne sont parcourus que jusqu'à la
        première instruction après une reprise : une trace qui déborde (ETM par SWO) a des
        trous dans presque chaque paquet, et des millions d'instructions entre eux."""
        if not self._resumed:
            self.gaps += gaps
            return events
        first = next((i for i, e in enumerate(events) if isinstance(e, Step)), len(events))
        if first < len(events):
            self._resumed = False
        if not gaps:
            return events
        head = [e for e in events[:first] if not isinstance(e, TraceGap)]
        self.gaps += gaps - (first - len(head))
        return head + events[first:]

    def discontinuity(self) -> None:
        """Des octets de trace ont été perdus avant le décodage : le décodeur de paquets
        attend la prochaine synchronisation, le flot une nouvelle adresse ; un trou."""
        with self._lock:
            self.packets.synced = False
            reset = getattr(self.flow, "_reset", None)
            if reset is not None:
                reset()
            else:
                self.flow.addr = None
            self.history.append(TraceGap())
            self.gaps += 1
            self.generation += 1

    def feed_events(self, events: list[Event], errors: int = 0) -> int:
        """Ajoute des événements déjà décodés (trace MTB) ; retourne le nombre d'instructions."""
        with self._lock:
            self.history.extend(events)
            self.cov.add(events)
            n = sum(1 for e in events if isinstance(e, Step))
            self.fed_instructions += n
            self.fed_errors += errors
            self.generation += 1
            return n

    def feed(self, data: bytes) -> int:
        """Ajoute des octets de trace ETM ; retourne le nombre d'instructions décodées."""
        with self._lock:
            self.bytes += len(data)
            return self._add(lambda: self.flow.feed(self.packets.feed(data)))

    def _add(self, decode) -> int:
        """Événements de `decode()` dans l'historique et la couverture. Par paquet de quelques
        Ko de trace, des millions d'instructions : rien ici ne les parcourt une à une en
        Python (le décodeur compte instructions, trous et, en ETMv4, la couverture)."""
        fs = self.flow.stats
        n0, g0 = fs.instructions, fs.gaps
        events = self._clean(decode(), fs.gaps - g0)
        if not events:
            return 0
        self.history.extend(events)
        if hasattr(self.flow, "take_counts"):
            self.cov.add_counts(*self.flow.take_counts())
        else:
            self.cov.add(events)
        self.generation += 1
        return fs.instructions - n0

    def flush(self) -> int:
        """ETMv4 : valide les éléments encore spéculatifs ; retourne le nombre d'instructions.
        Un Commit reçu ensuite pour l'un d'eux est sans effet ; un Cancel ne pourrait plus
        l'annuler (sans conséquence sur un ETM-M7, qui ne spécule pas les branchements)."""
        if self.version != 4:
            return 0
        with self._lock:
            return self._add(self.flow.flush)

    def reset(self) -> None:
        with self._lock:
            self.packets, self.flow = self._decoders()
            self.history.clear()
            self.bytes = 0
            self.fed_instructions = self.fed_errors = self.gaps = 0
            self.generation += 1

    def last(self, n: int) -> list[Event]:
        with self._lock:
            if n >= len(self.history):
                return list(self.history)
            return list(self.history)[-n:]

    @property
    def stats(self) -> dict[str, int]:
        fs = self.flow.stats
        return {"octets": self.bytes, "instructions": fs.instructions + self.fed_instructions,
                "non exécutées": fs.not_executed,
                "erreurs": self.packets.errors + getattr(fs, "errors", 0) + self.fed_errors,
                "atomes perdus": fs.lost_atoms, "code inconnu": fs.unknown_code, "cycles": fs.cycles,
                "débordements": getattr(fs, "overflows", 0), "trous": self.gaps}

    @property
    def timing(self) -> str:
        """Origine des durées : mesurées (cycle-accurate) ou estimées (modèle)."""
        if self.timing_label:
            return self.timing_label
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
            if isinstance(e, TraceGap):              # trace interrompue : la pile est perdue
                rows.append(FlowRow(base + i, 0, "gap", "", 0))
                depth, cur_func, pending_call, pending_ret = 0, None, False, False
                continue
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
