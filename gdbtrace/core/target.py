"""Lancement du serveur gdb côté cible : QEMU (sans matériel) ou OpenOCD (sonde réelle)."""
from __future__ import annotations

import shutil
import socket
import subprocess
import time
from dataclasses import dataclass, field

from .live import LiveMemory, OpenOcdTcl, QemuQmp


def _wait_port(host: str, port: int, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return
        except OSError:
            time.sleep(0.1)
    raise TimeoutError(f"{host}:{port} n'écoute pas après {timeout}s")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@dataclass
class TargetServer:
    """Classe de base : un processus qui expose le protocole gdb-remote."""
    host: str = "127.0.0.1"
    gdb_port: int = 3333
    proc: subprocess.Popen | None = field(default=None, repr=False)
    #: Faut-il faire `load` après connexion (OpenOCD oui, QEMU charge l'ELF lui-même).
    needs_load: bool = True

    def command_line(self) -> list[str]:
        raise NotImplementedError

    def reset(self, session) -> None:
        """Reset matériel par défaut : commande monitor OpenOCD / pyOCD / J-Link."""
        session.monitor("reset halt")

    def live_memory(self) -> LiveMemory | None:
        """Canal mémoire utilisable cible en marche, ou None si le serveur n'en offre pas."""
        return None

    @property
    def remote(self) -> str:
        return f"{self.host}:{self.gdb_port}"

    def start(self, timeout: float = 10.0) -> None:
        cmd = self.command_line()
        if not shutil.which(cmd[0]):
            raise FileNotFoundError(f"{cmd[0]} introuvable dans le PATH")
        self.proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        try:
            _wait_port(self.host, self.gdb_port, timeout)
        except TimeoutError:
            err = b""
            if self.proc.poll() is not None and self.proc.stderr:
                err = self.proc.stderr.read()
            self.stop()
            raise RuntimeError(f"échec du démarrage de {cmd[0]}: {err.decode(errors='replace')}")

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.proc = None


@dataclass
class QemuTarget(TargetServer):
    elf: str = ""
    machine: str = "mps2-an385"
    qemu: str = "qemu-system-arm"
    needs_load: bool = False
    #: port QMP (0 = choisi automatiquement), sert à la lecture mémoire en direct
    qmp_port: int = 0
    #: ETM simulé : une instruction par bloc TCG, journal activé à chaque exécution
    #: (plus lent ; QEMU n'émule pas d'ETM, voir trace/etm/qemu_sim.py)
    etm_sim: bool = False

    def __post_init__(self) -> None:
        if not self.qmp_port:
            self.qmp_port = free_port()

    def reset(self, session) -> None:
        # Le gdbstub de QEMU ne sait pas faire « reset halt » : reset via la table des vecteurs.
        session.reset_from_vectors()

    def command_line(self) -> list[str]:
        return [
            self.qemu, "-M", self.machine, "-nographic", "-monitor", "none",
            "-serial", "none", "-kernel", self.elf,
            "-gdb", f"tcp:{self.host}:{self.gdb_port}", "-S",
            "-qmp", f"tcp:{self.host}:{self.qmp_port},server=on,wait=off",
        ] + (["-accel", "tcg,one-insn-per-tb=on"] if self.etm_sim else [])

    def live_memory(self) -> LiveMemory | None:
        return QemuQmp(self.host, self.qmp_port)


@dataclass
class OpenOcdTarget(TargetServer):
    """Sonde réelle. `configs` = liste de fichiers -f (interface + cible ou carte)."""
    configs: list[str] = field(default_factory=lambda: ["board/st_nucleo_f4.cfg"])
    tcl_port: int = 6666
    telnet_port: int = 4444
    openocd: str = "openocd"
    extra: list[str] = field(default_factory=list)
    #: support RTOS d'OpenOCD (« auto », « FreeRTOS »...) : gdb voit alors les tâches comme threads
    rtos: str | None = None

    def command_line(self) -> list[str]:
        cmd = [self.openocd]
        for c in self.configs:
            cmd += ["-f", c]
        if self.rtos:
            # $_TARGETNAME est défini par les fichiers de configuration de la cible
            cmd += ["-c", f"$_TARGETNAME configure -rtos {self.rtos}"]
        cmd += [
            "-c", f"gdb_port {self.gdb_port}",
            "-c", f"tcl_port {self.tcl_port}",
            "-c", f"telnet_port {self.telnet_port}",
            *self.extra,
        ]
        return cmd

    def live_memory(self) -> LiveMemory | None:
        return OpenOcdTcl(self.host, self.tcl_port)


@dataclass
class RemoteTarget(TargetServer):
    """Serveur gdb déjà lancé ailleurs (J-Link GDB Server, pyOCD, probe-rs...).

    `tcl_port` : si le serveur est un OpenOCD lancé à part, son port TCL permet la
    lecture mémoire en direct.
    """
    tcl_host: str | None = None
    tcl_port: int = 0

    def live_memory(self) -> LiveMemory | None:
        if self.tcl_port:
            return OpenOcdTcl(self.tcl_host or self.host, self.tcl_port)
        return None

    def start(self, timeout: float = 10.0) -> None:
        _wait_port(self.host, self.gdb_port, timeout)

    def stop(self) -> None:
        pass
