"""Vue Terminal (core/terminal.py) : semihosting servi par QEMU sur une socket, RTT lu et écrit
par le canal mémoire ou par gdb ; démo `examples/terminal`."""
import os
import subprocess
import sys

import pytest
from conftest import ROOT, needs_qemu

from gdbtrace.core.live import LiveMemory, OpenOcdTcl
from gdbtrace.core.terminal import RTT_ID, RttChannel, Terminal

CB = 0x2000_0000
UP, DOWN = 0x2000_0100, 0x2000_0200


class Ram:
    """Bloc de contrôle RTT en mémoire factice : un canal montant de 16 octets, un descendant
    de 8."""

    def __init__(self):
        self.mem = bytearray(0x400)
        self.put(CB, RTT_ID + b"\0" * 6 + (1).to_bytes(4, "little") + (1).to_bytes(4, "little"))
        self.desc(0, UP, 16)
        self.desc(1, DOWN, 8)

    def put(self, addr, data):
        self.mem[addr - CB:addr - CB + len(data)] = data

    def u32(self, addr):
        return int.from_bytes(self.mem[addr - CB:addr - CB + 4], "little")

    def desc(self, i, buf, size, wr=0, rd=0):
        at = CB + 24 + 24 * i
        self.put(at, b"".join(v.to_bytes(4, "little") for v in (0, buf, size, wr, rd, 0)))

    def read(self, addr, n):
        return bytes(self.mem[addr - CB:addr - CB + n])

    def target_writes(self, text: bytes):
        at = CB + 24
        wr = self.u32(at + 12)
        for b in text:
            self.put(UP + wr, bytes([b]))
            wr = (wr + 1) % 16
        self.put(at + 12, wr.to_bytes(4, "little"))


def test_rtt_poll_wraps_and_advances_rdoff():
    ram, ch = Ram(), RttChannel(CB)
    ram.target_writes(b"bonjour ")
    assert ch.poll(ram.read, ram.put) == "bonjour "
    assert ram.u32(CB + 24 + 16) == 8                       # RdOff avancé
    ram.target_writes("éàù\n".encode())                    # franchit la fin du tampon
    assert ch.poll(ram.read, ram.put) == "éàù\n" and ch.poll(ram.read, ram.put) == ""


def test_rtt_send_respects_free_space():
    ram, ch = Ram(), RttChannel(CB)
    assert ch.send(ram.read, ram.put, b"abcdefghij") == 7  # 8 octets, un toujours libre
    assert ram.read(DOWN, 7) == b"abcdefg" and ram.u32(CB + 48 + 12) == 7
    blank = RttChannel(CB + 0x300)
    assert blank.poll(ram.read, ram.put) == ""              # bloc pas encore initialisé
    with pytest.raises(RuntimeError, match="pas de canal descendant"):
        blank.send(ram.read, ram.put, b"x")


def test_terminal_text_by_source_and_new_text():
    t = Terminal()
    seen = []
    t.listeners.append(lambda text, src: seen.append(src))
    t.feed("a", "rtt")
    t.feed("b", "semihosting")
    assert t.take_new() == "ab" and t.take_new() == ""
    t.feed("c", "rtt")
    assert t.text("rtt") == "ac" and t.take_new() == "c" and seen == ["rtt", "semihosting", "rtt"]
    t.clear()
    assert t.text() == "" and t.take_new() == ""


def test_openocd_tcl_write_is_word_or_byte():
    sent = []

    class Fake(OpenOcdTcl):
        def query(self, script):
            sent.append(script)
            return "OK"
    f = Fake.__new__(Fake)
    f.write(0x20000010, b"\x01\x00\x00\x00")
    f.write(0x20000013, b"ab")
    assert sent[0].startswith("write_memory 0x20000010 32 {0x1}") and "8 {0x61 0x62}" in sent[1]
    assert f.can_write and not LiveMemory().can_write


SCRIPT = """
import time
gdbtrace.go(until="main"); gdbtrace.wait()
gdbtrace.set("semihosting_on", 1)
gdbtrace.go(); time.sleep(0.8); gdbtrace.halt(); gdbtrace.wait()
gdbtrace.terminal(quiet=True)
assert "ligne 0\\n" in gdbtrace.term.text("semihosting"), gdbtrace.term.text()[:200]
assert "ligne 0\\n" in gdbtrace.term.text("rtt"), gdbtrace.term.text("rtt")[:200]
assert gdbtrace.terminal_send("Z") == "rtt"
gdbtrace.go(); time.sleep(0.3); gdbtrace.halt(); gdbtrace.wait()
gdbtrace.terminal(quiet=True)
assert "écho : Z" in gdbtrace.term.text("rtt"), gdbtrace.term.text("rtt")[-300:]
print("OK")
"""


@needs_qemu
def test_semihosting_and_rtt_under_qemu(terminal_elf, tmp_path):
    """Semihosting servi par QEMU (socket) ; RTT lu et écrit par gdb à chaque arrêt (QMP
    n'écrit pas) : texte reçu, et écho de ce qui est envoyé."""
    script = tmp_path / "s.py"
    script.write_text(SCRIPT)
    r = subprocess.run([sys.executable, "-m", "gdbtrace", terminal_elf, "--batch", "--semihosting",
                        "--rtt", "--script", str(script)],
                       cwd=ROOT, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0 and "OK" in r.stdout, r.stdout + r.stderr
    assert "semihosting : servi par QEMU" in r.stdout and "lu aux arrêts seulement" in r.stdout


@needs_qemu
def test_terminal_view(terminal_elf, tmp_path):
    if not os.environ.get("DISPLAY"):
        pytest.skip("pas de DISPLAY")
    from test_gui import close_app, pump

    from gdbtrace.__main__ import build_parser, setup_terminal
    from gdbtrace.core.session import Session
    from gdbtrace.core.target import QemuTarget, free_port
    from gdbtrace.gui.app import App
    from gdbtrace.views.terminal import TerminalView
    a = App(layout_file=str(tmp_path / "layout.json"))
    try:
        target = QemuTarget(elf=terminal_elf, gdb_port=free_port(), semihosting_port=free_port())
        s = Session()
        s.connect(terminal_elf, target)
        s.break_("main", temporary=True)
        s.go()
        s.wait_stopped(10)
        a.attach(s)
        assert pump(a, lambda: s.state == "stopped")
        args = build_parser().parse_args([terminal_elf, "--semihosting", "--rtt"])
        setup_terminal(args, a.api.term, s, target, a.api.live, a.symbols, print)
        s.set_var("semihosting_on", "1")
        v = a.views.show(TerminalView, "bottom")
        a.go()
        assert pump(a, lambda: "ligne 3" in v.text.get("1.0", "end"), timeout=10)   # semihosting, en marche
        a.halt()
        assert pump(a, lambda: s.state == "stopped")
        assert pump(a, lambda: "ligne" in a.api.term.text("rtt"), timeout=5)    # RTT lu à l'arrêt
        assert "semihosting" in v.info.cget("text") and "RTT" in v.info.cget("text")
        v.input_var.set("Q")
        v._send()
        assert v.input_var.get() == "" and "rtt" in a.status.get()
    finally:
        close_app(a)
