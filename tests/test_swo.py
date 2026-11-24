"""Capture SWO (TCP, fichier), session de trace, configuration ITM/DWT/TPIU."""
import socket
import threading
import time

from conftest import needs_qemu
from test_itm import SYNC, hw, stim

from gdbtrace.core.symbols import SymbolTable
from gdbtrace.trace.config import (DEMCR, DWT_CTRL, ITM_TCR, TPIU_ACPR, SwoConfig,
                                   openocd_tpiu_commands)
from gdbtrace.trace.itm import ItmTextSink
from gdbtrace.trace.profile import SLEEP, Profile
from gdbtrace.trace.swo import (ExceptionStats, FileSwoSource, TcpSwoSource, TraceSession,
                                profile_sink)
from gdbtrace.trace.tpiu import frame_bytes


# ====================================================================== configuration
def test_register_values():
    cfg = SwoConfig(cpu_hz=72_000_000, swo_hz=2_250_000, ports=0x3, pc_sampling_hz=10_000)
    assert cfg.prescaler == 31 and cfg.actual_swo_hz == 2_250_000
    tap, preset, hz = cfg.pc_sampling()
    assert (tap, preset) == (1, 6) and round(hz) == 10045        # 72 MHz / (7 × 1024)
    w = {r.name: r for r in cfg.register_writes()}
    assert w["ITM_TCR"].value == 0x0001000F                        # bus 1, DWT, sync, TS, ITM
    assert w["ITM_TER"].value == 0x3 and w["TPIU_SPPR"].value == 2 and w["TPIU_FFCR"].value == 0x100
    assert w["DWT_CTRL"].value == 0x000116CD and w["DWT_CTRL"].mask is not None
    assert w["DEMCR.TRCENA"].mask == 1 << 24
    names = [r.name for r in cfg.register_writes()]
    assert names.index("DEMCR.TRCENA") == 0 and names.index("ITM_LAR") < names.index("ITM_TCR")
    # pas d'échantillonnage du PC ni d'exceptions
    quiet = SwoConfig(cpu_hz=25_000_000, pc_sampling_hz=None, exceptions=False, timestamps=False)
    assert {r.name: r.value for r in quiet.register_writes()}["DWT_CTRL"] == 0x401
    assert "SWO 2.083 MHz" in quiet.summary()


def test_openocd_commands():
    cfg = SwoConfig(cpu_hz=168_000_000, swo_hz=2_000_000)
    assert openocd_tpiu_commands(cfg, "stm32f4x.tpiu", 3443) == [
        "stm32f4x.tpiu disable",                     # reconfiguration d'un TPIU activé
        "stm32f4x.tpiu configure -protocol uart -traceclk 168000000 -pin-freq 2000000 "
        "-output :3443 -formatter 0",
        "stm32f4x.tpiu enable"]


class FakeTarget:
    def __init__(self, words):
        self.words = dict(words)

    def read_u32(self, a):
        return self.words.get(a, 0)

    def write_u32(self, a, v):
        self.words[a] = v


def test_apply_preserves_other_bits():
    t = FakeTarget({DEMCR: 0x1, DWT_CTRL: 0x40000000})            # NUMCOMP = 4 (lecture seule)
    done = SwoConfig(cpu_hz=25_000_000).apply(t)
    assert t.words[DEMCR] == 0x01000001
    assert t.words[DWT_CTRL] & 0xF0000000 == 0x40000000 and t.words[DWT_CTRL] & 1
    assert t.words[ITM_TCR] & 1 and t.words[TPIU_ACPR] == 11
    assert len(done) == 10


@needs_qemu
def test_apply_on_qemu_is_harmless(session):
    # QEMU n'émule pas l'ITM : les écritures sont ignorées, mais rien ne doit échouer
    done = SwoConfig(cpu_hz=25_000_000).apply(session)
    assert len(done) == 10 and session.state == "stopped"


# ====================================================================== flux de test
def synthetic_stream(sym):
    fib, main = sym.function("fib"), sym.function("main")
    text = b"".join(stim(0, c) for c in b"hello\nworld\n")
    pcs = hw(2, fib.start + 4, 4) * 5 + hw(2, main.start, 4) * 2 + hw(2, 0, 1)   # + sommeil
    exc = (hw(1, 15 | 1 << 12, 2) + bytes([0xC0, 100]) + hw(1, 15 | 2 << 12, 2)  # SysTick, 100
           + hw(1, 15 | 1 << 12, 2) + bytes([0xC0, 50]) + hw(1, 15 | 2 << 12, 2)   # SysTick, 50
           + hw(1, 16 | 1 << 12, 2) + hw(1, 16 | 2 << 12, 2))                      # IRQ0
    return SYNC + text + pcs + exc + b"\x70"


def make_session(sym, formatter=False):
    sess = TraceSession(formatter=formatter)
    text, prof, exc = ItmTextSink(), Profile(sym), ExceptionStats()
    sess.add_sink(lambda pkts: [text.add(p) for p in pkts])
    sess.add_sink(profile_sink(prof))
    sess.add_sink(exc)
    return sess, text, prof, exc


def check_results(sess, text, prof, exc):
    assert text.text(0) == "hello\nworld"
    assert [(r.name, r.samples) for r in prof.top_functions()] == [("fib", 5), ("main", 2), (SLEEP, 1)]
    rows = {r.name: r for r in exc.rows()}
    assert rows["SysTick"].entries == 2 and rows["SysTick"].total_time == 150
    assert rows["SysTick"].max_time == 100 and rows["IRQ0"].entries == 1
    assert sess.stats["overflows"] == 1 and sess.stats["erreurs"] == 0


class FakeSwoServer(threading.Thread):
    def __init__(self, payload, chunks=3):
        super().__init__(daemon=True)
        self.payload, self.chunks = payload, chunks
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(1)
        self.port = self.sock.getsockname()[1]

    def run(self):
        conn, _ = self.sock.accept()
        step = len(self.payload) // self.chunks + 1
        for i in range(0, len(self.payload), step):
            conn.sendall(self.payload[i:i + step])
            time.sleep(0.02)
        conn.close()
        self.sock.close()


def wait(cond, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


def test_tcp_swo_source(elf):
    sym = SymbolTable(elf)
    srv = FakeSwoServer(synthetic_stream(sym))
    srv.start()
    sess, text, prof, exc = make_session(sym)
    src = TcpSwoSource("127.0.0.1", srv.port, retry=0.1)
    src.start(sess)
    try:
        assert wait(lambda: sess.stats["overflows"] == 1)
        check_results(sess, text, prof, exc)
        assert wait(lambda: src.error is not None)       # serveur fermé : erreur, reconnexion
        assert not src.connected
    finally:
        src.stop()
    assert not src.running


def test_tcp_swo_source_refused_explains():
    """Port fermé (OpenOCD avant `tpiu enable`) : l'erreur dit comment ouvrir le port."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]                         # libre, personne n'y écoute
    src = TcpSwoSource("127.0.0.1", port, retry=0.1)
    src.start(TraceSession())
    try:
        assert wait(lambda: src.error is not None)
        assert "rien n'écoute" in src.error and "Configurer la cible" in src.error
    finally:
        src.stop()


def test_formatted_stream_and_file_replay(elf, tmp_path):
    sym = SymbolTable(elf)
    path = tmp_path / "capture.bin"
    path.write_bytes(frame_bytes(1, synthetic_stream(sym)))        # TPIU formateur actif
    sess, text, prof, exc = make_session(sym, formatter=True)
    src = FileSwoSource(str(path), chunk=7)
    src.start(sess)
    assert src.done.wait(3)
    check_results(sess, text, prof, exc)
    src.stop()
