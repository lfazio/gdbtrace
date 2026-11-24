"""Canal mémoire en direct : regroupement, poller, client TCL OpenOCD (faux serveur et vrai
OpenOCD sans sonde), sortie des serveurs gdb, QEMU QMP."""
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass

import pytest
from conftest import needs_qemu

from gdbtrace.core.live import (LiveMemoryError, LivePoller, OpenOcdTcl, merge_ranges,
                                slice_ranges)
from gdbtrace.core.target import TargetServer, _wait_port, free_port


# ---------------------------------------------------------------------- regroupement
def test_merge_ranges():
    assert merge_ranges([(0x100, 4), (0x104, 4), (0x200, 8)]) == [(0x100, 8), (0x200, 8)]
    assert merge_ranges([(0x100, 4), (0x110, 4)], gap=16) == [(0x100, 0x14)]
    assert merge_ranges([(0x100, 4), (0x120, 4)], gap=16) == [(0x100, 4), (0x120, 4)]
    assert merge_ranges([(0x10, 0), (0x8, 16), (0x0, 4)]) == [(0x0, 0x18)]


def test_slice_ranges():
    blocks = {(0x100, 8): bytes(range(8))}
    assert slice_ranges(blocks, [(0x102, 2), (0x200, 4)]) == {0x102: b"\x02\x03"}


# ---------------------------------------------------------------------- poller (lecteur factice)
class FakeMemory:
    def __init__(self):
        self.calls = []
        self.fail = False

    def read(self, addr, n):
        self.calls.append((addr, n))
        if self.fail:
            raise LiveMemoryError("boom")
        return bytes((addr + i) & 0xFF for i in range(n))

    def close(self):
        pass


def test_poller_only_while_running_and_merges():
    mem, running, got, errors = FakeMemory(), [False], [], []
    p = LivePoller(mem, lambda: running[0], lambda fn, *a: fn(*a), period_ms=10,
                   on_error=errors.append)
    p.subscribe("a", [(0x10, 2), (0x12, 2)], got.append)
    p.start()
    try:
        time.sleep(0.08)
        assert got == [] and mem.calls == []          # cible arrêtée : rien
        running[0] = True
        time.sleep(0.08)
        assert got and got[-1] == {0x10: b"\x10\x11", 0x12: b"\x12\x13"}
        assert set(mem.calls) == {(0x10, 4)}           # deux plages, une seule lecture
        mem.fail = True
        time.sleep(0.08)
        assert errors and len(errors) == 1             # erreur signalée une seule fois
        p.unsubscribe("a")
        n = len(got)
        mem.fail = False
        time.sleep(0.05)
        assert len(got) == n
    finally:
        p.stop()


# ---------------------------------------------------------------------- OpenOCD TCL (faux serveur)
class FakeOpenOcd(threading.Thread):
    """Imite le serveur TCL d'OpenOCD : commandes et réponses terminées par 0x1A."""

    def __init__(self):
        super().__init__(daemon=True)
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(1)
        self.port = self.sock.getsockname()[1]
        self.commands = []
        self.words: dict[int, int] = {}      # mots 32 bits écrits/préchargés

    def run(self):
        conn, _ = self.sock.accept()
        buf = b""
        while True:
            data = conn.recv(4096)
            if not data:
                return
            buf += data
            while b"\x1a" in buf:
                cmd, _, buf = buf.partition(b"\x1a")
                cmd = cmd.decode()
                self.commands.append(cmd)
                conn.sendall(self.reply(cmd).encode() + b"\x1a")

    def reply(self, cmd):
        w = re.search(r"write_memory (\S+) 32 \{(0x[0-9a-fA-F]+)\}", cmd)
        if w:
            self.words[int(w.group(1), 16)] = int(w.group(2), 16)
            return "OK"
        m = re.search(r"read_memory (\S+) (\d+) (\d+)", cmd)
        if not m:
            return "invalid command name"
        addr, width, count = int(m.group(1), 16), int(m.group(2)), int(m.group(3))
        if 0xDEAD0000 <= addr < 0xDEAE0000:
            return "ERR read_memory: failed to read memory"
        step = width // 8
        if width == 32 and count == 1 and addr in self.words:
            return hex(self.words[addr])
        return " ".join(hex((addr + i * step) & ((1 << width) - 1)) for i in range(count))


def test_openocd_tcl_client():
    srv = FakeOpenOcd()
    srv.start()
    tcl = OpenOcdTcl("127.0.0.1", srv.port)
    try:
        # aligné : accès 32 bits, petit-boutiste
        assert tcl.read(0x20000000, 8) == (0x20000000).to_bytes(4, "little") + (0x20000004).to_bytes(4, "little")
        assert "read_memory 0x20000000 32 2" in srv.commands[-1]
        # non aligné : accès octets
        assert tcl.read(0x20000001, 3) == bytes([0x01, 0x02, 0x03])
        assert "read_memory 0x20000001 8 3" in srv.commands[-1]
        with pytest.raises(LiveMemoryError, match="failed to read memory"):
            tcl.read(0xDEAD0000, 4)
        # le client reste utilisable après une erreur
        assert len(tcl.read(0x1000, 4)) == 4
    finally:
        tcl.close()


def test_openocd_pcsr_sampling():
    srv = FakeOpenOcd()
    srv.words = {0xE000EDFC: 0x00000000, 0xE000101C: 0x08000ABC}
    srv.start()
    tcl = OpenOcdTcl("127.0.0.1", srv.port)
    try:
        assert tcl.sample_pc() == 0x08000ABC
        assert srv.words[0xE000EDFC] == 1 << 24              # DEMCR.TRCENA activé une fois
        n = len(srv.commands)
        srv.words[0xE000101C] = 0xFFFFFFFF                   # pas d'échantillon (cœur arrêté)
        assert tcl.sample_pc() is None
        assert len(srv.commands) == n + 1                    # TRCENA n'est plus relu
    finally:
        tcl.close()


# ---------------------------------------------------------------------- vrai OpenOCD, sans sonde
@pytest.mark.skipif(not shutil.which("openocd"), reason="openocd absent")
def test_openocd_tcl_results_not_echoed():
    """OpenOCD (0.12) recopie dans son journal le résultat de chaque commande TCL : sur
    stderr, et vers la console gdb tant que la cible tourne. `query()` l'évite.

    OpenOCD en `noinit` sert le port TCL sans sonde. Son stderr n'est volontairement pas lu,
    comme le faisait TargetServer : avec l'écho, ~11 Kio par commande le bloquaient après
    5 commandes (le tube fait 64 Kio), lectures TCL et arrêt de la cible compris."""
    port = free_port()
    proc = subprocess.Popen(["openocd", "-c", f"tcl_port {port}", "-c", "telnet_port disabled",
                             "-c", "gdb_port disabled", "-c", "noinit"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    tcl = OpenOcdTcl("127.0.0.1", port)
    try:
        _wait_port("127.0.0.1", port, 5)
        for _ in range(50):                                  # ≈ 560 Kio de résultats
            assert tcl.query('string repeat "0xa5a5a5a5 " 1024').split() == ["0xa5a5a5a5"] * 1024
        assert tcl.query("nosuchcmd 1").startswith("ERR invalid command name")
        assert tcl.query("list OK") == "OK"
    finally:
        tcl.close()
        proc.kill()
        log = proc.communicate(timeout=5)[1].decode(errors="replace")
    assert "0xa5a5a5a5" not in log and "OK" not in log.split()


# ---------------------------------------------------------------------- sortie des serveurs gdb
def _server(script: str, port: int) -> TargetServer:
    @dataclass
    class Server(TargetServer):
        def command_line(self) -> list[str]:
            return [sys.executable, "-c", script]
    return Server(gdb_port=port)


def test_server_stderr_is_drained():
    """Un serveur bavard sur stderr ne se bloque pas quand le tube (64 Kio) est plein."""
    port = free_port()
    t = _server("import socket, sys, time\n"
                "for i in range(20000): sys.stderr.write(f'Info : ligne {i}\\n')\n"
                "sys.stderr.flush()\n"
                f"s = socket.socket(); s.bind(('127.0.0.1', {port})); s.listen(1)\n"
                "time.sleep(30)\n", port)
    try:
        t.start(timeout=5)
        deadline = time.monotonic() + 2
        while t.log[-1] != "Info : ligne 19999" and time.monotonic() < deadline:
            time.sleep(0.01)
        assert t.log[-1] == "Info : ligne 19999" and len(t.log) == t.log.maxlen
    finally:
        t.stop()


def test_server_start_failure_reports_stderr():
    t = _server("import sys; sys.stderr.write('Error: open failed\\n'); sys.exit(1)", free_port())
    with pytest.raises(RuntimeError, match="Error: open failed"):
        t.start(timeout=1)


# ---------------------------------------------------------------------- QEMU QMP
@needs_qemu
def test_qemu_live_read_while_running(session):
    lm = session.target.live_memory()
    try:
        addr = session.address_of("ticks")
        assert lm.read(addr, 4) == session.read_memory(addr, 4)   # cohérent avec gdb à l'arrêt
        session.go()
        time.sleep(0.2)
        assert session.state == "running"
        t1 = int.from_bytes(lm.read(addr, 4), "little")
        time.sleep(0.1)
        t2 = int.from_bytes(lm.read(addr, 4), "little")
        assert t2 > t1 > 0                                         # SysTick à 1 kHz
        # bloc privé Cortex-M (SysTick), lisible lui aussi en marche
        ctrl = int.from_bytes(lm.read(0xE000E010, 4), "little")
        assert ctrl & 7 == 7
        # lecture non alignée et lecture longue (plusieurs blocs)
        assert len(lm.read(0x20000001, 7)) == 7
        assert len(lm.read(0x20000000, 9000)) == 9000
    finally:
        lm.close()


def test_server_refuses_a_port_already_in_use():
    """Un autre serveur tient le port (OpenOCD resté en marche) : erreur claire au lancement,
    au lieu de se connecter en silence à l'autre serveur."""
    with socket.socket() as other:
        other.bind(("127.0.0.1", 0))
        other.listen(1)
        port = other.getsockname()[1]
        t = _server("import time; time.sleep(30)", port)
        with pytest.raises(RuntimeError, match=f"port {port} déjà utilisé"):
            t.start(timeout=1)
        assert t.proc is None


def test_server_dies_with_gdbtrace(tmp_path):
    """Un GdbTrace tué brutalement n'abandonne pas son serveur (OpenOCD orphelin : sonde et
    ports tenus, session suivante connectée à lui)."""
    port, pidfile = free_port(), tmp_path / "pid"
    server = (f"import os, socket, time\nopen({str(pidfile)!r}, 'w').write(str(os.getpid()))\n"
              f"s = socket.socket(); s.bind(('127.0.0.1', {port})); s.listen(1); time.sleep(60)\n")
    gdbtrace = tmp_path / "launcher.py"           # le « GdbTrace » qui lance le serveur
    gdbtrace.write_text(f"import sys, time\nsys.path.insert(0, {os.path.dirname(__file__)!r})\n"
                        f"from test_live import _server\n_server({server!r}, {port}).start(timeout=5)\n"
                        f"time.sleep(60)\n")
    gt = subprocess.Popen([sys.executable, str(gdbtrace)], cwd=os.path.dirname(os.path.dirname(__file__)))
    deadline = time.monotonic() + 10
    while not pidfile.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    server_pid = int(pidfile.read_text())
    gt.kill()                                             # sans aucun nettoyage
    gt.wait()
    deadline = time.monotonic() + 5
    while os.path.exists(f"/proc/{server_pid}") and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not os.path.exists(f"/proc/{server_pid}")


def test_server_outlives_the_thread_that_started_it():
    """Le GUI se connecte dans un thread de travail : le serveur doit lui survivre
    (PR_SET_PDEATHSIG suit le thread créateur ; sans précaution, OpenOCD recevait SIGTERM à
    la fin de la connexion, et gdb perdait la cible : « No registers »)."""
    port = free_port()
    t = _server("import socket, time\ns = socket.socket(); s.bind(('127.0.0.1', %d)); "
                "s.listen(1); time.sleep(30)" % port, port)
    th = threading.Thread(target=t.start, kwargs={"timeout": 5})
    th.start()
    th.join()
    try:
        time.sleep(0.5)
        assert t.proc is not None and t.proc.poll() is None
    finally:
        t.stop()
