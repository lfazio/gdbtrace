"""Tests de fumée de la GUI tkinter (nécessite un affichage : xvfb-run pytest)."""
import os
import time

import pytest
from conftest import FIB_LINE, needs_qemu

from gdbtrace.core.session import Session
from gdbtrace.core.target import QemuTarget, free_port

tk = pytest.importorskip("tkinter")
pytestmark = [needs_qemu, pytest.mark.skipif(not os.environ.get("DISPLAY"), reason="pas de DISPLAY")]


def close_app(app) -> None:
    """Ferme l'App puis ramasse ses objets tkinter dans le thread principal.

    Sinon, le ramasse-miettes peut se déclencher plus tard dans un thread de travail (trace,
    échantillonneur) : chaque `Variable.__del__` d'une App détruite y attend alors jusqu'à
    une seconde un mainloop Tk qui ne tourne pas (« main thread is not in main loop »),
    ce qui bloque ce thread pendant des secondes.
    """
    import gc
    app.quit()
    gc.collect()


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
    close_app(a)


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
    app.execute("x = gdbtrace.u32(0x20000000); print('X=', x)")
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


def test_chrono_in_status_bar(app):
    """Chaque arrêt affiche le temps de la dernière exécution (SysTick sous QEMU) et le
    cumul ; Run › Remettre le chrono à zéro efface le cumul."""
    run_to(app, "fib")                       # depuis le reset : SysTick pas encore lancé
    assert app.chrono_label.cget("text") == ""
    run_to(app, "animate")
    assert pump(app, lambda: "⏱" in app.chrono_label.cget("text"))
    first = app.api._chrono.total
    run_to(app, "animate")
    assert pump(app, lambda: app.api._chrono.total > first)
    text = app.chrono_label.cget("text")
    # la démo compte ses ticks dans `ticks`, pas dans un compteur connu : à une période près
    assert "SysTick (QEMU : temps simulé)" in text and "Σ" in text and "≈" in text
    app.reset_chrono()
    assert app.api._chrono.total == 0 and app.chrono_label.cget("text") == "⏱ Σ 0"


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
    # QEMU 8.2 ignore les écritures périphériques du débogueur, la relecture le révèle ;
    # QEMU 11 les applique : rien à signaler
    if app.session.read_u32(0x40000000) & 1:
        assert "relu 0x1" in app.status.get() and "ignorée" in app.status.get()
    else:
        assert "ignorée" not in app.status.get()
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
    close_app(a)


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
    assert pump(app, lambda: pv.profile.total > 300), (pv.profile.total, pv.sampler and pv.sampler.errors)
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
    # double-clic sur la ligne la plus chaude → source (la vue Source ne suit plus le PC)
    pv.lines.focus(first_line)
    pv._goto_line()
    from gdbtrace.views.code import SourceView
    src = app.views.find(SourceView)
    hot_line = int(pv.lines.item(first_line, "text").split(":")[1])
    assert src.current_file.endswith("tasks.c") and not src.follow.get()
    assert pump(app, lambda: src.text.bbox(f"{hot_line}.0") is not None, timeout=3)  # ligne visible


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
    tick = rows["SysTick (15)"]                    # entrées, total, min, max (horodatage ITM)
    assert tick[:2] == ("2", "200") and tick[3] == "100"
    # filtre de ports : seul le port 1
    iv.clear()
    iv.ports_var.set("1")
    iv.on_line(0, "ignoré", 0)
    iv.on_line(1, "gardé", 0)
    app.root.update()                       # lignes affichées par salve, au repos de Tk
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
    close_app(a)


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
    assert pump(app, lambda: trace_drawn(tv))               # rendu à la notification suivante
    rows = tv.listing.get_children()
    last = tv.listing.item(rows[-1], "values")
    # dernière instruction tracée : dans fib, juste avant l'adresse du point d'arrêt
    ins = app.trace.etm.image.instr(int(last[0], 16))
    assert int(last[0], 16) + ins.size == app.current_frame.pc
    flow = [tv.flow.item(i, "values")[0].strip() for i in tv.flow.get_children()]
    assert "→ update_sensor" in flow and "→ fib" in flow
    # dernier appel : fib ; une interruption a pu suivre (⚡ … ↩), la trace reprend dans fib
    after = flow[len(flow) - 1 - flow[::-1].index("→ fib"):]
    assert after[-1] in ("→ fib", "• fib"), after
    assert after[-1] == "→ fib" or "↩" in after, after
    # double-clic sur une ligne du listing → source
    tv.listing.focus(rows[-1])
    tv._goto(tv.listing)
    src = app.views.find(SourceView)
    assert src.current_file.endswith("main.c")
    # 2e exécution : la trace s'enrichit (ETM simulé à chaque arrêt). QEMU chargé : le pas qui
    # enjambe le breakpoint revient parfois sans avoir avancé, et gdb revoit le même arrêt sans
    # rien avoir exécuté (limite connue, CLAUDE.md) : on relance, trois fois au plus
    n = app.trace.etm.stats["instructions"]
    for _ in range(3):
        app.go()
        grew = pump(app, lambda: app.trace.etm.stats["instructions"] > n and not app.trace.etm_busy)
        if grew:
            break
        assert pump(app, lambda: app.session.state == "stopped"), etm_diagnostics(app)
    assert grew, etm_diagnostics(app)


def trace_drawn(tv) -> bool:
    """La liste de la vue Trace est dessinée pour la trace actuelle. Ce rendu suit la
    notification qui suit la fin du décodage (au plus ETM_NOTIFY_PERIOD plus tard), pas
    l'arrêt : une attente fixe après l'arrêt ne suffit pas toujours."""
    return (tv.etm is not None and tv._generation == tv.etm.generation
            and bool(tv.listing.get_children()))


def coverage_drawn(cv) -> bool:
    """L'arbre de la vue Couverture reflète la base actuelle (pas un rendu d'avant le Go)."""
    db = cv.db
    return (db is not None and cv._gen == (id(db), db.generation, cv.hide_var.get())
            and bool(cv.tree.get_children()))


def etm_diagnostics(app) -> str:
    """Message d'échec : état de la simulation ETM et erreurs affichées dans la console."""
    from gdbtrace.views.console import ConsoleView
    sim = app.trace.etm_sim
    con = app.views.find(ConsoleView)
    errors = [l for l in con.out.get("1.0", "end").splitlines() if "Error" in l or "ETM" in l]
    return (f"état={app.session.state} frame={app.current_frame} busy={app.trace.etm_busy} "
            f"runs={sim and sim.runs} path={sim and sim.path} stats={app.trace.etm.stats} "
            f"erreurs={errors[-5:]}")


def test_timing_view_with_simulated_etm(etm_app):
    """Vue Timing : statistiques et chronogramme d'une exécution réelle (ETM simulé)."""
    from gdbtrace.views.code import SourceView
    from gdbtrace.views.timing import OTHER, SLOTS, TimingView
    app = etm_app
    tv = app.views.show(TimingView)
    assert tv.result is None
    app.go()
    pump(app, lambda: False, timeout=0.4)
    app.halt()
    assert pump(app, lambda: app.session.state == "stopped")
    assert pump(app, lambda: app.trace.etm.stats["instructions"] > 10_000 and not app.trace.etm_busy), \
        etm_diagnostics(app)
    assert pump(app, lambda: tv.result is not None and not tv._busy, timeout=30)
    r = tv.result
    assert sum(s.net for s in r.stats.values()) == r.total > 0
    assert "estimé" in tv.info.cget("text")             # ETM simulé : cycles du modèle
    # tableau : une ligne par fonction / exception, trié par temps brut décroissant
    rows = tv.table.get_children()
    assert {"delay", "[SysTick]", "SysTick_Handler"} <= set(rows)
    assert tv.table.set("[SysTick]", "kind") == "exception"
    gross = [r.stats[i].gross for i in rows]
    assert gross == sorted(gross, reverse=True)
    tv._sort("calls")                                    # tri par nombre d'appels
    calls = [r.stats[i].calls for i in tv.table.get_children()]
    assert calls == sorted(calls, reverse=True)
    # unités : µs à 25 MHz, puis cycles
    d = r.stats["delay"]
    assert tv.table.set("delay", "max") == tv.fmt(d.max)
    tv.unit_var.set("cycles")
    tv._render()
    assert tv.table.set("delay", "max") == f"{d.max:,}".replace(",", " ")
    # couleurs : par identité, slots dans l'ordre, au plus 7, le reste en « Autres »
    assert 0 < len(tv.color_of) <= 7 and set(tv.color_of.values()) <= set(SLOTS)
    ranked = sorted(r.stats.values(), key=lambda s: (-s.gross, s.name))
    assert [tv.color(s.name) for s in ranked[:7]] == SLOTS[:7]       # premier résultat : ordre fixe
    assert len(ranked) <= 7 or tv.color(ranked[-1].name) == OTHER
    # chronogramme : rectangles visibles, zoom, survol, clic → source
    tv.tabs.select(next(t for t in tv.tabs.tabs() if tv.tabs.tab(t, "text") == "Chronogramme"))
    pump(app, lambda: False, timeout=0.3)
    ch = tv.chart
    n_all = len(ch.items)
    assert n_all > 0
    ch.reset_zoom()
    # zoom sur un appel de update_sensor : il doit apparaître avec son nom
    iv = next(i for i in r.intervals if i.name == "update_sensor" and not i.partial_start)
    ch.v0, ch.v1 = iv.start - 10, iv.end + 10
    ch.draw()
    rid = next(k for k, idx in ch.items.items() if r.intervals[idx] is iv)
    x0, y0, x1, y1 = ch.canvas.coords(rid)
    labels = [ch.canvas.itemcget(t, "text") for t in ch.canvas.find_withtag("label")]
    assert "update_sensor" in labels
    assert ch.interval_at((x0 + x1) / 2, (y0 + y1) / 2) is iv
    ch._motion(type("E", (), {"x": (x0 + x1) / 2, "y": (y0 + y1) / 2})())
    tip = [ch.canvas.itemcget(t, "text") for t in ch.canvas.find_withtag("tip")
           if ch.canvas.type(t) == "text"]
    assert tip[0].endswith("cycles") and "update_sensor" in tip[1]   # la valeur d'abord
    before = ch.v1 - ch.v0
    ch.zoom((x0 + x1) / 2, 2.0)
    assert ch.v1 - ch.v0 == pytest.approx(before / 2)
    ch.v0, ch.v1 = iv.start - 10, iv.end + 10
    ch.draw()
    rid = next(k for k, idx in ch.items.items() if r.intervals[idx] is iv)
    x0, y0, x1, y1 = ch.canvas.coords(rid)
    ch._press(type("E", (), {"x": (x0 + x1) / 2, "y": (y0 + y1) / 2})())     # clic sans glisser
    ch._release(type("E", (), {"x": (x0 + x1) / 2, "y": (y0 + y1) / 2})())
    src = app.views.find(SourceView)
    assert src.current_file.endswith("main.c")
    line = app.symbols.line_at(iv.entry).line
    pump(app, lambda: False, timeout=0.2)
    assert src.text.bbox(f"{line}.0") is not None        # ligne d'entrée visible


def test_watch_added_while_running_or_out_of_scope(app):
    """Watch : une expression ajoutée cible en marche s'affiche tout de suite et est évaluée à
    l'arrêt suivant ; une locale hors de portée est réessayée à chaque arrêt."""
    from gdbtrace.views.data import WatchView
    w = app.views.show(WatchView)
    rows = lambda: {w.tree.item(i, "text"): w.tree.set(i, "value") for i in w.tree.get_children()}  # noqa: E731
    w.add("n")                                   # au reset : aucune locale `n`
    assert "hors de portée" in rows()["n"]
    app.go()
    assert pump(app, lambda: app.session.state == "running")
    w.add("counter")
    assert rows()["counter"] == "<évaluée au prochain arrêt>"
    app.halt()
    assert pump(app, lambda: app.session.state == "stopped" and rows()["counter"].isdigit())
    assert list(rows()) == ["n", "counter"]                       # ordre d'ajout conservé
    run_to(app, "fib")
    assert pump(app, lambda: rows()["n"].isdigit())               # locale de fib, enfin visible
    assert int(rows()["n"]) == int(app.session.evaluate("n"))


def test_files_view(app):
    """Arborescence des fichiers : fichiers du projet, fonctions, filtre, ouverture."""
    from gdbtrace.views.code import SourceView
    from gdbtrace.views.files import EXTERNAL, FilesView, build_tree
    fv = app.views.find(FilesView)
    assert fv is not None and app.views.where(fv) == "left"
    top = [fv.tree.item(i, "text") for i in fv.tree.get_children()]
    assert top[:2] == ["main.c", "startup.c"] and top[-1] == EXTERNAL
    main = fv.file_item[os.path.normpath(app.symbols.line_at(app.symbols.function("fib").start).file)]
    fv.tree.focus(main)
    fv.tree.item(main, open=True)
    fv._expand(None)
    funcs = [fv.tree.item(i, "text") for i in fv.tree.get_children(main)]
    assert "fib()" in funcs and "main()" in funcs
    fib = next(i for i in fv.tree.get_children(main) if fv.tree.item(i, "text") == "fib()")
    fv.tree.focus(fib)
    fv._activate()
    src = app.views.find(SourceView)
    assert src.current_file.endswith("main.c")
    pump(app, lambda: False, timeout=0.2)
    assert src.text.bbox(f"{FIB_LINE - 1}.0") is not None       # en-tête de fib() visible
    # filtre sur un nom de fonction : seuls les fichiers qui la contiennent, dépliés
    fv.filter_var.set("Reset_Handler")
    files = list(fv.target.values())
    assert [os.path.basename(f) for f, ln in files if ln == 0] == ["startup.c"]
    assert any(ln > 0 for _f, ln in files)
    fv.filter_var.set("")
    # fichier courant en gras après un arrêt dans fib
    run_to(app, "fib")
    main_c = os.path.normpath(app.symbols.line_at(app.symbols.function("fib").start).file)
    assert "current" in fv.tree.item(fv.file_item[main_c], "tags")
    # fusion des dossiers à enfant unique
    root, tree = build_tree(["/p/a/b/c/x.c", "/p/y.c", "/usr/lib/z.c"], "/p")
    assert root == "/p" and set(tree) == {"a/b/c/", "y.c", EXTERNAL}


def test_disasm_mixed_source(app):
    """Désassemblage avec source C intercalée (List.Mix) ; breakpoint sur une ligne C."""
    from gdbtrace.views.code import DisasmView
    run_to(app, "fib")
    dv = app.views.show(DisasmView)
    pump(app, lambda: False, timeout=0.2)
    assert dv.mixed.get()
    text = dv.text.get("1.0", "end")
    assert f"main.c:{FIB_LINE}" in text and "uint32_t a = 0, b = 1;" in text
    # chaque ligne C précède ses instructions
    src_rows = sorted(dv.line_src)
    row = next(r for r in src_rows if dv.line_src[r][1] == FIB_LINE)
    assert row + 1 in dv.line_addr
    addr = dv.line_addr[row + 1]
    assert app.symbols.line_at(addr).line == FIB_LINE
    # double-clic sur une ligne C -> breakpoint source
    dv.text.see(f"{row}.0")
    pump(app, lambda: False, timeout=0.1)
    x, y, _w, h = dv.text.bbox(f"{row}.2")
    dv._toggle_bp(type("E", (), {"x": x, "y": y + h // 2})())
    assert pump(app, lambda: any(bp.line == FIB_LINE for bp in app.cached_breakpoints))
    # sans source mêlée : seulement l'assembleur
    dv.mixed.set(False)
    dv._toggle_mixed()
    assert not dv.line_src and f"main.c:{FIB_LINE}" not in dv.text.get("1.0", "end")


def test_program_elf_from_menu(app, rtos_elf):
    """Fichier › Programmer un ELF… : nouvel ELF dans la cible, symboles et vues suivent."""
    from gdbtrace.views.data import WatchView
    from gdbtrace.views.files import FilesView
    w = app.views.show(WatchView)
    w.add("ticks")
    app.go()
    assert pump(app, lambda: app.session.state == "running")
    app.program_elf(rtos_elf)                                   # cible en marche : arrêtée d'abord
    assert app.busy == "programmation de freertos.elf"
    app.go()                                                    # refusé pendant la programmation
    assert "programmation de freertos.elf en cours" in app.status.get()
    assert pump(app, lambda: "vérifié" in app.status.get(), timeout=30), app.status.get()
    assert app.busy is None and app.session.state == "stopped"
    pump(app, lambda: False, timeout=0.3)
    assert app.session.elf == rtos_elf and app.symbols.path == rtos_elf
    assert app.current_frame.func == "Reset_Handler" and app.recent_elfs[0] == rtos_elf
    assert app.root.title().startswith("gdbtrace — freertos.elf : Reset_Handler")
    fv = app.views.find(FilesView)
    roots = [fv.tree.item(i, "text") for i in fv.tree.get_children()]
    assert {"freertos/", "common/"} <= set(roots), roots      # demo FreeRTOS et bloc RTT commun
    rows = {w.tree.item(i, "text"): w.tree.set(i, "value") for i in w.tree.get_children()}
    assert "ticks" in rows and "hors de portée" in rows["ticks"]     # absent du nouveau programme
    run_to(app, "worker_step")                                  # le nouveau programme tourne
    # Reprogrammer : même ELF, la cible repart du reset
    app.reprogram()
    assert pump(app, lambda: app.current_frame is not None
                and app.current_frame.func == "Reset_Handler", timeout=30)


def test_program_elf_without_connection(elf, tmp_path):
    """Sans cible connectée, Programmer lance la cible de la ligne de commande avec l'ELF."""
    from gdbtrace.gui.app import App
    a = App(layout_file=str(tmp_path / "layout.json"))
    try:
        a.target_factory = lambda e: QemuTarget(elf=e, gdb_port=free_port())
        a.program_elf(elf)
        assert pump(a, lambda: a.session is not None and "vérifié" in a.status.get(), timeout=30), \
            a.status.get()
        assert a.current_frame.func == "Reset_Handler" and a.symbols.path == elf
        a.save_layout()
        import json
        assert json.load(open(tmp_path / "layout.json"))["recent_elfs"] == [elf]
    finally:
        close_app(a)


def test_program_elf_retargets_simulated_etm(etm_app, rtos_elf):
    """ETM simulé : après programmation, la trace est décodée avec le code du nouvel ELF."""
    app = etm_app
    app.program_elf(rtos_elf)
    assert pump(app, lambda: "vérifié" in app.status.get(), timeout=30), app.status.get()
    etm = app.trace.etm
    assert etm.image is not None and app.trace.etm_sim is not None and etm.stats["instructions"] == 0
    run_to(app, "worker_step")
    assert pump(app, lambda: etm.stats["instructions"] > 1000 and not app.trace.etm_busy), \
        etm_diagnostics(app)
    assert etm.stats["erreurs"] == 0 and etm.stats["code inconnu"] == 0
    assert "→ worker_step" in [f"→ {r.func}" for r in etm.call_flow(5000) if r.kind == "call"]



def test_coverage_view(etm_app, tmp_path):
    """Vue Couverture : arbre fichiers/fonctions, annotations Source et Désassemblage, export."""
    from gdbtrace.views.code import DisasmView, SourceView
    from gdbtrace.views.coverage import CoverageView
    app = etm_app
    cv = app.views.show(CoverageView)
    run_to(app, "animate")
    app.go()
    pump(app, lambda: False, timeout=0.4)
    app.halt()
    assert pump(app, lambda: app.session.state == "stopped" and not app.trace.etm_busy
                and app.trace.etm.stats["instructions"] > 10_000), etm_diagnostics(app)
    assert pump(app, lambda: coverage_drawn(cv) and "instructions" in cv.info.cget("text"))
    assert "ETM simulé" in cv.info.cget("text")
    files = {cv.tree.item(i, "text"): i for i in cv.tree.get_children()}
    assert list(files)[:2] == ["main.c", "startup.c"]     # projet d'abord (racine de la vue Fichiers)
    funcs = {cv.tree.item(i, "text"): cv.tree.item(i, "values")
             for f in files.values() for i in cv.tree.get_children(f)}
    assert funcs["fib"][0] == "✔ ok" and funcs["Default_Handler"][0] == "✘ jamais"
    fadd = next(v for k, v in funcs.items() if "__addsf3" in k)
    assert fadd[0] == "½ partiel" and int(fadd[6]) > 0                    # entrées
    cv.hide_var.set(True)
    cv.render(force=True)
    shown = {cv.tree.item(i, "text") for f in cv.tree.get_children() for i in cv.tree.get_children(f)}
    assert "fib" not in shown and "Default_Handler" in shown
    # double-clic sur une fonction : source annotée
    item = next(i for f in cv.tree.get_children() for i in cv.tree.get_children(f)
                if cv.tree.item(i, "text") == "Default_Handler")
    cv.tree.focus(item)
    cv._goto()
    src = app.views.find(SourceView)
    assert cv.annotate_var.get() and app.coverage_in_source and src.current_file.endswith("startup.c")
    dh = app.symbols.line_at(app.symbols.function("Default_Handler").start).line
    rh = app.symbols.line_at(app.symbols.function("Reset_Handler").start).line
    assert src.text.get(f"{dh}.1") == "✘" and "cov_never" in src.text.tag_names(f"{dh}.3")
    assert src.text.get(f"{rh}.1") == "✔" and "cov_ok" in src.text.tag_names(f"{rh}.3")
    # désassemblage : instructions partielles nommées
    dv = app.views.show(DisasmView)
    dv.follow.set(False)
    dv._show(app.symbols.function("__addsf3").start)
    text = dv.text.get("1.0", "end")
    assert "branchement toujours pris" in text or "branchement jamais pris" in text
    assert dv.text.tag_ranges("cov_partial") and dv.text.tag_ranges("cov_ok")
    # sans annotation : plus de marques
    cv.annotate_var.set(False)
    cv._toggle_annotate()
    assert src.text.get(f"{dh}.1") == " " and not dv.text.tag_ranges("covnote")
    # RàZ, export
    cv.export(str(tmp_path / "c.info"))
    assert "SF:" in (tmp_path / "c.info").read_text()
    cv._reset()
    assert "instructions 0.0 %" in cv.info.cget("text")


@pytest.fixture
def mtb_app(m0_elf, tmp_path):
    """Démo Cortex-M0+ sous QEMU microbit, MTB simulée (tampon de 1 Ko du firmware)."""
    from gdbtrace.gui.app import App
    a = App(layout_file=str(tmp_path / "layout.json"))
    s = Session()
    s.connect(m0_elf, QemuTarget(elf=m0_elf, machine="microbit", gdb_port=free_port(), mtb_sim=1024))
    a.attach(s)
    yield a
    close_app(a)


def test_mtb_simulated_views(mtb_app):
    """MTB simulée : Trace.List, flot d'appels, Couverture et Timing alimentés comme par l'ETM."""
    from gdbtrace.views.coverage import CoverageView
    from gdbtrace.views.timing import TimingView
    from gdbtrace.views.trace import TraceView
    app = mtb_app
    assert app.trace.mtb_sim is not None and app.trace.etm_sim is None
    tv = app.views.show(TraceView)
    run_to(app, "animate")
    assert pump(app, lambda: app.trace.etm.stats["instructions"] > 50 and not app.trace.etm_busy), \
        etm_diagnostics(app)
    assert pump(app, lambda: trace_drawn(tv))               # rendu à la notification suivante
    assert "MTB simulée (QEMU, 1024 octets)" in tv.info.cget("text")
    rows = tv.listing.get_children()
    last = tv.listing.item(rows[-1], "values")
    ins = app.trace.etm.image.instr(int(last[0], 16))
    assert int(last[0], 16) + ins.size == app.current_frame.pc        # jusqu'au point d'arrêt
    flow = [tv.flow.item(i, "values")[0].strip() for i in tv.flow.get_children()]
    assert "→ main" in flow and flow[-1] == "→ animate"                # depuis le reset : complet
    cv = app.views.show(CoverageView)
    cv.render(force=True)
    assert "MTB simulée" in cv.info.cget("text")
    funcs = {cv.tree.item(i, "text"): cv.tree.item(i, "values")
             for f in cv.tree.get_children() for i in cv.tree.get_children(f)}
    assert funcs["peripherals_init"][0] == "✔ ok" and funcs["HardFault_Handler"][0] == "✘ jamais"
    tm = app.views.show(TimingView)
    tm.analyze()
    assert pump(app, lambda: tm.result is not None and not tm._busy, timeout=30)
    assert "estimé (modèle Cortex-M0, MTB simulée)" in tm.info.cget("text")   # QEMU microbit : un M0


def test_mtb_autohalt_views(m0_elf, tmp_path):
    """MTB simulée en AUTOHALT : pendant les arrêts internes, l'interface voit la cible en
    marche ; la trace grossit ; Break livre un vrai arrêt."""
    from gdbtrace.gui.app import App
    from gdbtrace.views.trace import TraceView
    a = App(layout_file=str(tmp_path / "layout.json"))
    try:
        s = Session()
        s.connect(m0_elf, QemuTarget(elf=m0_elf, machine="microbit", gdb_port=free_port(),
                                     mtb_sim=1024, mtb_autohalt=True))
        a.attach(s)
        tv = a.views.show(TraceView)
        a.go()
        states = []
        deadline = time.monotonic() + 1.2
        while time.monotonic() < deadline:
            a.root.update()
            states.append(a.session.state)
            time.sleep(0.02)
        sim = a.trace.mtb_sim
        assert len(states) > 25 and set(states[5:]) == {"running"}    # interface fluide
        assert sim.autohalts >= 2                                      # arrêts internes
        a.halt()                    # tronçons convertis à l'arrêt visible (coût de la simulation)
        assert pump(a, lambda: a.session.state == "stopped" and not a.trace.etm_busy, timeout=120)
        pump(a, lambda: False, timeout=0.3)
        assert "AUTOHALT" in tv.info.cget("text")
        assert a.trace.etm.stats["instructions"] == sim.logged and sim.decoder.stats.errors == 0
    finally:
        close_app(a)


def os_kinds(app) -> set[str]:
    return {v.kind for v in app.views.views} & {"rtos", "tasktime", "threads"}


def test_configuration_open_connect_close(rtos_elf, tmp_path):
    """Configuration › Ouvrir… charge sans connecter (titre, SVD, vues selon l'OS) ;
    Connecter lance la session décrite ; Fermer déconnecte et rouvre les vues de l'OS."""
    import json

    from conftest import ROOT
    from gdbtrace.gui.app import App
    from gdbtrace.views.rtos import RtosView
    path = os.path.join(ROOT, "configs", "qemu-freertos.yaml")
    a = App(layout_file=str(tmp_path / "layout.json"))
    try:
        assert a.open_config(path)
        assert os_kinds(a) == {"rtos", "tasktime"}               # FreeRTOS : pas de Threads
        assert "[FreeRTOS sous QEMU]" in a.root.title() and a.session is None
        assert pump(a, lambda: a.device is not None and a.device.name == "MPS2_AN385")
        a.connect_config()
        assert pump(a, lambda: a.session is not None and a.session.state == "stopped"
                    and a.current_frame is not None, timeout=30)
        assert a.api.config["os"] == "freertos"
        a.go()
        pump(a, lambda: False, timeout=0.5)
        a.halt()
        rv = a.views.find(RtosView)
        assert pump(a, lambda: a.session.state == "stopped" and len(rv.tasks.get_children()) >= 7,
                    timeout=20)
        first = a.session
        a.connect_config()                                        # reconnexion : session remplacée
        assert pump(a, lambda: a.session is not first and a.session.state == "stopped"
                    and a.current_frame is not None, timeout=30)
        assert first.state == "disconnected"
        a.close_config()
        assert pump(a, lambda: a.session.state == "disconnected", timeout=20)
        # Fermer pendant qu'un Connecter attend la déconnexion : la session ne démarre pas
        a.open_config(path)
        a.connect_config()
        assert pump(a, lambda: a.session.state == "stopped" and a.current_frame is not None, timeout=30)
        second = a.session
        a.connect_config()
        a.close_config()
        pump(a, lambda: False, timeout=3)
        assert a.session is second and second.state == "disconnected"
        assert a.config is None and a.device is None and a.api.config is None
        assert os_kinds(a) == {"rtos", "tasktime", "threads"}
        assert a.root.title() == "gdbtrace — freertos.elf"
        a.save_layout()
        data = json.load(open(tmp_path / "layout.json"))
        assert data["recent_configs"][0] == path and {"rtos", "threads"} <= set(data["known_kinds"])
    finally:
        close_app(a)


def test_configuration_svd_read_outlived(tmp_path, monkeypatch):
    """Un SVD encore en lecture quand la configuration est fermée (ou rouverte) n'est ni
    installé à retardement, ni relu deux fois."""
    from conftest import ROOT
    from gdbtrace.gui import app as appmod
    parse, calls = appmod.svd.parse, []

    def slow(path):
        calls.append(path)
        time.sleep(0.8)
        return parse(path)
    monkeypatch.setattr(appmod.svd, "parse", slow)
    a = appmod.App(layout_file=str(tmp_path / "layout.json"))
    try:
        path = os.path.join(ROOT, "configs", "qemu-demo.yaml")
        assert a.open_config(path)
        a.apply_config(a.config)                      # même SVD, encore en lecture : pas relu
        assert len(calls) == 1
        a.close_config()
        pump(a, lambda: False, timeout=1.5)
        assert a.device is None and a.svd_path is None
        assert a.open_config(path)
        assert pump(a, lambda: a.device is not None, timeout=5) and len(calls) == 2
    finally:
        close_app(a)


def test_configuration_os_views_and_layout(tmp_path):
    """OS « aucun » : ni RTOS, ni Temps des tâches, ni Threads ; le layout enregistré ne les
    oublie pas : relancé sans configuration, GdbTrace les rouvre."""
    from conftest import ROOT
    from gdbtrace.gui.app import App
    layout = str(tmp_path / "layout.json")
    a = App(layout_file=layout)
    try:
        assert a.open_config(os.path.join(ROOT, "configs", "qemu-demo.yaml"))
        assert os_kinds(a) == set()
        assert a.open_config(os.path.join(ROOT, "configs", "native-pthreads.yaml"))
        assert os_kinds(a) == {"threads"}
        a.apply_os_views("aucun")
        a.save_layout()
    finally:
        close_app(a)
    b = App(layout_file=layout)
    try:
        assert os_kinds(b) == {"rtos", "tasktime", "threads"} and b.config is None
        assert len(b.recent_configs) == 2
    finally:
        close_app(b)


def test_configuration_dialog(tmp_path):
    """La boîte de dialogue : champs → configuration ; une erreur reste affichée sans fermer ;
    OK applique ; configuration modifiée : la question d'enregistrement à la fermeture."""
    import yaml

    from gdbtrace.core.config import SessionConfig
    from gdbtrace.gui.app import App
    a = App(layout_file=str(tmp_path / "layout.json"))
    try:
        ed = a._edit(SessionConfig({}), "essai")
        ed.vars[("", "nom")].set("Carte X")
        ed.vars[("cible", "elf")].set(str(tmp_path / "fw.elf"))
        ed.vars[("cible", "cpu-hz")].set("16000000")
        ed.vars[("cible", "programmer")].set(True)
        ed.server_var.set("stlink")
        ed._show_server()
        ed.vars[("serveur", "stlink-ap")].set("1")
        ed.vars[("serveur", "ram-boot")].set(True)
        ed.os_var.set("pthread")                                   # incohérent avec ST-LINK
        assert not ed.ok() and "--os pthread" in ed.error_var.get()
        ed.os_var.set("freertos")
        ed.vars[("trace", "etm-port")].set("etr")                   # sans --etm-config : refusé
        assert not ed.ok() and "--etm-port etr" in ed.error_var.get()
        ed.vars[("trace", "etm-config")].set(True)
        ed.texts[("trace", "var-trace")].insert("1.0", "produced:write\ncounter:rw+pc")
        ed.vars[("trace", "swo-config")].set(True)
        ed.texts[("couverture", "exporter")].insert("1.0", "cov.html")
        assert ed.ok()
        cfg = a.config
        assert cfg.name == "Carte X" and cfg.modified and "[Carte X*]" in a.root.title()
        assert cfg.data["serveur"] == {"type": "stlink", "stlink-ap": 1, "ram-boot": True}
        assert cfg.data["trace"] == {"swo-config": True, "var-trace": ["produced:write", "counter:rw+pc"],
                                     "etm-config": True, "etm-port": "etr"}
        assert cfg.data["cible"] == {"elf": str(tmp_path / "fw.elf"), "cpu-hz": 16_000_000,
                                     "programmer": True}
        assert os_kinds(a) == {"rtos", "tasktime"} and a.cpu_hz == 16_000_000
        # la boîte rouverte sur cette configuration retrouve chaque valeur
        ed2 = a._edit(cfg.copy(), "relue")
        assert ed2.get_config().data == cfg.data
        ed2.cancel()
        # fermeture : annuler garde tout ; non : ferme sans enregistrer ; oui : enregistre
        a.ask_save = lambda title, msg: None
        a.close_config()
        assert a.config is cfg
        cfg.path = str(tmp_path / "x.yaml")
        a.ask_save = lambda title, msg: True
        a.close_config()
        assert a.config is None and (tmp_path / "x.yaml").exists()
        saved = yaml.safe_load(open(tmp_path / "x.yaml"))
        assert saved["cible"]["elf"] == "fw.elf" and saved["couverture"] == {"exporter": ["cov.html"]}
        assert a.recent_configs[0] == str(tmp_path / "x.yaml")
    finally:
        close_app(a)


def test_cortex_m0_trace_views_say_why(m0_elf, tmp_path):
    """Cortex-M0 sans MTB (QEMU microbit) : ni ITM, ni SWO, ni ETM. Chaque vue de trace le
    dit (cœur reconnu à la connexion), au lieu d'attendre une capture qui ne viendra pas."""
    from gdbtrace.gui.app import App
    from gdbtrace.views.coverage import CoverageView
    from gdbtrace.views.itm import ItmView
    from gdbtrace.views.tasktime import TaskTimeView
    from gdbtrace.views.trace import TraceView
    from gdbtrace.views.vartrace import DWT, SAMPLED, VarTraceView
    a = App(layout_file=str(tmp_path / "layout.json"))
    try:
        s = Session()
        s.connect(m0_elf, QemuTarget(elf=m0_elf, machine="microbit", gdb_port=free_port()))
        a.attach(s)
        assert a.trace.core_name == "Cortex-M0"
        why = "Cortex-M0 (ARMv6-M) : ni ITM, ni SWO, ni ETM"
        labels = {}
        for cls, attr in ((ItmView, "status"), (VarTraceView, "info"), (TaskTimeView, "info"),
                          (TraceView, "info"), (CoverageView, "info")):
            v = a.views.show(cls)
            v.refresh()
            labels[cls.kind] = getattr(v, attr)
        assert pump(a, lambda: all(why in w.cget("text") for w in labels.values())), \
            {k: w.cget("text") for k, w in labels.items()}
        vt = a.views.find(VarTraceView)
        assert vt.source_var.get() == SAMPLED            # sans ITM : échantillonné par défaut
        vt.source_var.set(DWT)
        vt.trace_expr("counter", "write", False)         # trace du DWT refusée, en le disant
        assert why in a.status.get() and not a.trace.datatrace.vars
    finally:
        close_app(a)


def test_etm_over_swo_in_gui(app, qemu_paths, tmp_path):
    """Chemin de l'utilisateur sur la F746 : --etm-config --etm-port swo dans le GUI. Le journal
    de la mise en route va dans la console, la capture démarre, la couverture se remplit ; sans
    OpenOCD, Trace › ETM par SWO… dit pourquoi (console, vues Couverture et Trace)."""
    from gdbtrace.__main__ import build_parser, setup_trace
    from gdbtrace.trace.etm.encoder import encode
    from gdbtrace.trace.etm.qemu_log import Step
    from gdbtrace.trace.tpiu import frame_bytes
    from gdbtrace.views.console import ConsoleView
    from gdbtrace.views.coverage import CoverageView
    from gdbtrace.views.trace import TraceView
    con = app.views.find(ConsoleView)
    cv, tv = app.views.show(CoverageView), app.views.show(TraceView)
    assert "aucune capture" in cv.info.cget("text") and "ETM par SWO" in tv.info.cget("text")
    # Trace › ETM par SWO… sous QEMU : aucune sonde ne sert le SWO
    assert app.start_etm_swo(216_000_000) is None
    assert pump(app, lambda: "ne sert pas le SWO" in con.out.get("1.0", "end"))
    assert "la sonde ne sert pas le SWO" in cv.info.cget("text")
    assert "la sonde ne sert pas le SWO" in tv.info.cget("text")
    # --etm-config --etm-port swo, la capture rejouée tenant lieu du port SWO d'OpenOCD
    img, events = qemu_paths["bare"]
    events = events[:20_000]
    n = sum(isinstance(e, Step) for e in events)
    capture = tmp_path / "swo.bin"
    capture.write_bytes(frame_bytes(2, encode(events, img)))
    args = build_parser().parse_args([app.session.elf, "--etm-config", "--etm-port", "swo",
                                      "--cpu-hz", "4000000", "--swo-file", str(capture)])
    app.safe(setup_trace, args, app.trace, app.session, app.session.target,
             lambda line: app.write_console(line + "\n", "gdb"))
    text = con.out.get("1.0", "end")
    assert "TPIU_FFCR = 0x00000102" in text and "capture SWO rejouée" in text
    assert "ETM par SWO (ETMv3)" in text and app.trace.etm_swo
    assert app.trace.status() != "pas de capture SWO"
    assert pump(app, lambda: app.trace.etm.stats["instructions"] >= n), app.trace.trace_status()
    assert pump(app, lambda: "instructions" in cv.info.cget("text") and coverage_drawn(cv))
    assert "aucune capture" not in cv.info.cget("text")
    t, _, _ = app.trace.etm.cov.total()
    assert t.covered > 100 and cv.tree.get_children()
    assert "instructions décodées" in tv.info.cget("text")
    app.trace.stop()


def test_watch_and_locals_display_formats(app):
    """Clic droit › format d'affichage : Watch (hérité par les membres, gardé en marche et dans
    le layout, changé cible en marche) et locales de la vue Pile."""
    from gdbtrace.core.formats import ascii_repr
    from gdbtrace.views.data import StackView, WatchView

    def pick(view, item, label):
        m = view._format_menu(item)
        m.invoke(m.index(label))

    app.session.break_("update_sensor", temporary=True)
    app.go()
    assert pump(app, lambda: app.current_frame is not None and app.current_frame.func == "update_sensor")
    w = app.views.find(WatchView)
    for e in ("offset", "last_char", "sensors", "ticks"):
        w.add(e)
    r = w.roots
    pick(w, r["offset"], "Hexadécimal")
    assert w.tree.set(r["offset"], "value") == "0xfffd"
    pick(w, r["offset"], "Binaire")
    assert w.tree.set(r["offset"], "value") == "0b1111_1111_1111_1101"
    ch = int(w.tree.set(r["last_char"], "value").split()[0])          # naturel : « 65 'A' »
    pick(w, r["last_char"], "ASCII")
    assert w.tree.set(r["last_char"], "value") == ascii_repr(bytes([ch]))
    # structure : le format vaut pour les membres, dépliés ensuite compris
    pick(w, r["sensors"], "Hexadécimal")
    w.tree.focus(r["sensors"])
    w._expand(None)
    s1 = w.tree.get_children(r["sensors"])[1]
    w.tree.focus(s1)
    w._expand(None)
    kids = {w.tree.item(i, "text"): i for i in w.tree.get_children(s1)}
    assert w.tree.set(kids["id"], "value") == "0x2"
    pick(w, kids["id"], "Décimal")                                     # un membre garde le sien
    assert w.tree.set(kids["id"], "value") == "2" and w.format_of(kids["value"]) == "hexadecimal"
    w._format_menu(kids["id"])
    assert w.format_menu.var.get() == "decimal"                        # bouton radio coché
    # cible en marche : suivi en direct au format choisi, et format changé en marche
    pick(w, r["ticks"], "Hexadécimal")
    app.go()
    assert pump(app, lambda: app.session.state == "running")
    t0 = w.tree.set(r["ticks"], "value")
    assert pump(app, lambda: w.tree.set(r["ticks"], "value").startswith("0x")
                and w.tree.set(r["ticks"], "value") != t0)
    pick(w, r["ticks"], "ASCII")
    assert pump(app, lambda: w.tree.set(r["ticks"], "value").startswith("'"))
    app.halt()
    assert pump(app, lambda: app.session.state == "stopped")
    pump(app, lambda: False, timeout=0.3)
    assert w.tree.set(r["ticks"], "value").startswith("'") and w.tree.set(r["offset"], "value").startswith("0b")
    # layout : formats gardés, appliqués à la création des racines
    state = w.get_state()
    assert state["formats"]["offset"] == "binary" and state["formats"]["sensors\t[1]\tid"] == "decimal"
    w2 = app.views.open(WatchView, None, {"exprs": ["offset"], "formats": state["formats"]})
    off = w2.tree.set(w2.roots["offset"], "value")          # offset = -(tick % 7) : négatif ou nul
    assert off == w.tree.set(r["offset"], "value") and off.startswith("0b") and len(off) == 21
    # locales de la vue Pile
    app.session.break_("update_sensor", temporary=True)
    app.go()
    assert pump(app, lambda: app.session.state == "stopped" and app.current_frame is not None
                and app.current_frame.func == "update_sensor")
    sv = app.views.show(StackView)
    sv.refresh()
    tick = next(i for i in sv.locals.get_children() if sv.locals.set(i, "name") == "tick")
    pick(sv, tick, "Hexadécimal")
    assert sv.locals.set(tick, "value").startswith("0x")
    assert sv.get_state() == {"formats": {"update_sensor/tick": "hexadecimal"}}
    sv.refresh()                                                        # arrêt suivant : gardé
    tick = next(i for i in sv.locals.get_children() if sv.locals.set(i, "name") == "tick")
    assert sv.locals.set(tick, "value").startswith("0x")


def test_example_session_script_in_gui(app):
    """examples/scripts/session.py dans le GUI : la vue Watch se remplit (formats compris),
    le script va au bout sans erreur."""
    from conftest import ROOT
    from gdbtrace.views.console import ConsoleView
    from gdbtrace.views.data import WatchView
    con = app.views.find(ConsoleView)
    app.run_script(os.path.join(ROOT, "examples", "scripts", "session.py"))
    assert pump(app, lambda: "== Fin" in con.out.get("1.0", "end") and not app.console.busy,
                timeout=60), con.out.get("1.0", "end")[-3000:]
    text = con.out.get("1.0", "end")
    assert "Traceback" not in text and "Error" not in text, text[-3000:]
    assert "watchpoint-trigger" in text and "fib(n = " in text
    w = app.views.find(WatchView)
    assert {"counter", "sensors", "last_char", "offset", "temperature", "ticks", "*s"} <= set(w.roots)
    assert w.format_of(w.roots["offset"]) == "binary" and w.format_of(w.roots["sensors"]) == "hexadecimal"
    assert pump(app, lambda: w.tree.set(w.roots["offset"], "value").startswith("0b"))
    assert w.tree.set(w.roots["last_char"], "value").startswith("'")


def test_etm_filter_in_gui(etm_app):
    """Trace › Filtre de trace ETM… sur l'ETM simulé : la vue Trace ne montre que la plage,
    avec les trous, et son libellé nomme le filtre ; la couverture se limite à la plage."""
    from gdbtrace.trace.etm.qemu_log import Step
    from gdbtrace.views.coverage import CoverageView
    from gdbtrace.views.trace import TraceView
    app = etm_app
    tv, cv = app.views.show(TraceView), app.views.show(CoverageView)
    assert app.set_etm_filter(["fib"]) is not None
    lo, hi = app.trace.etm_filter.ranges[0]
    run_to(app, "animate")
    assert pump(app, lambda: not app.trace.etm_busy and app.trace.etm.stats["instructions"] > 50), \
        etm_diagnostics(app)
    assert pump(app, lambda: trace_drawn(tv) and coverage_drawn(cv))
    labels = [tv.listing.set(i, "func") for i in tv.listing.get_children()]
    assert "── trace interrompue ──" in labels and "filtre" in cv.info.cget("text")
    assert "filtre : plages fib" in tv.info.cget("text")
    steps = [e for e in app.trace.etm.last(10**6) if isinstance(e, Step)]
    assert all(lo <= e.addr < hi for e in steps)
    files = {cv.tree.item(i, "text") for i in cv.tree.get_children()}
    funcs = {cv.tree.item(f, "text") for i in cv.tree.get_children() for f in cv.tree.get_children(i)}
    assert funcs == {"fib"} and len(files) == 1
    assert app.set_etm_filter() is not None and app.trace.etm_filter is None


def test_vartrace_view(app, tmp_path):
    """Var.TRACE dans le GUI : tracer `offset` et `counter`, une capture SWO rejouée remplit le
    tableau (dernière valeur au format de la variable) et un graphe par variable ; survol."""
    from test_datatrace import data, lts
    from test_itm import SYNC
    from gdbtrace.views.vartrace import DWT, VarTraceView
    v = app.views.show(VarTraceView)
    app.root.geometry("1200x800")
    v.source_var.set(DWT)                   # capture SWO rejouée ci-dessous (QEMU n'en a pas)
    v._source_chosen = True
    v.trace_expr("offset")
    v.trace_expr("counter", access="rw")
    assert sorted(app.trace.datatrace.vars) == [2, 3]
    cap = tmp_path / "swo.bin"
    cap.write_bytes(SYNC + data(3, 0xFFFD, 2) + data(2, 1, 4) + lts(5000) + data(3, 0xFFFB, 2)
                    + lts(5000) + data(2, 2, 4) + data(3, 0xFFFE, 2))
    app.trace.start_file(str(cap)).done.wait(5)
    assert pump(app, lambda: app.trace.datatrace.vars[3].count == 3)
    v.render(force=True)
    rows = {v.tree.item(i, "text"): v.tree.item(i, "values") for i in v.tree.get_children()}
    assert rows["offset"][1:] == ("3", "-2") and rows["counter"][0] == "les deux"
    assert "2 variable(s), 5 échantillon(s)" in v.info.cget("text")
    lines = [i for i in v.canvas.find_all() if v.canvas.type(i) == "line" and v.canvas.itemcget(i, "width") == "2.0"]
    assert len(lines) == 2                                         # une courbe par variable
    ev = type("E", (), {"x": v.canvas.winfo_width() // 2, "y": 60})()
    v._motion(ev)
    tip = [v.canvas.itemcget(i, "text") for i in v.canvas.find_withtag("hover") if v.canvas.type(i) == "text"]
    assert tip and "offset = " in tip[0] and "counter = " in tip[0]
    assert v.get_state() == {"vars": [["offset", "write", False, "dwt", 10], ["counter", "rw", False, "dwt", 10]]}
    app.trace.untrace_variable(app.session)
    v.render(force=True)
    assert not v.tree.get_children() and "aucune variable tracée" in v.info.cget("text")


def test_tasktime_view(rtos_app, tmp_path):
    """Temps des tâches : changements reçus par l'ITM (flux bâti sur les vrais TCB du firmware),
    noms relus dans les TCB, tableau et chronogramme."""
    from test_datatrace import lts
    from test_itm import SYNC
    from test_tasktrace import switch
    from gdbtrace.views.tasktime import TaskTimeView
    app = rtos_app
    s = app.session
    s.break_("gdbtrace_task_switched_in")
    stops = []                  # arrêts reçus : l'état « stopped » d'avant le Go ne compte pas
    s.on_event(lambda kind, data: stops.append(data) if kind == "stopped" else None)
    seq = []
    for _ in range(8):
        n = len(stops)
        app.go()
        assert pump(app, lambda: len(stops) > n and s.state == "stopped")
        assert stops[-1].get("frame", {}).get("func") == "gdbtrace_task_switched_in", stops[-1]
        seq.append(int(s.evaluate("(unsigned long)tcb")))
        pump(app, lambda: False, timeout=0.05)
    for b in s.breakpoints():
        s.delete_break(b.number)
    cap = tmp_path / "itm.bin"
    cap.write_bytes(SYNC + b"".join(switch(t) + lts(2500) for t in seq) + switch(seq[0]))
    v = app.views.show(TaskTimeView)
    app.cpu_hz = 25_000_000
    app.trace.start_file(str(cap)).done.wait(5)
    assert pump(app, lambda: app.trace.tasktrace.switches == len(seq) + 1)
    v.render(force=True)
    rows = {v.tree.item(i, "text"): v.tree.item(i, "values") for i in v.tree.get_children()}
    assert not any(n.startswith("TCB ") for n in rows)                  # noms relus
    assert sum(int(r[0]) for r in rows.values()) == len(seq) + 1
    assert "changements de tâche sur 800.0 µs" in v.info.cget("text")    # 8 × 2500 cycles à 25 MHz
    from gdbtrace.views.tasktime import ROW, TOP
    assert len(v._bars) == len(seq)                       # une entrée de survol par tranche
    images = [i for i in v.canvas.find_all() if v.canvas.type(i) == "image"]
    assert len(images) == 1                               # les barres : une image
    left = v.canvas.coords(images[0])[0]
    for x0, x1, y, sl in v._bars:                         # chaque tranche à sa couleur de tâche
        rgb = v._image.get(int((x0 + x1) / 2 - left), int(y - TOP + ROW / 2))
        assert "#%02x%02x%02x" % tuple(rgb) == v._colors[sl.tcb]
    x0, x1, y, _sl = v._bars[0]
    v._motion(type("E", (), {"x": (x0 + x1) / 2, "y": y + 8})())
    tip = [v.canvas.itemcget(i, "text") for i in v.canvas.find_withtag("hover") if v.canvas.type(i) == "text"]
    assert tip and "100.00 µs" in tip[0]


def test_breakpoint_options_view(app):
    """Vue Breakpoints : condition et arrêt au Nᵉ passage, message à chaque passage (console),
    colonne « Condition / action »."""
    from gdbtrace.views.console import ConsoleView
    from gdbtrace.views.data import BreakpointsView
    s = app.session
    v = app.views.show(BreakpointsView)
    con = app.views.find(ConsoleView)
    stops = []
    s.on_event(lambda kind, data: stops.append(data) if kind == "stopped" else None)
    b = s.break_("animate")
    app.breakpoints_changed()
    v.apply_options(b.number, count=3, condition="tick > 0")
    assert pump(app, lambda: v.tree.set(b.number, "opts") == "si tick > 0 · au 3ᵉ passage (reste 2)")
    n = len(stops)
    app.go()
    assert pump(app, lambda: len(stops) > n and s.state == "stopped")
    c = int(s.evaluate("counter"))
    assert c in (2, 3)                  # 3 ; 2 si QEMU a doublé un passage (test_breakactions.collapse)
    assert pump(app, lambda: v.tree.set(b.number, "hits") == "3" and "(reste" not in v.tree.set(b.number, "opts"))
    m = s.break_("update_sensor")
    app.breakpoints_changed()
    v.apply_options(m.number, log="capteur {tick}")
    assert pump(app, lambda: v.tree.set(m.number, "opts") == "« capteur {tick} » → continue")
    for _ in range(2):                  # un Go de plus si QEMU a doublé le passage sur animate
        n = len(stops)
        app.go()
        assert pump(app, lambda: len(stops) > n and s.state == "stopped")
        c2 = int(s.evaluate("counter"))
        if c2 != c:
            break
    assert c2 == c + 1 and len(stops) == n + 1                  # passage par le message : invisible
    assert pump(app, lambda: f"capteur {c2}\n" in con.out.get("1.0", "end"))
    ranges = con.out.tag_ranges("action")                       # messages en couleur à part
    assert ranges and con.out.get(ranges[-2], ranges[-1]) == f"capteur {c2}\n"
    assert con.out.tag_cget("action", "foreground")


def test_go_till_and_step_change(app):
    """Run › Go.Till puis Step.Change : arrêts et bilan dans la barre d'état."""
    s = app.session
    stops = []
    s.on_event(lambda kind, data: stops.append(data) if kind == "stopped" else None)
    app.go_till("counter >= 7")
    assert pump(app, lambda: len(stops) == 1 and s.state == "stopped")
    assert s.evaluate("counter") == "7" and app.cached_breakpoints == []
    app.step_loop("last_char", change=True)
    assert pump(app, lambda: len(stops) == 2 and s.state == "stopped"), app.status.get()
    assert pump(app, lambda: "Step.Change last_char" in app.status.get()), app.status.get()
    assert "71 'G' → 72 'H'" in app.status.get() and app.current_frame.func == "animate"
    app.step_loop("0", change=False, max_steps=3)
    assert pump(app, lambda: "limite atteinte" in app.status.get()), app.status.get()


def test_reset_and_program_wait_for_programming(app, elf):
    """Pendant la programmation de la flash, Reset et une seconde programmation attendent :
    ils couperaient l'écriture ou fausseraient la vérification."""
    calls = []
    app.session.reset = lambda: calls.append("reset")
    app._exclusive = "programmation de demo.elf"
    app.reset()
    app.program_elf(elf)
    pump(app, lambda: False, timeout=0.2)
    assert calls == [] and "patienter" in app.status.get()
    assert app._exclusive == "programmation de demo.elf"         # pas de seconde programmation
    app._exclusive = None
    app.reset()
    assert pump(app, lambda: calls == ["reset"])


def test_go_after_a_target_reports_its_end(app):
    """Fin signalée par le serveur d'une cible (pas un programme natif) : TERMINÉ, et Go
    explique au lieu de tenter une relance réservée aux programmes natifs."""
    s = app.session
    s._on_mi({"type": "notify", "message": "stopped", "payload": {"reason": "exited-normally"}})
    assert pump(app, lambda: "reprogrammer" in app.status.get()), app.status.get()
    assert s.state == "exited" and app.state_label.cget("text") == "TERMINÉ"
    app.go()
    assert "Programme terminé sur la cible" in app.status.get()


def test_refresh_after_a_resume_is_not_an_error(app):
    """Un script relance la cible juste après un arrêt : le rafraîchissement des vues, encore en
    cours, voit gdb refuser (« Selected thread is running »). Vue périmée, pas une erreur ; une
    erreur de rafraîchissement cible arrêtée reste affichée."""
    from gdbtrace.core.gdb_backend import GdbError
    from gdbtrace.views.console import ConsoleView
    from gdbtrace.views.registers import RegistersView
    con = app.views.find(ConsoleView)
    v = app.views.find(RegistersView)
    s = app.session

    def resumed():
        s.state = "running"                     # la relance est arrivée pendant le rafraîchissement
        raise GdbError("-data-list-register-values x: Selected thread is running.")
    v.refresh = resumed
    app.views.refresh_all()
    pump(app, lambda: False, timeout=0.1)
    assert "Selected thread is running" not in con.out.get("1.0", "end")
    s.state = "stopped"

    def broken():
        raise GdbError("lecture impossible")
    v.refresh = broken
    app.views.refresh_all()
    assert pump(app, lambda: "lecture impossible" in con.out.get("1.0", "end"))
