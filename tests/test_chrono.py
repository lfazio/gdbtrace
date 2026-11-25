"""Chrono (core/chrono.py) : temps du cœur entre la reprise et l'arrêt, par SysTick et le
compteur de ticks (Cortex-M0/M0+, QEMU) ou par DWT_CYCCNT (M3 et au-delà)."""
import time

import pytest
from conftest import needs_qemu

from gdbtrace.core.chrono import (DWT_CYCCNT, ICSR, PENDSTSET, SYST_CSR, Chrono, Measure,
                                  format_duration)

TICKS = 0x20000100


class FakeTarget:
    """Cible arrêtée : registres et mémoire en dictionnaire, compteur d'arrêts visibles."""

    def __init__(self, ticks=True):
        self.mem = {SYST_CSR: 0x7, SYST_CSR + 4: 7999, SYST_CSR + 8: 1000, ICSR: 0,
                    TICKS: 10, DWT_CYCCNT: 0, 0xE000EDFC: 0, 0xE0001000: 0}
        self.ticks = ticks
        self.state = "stopped"
        self.stop_count = 0
        self.stop_reason = {"reason": "breakpoint-hit"}
        self.target = None

    def address_of_expr(self, name):
        if self.ticks and name == "xTickCount":
            return TICKS
        raise RuntimeError(f"No symbol {name}")

    def read_u32(self, addr):
        return self.mem[addr]

    def write_u32(self, addr, value):
        self.mem[addr] = value

    def read_memory(self, addr, n):
        return b"".join(self.mem[addr + i].to_bytes(4, "little") for i in range(0, n, 4))

    def run(self, **regs):
        """Une exécution : registres à l'arrêt, puis l'arrêt visible."""
        for k, v in regs.items():
            self.mem[{"cvr": SYST_CSR + 8, "ticks": TICKS, "icsr": ICSR, "cyccnt": DWT_CYCCNT}[k]] = v
        self.stop_count += 1


def test_systick_with_tick_counter():
    t = FakeTarget()
    c = Chrono(t, core=lambda: 0xC20)
    c.start()
    t.run(cvr=5000, ticks=12)                       # deux tours de 8000 cycles, et 1000 → 5000
    m = c.stop()
    assert (m.cycles, m.source, m.note) == (2 * 8000 + 1000 - 5000, "SysTick", "")
    assert c.stop() is m and c.total == 12000       # une seule mesure par arrêt
    c.start()
    t.run(cvr=4000, ticks=12)
    assert c.stop().cycles == 1000 and c.total == 13000 and c.runs == 2
    c.reset()
    assert c.total == 0 and c.runs == 0


def test_systick_pending_tick_counts():
    """Le compteur vient de repasser par zéro, l'interruption n'est pas encore prise :
    xTickCount n'a pas avancé, mais le tour compte (ICSR.PENDSTSET)."""
    t = FakeTarget()
    c = Chrono(t, core=lambda: 0xC60)
    c.start()
    t.run(cvr=7990, icsr=PENDSTSET)
    assert c.stop().cycles == 8000 + 1000 - 7990


def test_systick_without_tick_counter():
    t = FakeTarget(ticks=False)
    c = Chrono(t, core=lambda: 0xC20)
    c.start()
    t.run(cvr=7000)
    m = c.stop()
    assert m.cycles == (1000 - 7000) % 8000 and "à une période de SysTick près" in m.note


def test_cyccnt_and_its_wraps():
    t = FakeTarget()
    c = Chrono(t, core=lambda: 0xC27)               # Cortex-M7 : compteur de cycles
    c.start()
    assert t.mem[0xE000EDFC] & 1 << 24 and t.mem[0xE0001000] & 1   # TRCENA, CYCCNTENA
    t.run(cyccnt=1234)
    assert (c.stop().cycles, c.last.source) == (1234, "DWT_CYCCNT")
    c.start()                                       # 600 000 ticks de 8000 cycles : un tour
    t.run(cyccnt=1234 + 1000, ticks=12 + 600_000)
    assert c.stop().cycles == 1000 + 2 ** 32


def test_reset_or_no_systick_gives_no_measure():
    t = FakeTarget()
    c = Chrono(t, core=lambda: 0xC20)
    c.start()
    t.stop_reason = {"reason": "reset"}
    t.run(cvr=10)
    assert c.stop() is None and c.total == 0
    t.mem[SYST_CSR] = 0                              # SysTick arrêté : pas de départ
    t.stop_reason = {"reason": "breakpoint-hit"}
    c.start()
    t.run()
    assert c.stop() is None


def test_format_duration():
    assert format_duration(0.004981) == "4,98 ms"
    assert format_duration(312e-6) == "312 µs" and format_duration(2.41) == "2,41 s"
    assert format_duration(None) == "?" and Measure(16000, "SysTick").seconds(16_000_000) == 0.001


@pytest.fixture
def m0_rtos_session(m0_elf):
    """Démo FreeRTOS sur un Cortex-M0 (QEMU microbit), arrêtée dans worker_step()."""
    import os

    from conftest import M0_DIR
    from gdbtrace.core.session import Session
    from gdbtrace.core.target import QemuTarget, free_port
    elf = os.path.join(M0_DIR, "freertos.elf")
    s = Session()
    s.connect(elf, QemuTarget(elf=elf, gdb_port=free_port(), machine="microbit"))
    s.go()
    time.sleep(0.3)
    s.halt()
    s.break_("worker_step", temporary=True)
    s.go()
    s.wait_stopped(10)
    yield s
    s.disconnect()


@needs_qemu
@pytest.mark.parametrize("fixture", ["rtos_session", "m0_rtos_session"])
def test_chrono_between_two_passes(fixture, request):
    """Deux passages par worker_step (la tâche attend 5 ms, soit 5 ticks, entre les deux) :
    SysTick et xTickCount de FreeRTOS, en temps simulé par QEMU (bornes larges). Un arrêt
    prolongé n'est pas compté."""
    from gdbtrace.core.scripting import ScriptAPI
    s = request.getfixturevalue(fixture)
    api = ScriptAPI(s)
    period = (s.read_u32(SYST_CSR + 4) & 0xFFFFFF) + 1      # cycles par tick (1 ms)
    s.break_("worker_step")
    s.go()
    s.wait_stopped(10)
    for _ in range(2):
        s.go()
        s.wait_stopped(10)
        r = api.chrono()
        assert r["source"] == "SysTick (QEMU : temps simulé)" and not r["note"], r
        assert 3 * period < r["cycles"] < 60 * period, (r, period)
    assert api.chrono()["cumul_cycles"] > r["cycles"]       # même arrêt : pas compté deux fois
    time.sleep(0.5)                                          # cible arrêtée : rien ne s'ajoute
    s.go()
    s.wait_stopped(10)
    assert api.chrono()["cycles"] < 60 * period
