"""ETM par SWO : formateur TPIU sur la broche SWO, capturée par la sonde de débogage."""
import os
import subprocess
import sys

import pytest
from conftest import ROOT, needs_qemu

from gdbtrace.trace.config import SwoConfig, openocd_tpiu_commands
from gdbtrace.trace.etm import config as ec


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
    assert "-formatter 1" in openocd_tpiu_commands(cfg, "stm32l4x.tpiu", 3443)[0]
    assert "-formatter 0" in openocd_tpiu_commands(SwoConfig(cpu_hz=16_000_000), "x.tpiu", 3443)[0]


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
