"""Lancement du serveur gdb côté cible : QEMU (sans matériel) ou OpenOCD (sonde réelle)."""
from __future__ import annotations

import collections
import glob
import os
import re
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

    def after_load(self, session) -> None:
        """Après l'écriture de l'ELF à la connexion (`load`) : rien par défaut (flash : le
        reset suivant démarre l'image ; QEMU l'a chargée lui-même)."""

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
    #: semihosting traité par QEMU, entrées-sorties sur ce port TCP (0 : pas de semihosting)
    semihosting_port: int = 0

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
        ] + (["-accel", "tcg,one-insn-per-tb=on"] if self.etm_sim or self.mtb_sim is not None else []) + (
            ["-semihosting-config", "enable=on,target=native,chardev=gdbtrace-sh",
             "-chardev", f"socket,id=gdbtrace-sh,host={self.host},port={self.semihosting_port},server=on,wait=off"]
            if self.semihosting_port else [])

    def live_memory(self) -> LiveMemory | None:
        return QemuQmp(self.host, self.qmp_port)


#: sondes ST-LINK (idProduct USB, fabricant 0483)
STLINK_PIDS = {"3748": "ST-LINK/V2", "374b": "ST-LINK/V2-1", "3752": "ST-LINK/V2-1",
               "374e": "STLINK-V3", "374f": "STLINK-V3", "3753": "STLINK-V3", "3754": "STLINK-V3"}
#: IDCODE du port de débogage (SW-DP) → cœur, pour dire quelle puce a répondu
DP_CORES = {0x0BB11477: "Cortex-M0", 0x0BC11477: "Cortex-M0+", 0x1BA01477: "Cortex-M3",
            0x2BA01477: "Cortex-M3/M4", 0x5BA02477: "Cortex-M7", 0x6BA02477: "Cortex-M33/M55"}


def stlink_probes() -> list[tuple[str, str]]:
    """[(numéro de série, produit)] des sondes ST-LINK branchées (lues dans /sys)."""
    out = []
    for dev in glob.glob("/sys/bus/usb/devices/*"):
        try:
            with open(os.path.join(dev, "idVendor")) as f:
                vid = f.read().strip()
            with open(os.path.join(dev, "idProduct")) as f:
                pid = f.read().strip()
            with open(os.path.join(dev, "serial")) as f:
                serial = f.read().strip()
        except OSError:
            continue
        if vid == "0483" and pid in STLINK_PIDS:
            out.append((serial, STLINK_PIDS[pid]))
    return sorted(out)


def _core_of(idcode: int) -> str:
    name = DP_CORES.get(idcode)
    return f"{name} (IDCODE {idcode:#010x})" if name else f"IDCODE {idcode:#010x}"


def explain_openocd_failure(log: list[str], configs: list[str],
                            serial: str | None = None) -> str | None:
    """Cause lisible d'un OpenOCD qui n'a pas démarré, d'après son journal, ou None. La plus
    fréquente avec plusieurs cartes branchées : OpenOCD a pris la première sonde venue, et
    la puce derrière n'est pas celle de la configuration."""
    text = "\n".join(log)
    probes = stlink_probes()
    listing = ", ".join(f"{sn} ({prod})" for sn, prod in probes) or "aucune trouvée dans /sys"
    which = f"la sonde {serial}" if serial else "la première sonde trouvée (STLINK_SERIAL non défini)"
    cfg = ", ".join(os.path.basename(c) for c in configs)
    got = re.search(r"UNEXPECTED idcode: (0x[0-9a-fA-F]+)", text)
    want = re.search(r"expected \d+ of \d+: (0x[0-9a-fA-F]+)", text)
    if got and want:
        return (f"mauvaise sonde : derrière {which}, la puce est un {_core_of(int(got.group(1), 16))}, "
                f"alors que {cfg} attend un {_core_of(int(want.group(1), 16))}.\n"
                f"Sondes ST-LINK branchées : {listing}.\n"
                "Choisir celle de la carte : STLINK_SERIAL=<numéro> devant la commande.")
    opened = re.search(r"STLINK V\d|ST-LINK V\d", text)
    if (re.search(r"LIBUSB_ERROR_BUSY|LIBUSB_ERROR_ACCESS|device is busy|claim.*interface", text, re.I)
            or ("clock speed" in text and not opened)):    # bloqué avant d'ouvrir la sonde
        return (f"{which} est déjà prise par un autre programme (une autre session GdbTrace, "
                f"OpenOCD, STM32CubeIDE ?) : la libérer, ou en choisir une autre.\n"
                f"Sondes ST-LINK branchées : {listing}.")
    if serial and re.search(r"open failed|unable to open|no device found", text, re.I):
        return (f"aucune sonde de numéro de série {serial} (STLINK_SERIAL).\n"
                f"Sondes ST-LINK branchées : {listing}.")
    return None


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

    def start(self, timeout: float = 10.0) -> None:
        try:
            super().start(timeout)
        except RuntimeError as exc:
            why = explain_openocd_failure(list(self.log), self.configs,
                                          os.environ.get("STLINK_SERIAL") or None)
            if why is None:
                raise
            raise RuntimeError(why) from exc


def _latest(patterns: list[str]) -> str | None:
    """Chemin le plus récent (ordre des noms de version) parmi les motifs glob."""
    import glob
    found = sorted({p for pat in patterns for p in glob.glob(os.path.expanduser(pat))})
    return found[-1] if found else None


def find_stlink_gdbserver() -> str | None:
    """ST-LINK_gdbserver : PATH, puis STM32CubeIDE et STM32CubeCLT (installations Linux)."""
    return shutil.which("ST-LINK_gdbserver") or _latest([
        f"{root}/stm32cubeide*/plugins/*stlink-gdb-server*/tools/bin/ST-LINK_gdbserver"
        for root in ("/opt/st", "~/st")] + [
        f"{root}/stm32cubeclt*/STLink-gdb-server/bin/ST-LINK_gdbserver"
        for root in ("/opt/st", "~/st")])


def find_cubeprogrammer() -> str | None:
    """Dossier `bin` de STM32CubeProgrammer, dont ST-LINK_gdbserver se sert (-cp)."""
    found = _latest([
        f"{root}/stm32cubeide*/plugins/*cubeprogrammer*/tools/bin/STM32_Programmer_CLI"
        for root in ("/opt/st", "~/st")] + [
        f"{root}/stm32cubeclt*/STM32CubeProgrammer/bin/STM32_Programmer_CLI"
        for root in ("/opt/st", "~/st")] + [
        "~/STMicroelectronics/STM32Cube/STM32CubeProgrammer/bin/STM32_Programmer_CLI"])
    if found:
        return os.path.dirname(found)
    cli = shutil.which("STM32_Programmer_CLI")
    return os.path.dirname(os.path.realpath(cli)) if cli else None


#: sections et symboles usuels de la table des vecteurs (CMSIS, STM32Cube, exemples GdbTrace)
VECTOR_SECTIONS = (".isr_vector", ".vectors", ".intvec", ".vector_table")
VECTOR_SYMBOLS = ("g_pfnVectors", "__Vectors", "__isr_vector", "__vector_table", "vector_table",
                  "_vectors")


def vector_table_address(elf: str) -> int | None:
    """Adresse de la table des vecteurs de l'ELF (section, sinon symbole), None si inconnue."""
    from elftools.elf.elffile import ELFFile
    with open(elf, "rb") as fh:
        e = ELFFile(fh)
        for name in VECTOR_SECTIONS:
            sec = e.get_section_by_name(name)
            if sec is not None and sec["sh_size"]:
                return sec["sh_addr"]
        symtab = e.get_section_by_name(".symtab")
        if symtab is not None:
            for name in VECTOR_SYMBOLS:
                syms = symtab.get_symbol_by_name(name)
                if syms:
                    return syms[0]["st_value"]
    return None


@dataclass
class StLinkTarget(TargetServer):
    """ST-LINK_gdbserver (STM32CubeIDE, STM32CubeCLT), derrière un relais RSP
    (`core/rsp_relay.py`) : gdb se connecte au relais (`gdb_port`), qui parle au serveur
    (`server_port`). Le relais donne la mémoire en direct (le serveur n'a pas de port TCL) et
    le reset système.

    `apid` : port d'accès du cœur (-m ; STM32N6 : 1, le cœur n'est pas sur l'AP 0).
    `ram_boot` : l'image s'exécute en RAM, chargée par le débogueur (STM32N6 en démarrage de
    développement, sans flash interne) : reset système, la ROM de démarrage tourne un instant,
    puis chargement, et sp / pc / VTOR pris dans la table des vecteurs de l'ELF — ce que fait
    l'IDE, et que la ROM ne fait pas. Sinon (flash), le reset arrête le cœur au vecteur de
    reset (DEMCR.VC_CORERESET)."""
    server: str = ""
    programmer: str = ""
    serial: str | None = None
    apid: int = 0
    ram_boot: bool = False
    server_port: int = 0
    #: SWO capturé par la sonde et servi en TCP par le serveur (-z ; 0 : pas de SWO)
    swo_port: int = 0
    cpu_hz: int = 0
    swo_hz: int = 2_000_000
    relay: "object | None" = field(default=None, repr=False)
    #: le CRC du serveur (`compare-sections`, qCRC) est faux sur un STM32N6 alors que la
    #: mémoire est juste : la programmation se vérifie en relisant les sections
    verify_by_read = True

    def ports(self) -> list[int]:
        return [self.gdb_port, self.server_port, self.swo_port]

    @property
    def swo_div(self) -> int:
        """Diviseur donné au serveur (-b) : la sonde attend cpu_hz / swo_div bit/s."""
        return max(1, round(self.cpu_hz / self.swo_hz)) if self.cpu_hz else 1

    @property
    def swo_baud(self) -> int:
        return round(self.cpu_hz / self.swo_div) if self.cpu_hz else 0

    def command_line(self) -> list[str]:
        server = self.server or find_stlink_gdbserver()
        if not server:
            raise FileNotFoundError("ST-LINK_gdbserver introuvable (PATH, STM32CubeIDE ou "
                                    "STM32CubeCLT sous /opt/st ou ~/st)")
        programmer = self.programmer or find_cubeprogrammer()
        if not programmer:
            raise FileNotFoundError("STM32CubeProgrammer introuvable : ST-LINK_gdbserver en a "
                                    "besoin (-cp)")
        # -e : persistant (le relais se reconnecte après un reset système) ; -g : sans reset
        # à la connexion (gdb arrête le cœur, la ROM d'une N6 refuse un arrêt au démarrage)
        cmd = [server, "-p", str(self.server_port), "-l", "1", "-d", "-e", "-g",
               "-m", str(self.apid), "-cp", programmer]
        if self.serial:
            cmd += ["-i", self.serial]
        if self.swo_port:
            cmd += ["-z", str(self.swo_port), "-a", str(self.cpu_hz), "-b", str(self.swo_div)]
        return cmd

    def start(self, timeout: float = 15.0) -> None:
        from .rsp_relay import RspRelay
        if not self.server_port:
            self.server_port = free_port()
        cmd = self.command_line()
        self._check_ports()
        self.log.clear()
        ready = threading.Event()
        self.proc = _spawn(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           preexec_fn=_die_with_parent)

        def drain() -> None:
            for line in self.proc.stdout:
                text = line.decode(errors="replace").rstrip()
                self.log.append(text)
                if "Waiting for debugger connection" in text:
                    ready.set()
            ready.set()
        threading.Thread(target=drain, name="stlink-stdout", daemon=True).start()
        # pas de sondage du port : une connexion arrête le cœur (le serveur la prend pour gdb)
        deadline = time.monotonic() + timeout
        while not ready.wait(0.1):
            if time.monotonic() > deadline:
                break
        if self.proc.poll() is not None or not any("Waiting for debugger" in ln for ln in self.log):
            err = "\n".join(list(self.log)[-30:])
            self.stop()
            raise RuntimeError(f"échec du démarrage de ST-LINK_gdbserver : {err}")
        self.relay = RspRelay((self.host, self.server_port), self.host, self.gdb_port,
                              log=self.log.append)
        self.gdb_port = self.relay.port
        self.relay.start()

    def stop(self) -> None:
        if self.relay is not None:
            self.relay.close()
            self.relay = None
        super().stop()

    def live_memory(self) -> LiveMemory | None:
        from .rsp_relay import RelayMemory
        return RelayMemory(self.relay) if self.relay is not None else None

    def reset(self, session) -> None:
        relay = self.relay
        catch = not self.ram_boot
        if catch:                               # flash : arrêt au vecteur de reset
            demcr = session.read_u32(DEMCR_ADDR)
            session.write_u32(DEMCR_ADDR, demcr | DEMCR_VC_CORERESET)
        relay.reset_system(settle=0.4 if self.ram_boot else 0.05)
        session.cli("flushregs")
        if catch:
            session.write_u32(DEMCR_ADDR, session.read_u32(DEMCR_ADDR) & ~DEMCR_VC_CORERESET)
        elif session.elf:
            session.load()
            self.after_load(session)

    def after_load(self, session) -> None:
        """Image en RAM : la démarrer à sa table des vecteurs (VTOR, sp, pc, xPSR mode
        Thread) ; le cœur reste arrêté."""
        if not self.ram_boot or not session.elf:
            return
        vt = vector_table_address(session.elf)
        if vt is None:
            raise RuntimeError("image en RAM : table des vecteurs introuvable dans l'ELF "
                               f"(sections {', '.join(VECTOR_SECTIONS)} ou symboles "
                               f"{', '.join(VECTOR_SYMBOLS[:3])}…)")
        session.write_u32(0xE000ED08, vt)
        session.write_register("sp", session.read_u32(vt))
        session.write_register("pc", session.read_u32(vt + 4) & ~1)
        session.write_register("xpsr", 0x01000000)
        session.cli("flushregs")


DEMCR_ADDR = 0xE000EDFC
DEMCR_VC_CORERESET = 1 << 0


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
    #: processus déjà lancé auquel s'attacher (`--pid`) : ni lancement ni relance ; à la
    #: déconnexion, il est détaché et continue
    attach_pid: int | None = None
    #: mode non-stop de gdb : un thread s'arrête et repart seul, les autres continuent
    non_stop: bool = False
    native = True

    @property
    def remote(self) -> str:
        return f"processus {self.attach_pid}" if self.attach_pid else "programme natif"

    def start(self, timeout: float = 10.0) -> None:
        pass

    def stop(self) -> None:
        pass

    def reset(self, session) -> None:
        """Pas de reset matériel : le programme est relancé, arrêté au début de `main`."""
        if self.attach_pid:
            raise RuntimeError(f"processus attaché ({self.attach_pid}) : GdbTrace ne l'a pas "
                               "lancé et ne peut pas le relancer")
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


@dataclass
class NativeRemoteTarget(RemoteTarget):
    """Programme natif servi par un gdbserver (autre machine, conteneur, ou celle-ci) :
    `gdbserver --multi HOST:PORT` (GdbTrace lance le programme, `exec_file` : son chemin
    côté serveur, l'ELF par défaut, avec `args` / `cwd`) ou `gdbserver HOST:PORT prog …`
    (déjà chargé : GdbTrace va jusqu'au début de `main`). Arrêt au début de `main` dans les
    deux cas. La mémoire se lit en marche par `/proc/<pid>/mem` quand le serveur est sur cette
    machine ; sinon, pas de vues en direct."""
    args: list[str] = field(default_factory=list)
    cwd: str | None = None
    exec_file: str | None = None
    needs_load: bool = False
    pid: int | None = None
    non_stop: bool = False
    native = True
    remote_native = True
    #: le serveur a lancé le programme lui-même (sans --multi) : pas de relance
    preloaded: bool = False

    def start(self, timeout: float = 10.0) -> None:
        """Pas de sondage du port : gdbserver prendrait la sonde pour son client, et ne
        servirait plus gdb (connexion expirée)."""

    def reset(self, session) -> None:
        session.restart()

    def live_memory(self) -> LiveMemory | None:
        if self.host in ("127.0.0.1", "localhost", "::1"):
            return ProcMem(lambda: self.pid)
        return None
