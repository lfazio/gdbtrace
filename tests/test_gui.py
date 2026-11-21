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


# ====================================================================== trace : profil et ITM
def test_profile_view_live_per_task(rtos_app):
    from gdbtrace.views.profile import ProfileView
    app = rtos_app
    run_to(app, "worker_step")
    pv = app.views.show(ProfileView)
    pv.start()
    assert pv.running_source == "Live"
    app.go()
    assert pump(app, lambda: pv.profile.total > 300, timeout=8)
    app.halt()
    assert pump(app, lambda: app.session.state == "stopped")
    pv.stop()
    funcs = [pv.funcs.item(i, "text") for i in pv.funcs.get_children()]
    assert funcs[0] == "prvTaskCheckFreeStackSpace"
    pv.funcs.focus(funcs[0])
    pv._show_lines()
    first_line = pv.lines.get_children()[0]
    assert pv.lines.item(first_line, "text").startswith("tasks.c:")
    tasks = {pv.tasks.item(i, "text"): float(pv.tasks.item(i, "values")[1]) for i in pv.tasks.get_children()}
    assert tasks["IDLE"] > 80
    pv._goto_line()                                   # double-clic → source
    from gdbtrace.views.code import SourceView
    assert app.views.find(SourceView).current_file.endswith("tasks.c")


def test_itm_view_with_fake_swo_server(rtos_app, rtos_elf):
    from test_itm import SYNC, hw, stim
    from test_swo import FakeSwoServer
    from gdbtrace.views.itm import ItmView
    from gdbtrace.views.profile import ProfileView
    app = rtos_app
    exc = hw(1, 15 | 1 << 12, 2) + bytes([0xC0, 100]) + hw(1, 15 | 2 << 12, 2)
    payload = (SYNC + b"".join(stim(0, c) for c in b"consumer: commande 8\n") + stim(1, 42, 4)
               + exc * 2)
    srv = FakeSwoServer(payload)
    srv.start()
    iv = app.views.show(ItmView)
    iv.addr_var.set(f"127.0.0.1:{srv.port}")
    iv._connect()
    assert pump(app, lambda: "0x0000002a" in iv.contents())
    assert iv.contents() == "[0] consumer: commande 8\n[1] 0x0000002a\n"
    assert app.api.itm(0) == "consumer: commande 8"
    pv = app.views.find(ProfileView)
    pv.render()
    rows = {pv.excs.item(i, "text"): pv.excs.item(i, "values") for i in pv.excs.get_children()}
    assert rows["SysTick (15)"] == ("2", "200", "100")
    # filtre de ports : seul le port 1
    iv.clear()
    iv.ports_var.set("1")
    iv.on_line(0, "ignoré", 0)
    iv.on_line(1, "gardé", 0)
    assert iv.contents() == "[1] gardé\n"
    app.trace.stop()


# ====================================================================== trace ETM (simulée sous QEMU)
@pytest.fixture
def etm_app(elf, tmp_path):
    from gdbtrace.gui.app import App
    a = App(layout_file=str(tmp_path / "layout.json"))
    s = Session()
    s.connect(elf, QemuTarget(elf=elf, gdb_port=free_port(), etm_sim=True))
    a.attach(s)
    yield a
    a.quit()


def test_trace_view_with_simulated_etm(etm_app):
    from gdbtrace.views.code import SourceView
    from gdbtrace.views.trace import TraceView
    app = etm_app
    assert app.trace.etm is not None and app.trace.etm_sim is not None
    tv = app.views.show(TraceView)
    app.session.break_("fib")
    app.go()
    assert pump(app, lambda: app.current_frame is not None and app.current_frame.func == "fib")
    assert pump(app, lambda: app.trace.etm.stats["instructions"] > 100 and not app.trace.etm_busy)
    pump(app, lambda: False, timeout=0.3)
    rows = tv.listing.get_children()
    last = tv.listing.item(rows[-1], "values")
    # dernière instruction tracée : dans fib, juste avant l'adresse du point d'arrêt
    ins = app.trace.etm.image.instr(int(last[0], 16))
    assert int(last[0], 16) + ins.size == app.current_frame.pc
    flow = [tv.flow.item(i, "values")[0].strip() for i in tv.flow.get_children()]
    assert flow[-1] == "→ fib" and "→ update_sensor" in flow
    cov = {tv.cov.item(i, "text"): tv.cov.item(i, "values") for i in tv.cov.get_children()}
    assert cov["peripherals_init"][1] == "100" and cov["Default_Handler"][1] == "0"
    # double-clic sur une ligne du listing → source
    tv.listing.focus(rows[-1])
    tv._goto(tv.listing)
    src = app.views.find(SourceView)
    assert src.current_file.endswith("main.c")
    # couverture dans la source : lignes de main() en vert
    tv.cov_var.set(True)
    tv._toggle_coverage()
    main_line = app.symbols.line_at(app.symbols.function("main").start).line
    assert "cov_hit" in src.text.tag_names(f"{main_line}.2")
    # 2e exécution : la trace s'enrichit (ETM simulé à chaque arrêt)
    n = app.trace.etm.stats["instructions"]
    app.go()
    assert pump(app, lambda: app.trace.etm.stats["instructions"] > n and not app.trace.etm_busy)
