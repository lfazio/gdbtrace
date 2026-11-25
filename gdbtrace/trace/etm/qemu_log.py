"""Chemin d'exécution réel d'un Cortex-M tiré du journal de QEMU.

QEMU lancé avec `-accel tcg,one-insn-per-tb=on -d exec,nochain,int` écrit une ligne
« Trace » avant chaque instruction, et décrit les exceptions. C'est la vérité terrain qui
sert à fabriquer et vérifier la trace ETM (QEMU n'émule pas d'ETM), et la source de la
trace « ETM simulé » de l'interface.

Piège : une ligne « Trace » suivie de « Stopped execution of TB chain before <même PC> »
désigne une instruction **non exécutée** (interruption prise avant) ; elle réapparaîtra au
retour d'exception.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, Iterator

_TRACE = re.compile(r"^Trace \d+: 0x[0-9a-f]+ \[[0-9a-f]+/([0-9a-f]+)/")
_STOPPED = re.compile(r"^Stopped execution of TB chain before 0x[0-9a-f]+ \[([0-9a-f]+)\]")
_TAKING = re.compile(r"\.\.\.taking pending (?:non)?secure exception (\d+)")
_NEWPC = re.compile(r"\.\.\.loaded new PC 0x([0-9a-f]+)")


# `cycles` : durée de l'événement (instruction, latence d'exception), mesurée par un ETM
# cycle-accurate ou estimée par le modèle (trace/etm/cycles.py). Hors égalité : deux
# chemins identiques restent égaux quelle que soit la source de temps.

@dataclass(frozen=True)
class Step:
    """Instruction atteinte. `executed` : atome E de l'ETM (exécutée, ou branchement pris) ;
    False pour un atome N (condition fausse, ou branchement conditionnel non pris)."""
    addr: int
    cycles: int = field(default=0, compare=False)
    executed: bool = field(default=True, compare=False)


@dataclass(frozen=True)
class ExcEntry:
    number: int
    handler: int
    tailchain: bool = False      # enchaînée sans retour au code interrompu
    cycles: int = field(default=0, compare=False)
    #: adresse de retour préférée, quand QEMU la donne : interruption prise avant une
    #: instruction (« Stopped execution of TB chain before <PC> ») ; None sinon (exception
    #: synchrone comme SVC : l'instruction qui suit la dernière exécutée)
    ret: int | None = field(default=None, compare=False)


@dataclass(frozen=True)
class ExcReturn:
    """Retour d'exception : l'exécution reprend à l'adresse de la Step suivante."""
    cycles: int = field(default=0, compare=False)


@dataclass(frozen=True)
class TraceGap:
    """Trace interrompue puis reprise ailleurs (filtre de l'ETM : plage ou start/stop) : ce
    qui s'est exécuté entre les deux n'est pas tracé. L'ETM le signale par un I-sync « trace
    activée » (ETMv3) ou un paquet Trace On (ETMv4). `next_addr` (ETM simulé) : instruction
    exécutée juste après la dernière tracée, qui donne l'issue d'un branchement en fin de
    tronçon."""
    cycles: int = field(default=0, compare=False)
    next_addr: int | None = field(default=None, compare=False)


Event = Step | ExcEntry | ExcReturn | TraceGap


def parse(lines: Iterable[str]) -> Iterator[Event]:
    pending: int | None = None          # dernière instruction annoncée, pas encore confirmée
    number: int | None = None
    tailchain = False
    held_return = False                 # ExcReturn retenu : annulé si enchaînement
    stopped: int | None = None          # instruction devant laquelle l'exécution s'est arrêtée
    for line in lines:
        m = _TRACE.match(line)
        if m:
            if pending is not None:
                yield Step(pending)
            if held_return:
                yield ExcReturn()
                held_return = False
            pending = int(m.group(1), 16)
            stopped = None
            continue
        m = _STOPPED.match(line)
        if m:
            if pending is not None and int(m.group(1), 16) == pending:
                pending = None          # pas exécutée : interruption prise avant
                stopped = int(m.group(1), 16)
            continue
        if line.startswith("Taking exception") or line.startswith("Exception return"):
            if pending is not None:     # l'instruction annoncée a bien été exécutée
                yield Step(pending)
                pending = None
            if line.startswith("Exception return"):
                held_return = True
            continue
        if "tailchaining to pending exception" in line:
            held_return, tailchain = False, True
            continue
        m = _TAKING.search(line)
        if m:
            number = int(m.group(1))
            continue
        m = _NEWPC.search(line)
        if m and number is not None:
            if held_return:             # retour puis nouvelle exception sans instruction
                yield ExcReturn()
                held_return = False
            yield ExcEntry(number, int(m.group(1), 16) & ~1, tailchain,
                           ret=None if tailchain else stopped)
            number, tailchain, stopped = None, False, None
    # Fin du journal : une instruction annoncée sans « Stopped execution » derrière a été
    # exécutée (ex. celle qui précède un point d'arrêt, dont l'instruction n'est jamais
    # annoncée).
    if pending is not None:
        yield Step(pending)
    if held_return:
        yield ExcReturn()


QEMU_LOG_ARGS = ["-accel", "tcg,one-insn-per-tb=on"]
LOG_ITEMS = "exec,nochain,int"


def record(elf: str, seconds: float, path: str, machine: str = "mps2-an385",
           qemu: str = "qemu-system-arm", instructions: int | None = None) -> None:
    """Exécute le firmware sous QEMU pendant `seconds` en journalisant chaque instruction.
    Le temps n'est compté qu'une fois le journal réellement alimenté : sur une machine
    chargée, QEMU peut mettre du temps à démarrer.

    Avec `instructions`, l'enregistrement s'arrête dès que le journal en compte au moins
    autant (`seconds` n'est plus qu'un plafond). Pour une durée donnée, une machine chargée
    exécute deux à trois fois moins d'instructions, et le firmware avance d'autant moins
    (ticks, appels) : un nombre d'instructions donne des chemins comparables d'une fois sur
    l'autre ; `head()` les ramène ensuite exactement à ce nombre."""
    import os
    import subprocess
    import time
    if os.path.exists(path):
        os.remove(path)
    cmd = [qemu, "-M", machine, "-nographic", "-monitor", "none", "-serial", "none",
           "-kernel", elf, *QEMU_LOG_ARGS, "-d", LOG_ITEMS, "-D", path]
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and proc.poll() is None:
            if os.path.exists(path) and os.path.getsize(path) > 0:
                break
            time.sleep(0.02)
        if instructions is None:
            try:
                proc.wait(timeout=seconds)
            except subprocess.TimeoutExpired:
                pass
        elif os.path.exists(path):
            # +2 % : quelques lignes « Trace » sont des instructions non exécutées (préemptées)
            _wait_instructions(proc, path, instructions + instructions // 50,
                               time.monotonic() + seconds)
    finally:
        if proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=5)


def _wait_instructions(proc, path: str, count: int, deadline: float) -> None:
    """Attend que le journal compte `count` lignes « Trace » (ou la fin de QEMU, ou l'échéance)."""
    import time
    marker = b"\nTrace "
    seen, tail = 0, b""
    with open(path, "rb") as fh:
        while seen < count and time.monotonic() < deadline and proc.poll() is None:
            chunk = fh.read(1 << 20)
            if not chunk:
                time.sleep(0.02)
                continue
            data = tail + chunk
            seen += data.count(marker)
            tail = data[-(len(marker) - 1):]     # trop court pour contenir un marqueur déjà compté


def head(events: Iterable[Event], instructions: int) -> list[Event]:
    """Les événements jusqu'à la `instructions`-ième instruction exécutée, incluse, et rien
    après : le chemin coupé finit toujours par une instruction. Une entrée ou un retour
    d'exception qui la suit appartient à la suite (sa reprise n'est pas dans le chemin) ;
    le garder rendait la fin du chemin aléatoire, selon l'endroit où tombait une interruption."""
    out: list[Event] = []
    if instructions <= 0:
        return out
    n = 0
    for e in events:
        out.append(e)
        if isinstance(e, Step):
            n += 1
            if n == instructions:
                break
    return out


def parse_file(path: str) -> list[Event]:
    with open(path, encoding="latin-1") as fh:
        return list(parse(fh))


def steps(events: Iterable[Event]) -> list[int]:
    return [e.addr for e in events if isinstance(e, Step)]
