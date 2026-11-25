"""FreeRTOS sur un Cortex-M0 (ARMv6-M), sans carte : la démo FreeRTOS de examples/nucleo-g070
(port ARM_CM0 de V11.3) compilée pour QEMU `-M microbit` (examples/m0plus/freertos.elf).

Valide ce que la Nucleo-G070RB (Cortex-M0+) utilise : le cadre de pile du port ARM_CM0
(EXC_RETURN en tête, puis r4-r11), donc le contexte et la pile d'appels des tâches non
courantes ; et le refus clair de toute trace ITM / SWO / ETM sur ce cœur."""
import os
import time

import pytest
from conftest import M0_DIR, needs_qemu

from gdbtrace.core.rtos import freertos as fr
from gdbtrace.core.session import Session
from gdbtrace.core.symbols import SymbolTable
from gdbtrace.core.target import QemuTarget, free_port

ELF = os.path.join(M0_DIR, "freertos.elf")
EXPECTED = {"producer": 2, "consumer": 3, "worker": 2, "sleeper": 1, "hog": 1, "IDLE": 0, "Tmr Svc": 4}


@pytest.fixture
def m0_rtos(m0_elf):
    """Démo FreeRTOS arrêtée dans worker_step(), ordonnanceur démarré (QEMU microbit, M0)."""
    s = Session()
    s.connect(ELF, QemuTarget(elf=ELF, gdb_port=free_port(), machine="microbit"))
    s.go()
    time.sleep(0.3)
    s.halt()
    s.break_("worker_step", temporary=True)
    s.go()
    s.wait_stopped(10)
    sym = SymbolTable(ELF)
    assert fr.detect(sym)
    yield s, fr.FreeRtosLayout.from_session(s, sym)
    s.disconnect()


@needs_qemu
def test_m0_tasks_use_the_cm0_frame(m0_rtos):
    s, L = m0_rtos
    assert L.port == "CM0"                              # ARM_CM0 V11.3 sur un ARMv6-M
    snap = fr.snapshot(L, s.read_memory, current_sp=s.register("sp"))
    assert snap.incoherent is None and snap.scheduler_running
    assert {t.name: t.priority for t in snap.tasks} == EXPECTED
    assert snap.current.name == "worker" and snap.task("sleeper").state == "Suspended"


@needs_qemu
@pytest.mark.parametrize("name,top", [("consumer", "consumer_task"), ("hog", "hog_task"),
                                      ("producer", "producer_task")])
def test_m0_task_context_and_backtrace(m0_rtos, name, top):
    """Contexte d'une tâche non courante relu sur sa pile : pile d'appels jusqu'à sa fonction,
    registres du cœur restaurés ensuite."""
    s, L = m0_rtos
    snap = fr.snapshot(L, s.read_memory)
    before = {r: s.register(r) for r in fr.CORE_REGS}
    regs = fr.task_registers(L, snap.task(name), s.read_memory)
    task = snap.task(name)
    assert task.stack_base <= regs["sp"] <= task.stack_end + 4
    funcs = [f.func for f in fr.task_backtrace(s, regs)]
    assert funcs[-1] == top, funcs
    assert {r: s.register(r) for r in fr.CORE_REGS} == before
    assert s.frame().func == "worker_step"


@needs_qemu
def test_m0_refuses_itm_and_etm(m0_rtos):
    from gdbtrace.trace.config import SwoConfig
    from gdbtrace.trace.manager import TraceManager
    s, _L = m0_rtos
    tm = TraceManager()
    assert tm.detect_core(s) == 0xC20 and tm.core_name == "Cortex-M0"
    with pytest.raises(RuntimeError, match="ni ITM, ni SWO, ni ETM"):
        tm.configure_target(SwoConfig(cpu_hz=16_000_000), s)
