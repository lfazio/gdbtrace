"""Relais RSP (protocole gdb-remote) entre gdb et un serveur gdb qui n'a pas de port latéral.

OpenOCD lit la mémoire cible en marche par son port TCL et QEMU par QMP. ST-LINK_gdbserver
n'a ni l'un ni l'autre, mais il répond aux paquets `m` / `M` pendant que la cible tourne :
c'est gdb, en all-stop, qui n'en envoie pas. gdb se connecte donc au relais, qui transmet
tout au serveur et y glisse ses propres requêtes (`RelayMemory`, le canal « en direct ») :

- cible en marche : gdb n'attend que le paquet d'arrêt, tout autre paquet du serveur est la
  réponse du relais ;
- cible arrêtée : seulement quand gdb n'attend pas de réponse ; ce que gdb envoie pendant
  une requête du relais est retenu jusqu'à sa réponse.

Le relais fait aussi ce que gdb ne sait pas faire avec ce serveur :

- reset système (AIRCR) avec reconnexion (`reset_system`) : après SYSRESETREQ, ST-LINK_gdbserver
  perd la cible (« Target is not responding ») et n'accepte plus de reprise ; une nouvelle
  connexion la retrouve arrêtée. Le relais se détache, se reconnecte et rejoue la poignée de
  main sans que gdb perde sa connexion ;
- les paquets « O » (texte du serveur) arrivés hors d'une marche ou d'une commande monitor
  sont retirés : gdb les prendrait pour la réponse à sa requête (« Invalid hex digit 79 »).
"""
from __future__ import annotations

import collections
import os
import select
import socket
import threading
import time

from .live import DEMCR, DEMCR_TRCENA, DWT_PCSR, LiveMemory, LiveMemoryError

AIRCR = 0xE000ED0C
AIRCR_SYSRESETREQ = 0x05FA0004
#: paquets de reprise : leur réponse est le paquet d'arrêt, reçu quand la cible s'arrête
RESUME = ("c", "C", "s", "S", "vCont;", "vRun", "vAttach", "bc", "bs")
STOP_REPLIES = "STWX"
#: GDBTRACE_RSP_TRACE=fichier : journal des paquets (débogage du relais)
_TRACE = os.environ.get("GDBTRACE_RSP_TRACE")


def _trace(direction: str, data: bytes) -> None:
    if _TRACE:
        with open(_TRACE, "a") as fh:
            fh.write(f"{time.monotonic():.3f} {direction} {data[:300]!r}\n")


def checksum(payload: bytes) -> bytes:
    return b"%02x" % (sum(payload) & 0xFF)


def frame(payload: str | bytes) -> bytes:
    data = payload.encode() if isinstance(payload, str) else payload
    return b"$" + data + b"#" + checksum(data)


def unrle(payload: bytes) -> bytes:
    """Développe le codage par plages (`X*n` : X répété ord(n) - 29 fois de plus)."""
    if b"*" not in payload:
        return payload
    out = bytearray()
    i = 0
    while i < len(payload):
        c = payload[i]
        if c == 0x2A and out and i + 1 < len(payload):       # '*'
            out += bytes([out[-1]]) * (payload[i + 1] - 29)
            i += 2
            continue
        out.append(c)
        i += 1
    return bytes(out)


def take_token(buf: bytearray) -> bytes | None:
    """Retire de `buf` le premier élément complet : b"+", b"-", b"\\x03" ou un paquet
    `$…#cc` / `%…#cc` ; None s'il est incomplet. Les octets parasites sont ignorés."""
    while buf:
        c = buf[0]
        if c in (0x2B, 0x2D, 0x03):                          # + - ^C
            del buf[0]
            return bytes([c])
        if c in (0x24, 0x25):                                # $ %
            end = buf.find(b"#")
            if end < 0 or len(buf) < end + 3:
                return None
            tok = bytes(buf[:end + 3])
            del buf[:end + 3]
            return tok
        del buf[0]
    return None


def payload_of(tok: bytes) -> bytes:
    return tok[1:-3]


class _Job:
    def __init__(self, packet: bytes):
        self.packet = packet
        self.done = threading.Event()
        self.reply: bytes | None = None
        self.error: str | None = None


class RspRelay:
    """Relais d'une connexion gdb vers le serveur `server` (hôte, port). `port` : port
    d'écoute pour gdb (0 : choisi par le système, lu ensuite dans `self.port`)."""

    def __init__(self, server: tuple[str, int], host: str = "127.0.0.1", port: int = 0,
                 log=None):
        self.server = server
        self.host = host
        self._listen = socket.socket()
        self._listen.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listen.bind((host, port))
        self._listen.listen(1)
        self.port = self._listen.getsockname()[1]
        self.log = log or (lambda _msg: None)
        self._wake_r, self._wake_w = socket.socketpair()
        self._jobs: collections.deque[_Job] = collections.deque()
        self._resets: collections.deque[tuple[_Job, float]] = collections.deque()
        self._lock = threading.Lock()
        self._closed = False
        self._thread: threading.Thread | None = None
        self._reset_state()

    def _reset_state(self) -> None:
        self.noack = False
        self.running = False          # gdb a relancé la cible, paquet d'arrêt pas encore reçu
        self.gdb_waiting = False      # gdb attend la réponse à une requête
        self._noack_asked = False
        self._monitor = False         # commande monitor en cours (qRcmd : paquets O légitimes)
        self._acks: collections.deque[str] = collections.deque()   # « gdb » / « relay »
        self._inflight: _Job | None = None
        #: requêtes de la poignée de main de gdb (jusqu'à sa première lecture de registres),
        #: rejouées après une reconnexion : la disposition du paquet `g` du serveur en dépend
        #: (description de la cible lue ou non)
        self._handshake: list[bytes] = []
        self._handshaking = True
        self.connected = False

    # ------------------------------------------------------------------ cycle de vie
    def start(self) -> None:
        self._thread = threading.Thread(target=self._serve, name="rsp-relay", daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._closed = True
        self._wake()
        for s in (self._listen,):
            try:
                s.close()
            except OSError:
                pass
        if self._thread is not None:
            self._thread.join(timeout=2)
        self._fail_jobs("relais fermé")

    def _wake(self) -> None:
        try:
            self._wake_w.send(b"x")
        except OSError:
            pass

    # ------------------------------------------------------------------ requêtes du relais
    def request(self, packet: str, timeout: float = 3.0) -> bytes:
        """Envoie `packet` au serveur hors de gdb et rend la réponse (sans RLE)."""
        if self._closed:
            raise LiveMemoryError("relais RSP fermé")
        job = _Job(frame(packet))
        with self._lock:
            self._jobs.append(job)
        self._wake()
        if not job.done.wait(timeout):
            with self._lock:
                if job in self._jobs:
                    self._jobs.remove(job)
            raise LiveMemoryError(f"le serveur gdb ne répond pas à {packet[:24]}")
        if job.error:
            raise LiveMemoryError(job.error)
        return unrle(job.reply or b"")

    def reset_system(self, settle: float = 0.4, timeout: float = 10.0) -> None:
        """Reset système par AIRCR.SYSRESETREQ, cible arrêtée (gdb sans requête en cours) :
        détachement, `settle` secondes pour que la ROM de démarrage tourne, reconnexion ;
        le serveur arrête alors le cœur, et gdb n'a rien vu (registres à relire : flushregs)."""
        job = _Job(b"")
        with self._lock:
            self._resets.append((job, settle))
        self._wake()
        if not job.done.wait(timeout):
            raise LiveMemoryError("reset par le relais RSP : pas de réponse du serveur")
        if job.error:
            raise LiveMemoryError(job.error)

    def _fail_jobs(self, why: str) -> None:
        with self._lock:
            jobs = list(self._jobs) + [j for j, _ in self._resets]
            self._jobs.clear()
            self._resets.clear()
        if self._inflight is not None:
            jobs.append(self._inflight)
            self._inflight = None
        for j in jobs:
            j.error = why
            j.done.set()

    # ------------------------------------------------------------------ boucle
    def _serve(self) -> None:
        while not self._closed:
            try:
                r, _, _ = select.select([self._listen, self._wake_r], [], [], 0.5)
            except (OSError, ValueError):
                break
            if self._wake_r in r:
                self._drain_wake()
                self._fail_jobs("gdb n'est pas connecté au relais")
            if self._listen not in r or self._closed:
                continue
            try:
                gdb, _ = self._listen.accept()
            except OSError:
                break
            try:
                srv = socket.create_connection(self.server, timeout=5)
            except OSError as exc:
                self.log(f"relais RSP : serveur {self.server[0]}:{self.server[1]} injoignable ({exc})")
                gdb.close()
                continue
            self._reset_state()
            self.connected = True
            try:
                self._pump(gdb, srv)
            except OSError as exc:
                self.log(f"relais RSP : {exc}")
            finally:
                self.connected = False
                for s in (gdb, self._srv if hasattr(self, "_srv") else srv):
                    try:
                        s.close()
                    except OSError:
                        pass
                self._fail_jobs("connexion gdb fermée")

    def _drain_wake(self) -> None:
        try:
            self._wake_r.recv(4096)
        except OSError:
            pass

    def _pump(self, gdb: socket.socket, srv: socket.socket) -> None:
        self._gdb, self._srv = gdb, srv
        gbuf, sbuf = bytearray(), bytearray()
        while not self._closed:
            r, _, _ = select.select([self._gdb, self._srv, self._wake_r], [], [], 0.5)
            if self._wake_r in r:
                self._drain_wake()
            if self._gdb in r:
                data = self._gdb.recv(65536)
                if not data:
                    return
                gbuf += data
            if self._srv in r:
                data = self._srv.recv(65536)
                if not data:
                    self.log("relais RSP : le serveur gdb a fermé la connexion")
                    return
                sbuf += data
            while (tok := take_token(sbuf)) is not None:
                self._from_server(tok)
            while not self._holding() and (tok := take_token(gbuf)) is not None:
                self._from_gdb(tok)
            if self._inflight is not None and time.monotonic() - self._sent_at > 5:
                job, self._inflight = self._inflight, None
                job.error = "le serveur gdb n'a pas répondu à la requête du relais"
                job.done.set()
            self._maybe_send()

    def _holding(self) -> bool:
        """gdb est retenu pendant une requête du relais cible arrêtée."""
        return self._inflight is not None and not self.running

    def _idle(self) -> bool:
        return not self.running and not self.gdb_waiting

    def _maybe_send(self) -> None:
        if self._inflight is not None:
            return
        with self._lock:
            if self._resets and self._idle():
                job, settle = self._resets.popleft()
            else:
                job = None
                if self._jobs and (self.running or self._idle()):
                    self._inflight = self._jobs.popleft()
        if job is not None:
            self._do_reset(job, settle)
            return
        if self._inflight is not None:
            self._sent_at = time.monotonic()
            self._srv.sendall(self._inflight.packet)
            if not self.noack:
                self._acks.append("relay")

    # ------------------------------------------------------------------ trafic
    def _from_gdb(self, tok: bytes) -> None:
        _trace("gdb>", tok)
        if tok[:1] in (b"$", b"%"):
            p = payload_of(tok)
            text = p.decode(errors="replace")
            if text.startswith(RESUME) and not text.startswith("vCont?"):
                self.running = True
            elif text == "k":
                pass                          # pas de réponse attendue
            else:
                self.gdb_waiting = True
            if self._handshaking:
                if text == "g" or text[:1] in "mMXPGZz" or self.running:
                    self._handshaking = False
                else:
                    self._handshake.append(p)
            self._noack_asked = text == "QStartNoAckMode"
            self._monitor = text.startswith("qRcmd,")
            if not self.noack:
                self._acks.append("gdb")
        self._srv.sendall(tok)

    def _from_server(self, tok: bytes) -> None:
        _trace("<srv", tok)
        if tok in (b"+", b"-"):
            owner = self._acks.popleft() if self._acks else "gdb"
            if owner == "gdb":
                self._gdb.sendall(tok)
            return
        if tok[:1] == b"%":                   # notification (mode non-stop)
            self._gdb.sendall(tok)
            return
        p = payload_of(tok)
        console = p[:1] == b"O" and p != b"OK"
        if self._inflight is not None and not console and not (
                self.running and p[:1] in (b"S", b"T", b"W", b"X", b"F")):
            job, self._inflight = self._inflight, None
            if not self.noack:
                self._srv.sendall(b"+")
            job.reply = p
            job.done.set()
            return
        if console and not (self.running or self._monitor):
            if not self.noack:
                self._srv.sendall(b"+")
            self.log("serveur gdb : " + _hex_text(p[1:]))
            return
        if self.running and p[:1] in (b"S", b"T", b"W", b"X"):
            self.running = False
        elif not console:
            self.gdb_waiting = False
            self._monitor = False
            if self._noack_asked and p == b"OK":
                self.noack = True
        self._gdb.sendall(tok)

    # ------------------------------------------------------------------ reset
    def _do_reset(self, job: _Job, settle: float) -> None:
        try:
            self._exchange(frame(f"M{AIRCR:x},4:" + AIRCR_SYSRESETREQ.to_bytes(4, "little").hex()))
            self._exchange(frame("D"))
            self._srv.close()
            time.sleep(settle)
            self._srv = socket.create_connection(self.server, timeout=5)
            noack, self.noack = self.noack, False
            self._acks.clear()
            for p in self._handshake or [b"qSupported:swbreak+;hwbreak+", b"?"]:
                if p == b"QStartNoAckMode":
                    if noack:
                        self._exchange(frame(p))
                        self.noack = True
                    continue
                self._exchange(frame(p))
            self.noack = noack
        except (OSError, LiveMemoryError) as exc:
            job.error = f"reset système par le relais : {exc}"
        job.done.set()

    def _exchange(self, packet: bytes, timeout: float = 5.0) -> bytes:
        """Requête synchrone du relais lui-même (gdb retenu) : réponse, paquets O sautés."""
        _trace("rly>", packet)
        self._srv.sendall(packet)
        buf = bytearray()
        deadline = time.monotonic() + timeout
        self._srv.settimeout(0.5)
        try:
            while time.monotonic() < deadline:
                try:
                    data = self._srv.recv(65536)
                except socket.timeout:
                    continue
                if not data:
                    raise LiveMemoryError("le serveur gdb a fermé la connexion")
                buf += data
                while (tok := take_token(buf)) is not None:
                    if tok in (b"+", b"-"):
                        continue
                    p = payload_of(tok)
                    if not self.noack:
                        self._srv.sendall(b"+")
                    _trace("<rly", tok)
                    if p[:1] == b"O" and p != b"OK":
                        self.log("serveur gdb : " + _hex_text(p[1:]))
                        continue
                    return p
        finally:
            self._srv.settimeout(None)
        raise LiveMemoryError(f"pas de réponse du serveur gdb à {packet[:24]!r}")


def _hex_text(h: bytes) -> str:
    try:
        return bytes.fromhex(h.decode()).decode(errors="replace").strip()
    except ValueError:
        return h.decode(errors="replace")


class RelayMemory(LiveMemory):
    """Canal mémoire « en direct » par le relais RSP : paquets `m` / `M` glissés entre ceux
    de gdb (cible en marche ou arrêtée)."""
    name = "relais gdb-remote"
    CHUNK = 4096

    def __init__(self, relay: RspRelay):
        super().__init__()
        self.relay = relay

    def read(self, addr: int, length: int) -> bytes:
        out = bytearray()
        pos = 0
        while pos < length:
            n = min(self.CHUNK, length - pos)
            reply = self.relay.request(f"m{addr + pos:x},{n:x}")
            if reply[:1] == b"E" and len(reply) == 3:
                raise LiveMemoryError(f"lecture de {addr + pos:#x} refusée par le serveur ({reply.decode()})")
            try:
                data = bytes.fromhex(reply.decode())
            except ValueError:
                raise LiveMemoryError(f"réponse illisible du serveur : {reply[:40]!r}") from None
            if len(data) != n:
                raise LiveMemoryError(f"lecture de {addr + pos:#x} : {len(data)} octets au lieu de {n}")
            out += data
            pos += n
        return bytes(out)

    def write(self, addr: int, data: bytes) -> None:
        reply = self.relay.request(f"M{addr:x},{len(data):x}:{data.hex()}")
        if reply != b"OK":
            raise LiveMemoryError(f"écriture en {addr:#x} refusée par le serveur ({reply.decode()})")

    def write_u32(self, addr: int, value: int) -> None:
        self.write(addr, value.to_bytes(4, "little"))

    def sample_pc(self) -> int | None:
        """DWT_PCSR lu cible en marche (DEMCR.TRCENA mis au premier appel)."""
        if not getattr(self, "_trcena", False):
            demcr = int.from_bytes(self.read(DEMCR, 4), "little")
            if not demcr & DEMCR_TRCENA:
                self.write_u32(DEMCR, demcr | DEMCR_TRCENA)
            self._trcena = True
        pc = int.from_bytes(self.read(DWT_PCSR, 4), "little")
        return None if pc == 0xFFFFFFFF else pc
