"""Trace sur disque : enregistrement (`--trace-record FICHIER`, clé trace.trace-record) et
relecture (`--trace-file FICHIER`), pour les captures qui dépassent la mémoire.

- `FICHIER` : le flux brut tel qu'il arrive, écrit en continu — octets SWO, trames TPIU
  (SWO formaté, port parallèle), ou flux ETM sorti de l'ETR (STM32N6). Les outils qui lisent
  une capture brute (OpenCSD, `--swo-file`) le lisent tel quel.
- `FICHIER.idx` : index, une ligne JSON par entrée — en-tête (source, formateur, version de
  l'ETM, ELF, date), positions horodatées par l'hôte (octet, secondes) tous les 256 Ko ou
  toutes les 0,5 s, points de synchronisation du flux (trame TPIU, A-sync de l'ETM), trous
  (octets perdus avant l'écriture), fin.
- `FICHIER.points` : points de reprise du décodage, écrits à la relecture — octet, nombre
  d'événements décodés avant lui, état des décodeurs (déformateur, paquets, flot, sans leurs
  caches) — tous les `POINT_EVENTS` événements. La relecture calcule couverture et timing au
  fil du décodage (`EtmTrace.stream_timing_on`) sans garder l'historique ; une page de
  Trace.List se décode depuis le dernier point qui la précède (`RecordedTrace.rows`).
"""
from __future__ import annotations

import base64
import copy
import io
import json
import os
import pickle
import threading
import time
import types
import zlib
from collections import deque

from .tpiu import TpiuDeformatter

FORMAT = 1
INDEX_BYTES = 256 * 1024
INDEX_SECONDS = 0.5
POINT_EVENTS = 1_000_000
CHUNK = 4096                      # lecture par morceaux : un point de reprise entre deux
#: motifs de synchronisation : trame TPIU complète, A-sync de l'ETMv4 puis de l'ETMv3
SYNC_PATTERNS = (("tpiu", b"\xff\xff\xff\x7f"), ("etm", b"\x00" * 11 + b"\x80"),
                 ("etm", b"\x00" * 5 + b"\x80"))
ITM_STREAM, ETM_STREAM = 1, 2


def index_path(path: str) -> str:
    return path + ".idx"


def points_path(path: str) -> str:
    return path + ".points"


# ====================================================================== enregistrement
class TraceRecorder:
    """Écrit un flux de trace et son index, depuis le fil de la capture (`write`, `gap`)."""

    def __init__(self, path: str, source: str, formatter: bool = False, etm: int | None = None,
                 elf: str | None = None, cycle_accurate: bool = False):
        self.path = path
        os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
        self._raw = open(path, "wb")
        self._idx = open(index_path(path), "w", encoding="utf-8")
        self._lock = threading.Lock()
        self.offset = 0
        self.closed = False
        self._t0 = time.monotonic()
        self._next_o, self._next_t = 0, 0.0         # la première écriture date le début
        self._sync_block = -1
        self.header = {"gdbtrace-trace": FORMAT, "source": source, "formateur": bool(formatter),
                       "etm": etm, "cycle-accurate": bool(cycle_accurate),
                       "elf": os.path.abspath(elf) if elf else None,
                       "début": time.strftime("%Y-%m-%dT%H:%M:%S")}
        self._line(self.header)
        stale = points_path(path)
        if os.path.exists(stale):                # ceux d'une ancienne capture du même nom
            os.remove(stale)

    def _line(self, entry: dict) -> None:
        self._idx.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def write(self, data: bytes) -> None:
        if not data:
            return
        with self._lock:
            if self.closed:
                return
            start = self.offset
            self._raw.write(data)
            self.offset += len(data)
            block = start // INDEX_BYTES
            if block != self._sync_block:        # un point de synchronisation par bloc
                for kind, pat in SYNC_PATTERNS:
                    i = data.find(pat)
                    if i >= 0:
                        self._line({"o": start + i, "sync": kind})
                        self._sync_block = block
                        break
            t = time.monotonic() - self._t0
            if self.offset >= self._next_o or t >= self._next_t:
                self._line({"o": self.offset, "t": round(t, 6)})
                self._next_o, self._next_t = self.offset + INDEX_BYTES, t + INDEX_SECONDS
                self._raw.flush()
                self._idx.flush()

    def gap(self) -> None:
        """Octets perdus avant l'écriture (SWO débordé, tampon de l'ETR plein)."""
        with self._lock:
            if not self.closed:
                self._line({"o": self.offset, "trou": True})

    def close(self) -> None:
        with self._lock:
            if self.closed:
                return
            self.closed = True
            self._line({"o": self.offset, "t": round(time.monotonic() - self._t0, 6), "fin": True})
            self._raw.close()
            self._idx.close()


def read_index(path: str) -> dict:
    """En-tête et entrées de l'index ; sans index, une capture TPIU formatée (l'ancien
    `--trace-file`)."""
    out = {"header": {"gdbtrace-trace": 0, "source": "tpiu", "formateur": True, "etm": None},
           "times": [], "syncs": [], "gaps": [], "end": None}
    try:
        fh = open(index_path(path), encoding="utf-8")
    except OSError:
        return out
    with fh:
        for k, line in enumerate(fh):
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue                         # dernière ligne coupée (capture interrompue)
            if k == 0 and "gdbtrace-trace" in e:
                out["header"] = e
            elif e.get("fin"):
                out["end"] = (e["o"], e.get("t"))
            elif e.get("trou"):
                out["gaps"].append(e["o"])
            elif "sync" in e:
                out["syncs"].append((e["o"], e["sync"]))
            elif "t" in e:
                out["times"].append((e["o"], e["t"]))
    return out


# ====================================================================== état des décodeurs
class _Pickler(pickle.Pickler):
    def __init__(self, fh, refs: dict):
        super().__init__(fh, protocol=4)
        self.refs = refs

    def persistent_id(self, obj):
        tag = self.refs.get(id(obj))
        if tag is not None:
            return tag
        if isinstance(obj, (types.MethodType, types.FunctionType, types.BuiltinFunctionType)):
            return "rien"                        # rappels (scorer du déformateur) : retirés
        return None


class _Unpickler(pickle.Unpickler):
    def __init__(self, fh, objs: dict):
        super().__init__(fh)
        self.objs = objs

    def persistent_load(self, pid):
        return self.objs.get(pid)


def save_state(deformatter, packets, flow, image, symbols) -> str:
    """État des décodeurs (texte base64), sans l'image du code ni les caches du flot."""
    if hasattr(flow, "_blocks"):                 # ETMv4 : blocs et comptes, recalculés
        flow = copy.copy(flow)
        flow._blocks, flow._hits, flow._walked = {}, {}, {}
    buf = io.BytesIO()
    _Pickler(buf, {id(image): "image", id(symbols): "symboles"}).dump((deformatter, packets, flow))
    return base64.b64encode(zlib.compress(buf.getvalue())).decode("ascii")


def load_state(text: str, image, symbols) -> tuple:
    raw = zlib.decompress(base64.b64decode(text))
    return _Unpickler(io.BytesIO(raw), {"image": image, "symboles": symbols, "rien": None}).load()


# ====================================================================== relecture
class _NoCoverage:
    def add(self, events) -> None:
        pass

    def add_counts(self, *args) -> None:
        pass


class RecordedTrace:
    """Une capture enregistrée : relecture complète (`replay`, `scan`), pages de Trace.List
    (`events`, `rows`). `image` / `symbols` : ceux de l'ELF de la capture."""

    def __init__(self, path: str, image, symbols=None, version: int | None = None,
                 cycle_accurate: bool | None = None, max_spec: int = 1, commit_opt1: bool = False,
                 heal_ranges=None):
        self.path = path
        self.index = read_index(path)
        h = self.index["header"]
        self.kind = h.get("source", "tpiu")
        self.formatter = bool(h.get("formateur", True))
        self.version = version or h.get("etm") or 3
        self.cycle_accurate = h.get("cycle-accurate", False) if cycle_accurate is None else cycle_accurate
        self.image, self.symbols = image, symbols
        self.max_spec, self.commit_opt1, self.heal_ranges = max_spec, commit_opt1, heal_ranges
        self.size = os.path.getsize(path)
        self._points: list | None = None
        self.events_total: int | None = None    # connu après une relecture complète

    @property
    def has_etm(self) -> bool:
        return self.kind == "etm" or self.formatter

    def duration(self) -> float | None:
        """Durée de la capture (horloge de l'hôte), si l'index la donne."""
        end = self.index["end"] or (self.index["times"][-1] if self.index["times"] else None)
        return end[1] if end else None

    def time_at(self, offset: int) -> float | None:
        """Instant de l'hôte (s depuis le début) où l'octet `offset` est arrivé (interpolé)."""
        pts = [(0, 0.0)] + list(self.index["times"]) + ([self.index["end"]] if self.index["end"] else [])
        prev = pts[0]
        for o, t in pts[1:]:
            if o >= offset and t is not None:
                if o == prev[0]:
                    return t
                return prev[1] + (t - prev[1]) * (offset - prev[0]) / (o - prev[0])
            prev = (o, t)
        return prev[1] if len(pts) > 1 else None

    def _etm(self, cls=None, history: int = 1000):
        from .etm.trace import EtmTrace
        etm = (cls or EtmTrace)(self.image, self.symbols, history=history,
                                cycle_accurate=self.cycle_accurate, version=self.version,
                                max_spec=self.max_spec, commit_opt1=self.commit_opt1)
        if self.heal_ranges:
            etm.set_heal_ranges(self.heal_ranges)
        return etm

    def _deformatter(self) -> TpiuDeformatter | None:
        if self.kind != "etm" and self.formatter:
            return TpiuDeformatter(ids={ITM_STREAM, ETM_STREAM})
        return None

    # ------------------------------------------------------------------ relecture complète
    def replay(self, etm, session=None, points: bool = True, chunk: int = CHUNK, stop=None,
               pause=None, progress=None) -> None:
        """Relit tout le fichier dans `etm` (EtmTrace, ou None : ITM seul) et `session`
        (TraceSession : ITM, déformateur ; None : une session sans abonnés). Synchrone : les
        points de reprise tombent sur l'octet exact où les décodeurs en sont. `stop` : Event
        qui interrompt ; `pause()` : appelé entre deux morceaux (laisser la main au GUI)."""
        from .swo import TraceSession
        if self.kind != "etm" and session is None:
            session = TraceSession(formatter=self.formatter)
        if session is not None and etm is not None and self.kind != "etm":
            session.etm_sink = etm.feed
        gaps = deque(sorted(self.index["gaps"]))
        tmp = points_path(self.path) + ".tmp"
        out = open(tmp, "w", encoding="utf-8") if points and etm is not None else None
        if out is not None:
            out.write(json.dumps({"points": FORMAT, "etm": self.version, "elf": getattr(self.image, "path", None),
                                  "événements": POINT_EVENTS}) + "\n")
        last = etm.events_total if etm is not None else 0
        offset = 0
        try:
            with open(self.path, "rb") as fh:
                while stop is None or not stop.is_set():
                    while gaps and gaps[0] <= offset:
                        gaps.popleft()
                        if etm is not None:
                            etm.discontinuity()
                    n = chunk if not gaps else max(1, min(chunk, gaps[0] - offset))
                    data = fh.read(n)
                    if not data:
                        break
                    if self.kind == "etm":
                        if etm is not None:
                            etm.feed(data)
                    else:
                        session.feed(data)
                    offset += len(data)
                    if out is not None and etm.events_total - last >= POINT_EVENTS:
                        state = save_state(session.deformatter if session is not None else None,
                                           etm.packets, etm.flow, self.image, self.symbols)
                        out.write(json.dumps({"o": offset, "n": etm.events_total, "s": state}) + "\n")
                        last = etm.events_total
                    if progress is not None:
                        progress(offset, self.size)
                    if pause is not None:
                        pause()
            if etm is not None:
                etm.flush()
                self.events_total = etm.events_total
        finally:
            if out is not None:
                out.close()
                if stop is None or not stop.is_set():
                    os.replace(tmp, points_path(self.path))
                    self._points = None
                else:
                    os.remove(tmp)

    def scan(self, timing: bool = True, progress=None):
        """Relecture complète hors session : (EtmTrace avec couverture et timing de toute la
        trace, son historique réduit aux derniers événements) ; écrit les points de reprise."""
        etm = self._etm()
        if timing:
            etm.stream_timing_on(self.symbols)
        self.replay(etm, progress=progress)
        return etm

    # ------------------------------------------------------------------ pages
    def points(self) -> list[tuple[int, int, str]]:
        if self._points is None:
            pts: list = []
            try:
                with open(points_path(self.path), encoding="utf-8") as fh:
                    for k, line in enumerate(fh):
                        e = json.loads(line)
                        if k == 0:
                            if e.get("etm") != self.version:
                                break           # points d'un autre décodage : inutilisables
                            continue
                        pts.append((e["o"], e["n"], e["s"]))
            except (OSError, json.JSONDecodeError):
                pts = []
            self._points = pts
        return self._points

    def events(self, start: int, count: int, chunk: int = 16 * 1024) -> list:
        """Événements [start, start + count) de la trace, décodés depuis le dernier point de
        reprise qui les précède (sans point : depuis le début du fichier)."""
        from .etm.trace import EtmTrace
        lo, hi = start, start + count

        class Window(EtmTrace):
            def _account(self, evs):
                base = self.events_total
                self.events_total += len(evs)
                if base + len(evs) > lo and base < hi:
                    got.extend(evs[max(0, lo - base):hi - base])

        got: list = []
        base_o, base_n, state = 0, 0, None
        for o, n, s in self.points():
            if n <= start:
                base_o, base_n, state = o, n, s
        etm = self._etm(Window, history=1)
        etm.cov = _NoCoverage()
        deformatter = self._deformatter()
        if state is not None:
            deformatter, etm.packets, etm.flow = load_state(state, self.image, self.symbols)
            if self.kind != "etm" and deformatter is None:
                deformatter = self._deformatter()
        etm.events_total = base_n
        gaps = deque(g for g in sorted(self.index["gaps"]) if g > base_o)
        with open(self.path, "rb") as fh:
            fh.seek(base_o)
            offset = base_o
            while etm.events_total < hi:
                while gaps and gaps[0] <= offset:
                    gaps.popleft()
                    etm.discontinuity()
                n = chunk if not gaps else max(1, min(chunk, gaps[0] - offset))
                data = fh.read(n)
                if not data:
                    etm.flush()
                    break
                offset += len(data)
                if self.kind == "etm":
                    etm.feed(data)
                elif deformatter is not None:
                    e = b"".join(c for sid, c in deformatter.feed(data) if sid == ETM_STREAM)
                    if e:
                        etm.feed(e)
        return got

    def rows(self, start: int, count: int):
        """Lignes de Trace.List [start, start + count), index absolus (0 : le premier
        événement de la capture)."""
        from .etm.listing import listing_rows
        evs = self.events(start, count)
        rows = listing_rows(evs, self.image, self.symbols)
        for r in rows:
            r.index = start + r.index + len(evs)
        return rows
