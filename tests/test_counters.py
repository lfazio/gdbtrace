"""Compteurs de performance (core/counters.py) : DWT (tours de 8 bits rattrapés par les
paquets de l'ITM), PMU d'un Cortex-M55 (compteurs chaînés), absents sur un ARMv6-M et sous
QEMU."""
import os

import pytest
from conftest import needs_qemu

from gdbtrace.core import counters as cn
from gdbtrace.trace.itm import EventCounter


class FakeTarget:
    """Registres en dictionnaire ; DWT et PMU qui « tournent » entre deux arrêts."""

    def __init__(self, pmu=False, dwt=True):
        self.mem = {cn.DEMCR: 0, cn.DWT_CTRL: 0, cn.PMU_TYPE: 0x00A05F08 if pmu else 0, cn.PMU_CTRL: 0}
        for k in range(7):
            self.mem[cn.DWT_CTRL + 4 * k] = self.mem.get(cn.DWT_CTRL + 4 * k, 0)
        for k in range(8):
            self.mem[cn.PMU_EVCNTR + 4 * k] = 0
        self.mem[cn.PMU_CCNTR] = 0
        self.dwt = dwt
        self.state = "stopped"
        self.stop_count = 0
        self.stop_reason = {"reason": "breakpoint-hit"}
        self.target = None

    def read_u32(self, a):
        return self.mem.get(a, 0)

    def write_u32(self, a, v):
        if a == cn.DWT_CTRL and not self.dwt:
            return                                       # QEMU : écriture ignorée
        self.mem[a] = v

    def read_memory(self, a, n):
        return b"".join(self.read_u32(a + i).to_bytes(4, "little") for i in range(0, n, 4))

    def run(self, **deltas):
        for name, d in deltas.items():
            if name == "CYC":
                self.mem[cn.DWT_CTRL + 4] = (self.mem[cn.DWT_CTRL + 4] + d) & 0xFFFFFFFF
                self.mem[cn.PMU_CCNTR] = self.mem[cn.DWT_CTRL + 4]
            elif name in cn.DWT_NAMES:
                a = cn.DWT_CTRL + 8 + 4 * cn.DWT_NAMES.index(name)
                self.mem[a] = (self.mem[a] + d) & 0xFF
            else:                                        # PMU : paire chaînée de 16 bits
                k = [e[0] for e in cn.PMU_EVENTS].index(name)
                lo, hi = cn.PMU_EVCNTR + 8 * k, cn.PMU_EVCNTR + 8 * k + 4
                v = (self.mem[lo] | self.mem[hi] << 16) + d
                self.mem[lo], self.mem[hi] = v & 0xFFFF, (v >> 16) & 0xFFFF
        self.stop_count += 1


def make(target, core=0xC27, capture=False):
    tally = cn.EventCounterTally()
    c = cn.Counters(target, core=lambda: core, tally=lambda: tally, capturing=lambda: capture)
    return c, tally


def test_dwt_short_run_is_exact_and_gives_instructions():
    t = FakeTarget()
    c, _ = make(t)
    assert "armés (DWT)" in c.enable()
    assert t.mem[cn.DWT_CTRL] & cn.CYCCNTENA and t.mem[cn.DWT_CTRL] >> 17 & 0x1F == 0x1F
    assert t.mem[cn.DEMCR] & cn.TRCENA
    t.mem[cn.DWT_CTRL + 8] = 250                       # CPICNT près du tour
    c.start()
    t.run(CYC=200, CPI=10, LSU=20, FOLD=5)
    m = c.stop()
    assert m.source == "DWT" and m.cycles == 200 and m.get("CPI").delta == 10 and all(v.exact for v in m.values)
    assert m.instructions == 200 - 10 - 0 - 0 - 20 + 5 and m.ipc == pytest.approx(175 / 200)
    assert c.stop() is m and c.totals["CYC"] == 200 and c.runs == 1


def test_dwt_long_run_needs_the_overflow_packets():
    t = FakeTarget()
    c, tally = make(t)
    c.enable()
    c.start()
    t.run(CYC=100_000, CPI=300)                         # 300 = un tour + 44
    m = c.stop()
    assert m.get("CPI").delta == 44 and not m.get("CPI").exact and m.instructions is None
    assert "modulo 256" in m.note
    c2, tally2 = make(t, capture=True)
    c2.enable()
    c2.start()
    t.run(CYC=100_000, CPI=300, LSU=600)
    tally2([EventCounter(flags=0b0001), EventCounter(flags=0b1000), EventCounter(flags=0b1000)])
    m2 = c2.stop()
    assert m2.get("CPI").delta == 300 and m2.get("LSU").delta == 600 and m2.get("CPI").exact
    assert m2.instructions == 100_000 - 300 - 600


def test_pmu_chained_counters():
    t = FakeTarget(pmu=True)
    c, _ = make(t, core=0xD22)
    assert "armés (PMU)" in c.enable()
    assert t.mem[cn.PMU_EVTYPER] == 0x08 and t.mem[cn.PMU_EVTYPER + 4] == cn.CHAIN
    assert t.mem[cn.PMU_CNTENSET] == (1 << 31) | 0xFF and t.mem[cn.PMU_CTRL] & 1
    t.mem[cn.PMU_EVCNTR] = 0xFFF0                       # INST_RETIRED : la paire va déborder
    c.start()
    t.run(CYC=200_000, INST_RETIRED=150_000, L1D_CACHE_REFILL=42, EXC_TAKEN=3)
    m = c.stop()
    assert m.source == "PMU" and m.instructions == 150_000 and m.get("L1D_CACHE_REFILL").delta == 42
    assert m.get("EXC_TAKEN").delta == 3 and m.ipc == pytest.approx(0.75)


def test_absent_and_invalidated():
    c, _ = make(FakeTarget(), core=0xC60)
    assert "ARMv6-M" in c.enable() and not c.enabled
    c, _ = make(FakeTarget(dwt=False))
    assert "non émulé (QEMU)" in c.enable() and not c.enabled and c.stop() is None
    t = FakeTarget()
    c, _ = make(t)
    c.enable()
    c.start()
    t.stop_reason = {"reason": "reset"}
    t.run(CYC=10)
    assert c.stop() is None
    assert c.enable(False) == "compteurs désarmés" and not t.mem[cn.DWT_CTRL] >> 17 & 0x1F


@needs_qemu
def test_counters_absent_under_qemu(session):
    from gdbtrace.core.scripting import ScriptAPI
    api = ScriptAPI(session)
    assert api.counters() is None
    assert not api._counters.enabled and "QEMU" in api._counters.reason


@needs_qemu
def test_counters_view_under_qemu(elf, tmp_path):
    if not os.environ.get("DISPLAY"):
        pytest.skip("pas de DISPLAY")
    from test_gui import close_app, pump

    from gdbtrace.core.session import Session
    from gdbtrace.core.target import QemuTarget, free_port
    from gdbtrace.gui.app import App
    from gdbtrace.views.counters import CountersView
    a = App(layout_file=str(tmp_path / "layout.json"))
    try:
        s = Session()
        s.connect(elf, QemuTarget(elf=elf, gdb_port=free_port()))
        a.attach(s)
        assert pump(a, lambda: s.state == "stopped")
        v = a.views.show(CountersView, "bottom")
        v.on_var.set(True)
        v._toggle()
        assert not v.on_var.get() and "QEMU" in v.info.cget("text")
    finally:
        close_app(a)
