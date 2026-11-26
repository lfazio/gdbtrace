"""Trace.Find / Trace.FindAll et export CSV (trace/etm/listing.py) : sur le chemin réel de la
démo sous QEMU, par l'API, le serveur MCP et la vue Trace."""
import csv

import pytest
from conftest import needs_qemu

from gdbtrace.core.symbols import SymbolTable
from gdbtrace.trace.etm.listing import export_csv, find_rows, listing_rows


@pytest.fixture
def rows(qemu_paths, elf):
    img, events = qemu_paths["bare"]
    return listing_rows(events[:20_000], img, SymbolTable(elf))


@needs_qemu
def test_find_by_address_function_exception_text(rows):
    fib = next(r for r in rows if r.func == "fib")
    at = find_rows(rows, f"{fib.addr:#x}")
    assert at and all(rows[i].addr == fib.addr for i in at)
    assert find_rows(rows, f"{fib.addr:x}") == at                  # sans 0x
    entries = find_rows(rows, "fib")                                # une par entrée dans fib
    assert entries and all(rows[i].func == "fib" and rows[i - 1].func != "fib" for i in entries)
    assert len(entries) < len([r for r in rows if r.func == "fib"])
    exc = find_rows(rows, "exception")
    assert exc and all(rows[i].kind == "exception" for i in exc)
    assert find_rows(rows, "exception SysTick") == exc == find_rows(rows, "exc 15")
    assert find_rows(rows, "SysTick") == exc                       # nom d'exception seul
    assert all("main.c" in rows[i].file for i in find_rows(rows, "main.c"))
    assert find_rows(rows, "") == [] and find_rows(rows, "introuvable_xyz") == []


@needs_qemu
def test_export_csv(rows, tmp_path):
    path = str(tmp_path / "t.csv")
    assert export_csv(rows, path) == len(rows)
    with open(path, encoding="utf-8") as fh:
        data = list(csv.reader(fh, delimiter=";"))
    assert data[0] == ["index", "type", "adresse", "fonction", "instruction", "fichier", "ligne"]
    first = next(d for d in data[1:] if d[1] == "instruction" and d[3] == "fib")
    assert first[2].startswith("0x") and first[5].endswith("main.c") and int(first[6]) > 0


@needs_qemu
def test_trace_find_in_the_view(elf, tmp_path):
    """Vue Trace sous l'ETM simulé : Tout trouver surligne, ▶ et ◀ parcourent."""
    import os
    if not os.environ.get("DISPLAY"):
        pytest.skip("pas de DISPLAY")
    from test_gui import close_app, pump, trace_drawn

    from gdbtrace.core.session import Session
    from gdbtrace.core.target import QemuTarget, free_port
    from gdbtrace.gui.app import App
    from gdbtrace.views.trace import TraceView
    a = App(layout_file=str(tmp_path / "layout.json"))
    try:
        s = Session()
        s.connect(elf, QemuTarget(elf=elf, gdb_port=free_port(), etm_sim=True))
        a.attach(s)
        tv = a.views.show(TraceView)
        s.break_("fib")
        a.go()
        assert pump(a, lambda: a.current_frame is not None and a.current_frame.func == "fib")
        assert pump(a, lambda: a.trace.etm.stats["instructions"] > 100 and not a.trace.etm_busy)
        assert pump(a, lambda: trace_drawn(tv))
        tv.find_var.set("update_sensor")
        hits = tv.find_all()
        assert hits and all("hit" in tv.listing.item(tv._row_iids[i], "tags") for i in hits)
        assert tv.listing.selection() == (tv._row_iids[hits[0]],)
        assert "occurrence" in tv.find_info.cget("text")
        if len(hits) > 1:
            assert tv.find_next(1) == hits[1] and tv.find_next(-1) == hits[0]
        tv.find_var.set("zzz_absent")
        assert tv.find_next(1) is None and tv.find_info.cget("text") == "aucune occurrence"
        api_hits = a.api.trace_find("update_sensor", n=int(tv.count_var.get()))
        assert len(api_hits) == len(hits) and api_hits[0]["fonction"] == "update_sensor"
        assert a.api.trace_export(str(tmp_path / "x.csv")) > 100
    finally:
        close_app(a)
