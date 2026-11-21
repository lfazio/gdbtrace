"""Analyse temporelle de la trace d'instructions (Trace.STATistic.Func / Trace.Chart.Func).

À partir des événements de la trace ETM, chacun avec ses cycles (mesurés en mode
cycle-accurate, sinon estimés par le modèle), on reconstruit une pile d'appels fantôme et on
mesure, pour chaque fonction et chaque exception :
- appels complets (entrée et sortie vues dans la trace) et partiels (coupés par le début ou
  la fin de la trace, ou par un changement de contexte RTOS) ;
- temps **brut** (de l'entrée à la sortie, interruptions déduites) ;
- temps **net** (le temps passé dans la fonction elle-même : sans les appelés ni les
  interruptions) ; chaque cycle de la trace est attribué à exactement un temps net ;
- min / max / moyenne du brut sur les appels complets.

Pile d'appels :
- un BL/BLX exécuté empile un cadre avec son adresse de retour (adresse + taille) ; on dépile
  quand l'exécution atteint une adresse de retour de la pile (robuste aux retours inhabituels) ;
- un branchement non-appel vers le début d'une autre fonction est un appel terminal ;
- une exception empile un cadre « [nom] » au-dessus du code interrompu ; son retour le dépile ;
- un retour d'exception qui ne peut pas continuer le code interrompu (changement de tâche
  d'un RTOS) ferme les cadres de l'ancien contexte comme partiels.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .etm.image import DIRECT, INDIRECT
from .etm.qemu_log import Event, ExcEntry, ExcReturn, Step
from .itm import exception_name

FUNCTION, EXCEPTION = "function", "exception"


@dataclass
class FuncStats:
    name: str
    kind: str = FUNCTION
    calls: int = 0               # appels complets
    partial: int = 0             # appels partiels (bornés par la trace ou un changement de tâche)
    gross: int = 0               # brut total (complets + partiels), interruptions déduites
    net: int = 0                 # net total
    gross_complete: int = 0
    min: int | None = None
    max: int = 0
    entry: int | None = None     # adresse d'entrée (navigation vers la source)

    @property
    def avg(self) -> float | None:
        return self.gross_complete / self.calls if self.calls else None


@dataclass
class Interval:
    start: int
    end: int
    depth: int
    name: str
    kind: str
    partial_start: bool
    partial_end: bool
    entry: int | None

    @property
    def duration(self) -> int:
        return self.end - self.start


@dataclass
class TimingResult:
    stats: dict[str, FuncStats]
    intervals: list[Interval]
    total: int                   # cycles couverts par la trace
    context_switches: int = 0

    def rows(self, kind: str | None = None) -> list[FuncStats]:
        out = [s for s in self.stats.values() if kind is None or s.kind == kind]
        return sorted(out, key=lambda s: (-s.gross, s.name))


@dataclass
class _Frame:
    name: str
    kind: str
    start: int
    depth: int
    ret: int | None = None       # adresse de retour (cadres de fonction appelés par BL/BLX)
    entry: int | None = None
    func_start: int | None = None
    partial_start: bool = False
    self_c: int = 0
    irq: int = 0
    handler: int | None = None   # cadres d'exception : adresse du handler
    saved_prev: object = None    # cadres d'exception : dernière instruction du code interrompu


@dataclass
class _Prev:
    addr: int
    ins: object


class TimingAnalyzer:
    def __init__(self, image, symbols, max_intervals: int = 200_000):
        self.image = image
        self.symbols = symbols
        self.max_intervals = max_intervals

    # ------------------------------------------------------------------ API
    def analyze(self, events: list[Event]) -> TimingResult:
        self.stack: list[_Frame] = []
        self.stats: dict[str, FuncStats] = {}
        self.intervals: list[Interval] = []
        self.t = 0
        self.prev: _Prev | None = None
        self.switches = 0
        for e in events:
            if isinstance(e, Step):
                self._step(e)
            elif isinstance(e, ExcEntry):
                self._exc_entry(e)
            elif isinstance(e, ExcReturn):
                self._exc_return(e)
        while self.stack:                       # fin de trace : cadres encore ouverts
            self._close(len(self.stack) - 1, complete_end=False)
        if len(self.intervals) > self.max_intervals:
            self.intervals = self.intervals[-self.max_intervals:]
        return TimingResult(self.stats, self.intervals, self.t, self.switches)

    # ------------------------------------------------------------------ outils
    def _func(self, addr: int):
        return self.symbols.function_at(addr) if self.symbols else None

    def _name(self, addr: int) -> tuple[str, int | None]:
        f = self._func(addr)
        return (f.name, f.start) if f else (f"{addr:#x}", None)

    def _ctx_base(self) -> int:
        """Indice du premier cadre du contexte courant (au-dessus de la dernière exception)."""
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i].kind == EXCEPTION:
                return i + 1
        return 0

    def _push(self, addr: int, ret: int | None, partial: bool) -> None:
        name, fstart = self._name(addr)
        self.stack.append(_Frame(name, FUNCTION, self.t, len(self.stack), ret=ret, entry=addr,
                                 func_start=fstart, partial_start=partial))

    def _close(self, i: int, complete_end: bool = True) -> None:
        """Ferme les cadres i..sommet (le sommet d'abord) à l'instant courant."""
        while len(self.stack) > i:
            fr = self.stack.pop()
            dur = self.t - fr.start
            gross = dur - fr.irq
            if fr.kind == EXCEPTION:            # l'interruption est déduite des cadres interrompus
                for below in self.stack:
                    below.irq += dur
            st = self.stats.get(fr.name)
            if st is None:
                st = self.stats[fr.name] = FuncStats(fr.name, fr.kind, entry=fr.entry)
            complete = complete_end and not fr.partial_start and len(self.stack) >= i
            st.gross += gross
            st.net += fr.self_c
            if complete:
                st.calls += 1
                st.gross_complete += gross
                st.min = gross if st.min is None else min(st.min, gross)
                st.max = max(st.max, gross)
            else:
                st.partial += 1
            self.intervals.append(Interval(fr.start, self.t, fr.depth, fr.name, fr.kind,
                                           fr.partial_start, not complete_end, fr.entry))

    @staticmethod
    def _plausible(prev: _Prev, addr: int) -> bool:
        """`addr` peut-elle suivre `prev` dans le même contexte d'exécution ?"""
        ins = prev.ins
        return (addr == prev.addr + ins.size or (ins.kind == DIRECT and addr == ins.target)
                or ins.kind == INDIRECT)

    # ------------------------------------------------------------------ événements
    def _step(self, e: Step) -> None:
        addr = e.addr
        ins = self.image.instr(addr)
        base = self._ctx_base()
        prev = self.prev
        # 1. retour : l'exécution atteint l'adresse de retour d'un cadre du contexte
        returned = False
        for i in range(len(self.stack) - 1, base - 1, -1):
            if self.stack[i].ret == addr:
                self._close(i)
                returned = True
                break
        # 2. reprise après exception qui ne continue pas le code interrompu : changement de tâche
        if not returned and prev is not None and not self._plausible(prev, addr) \
                and len(self.stack) > base:
            self._close(base, complete_end=False)
            self.switches += 1
            prev = None
        ctx = self.stack[base:]
        name, fstart = self._name(addr)
        if not returned and prev is not None and prev.ins.call and addr != prev.addr + prev.ins.size:
            self._push(addr, prev.addr + prev.ins.size, partial=False)          # appel
        elif not ctx:
            exc = self.stack[base - 1] if base else None
            # entrée du handler juste après l'exception : appel complet
            self._push(addr, None, partial=not (exc is not None and exc.handler == addr))
        elif ctx[-1].func_start != fstart:
            top = ctx[-1]
            taken_jump = (prev is not None and prev.ins.kind in (DIRECT, INDIRECT)
                          and not prev.ins.ret and addr != prev.addr + prev.ins.size)
            if taken_jump and fstart == addr:                                    # appel terminal
                ret = top.ret
                self._close(len(self.stack) - 1)
                self._push(addr, ret, partial=False)
            elif (prev is not None and prev.ins.ret and not returned
                  and addr != prev.addr + prev.ins.size):
                # (un retour conditionnel non exécuté, ex. `it eq; bx lr`, continue en séquence)
                # retour vers un appelant absent de la pile (cadre racine commencé avant la
                # trace) : le cadre se termine, l'appelant devient la nouvelle racine
                self._close(len(self.stack) - 1)
                if len(self.stack) <= base or self.stack[-1].func_start != fstart:
                    self._push(addr, None, partial=True)
            # sinon : saut ou débordement au milieu d'une autre fonction (code partagé, comme
            # dans la bibliothèque soft-float) : on reste dans le cadre courant, sinon son
            # adresse de retour serait perdue
        self.stack[-1].self_c += e.cycles
        self.t += e.cycles
        self.prev = _Prev(addr, ins)

    def _exc_entry(self, e: ExcEntry) -> None:
        if e.tailchain:                         # fin de l'exception précédente sans dépilement
            k = self._last_exception()
            if k is not None:
                saved = self.stack[k].saved_prev
                self._close(k)
                self.prev = saved
        label = f"[{exception_name(e.number)}]"
        self.stack.append(_Frame(label, EXCEPTION, self.t, len(self.stack), entry=e.handler,
                                 handler=e.handler, saved_prev=self.prev))
        self.prev = None
        self.stack[-1].self_c += e.cycles       # latence d'entrée
        self.t += e.cycles

    def _exc_return(self, e: ExcReturn) -> None:
        k = self._last_exception()
        if k is None:                           # retour d'une exception commencée avant la trace
            if self.stack:
                self.stack[-1].self_c += e.cycles
            self.t += e.cycles
            self.prev = None
            return
        # le handler (et ce qu'il appelle) se termine avec son instruction de retour ;
        # la latence de sortie n'appartient qu'au cadre de l'exception
        self._close(k + 1)
        self.stack[k].self_c += e.cycles
        self.t += e.cycles
        saved = self.stack[k].saved_prev
        self._close(k)
        self.prev = saved

    def _last_exception(self) -> int | None:
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i].kind == EXCEPTION:
                return i
        return None


def to_us(cycles: float, cpu_hz: int) -> float:
    return cycles * 1e6 / cpu_hz
