"""ETM par SWO : formateur TPIU sur la broche SWO, capturée par la sonde de débogage."""
import json
import os
import subprocess
import sys
import threading
import time
import types

import pytest
from conftest import ROOT, needs_qemu
from test_swo import FakeSwoServer

from gdbtrace.core.target import OpenOcdTarget, RemoteTarget, free_port
from gdbtrace.trace.config import TPIU_FFCR, SwoConfig, openocd_tpiu_commands
from gdbtrace.trace.etm import config as ec
from gdbtrace.trace.manager import TraceManager


class FakeOpenOcd:
    """Session gdb sur OpenOCD, cible arrêtée : registres, `monitor`, ETM-M7 (TRCIDR1)."""

    def __init__(self):
        self.regs = {ec.ETMIDR: 0x4100F400}
        self.writes, self.monitors = [], []
        self.target = OpenOcdTarget(configs=["configs/nucleo-f746zg.cfg"])

    def read_u32(self, a):
        return ec.TRCSTATR_IDLE if a == ec.TRCSTATR else self.regs.get(a, 0)

    def write_u32(self, a, v):
        self.writes.append((a, v))
        self.regs[a] = v

    def monitor(self, cmd):
        self.monitors.append(cmd)
        return "stm32f7x.tpiu\n" if cmd == "tpiu names" else ""

    def add_resume_hook(self, fn):
        pass

    def on_event(self, fn):
        pass


class OpenOcdSwoPort(FakeSwoServer):
    """Port SWO d'OpenOCD (`-output :port`) : sert la capture puis garde la connexion."""

    def __init__(self, payload):
        super().__init__(payload)
        self.done = threading.Event()

    def run(self):
        conn, _ = self.sock.accept()
        conn.sendall(self.payload)
        self.done.wait(30)
        conn.close()
        self.sock.close()


def until(cond, timeout=20.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


def regs(cfg):
    return {w.name: (w.value, w.mask) for w in cfg.register_writes()}


def test_etm_swo_registers():
    r = regs(ec.EtmConfig(port="swo", cpu_hz=16_000_000, swo_hz=2_000_000, stm32=True))
    assert r["TPIU_CSPSR"][0] == 1 and r["TPIU_ACPR"][0] == 7          # 16 MHz / 8 = 2 Mbit/s
    assert r["TPIU_SPPR"][0] == 2 and r["TPIU_FFCR"][0] == 0x102      # NRZ, formateur actif
    assert r["DBGMCU_CR.TRACE"] == (1 << 5, 0xE0)                     # TRACE_IOEN, mode SWO (00)
    assert regs(ec.EtmConfig(port="swo", cpu_hz=16_000_000, manchester=True))["TPIU_SPPR"][0] == 1
    par = regs(ec.EtmConfig(port_width=4, stm32=True))
    assert par["TPIU_SPPR"][0] == 0 and par["DBGMCU_CR.TRACE"][0] == 1 << 5 | 3 << 6
    with pytest.raises(ValueError, match="cpu-hz"):
        ec.EtmConfig(port="swo").register_writes()


def test_etm_swo_bandwidth_summary():
    # 2 Mbit/s NRZ : ~1,5 Mbit/s utiles → ~3,2 MHz de cœur à 0,76 bit/instr. et 1,6 cycle/instr.
    assert ec.swo_core_limit_hz(2e6) == pytest.approx(3.16e6, rel=0.01)
    s = ec.EtmConfig(port="swo", cpu_hz=4_000_000, swo_hz=2_000_000).summary()
    assert "ETM par SWO" in s and "~3.2 MHz" in s and "trous" in s
    assert "trous" not in ec.EtmConfig(port="swo", cpu_hz=2_000_000, swo_hz=2_000_000).summary()


def test_etmv4_programming():
    """ETM-M7 (TRCIDR1 : architecture 4) : registres TRC*, arrêt et attente d'IDLE d'abord,
    remise en marche en dernier."""
    class S:
        def __init__(self):
            self.writes = []

        def read_u32(self, a):
            if a == ec.ETMIDR:
                return 0x4100F400                                # TRCIDR1 d'un ETM-M7
            return ec.TRCSTATR_IDLE if a == ec.TRCSTATR else 0

        def write_u32(self, a, v):
            self.writes.append((a, v))
    s = S()
    cfg = ec.EtmConfig(port="swo", cpu_hz=216_000_000, stall=True)
    done = dict(cfg.apply(s))
    assert cfg.version == 4 and "ETMv4" in cfg.summary() and "stall" in cfg.summary()
    addrs = [a for a, _ in s.writes]
    assert addrs.index(ec.TRCPRGCTLR) < addrs.index(ec.TRCTRACEIDR)
    assert s.writes[-1] == (ec.TRCPRGCTLR, 1)
    assert done["TRCVICTLR"] == 0x201 and done["TRCTRACEIDR"] == 2 and done["TPIU_FFCR"] == 0x102
    assert done["TRCSTALLCTLR"] & 1 << 8 and done["TRCVIIECTLR"] == 0
    with pytest.raises(ValueError, match="cycle-accurate"):    # refusé avant toute écriture
        ec.EtmConfig(cycle_accurate=True).apply(S())
    assert ec.etm_version(lambda a: 0x4114F250) == 2                   # ETM-M3 / M4 : ETMv3


def test_swo_formatter_for_etm():
    cfg = SwoConfig(cpu_hz=16_000_000, swo_hz=2_000_000, formatter=True)
    assert {w.name: w.value for w in cfg.register_writes()}["TPIU_FFCR"] == 0x102
    assert "-formatter 1" in openocd_tpiu_commands(cfg, "stm32l4x.tpiu", 3443)[1]
    assert "-formatter 0" in openocd_tpiu_commands(SwoConfig(cpu_hz=16_000_000), "x.tpiu", 3443)[1]


@needs_qemu
def test_etm_over_swo_capture_replay(qemu_paths, elf, tmp_path):
    """Capture SWO formatée (ETM ID 2 + ITM ID 1) rejouée par la ligne de commande avec
    --etm-config --etm-port swo : même chemin décodé, texte ITM intact."""
    from test_itm import SYNC, stim
    from gdbtrace.trace.etm.encoder import encode
    from gdbtrace.trace.etm.qemu_log import Step
    from gdbtrace.trace.tpiu import frame_bytes
    img, events = qemu_paths["bare"]
    events = events[:20_000]
    n = sum(isinstance(e, Step) for e in events)
    etm_bytes = encode(events, img)
    itm_bytes = SYNC + b"".join(stim(0, c) for c in b"swo ok\n")
    half = len(etm_bytes) // 2
    capture = tmp_path / "swo.bin"
    capture.write_bytes(frame_bytes(2, etm_bytes[:half]) + frame_bytes(1, itm_bytes)[4:]
                        + frame_bytes(2, etm_bytes[half:])[4:])
    script = tmp_path / "s.py"
    script.write_text(f"import time\nfor _ in range(200):\n"
                      f"    if gdbtrace.trace.etm.stats['instructions'] >= {n}: break\n"
                      f"    time.sleep(0.05)\n"
                      f"print('instructions', gdbtrace.trace.etm.stats['instructions'])\n"
                      f"print('itm', gdbtrace.itm(0))\n")
    r = subprocess.run([sys.executable, "-m", "gdbtrace", elf, "--etm-config", "--etm-port", "swo",
                        "--cpu-hz", "4000000", "--swo-hz", "2000000", "--swo-file", str(capture),
                        "--batch", "--script", str(script)],
                       cwd=ROOT, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr
    assert "ETM par SWO" in r.stdout and "TPIU_FFCR = 0x00000102" in r.stdout
    assert f"instructions {n}" in r.stdout and "itm swo ok" in r.stdout
    assert os.path.exists(capture)


# ====================================================================== mise en route (GUI, batch)
@needs_qemu
def test_start_etm_swo_on_openocd(qemu_paths, elf):
    """Chemin d'une Nucleo-F746 : TPIU formaté, `tpiu configure/enable` d'OpenOCD, capture du
    port TCP, ETMv4 programmé et détecté ; trace décodée et couverture remplie."""
    from gdbtrace.core.symbols import SymbolTable
    from gdbtrace.trace.etm.encoder4 import encode as encode4
    from gdbtrace.trace.etm.qemu_log import Step
    from gdbtrace.trace.tpiu import frame_bytes
    img, events = qemu_paths["bare"]
    events = events[:50_000]
    n = sum(isinstance(e, Step) for e in events)
    port = OpenOcdSwoPort(frame_bytes(2, encode4(events, img)))
    port.start()
    tm = TraceManager()
    assert tm.setup_etm(elf, SymbolTable(elf)) is None
    s = FakeOpenOcd()
    try:
        log = tm.start_etm_swo(s, s.target, 216_000_000, 2_000_000, stall=True, port=port.port)
        assert s.monitors[0] == "tpiu names" and s.monitors[1] == "stm32f7x.tpiu disable"
        assert f"-output :{port.port} -formatter 1" in s.monitors[2]
        assert s.monitors[3] == "stm32f7x.tpiu enable"
        assert s.regs[TPIU_FFCR] == 0x102 and s.regs[ec.TRCPRGCTLR] == 1
        assert any(f"capture SWO TCP {port.port} démarrée" in line for line in log)
        assert log[-1].startswith("ETM par SWO (ETMv4)") and "stall" in log[-1]
        assert tm.etm_swo and tm.etm.version == 4 and tm.setup_error is None
        assert until(lambda: tm.etm.stats["instructions"] >= 0.99 * n), tm.trace_status()
        tm.etm.flush()
        st = tm.etm.stats
        assert st["erreurs"] == 0 and st["débordements"] == 0
        assert "octets ETM" in tm.trace_status() and "0 erreurs" in tm.trace_status()
        t, called, _ = tm.etm.cov.total()
        assert t.covered > 100 and called >= 5
    finally:
        port.done.set()
        tm.close()


def test_etm_swo_diagnostics(tmp_path):
    """Chaque raison pour laquelle aucune instruction n'arrive est nommée (vues Couverture et
    Trace) : pas de capture, sonde qui ne sert pas le SWO, flux non formaté, pas d'ETM."""
    from test_itm import SYNC, stim
    from gdbtrace.trace.tpiu import frame_bytes
    tm = TraceManager()
    assert "indisponible" in tm.trace_status()
    tm.etm = types.SimpleNamespace(stats={"instructions": 0, "débordements": 0, "erreurs": 0})
    assert tm.trace_status().startswith("aucune capture") and "ETM par SWO" in tm.trace_status()
    # --remote sans --tcl : rien n'est écrit, la cause reste affichée
    s = FakeOpenOcd()
    with pytest.raises(RuntimeError, match="ne sert pas le SWO"):
        tm.start_etm_swo(s, RemoteTarget(gdb_port=3333), 216_000_000)
    assert s.writes == [] and not tm.etm_swo
    assert tm.trace_status().startswith("ETM par SWO : la sonde ne sert pas le SWO")
    with pytest.raises(ValueError, match="cpu-hz"):
        tm.start_etm_swo(s, s.target, 0)
    try:
        itm = SYNC + b"".join(stim(0, c) for c in b"ok\n")
        raw = tmp_path / "raw.bin"
        raw.write_bytes(itm)
        tm.start_file(str(raw), formatter=False).done.wait(5)
        assert "sans formateur TPIU" in tm.trace_status()
        fmt = tmp_path / "itm.bin"
        fmt.write_bytes(frame_bytes(1, itm))
        tm.start_file(str(fmt), formatter=True).done.wait(5)
        assert "aucun octet ETM (ID 2)" in tm.trace_status() and "--cpu-hz" in tm.trace_status()
        silent = OpenOcdSwoPort(b"")
        silent.start()
        tm.start_tcp("127.0.0.1", silent.port, formatter=True)
        assert until(lambda: tm.source.connected, 5)
        assert "rien reçu" in tm.trace_status()
        silent.done.set()
        tm.start_tcp("127.0.0.1", free_port(), formatter=True)       # OpenOCD n'a pas ouvert le port
        assert until(lambda: tm.source.error is not None, 5)
        assert "rien n'écoute" in tm.trace_status()
    finally:
        tm.close()


@needs_qemu
def test_etm_over_swo_coverage_campaign(qemu_paths, elf, tmp_path):
    """--coverage accepte l'ETM par SWO (refusé avant) ; la campagne remplit la base."""
    from gdbtrace.trace.etm.encoder import encode
    from gdbtrace.trace.etm.qemu_log import Step
    from gdbtrace.trace.tpiu import frame_bytes
    img, events = qemu_paths["bare"]
    events = events[:20_000]
    n = sum(isinstance(e, Step) for e in events)
    capture = tmp_path / "swo.bin"
    capture.write_bytes(frame_bytes(2, encode(events, img)))
    script = tmp_path / "s.py"
    script.write_text(f"import time\nfor _ in range(200):\n"
                      f"    if gdbtrace.trace.etm.stats['instructions'] >= {n}: break\n"
                      f"    time.sleep(0.05)\n")
    cov = tmp_path / "cov.json"
    r = subprocess.run([sys.executable, "-m", "gdbtrace", elf, "--etm-config", "--etm-port", "swo",
                        "--cpu-hz", "4000000", "--swo-file", str(capture), "--batch",
                        "--script", str(script), "--coverage", str(cov)],
                       cwd=ROOT, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr
    assert "capture SWO rejouée" in r.stdout and "aucune instruction tracée" not in r.stdout
    assert len(json.loads(cov.read_text())["e"]) > 100


def test_etm_options_need_etm_config(tmp_path):
    """--etm-port swo, --etm-stall, --stm32 sans --etm-config étaient ignorés en silence."""
    def run(*args):
        return subprocess.run([sys.executable, "-m", "gdbtrace", *args, "--batch"], cwd=ROOT,
                              capture_output=True, text=True, timeout=60)
    r = run("fw.elf", "--etm-port", "swo")
    assert r.returncode == 2 and "--etm-port swo : avec --etm-config" in r.stderr
    r = run("fw.elf", "--etm-stall", "--stm32")
    assert r.returncode == 2 and "--etm-stall, --stm32 : avec --etm-config" in r.stderr
