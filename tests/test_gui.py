"""Tests de fumée de la GUI tkinter (nécessite un affichage : xvfb-run pytest)."""
import os
import time

import pytest
from conftest import FIB_LINE, needs_qemu

from gdbtrace.core.session import Session
from gdbtrace.core.target import QemuTarget, free_port

tk = pytest.importorskip("tkinter")
pytestmark = [needs_qemu, pytest.mark.skipif(not os.environ.get("DISPLAY"), reason="pas de DISPLAY")]


def pump(app, cond, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.root.update()
        if cond():
            return True
        time.sleep(0.02)
    return False


@pytest.fixture
def app(elf, tmp_path):
    from gdbtrace.gui.app import App
    a = App(layout_file=str(tmp_path / "layout.json"))
    s = Session()
    s.connect(elf, QemuTarget(elf=elf, gdb_port=free_port()))
    a.attach(s)
    yield a
    a.quit()


def test_views_follow_breakpoint(app):
    from gdbtrace.views.code import DisasmView, SourceView
    from gdbtrace.views.registers import RegistersView
    src = app.views.find(SourceView)
    assert src.current_file.endswith("startup.c")
    app.toggle_breakpoint_at(file=src.current_file.replace("startup.c", "main.c"), line=FIB_LINE)
    assert app.cached_breakpoints and app.cached_breakpoints[0].line == FIB_LINE
    app.go()
    assert pump(app, lambda: app.current_frame is not None and app.current_frame.func == "fib")
    assert src.current_file.endswith("main.c")
    assert "current" in src.text.tag_names(f"{FIB_LINE}.4")
    regs = app.views.find(RegistersView)
    assert "pc" in regs.tree.get_children()
    dis = app.views.find(DisasmView)
    assert 0xAC in dis.line_addr.values()


def test_console_and_watch(app):
    from gdbtrace.views.console import ConsoleView
    from gdbtrace.views.data import WatchView
    con = app.views.find(ConsoleView)
    app.session.break_("main", temporary=True)
    app.go()
    assert pump(app, lambda: app.current_frame is not None and app.current_frame.func == "main")
    app.execute("x = t32.u32(0x20000000); print('X=', x)")
    assert pump(app, lambda: "X= 1" in con.out.get("1.0", "end"))
    app.execute("!info line main")
    assert pump(app, lambda: "starts at address" in con.out.get("1.0", "end"))
    w = app.views.find(WatchView)
    w.add("sensors")
    root = w.tree.get_children()[0]
    assert w.tree.set(root, "type") == "struct sensor [4]"


def test_detach_and_layout(app, tmp_path):
    from gdbtrace.views.memory import MemoryView
    m = app.views.find(MemoryView)
    m.addr_var.set("sensors")
    m2 = app.views.move(m, None)
    assert app.views.where(m2) is None and m2.addr_var.get() == "sensors"
    m2.refresh()
    assert m2.text.get("1.0", "1.8") == "20000000"
    app.save_layout()
    import json
    data = json.loads((tmp_path / "layout.json").read_text())
    assert {"kind": "memory", "where": None} .items() <= next(v for v in data["views"] if v["kind"] == "memory").items()


def run_to(app, func):
    app.session.break_(func, temporary=True)
    app.go()
    assert pump(app, lambda: app.current_frame is not None and app.current_frame.func == func)


def test_live_views_while_running(app):
    """Watch, Mémoire et Périphériques se mettent à jour pendant que la cible tourne."""
    from conftest import SVD
    from gdbtrace.views.data import WatchView
    from gdbtrace.views.memory import MemoryView
    from gdbtrace.views.peripherals import PeripheralsView
    assert app.live is not None and app.poller is not None
    assert app.live_label.cget("bg") == "#208020"
    app.load_svd(SVD)
    run_to(app, "animate")                      # SysTick et TIMER0 configurés

    w = app.views.find(WatchView)
    w.add("ticks")
    w.add("tick")                               # locale : gelée en marche
    items = {w.tree.item(i, "text"): i for i in w.tree.get_children()}
    assert w.layouts[items["ticks"]].live and not w.layouts[items["tick"]].live

    p = app.views.find(PeripheralsView)
    p.expand("TIMER0")
    p.expand("SysTick")
    assert p.tree.set("SysTick.CTRL.CLKSOURCE", "value") == "1  PROCESSOR"
    assert p.tree.set("TIMER0.CTRL.EN", "value") == "1  ENABLED"

    m = app.views.find(MemoryView)
    m.addr_var.set("ticks")
    m.len_var.set("16")
    m.refresh()

    ticks0 = int(w.tree.set(items["ticks"], "value"))
    timer0 = p.tree.set("TIMER0.VALUE", "value")
    mem0 = m.text.get("1.0", "1.end")
    app.go()
    assert pump(app, lambda: app.session.state == "running")
    assert pump(app, lambda: int(w.tree.set(items["ticks"], "value")) > ticks0 + 20)
    assert pump(app, lambda: p.tree.set("TIMER0.VALUE", "value") != timer0)
    assert pump(app, lambda: m.text.get("1.0", "1.end") != mem0)
    assert "disabled" in w.tree.item(items["tick"], "tags")
    assert "changed" in w.tree.item(items["ticks"], "tags")

    app.halt()
    assert pump(app, lambda: app.session.state == "stopped")
    pump(app, lambda: False, timeout=0.3)
    assert w.tree.set(items["ticks"], "value") == app.session.evaluate("ticks")


def test_peripheral_write_and_layout_state(app, tmp_path, monkeypatch):
    from conftest import SVD
    from gdbtrace.views.peripherals import PeripheralsView
    app.load_svd(SVD)
    run_to(app, "animate")
    p = app.views.find(PeripheralsView)
    p.expand("TIMER0")
    # écriture d'un champ (lecture-modification-écriture) : EN 1 → 0, autres bits conservés
    writes = []
    real_write = app.session.write_memory
    monkeypatch.setattr(app.session, "write_memory",
                        lambda a, d: (writes.append((a, d)), real_write(a, d)))
    monkeypatch.setattr(p, "_ask_field", lambda f: 0)
    p.tree.focus("TIMER0.CTRL.EN")
    p._edit(None)
    assert writes == [(0x40000000, (0x0).to_bytes(4, "little"))]
    # QEMU ignore les écritures périphériques du débogueur : la relecture le révèle
    assert app.session.read_u32(0x40000000) & 1 == 1
    assert "relu 0x1" in app.status.get() and "ignorée" in app.status.get()
    # registre en lecture seule : refusé
    p.expand("SCB")
    p.tree.focus("SCB.CPUID")
    p._edit(None)
    assert "lecture seule" in app.status.get()
    # le SVD et les périphériques dépliés sont mémorisés dans le layout
    state = p.get_state()
    assert state["svd"].endswith("mps2-an385.svd") and "TIMER0" in state["open"]


def test_internal_cli_output_not_in_console(app):
    from gdbtrace.views.console import ConsoleView
    con = app.views.find(ConsoleView)
    app.session.ptype("ticks")
    pump(app, lambda: False, timeout=0.2)
    assert "type =" not in con.out.get("1.0", "end")
    app.execute("!ptype ticks")                 # commande explicite : affichée
    assert pump(app, lambda: "type = volatile unsigned long" in con.out.get("1.0", "end"))


# ====================================================================== vue RTOS (firmware FreeRTOS)
@pytest.fixture
def rtos_app(rtos_elf, tmp_path):
    from gdbtrace.gui.app import App
    a = App(layout_file=str(tmp_path / "layout.json"))
    s = Session()
    s.connect(rtos_elf, QemuTarget(elf=rtos_elf, gdb_port=free_port()))
    a.attach(s)
    yield a
    a.quit()


def test_rtos_view(rtos_app):
    import tkinter as tk
    from gdbtrace.views.rtos import RtosView
    app = rtos_app
    v = app.views.show(RtosView)
    pump(app, lambda: False, timeout=0.2)
    assert v.layout is not None and v.snap.tasks == []        # au reset : aucune tâche
    assert "non démarré" in v.info.cget("text")

    run_to(app, "worker_step")
    names = {v.tasks.item(i, "text"): v.tasks.item(i, "values") for i in v.tasks.get_children()}
    assert set(names) == {"producer", "consumer", "worker", "sleeper", "hog", "IDLE", "Tmr Svc"}
    assert names["worker"][0] == "Running" and app.task_label.cget("text") == "tâche : worker"
    assert names["consumer"][6] == "cmd_queue (réception)"
    objs = {v.objects.item(i, "text"): v.objects.item(i, "values") for i in v.objects.get_children()}
    assert objs["data_mutex"][1] == "pris par worker"

    # fenêtre de contexte d'une tâche bloquée
    cons = next(i for i in v.tasks.get_children() if v.tasks.item(i, "text") == "consumer")
    v.tasks.focus(cons)
    v._open_context()
    wins = [w for w in v.winfo_children() if isinstance(w, tk.Toplevel)]
    assert len(wins) == 1
    frames = [wins[0].frames_tv.item(i, "values")[0] for i in wins[0].frames_tv.get_children()]
    assert frames == ["xQueueReceive", "consumer_task"]
    assert app.session.frame().func == "worker_step"           # registres restaurés
    wins[0].destroy()

    # en marche : mise à jour par le canal live
    tick0 = v.snap.tick
    app.go()
    assert pump(app, lambda: v.snap.tick > tick0 + 50)
    states = {v.tasks.item(i, "text"): v.tasks.item(i, "values")[0] for i in v.tasks.get_children()}
    assert states["sleeper"] == "Suspended"
    assert pump(app, lambda: any(v.tasks.item(i, "values")[5] for i in v.tasks.get_children()))
    app.halt()
    assert pump(app, lambda: app.session.state == "stopped")
