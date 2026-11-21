"""Accès mémoire « en direct », en parallèle de gdb, pendant que la cible tourne.

gdb (mode all-stop) refuse de lire la mémoire tant que la cible est en marche.
GdbTrace passe donc par un canal latéral :

- OpenOCD : port TCL (`read_memory`), qui accède à la mémoire via le MEM-AP CoreSight
  sans arrêter le cœur (Cortex-M) ;
- QEMU : QMP (commande moniteur `x`), qui lit la mémoire de la VM en marche.

`LivePoller` regroupe les plages demandées par les vues et les relit périodiquement.
"""
from __future__ import annotations

import json
import socket
import threading
from dataclasses import dataclass, field
from typing import Callable

Range = tuple[int, int]  # (adresse, longueur)


class LiveMemoryError(RuntimeError):
    pass


class LiveMemory:
    """Interface commune. Les implémentations sont thread-safe."""
    name = "live"

    def __init__(self) -> None:
        self._lock = threading.Lock()

    def read(self, addr: int, length: int) -> bytes:
        raise NotImplementedError

    def sample_pc(self) -> int | None:
        """Un échantillon du PC, cible en marche, sans l'arrêter (profilage statistique)."""
        raise LiveMemoryError(f"{self.name} : échantillonnage du PC non disponible")

    def close(self) -> None:
        pass


DEMCR = 0xE000EDFC
DEMCR_TRCENA = 1 << 24
DWT_PCSR = 0xE000101C


# ====================================================================== OpenOCD
class OpenOcdTcl(LiveMemory):
    """Client du serveur TCL d'OpenOCD (commandes et réponses terminées par 0x1A)."""
    name = "OpenOCD TCL"
    TERM = b"\x1a"
    CHUNK = 4096

    def __init__(self, host: str = "127.0.0.1", port: int = 6666, timeout: float = 3.0):
        super().__init__()
        self.host, self.port, self.timeout = host, port, timeout
        self._sock: socket.socket | None = None
        self._buf = b""

    def _connect(self) -> socket.socket:
        if self._sock is None:
            self._sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
            self._buf = b""
        return self._sock

    def command(self, cmd: str) -> str:
        with self._lock:
            try:
                sock = self._connect()
                sock.sendall(cmd.encode() + self.TERM)
                while self.TERM not in self._buf:
                    chunk = sock.recv(65536)
                    if not chunk:
                        raise LiveMemoryError("connexion TCL fermée par OpenOCD")
                    self._buf += chunk
            except OSError as exc:
                self._close_locked()
                raise LiveMemoryError(f"OpenOCD TCL {self.host}:{self.port} : {exc}") from exc
            reply, _, self._buf = self._buf.partition(self.TERM)
            return reply.decode(errors="replace")

    def query(self, script: str) -> str:
        """Exécute `script` ; retourne son résultat, ou « ERR message » en cas d'échec.

        OpenOCD (0.12 au moins) recopie dans son journal le résultat de chaque commande reçue
        par le port TCL : sur stderr, et vers gdb (paquets « O », donc la console de
        gdbtrace) tant que la cible tourne. Pour des lectures en continu, c'est un flot de
        texte qui finit par bloquer OpenOCD si son stderr n'est pas lu. Avec
        `return -level 0 -code exit`, le résultat part bien sur la socket, mais
        command_run_line() ignore le code JIM_EXIT au lieu de recopier le résultat.
        """
        return self.command(
            f"if {{[catch {{{script}}} r]}} {{return -level 0 -code exit \"ERR $r\"}} "
            f"else {{return -level 0 -code exit $r}}").strip()

    def read(self, addr: int, length: int) -> bytes:
        out = bytearray()
        pos = 0
        while pos < length:
            n = min(self.CHUNK, length - pos)
            out += self._read_chunk(addr + pos, n)
            pos += n
        return bytes(out)

    def _read_chunk(self, addr: int, n: int) -> bytes:
        # Accès 32 bits quand c'est possible : beaucoup de registres périphériques
        # n'acceptent pas les accès octet.
        width = 32 if addr % 4 == 0 and n % 4 == 0 else 8
        count = n * 8 // width
        reply = self.query(f"read_memory {addr:#x} {width} {count}")
        if reply.startswith("ERR") or (reply and not reply.startswith("0x")):
            raise LiveMemoryError(f"read_memory {addr:#x} : {reply[4:] if reply.startswith('ERR') else reply}")
        words = [int(w, 16) for w in reply.split()]
        if len(words) != count:
            raise LiveMemoryError(f"read_memory {addr:#x} : {len(words)} valeurs au lieu de {count}")
        size = width // 8
        return b"".join(w.to_bytes(size, "little") for w in words)

    def write_u32(self, addr: int, value: int) -> None:
        reply = self.query(f"write_memory {addr:#x} 32 {{{value:#x}}}; list OK")
        if reply != "OK":
            raise LiveMemoryError(f"write_memory {addr:#x} : {reply}")

    def sample_pc(self) -> int | None:
        """Lecture de DWT_PCSR par l'AHB-AP : PC récent, cœur non arrêté (Cortex-M3/M4/M7).

        DEMCR.TRCENA doit être à 1 (activé au premier appel). 0xFFFFFFFF : pas d'échantillon
        (cœur arrêté ou en sommeil). Les Cortex-M0/M0+ n'ont pas de PCSR."""
        if not getattr(self, "_trcena", False):
            demcr = int.from_bytes(self.read(DEMCR, 4), "little")
            if not demcr & DEMCR_TRCENA:
                self.write_u32(DEMCR, demcr | DEMCR_TRCENA)
            self._trcena = True
        pc = int.from_bytes(self.read(DWT_PCSR, 4), "little")
        return None if pc == 0xFFFFFFFF else pc

    def _close_locked(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
        self._sock = None

    def close(self) -> None:
        with self._lock:
            self._close_locked()


# ====================================================================== QEMU
class QemuQmp(LiveMemory):
    """Lecture mémoire d'une VM QEMU en marche, via QMP.

    On utilise la commande moniteur `x` (et non `pmemsave`) : elle passe par l'espace
    d'adresses du CPU, qui contient le bloc privé Cortex-M (SysTick, NVIC, SCB à
    0xE000xxxx), absent de l'espace système que lit `pmemsave`. Elle permet aussi des
    accès 32 bits, préférables pour les registres périphériques.
    """
    name = "QEMU QMP"
    CHUNK = 4096

    def __init__(self, host: str = "127.0.0.1", port: int = 4445, timeout: float = 3.0):
        super().__init__()
        self.host, self.port, self.timeout = host, port, timeout
        self._sock: socket.socket | None = None
        self._rfile = None

    def _connect(self) -> None:
        if self._sock is not None:
            return
        self._sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        self._rfile = self._sock.makefile("rb")
        self._recv()  # message d'accueil {"QMP": ...}
        self._execute_locked("qmp_capabilities")

    def _recv(self) -> dict:
        while True:
            line = self._rfile.readline()
            if not line:
                raise LiveMemoryError("connexion QMP fermée par QEMU")
            msg = json.loads(line)
            if "event" not in msg:  # les événements asynchrones sont ignorés
                return msg

    def _execute_locked(self, cmd: str, **args) -> dict:
        payload = {"execute": cmd}
        if args:
            payload["arguments"] = args
        self._sock.sendall(json.dumps(payload).encode() + b"\r\n")
        msg = self._recv()
        if "error" in msg:
            raise LiveMemoryError(f"QMP {cmd} : {msg['error'].get('desc', msg['error'])}")
        return msg.get("return", {})

    def execute(self, cmd: str, **args) -> dict:
        with self._lock:
            try:
                self._connect()
                return self._execute_locked(cmd, **args)
            except (OSError, ValueError) as exc:
                self._close_locked()
                raise LiveMemoryError(f"QMP {self.host}:{self.port} : {exc}") from exc

    def sample_pc(self) -> int | None:
        """PC de la vCPU lu par le moniteur pendant l'exécution : l'équivalent émulé de la
        lecture de DWT_PCSR (QEMU n'émule ni le DWT ni le SWO)."""
        out = self.hmp("info registers")
        k = out.find("R15=")
        if k < 0:
            raise LiveMemoryError(f"info registers : PC introuvable ({out.strip()[:60]})")
        return int(out[k + 4:k + 12], 16)

    def hmp(self, command: str) -> str:
        """Commande du moniteur humain (HMP) via QMP ; retourne sa sortie texte."""
        return self.execute("human-monitor-command", **{"command-line": command})

    def read(self, addr: int, length: int) -> bytes:
        out = bytearray()
        pos = 0
        while pos < length:
            n = min(self.CHUNK, length - pos)
            out += self._read_chunk(addr + pos, n)
            pos += n
        return bytes(out)

    def _read_chunk(self, addr: int, n: int) -> bytes:
        size = 4 if addr % 4 == 0 and n % 4 == 0 else 1
        count = n // size
        text = self.hmp(f"x /{count}x{'w' if size == 4 else 'b'} {addr:#x}")
        values = []
        for line in text.splitlines():
            head, sep, rest = line.partition(":")
            if not sep:
                continue
            try:
                int(head.strip(), 16)  # préfixe d'adresse attendu
                values += [int(v, 16) for v in rest.split()]
            except ValueError:
                raise LiveMemoryError(f"x {addr:#x} : {text.strip()}") from None
        if len(values) != count:
            raise LiveMemoryError(f"x {addr:#x} : {len(values)} valeurs au lieu de {count} "
                                  f"({text.strip()[:80]})")
        return b"".join(v.to_bytes(size, "little") for v in values)

    def _close_locked(self) -> None:
        for obj in (self._rfile, self._sock):
            try:
                if obj is not None:
                    obj.close()
            except OSError:
                pass
        self._sock = self._rfile = None

    def close(self) -> None:
        with self._lock:
            self._close_locked()


# ====================================================================== regroupement
def merge_ranges(ranges: list[Range], gap: int = 16) -> list[Range]:
    """Fusionne les plages qui se chevauchent ou sont séparées de moins de `gap` octets."""
    out: list[list[int]] = []
    for addr, n in sorted(r for r in ranges if r[1] > 0):
        if out and addr <= out[-1][0] + out[-1][1] + gap:
            end = max(out[-1][0] + out[-1][1], addr + n)
            out[-1][1] = end - out[-1][0]
        else:
            out.append([addr, n])
    return [(a, n) for a, n in out]


def slice_ranges(blocks: dict[Range, bytes], wanted: list[Range]) -> dict[int, bytes]:
    """Extrait chaque plage voulue des blocs lus. Résultat indexé par adresse de début."""
    out: dict[int, bytes] = {}
    for addr, n in wanted:
        for (baddr, bn), data in blocks.items():
            if baddr <= addr and addr + n <= baddr + bn:
                out[addr] = data[addr - baddr: addr - baddr + n]
                break
    return out


# ====================================================================== poller
@dataclass
class _Sub:
    ranges: list[Range]
    callback: Callable[[object], None]
    token: int = field(default=0)
    #: tâche de lecture libre (parcours de listes chaînées...) : job(read) -> résultat
    job: Callable[[Callable[[int, int], bytes]], object] | None = None


class LivePoller:
    """Thread unique qui relit périodiquement les plages demandées par les vues.

    - `subscribe(key, ranges, callback)` : `callback({addr: bytes})` est appelé dans le
      thread GUI (via `post`) à chaque lecture, tant que `is_running()` est vrai ;
    - les erreurs passent par `on_error(msg)`, une seule fois par message distinct.
    """

    def __init__(self, reader: LiveMemory, is_running: Callable[[], bool],
                 post: Callable[..., None], period_ms: int = 200,
                 on_error: Callable[[str], None] | None = None):
        self.reader = reader
        self.is_running = is_running
        self.post = post
        self.period = period_ms / 1000.0
        self.on_error = on_error or (lambda msg: None)
        self._subs: dict[object, _Sub] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._tokens = 0
        self._last_error: str | None = None
        self._thread: threading.Thread | None = None
        self.reads = 0  # statistique, utile aux tests

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, name="live-poller", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
        self._thread = None

    def subscribe(self, key: object, ranges: list[Range],
                  callback: Callable[[dict[int, bytes]], None]) -> None:
        with self._lock:
            self._tokens += 1
            self._subs[key] = _Sub(list(ranges), callback, self._tokens)
        self._wake.set()

    def subscribe_job(self, key: object, job: Callable[[Callable[[int, int], bytes]], object],
                      callback: Callable[[object], None]) -> None:
        """`job(read)` est exécuté dans le thread du poller (lectures en chaîne de pointeurs,
        impossibles avec des plages fixes) ; `callback(résultat)` dans le thread GUI."""
        with self._lock:
            self._tokens += 1
            self._subs[key] = _Sub([], callback, self._tokens, job)
        self._wake.set()

    def unsubscribe(self, key: object) -> None:
        with self._lock:
            self._subs.pop(key, None)

    def subscribed(self, key: object) -> bool:
        with self._lock:
            return key in self._subs

    def poll_now(self) -> None:
        self._wake.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            self._wake.wait(self.period)
            self._wake.clear()
            if self._stop.is_set():
                break
            if not self.is_running():
                continue
            with self._lock:
                subs = list(self._subs.items())
            if subs:
                self._poll(subs)

    def _report(self, msg: str) -> None:
        if msg != self._last_error:
            self._last_error = msg
            self.post(self.on_error, msg)

    def _poll(self, subs: list[tuple[object, _Sub]]) -> None:
        for key, sub in subs:
            if sub.job is None:
                continue
            try:
                result = sub.job(self.reader.read)
            except Exception as exc:  # noqa: BLE001
                self._report(f"lecture live : {exc}")
                continue
            self.post(self._deliver, key, sub.token, result)
        subs = [(k, s) for k, s in subs if s.job is None]
        wanted = [r for _, s in subs for r in s.ranges]
        blocks: dict[Range, bytes] = {}
        for addr, n in merge_ranges(wanted):
            try:
                blocks[(addr, n)] = self.reader.read(addr, n)
            except Exception as exc:  # noqa: BLE001 - une plage en erreur n'arrête pas les autres
                self._report(f"lecture live {addr:#x}+{n} : {exc}")
        if blocks and len(blocks) == len(merge_ranges(wanted)):
            self._last_error = None  # tout a été lu : une nouvelle erreur sera de nouveau signalée
        self.reads += 1
        for key, sub in subs:
            data = slice_ranges(blocks, sub.ranges)
            if data:
                self.post(self._deliver, key, sub.token, data)

    def _deliver(self, key: object, token: int, data: object) -> None:
        """Exécuté dans le thread GUI : ignore les résultats d'un abonnement périmé."""
        with self._lock:
            sub = self._subs.get(key)
        if sub is not None and sub.token == token and self.is_running():
            sub.callback(data)
