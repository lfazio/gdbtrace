"""Lancement du serveur gdb côté cible : QEMU (sans matériel) ou OpenOCD (sonde réelle)."""
from __future__ import annotations

import collections
import os
import queue
import shutil
import signal
import socket
import subprocess
import threading
import time
from dataclasses import dataclass, field

from .live import LiveMemory, OpenOcdTcl, ProcMem, QemuQmp


def _wait_port(host: str, port: int, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return
        except OSError:
            time.sleep(0.1)
    raise TimeoutError(f"{host}:{port} n'écoute pas après {timeout}s")


def _load_prctl():
    """`prctl` de la libc, résolu ici, dans GdbTrace : l'enfant d'un fork ne doit ni importer
    ni charger de bibliothèque (dlopen), car un autre fil pouvait tenir le verrou d'import, de
    malloc ou du chargeur au moment du fork, et l'enfant s'y bloquerait."""
    try:
        import ctypes
        return ctypes.CDLL("libc.so.6", use_errno=True).prctl
    except Exception:  # noqa: BLE001 - autre système : pas de filet
        return None


_PRCTL = _load_prctl()
_PR_SET_PDEATHSIG = 1
_SIGTERM = int(signal.SIGTERM)


def _die_with_parent() -> None:
    """Dans le processus du serveur (Linux) : SIGTERM quand GdbTrace meurt, même brutalement.
    Sinon un OpenOCD orphelin garde la sonde et ses ports, et la session suivante s'y connecte
    à son insu. Exécutée entre fork et exec : n'appelle que la fonction déjà résolue."""
    if _PRCTL is not None:
        try:
            _PRCTL(_PR_SET_PDEATHSIG, _SIGTERM)
        except Exception:  # noqa: BLE001 - pas de filet, le serveur démarre quand même
            pass


_spawner: "queue.Queue | None" = None
_spawner_lock = threading.Lock()


def _spawn(cmd: list[str], **kw) -> subprocess.Popen:
    """Popen exécuté par un thread qui vit autant que GdbTrace. PR_SET_PDEATHSIG suit le
    *thread* créateur, pas le processus : lancé depuis un thread de travail (connexion en
    tâche de fond du GUI), le serveur recevrait SIGTERM dès la fin de ce thread."""
    global _spawner
    with _spawner_lock:
        if _spawner is None:
            q: queue.Queue = queue.Queue()

            def run() -> None:
                while True:
                    args, kwargs, box, done = q.get()
                    try:
                        box.append(subprocess.Popen(args, **kwargs))
                    except BaseException as exc:  # noqa: BLE001 - renvoyée à l'appelant
                        box.append(exc)
                    done.set()
            threading.Thread(target=run, name="server-spawner", daemon=True).start()
            _spawner = q
    box: list = []
    done = threading.Event()
    _spawner.put((cmd, kw, box, done))
    done.wait()
    if isinstance(box[0], BaseException):
        raise box[0]
    return box[0]


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
    #: programme natif exécuté par gdb lui-même (NativeTarget) plutôt qu'une cible distante
    native = False
    #: dernières lignes écrites par le serveur sur stderr
    log: collections.deque = field(default_factory=lambda: collections.deque(maxlen=200),
                                   repr=False)

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

    def ports(self) -> list[int]:
        """Ports TCP que le serveur va ouvrir : vérifiés libres avant de le lancer."""
        return [self.gdb_port]

    def _check_ports(self) -> None:
        """Un port déjà tenu par un autre serveur (un OpenOCD resté en marche…) ferait échouer
        le nôtre en silence, et la session se connecterait à l'autre, sur une cible dans un
        état inconnu. SO_REUSEADDR comme OpenOCD : un port en TIME_WAIT reste utilisable."""
        for port in self.ports():
            if not port:
                continue
            with socket.socket() as s:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                try:
                    s.bind((self.host, port))
                except OSError:
                    raise RuntimeError(
                        f"port {port} déjà utilisé sur {self.host} : un autre serveur (OpenOCD, "
                        f"QEMU…) tourne-t-il encore ? L'arrêter, ou changer de port "
                        f"(--gdb-port)") from None

    def start(self, timeout: float = 10.0) -> None:
        cmd = self.command_line()
        if not shutil.which(cmd[0]):
            raise FileNotFoundError(f"{cmd[0]} introuvable dans le PATH")
        self._check_ports()
        self.proc = _spawn(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                           preexec_fn=_die_with_parent)
        # stderr est lu en continu : un tube plein (64 Kio) bloque tout le serveur, gdb et
        # port TCL compris, et OpenOCD y journalise toute la session.
        self.log.clear()
        drain = threading.Thread(target=self._drain, args=(self.proc.stderr,),
                                 name=f"{cmd[0]}-stderr", daemon=True)
        drain.start()
        try:
            _wait_port(self.host, self.gdb_port, timeout)
        except TimeoutError:
            if self.proc.poll() is not None:
                drain.join(timeout=1)     # le serveur est mort : sa sortie est complète
            err = "\n".join(list(self.log)[-30:])
            self.stop()
            raise RuntimeError(f"échec du démarrage de {cmd[0]}: {err}") from None

    def _drain(self, pipe) -> None:
        for line in pipe:
            self.log.append(line.decode(errors="replace").rstrip())

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
    #: MTB simulée (Cortex-M0+) : même journal que l'ETM simulé, tampon de `mtb_sim` octets
    #: (None : pas de MTB simulée ; 0 : tampon illimité, toute l'exécution)
    mtb_sim: int | None = None
    #: MTB simulée en mode AUTOHALT (arrêts internes, trace complète)
    mtb_autohalt: bool = False

    #: second port QMP réservé au contrôle de la machine (reset) : le premier sert au canal
    #: live, et un serveur QMP n'accepte qu'un client à la fois
    ctl_port: int = 0
    #: image chargée par QEMU (-kernel), rechargée par QEMU à chaque reset de la machine
    kernel: str = field(default="", repr=False)
    _ctl: QemuQmp | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if not self.qmp_port:
            self.qmp_port = free_port()
        if not self.ctl_port:
            self.ctl_port = free_port()

    def start(self, timeout: float = 10.0) -> None:
        self.kernel = self.elf
        super().start(timeout)

    def stop(self) -> None:
        if self._ctl is not None:
            self._ctl.close()
            self._ctl = None
        super().stop()

    def reset(self, session) -> None:
        """Reset de la machine (périphériques, NVIC, mode du cœur), cible arrêtée au vecteur de
        reset. Le gdbstub de QEMU ne sait pas faire « reset halt » : reset QMP, puis, si un autre
        ELF a été programmé depuis le lancement, on le réécrit (QEMU recharge son -kernel)."""
        try:
            if self._ctl is None:
                self._ctl = QemuQmp(self.host, self.ctl_port)
            self._ctl.hmp("system_reset")
        except OSError:
            pass                                # QEMU sans port de contrôle : reset logiciel
        else:
            if session.elf and self.kernel and os.path.abspath(session.elf) != os.path.abspath(self.kernel):
                session.load()
        session.reset_from_vectors()

    def command_line(self) -> list[str]:
        return [
            self.qemu, "-M", self.machine, "-nographic", "-monitor", "none",
            "-serial", "none", "-kernel", self.elf,
            "-gdb", f"tcp:{self.host}:{self.gdb_port}", "-S",
            "-qmp", f"tcp:{self.host}:{self.qmp_port},server=on,wait=off",
            "-qmp", f"tcp:{self.host}:{self.ctl_port},server=on,wait=off",
        ] + (["-accel", "tcg,one-insn-per-tb=on"] if self.etm_sim or self.mtb_sim is not None else [])

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

    def ports(self) -> list[int]:
        return [self.gdb_port, self.tcl_port, self.telnet_port]

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
class NativeTarget(TargetServer):
    """Programme natif (hôte : x86-64, AArch64…) exécuté sous gdb lui-même : ni serveur gdb
    ni chargement. La session le démarre arrêté au début de `main` (`-exec-run --start`) ;
    `args` : arguments du programme, `cwd` : son répertoire de travail. La mémoire se lit en
    marche dans `/proc/<pid>/mem`."""
    args: list[str] = field(default_factory=list)
    cwd: str | None = None
    needs_load: bool = False
    #: PID du programme débogué, tenu à jour par la session (None : pas démarré)
    pid: int | None = None
    native = True

    @property
    def remote(self) -> str:
        return "programme natif"

    def start(self, timeout: float = 10.0) -> None:
        pass

    def stop(self) -> None:
        pass

    def reset(self, session) -> None:
        """Pas de reset matériel : le programme est relancé, arrêté au début de `main`."""
        session.restart()

    def live_memory(self) -> LiveMemory | None:
        return ProcMem(lambda: self.pid)


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
