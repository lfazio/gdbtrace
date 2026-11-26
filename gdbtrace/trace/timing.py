"""Analyse temporelle de la trace d'instructions (Trace.STATistic.Func / Trace.Chart.Func).

À partir des événements de la trace ETM, chacun avec ses cycles (mesurés en mode
cycle-accurate, sinon estimés par le modèle), on reconstruit une pile d'appels fantôme et on
mesure, pour chaque fonction et chaque exception :
- appels complets (entrée et sortie vues dans la trace) et partiels (coupés par le début ou
  la fin de la trace) ;
- temps **brut** (de l'entrée à la sortie, interruptions déduites) ;
- temps **net** (le temps passé dans la fonction elle-même : sans les appelés ni les
  interruptions) ; chaque cycle de la trace est attribué à exactement un temps net ;
- min / max / moyenne du brut sur les appels complets.

Pile d'appels :
- un BL/BLX exécuté empile un cadre avec son adresse de retour (adresse + taille) ; on dépile
  quand l'exécution atteint une adresse de retour de la pile (robuste aux retours inhabituels) ;
- un branchement non-appel vers le début d'une autre fonction est un appel terminal ;
- une exception empile un cadre « [nom] » au-dessus du code interrompu ; son retour le dépile ;
- un retour d'exception qui ne peut pas continuer le code interrompu est un changement de
  tâche d'un RTOS : la pile de la tâche sortante est **mise de côté** ; elle est restaurée
  quand l'exécution reprend à une adresse qui suit sa dernière instruction. Si plusieurs
  tâches attendent au même endroit (ex. toutes dans `vTaskDelay`), la décision est reportée
  au premier retour : l'adresse de retour désigne la bonne pile. Le temps
  passé dans les autres tâches est déduit du brut, comme une interruption : une fonction
  préemptée reste un seul appel, dessiné en plusieurs morceaux dans le chronogramme.

Arbre d'appels (Trace.STATistic.TREE) : chaque cadre porte son chemin d'appel, fixé à
l'empilement (le chemin du cadre du dessous, plus son nom) ; une exception est la racine de
son propre arbre. Les mêmes statistiques sont tenues par chemin (`TimingResult.tree`) : la
somme des nets de l'arbre égale celle des fonctions. Profondeur et nombre de nœuds sont
plafonnés (`MAX_DEPTH`, `max_nodes`) : au-delà, un nœud « … » réunit le reste.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace

from .etm.image import DIRECT, INDIRECT
from .etm.qemu_log import MARKS, CycleMark, Event, ExcEntry, ExcReturn, Step, TraceGap
from .itm import exception_name

FUNCTION, EXCEPTION = "function", "exception"
MAX_DEPTH = 32                  # profondeur de l'arbre d'appels ; au-delà, un nœud « … »
ELLIPSIS = "…"


def measured_cycles(events: list) -> list[Event]:
    """ETMv4 cycle-accurate : les événements entre deux `CycleMark` portent des cycles
    estimés par le modèle ; ils sont ramenés à la mesure de la marque qui les clôt (au
    prorata de l'estimation, arrondis sans perte). Le premier segment reste estimé : son
    début est hors de la fenêtre, la mesure le déborde. Un segment qui contient un trou de
    trace met l'écart sur le trou (cycles non tracés), pas sur les instructions. Les marques
    sont retirées ; sans `CycleMark`, les événements ne changent pas."""
    if not any(isinstance(e, CycleMark) for e in events):
        return [e for e in events if not isinstance(e, MARKS)]
    out: list[Event] = []
    seg: list[Event] = []
    started = False
    for e in events:
        if isinstance(e, CycleMark):
            out += _scaled(seg, e.cycles) if started else seg
            seg, started = [], True
        elif not isinstance(e, MARKS):
            seg.append(e)
    return out + seg                        # après la dernière mesure : estimés


def _scaled(seg: list[Event], measured: int) -> list[Event]:
    if not seg:
        return seg
    est = sum(e.cycles for e in seg)
    gap = next((i for i, e in enumerate(seg) if isinstance(e, TraceGap)), None)
    if gap is not None or est <= 0:
        k = gap if gap is not None else len(seg) - 1
        out = list(seg)
        out[k] = replace(seg[k], cycles=seg[k].cycles + measured - est)
        return out
    out, acc, done = [], 0, 0
    for e in seg:
        acc += e.cycles
        cum = acc * measured // est
        out.append(replace(e, cycles=cum - done) if e.cycles or cum != done else e)
        done = cum
    return out


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
    resumed: bool = False        # morceau qui commence à la reprise de la tâche
    suspended: bool = False      # morceau qui s'arrête à un changement de tâche

    @property
    def duration(self) -> int:
        return self.end - self.start


@dataclass
class TimingResult:
    stats: dict[str, FuncStats]
    intervals: list[Interval]
    total: int                   # cycles couverts par la trace
    context_switches: int = 0
    #: arbre d'appels : chemin (noms, de la racine au nœud) → statistiques du nœud
    tree: dict[tuple, FuncStats] = field(default_factory=dict)

    def rows(self, kind: str | None = None) -> list[FuncStats]:
        out = [s for s in self.stats.values() if kind is None or s.kind == kind]
        return sorted(out, key=lambda s: (-s.gross, s.name))

    def children(self, path: tuple = ()) -> list[tuple[tuple, FuncStats]]:
        """Nœuds fils de `path` (racines si vide), du plus gros temps brut au plus petit."""
        if not hasattr(self, "_kids"):
            kids: dict[tuple, list] = {}
            for p in self.tree:
                kids.setdefault(p[:-1], []).append(p)
            self._kids = kids
        out = [(p, self.tree[p]) for p in self._kids.get(tuple(path), [])]
        return sorted(out, key=lambda x: (-x[1].gross, x[1].name))

    def has_children(self, path: tuple) -> bool:
        self.children(())
        return bool(self._kids.get(tuple(path)))


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
    piece: int = -1              # début du morceau visible courant (reprises de tâche)
    resumed: bool = False
    path: tuple = ()             # chemin d'appel (arbre d'appels)

    def __post_init__(self):
        if self.piece < 0:
            self.piece = self.start


@dataclass
class _Suspended:
    """Pile d'une tâche sortie par un changement de contexte."""
    frames: list[_Frame]
    prev: "_Prev"
    at: int


@dataclass
class _Prev:
    addr: int
    ins: object


class TimingAnalyzer:
    def __init__(self, image, symbols, max_intervals: int = 200_000, max_tasks: int = 64,
                 max_nodes: int = 50_000):
        self.image = image
        self.symbols = symbols
        self.max_intervals = max_intervals
        self.max_tasks = max_tasks
        self.max_nodes = max_nodes

    # ------------------------------------------------------------------ API
    def analyze(self, events: list[Event]) -> TimingResult:
        self.stack: list[_Frame] = []
        self.stats: dict[str, FuncStats] = {}
        self.tree: dict[tuple, FuncStats] = {}
        self._paths: set[tuple] = set()
        self.intervals: list[Interval] = []
        self.t = 0
        self.prev: _Prev | None = None
        self.switches = 0
        self.suspended: list[_Suspended] = []
        self.after_exc = False                  # le pas suit un retour d'exception
        for e in measured_cycles(events):
            if isinstance(e, Step):
                self._step(e)
            elif isinstance(e, ExcEntry):
                self._exc_entry(e)
            elif isinstance(e, ExcReturn):
                self._exc_return(e)
            elif isinstance(e, TraceGap):
                self._gap(e)
        while self.stack:                       # fin de trace : cadres encore ouverts
            self._close(len(self.stack) - 1, complete_end=False)
        for susp in self.suspended:             # tâches jamais reprises
            self._finish(susp)
        self.suspended = []
        if len(self.intervals) > self.max_intervals:
            self.intervals = self.intervals[-self.max_intervals:]
        return TimingResult(self.stats, self.intervals, self.t, self.switches, self.tree)

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

    def _path(self, parent: tuple, name: str) -> tuple:
        """Chemin d'un nouveau cadre sous `parent`, plafonds compris."""
        if parent and parent[-1] == ELLIPSIS and len(parent) > MAX_DEPTH:
            return parent                        # déjà sous le nœud « … » de profondeur
        path = parent + (ELLIPSIS,) if len(parent) >= MAX_DEPTH else parent + (name,)
        if path not in self._paths:
            if len(self._paths) >= self.max_nodes and path[-1] != ELLIPSIS:
                path = parent + (ELLIPSIS,)
            self._paths.add(path)
        return path

    def _push(self, addr: int, ret: int | None, partial: bool) -> None:
        name, fstart = self._name(addr)
        parent = self.stack[-1].path if self.stack else ()
        self.stack.append(_Frame(name, FUNCTION, self.t, len(self.stack), ret=ret, entry=addr,
                                 func_start=fstart, partial_start=partial,
                                 path=self._path(parent, name)))

    def _close(self, i: int, complete_end: bool = True) -> None:
        """Ferme les cadres i..sommet (le sommet d'abord) à l'instant courant."""
        while len(self.stack) > i:
            fr = self.stack.pop()
            dur = self.t - fr.start
            if fr.kind == EXCEPTION:            # l'interruption est déduite des cadres interrompus
                for below in self.stack:
                    below.irq += dur
            self._account(fr, self.t, complete_end and not fr.partial_start)
            if self.t > fr.piece or fr.piece == fr.start:
                self.intervals.append(Interval(fr.piece, self.t, fr.depth, fr.name, fr.kind,
                                           fr.partial_start and not fr.resumed, not complete_end,
                                           fr.entry, resumed=fr.resumed))

    def _account(self, fr: _Frame, end: int, complete: bool) -> None:
        gross = end - fr.start - fr.irq
        st = self.stats.get(fr.name)
        if st is None:
            st = self.stats[fr.name] = FuncStats(fr.name, fr.kind, entry=fr.entry)
        _add(st, gross, fr.self_c, complete)
        node = self.tree.get(fr.path)
        if node is None:
            name = fr.path[-1] if fr.path else fr.name
            node = self.tree[fr.path] = FuncStats(name, fr.kind, entry=fr.entry if name == fr.name else None)
        _add(node, gross, fr.self_c, complete)

    # ------------------------------------------------------------------ tâches (RTOS)
    def _suspend(self, base: int, prev: "_Prev") -> None:
        """Changement de tâche : la pile du contexte sortant est mise de côté."""
        frames = self.stack[base:]
        del self.stack[base:]
        for fr in frames:                       # morceau visible jusqu'au changement
            self.intervals.append(Interval(fr.piece, self.t, fr.depth, fr.name, fr.kind,
                                           fr.partial_start and not fr.resumed, False, fr.entry,
                                           resumed=fr.resumed, suspended=True))
        self.suspended.append(_Suspended(frames, prev, self.t))
        if len(self.suspended) > self.max_tasks:
            self._finish(self.suspended.pop(0))

    def _finish(self, susp: _Suspended) -> None:
        """Tâche jamais reprise : ses appels se terminent, partiels, à sa suspension."""
        for fr in reversed(susp.frames):
            self._account(fr, susp.at, False)

    def _resume(self, addr: int) -> _Prev | None:
        """Reprend la tâche suspendue dont la dernière instruction mène à `addr`."""
        found: dict[int, list[_Suspended]] = {}
        for susp in self.suspended:             # du plus ancien au plus récent
            p, ins = susp.prev, susp.prev.ins
            if addr == p.addr + ins.size or (ins.kind == DIRECT and addr == ins.target):
                found.setdefault(2, []).append(susp)
            elif ins.kind == INDIRECT and (any(fr.ret == addr for fr in susp.frames)
                                           or self._name(addr)[1] == susp.frames[-1].func_start):
                found.setdefault(1, []).append(susp)
        if not found:
            return None
        cands = found[max(found)]
        # plusieurs tâches arrêtées au même endroit : indiscernables ici, sauf si leurs piles
        # sont identiques (même suite d'appels) ; sinon on attend le premier retour
        if len({tuple((fr.name, fr.ret) for fr in c.frames) for c in cands}) > 1:
            return None
        self._restore(cands[0], self.t)
        return cands[0].prev

    def _restore(self, susp: _Suspended, resumed_at: int) -> None:
        self.suspended.remove(susp)
        away = resumed_at - susp.at             # temps passé dans les autres tâches
        for fr in susp.frames:
            fr.irq += away
            fr.piece, fr.resumed = self.t, True
            fr.depth = len(self.stack)
            self.stack.append(fr)

    def _late_resume(self, base: int, addr: int) -> bool:
        """Une racine partielle (tâche reprise sans pile identifiée) retourne vers `addr` :
        la tâche suspendue dont le sommet est la même fonction et retourne là est la bonne
        (s'il y en a une seule, ou des piles identiques). La racine est fusionnée dans ce
        sommet, qui se termine ici comme un appel complet."""
        if len(self.stack) - base != 1:
            return False
        root = self.stack[-1]
        if not root.partial_start or root.kind != FUNCTION:
            return False
        cands = [susp for susp in self.suspended
                 if susp.frames[-1].ret == addr and susp.frames[-1].func_start == root.func_start]
        if not cands or len({tuple((fr.name, fr.ret) for fr in c.frames) for c in cands}) > 1:
            return False
        susp = cands[0]
        top = susp.frames[-1]
        self.stack.pop()
        self.intervals.append(Interval(root.piece, self.t, root.depth, root.name, root.kind,
                                       False, False, root.entry, resumed=True))
        self._restore(susp, root.start)
        top.self_c += root.self_c
        top.irq += root.irq
        top.piece = self.t                      # déjà dessiné par le morceau de la racine
        self._close(len(self.stack) - 1)
        return True

    @staticmethod
    def _plausible(prev: _Prev, addr: int, ctx: list[_Frame] | None = None) -> bool:
        """`addr` peut-elle suivre `prev` dans le même contexte d'exécution ? Après un retour
        d'exception (`ctx` fourni), un retour de fonction interrompu ne peut reprendre qu'à une
        adresse de retour connue de la pile (si elle en a une)."""
        ins = prev.ins
        if addr == prev.addr + ins.size or (ins.kind == DIRECT and addr == ins.target):
            return True
        if ins.kind != INDIRECT:
            return False
        if ctx and ins.ret:
            rets = [fr.ret for fr in ctx if fr.ret is not None]
            return not rets or addr in rets
        return True

    # ------------------------------------------------------------------ événements
    def _step(self, e: Step) -> None:
        addr = e.addr
        ins = self.image.instr(addr)
        base = self._ctx_base()
        prev = self.prev
        # 1. reprise après exception qui ne continue pas le code interrompu : changement de
        #    tâche ; la pile sortante est mise de côté, celle de la tâche reprise restaurée
        after_exc, self.after_exc = self.after_exc, False
        if prev is not None and not self._plausible(prev, addr, self.stack[base:] if after_exc else None):
            had = len(self.stack) > base
            if had:
                self._suspend(base, prev)
            prev = self._resume(addr)
            if had or prev is not None:
                self.switches += 1
        # 2. retour : l'exécution atteint l'adresse de retour d'un cadre du contexte
        returned = False
        for i in range(len(self.stack) - 1, base - 1, -1):
            if self.stack[i].ret == addr:
                self._close(i)
                returned = True
                break
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
                # trace) : le cadre se termine, l'appelant devient la nouvelle racine — sauf si
                # c'est une tâche reprise dont la pile, en attente, contient cet appelant
                if not self._late_resume(base, addr):
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
                                 handler=e.handler, saved_prev=self.prev,
                                 path=self._path((), label)))
        self.prev = None
        self.stack[-1].self_c += e.cycles       # latence d'entrée
        self.t += e.cycles

    def _gap(self, e: TraceGap) -> None:
        """Trace interrompue (filtre de l'ETM) : la pile d'appels ne vaut plus ; les cadres
        ouverts sont fermés comme partiels, et la reprise repart d'une pile vide."""
        while self.stack:
            self._close(len(self.stack) - 1, complete_end=False)
        for susp in self.suspended:
            self._finish(susp)
        self.suspended = []
        self.prev = None
        self.after_exc = False                  # le temps non tracé (e.cycles) n'est à personne

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
        self.after_exc = True

    def _last_exception(self) -> int | None:
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i].kind == EXCEPTION:
                return i
        return None


def _add(st: FuncStats, gross: int, net: int, complete: bool) -> None:
    st.gross += gross
    st.net += net
    if complete:
        st.calls += 1
        st.gross_complete += gross
        st.min = gross if st.min is None else min(st.min, gross)
        st.max = max(st.max, gross)
    else:
        st.partial += 1


def to_us(cycles: float, cpu_hz: int) -> float:
    return cycles * 1e6 / cpu_hz
