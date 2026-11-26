"""Terminal (TERM.view) : le texte que le programme écrit par le semihosting ou le RTT, et ce
que l'utilisateur lui envoie.

- **Semihosting** (BKPT 0xAB, SYS_WRITE0 / SYS_WRITE / SYS_READC) : traité par le serveur gdb,
  qui en sert les entrées-sorties sur une socket TCP : QEMU (`-semihosting-config
  target=native,chardev=…`, `QemuTarget.semihosting_port`), OpenOCD (`arm semihosting
  enable`, `arm semihosting_redirect tcp PORT`). `TcpSource` lit cette socket.
- **RTT** (bloc de contrôle « SEGGER RTT » en RAM) : `RttChannel` lit le tampon montant 0 et
  écrit le descendant 0, par deux fonctions `read(addr, n)` / `write(addr, data)` : le canal
  mémoire en direct pendant la marche (OpenOCD TCL, relais ST-LINK : il faut pouvoir écrire
  RdOff), gdb à l'arrêt. QEMU (QMP) ne sait pas écrire : RTT lu aux arrêts seulement.

`Terminal` réunit les sources : texte reçu (borné), abonnés, envoi.
"""
from __future__ import annotations

import codecs
import socket
import threading
import time
from typing import Callable

RTT_ID = b"SEGGER RTT"
RTT_SYMBOL = "_SEGGER_RTT"
BUFFER_DESC = 24                 # sName, pBuffer, SizeOfBuffer, WrOff, RdOff, Flags
MAX_TEXT = 200_000               # caractères gardés


class Terminal:
    """Texte reçu de la cible et envoi vers elle (vue Terminal, `gdbtrace.terminal()`)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._chunks: list[tuple[str, str]] = []           # (source, texte)
        self._size = 0
        self._cursor = 0                                   # pour take_new() (scripts)
        self._total = 0
        self.listeners: list[Callable[[str, str], None]] = []
        self.sources: list = []
        self.rtt: RttChannel | None = None
        #: (lecture, écriture) cible arrêtée (gdb) et en marche (canal mémoire ou None)
        self.stopped_io: tuple[Callable, Callable] | None = None
        self.live_io: tuple[Callable, Callable] | None = None
        self.is_running: Callable[[], bool] = lambda: False

    # ------------------------------------------------------------------ réception
    def feed(self, text: str, source: str) -> None:
        """Texte reçu (appelé depuis le fil d'une source : les abonnés ne doivent pas bloquer)."""
        if not text:
            return
        with self._lock:
            self._chunks.append((source, text))
            self._size += len(text)
            self._total += len(text)
            while self._size > MAX_TEXT and len(self._chunks) > 1:
                _s, old = self._chunks.pop(0)
                self._size -= len(old)
        for fn in list(self.listeners):
            try:
                fn(text, source)
            except Exception:  # noqa: BLE001 - un abonné défaillant n'arrête pas la source
                pass

    def text(self, source: str | None = None) -> str:
        """Texte gardé ; d'une seule source (« semihosting », « rtt ») si `source`."""
        with self._lock:
            return "".join(t for s, t in self._chunks if source is None or s == source)

    def take_new(self) -> str:
        """Texte arrivé depuis l'appel précédent (scripts, serveur MCP)."""
        with self._lock:
            start = self._cursor
            self._cursor = self._total
            whole = "".join(t for _s, t in self._chunks)
        kept_from = self._total - len(whole)
        return whole[max(0, start - kept_from):]

    def clear(self) -> None:
        with self._lock:
            self._chunks.clear()
            self._size = 0
            self._cursor = self._total

    @property
    def active(self) -> bool:
        return bool(self.sources) or self.rtt is not None

    def describe(self) -> str:
        parts = [s.label for s in self.sources]
        if self.rtt is not None:
            parts.append(self.rtt.label + ("" if self.live_io else " (lu aux arrêts)"))
        return ", ".join(parts) if parts else "aucune source (--semihosting, --rtt)"

    # ------------------------------------------------------------------ RTT
    def poll_rtt(self, running: bool | None = None) -> int:
        """Lit le RTT par le bon chemin (canal mémoire en marche, gdb à l'arrêt) ; rend le
        nombre d'octets reçus."""
        if self.rtt is None:
            return 0
        running = self.is_running() if running is None else running
        io = self.live_io if running else self.stopped_io
        if io is None:
            return 0
        text = self.rtt.poll(*io)
        self.feed(text, "rtt")
        return len(text)

    # ------------------------------------------------------------------ envoi
    def send(self, text: str) -> str:
        """Envoie du texte à la cible : RTT (canal descendant 0) s'il y en a un, sinon la
        socket du semihosting (SYS_READC). Rend où c'est parti."""
        data = text.encode("utf-8")
        if self.rtt is not None:
            io = self.live_io if self.is_running() else self.stopped_io
            if io is None:
                raise RuntimeError("RTT : la cible tourne et ce serveur ne sait pas écrire en marche")
            n = self.rtt.send(*io, data)
            if n < len(data):
                raise RuntimeError(f"RTT : {n} octet(s) sur {len(data)} envoyés (tampon descendant plein)")
            return "rtt"
        for src in self.sources:
            if src.can_send:
                src.send(data)
                return src.label
        raise RuntimeError("aucune source qui accepte une entrée (--rtt, --semihosting)")

    def close(self) -> None:
        for src in self.sources:
            src.close()
        self.sources = []
        self.rtt = None


class TcpSource:
    """Socket TCP servie par le serveur gdb (semihosting de QEMU ou d'OpenOCD) : lue dans un
    fil, texte UTF-8 versé dans le terminal."""

    can_send = True

    def __init__(self, terminal: Terminal, host: str, port: int, label: str, timeout: float = 5.0):
        self.terminal, self.label = terminal, label
        self.sock: socket.socket | None = None
        deadline = time.monotonic() + timeout
        while True:
            try:
                self.sock = socket.create_connection((host, port), timeout=1.0)
                break
            except OSError:
                if time.monotonic() > deadline:
                    raise RuntimeError(f"{label} : pas de connexion à {host}:{port}") from None
                time.sleep(0.1)
        self.sock.settimeout(None)
        self._stop = False
        self.thread = threading.Thread(target=self._loop, name=f"terminal-{label}", daemon=True)
        self.thread.start()

    def _loop(self) -> None:
        dec = codecs.getincrementaldecoder("utf-8")(errors="replace")
        while not self._stop:
            try:
                data = self.sock.recv(4096)
            except OSError:
                break
            if not data:
                break
            self.terminal.feed(dec.decode(data), self.label)

    def send(self, data: bytes) -> None:
        self.sock.sendall(data)

    def close(self) -> None:
        self._stop = True
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass


class RttChannel:
    """Canaux 0 (montant, descendant) d'un bloc de contrôle RTT à `addr`."""

    label = "RTT"

    def __init__(self, addr: int):
        self.addr = addr
        self._dec = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self.received = 0

    def _descs(self, read) -> tuple[int, int] | None:
        """Adresses des descripteurs montant 0 et descendant 0, ou None si le bloc n'est pas
        encore initialisé (identifiant absent : programme pas encore passé par là)."""
        head = read(self.addr, 24)
        if not head.startswith(RTT_ID):
            return None
        n_up = int.from_bytes(head[16:20], "little")
        n_down = int.from_bytes(head[20:24], "little")
        if not (0 < n_up <= 16 and 0 <= n_down <= 16):
            return None
        up = self.addr + 24
        return up, (up + BUFFER_DESC * n_up) if n_down else 0

    @staticmethod
    def _desc(read, at: int) -> tuple[int, int, int, int]:
        d = read(at, BUFFER_DESC)
        buf, size, wr, rd = (int.from_bytes(d[k:k + 4], "little") for k in (4, 8, 12, 16))
        return buf, size, wr, rd

    def poll(self, read, write) -> str:
        """Lit ce que le programme a écrit sur le canal montant 0 et avance RdOff."""
        descs = self._descs(read)
        if descs is None:
            return ""
        up = descs[0]
        buf, size, wr, rd = self._desc(read, up)
        if not size or wr >= size or rd >= size or wr == rd:
            return ""
        if wr > rd:
            data = read(buf + rd, wr - rd)
        else:
            data = read(buf + rd, size - rd) + (read(buf, wr) if wr else b"")
        write(up + 16, wr.to_bytes(4, "little"))          # RdOff = WrOff lu
        self.received += len(data)
        return self._dec.decode(data)

    def send(self, read, write, data: bytes) -> int:
        """Écrit `data` sur le canal descendant 0 ; rend le nombre d'octets qui tiennent."""
        descs = self._descs(read)
        if descs is None or not descs[1]:
            raise RuntimeError("RTT : pas de canal descendant (bloc absent ou pas encore initialisé)")
        down = descs[1]
        buf, size, wr, rd = self._desc(read, down)
        n = 0
        while n < len(data):
            nxt = (wr + 1) % size
            if nxt == rd:
                break
            write(buf + wr, data[n:n + 1])
            wr, n = nxt, n + 1
        write(down + 12, wr.to_bytes(4, "little"))        # WrOff
        return n
