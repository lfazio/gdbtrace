"""Temps par tâche par l'ITM : changements de tâche (port 31, adresse du TCB entrant) et trace
des exceptions → temps brut et net par tâche. Le crochet du firmware FreeRTOS de démo est
vérifié sous QEMU (QEMU n'émule pas l'ITM : flux reconstitués à partir de ses appels réels)."""
from conftest import needs_qemu
from test_datatrace import lts
from test_itm import SYNC, hw, stim

from gdbtrace.trace.itm import ItmDecoder
from gdbtrace.trace.tasktrace import TASK_PORT, TaskTraceSink, freertos_namer

A, B, C = 0x20001000, 0x20002000, 0x20003000


def switch(tcb: int) -> bytes:
    return stim(TASK_PORT, tcb, 4)


def exc(number: int, entry: bool) -> bytes:
    return hw(1, number | (1 if entry else 2) << 12, 2)


def test_slices_gross_net_and_timeline():
    sink = TaskTraceSink()
    stream = (SYNC + lts(10) + switch(A) + lts(100) + exc(15, True) + lts(20) + exc(15, False)
              + lts(80) + switch(B) + lts(50) + switch(A) + lts(30) + switch(C))
    sink(ItmDecoder().feed(stream))
    sink.namer = {A: "producer", B: "consumer"}.get
    rows = {t.name: t for t in sink.rows()}
    a, b = rows["producer"], rows["consumer"]
    assert (a.activations, a.gross, a.isr, a.net, a.max_slice) == (2, 230, 20, 210, 200)
    assert (b.activations, b.gross) == (1, 50) and rows[f"TCB {C:#010x}"].activations == 1
    assert sink.span == 280 and sink.exception_cycles == 20 and sink.switches == 4
    assert [(s.tcb, s.start, s.end) for s in sink.timeline()] == [(A, 10, 210), (B, 210, 260), (A, 260, 290)]
    assert sink.timestamps and list(sink.rows())[0].name == "producer"      # le plus gros d'abord
    sink.reset()
    assert not sink.rows() and sink.switches == 0


@needs_qemu
def test_freertos_hook_and_names(rtos_session, tmp_path):
    """Le crochet de la démo reçoit exactement pxCurrentTCB à chaque changement de tâche ; les
    noms sont relus dans les TCB ; un flux bâti sur ces changements donne les bons temps."""
    from gdbtrace.core.rtos.freertos import FreeRtosLayout, snapshot
    from gdbtrace.core.scripting import ScriptAPI
    from gdbtrace.trace.manager import TraceManager
    s = rtos_session
    layout = FreeRtosLayout.from_session(s)
    names = freertos_namer(layout, s.read_memory)
    s.break_("gdbtrace_task_switched_in")
    seq = []
    for _ in range(12):
        s.go()
        rec = s.wait_stopped(5)
        assert rec.get("frame", {}).get("func") == "gdbtrace_task_switched_in", rec
        tcb = int(s.evaluate("(unsigned long)tcb"))
        assert tcb == int(s.evaluate("(unsigned long)pxCurrentTCB"))
        seq.append(tcb)
    known = {t.name for t in snapshot(layout, s.read_memory).tasks}
    assert {names(t) for t in seq} <= known and len(set(seq)) >= 3
    # flux reconstitué : 1000 cycles par tranche
    stream = bytearray(SYNC)
    for tcb in seq:
        stream += switch(tcb) + lts(1000)
    stream += switch(seq[0])
    cap = tmp_path / "itm.bin"
    cap.write_bytes(bytes(stream))
    tm = TraceManager()
    api = ScriptAPI(s)
    api.trace = tm
    try:
        tm.start_file(str(cap)).done.wait(5)
        rows = api.task_times()
        by = {t.name: t for t in rows}
        for name in {names(t) for t in seq}:
            n = sum(1 for t in seq if names(t) == name)
            assert by[name].gross == 1000 * n, name
        assert sum(t.gross for t in rows) == 1000 * len(seq)
        assert len(api.task_timeline(5)) == 5
    finally:
        tm.close()
