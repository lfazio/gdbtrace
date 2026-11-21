"""Profilage statistique : agrégats sur des PC synthétiques, puis échantillonnage réel sous QEMU."""
import time

from conftest import needs_qemu, source_line

from gdbtrace.core.rtos import freertos as fr
from gdbtrace.core.symbols import SymbolTable
from gdbtrace.trace.profile import SLEEP, UNKNOWN, PcSampler, Profile


def test_aggregation_on_real_symbols(elf):
    sym = SymbolTable(elf)
    fib, main = sym.function("fib"), sym.function("main")
    p = Profile(sym)
    for _ in range(6):
        p.add(fib.start + 8, task="A")
    for _ in range(3):
        p.add(main.start | 1, task="B")       # bit Thumb ignoré
    p.add(None)                               # sommeil (paquet SWO)
    p.add(0x0FFFFFF0)                         # hors symboles
    top = p.top_functions()
    assert [(r.name, r.samples) for r in top] == [("fib", 6), ("main", 3), (SLEEP, 1), (UNKNOWN, 1)]
    assert top[0].percent == 100 * 6 / 11
    assert [(r.line, r.samples) for r in p.lines_of("fib")] == [(sym.line_at(fib.start + 8).line, 6)]
    assert [(r.name, r.samples) for r in p.task_rows()] == [("A", 6), ("B", 3)]
    assert "fib" in p.report()
    p.reset()
    assert p.total == 0 and p.top_functions() == []


def run_sampler(session, prof, seconds, task_of=None):
    lm = session.target.live_memory()
    errors = []
    sampler = PcSampler(lm, prof, lambda: session.state == "running", task_of=task_of,
                        on_error=errors.append)
    try:
        sampler.start()
        session.go()
        time.sleep(seconds)
        session.halt()
    finally:
        sampler.stop()
        lm.close()
    assert not errors
    return prof


@needs_qemu
def test_bare_metal_profile(session, elf):
    prof = run_sampler(session, Profile(SymbolTable(elf)), 1.0)
    assert prof.total > 300 and prof.rate > 300
    top = prof.top_functions()
    assert top[0].name == "delay" and top[0].percent > 50        # boucle d'attente active
    hot = prof.lines_of("delay")[0]
    assert hot.line == source_line("while (n--) {}")


@needs_qemu
def test_freertos_profile_per_task(rtos_session, rtos_elf):
    sym = SymbolTable(rtos_elf)
    task_of = fr.CurrentTaskReader(fr.FreeRtosLayout.from_session(rtos_session, sym))
    prof = run_sampler(rtos_session, Profile(sym), 1.0, task_of=task_of)
    # le hook IDLE du firmware de démo appelle uxTaskGetStackHighWaterMark en boucle
    assert prof.top_functions()[0].name == "prvTaskCheckFreeStackSpace"
    tasks = {r.name: r.percent for r in prof.task_rows()}
    assert tasks["IDLE"] > 80
    assert set(tasks) <= {"IDLE", "producer", "consumer", "worker", "hog", "sleeper", "Tmr Svc", "?"}
