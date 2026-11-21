"""Tests de fumée de la GUI tkinter (nécessite un affichage : xvfb-run pytest)."""
import os
import time

import pytest
from conftest import needs_qemu

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
    app.toggle_breakpoint_at(file=src.current_file.replace("startup.c", "main.c"), line=15)
    assert app.cached_breakpoints and app.cached_breakpoints[0].line == 15
    app.go()
    assert pump(app, lambda: app.current_frame is not None and app.current_frame.func == "fib")
    assert src.current_file.endswith("main.c")
    assert "current" in src.text.tag_names("15.4")
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
