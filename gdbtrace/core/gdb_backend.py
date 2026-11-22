"""Pilote GDB/MI asynchrone.

Un thread lit la sortie de gdb ligne par ligne. Chaque commande est envoyée avec
un jeton numérique. Le résultat (^done, ^error...) revient au demandeur par une
queue. Les enregistrements asynchrones (*stopped, *running, =breakpoint-...) et
les flux (console, target, log) partent vers les abonnés.

Les abonnés sont appelés dans le thread lecteur : ils ne doivent pas bloquer.
L'interface graphique se contente de poser l'événement dans sa propre queue.
"""
from __future__ import annotations

import itertools
import queue
import re
import subprocess
import threading
from typing import Any, Callable

from pygdbmi.gdbmiparser import parse_response

Listener = Callable[[dict], None]


_STATUS_RECORD = re.compile(r"^\d*\+")       # enregistrement asynchrone de statut GDB/MI

class GdbError(RuntimeError):
    """gdb a répondu ^error à une commande."""


class GdbTimeout(TimeoutError):
    """gdb n'a pas répondu dans le délai imparti."""


class GdbMI:
    def __init__(self, gdb_path: str = "gdb-multiarch", args: list[str] | None = None):
        self.gdb_path = gdb_path
        self.args = args or []
        self._proc: subprocess.Popen | None = None
        self._tokens = itertools.count(1)
        self._pending: dict[int, queue.Queue] = {}
        self._pending_lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._listeners: list[Listener] = []
        self._reader: threading.Thread | None = None
        self._console_lock = threading.Lock()
        self._capture: list[str] | None = None  # sortie de console() en cours

    # ------------------------------------------------------------------ cycle de vie
    def start(self) -> None:
        cmd = [self.gdb_path, "--interpreter=mi3", "--nx", "-q", *self.args]
        self._proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        self._reader = threading.Thread(target=self._read_loop, name="gdb-mi-reader", daemon=True)
        self._reader.start()
        # Mode asynchrone : gdb accepte des commandes pendant que la cible tourne.
        self.command("-gdb-set mi-async on")
        self.command("-gdb-set pagination off")
        self.command("-gdb-set confirm off")

    def stop(self) -> None:
        if not self._proc:
            return
        try:
            self.command("-gdb-exit", timeout=2)
        except Exception:
            pass
        try:
            self._proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self._proc.kill()
        self._proc = None

    @property
    def alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    # ------------------------------------------------------------------ abonnés
    def add_listener(self, fn: Listener) -> None:
        self._listeners.append(fn)

    def remove_listener(self, fn: Listener) -> None:
        if fn in self._listeners:
            self._listeners.remove(fn)

    # ------------------------------------------------------------------ commandes
    def command(self, cmd: str, timeout: float = 10.0) -> dict[str, Any]:
        """Envoie une commande MI et retourne le champ `payload` du résultat."""
        rec = self.command_raw(cmd, timeout)
        if rec.get("message") == "error":
            msg = (rec.get("payload") or {}).get("msg", "erreur gdb")
            raise GdbError(f"{cmd}: {msg}")
        return rec.get("payload") or {}

    def command_raw(self, cmd: str, timeout: float = 10.0) -> dict:
        if not self.alive:
            raise RuntimeError("gdb n'est pas démarré")
        token = next(self._tokens)
        q: queue.Queue = queue.Queue(maxsize=1)
        with self._pending_lock:
            self._pending[token] = q
        with self._write_lock:
            assert self._proc and self._proc.stdin
            self._proc.stdin.write(f"{token}{cmd}\n")
            self._proc.stdin.flush()
        try:
            return q.get(timeout=timeout)
        except queue.Empty:
            raise GdbTimeout(f"pas de réponse de gdb à « {cmd} »") from None
        finally:
            with self._pending_lock:
                self._pending.pop(token, None)

    def console(self, cli: str, timeout: float = 10.0) -> str:
        """Exécute une commande CLI gdb et retourne sa sortie texte.

        La sortie est capturée et **pas** diffusée aux abonnés : les commandes internes
        (ptype, info line...) ne doivent pas polluer la console de l'utilisateur.
        Les appels sont sérialisés pour que chaque capture reçoive sa propre sortie.
        La sortie d'une commande `monitor` (réponse du serveur gdb : OpenOCD, QEMU) arrive
        par le flux « target » de MI (`@"..."`), pas par le flux « console » : les deux sont
        capturés.
        """
        escaped = cli.replace("\\", "\\\\").replace('"', '\\"')
        with self._console_lock:
            self._capture = []
            try:
                self.command(f'-interpreter-exec console "{escaped}"', timeout)
            finally:
                out, self._capture = self._capture, None
        return "".join(out)

    # ------------------------------------------------------------------ lecteur
    def _read_loop(self) -> None:
        assert self._proc and self._proc.stdout
        for line in self._proc.stdout:
            line = line.rstrip("\n")
            if not line or line.strip() == "(gdb)":
                continue
            if _STATUS_RECORD.match(line):
                continue                        # progression (`+download,{...}`) : sans intérêt ici
            try:
                rec = parse_response(line)
            except Exception:
                rec = {"type": "output", "message": None, "payload": line, "token": None}
            if rec.get("type") == "result" and rec.get("token") is not None:
                with self._pending_lock:
                    q = self._pending.get(int(rec["token"]))
                if q is not None:
                    q.put(rec)
                    continue
            capture = self._capture
            if capture is not None and rec.get("type") in ("console", "target"):
                capture.append(rec.get("payload") or "")
                continue
            self._dispatch(rec)
        self._dispatch({"type": "notify", "message": "gdb-exited", "payload": None, "token": None})

    def _dispatch(self, rec: dict) -> None:
        for fn in list(self._listeners):
            try:
                fn(rec)
            except Exception:  # un abonné défaillant ne doit pas tuer le lecteur
                pass
