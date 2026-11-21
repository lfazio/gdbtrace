"""Awareness FreeRTOS : mémoire synthétique (robustesse) puis firmware réel sous QEMU."""
import time

import pytest
from conftest import needs_qemu

from gdbtrace.core.live import LivePoller
from gdbtrace.core.rtos import freertos as fr
from gdbtrace.core.symbols import SymbolTable


# ====================================================================== mémoire synthétique
class Mem:
    def __init__(self, size=0x3000):
        self.b = bytearray(size)

    def w(self, addr, v):
        self.b[addr:addr + 4] = (v & 0xFFFFFFFF).to_bytes(4, "little")

    def read(self, addr, n):
        if addr + n > len(self.b) or addr < 0:
            raise IOError(f"hors mémoire {addr:#x}")
        return bytes(self.b[addr:addr + n])


def tiny_layout():
    # ListItem_t : value 0, next 4, prev 8, owner 12, container 16 ; List_t : count 0, index 4,
    # xListEnd 8 (value 8, next 12, prev 16) ; TCB : top 0, state 4, event 24, prio 44,
    # stack 48, name 52 (8 car.), end 60
    return fr.FreeRtosLayout(
        ptr=4, tcb_size=64, tcb_top=0, tcb_state=4, tcb_event=24, tcb_prio=44, tcb_stack=48,
        tcb_name=52, name_len=8, tcb_end=60, li_next=4, li_owner=12, li_container=16, li_size=20,
        list_size=20, list_count=0, list_end=8, list_end_next=12,
        current_tcb=0x1100, ready=0x1000, n_prio=1, delayed1=0x1014, delayed2=0x1028, pending=0x103C)


A, B = 0x2000, 0x2100


def build(mem, L, order=(A, B)):
    for lst in (L.ready, L.delayed1, L.delayed2, L.pending):     # listes vides
        mem.w(lst, 0)
        mem.w(lst + L.list_end_next, lst + L.list_end)
    mem.w(L.ready, len(order))
    mem.w(L.ready + L.list_end_next, order[0] + L.tcb_state)
    for i, t in enumerate(order):
        nxt = order[i + 1] + L.tcb_state if i + 1 < len(order) else L.ready + L.list_end
        mem.w(t + L.tcb_state + L.li_next, nxt)
        mem.w(t + L.tcb_state + L.li_owner, t)
        mem.w(t + L.tcb_state + L.li_container, L.ready)
        mem.b[t + L.tcb_name:t + L.tcb_name + 8] = (b"T%x" % t).ljust(8, b"\0")
        base = t + 0x400
        mem.w(t + L.tcb_stack, base)
        mem.w(t + L.tcb_end, base + 0xFC)                   # pile de 256 octets
        mem.w(t + L.tcb_top, base + 0x80)
        mem.b[base:base + 100] = b"\xa5" * 100              # 100 octets jamais utilisés
    mem.w(L.current_tcb, A)
    return mem


def test_snapshot_synthetic():
    L = tiny_layout()
    snap = fr.snapshot(L, build(Mem(), L).read)
    assert snap.incoherent is None
    a, b = snap.tasks if snap.tasks[0].tcb == A else snap.tasks[::-1]
    assert a.state == "Running" and a.current and b.state == "Ready"
    assert a.stack_size == 256 and a.stack_free_min == 100 and a.stack_used == 128
    assert a.stack_percent == pytest.approx(100 * 156 / 256)


def test_cycle_and_bad_owner_are_incoherent():
    L = tiny_layout()
    mem = build(Mem(), L)
    mem.w(B + L.tcb_state + L.li_next, A + L.tcb_state)      # cycle B -> A
    msg = fr.snapshot(L, mem.read).incoherent
    assert msg and "chaînage" in msg
    mem = build(Mem(), L)
    mem.w(B + L.tcb_state + L.li_owner, 0x2F00)              # pvOwner faux
    assert "TCB" in fr.snapshot(L, mem.read).incoherent
    mem = build(Mem(), L)
    mem.w(L.ready, 100000)                                    # compteur aberrant
    assert fr.snapshot(L, mem.read).incoherent
    mem = build(Mem(), L)
    mem.w(L.ready + L.list_end_next, 0x2FFE)                  # pointeur non aligné
    assert fr.snapshot(L, mem.read).incoherent


def test_retry_recovers_from_transient_incoherence():
    L = tiny_layout()
    mem = build(Mem(), L)
    calls = {"n": 0}

    def flaky(addr, n):                       # 1re lecture des listes du noyau : compteur faux
        data = mem.read(addr, n)
        if addr == L.ready:
            calls["n"] += 1
            if calls["n"] == 1:
                return b"\x63" + data[1:]
        return data
    assert fr.snapshot(L, flaky, attempts=1).incoherent
    calls["n"] = 0
    assert fr.snapshot(L, flaky, attempts=3).incoherent is None


def test_cm3_saved_context():
    L = tiny_layout()
    mem = Mem()
    top = 0x2800
    for i in range(8):                       # r4..r11
        mem.w(top + 4 * i, 4 + i)
    hw = [0, 1, 2, 3, 12, 0x111, 0x1234, 0x01000000]
    for i, v in enumerate(hw):
        mem.w(top + 32 + 4 * i, v)
    task = fr.Task(0, "t", "Ready", 1, 1, 1, 0, None, top, None, None, None, None)
    regs = fr.task_registers(L, task, mem.read)
    assert [regs[f"r{i}"] for i in range(4)] == [0, 1, 2, 3] and regs["r12"] == 12
    assert regs["r4"] == 4 and regs["r11"] == 11 and regs["pc"] == 0x1234 and regs["lr"] == 0x111
    assert regs["sp"] == top + 64
    mem.w(top + 32 + 28, 0x01000200)         # bit 9 : 4 octets d'alignement ajoutés
    assert fr.task_registers(L, task, mem.read)["sp"] == top + 68


# ====================================================================== firmware réel (QEMU)
EXPECTED = {  # nom : priorité
    "producer": 2, "consumer": 3, "worker": 2, "sleeper": 1, "hog": 1, "IDLE": 0, "Tmr Svc": 4}
HWM_IDS = ["producer", "consumer", "worker", "sleeper", "hog", "IDLE"]


@pytest.fixture
def rtos(rtos_session, rtos_elf):
    sym = SymbolTable(rtos_elf)
    assert fr.detect(sym)
    return rtos_session, fr.FreeRtosLayout.from_session(rtos_session, sym)


@needs_qemu
def test_tasks_states_and_priorities(rtos):
    s, L = rtos
    snap = fr.snapshot(L, s.read_memory, current_sp=s.register("sp"))
    assert snap.incoherent is None and snap.scheduler_running
    assert {t.name: t.priority for t in snap.tasks} == EXPECTED
    st = {t.name: t.state for t in snap.tasks}
    assert st["worker"] == "Running" and snap.current.name == "worker"
    assert st["sleeper"] == "Suspended"
    assert st["consumer"] == "Blocked" and snap.task("consumer").waiting_on == "cmd_queue (réception)"
    assert st["hog"] == "Blocked" and snap.task("hog").waiting_on == "done_sem"
    assert st["IDLE"] == "Ready"
    assert snap.tick and snap.tick > 100
    # tâche courante : utilisation calculée avec le vrai SP
    w = snap.task("worker")
    assert w.stack_used == w.stack_end + 4 - s.register("sp")


@needs_qemu
def test_stack_high_water_marks_match_freertos(rtos):
    s, L = rtos
    snap = fr.snapshot(L, s.read_memory)
    ref = [int(x) for x in s.evaluate("stack_hwm").strip("{}").split(",")]
    for name in ("producer", "consumer", "worker", "hog", "IDLE"):
        # même algorithme que uxTaskGetStackHighWaterMark (en mots)
        assert snap.task(name).stack_free_min // 4 == ref[HWM_IDS.index(name)], name
    # sleeper a enregistré sa valeur avant vTaskSuspend(), qui descend plus bas dans la pile
    assert snap.task("sleeper").stack_free_min // 4 <= ref[HWM_IDS.index("sleeper")]
    hog = snap.task("hog")
    assert hog.stack_percent > 80                        # pile volontairement très utilisée


@needs_qemu
def test_kernel_objects(rtos):
    s, L = rtos
    snap = fr.snapshot(L, s.read_memory)
    objs = {o.name: o for o in snap.objects}
    assert set(objs) == {"cmd_queue", "data_mutex", "done_sem", "TmrQ"}
    assert objs["cmd_queue"].kind == "queue" and objs["cmd_queue"].length == 4
    assert objs["cmd_queue"].item_size == 8 and objs["cmd_queue"].waiting_receive == ["consumer"]
    assert objs["data_mutex"].kind == "mutex" and objs["data_mutex"].holder == "worker"
    assert objs["done_sem"].kind == "sémaphore binaire" and objs["done_sem"].waiting_receive == ["hog"]


@needs_qemu
def test_task_context_and_backtrace_restore_registers(rtos):
    s, L = rtos
    snap = fr.snapshot(L, s.read_memory)
    before = {r: s.register(r) for r in fr.CORE_REGS}
    regs = fr.task_registers(L, snap.task("consumer"), s.read_memory)
    frames = fr.task_backtrace(s, regs)
    funcs = [f.func for f in frames]
    assert funcs[0] == "xQueueReceive" and "consumer_task" in funcs
    assert funcs[-1] == "consumer_task"                  # artefact de pile initiale retiré
    assert {r: s.register(r) for r in fr.CORE_REGS} == before
    assert s.frame().func == "worker_step"


@needs_qemu
def test_script_api(rtos_session, capsys):
    from gdbtrace.core.scripting import ScriptAPI
    gdbtrace = ScriptAPI(rtos_session, rtos_session.target.live_memory())
    try:
        names = [t.name for t in gdbtrace.tasks()]
        assert set(names) == set(EXPECTED)
        out = capsys.readouterr().out
        assert "*worker" in out and "Suspended" in out
        assert {o.name for o in gdbtrace.queues()} >= {"cmd_queue", "data_mutex"}
        assert "pris par worker" in capsys.readouterr().out
        frames = gdbtrace.task("hog")
        assert [f.func for f in frames][-1] == "hog_task"
        # en marche : même API, via le canal live
        rtos_session.go()
        time.sleep(0.1)
        assert {t.name for t in gdbtrace.tasks()} == set(EXPECTED)
    finally:
        gdbtrace.live.close()


@needs_qemu
def test_live_snapshots_while_running(rtos):
    s, L = rtos
    lm = s.target.live_memory()
    got = []
    poller = LivePoller(lm, lambda: s.state == "running", lambda fn, *a: fn(*a), period_ms=50)
    poller.subscribe_job("rtos", lambda read: fr.snapshot(L, read, attempts=4), got.append)
    try:
        poller.start()
        s.go()
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and len([g for g in got if not g.incoherent]) < 4:
            time.sleep(0.05)
        good = [g for g in got if not g.incoherent]
        assert len(good) >= 4
        assert all({t.name for t in g.tasks} == set(EXPECTED) for g in good)
        assert good[-1].tick > good[0].tick
        idle = [g.task("IDLE").runtime for g in good]
        assert idle[-1] > idle[0]                        # compteur de temps d'exécution
        # % CPU entre deux snapshots : IDLE domine. En marche, un snapshot peut rester
        # incohérent après ses essais (listes modifiées pendant la lecture, et le poller lit
        # en même temps) : on recommence ; la vue RTOS, elle, garde le précédent
        deadline = time.monotonic() + 3
        delta = fr.snapshot(L, lm.read, prev=good[-1], attempts=4)
        while delta.incoherent and time.monotonic() < deadline:
            delta = fr.snapshot(L, lm.read, prev=good[-1], attempts=4)
        assert not delta.incoherent, delta.incoherent
        assert delta.task("IDLE").cpu > 50
    finally:
        poller.stop()
        lm.close()
