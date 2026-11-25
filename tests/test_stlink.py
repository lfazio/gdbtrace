"""ST-LINK_gdbserver et STM32N6, sans carte : relais RSP (contre un faux serveur gdb-remote),
décodage ARMv8.1-M (contre l'assembleur), capture ETR, SWO et DWT de l'ARMv8-M, options.

La même chose sur la vraie carte : tests/test_board_n6.py.
"""
import os
import re
import shutil
import socket
import subprocess
import threading
import time

import pytest
from conftest import ROOT

from gdbtrace.core.rsp_relay import (AIRCR, AIRCR_SYSRESETREQ, RelayMemory, RspRelay, frame,
                                     payload_of, take_token, unrle)


# ====================================================================== faux serveur gdb-remote
class FakeServer:
    """Serveur gdb-remote minimal à la manière de ST-LINK_gdbserver : répond aux `m` même cible
    en marche, perd la cible après un reset AIRCR (plus de reprise possible), la retrouve
    arrêtée à la connexion suivante. `console_before_next` : paquet « O » envoyé avant la
    réponse à la prochaine requête (texte du serveur hors d'une marche)."""

    def __init__(self):
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(2)
        self.port = self.sock.getsockname()[1]
        self.mem = bytearray(range(256)) * 64          # 16 Ko à l'adresse 0x1000
        self.running = False
        self.connections: list[list[str]] = []          # paquets reçus, par connexion
        self.console_before_next = False
        self.writes: list[tuple[int, bytes]] = []
        self._stop = False
        threading.Thread(target=self._serve, daemon=True).start()

    def close(self):
        self._stop = True
        self.sock.close()

    def _serve(self):
        while not self._stop:
            try:
                c, _ = self.sock.accept()
            except OSError:
                return
            got: list[str] = []
            self.connections.append(got)
            self._session(c, got)

    def _session(self, c, got):
        buf, noack = bytearray(), False

        def send(p):
            if self.console_before_next and not self.running:
                self.console_before_next = False
                c.sendall(frame("O" + "Target is not responding\n".encode().hex()))
            c.sendall(frame(p))
        while True:
            try:
                data = c.recv(65536)
            except OSError:
                return
            if not data:
                return
            buf += data
            while (tok := take_token(buf)) is not None:
                if tok in (b"+", b"-"):
                    continue
                if tok == b"\x03":
                    if self.running:
                        self.running = False
                        c.sendall(frame("S02"))
                    continue
                if not noack:
                    c.sendall(b"+")
                p = payload_of(tok).decode()
                got.append(p)
                if p.startswith("qSupported"):
                    send("PacketSize=4000;QStartNoAckMode+")
                elif p == "QStartNoAckMode":
                    send("OK")
                    noack = True
                elif p == "?":
                    send("S05")
                elif p == "g":
                    send("00" * 68)
                elif p.startswith("m"):
                    a, n = (int(x, 16) for x in p[1:].split(","))
                    ok = 0x1000 <= a and a + n <= 0x1000 + len(self.mem)
                    send(self.mem[a - 0x1000:a - 0x1000 + n].hex() if ok else "E31")
                elif p.startswith("M"):
                    head, data = p[1:].split(":")
                    a = int(head.split(",")[0], 16)
                    self.writes.append((a, bytes.fromhex(data)))
                    if 0x1000 <= a < 0x1000 + len(self.mem):
                        self.mem[a - 0x1000:a - 0x1000 + len(data) // 2] = bytes.fromhex(data)
                    send("OK")
                elif p in ("c", "vCont;c"):
                    self.running = True             # réponse : le paquet d'arrêt, plus tard
                elif p == "D":
                    send("OK")
                else:
                    send("")


class Gdb:
    """Client « gdb » brut : poignée de main en mode sans acquittement, puis requêtes."""

    def __init__(self, port):
        self.c = socket.create_connection(("127.0.0.1", port), timeout=3)
        self.buf = bytearray()
        self.noack = False
        self.ask("qSupported:swbreak+;hwbreak+")
        self.ask("QStartNoAckMode")
        self.noack = True
        self.ask("?")

    def send(self, p):
        self.c.sendall(frame(p))

    def reply(self, timeout=3.0):
        self.c.settimeout(timeout)
        while True:
            while (tok := take_token(self.buf)) is not None:
                if tok[:1] == b"$":
                    if not self.noack:
                        self.c.sendall(b"+")
                    return payload_of(tok).decode()
            self.buf += self.c.recv(65536)

    def ask(self, p):
        self.send(p)
        return self.reply()


@pytest.fixture
def relay():
    srv = FakeServer()
    log: list[str] = []
    r = RspRelay(("127.0.0.1", srv.port), log=log.append)
    r.test_log = log
    r.start()
    yield srv, r
    r.close()
    srv.close()


def test_relay_forwards_gdb_and_reads_memory_while_running(relay):
    srv, r = relay
    gdb = Gdb(r.port)
    assert gdb.ask("m1000,4") == "00010203"
    live = RelayMemory(r)
    assert live.read(0x1004, 4) == bytes([4, 5, 6, 7])     # gdb inactif, cible arrêtée
    gdb.send("c")
    time.sleep(0.1)
    assert r.running
    t0 = time.monotonic()
    assert live.read(0x1100, 0x1800) == bytes(srv.mem[0x100:0x1900])   # plusieurs paquets
    assert time.monotonic() - t0 < 2
    gdb.c.sendall(b"\x03")
    assert gdb.reply() == "S02"                             # l'arrêt revient à gdb
    assert not r.running
    assert gdb.ask("m1002,2") == "0203"


def test_relay_write_and_errors(relay):
    srv, r = relay
    gdb = Gdb(r.port)                                       # connexion gardée ouverte
    live = RelayMemory(r)
    live.write_u32(0x1000, 0xDEADBEEF)
    assert srv.writes[-1] == (0x1000, bytes.fromhex("efbeadde"))
    from gdbtrace.core.live import LiveMemoryError
    with pytest.raises(LiveMemoryError, match="E31"):
        live.read(0x9000, 4)
    assert gdb.ask("m1000,4") == "efbeadde"


def test_relay_drops_server_text_outside_a_run(relay):
    """Un paquet « O » hors d'une marche serait lu par gdb comme la réponse à sa requête
    (« Invalid hex digit 79 »)."""
    srv, r = relay
    gdb = Gdb(r.port)
    srv.console_before_next = True
    assert gdb.ask("m1000,2") == "0001"
    assert any("Target is not responding" in line for line in r_log(r))


def r_log(r):
    return r.test_log


def test_relay_reset_reconnects_and_replays_the_handshake(relay):
    srv, r = relay
    gdb = Gdb(r.port)
    r.reset_system(settle=0.05)
    assert srv.writes[-1] == (AIRCR, AIRCR_SYSRESETREQ.to_bytes(4, "little"))
    first, second = srv.connections[0], srv.connections[1]
    assert first[-1] == "D"                                 # détaché après le reset
    assert second[:3] == ["qSupported:swbreak+;hwbreak+", "QStartNoAckMode", "?"]
    assert gdb.ask("m1000,1") == "00"                       # gdb n'a rien vu
    assert len(srv.connections) == 2


def test_relay_fails_requests_without_gdb():
    srv = FakeServer()
    r = RspRelay(("127.0.0.1", srv.port))
    r.start()
    try:
        from gdbtrace.core.live import LiveMemoryError
        with pytest.raises(LiveMemoryError):
            RelayMemory(r).read(0x1000, 4)
    finally:
        r.close()
        srv.close()


def test_unrle():
    assert unrle(b"0* ") == b"0" * 4                        # ' ' = 32 → 3 de plus
    assert unrle(b"ab") == b"ab"


# ====================================================================== décodage ARMv8.1-M
LOB_SRC = """
.syntax unified
.thumb
f:
 wls lr, r0, 1f
2: adds r1, #1
 le lr, 2b
1: dls lr, r0
3: adds r1, #1
 le lr, 3b
 sg
 bxns lr
 blxns r3
 vldrw.u32 q0, [r1]
 dlstp.32 lr, r2
4: vstrw.32 q0, [r1], #16
 letp lr, 4b
 wlstp.8 lr, r2, 5f
 nop
5: bf 6f, 7f
 nop
6: nop
7: lctp
 le 2b
 bx lr
"""


def assemble(tmp_path, src):
    if not shutil.which("arm-none-eabi-as"):
        pytest.skip("arm-none-eabi-as absent")
    s, o, b = tmp_path / "t.s", tmp_path / "t.o", tmp_path / "t.bin"
    s.write_text(src)
    subprocess.run(["arm-none-eabi-as", "-mcpu=cortex-m55", "-mthumb", str(s), "-o", str(o)], check=True)
    subprocess.run(["arm-none-eabi-objcopy", "-O", "binary", str(o), str(b)], check=True)
    dis = subprocess.run(["arm-none-eabi-objdump", "-d", str(o)], capture_output=True, text=True).stdout
    ref = {}
    for line in dis.splitlines():
        m = re.match(r"\s+([0-9a-f]+):\s+(?:[0-9a-f]{4} ?){1,2}\s+(\S+)\s*(.*)", line)
        if m:
            ref[int(m.group(1), 16)] = (m.group(2), m.group(3))
    return b.read_bytes(), ref


def test_v81m_branches_match_the_assembler(tmp_path):
    from gdbtrace.trace.etm.image import DIRECT, INDIRECT, NORMAL, CodeImage, decode_v8m
    code, ref = assemble(tmp_path, LOB_SRC)
    img = CodeImage.from_segments([(0, code)], arch=21)
    seen = {}
    addr = 0
    while addr < len(code):
        ins = img.instr(addr)
        assert ins is not None, hex(addr)
        seen[ins.op or ref[addr][0]] = ins
        mnem, ops = ref[addr]
        if mnem in ("wls", "le", "letp", "wlstp"):
            target = int(re.search(r"([0-9a-f]+) <", ops).group(1), 16)
            assert (ins.kind, ins.target) == (DIRECT, target), (hex(addr), mnem, ins)
            assert ins.cond == (mnem != "le" or ops.startswith("lr"))
        elif mnem in ("bxns", "blxns"):
            assert ins.kind == INDIRECT and ins.call == (mnem == "blxns")
            assert ins.ret == (ops == "lr")
        elif mnem in ("dls", "dlstp", "lctp", "bf", "vldrw.u32", "vstrw.32", "sg"):
            assert ins.kind == NORMAL, (mnem, ins)
        addr += ins.size
    assert decode_v8m(0, code[:4]).op == "wls"
    assert img.instr(0).waypoint and not img.instr(0x0a).waypoint   # WLS oui, DLS non


def test_unreadable_instruction_v7m_versus_v8m():
    """ARMv7-M : une instruction que capstone ne lit pas reste un trou (désynchro, données) ;
    ARMv8-M : une instruction ordinaire de la taille de son encodage (Helium…)."""
    from gdbtrace.trace.etm.image import NORMAL, CodeImage
    code = [(0, bytes.fromhex("ffffffff"))]
    assert CodeImage.from_segments(code).instr(0) is None
    ins = CodeImage.from_segments(code, arch=21).instr(0)
    assert (ins.kind, ins.size) == (NORMAL, 4) and ins.text.startswith(".inst.w")


def test_n6_firmware_has_low_overhead_loops():
    if not shutil.which("arm-none-eabi-gcc"):
        pytest.skip("arm-none-eabi-gcc absent")
    d = os.path.join(ROOT, "examples", "nucleo-n6")
    subprocess.run(["make", "-s", "-C", d], check=True)
    from gdbtrace.core.symbols import SymbolTable
    from gdbtrace.core.target import vector_table_address
    from gdbtrace.trace.etm.image import CodeImage
    elf = os.path.join(d, "nucleo-n6.elf")
    img, sym = CodeImage(elf), SymbolTable(elf)
    assert img.v8m and img.arch == 21
    f = sym.function("lob_count")
    ops = [img.instr(a).op for a in range(f.start, f.end, 2) if img.instr(a) is not None]
    assert "dls" in ops and "le" in ops
    assert vector_table_address(elf) == sym.objects["vector_table"][0]
    from gdbtrace.trace.tmc import buffer_from_symbols
    assert buffer_from_symbols(sym) == (0x34010000, 256 * 1024)


# ====================================================================== capture ETR
class FakeTarget:
    """Registres et mémoire d'une cible arrêtée, pour EtrCapture."""

    def __init__(self, buffer, size):
        self.regs: dict[int, int] = {}
        self.mem = bytearray(size)
        self.buffer = buffer
        self.full = False

    def read_u32(self, addr):
        from gdbtrace.trace import tmc
        if addr in (tmc.TRCSTATR,):
            return 1
        if addr == 0xE0083000 + tmc.STS:
            return tmc.STS_READY | (tmc.STS_FULL if self.full else 0)
        if addr == 0xE0082000 + tmc.STS:
            return tmc.STS_READY
        return self.regs.get(addr, 0)

    def write_u32(self, addr, value):
        self.regs[addr] = value

    def read_memory(self, addr, n):
        off = addr - self.buffer
        return bytes(self.mem[off:off + n])


def test_etr_capture_setup_and_wrapped_buffer():
    from gdbtrace.trace import tmc
    from gdbtrace.trace.chips import STM32N6
    from gdbtrace.trace.tpiu import FULL_SYNC, frame_bytes
    buf, size = 0x34010000, 4096
    t = FakeTarget(buf, size)
    cap = tmc.EtrCapture(t, STM32N6, buf, size)
    log = cap.setup()
    etr, etf = STM32N6.etr, STM32N6.etf
    assert t.regs[0x54024C10] == 0x310                       # RIMU : sécurisé, privilégié, CID 1
    assert t.regs[0x44001004] & 0x300000 == 0x300000         # DBGCLKEN, TRACECLKEN
    assert t.regs[etr + tmc.RSZ] == size // 4 and t.regs[etr + tmc.DBALO] == buf
    assert t.regs[etr + tmc.MODE] == tmc.MODE_CB and t.regs[etf + tmc.MODE] == tmc.MODE_HWFIFO
    assert t.regs[etr + tmc.CTL] == 1 and t.regs[etf + tmc.CTL] == 1
    assert t.regs[STM32N6.funnel] == 0x301                   # ETM seul
    assert t.regs[STM32N6.replicator + 4] == 0xFF            # rien vers le TPIU
    assert any("256" not in line for line in log)
    # tampon qui a tourné : les trames les plus anciennes commencent au pointeur d'écriture
    payload = bytes(range(1, 200))
    frames = frame_bytes(2, payload)[len(FULL_SYNC):]
    k = len(frames) // 16 // 2 * 16
    t.mem[size - k:] = frames[:k]
    t.mem[:len(frames) - k] = frames[k:]
    t.regs[etr + tmc.RWP] = buf + len(frames) - k
    t.full = True
    assert cap.collect() == payload and cap.last_wrapped and cap.wraps == 1
    assert t.regs[tmc.TRCPRGCTLR] == 0                       # ETM coupé pour la lecture
    cap.restart()
    assert t.regs[tmc.TRCPRGCTLR] == 1 and t.regs[etr + tmc.RWP] == buf
    t.full = False
    t.mem[:len(frames)] = frames
    t.regs[etr + tmc.RWP] = buf + len(frames)
    assert cap.collect() == payload and not cap.last_wrapped
    assert "2 exécutions" in cap.describe() and "1 tampon plein" in cap.describe()


def test_etr_capture_rejects_a_misaligned_buffer():
    from gdbtrace.trace.chips import STM32N6
    from gdbtrace.trace.tmc import EtrCapture
    with pytest.raises(ValueError):
        EtrCapture(None, STM32N6, 0x34010004, 4096)


# ====================================================================== SWO, DWT, puce
def test_swo_of_the_n6_is_a_coresight_swo():
    from gdbtrace.trace.chips import STM32N6, detect_chip
    from gdbtrace.trace.config import SwoConfig
    cfg = SwoConfig(cpu_hz=64_000_000, swo_hz=2_000_000, chip=STM32N6)
    writes = {w.name: (w.addr, w.value) for w in cfg.register_writes()}
    assert writes["SWO_CODR"] == (0xE008A010, 3)            # ck_trace = cœur / 8 = 8 MHz
    assert writes["SWO_SPPR"] == (0xE008A0F0, 2)            # NRZ
    assert writes["DBGMCU_CR"] == (0x44001004, 0x300000)
    assert not any(n.startswith("TPIU") for n in writes)
    assert "CODR=3" in cfg.summary()
    with pytest.raises(ValueError, match="ne porte que l'ITM"):
        SwoConfig(cpu_hz=64_000_000, chip=STM32N6, formatter=True).register_writes()
    assert detect_chip(lambda a: {0x44001000: 0x10006486}[a]) is STM32N6
    assert detect_chip(lambda a: 1 / 0) is None             # adresse illisible : TPIU standard
    assert SwoConfig(cpu_hz=16_000_000).register_writes()[1].addr == 0xE0040004


def test_dwt_data_trace_on_armv8m():
    from gdbtrace.trace.datatrace import TracedVar, comparator_writes, dwt_is_v8m
    v = TracedVar("produced", 0x34186a00, 4, 3, access="write", pc=True)
    w = {n: (a, val) for n, a, val in comparator_writes(v, v8m=True)}
    assert w["DWT_FUNCTION3"] == (0xE0001058, 13 | 3 << 4 | 2 << 10)   # écriture, PC + valeur, mot
    assert not any(n.startswith("DWT_MASK") for n in w)
    assert dwt_is_v8m(lambda a: 0x47711A02) and not dwt_is_v8m(lambda a: 0)
    with pytest.raises(ValueError, match="non alignée"):
        comparator_writes(TracedVar("x", 0x34186a02, 4, 3), v8m=True)


# ====================================================================== cible et options
def test_stlink_command_line(tmp_path):
    from gdbtrace.core.target import StLinkTarget
    t = StLinkTarget(server="/x/ST-LINK_gdbserver", programmer="/x/bin", serial="SN", apid=1,
                     server_port=61300, swo_port=61301, cpu_hz=64_000_000, swo_hz=2_000_000)
    cmd = t.command_line()
    assert cmd[:2] == ["/x/ST-LINK_gdbserver", "-p"] and "61300" in cmd
    assert cmd[cmd.index("-m") + 1] == "1" and cmd[cmd.index("-i") + 1] == "SN"
    assert {"-d", "-e", "-g"} <= set(cmd)                   # SWD, persistant, sans reset
    assert cmd[cmd.index("-z") + 1:cmd.index("-z") + 6] == ["61301", "-a", "64000000", "-b", "32"]
    assert t.swo_baud == 2_000_000


def test_stlink_options_are_checked(capsys):
    from gdbtrace.__main__ import main
    elf = os.path.join(ROOT, "examples", "firmware", "demo.elf")
    for argv, msg in ((["--stlink-ap", "1"], "--stlink-ap / --ram-boot : avec --stlink"),
                      (["--stlink", "--etr-buffer", "0x34010000:0x1000"], "--etr-buffer"),
                      (["--stlink", "--etm-port", "etr"], "--etm-port etr : avec --etm-config")):
        with pytest.raises(SystemExit):
            main([elf, "--batch", *argv])
        assert msg in capsys.readouterr().err


class StreamTarget(FakeTarget):
    """ETR qui écrit pendant que la cible tourne : `write(frames)` avance le pointeur
    d'écriture, le tampon tourne ; `read` est le canal mémoire en marche."""

    def __init__(self, buffer, size):
        super().__init__(buffer, size)
        self.wp = buffer
        self.during_read = b""            # trames écrites pendant la prochaine lecture

    def write(self, frames):
        from gdbtrace.trace import tmc
        for i in range(0, len(frames), 16):
            off = self.wp - self.buffer
            self.mem[off:off + 16] = frames[i:i + 16]
            self.wp = self.buffer + (off + 16) % len(self.mem)
        self.regs[0xE0083000 + tmc.RWP] = self.wp

    def read(self, addr, n):
        from gdbtrace.trace import tmc
        if addr == 0xE0083000 + tmc.RWP:
            return self.wp.to_bytes(4, "little")
        out = self.read_memory(addr, n)
        if self.during_read:
            late, self.during_read = self.during_read, b""
            self.write(late)
        return out


def test_etr_streaming_follows_the_write_pointer_and_detects_overruns():
    from gdbtrace.trace.chips import STM32N6
    from gdbtrace.trace.tmc import EtrCapture
    from gdbtrace.trace.tpiu import FULL_SYNC, frame_bytes
    buf, size = 0x34010000, 4096
    t = StreamTarget(buf, size)
    cap = EtrCapture(t, STM32N6, buf, size, stream=True)
    cap.setup()
    cap.restart()
    payload = bytes(range(256)) * 40                         # 10 Ko : plus de deux tours
    frames = frame_bytes(2, payload)[len(FULL_SYNC):]
    got = bytearray()
    for i in range(0, len(frames), 1024):                    # écrit, lu, écrit, lu…
        t.write(frames[i:i + 1024])
        data, gap = cap.poll(t.read)
        assert not gap
        got += data
    assert bytes(got) == payload                             # deux tours et demi, sans perte
    # l'écriture rattrape la lecture : la zone lue est jetée, un trou est signalé
    t.write(frames[:3072])
    t.during_read = frames[:2048]
    data, gap = cap.poll(t.read)
    assert gap and data == b"" and cap.overruns == 1 and cap.stream_ptr == t.wp
    t.write(frames[:512])
    data, gap = cap.poll(t.read)
    assert not gap and len(data) > 400                       # reprise au pointeur d'écriture
    assert "lu en marche" in cap.describe() and "rattrapées" in cap.describe()
