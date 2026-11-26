"""Statistiques d'exceptions (trace/irqstats.py) : depuis la trace d'instructions
(`TimingAnalyzer`) et depuis la trace des exceptions de l'ITM (`ExceptionStats`)."""
import os

import pytest
from conftest import needs_qemu
from test_timing import S, run

from gdbtrace.core.symbols import SymbolTable
from gdbtrace.trace.etm.qemu_log import ExcEntry, ExcReturn
from gdbtrace.trace.irqstats import IrqStats
from gdbtrace.trace.itm import ExceptionTrace
from gdbtrace.trace.swo import ExceptionStats


def test_irq_stats_object():
    st = IrqStats("[SysTick]", 15)
    for t in (100, 1100, 2150, 3150):
        st.on_entry(t, cycles=12)
    for d in (30, 40, 20):
        st.on_duration(d)
    assert (st.count, st.period_n, st.period_min, st.period_max) == (4, 3, 1000, 1050)
    assert st.jitter == 50 and st.period_avg == pytest.approx(3050 / 3)
    assert (st.min, st.max, st.avg, st.entry_avg) == (20, 40, 30, 12)
    assert st.as_dict()["gigue"] == 50


def test_from_trace_nesting_and_tailchain():
    """SysTick interrompu par une IRQ, puis PendSV enchaîné à SysTick."""
    r = run([S(0x100, 1),
             ExcEntry(15, 0x500, cycles=12), S(0x500, 4),
             ExcEntry(16, 0x520, cycles=12), S(0x520, 2), S(0x522, 3), ExcReturn(cycles=10),
             S(0x502, 3), ExcReturn(cycles=10),
             S(0x100, 1),
             ExcEntry(15, 0x500, cycles=12), S(0x500, 4), S(0x502, 3),
             ExcEntry(14, 0x520, tailchain=True, cycles=6), S(0x520, 2), S(0x522, 3), ExcReturn(cycles=10),
             S(0x100, 1)])
    irq = r.irq
    tick, irq0, pendsv = irq["[SysTick]"], irq["[IRQ0]"], irq["[PendSV]"]
    assert tick.count == 2 and tick.preempted == 1 and tick.preempting == 0
    assert irq0.count == 1 and irq0.preempting == 1
    assert pendsv.count == 1 and pendsv.tail == 1
    assert tick.period_n == 1 and tick.period_min == tick.period_max > 0
    # durée de SysTick sans l'IRQ imbriquée : 12 + 4 + 3 + 10 au premier passage
    assert tick.complete == 2 and tick.max == 29 and irq0.max == 27
    assert tick.entry_avg == 12


def test_from_itm_packets():
    exc = ExceptionStats()
    E = ExceptionTrace
    exc([E(15, "entry", ts=100), E(16, "entry", ts=120), E(16, "exit", ts=150), E(15, "exit", ts=200),
         E(15, "entry", ts=1100), E(15, "exit", ts=1130), E(16, "entry", ts=2000), E(16, "exit", ts=2010)])
    rows = {r.name: r for r in exc.rows()}
    tick, irq0 = rows["SysTick"], rows["IRQ0"]
    assert (tick.count, tick.min, tick.max) == (2, 30, 70)       # 100 → 200 moins l'IRQ (30)
    assert tick.preempted == 1 and irq0.preempting == 1
    assert (tick.period_min, tick.jitter) == (1000, 0) and irq0.period_min == 1880
    exc.reset()
    assert exc.rows() == []


@needs_qemu
def test_rtos_path_exceptions(qemu_paths, rtos_elf):
    """Démo FreeRTOS sous QEMU : SysTick, PendSV et SVC ; entrées = événements ExcEntry."""
    from gdbtrace.trace.etm.cycles import annotate
    from gdbtrace.trace.timing import TimingAnalyzer
    img, events = qemu_paths["rtos"]
    r = TimingAnalyzer(img, SymbolTable(rtos_elf)).analyze(annotate(events, img))
    entries = {}
    for e in events:
        if isinstance(e, ExcEntry):
            entries[e.number] = entries.get(e.number, 0) + 1
    by_num = {st.number: st for st in r.irq.values()}
    assert {n: st.count for n, st in by_num.items()} == entries
    tick = by_num[15]
    assert tick.count > 5 and tick.period_min <= tick.period_avg <= tick.period_max
    assert tick.complete and tick.min <= tick.avg <= tick.max
    assert any(st.tail for st in r.irq.values())                  # enchaînement (PendSV)


@needs_qemu
def test_irq_stats_api_and_tab(rtos_elf, tmp_path):
    if not os.environ.get("DISPLAY"):
        pytest.skip("pas de DISPLAY")
    from test_gui import close_app, pump

    from gdbtrace.core.session import Session
    from gdbtrace.core.target import QemuTarget, free_port
    from gdbtrace.gui.app import App
    from gdbtrace.views.timing import TimingView
    a = App(layout_file=str(tmp_path / "layout.json"))
    try:
        s = Session()
        s.connect(rtos_elf, QemuTarget(elf=rtos_elf, gdb_port=free_port(), etm_sim=True))
        a.attach(s)
        v = a.views.show(TimingView)
        s.break_("worker_step")
        a.go()
        assert pump(a, lambda: a.current_frame is not None and a.current_frame.func == "worker_step", timeout=20)
        assert pump(a, lambda: a.trace.etm.stats["instructions"] > 100 and not a.trace.etm_busy, timeout=20)
        assert pump(a, lambda: v.result is not None and v.result.irq, timeout=20)
        names = [v.irqs.item(i, "text") for i in v.irqs.get_children()]
        assert "SysTick" in names, names
        rows = a.api.irq_stats()
        assert any(r["exception"] == "[SysTick]" and r["entrees"] for r in rows)
    finally:
        close_app(a)
