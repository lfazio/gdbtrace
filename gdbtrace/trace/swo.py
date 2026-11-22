"""Capture SWO : sources d'octets, session de trace et consommateurs de paquets.

    source (TCP / fichier) ─► TraceSession ─► [déformateur TPIU] ─► ItmDecoder ─► abonnés
                                                                     texte ITM, profil, exceptions

Sources TCP courantes (flux brut, formateur TPIU désactivé) :
- OpenOCD : `<cible>.tpiu configure -protocol uart -output :PORT ...` puis `enable` ;
- Orbuculum (orbuculum/ORBTrace), J-Link (port SWO du J-Link GDB Server).
"""
from __future__ import annotations

import socket
import threading
from dataclasses import dataclass
from typing import Callable

from .itm import ExceptionTrace, ItmDecoder, Overflow, Packet, PcSample, exception_name
from .tpiu import TpiuDeformatter

PacketSink = Callable[[list[Packet]], None]


class TraceSession:
    """Décode les octets reçus et distribue les paquets aux abonnés (thread de la source)."""

    def __init__(self, formatter: bool = False, itm_stream: int = 1, etm_stream: int = 2,
                 etm_sink: Callable[[bytes], object] | None = None):
        """Avec le formateur TPIU (port parallèle, ORBTrace…), les flux sont séparés par ID :
        ITM → décodeur ITM, ETM → `etm_sink(octets)` (trace d'instructions)."""
        self.deformatter = TpiuDeformatter() if formatter else None
        self.itm_stream = itm_stream
        self.etm_stream = etm_stream
        self.etm_sink = etm_sink
        self.etm_bytes = 0
        self.decoder = ItmDecoder()
        self.sinks: list[PacketSink] = []
        self.bytes = 0
        self.overflows = 0
        self._lock = threading.Lock()

    def add_sink(self, sink: PacketSink) -> None:
        self.sinks.append(sink)

    def remove_sink(self, sink: PacketSink) -> None:
        if sink in self.sinks:
            self.sinks.remove(sink)

    def feed(self, data: bytes) -> list[Packet]:
        with self._lock:
            self.bytes += len(data)
            if self.deformatter is not None:
                chunks = self.deformatter.feed(data)
                data = b"".join(c for sid, c in chunks if sid == self.itm_stream)
                etm = b"".join(c for sid, c in chunks if sid == self.etm_stream)
                if etm:
                    self.etm_bytes += len(etm)
                    if self.etm_sink is not None:
                        self.etm_sink(etm)
            pkts = self.decoder.feed(data)
            self.overflows += sum(isinstance(p, Overflow) for p in pkts)
        if pkts:
            for sink in list(self.sinks):
                sink(pkts)
        return pkts

    @property
    def stats(self) -> dict[str, int]:
        return {"octets": self.bytes, "paquets": self.decoder.packets,
                "erreurs": self.decoder.errors, "overflows": self.overflows,
                "octets ETM": self.etm_bytes}


# ====================================================================== sources
class SwoSource:
    """Thread qui pousse des octets dans une TraceSession."""
    description = "SWO"

    def __init__(self):
        self.session: TraceSession | None = None
        self.error: str | None = None
        self.connected = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self, session: TraceSession) -> None:
        self.session = session
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="swo-source", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._close()
        if self._thread is not None:
            self._thread.join(timeout=2)
        self._thread = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _run(self) -> None:
        raise NotImplementedError

    def _close(self) -> None:
        pass


class TcpSwoSource(SwoSource):
    """Flux SWO servi en TCP (OpenOCD -output :port, Orbuculum, J-Link). Reconnexion auto."""

    def __init__(self, host: str, port: int, retry: float = 1.0):
        super().__init__()
        self.host, self.port, self.retry = host, port, retry
        self.description = f"SWO TCP {host}:{port}"
        self._sock: socket.socket | None = None

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._sock = socket.create_connection((self.host, self.port), timeout=2)
                self._sock.settimeout(0.2)
                self.connected, self.error = True, None
                while not self._stop.is_set():
                    try:
                        data = self._sock.recv(65536)
                    except socket.timeout:
                        continue
                    if not data:
                        raise ConnectionError("connexion fermée par le serveur SWO")
                    self.session.feed(data)
            except ConnectionRefusedError:
                # OpenOCD n'ouvre ce port qu'à `tpiu enable` : connexion faite trop tôt
                self.error = (f"rien n'écoute sur {self.host}:{self.port} (OpenOCD : « Configurer "
                              f"la cible… » ou --swo-config ouvre ce port ; nouvel essai chaque "
                              f"{self.retry:g} s)")
            except OSError as exc:
                self.error = str(exc)
            finally:
                self.connected = False
                self._close()
            self._stop.wait(self.retry)

    def _close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
        self._sock = None


class FileSwoSource(SwoSource):
    """Rejoue une capture SWO brute (fichier binaire) ; `done` quand tout est lu."""

    def __init__(self, path: str, chunk: int = 4096):
        super().__init__()
        self.path, self.chunk = path, chunk
        self.description = f"fichier {path}"
        self.done = threading.Event()

    def _run(self) -> None:
        try:
            with open(self.path, "rb") as fh:
                self.connected = True
                while not self._stop.is_set():
                    data = fh.read(self.chunk)
                    if not data:
                        break
                    self.session.feed(data)
        except OSError as exc:
            self.error = str(exc)
        finally:
            self.connected = False
            self.done.set()


# ====================================================================== consommateurs
def profile_sink(profile) -> PacketSink:
    """Échantillons de PC du DWT (SWO) → Profile ; None = sommeil."""
    def sink(pkts: list[Packet]) -> None:
        for p in pkts:
            if isinstance(p, PcSample):
                profile.add(p.pc)
    return sink


@dataclass
class ExceptionRow:
    number: int
    name: str
    entries: int
    total_time: int           # unités de timestamp ITM (0 si horodatage désactivé)
    max_time: int


class ExceptionStats:
    """Trace des exceptions : nombre d'entrées et temps passé (si horodatage ITM actif)."""

    def __init__(self):
        self._lock = threading.Lock()
        self.reset()

    def reset(self) -> None:
        with self._lock:
            self.entries: dict[int, int] = {}
            self.total: dict[int, int] = {}
            self.max: dict[int, int] = {}
            self._stack: list[tuple[int, int]] = []    # (numéro, ts d'entrée), imbrication

    def __call__(self, pkts: list[Packet]) -> None:
        with self._lock:
            for p in pkts:
                if not isinstance(p, ExceptionTrace) or p.number == 0:
                    continue
                if p.event == "entry":
                    self.entries[p.number] = self.entries.get(p.number, 0) + 1
                    self._stack.append((p.number, p.ts))
                elif p.event == "exit":
                    # dépile jusqu'à l'exception qui sort (paquets perdus tolérés)
                    while self._stack:
                        num, ts0 = self._stack.pop()
                        if num == p.number:
                            dt = p.ts - ts0
                            self.total[num] = self.total.get(num, 0) + dt
                            self.max[num] = max(self.max.get(num, 0), dt)
                            break

    def rows(self) -> list[ExceptionRow]:
        with self._lock:
            return sorted((ExceptionRow(n, exception_name(n), c, self.total.get(n, 0),
                                        self.max.get(n, 0)) for n, c in self.entries.items()),
                          key=lambda r: -r.entries)
