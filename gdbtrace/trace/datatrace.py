"""Trace de données du DWT (Var.TRACE, Var.DRAW) : la valeur d'une variable à chaque accès,
cible en marche, sans l'arrêter ni instrumenter le code.

Un comparateur du DWT surveille l'adresse de la variable ; à chaque écriture (ou lecture)
qui la touche, il émet par l'ITM un paquet « valeur de donnée » (et, au choix, le PC de
l'instruction qui a fait l'accès), horodaté si l'horodatage ITM est actif. Ces paquets
voyagent par le SWO ou le port de trace, avec le reste de l'ITM.

DWT_FUNCTIONn (ARMv7-M) : 0xD données en écriture, 0xC en lecture, 0x2 les deux ; 0xF, 0xE,
0x3 ajoutent le PC. DWT_MASKn couvre la variable (2^MASK octets alignés). ARMv8-M (M33, M55 ;
DWT_DEVARCH = 0x1A02) : plus de MASK, FUNCTION = MATCH (13 « adresse avec valeur » en
écriture, 14 en lecture, 12 les deux) | ACTION (2 : valeur ; 3 : valeur et PC) << 4 |
DATAVSIZE (taille de la variable) << 10 ; mêmes paquets ITM. Il faut
DEMCR.TRCENA et ITM_TCR.DWTENA (programmés par la configuration SWO), et que la variable soit
globale ou statique (une adresse fixe). Les comparateurs sont partagés avec le filtre de
l'ETMv3 et les watchpoints de gdb : `TraceManager` les répartit.
"""
from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field

from ..core.live_vars import LeafLayout, classify, decode, root_identifier
from .itm import DataTrace, LocalTimestamp, Packet

DEMCR = 0xE000EDFC
DWT_CTRL = 0xE0001000
DWT_COMP0 = 0xE0001020              # DWT_COMPn : 0x20 + 16n ; MASK +4 ; FUNCTION +8
FUNCTION = {("write", False): 0xD, ("read", False): 0xC, ("rw", False): 0x2,
            ("write", True): 0xF, ("read", True): 0xE, ("rw", True): 0x3}
#: ARMv8-M : MATCH « adresse de donnée avec valeur » selon l'accès
MATCH_V8 = {"write": 13, "read": 14, "rw": 12}
DWT_DEVARCH = 0xE0001FBC
MAX_SAMPLES = 100_000


def dwt_is_v8m(read_u32) -> bool:
    """DWT de l'ARMv8-M (DWT_DEVARCH.ARCHID = 0x1A02 ; absent, lu à 0, en ARMv7-M)."""
    try:
        return read_u32(DWT_DEVARCH) & 0xFFFF == 0x1A02
    except Exception:  # noqa: BLE001
        return False


@dataclass
class Sample:
    t: float            # secondes depuis le premier échantillon (horodatage ITM, sinon l'hôte)
    raw: int            # valeur brute du paquet
    size: int           # taille de l'accès (octets)
    kind: str           # write ou read
    pc: int | None = None


@dataclass
class TracedVar:
    expr: str
    addr: int
    size: int
    comparator: int
    access: str = "write"           # write, read, rw ; « sample » : échantillonnée (core/snooper.py)
    pc: bool = False                # PC de l'instruction qui accède
    layout: LeafLayout = field(default_factory=LeafLayout)
    samples: deque = field(default_factory=lambda: deque(maxlen=MAX_SAMPLES))
    count: int = 0

    def text(self, s: Sample, fmt: str = "natural") -> str:
        """Valeur d'un échantillon, au format de la variable (un accès partiel : en hexa)."""
        if s.size != self.size or not self.layout.kind:
            return f"{s.raw:#0{2 + 2 * s.size}x}"
        return decode(self.layout, s.raw.to_bytes(s.size, "little"), fmt)

    def number(self, s: Sample) -> float | None:
        """Valeur numérique (graphe) : entiers, booléens, caractères, énumérations, flottants."""
        if s.size != self.size:
            return None
        k = self.layout.kind
        if k == "float":
            import struct
            return struct.unpack("<f" if s.size == 4 else "<d", s.raw.to_bytes(s.size, "little"))[0]
        return float(int.from_bytes(s.raw.to_bytes(s.size, "little"), "little",
                                    signed=k in ("int", "schar")))


def mask_for(addr: int, size: int) -> int:
    """MASK du comparateur : bloc aligné de 2^k octets qui contient [addr, addr + size[."""
    k = 0
    while (addr >> k) != ((addr + size - 1) >> k):
        k += 1
    return k


class DataTraceSink:
    """Abonné ITM : paquets « valeur de donnée » du DWT → échantillons par variable."""

    def __init__(self, cpu_hz: int = 25_000_000):
        self.cpu_hz = cpu_hz
        self.vars: dict[int, TracedVar] = {}        # comparateur → variable
        self.lost = 0                               # paquets d'un comparateur inconnu
        self._lock = threading.Lock()
        self._pc: dict[int, int] = {}               # dernier PC émis par comparateur
        self._t0_ts: int | None = None
        self._t0_host: float | None = None
        self._lts_seen = False                      # le flux porte des horodatages ITM
        self.generation = 0

    def add(self, var: TracedVar) -> None:
        with self._lock:
            self.vars[var.comparator] = var
            self.generation += 1

    def remove(self, comparator: int) -> TracedVar | None:
        with self._lock:
            self.generation += 1
            return self.vars.pop(comparator, None)

    def reset(self) -> None:
        with self._lock:
            for v in self.vars.values():
                v.samples.clear()
                v.count = 0
            self._t0_ts = self._t0_host = None
            self._lts_seen = False
            self.generation += 1

    def host_time(self) -> float:
        """Secondes de l'horloge de l'hôte depuis la même origine (variables échantillonnées)."""
        with self._lock:
            now = time.monotonic()
            if self._t0_host is None:
                self._t0_host = now
            return now - self._t0_host

    def add_sample(self, var: TracedVar, sample: Sample) -> None:
        """Un échantillon venu d'ailleurs que l'ITM (SNOOPer)."""
        with self._lock:
            if self.vars.get(var.comparator) is var:
                var.samples.append(sample)
                var.count += 1
                self.generation += 1

    def _time(self, ts: int) -> float:
        """Secondes depuis le premier échantillon : horodatage ITM (cycles du cœur, cumulés
        depuis le début du flux) dès qu'il y en a, sinon heure de réception sur l'hôte."""
        if self._t0_ts is None:
            self._t0_ts = ts
        if self._lts_seen:
            return (ts - self._t0_ts) / self.cpu_hz
        now = time.monotonic()
        if self._t0_host is None:
            self._t0_host = now
        return now - self._t0_host

    def __call__(self, pkts: list[Packet]) -> None:
        with self._lock:
            for p in pkts:
                if isinstance(p, LocalTimestamp):
                    self._lts_seen = True
                    continue
                if not isinstance(p, DataTrace):
                    continue
                if p.kind == "pc":
                    self._pc[p.comparator] = p.value
                    continue
                if p.kind not in ("read", "write"):
                    continue
                v = self.vars.get(p.comparator)
                if v is None:
                    self.lost += 1
                    continue
                v.samples.append(Sample(self._time(p.ts), p.value, p.size, p.kind,
                                        self._pc.pop(p.comparator, None) if v.pc else None))
                v.count += 1
                self.generation += 1


def variable_layout(session, expr: str, sizes: tuple = (1, 2, 4),
                    why: str = "un paquet du DWT porte 1, 2 ou 4 octets") -> tuple[int, int, LeafLayout]:
    """(adresse, taille, disposition) d'une variable globale ou statique (cible arrêtée) ;
    `sizes` : tailles acceptées."""
    try:
        local_names = {v.get("name") for v in session.locals()}
    except Exception:  # noqa: BLE001 - pas de frame : aucune locale
        local_names = set()
    if root_identifier(expr) in local_names:
        raise ValueError(f"« {expr} » est une variable locale : son adresse change d'un appel à "
                         "l'autre, elle ne se suit pas cible en marche")
    addr = session.address_of_expr(expr)
    size = session.sizeof(expr)
    if size not in sizes:
        raise ValueError(f"« {expr} » : {size} octets ; {why} — choisir un champ ou un élément "
                         "scalaire")
    kind = classify(session.ptype(expr), size)
    layout = LeafLayout(addr=addr, size=size, kind=kind[0], enum_names=kind[1], live=True) \
        if kind else LeafLayout(addr=addr, size=size)
    return addr, size, layout


def comparator_writes(var: TracedVar, v8m: bool = False) -> list[tuple[str, int, int]]:
    """[(nom, adresse, valeur)] : programmation du comparateur de `var` (`v8m` : DWT de
    l'ARMv8-M)."""
    base = DWT_COMP0 + 16 * var.comparator
    n = var.comparator
    if v8m:
        if var.addr % var.size:
            raise ValueError(f"« {var.expr} » : adresse {var.addr:#x} non alignée sur sa taille, "
                             "le DWT de l'ARMv8-M ne la couvre pas")
        function = MATCH_V8[var.access] | (3 if var.pc else 2) << 4 | {1: 0, 2: 1, 4: 2}[var.size] << 10
        return [(f"DWT_FUNCTION{n}.off", base + 8, 0),
                (f"DWT_COMP{n} ({var.expr})", base, var.addr),
                (f"DWT_FUNCTION{n}", base + 8, function)]
    return [(f"DWT_FUNCTION{n}.off", base + 8, 0),
            (f"DWT_COMP{n} ({var.expr})", base, var.addr),
            (f"DWT_MASK{n}", base + 4, mask_for(var.addr, var.size)),
            (f"DWT_FUNCTION{n}", base + 8, FUNCTION[(var.access, var.pc)])]
