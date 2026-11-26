"""SNOOPer (core/snooper.py) : variables échantillonnées par le canal mémoire, cible en marche,
sans trace ; même magasin d'échantillons que la trace de données du DWT."""
import os
import subprocess
import sys
import time

import pytest
from conftest import M0_DIR, ROOT, needs_qemu

from gdbtrace.core.live_vars import LeafLayout
from gdbtrace.core.snooper import Snooper
from gdbtrace.trace.datatrace import DataTraceSink


def test_snooper_samples_only_while_running():
    sink, mem, state = DataTraceSink(), {"v": 0}, {"run": False}

    def read(addr, n):
        mem["v"] += 1
        return mem["v"].to_bytes(n, "little")
    sn = Snooper(sink, read, lambda: state["run"], period_ms=2)
    var = sn.add("x", 0x2000, 4, LeafLayout(addr=0x2000, size=4, kind="uint", live=True))
    assert var.comparator < 0 and var.access == "sample" and sink.vars[var.comparator] is var
    time.sleep(0.05)
    assert var.count == 0                                # cible arrêtée : rien
    state["run"] = True
    time.sleep(0.1)
    state["run"] = False
    n = var.count
    assert n >= 10
    vals = [s.raw for s in var.samples]
    ts = [s.t for s in var.samples]
    assert vals == sorted(vals) and ts == sorted(ts) and var.number(var.samples[-1]) == vals[-1]
    time.sleep(0.03)
    assert var.count == n
    assert sn.remove("x") == ["x n'est plus échantillonnée"] and not sink.vars
    sn.close()


def test_read_errors_are_counted():
    sink = DataTraceSink()

    def read(addr, n):
        raise OSError("canal perdu")
    sn = Snooper(sink, read, lambda: True, period_ms=2)
    var = sn.add("x", 0x2000, 4, LeafLayout(addr=0x2000, size=4, kind="uint", live=True))
    time.sleep(0.05)
    sn.close()
    assert var.count == 0 and sn.errors > 0 and "canal perdu" in sn.last_error


SCRIPT = """
import time
gdbtrace.go(until="main"); gdbtrace.wait()
gdbtrace.go(); time.sleep(1.0); gdbtrace.halt(); gdbtrace.wait()
rows = gdbtrace.var_samples("ticks", None)
vals = [r.raw for r in rows]
assert len(vals) >= 40 and vals == sorted(vals) and vals[-1] > vals[0], vals[:10]
assert {r.kind for r in rows} == {"sample"}, rows[:3]
gdbtrace.snoop_stop("ticks")
print("OK", len(vals))
"""


@needs_qemu
@pytest.mark.parametrize("which", ["m3", "m0"])
def test_snoop_ticks_under_qemu(which, elf, m0_elf, tmp_path):
    """`--snoop ticks@10` : la globale incrémentée par SysTick, lue en marche par QMP, sur un
    Cortex-M3 (mps2-an385) et un Cortex-M0 (microbit, sans ITM ni DWT)."""
    script = tmp_path / "s.py"
    script.write_text(SCRIPT)
    target, extra = (elf, []) if which == "m3" else (os.path.join(M0_DIR, "demo.elf"), ["--machine", "microbit"])
    r = subprocess.run([sys.executable, "-m", "gdbtrace", target, *extra, "--batch", "--snoop", "ticks@10",
                        "--script", str(script)], cwd=ROOT, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0 and "OK" in r.stdout, r.stdout + r.stderr
    assert "ticks échantillonnée toutes les 10 ms" in r.stdout


@needs_qemu
def test_vartrace_view_sampled(elf, tmp_path):
    if not os.environ.get("DISPLAY"):
        pytest.skip("pas de DISPLAY")
    from test_gui import close_app, pump

    from gdbtrace.core.session import Session
    from gdbtrace.core.target import QemuTarget, free_port
    from gdbtrace.gui.app import App
    from gdbtrace.views.vartrace import SAMPLED, VarTraceView
    a = App(layout_file=str(tmp_path / "layout.json"))
    try:
        s = Session()
        s.connect(elf, QemuTarget(elf=elf, gdb_port=free_port()))
        a.attach(s)
        assert pump(a, lambda: s.state == "stopped")
        v = a.views.show(VarTraceView, "bottom")
        v.refresh()
        assert v.source_var.get() == SAMPLED                 # QEMU : pas de capture SWO
        v.period_var.set("5")
        v.trace_expr("ticks")
        assert v.wanted == [("ticks", "write", False, "sample", 5)]
        a.go()
        assert pump(a, lambda: next(iter(a.trace.datatrace.vars.values())).count > 20, timeout=10)
        a.halt()
        assert pump(a, lambda: s.state == "stopped")
        v.render(force=True)
        rows = [v.tree.item(i, "values") for i in v.tree.get_children()]
        assert rows and rows[0][0] == "échantillonné", rows
        v.tree.focus(v.tree.get_children()[0])
        v._remove()
        assert not a.trace.datatrace.vars and v.wanted == []
    finally:
        close_app(a)
