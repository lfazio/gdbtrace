"""Distributions de durées (Trace.STATistic.DISTribution) : réservoir, centiles, histogramme,
valeurs les plus longues."""
import os

import pytest
from conftest import needs_qemu

from gdbtrace.trace.distribution import Reservoir, distribution, percentile


def test_percentiles_nearest_rank():
    vals = list(range(1, 101))                       # 1..100
    assert (percentile(vals, 50), percentile(vals, 90), percentile(vals, 99)) == (50, 90, 99)
    assert percentile([7], 99) == 7


def test_reservoir_exact_below_capacity_and_longest():
    r = Reservoir(capacity=1000, top=3)
    for i, v in enumerate([5, 1, 9, 3, 9, 2]):
        r.add(v, where=100 + i)
    assert (r.count, r.min, r.max, r.total) == (6, 1, 9, 29) and not r.sampled
    assert r.longest() == [(9, 102), (9, 104), (5, 100)]   # à valeur égale : la première vue
    d = distribution("f", r, bins=4, log=False)
    assert [b.count for b in d.bins] == [2, 1, 1, 2] and sum(b.count for b in d.bins) == 6
    assert (d.bins[0].low, d.bins[-1].high) == (1, 9)
    assert d.percentiles == {50: 3, 90: 9, 99: 9} and d.mean == 29 / 6
    assert "6 valeurs" in d.summary()


def test_reservoir_sampling_keeps_exact_extremes():
    r = Reservoir(capacity=100, top=5)
    for i in range(10_000):
        r.add(i % 1000, where=i)
    assert r.count == 10_000 and len(r.values) == 100 and r.sampled
    assert (r.min, r.max) == (0, 999)
    assert [v for v, _w in r.longest()] == [999] * 5
    d = distribution("g", r, bins=10)
    assert sum(b.count for b in d.bins) == 10_000 and d.sampled
    assert 300 < d.percentiles[50] < 700                     # échantillon uniforme
    again = Reservoir(capacity=100, top=5)                   # graine fixe : même échantillon
    for i in range(10_000):
        again.add(i % 1000, where=i)
    assert again.values == r.values


def test_log_bins_and_scale():
    r = Reservoir()
    for v in (1, 2, 10, 100, 1000, 10_000):
        r.add(v)
    d = distribution("h", r, bins=4, scale=0.5, unit="µs")
    assert d.log and (d.min, d.max) == (0.5, 5000)
    assert [b.count for b in d.bins] == [2, 1, 1, 2]
    assert d.bins[1].low == 0.5 * 10 ** 1                    # décades
    assert distribution("vide", Reservoir()).summary() == "vide : aucune valeur"
    one = Reservoir()
    one.add(4)
    assert [b.count for b in distribution("u", one).bins] == [1]


def test_task_slices_and_variable_values():
    from collections import deque

    from gdbtrace.trace.datatrace import Sample, TracedVar, value_distribution
    from gdbtrace.trace.tasktrace import TaskTraceSink
    sink = TaskTraceSink()
    sink.names = {0x100: "producteur", 0x200: "consommateur"}
    sink.feed([(0, 0x100), (10, 0x200), (15, 0x100), (45, 0x200), (50, 0x100)])
    d = sink.distribution("producteur")
    assert (d.count, d.min, d.max) == (2, 10, 30) and d.longest[0] == (30, 3)
    assert sink.distribution(0x200).count == 2 and set(sink.task_names()) == {"producteur", "consommateur"}
    with pytest.raises(KeyError):
        sink.distribution("inconnue")
    var = TracedVar("mode", 0x2000, 4, 1, samples=deque(Sample(i, v, 4, "write") for i, v in
                                                          enumerate([1, 2, 2, 3])))
    dv = value_distribution(var, bins=3)
    assert (dv.count, dv.min, dv.max, dv.percentiles[50]) == (4, 1, 3, 2) and dv.longest[0] == (3, -1)


# ====================================================================== sous QEMU (ETM simulé)
def _stopped_after_run(s, seconds=1.5):
    import time
    s.go()
    time.sleep(seconds)
    s.halt()


@needs_qemu
def test_fib_durations_step_with_n(elf):
    """La démo appelle fib(n) avec n qui change à chaque tour : les durées de fib sont
    étagées (plusieurs classes occupées, p50 entre min et max) ; delay, lui, dure toujours
    pareil. Les appels les plus longs mènent à fib dans la trace."""
    from gdbtrace.core.scripting import ScriptAPI
    from gdbtrace.core.session import Session
    from gdbtrace.core.symbols import SymbolTable
    from gdbtrace.core.target import QemuTarget, free_port
    from gdbtrace.trace.manager import TraceManager
    s = Session()
    tm = TraceManager()
    try:
        target = QemuTarget(elf=elf, gdb_port=free_port(), etm_sim=True)
        s.connect(elf, target)
        sym = SymbolTable(elf)
        tm.setup_etm(elf, sym)
        live = target.live_memory()
        tm.enable_qemu_sim(live)
        tm.attach_session(s)
        api = ScriptAPI(s, live)
        api.trace, api.symbols = tm, sym
        _stopped_after_run(s)
        d = api.distribution("fib", bins=10)
        assert d.count >= 5 and d.min < d.percentiles[50] <= d.max
        assert sum(1 for b in d.bins if b.count) >= 3                   # durées étagées
        delay = api.distribution("delay", unit="cycles", show=False)
        assert delay.min == delay.max and len(delay.bins) == 1
        etm = tm.etm
        events = etm.last(etm.history.maxlen)
        _v, pos = d.longest[0]
        assert sym.function_at(events[pos].addr).name == "fib"         # l'entrée de l'appel
        assert api.distribution("SysTick", show=False).name == "[SysTick]"
        with pytest.raises(KeyError):
            api.distribution("inexistante", show=False)
    finally:
        tm.close()
        s.disconnect()


@needs_qemu
def test_distribution_tab_and_goto(elf, tmp_path):
    if not os.environ.get("DISPLAY"):
        pytest.skip("pas de DISPLAY")
    from test_gui import close_app, pump

    from gdbtrace.core.session import Session
    from gdbtrace.core.target import QemuTarget, free_port
    from gdbtrace.gui.app import App
    from gdbtrace.views.timing import TimingView
    from gdbtrace.views.trace import TraceView
    a = App(layout_file=str(tmp_path / "layout.json"))
    try:
        s = Session()
        s.connect(elf, QemuTarget(elf=elf, gdb_port=free_port(), etm_sim=True))
        a.attach(s)
        v = a.views.show(TimingView)
        a.go()
        assert pump(a, lambda: s.state == "running")
        import time
        time.sleep(0.8)
        a.halt()
        assert pump(a, lambda: s.state == "stopped" and a.trace.etm.stats["instructions"] > 1000
                    and not a.trace.etm_busy, timeout=60), (s.state, a.trace.etm.stats, a.trace.etm_busy)
        v.analyze()
        assert pump(a, lambda: v.result is not None and "fib" in v.result.durations, timeout=20)
        p = v.dist
        p.name_var.set("fib")
        p.bins_var.set("8")
        p.render()
        a.root.update()
        assert p.dist.count >= 5 and len(p.dist.bins) == 8
        assert p.info.cget("text").startswith(f"{p.dist.count} appels complets")
        rects = [i for i in p.canvas.find_all() if p.canvas.type(i) == "rectangle"]
        assert len(rects) == sum(1 for b in p.dist.bins if b.count)
        rows = {p.table.item(i, "text"): p.table.item(i, "values") for i in p.table.get_children()}
        assert {"minimum", "p50", "p90", "p99", "maximum", "moyenne", "plus longs"} <= set(rows)
        longest = p.table.get_children(next(i for i in p.table.get_children()
                                            if p.table.item(i, "text") == "plus longs"))[0]
        p.table.focus(longest)
        p._goto_call()                                       # la vue Trace à sa position
        tv = a.views.find(TraceView)
        sel = tv.listing.selection()
        assert sel and tv.listing.item(sel[0], "text") == str(p._where[longest])
        assert tv.listing.set(sel[0], "func") in ("fib", "")
        assert v.get_state()["distribution"]["name"] == "fib"
    finally:
        close_app(a)


@needs_qemu
def test_mcp_distribution_tool(elf):
    from test_mcp import Client
    c = Client()
    try:
        c("connect", args=f"{elf} --etm-sim")
        c("go", wait=1)
        c("halt")
        text = c("distribution", name="fib", bins=5)
        assert text.startswith("fib : ") and "p99" in text and "plus longs" in text
        assert "absente" in c("distribution", ok=False, name="absente")
    finally:
        c.close()
