"""Filtre de la trace d'instructions (TraceEnable de l'ETM) : plages d'adresses incluses et
déclencheurs start/stop.

- **Plages** : seules les instructions dont l'adresse tombe dans une plage sont tracées
  (fonctions, fichiers, ou adresses). Sur un SWO au débit limité, tracer une partie du code
  laisse tout le débit à ce qui compte.
- **Start / stop** : la trace démarre quand le cœur exécute une adresse de démarrage, et
  s'arrête après une adresse d'arrêt (TraceOn / TraceOff) ; avec des plages, les deux se
  combinent (ET).

Le matériel diffère : l'ETMv4 du Cortex-M7 a ses propres comparateurs d'adresse (plages
exactes) ; l'ETMv3.5 des M3/M4 n'en a pas et passe par les comparateurs du DWT, dont une
plage est un bloc aligné d'une puissance de deux : la plage programmée englobe alors la plage
demandée (`EtmConfig` donne le filtre effectif). L'ETM simulé applique le filtre effectif au
chemin de QEMU (`apply`), comme le matériel.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable

from .qemu_log import Event, ExcEntry, ExcReturn, Step, TraceGap


@dataclass
class EtmFilter:
    ranges: list[tuple[int, int]] = field(default_factory=list)   # [début, fin[
    start: list[int] = field(default_factory=list)                # TraceOn à ces adresses
    stop: list[int] = field(default_factory=list)                 # TraceOff après celles-ci
    #: ce que l'utilisateur a demandé (« fib », « main.c »), pour l'affichage
    labels: list[str] = field(default_factory=list)

    @property
    def active(self) -> bool:
        return bool(self.ranges or self.start or self.stop)

    def in_ranges(self, addr: int) -> bool:
        return not self.ranges or any(lo <= addr < hi for lo, hi in self.ranges)

    def describe(self) -> str:
        if not self.active:
            return "aucun filtre : tout le code"
        parts = []
        if self.ranges:
            names = [lb for lb in self.labels if not lb.startswith(("start ", "stop "))]
            spans = ", ".join(f"{lo:#x}–{hi:#x}" for lo, hi in self.ranges)
            parts.append(f"plages {', '.join(names) + ' ' if names else ''}({spans})")
        if self.start:
            parts.append("start " + ", ".join(f"{a:#x}" for a in self.start))
        if self.stop:
            parts.append("stop " + ", ".join(f"{a:#x}" for a in self.stop))
        return "filtre : " + " ; ".join(parts)

    def apply(self, events: Iterable[Event]) -> list[Event]:
        """Chemin exécuté → ce que l'ETM trace avec ce filtre (ETM simulé). Une instruction
        est tracée si elle est dans une plage et que la trace est démarrée ; une adresse de
        démarrage est tracée, une adresse d'arrêt aussi (la trace s'arrête après elle). Une
        reprise après une interruption de la trace est marquée par `TraceGap` ; une entrée ou
        un retour d'exception est gardé quand l'instruction d'avant est tracée."""
        evs = list(events)
        if not self.active:
            return evs
        start, stop = set(self.start), set(self.stop)
        on = not start
        traced: list[bool] = []
        live: list[bool] = []               # tracée, et la trace reste démarrée après elle
        for ev in evs:
            if isinstance(ev, Step):
                if ev.addr in start:
                    on = True
                traced.append(on and self.in_ranges(ev.addr))
                if ev.addr in stop:
                    on = False
                live.append(traced[-1] and on)
            else:
                traced.append(False)
                live.append(False)
        out: list[Event] = []
        last_traced = None                  # la dernière instruction vue était-elle tracée ?
        last_live = False                   # … et la trace est-elle encore démarrée ?
        started = False
        gap_next: int | None = None         # première instruction non tracée du trou
        kept: list[bool] = []               # exceptions en cours : entrée tracée ?
        for i, ev in enumerate(evs):
            if isinstance(ev, Step):
                if traced[i]:
                    if started and last_traced is False:
                        out.append(TraceGap(next_addr=gap_next))
                    out.append(ev)
                    started = True
                elif last_traced:
                    gap_next = ev.addr
                last_traced, last_live = traced[i], live[i]
            elif isinstance(ev, ExcEntry):
                # l'exception interrompt du code tracé : l'ETM la trace, même si son handler ne
                # l'est pas ; elle borne aussi les instructions d'avant (ETMv4 : adresse de
                # retour). Un enchaînement suppose l'entrée de l'exception d'origine tracée.
                keep = last_live and (not ev.tailchain or (bool(kept) and kept[-1]))
                if ev.tailchain and kept:
                    kept[-1] = keep
                else:
                    kept.append(keep)
                if keep:
                    out.append(ev)
            elif isinstance(ev, ExcReturn):
                was_kept = kept.pop() if kept else False
                if last_live and was_kept:          # retour exécuté dans un handler tracé
                    out.append(ev)
        return out


# ---------------------------------------------------------------------- spécifications
_RANGE = re.compile(r"^(0x[0-9a-fA-F]+|\d+)\s*([-+])\s*(0x[0-9a-fA-F]+|\d+)$")


def _int(text: str) -> int:
    return int(text, 0)


def resolve_range(spec: str, symbols) -> tuple[list[tuple[int, int]], str]:
    """Plage(s) d'une spécification : `fib` (fonction), `main.c` (fonctions d'un fichier,
    plages contiguës fusionnées), `0x100-0x200` (fin exclue) ou `0x100+64`."""
    spec = spec.strip()
    m = _RANGE.match(spec)
    if m:
        lo, op, b = _int(m.group(1)), m.group(2), _int(m.group(3))
        hi = lo + b if op == "+" else b
        if hi <= lo:
            raise ValueError(f"plage vide : {spec}")
        return [(lo, hi)], spec
    if symbols is None:
        raise ValueError(f"« {spec} » : pas de symboles pour résoudre le nom")
    f = symbols.function(spec)
    if f is not None:
        return [(f.start, max(f.end, f.start + 2))], spec
    funcs = []
    for fn in symbols.functions:
        le = symbols.line_at(fn.start)
        if le is not None and (le.file == spec or le.file.endswith("/" + spec)) and fn.size:
            funcs.append((fn.start, fn.end))
    if not funcs:
        raise ValueError(f"« {spec} » : ni fonction, ni fichier source, ni plage d'adresses "
                         "(0x100-0x200, 0x100+64)")
    merged: list[tuple[int, int]] = []
    for lo, hi in sorted(funcs):
        if merged and lo <= merged[-1][1] + 4:          # contiguës (alignement compris)
            merged[-1] = (merged[-1][0], max(merged[-1][1], hi))
        else:
            merged.append((lo, hi))
    return merged, spec


def resolve_address(spec: str, symbols) -> int:
    """Adresse d'un déclencheur : `fib` (entrée de la fonction), `main.c:59` (première
    instruction de la ligne) ou `0x1f4`."""
    spec = spec.strip()
    try:
        return _int(spec) & ~1
    except ValueError:
        pass
    if symbols is None:
        raise ValueError(f"« {spec} » : pas de symboles pour résoudre le nom")
    f = symbols.function(spec)
    if f is not None:
        return f.start
    if ":" in spec:
        file, _, line = spec.rpartition(":")
        for name in symbols.source_files():
            if name == file or name.endswith("/" + file):
                addrs = symbols.addresses_for_line(name, int(line))
                if addrs:
                    return min(addrs)
    raise ValueError(f"« {spec} » : ni fonction, ni fichier:ligne, ni adresse")


def build_filter(ranges: Iterable[str] = (), start: Iterable[str] = (), stop: Iterable[str] = (),
                 symbols=None) -> EtmFilter:
    """Filtre depuis des spécifications textuelles (ligne de commande, API, GUI)."""
    flt = EtmFilter()
    for spec in ranges:
        rs, label = resolve_range(spec, symbols)
        flt.ranges += rs
        flt.labels.append(label)
    for spec in start:
        flt.start.append(resolve_address(spec, symbols))
        flt.labels.append(f"start {spec}")
    for spec in stop:
        flt.stop.append(resolve_address(spec, symbols))
        flt.labels.append(f"stop {spec}")
    return flt
