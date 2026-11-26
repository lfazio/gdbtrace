"""Statistiques d'exceptions (Trace.STATistic.InterruptIsr) : par exception, le nombre
d'entrées, la durée, la période entre deux entrées et sa gigue, les préemptions et les
enchaînements.

Deux sources, une même forme (`IrqStats`) :
- la **trace d'instructions** (ETM, MTB : `TimingAnalyzer`, `TimingResult.irq`), en cycles
  mesurés ou estimés ;
- la **trace des exceptions de l'ITM** (paquets du DWT : `trace/swo.py`, `ExceptionStats`),
  en unités d'horodatage ITM.

Durée = de l'entrée au retour, sans les exceptions plus prioritaires qui l'ont interrompue.
La latence entre la demande d'interruption et l'entrée ne se voit dans aucune trace (seuls
les cycles de l'entrée elle-même, l'empilement, sont comptés).
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class IrqStats:
    name: str
    number: int | None = None
    count: int = 0                   # entrées
    complete: int = 0                # durées connues (entrée et retour vus)
    total: int = 0
    min: int | None = None
    max: int = 0
    period_n: int = 0
    period_sum: int = 0
    period_min: int | None = None
    period_max: int = 0
    preempted: int = 0               # une autre exception l'a interrompue
    preempting: int = 0              # elle en a interrompu une autre
    tail: int = 0                    # entrées enchaînées (tail-chaining)
    entry_cycles: int = 0            # somme des cycles d'entrée (empilement)
    last_entry: int | None = field(default=None, repr=False)

    @property
    def avg(self) -> float | None:
        return self.total / self.complete if self.complete else None

    @property
    def period_avg(self) -> float | None:
        return self.period_sum / self.period_n if self.period_n else None

    @property
    def jitter(self) -> int | None:
        """Gigue de la période : écart entre la plus longue et la plus courte."""
        return self.period_max - self.period_min if self.period_n else None

    @property
    def entry_avg(self) -> float | None:
        return self.entry_cycles / self.count if self.count and self.entry_cycles else None

    def on_entry(self, t: int | None, tail: bool = False, preempting: bool = False,
                 cycles: int = 0) -> None:
        self.count += 1
        self.tail += tail
        self.preempting += preempting
        self.entry_cycles += cycles
        if t is not None:
            if self.last_entry is not None and t > self.last_entry:
                p = t - self.last_entry
                self.period_n += 1
                self.period_sum += p
                self.period_min = p if self.period_min is None else min(self.period_min, p)
                self.period_max = max(self.period_max, p)
            self.last_entry = t

    def on_duration(self, d: int) -> None:
        self.complete += 1
        self.total += d
        self.min = d if self.min is None else min(self.min, d)
        self.max = max(self.max, d)

    def as_dict(self) -> dict:
        return {"exception": self.name, "numero": self.number, "entrees": self.count,
                "duree_min": self.min, "duree_moy": self.avg, "duree_max": self.max if self.complete else None,
                "periode_min": self.period_min, "periode_moy": self.period_avg,
                "periode_max": self.period_max if self.period_n else None, "gigue": self.jitter,
                "preemptee": self.preempted, "preempte": self.preempting, "enchainees": self.tail,
                "cycles_entree": self.entry_avg}


def sorted_rows(stats: dict[str, IrqStats]) -> list[IrqStats]:
    return sorted(stats.values(), key=lambda s: (-s.count, s.name))
