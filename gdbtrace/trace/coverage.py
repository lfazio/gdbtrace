"""Couverture de code depuis la trace d'instructions (l'équivalent de COVerage de Trace32).

Non intrusive : ni instrumentation ni recompilation, on mesure le binaire livré. Chaque
instruction de la trace ETM porte un atome **E** (exécutée, ou branchement pris) ou **N**
(condition fausse, ou branchement conditionnel non pris). La base compte, par adresse, les E
et les N. Elle s'accumule d'exécution en exécution (COVerage.ADD) jusqu'à sa remise à zéro,
et se sauvegarde puis se fusionne entre sessions et campagnes de test (COVerage.SAVE /
COVerage.LOAD /Add) tant que le code est identique.

Mesures, au niveau du code objet comme Trace32, rapportées aux lignes et aux fonctions par
les informations de débogage DWARF :
- **instructions** (statement coverage objet) : exécutée au moins une fois ; un branchement
  conditionnel compte dès qu'il est atteint, une autre instruction conditionnelle (bloc IT)
  seulement si sa condition a été vraie au moins une fois ;
- **branches** (decision coverage objet) : chaque branchement conditionnel (B<c>, CBZ/CBNZ,
  branchement dans un bloc IT) a deux issues, pris et non pris ; il est couvert quand les
  deux ont été vues. Une branche jamais prise trahit une condition toujours vraie ou toujours
  fausse dans les tests ;
- **lignes** et **fonctions** : ok (tout couvert), partiel, jamais.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
from dataclasses import dataclass, field

from .etm.image import DIRECT, INDIRECT
from .etm.qemu_log import Step

OK, PARTIAL, NEVER = "ok", "partiel", "jamais"
# état d'une instruction
I_OK, I_TAKEN, I_NOT_TAKEN, I_COND_FALSE, I_NEVER = "ok", "pris", "non pris", "cond. fausse", "jamais"
TAG_HELP = {
    I_OK: "exécutée (branchement : pris et non pris)",
    I_TAKEN: "branchement toujours pris",
    I_NOT_TAKEN: "branchement jamais pris",
    I_COND_FALSE: "condition toujours fausse : jamais exécutée",
    I_NEVER: "jamais atteinte",
}
FORMAT = "gdbtrace-coverage"


@dataclass
class InstrInfo:
    addr: int
    size: int
    text: str
    branch: bool                 # branchement conditionnel (deux issues)
    file: str | None
    line: int | None


@dataclass
class Counts:
    """Totaux d'une ligne, d'une fonction, d'un fichier ou du programme."""
    instrs: int = 0
    covered: int = 0             # instructions couvertes
    branches: int = 0            # branchements conditionnels
    outcomes: int = 0            # issues vues (0 à 2 par branchement)
    hits: int = 0                # exécutions (max sur les instructions : compte de passages)

    def add(self, o: "Counts") -> None:
        self.instrs += o.instrs
        self.covered += o.covered
        self.branches += o.branches
        self.outcomes += o.outcomes
        self.hits = max(self.hits, o.hits)

    @property
    def percent(self) -> float:
        return 100.0 * self.covered / self.instrs if self.instrs else 0.0

    @property
    def branch_percent(self) -> float | None:
        return 100.0 * self.outcomes / (2 * self.branches) if self.branches else None

    @property
    def status(self) -> str:
        if self.covered == 0:
            return NEVER
        if self.covered == self.instrs and self.outcomes == 2 * self.branches:
            return OK
        return PARTIAL


@dataclass
class FuncCov(Counts):
    name: str = ""
    file: str | None = None
    line: int | None = None
    start: int = 0
    entries: int = 0             # passages par la première instruction


@dataclass
class FileCov(Counts):
    path: str = ""
    functions: list[FuncCov] = field(default_factory=list)


class CoverageDB:
    def __init__(self, image, symbols):
        self.image = image
        self.symbols = symbols
        self.e: dict[int, int] = {}          # atomes E par adresse
        self.n: dict[int, int] = {}          # atomes N par adresse
        self.generation = 0
        self._lock = threading.Lock()
        self._universe: list[tuple[list, list[InstrInfo]]] | None = None
        self._by_addr: dict[int, InstrInfo] | None = None
        self._by_line: dict[tuple[str | None, int | None], list[InstrInfo]] = {}

    # ------------------------------------------------------------------ alimentation
    def add(self, events) -> None:
        """COVerage.ADD : ajoute les instructions d'une portion de trace."""
        e, n = self.e, self.n
        with self._lock:
            for ev in events:
                if isinstance(ev, Step):
                    if ev.executed:
                        e[ev.addr] = e.get(ev.addr, 0) + 1
                    else:
                        n[ev.addr] = n.get(ev.addr, 0) + 1
            self.generation += 1

    def reset(self) -> None:
        """COVerage.RESet."""
        with self._lock:
            self.e.clear()
            self.n.clear()
            self.generation += 1

    def executed(self) -> set[int]:
        """Adresses exécutées au moins une fois (atome E, ou branchement atteint)."""
        with self._lock:
            return set(self.e) | set(self.n)

    # ------------------------------------------------------------------ programme
    def universe(self) -> list[tuple[list, list[InstrInfo]]]:
        """[(fonctions de même adresse, instructions)] : le code mesurable du programme, hors
        « literal pools » ; les instructions d'un bloc IT sont conditionnelles."""
        if self._universe is not None:
            return self._universe
        sym = self.symbols
        by_start: dict[int, list] = {}
        for f in sym.functions if sym else []:
            if f.size:
                by_start.setdefault(f.start, []).append(f)
        out = []
        for start in sorted(by_start):
            funcs = sorted(by_start[start], key=lambda x: (-x.size, x.name.startswith("_"), x.name))
            f = funcs[0]
            instrs: list[InstrInfo] = []
            addr, it_left = f.start, 0
            prev, prev_in_it = None, False
            while addr < f.end:
                if sym.is_data(addr):            # literal pool : données au milieu du code
                    _drop_padding(instrs)
                    addr += 2
                    continue
                ins = self.image.instr(addr)
                if ins is None:
                    break
                mnem = ins.text.split()[0].split(".")[0] if ins.text else ""
                in_it = it_left > 0
                it_left = len(mnem) - 1 if mnem.startswith("it") and len(mnem) <= 4 and \
                    set(mnem[2:]) <= {"t", "e"} else max(0, it_left - 1)
                branch = ins.kind in (DIRECT, INDIRECT) and (ins.cond or in_it) and not ins.call
                if mnem == "nop" and prev is not None and prev.kind in (DIRECT, INDIRECT) \
                        and not prev.cond and not prev_in_it and not prev.call:
                    prev, prev_in_it = ins, in_it    # alignement après un saut : inatteignable
                    addr += ins.size
                    continue
                prev, prev_in_it = ins, in_it
                le = sym.line_at(addr)
                instrs.append(InstrInfo(addr, ins.size, ins.text, branch,
                                        os.path.normpath(le.file) if le else None, le.line if le else None))
                addr += ins.size
            _drop_padding(instrs)
            if instrs:
                out.append((funcs, instrs))
        self._universe = out
        self._by_addr = {i.addr: i for _f, ins in out for i in ins}
        for _f, ins in out:
            for i in ins:
                self._by_line.setdefault((i.file, i.line), []).append(i)
        return out

    def code_hash(self) -> str:
        """Empreinte du code chargé : on ne fusionne que des couvertures du même programme."""
        h = hashlib.sha256()
        for base, data in self.image.segments:
            h.update(base.to_bytes(4, "little"))
            h.update(data)
        return h.hexdigest()

    # ------------------------------------------------------------------ mesures
    def _counts(self, ins: InstrInfo, e: dict, n: dict, into: Counts) -> None:
        ne, nn = e.get(ins.addr, 0), n.get(ins.addr, 0)
        into.instrs += 1
        if ins.branch:
            into.branches += 1
            into.outcomes += (ne > 0) + (nn > 0)
            into.covered += (ne + nn) > 0
        else:
            into.covered += ne > 0
        into.hits = max(into.hits, ne + nn)

    def tag(self, addr: int) -> str:
        """État d'une instruction : ok, pris, non pris, cond. fausse, jamais."""
        self.universe()
        ins = self._by_addr.get(addr)
        with self._lock:
            ne, nn = self.e.get(addr, 0), self.n.get(addr, 0)
        if ne == 0 and nn == 0:
            return I_NEVER
        if ins is not None and ins.branch:
            return I_OK if ne and nn else I_TAKEN if ne else I_NOT_TAKEN
        return I_OK if ne else I_COND_FALSE

    def functions(self) -> list[FuncCov]:
        with self._lock:
            e, n = dict(self.e), dict(self.n)
        out = []
        for funcs, instrs in self.universe():
            name = " = ".join(sorted({x.name for x in funcs}, key=lambda s: (s.startswith("_"), s)))
            fc = FuncCov(name=name, file=instrs[0].file, line=instrs[0].line, start=instrs[0].addr,
                         entries=e.get(instrs[0].addr, 0))
            for ins in instrs:
                self._counts(ins, e, n, fc)
            out.append(fc)
        return out

    def files(self) -> list[FileCov]:
        """COVerage.ListModule : par fichier source, avec ses fonctions."""
        by_file: dict[str, FileCov] = {}
        for fc in self.functions():
            key = fc.file or "?"
            m = by_file.setdefault(key, FileCov(path=key))
            m.add(fc)
            m.functions.append(fc)
        for m in by_file.values():
            m.functions.sort(key=lambda f: (f.line or 0, f.name))
        return sorted(by_file.values(), key=lambda m: m.path)

    def lines(self, path: str) -> dict[int, Counts]:
        """COVerage.ListLine : {ligne: totaux} pour les lignes de `path` qui portent du code."""
        with self._lock:
            e, n = dict(self.e), dict(self.n)
        out: dict[int, Counts] = {}
        path = os.path.normpath(path)
        for _funcs, instrs in self.universe():
            for ins in instrs:
                if ins.file == path and ins.line is not None:
                    self._counts(ins, e, n, out.setdefault(ins.line, Counts()))
        return out

    def instructions(self, path: str, line: int) -> list[InstrInfo]:
        """Instructions de la ligne `line` de `path` (code objet de la ligne)."""
        self.universe()
        return self._by_line.get((os.path.normpath(path), line), [])

    def measured(self, addr: int) -> bool:
        """`addr` fait partie du code mesuré (ni literal pool, ni remplissage) ?"""
        self.universe()
        return addr in self._by_addr

    def total(self) -> tuple[Counts, int, int]:
        """(totaux du programme, fonctions entrées, fonctions)."""
        t = Counts()
        funcs = self.functions()
        for fc in funcs:
            t.add(fc)
        return t, sum(1 for f in funcs if f.covered), len(funcs)

    def summary(self) -> str:
        t, called, nfuncs = self.total()
        br = f"{t.branch_percent:.1f} % ({t.outcomes}/{2 * t.branches} issues)" if t.branches else "—"
        return (f"instructions {t.percent:.1f} % ({t.covered}/{t.instrs}) — branches {br} — "
                f"fonctions {called}/{nfuncs}")

    # ------------------------------------------------------------------ persistance
    def save(self, path: str) -> None:
        """COVerage.SAVE : base JSON, rechargeable et fusionnable."""
        with self._lock:
            data = {"format": FORMAT, "version": 1, "elf": os.path.abspath(self.image.path),
                    "code": self.code_hash(),
                    "e": {f"{a:#x}": c for a, c in sorted(self.e.items())},
                    "n": {f"{a:#x}": c for a, c in sorted(self.n.items())}}
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=0)

    def load(self, path: str, merge: bool = True) -> None:
        """COVerage.LOAD (/Add si `merge`) : refusé si le code a changé depuis la sauvegarde."""
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        if data.get("format") != FORMAT:
            raise ValueError(f"{path} : pas une base de couverture gdbtrace")
        if data.get("code") != self.code_hash():
            raise ValueError(f"{path} : couverture d'un autre programme (code différent de "
                             f"{os.path.basename(self.image.path)})")
        with self._lock:
            if not merge:
                self.e.clear()
                self.n.clear()
            for key, dst in (("e", self.e), ("n", self.n)):
                for a, c in data.get(key, {}).items():
                    a = int(a, 16)
                    dst[a] = dst.get(a, 0) + int(c)
            self.generation += 1


def line_of_function(db: CoverageDB, name: str) -> FuncCov | None:
    return next((f for f in db.functions() if name in f.name.split(" = ")), None)


def _drop_padding(instrs: list[InstrInfo]) -> None:
    """Retire les `nop` d'alignement placés par le compilateur avant un literal pool ou en
    fin de fonction : jamais exécutables, ils fausseraient la couverture."""
    while len(instrs) > 1 and instrs[-1].text.split()[0].split(".")[0] == "nop" \
            and not instrs[-1].branch:
        instrs.pop()
