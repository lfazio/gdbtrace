"""Programmes natifs (hôte) sous gdb, avec leurs pthreads : examples/native/pthreads.c."""
import os
import re
import shutil
import subprocess
import sys
import time

import pytest
from conftest import ROOT

from gdbtrace.core.session import Session
from gdbtrace.core.target import NativeTarget

NATIVE = os.path.join(ROOT, "examples", "native")
PROG = os.path.join(NATIVE, "pthreads")

pytestmark = pytest.mark.skipif(not (shutil.which("cc") and shutil.which("gdb-multiarch")),
                                reason="cc / gdb-multiarch absents")


@pytest.fixture(scope="session")
def native_elf():
    subprocess.run(["make", "-s", "-C", NATIVE], check=True)
    return PROG


@pytest.fixture
def native(native_elf):
    """Programme démarré, arrêté au début de main ; assez de tours pour ne pas finir."""
    s = Session()
    t = NativeTarget(args=["1000000"])
    s.connect(native_elf, t)
    yield s, t
    s.disconnect()


def _stop_in_consumer(s):
    bp = s.break_("consume_step")
    s.go()
    s.wait_stopped(10)
    s.delete_break(bp.number)


def test_starts_at_main_with_its_pid(native):
    s, t = native
    assert s.state == "stopped" and s.frame().func == "main"
    assert t.pid and os.path.exists(f"/proc/{t.pid}") and s.pointer_size == 8
    with open(f"/proc/{t.pid}/cmdline", "rb") as fh:
        assert fh.read().split(b"\0")[1] == b"1000000"           # --args transmis


def test_named_pthreads_and_the_stopping_thread(native):
    s, _t = native
    _stop_in_consumer(s)
    ths = s.thread_list()
    names = {t.name for t in ths}
    assert {"pthreads", "producer", "consumer"} <= names
    cur = next(t for t in ths if t.current)
    assert cur.name == "consumer" and cur.frame.func == "consume_step"
    assert s.stop_reason["thread-id"] == cur.id and all(t.lwp for t in ths)


def test_select_thread_changes_registers_and_stack(native):
    s, _t = native
    _stop_in_consumer(s)
    pc_consumer = s.register("pc")
    prod = next(t for t in s.thread_list() if t.name == "producer")
    s.select_thread(prod.id)
    assert "producer" in [f.func for f in s.backtrace()]
    assert s.register("pc") != pc_consumer
    assert next(t for t in s.thread_list() if t.current).name == "producer"


def test_thread_events_and_live_memory_while_running(native):
    s, t = native
    events = []
    s.on_event(lambda kind, data: events.append(data.get("event")) if kind == "threads" else None)
    lm = t.live_memory()
    addr = s.address_of("consumed")
    s.go()
    time.sleep(0.3)
    a = int.from_bytes(lm.read(addr, 8), "little")
    time.sleep(0.3)
    b = int.from_bytes(lm.read(addr, 8), "little")
    assert b > a > 0                                               # lu dans /proc/<pid>/mem
    assert {t.state for t in s.thread_list()} == {"running"}
    s.halt()
    assert s.state == "stopped" and events.count("thread-created") >= 3


def test_exit_code_go_again_and_restart(native_elf):
    s = Session()
    t = NativeTarget(args=["50"])
    exited, out = [], []
    s.on_event(lambda kind, data: exited.append(data) if kind == "exited" else None)
    s.on_event(lambda kind, data: out.append(data["text"]) if kind == "output" else None)
    s.connect(native_elf, t)
    try:
        s.go()
        s.wait_stopped(20)
        assert s.state == "exited" and s.exit_code == 0 and t.pid is None and len(exited) == 1
        # sa sortie passe par son propre terminal, pas par les tubes MI de gdb
        time.sleep(0.2)
        assert "produced=50 consumed=50 total=1275\n" in "".join(out)
        s.go()                                                     # terminé : relancé
        s.wait_stopped(20)
        assert s.state == "exited" and len(exited) == 2
        s.reset()                                                  # relancé, arrêté à main
        assert s.state == "stopped" and s.frame().func == "main" and t.pid
    finally:
        s.disconnect()


def test_cli_detects_a_native_elf(native_elf, tmp_path):
    script = tmp_path / "s.py"
    script.write_text("g = gdbtrace\ng.break_('consume_step'); g.go(); g.wait()\n"
                      "g.threads()\nprint('pile', [f.func for f in g.thread('producer')])\n")
    r = subprocess.run([sys.executable, "-m", "gdbtrace", native_elf, "--args", "1000000",
                        "--batch", "--script", str(script)],
                       cwd=ROOT, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    rows = {m.group(1): ln for ln in r.stdout.splitlines()
            if (m := re.match(r"[ *]\s*\d+\s+(\S+)", ln))}
    assert rows["consumer"].startswith("*") and "consume_step" in rows["consumer"]
    assert "'producer'" in r.stdout.splitlines()[-1]


@pytest.mark.skipif(not os.environ.get("DISPLAY"), reason="pas de DISPLAY")
def test_threads_view(native_elf, tmp_path):
    pytest.importorskip("tkinter")
    import gc
    from gdbtrace.gui.app import App
    from gdbtrace.views.threads import ThreadsView
    app = App(layout_file=str(tmp_path / "layout.json"))
    errors = []
    app.report_error = errors.append
    s = Session()
    s.connect(native_elf, NativeTarget(args=["1000000"]))
    app.attach(s)

    def pump(cond, timeout=10.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            app.root.update()
            if cond():
                return True
            time.sleep(0.02)
        return False
    try:
        tv = app.views.find(ThreadsView)
        s.break_("consume_step")
        app.go()
        assert pump(lambda: app.current_frame is not None and app.current_frame.func == "consume_step")
        rows = {tv.tree.item(i, "values")[0]: i for i in tv.tree.get_children()}
        assert {"producer", "consumer"} <= set(rows)
        assert "current" in tv.tree.item(rows["consumer"], "tags")
        assert "arrêt dans" in tv.info.cget("text")
        tv.tree.focus(rows["producer"])
        tv._select()                                               # double-clic
        assert pump(lambda: "current" in tv.tree.item(rows["producer"], "tags"))
        assert pump(lambda: app.current_frame is not None and app.current_frame.func != "consume_step")
        assert app.native and app.trace.etm is None and errors == []
    finally:
        app.quit()
        gc.collect()


def test_step_loop_ends_with_the_program(native_elf):
    """Step.Till qui traverse la fin du programme : la boucle s'arrête avec lui, tout de suite,
    l'état reste « exited » et aucun arrêt n'est inventé après la fin."""
    s = Session()
    s.connect(native_elf, NativeTarget(args=["3"]))
    kinds = []
    s.on_event(lambda kind, _d: kinds.append(kind) if kind in ("stopped", "exited") else None)
    try:
        t0 = time.monotonic()
        rec = s.step_till("0", max_steps=100_000)
        assert time.monotonic() - t0 < 30
        assert s.state == "exited" and kinds == ["exited"]
        assert rec["met"] is False and rec["reason"].startswith("exited") and rec["steps"] > 0
        s.go()                                          # programme natif : relancé
        s.wait_stopped(20)
        assert s.state == "exited" and s.exit_code == 0
    finally:
        s.disconnect()


def test_held_console_output_is_delivered_at_exit(native_elf):
    """Breakpoint à action : la sortie console de gdb est retenue pendant la marche ; la fin
    du programme la livre (« [Inferior 1 … exited normally] »), au lieu de la garder."""
    s = Session()
    out = []
    s.on_event(lambda k, d: out.append(d["text"]) if k == "output" and d.get("stream") == "console" else None)
    s.connect(native_elf, NativeTarget(args=["3"]))
    try:
        s.break_("produce_step", log="v={value}")
        s.go()
        s.wait_stopped(20)
        assert s.state == "exited" and s.action_log
        assert any("exited normally" in x for x in out), out
        assert not s._held and not s.internal_stop
    finally:
        s.disconnect()
