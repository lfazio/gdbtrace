"""Temps des tâches sans ITM : journal des changements de tâche en RAM (`gdbtrace_task_log`,
examples/freertos/main.c), lu par `TaskLogReader` en marche et à l'arrêt."""
import os
import time

import pytest
from conftest import M0_DIR, needs_qemu

from gdbtrace.trace.tasktrace import TASK_LOG_MAGIC, TaskLogReader, TaskTraceSink

BASE = 0x20000100
SIZE = 8


class Ring:
    """L'anneau du firmware en mémoire factice."""

    def __init__(self):
        self.head = 0
        self.entries = [(0, 0)] * SIZE
        self.magic = TASK_LOG_MAGIC
        self.during_read = None          # écritures du firmware pendant une lecture

    def write(self, ts, tcb):
        self.entries[self.head % SIZE] = (ts & 0xFFFFFFFF, tcb)
        self.head += 1

    def read(self, addr, n):
        if addr == BASE:
            if self.during_read:
                pending, self.during_read = self.during_read, None
                self._snapshot = self._bytes()
                for ts, tcb in pending:
                    self.write(ts, tcb)
                return self._snapshot[:n]
            return self._bytes()[:n]
        data = self._bytes()
        return data[addr - BASE:addr - BASE + n]

    def _bytes(self):
        out = b"".join(v.to_bytes(4, "little") for v in (self.magic, SIZE, self.head))
        return out + b"".join(ts.to_bytes(4, "little") + tcb.to_bytes(4, "little")
                              for ts, tcb in self.entries)


def test_reads_only_new_entries_across_the_ring_edge():
    ring, r = Ring(), TaskLogReader(BASE)
    for i in range(6):
        ring.write(100 * i, 0x2000_0000 + i)
    assert [tcb & 0xFF for _ts, tcb in r.poll(ring.read)] == [0, 1, 2, 3, 4, 5]
    assert r.poll(ring.read) == []
    for i in range(6, 11):                          # franchit la fin de l'anneau
        ring.write(100 * i, 0x2000_0000 + i)
    assert [ts for ts, _tcb in r.poll(ring.read)] == [600, 700, 800, 900, 1000]
    assert r.lost == 0 and r.read_entries == 11


def test_overrun_and_overwrite_during_read_are_lost_not_wrong():
    ring, r = Ring(), TaskLogReader(BASE)
    for i in range(20):                              # 20 écrites, 8 gardées
        ring.write(i, i)
    assert [ts for ts, _ in r.poll(ring.read)] == list(range(12, 20)) and r.lost == 12
    ring.write(20, 20)
    ring.during_read = [(21, 21), (22, 22), (23, 23)]   # le firmware écrit pendant la lecture
    got = [ts for ts, _ in r.poll(ring.read)]
    assert got == [20] and r.lost == 12
    assert [ts for ts, _ in r.poll(ring.read)] == [21, 22, 23]


def test_timestamps_unwrapped_and_restart_detected():
    ring, r = Ring(), TaskLogReader(BASE)
    ring.write(0xFFFF_FF00, 1)
    ring.write(0x0000_0100, 2)                       # l'horodatage 32 bits a fait un tour
    assert [ts for ts, _ in r.poll(ring.read)] == [0xFFFF_FF00, 0x1_0000_0100]
    ring.head = 0                                    # cible redémarrée
    ring.write(50, 3)
    assert r.poll(ring.read) == [(50, 3)] and r.restarted
    ring.magic = 0                                   # ordonnanceur pas encore démarré
    assert r.poll(ring.read) == []


def test_sink_from_the_log():
    sink = TaskTraceSink()
    sink.feed([(0, 0xA), (300, 0xB), (400, 0xA), (1000, 0xB)])
    rows = {t.tcb: t for t in sink.rows()}
    assert rows[0xA].gross == 300 + 600 and rows[0xB].gross == 100
    assert sink.timestamps and sink.switches == 4 and sink.exception_cycles == 0


def run_demo(elf, machine, script, tmp_path):
    import subprocess
    import sys

    from conftest import ROOT
    path = tmp_path / "s.py"
    path.write_text(script)
    args = [sys.executable, "-m", "gdbtrace", elf, "--batch", "--script", str(path)]
    if machine:
        args[4:4] = ["--machine", machine]
    return subprocess.run(args, cwd=ROOT, capture_output=True, text=True, timeout=120)


SCRIPT = """
import time
gdbtrace.go()
time.sleep(0.4)
live = gdbtrace.task_times()        # en marche : canal live (QMP)
time.sleep(0.4)
gdbtrace.halt(); gdbtrace.wait()
rows = gdbtrace.task_times()        # à l'arrêt : la fin du journal, par gdb
names = {t.name for t in rows}
assert {"worker", "consumer", "producer", "IDLE"} <= names, names
assert gdbtrace.trace.task_log_active and gdbtrace.trace.task_log.read_entries > 50
span = gdbtrace.trace.tasktrace.span
assert sum(t.gross for t in rows) <= span + 1 and sum(t.gross for t in rows) > 0.9 * span
print("OK", len(rows), "tâches")
"""


@needs_qemu
@pytest.mark.parametrize("which", ["m0", "m3"])
def test_task_times_without_itm(which, rtos_elf, m0_elf, tmp_path):
    """Démo FreeRTOS sans capture SWO : le temps des tâches vient du journal en RAM, sur un
    Cortex-M0 (QEMU microbit) comme sur un M3 (mps2-an385)."""
    elf, machine = ((os.path.join(M0_DIR, "freertos.elf"), "microbit") if which == "m0"
                    else (rtos_elf, None))
    r = run_demo(elf, machine, SCRIPT, tmp_path)
    assert r.returncode == 0 and "OK" in r.stdout, r.stdout + r.stderr
    assert "journal des tâches en RAM (SysTick)" in r.stdout


@needs_qemu
def test_task_time_view_from_ram(m0_elf, tmp_path):
    """Vue Temps des tâches sur un Cortex-M0 (QEMU microbit, pas d'ITM) : remplie par le
    journal en RAM, lu en marche puis à l'arrêt."""
    if not os.environ.get("DISPLAY"):
        pytest.skip("pas de DISPLAY")
    from test_gui import close_app, pump

    from gdbtrace.core.session import Session
    from gdbtrace.core.target import QemuTarget, free_port
    from gdbtrace.gui.app import App
    from gdbtrace.views.tasktime import TaskTimeView
    elf = os.path.join(M0_DIR, "freertos.elf")
    a = App(layout_file=str(tmp_path / "layout.json"))
    try:
        s = Session()
        s.connect(elf, QemuTarget(elf=elf, machine="microbit", gdb_port=free_port()))
        a.attach(s)
        assert a.trace.task_log_active and a.trace.core_name == "Cortex-M0"
        v = a.views.show(TaskTimeView)
        a.go()
        assert pump(a, lambda: a.trace.tasktrace.switches > 20, timeout=10)   # lu en marche
        a.halt()
        assert pump(a, lambda: s.state == "stopped", timeout=10)
        v.render(force=True)
        names = {v.tree.item(i, "text") for i in v.tree.get_children()}
        assert {"worker", "consumer", "IDLE"} <= names, names
        assert "journal en RAM (SysTick)" in v.info.cget("text")
    finally:
        close_app(a)
