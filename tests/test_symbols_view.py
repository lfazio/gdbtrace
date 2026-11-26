"""Navigateur de symboles (sYmbol.Browse, vue Symboles) et `gdbtrace.sym_list()`."""
import os

import pytest
from conftest import needs_qemu

from gdbtrace.core.symbols import SymbolTable, symbol_rows


def test_rows_filtered_by_name_and_kind(elf):
    sym = SymbolTable(elf)
    rows = symbol_rows(sym, "fib")
    assert [r["nom"] for r in rows] == ["fib"] and rows[0]["genre"] == "fonction"
    assert rows[0]["fichier"].endswith("main.c") and rows[0]["ligne"] and rows[0]["taille"]
    var = symbol_rows(sym, "SENS", "variables")
    assert [r["nom"] for r in var] == ["sensors"] and var[0]["taille"] == 48
    assert [r["nom"] for r in symbol_rows(sym, "*_handler", "fonctions")] == \
        ["Default_Handler", "HardFault_Handler", "Reset_Handler", "SysTick_Handler"]
    assert not symbol_rows(sym, "fib", "variables")
    assert symbol_rows(None) == []


@needs_qemu
def test_sym_list(session, capsys):
    from gdbtrace.core.scripting import ScriptAPI
    rows = ScriptAPI(session).sym_list("counter")
    out = capsys.readouterr().out
    assert [r["nom"] for r in rows] == ["counter"] and "variable" in out and "counter" in out


@needs_qemu
def test_symbols_view(elf, tmp_path):
    if not os.environ.get("DISPLAY"):
        pytest.skip("pas de DISPLAY")
    from test_gui import close_app, pump

    from gdbtrace.core.session import Session
    from gdbtrace.core.target import QemuTarget, free_port
    from gdbtrace.gui.app import App
    from gdbtrace.views.code import SourceView
    from gdbtrace.views.data import WatchView
    from gdbtrace.views.memory import MemoryView
    from gdbtrace.views.symbols import SymbolsView
    a = App(layout_file=str(tmp_path / "layout.json"))
    try:
        s = Session()
        s.connect(elf, QemuTarget(elf=elf, gdb_port=free_port()))
        a.attach(s)
        assert pump(a, lambda: s.state == "stopped")
        v = a.views.show(SymbolsView, "left")
        v.filter_var.set("update_")
        v.render()
        names = [v.tree.item(i, "text") for i in v.tree.get_children()]
        assert names == ["update_sensor"], names
        v.tree.selection_set(v.tree.get_children()[0])
        v._open()                                            # fonction : sa source
        src = a.views.find(SourceView)
        assert src is not None and src.current_file.endswith("main.c")
        v.filter_var.set("sensors")
        v.kind_var.set("variables")
        v.render()
        v.tree.selection_set(v.tree.get_children()[0])
        v._open()                                            # variable : la mémoire
        mem = a.views.find(MemoryView)
        assert int(mem.addr_var.get(), 16) == s.address_of("sensors")
        v._watch()
        assert "sensors" in a.views.find(WatchView).roots
        assert v.get_state() == {"filter": "sensors", "kind": "variables"}
    finally:
        close_app(a)
