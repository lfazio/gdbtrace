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


@dataclass(frozen=True)
class ExcReturn:
    """Retour d'exception : l'exécution reprend à l'adresse de la Step suivante."""
    cycles: int = field(default=0, compare=False)


Event = Step | ExcEntry | ExcReturn


def parse(lines: Iterable[str]) -> Iterator[Event]:
    pending: int | None = None          # dernière instruction annoncée, pas encore confirmée
    number: int | None = None
    tailchain = False
    held_return = False                 # ExcReturn retenu : annulé si enchaînement
    for line in lines:
        m = _TRACE.match(line)
        if m:
            if pending is not None:
                yield Step(pending)
            if held_return:
                yield ExcReturn()
                held_return = False
            pending = int(m.group(1), 16)
            continue
        m = _STOPPED.match(line)
        if m:
            if pending is not None and int(m.group(1), 16) == pending:
                pending = None          # pas exécutée : interruption prise avant
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
            yield ExcEntry(number, int(m.group(1), 16) & ~1, tailchain)
            number, tailchain = None, False
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
           qemu: str = "qemu-system-arm") -> None:
    """Exécute le firmware sous QEMU pendant `seconds` en journalisant chaque instruction.
    Le temps n'est compté qu'une fois le journal réellement alimenté : sur une machine
    chargée, QEMU peut mettre du temps à démarrer."""
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
        try:
            proc.wait(timeout=seconds)
        except subprocess.TimeoutExpired:
            pass
    finally:
        if proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=5)


def parse_file(path: str) -> list[Event]:
    with open(path, encoding="latin-1") as fh:
        return list(parse(fh))


def steps(events: Iterable[Event]) -> list[int]:
    return [e.addr for e in events if isinstance(e, Step)]
