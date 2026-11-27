"""Distribution de durées ou de valeurs (Trace.STATistic.DISTribution) : histogramme, centiles,
valeurs extrêmes avec leur position dans la trace.

`Reservoir` garde les valeurs d'une série sans croître sans fin : compte, minimum, maximum et
somme exacts ; jusqu'à `capacity` valeurs gardées telles quelles (centiles exacts), au-delà un
échantillon uniforme (algorithme R, graine fixe : même trace, même résultat) ; et les `top`
plus grandes valeurs avec leur position, toujours exactes. `distribution()` en tire
l'histogramme (classes linéaires, ou logarithmiques pour des durées très étalées) et les
centiles p50, p90, p99 (rang le plus proche, sur l'échantillon).
"""
from __future__ import annotations

import heapq
import math
import random
from dataclasses import dataclass, field

CAPACITY = 20_000
TOP = 10


@dataclass
class Reservoir:
    capacity: int = CAPACITY
    top: int = TOP
    count: int = 0
    total: float = 0.0
    min: float | None = None
    max: float | None = None
    values: list[float] = field(default_factory=list)
    #: plus grandes valeurs : tas de (valeur, -position, position), le plus petit en tête
    largest: list[tuple] = field(default_factory=list)
    _rng: random.Random = field(default_factory=lambda: random.Random(0x5EED), repr=False)

    def add(self, value: float, where: int | None = None) -> None:
        self.count += 1
        self.total += value
        self.min = value if self.min is None else min(self.min, value)
        self.max = value if self.max is None else max(self.max, value)
        if len(self.values) < self.capacity:
            self.values.append(value)
        else:
            k = self._rng.randrange(self.count)
            if k < self.capacity:
                self.values[k] = value
        item = (value, -(where if where is not None else -1), where)
        if len(self.largest) < self.top:
            heapq.heappush(self.largest, item)
        elif item > self.largest[0]:
            heapq.heapreplace(self.largest, item)

    @property
    def mean(self) -> float | None:
        return self.total / self.count if self.count else None

    @property
    def sampled(self) -> bool:
        """Centiles tirés d'un échantillon (plus de valeurs que la capacité) ?"""
        return self.count > len(self.values)

    def longest(self) -> list[tuple[float, int | None]]:
        """[(valeur, position)] des plus grandes valeurs, de la plus grande à la plus petite."""
        return [(v, w) for v, _k, w in sorted(self.largest, reverse=True)]


@dataclass
class Bin:
    low: float
    high: float
    count: int


@dataclass
class Distribution:
    name: str
    count: int
    min: float | None
    max: float | None
    mean: float | None
    percentiles: dict[int, float]
    bins: list[Bin]
    longest: list[tuple[float, int | None]]
    log: bool = False
    sampled: bool = False
    unit: str = ""

    def summary(self) -> str:
        if not self.count:
            return f"{self.name} : aucune valeur"
        u = f" {self.unit}" if self.unit else ""
        p = ", ".join(f"p{k} {fmt(v)}{u}" for k, v in sorted(self.percentiles.items()))
        text = (f"{self.name} : {self.count} valeurs, min {fmt(self.min)}{u}, moyenne {fmt(self.mean)}{u}, "
                f"max {fmt(self.max)}{u} ; {p}")
        return text + (" (centiles sur un échantillon)" if self.sampled else "")

    def as_dict(self) -> dict:
        return {"nom": self.name, "unité": self.unit, "nombre": self.count, "min": self.min,
                "max": self.max, "moyenne": self.mean,
                "centiles": {f"p{k}": v for k, v in self.percentiles.items()},
                "classes": [{"de": b.low, "à": b.high, "nombre": b.count} for b in self.bins],
                "plus longs": [{"valeur": v, "position": w} for v, w in self.longest],
                "logarithmique": self.log, "échantillonné": self.sampled}


def fmt(v: float | None) -> str:
    if v is None:
        return "–"
    if float(v).is_integer() and abs(v) < 1e12:
        return f"{int(v)}"
    return f"{v:.3g}" if abs(v) < 100 else f"{v:.1f}"


def percentile(sorted_values: list[float], p: float) -> float:
    """Rang le plus proche (p en %), sur des valeurs triées non vides."""
    k = max(0, math.ceil(p / 100 * len(sorted_values)) - 1)
    return sorted_values[min(k, len(sorted_values) - 1)]


def distribution(name: str, res: Reservoir, bins: int = 20, log: bool | None = None,
                 scale: float = 1.0, unit: str = "") -> Distribution:
    """Distribution d'un réservoir ; `scale` convertit les valeurs (cycles → µs) ; `log` None :
    classes logarithmiques si le maximum dépasse 100 fois le minimum non nul."""
    vals = sorted(v * scale for v in res.values)
    longest = [(v * scale, w) for v, w in res.longest()]
    if not vals:
        return Distribution(name, 0, None, None, None, {}, [], [], unit=unit)
    lo, hi = res.min * scale, res.max * scale
    if log is None:
        log = lo > 0 and hi / lo > 100
    if log and lo <= 0:
        log = False
    bins = max(1, int(bins))
    if hi == lo:
        edges = [lo, hi]
    elif log:
        a, b = math.log10(lo), math.log10(hi)
        edges = [float(f"{10 ** (a + (b - a) * i / bins):.12g}") for i in range(bins + 1)]
        edges[0], edges[-1] = lo, hi             # bornes arrondies : une décade tombe juste
    else:
        edges = [lo + (hi - lo) * i / bins for i in range(bins + 1)]
    counts = [0] * (len(edges) - 1)
    j = 0
    for v in vals:                           # valeurs triées : un seul parcours
        while j < len(counts) - 1 and v >= edges[j + 1]:
            j += 1
        counts[j] += 1
    if res.sampled:                          # échantillon : classes ramenées au compte total
        k = res.count / len(vals)
        counts = [round(c * k) for c in counts]
    out = [Bin(edges[i], edges[i + 1], counts[i]) for i in range(len(counts))]
    pct = {p: percentile(vals, p) for p in (50, 90, 99)}
    return Distribution(name, res.count, lo, hi, res.mean * scale, pct, out, longest, log,
                        res.sampled, unit)
