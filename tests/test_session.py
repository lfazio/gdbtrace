import time

from conftest import FIB_LINE, needs_qemu

from gdbtrace.core.scripting import ScriptAPI, hexdump

pytestmark = needs_qemu


def test_connect_stops_at_reset_handler(session):
    assert session.state == "stopped"
    assert session.frame().func == "Reset_Handler"


def test_breakpoint_go_and_locals(session):
    bp = session.break_("fib")
    assert bp.func == "fib" and bp.line == FIB_LINE
    session.go()
    reason = session.wait_stopped(5)
    assert reason["reason"] == "breakpoint-hit"
    assert session.frame().func == "fib"
    assert int(session.evaluate("n")) == 1
    assert session.breakpoints()[0].hits == 1


def test_step_over_and_out(session):
    session.break_("fib")
    session.go()
    session.wait_stopped(5)
    line = session.frame().line
    session.step_over()
    assert session.frame().line > line
    session.step_out()
    assert session.frame().func == "update_sensor"


def test_halt_while_running(session):
    session.go()
    time.sleep(0.3)
    assert session.state == "running"
    session.halt()
    assert session.state == "stopped"
    assert int(session.evaluate("counter")) > 0


def run_to_main(session):
    session.break_("main", temporary=True)
    session.go()
    session.wait_stopped(5)  # .data est copiée en RAM par Reset_Handler


def test_memory_read_write(session):
    run_to_main(session)
    addr = session.address_of("sensors")
    assert addr == 0x20000000
    data = session.read_memory(addr, 12)
    assert int.from_bytes(data[:4], "little") == 1  # sensors[0].id
    session.write_u32(addr, 0xCAFEBABE)
    assert session.read_u32(addr) == 0xCAFEBABE
    assert session.evaluate("sensors[0].id") == str(0xCAFEBABE - (1 << 32))


def test_registers_and_reset(session):
    regs = dict(session.registers())
    for r in ("r0", "sp", "pc", "xpsr"):
        assert r in regs
    session.break_("main")
    session.go()
    session.wait_stopped(5)
    assert session.frame().func == "main"
    session.reset()
    assert session.frame().func == "Reset_Handler"
    assert session.register("sp") == session.read_u32(0)


def test_disassemble_and_watchpoint(session):
    insns = session.disassemble_function("fib")
    assert insns[0]["address"] == 0xAC and insns[0]["func-name"] == "fib"
    session.watch("counter")
    session.go()
    session.wait_stopped(5)
    assert session.stop_reason["reason"] == "watchpoint-trigger"


def test_varobj_children(session):
    v = session.var_create("sensors")
    kids = session.var_children(v["name"])
    assert len(kids) == 4
    fields = session.var_children(kids[0]["name"])
    assert [f["exp"] for f in fields] == ["id", "value", "status"]


def test_script_api(session, capsys):
    gdbtrace = ScriptAPI(session)
    gdbtrace.break_("update_sensor")
    gdbtrace.go()
    gdbtrace.wait()
    assert gdbtrace.eval("tick") == "1"
    gdbtrace.d("sensors", 16)
    gdbtrace.bt()
    out = capsys.readouterr().out
    assert "20000000  01 00 00 00" in out
    assert "update_sensor" in out and "main" in out


def test_hexdump():
    assert hexdump(b"AB\x00", 0x10) == "00000010  41 42 00" + " " * 39 + "  AB."


def test_program_other_elf(session, rtos_elf):
    """Programmer un autre ELF dans la cible : symboles de gdb, écriture, vérification, reset."""
    from gdbtrace.core.symbols import SymbolTable
    session.go()
    import time
    time.sleep(0.2)
    res = session.program(rtos_elf)
    assert res.size > 1000 and res.verified and not res.mismatched
    assert "vérifié" in res.summary()
    assert session.elf == rtos_elf and session.target.elf == rtos_elf
    fr = session.frame()
    assert fr.func == "Reset_Handler" and fr.fullname.endswith("freertos/startup.c")
    assert fr.pc == SymbolTable(rtos_elf).function("Reset_Handler").start
    session.break_("worker_step", temporary=True)       # symbole du nouveau programme
    session.go()
    assert session.wait_stopped(10) and session.frame().func == "worker_step"


def test_download_progress_not_echoed(session):
    """Les enregistrements de progression `+download` de gdb ne sont pas de la sortie console."""
    out = []
    session.on_event(lambda kind, data: out.append(data.get("text", "")) if kind == "output" else None)
    session.program(session.elf, verify=False)
    assert not any("+download" in t for t in out)


def test_monitor_returns_server_output(session):
    """La sortie d'une commande `monitor` arrive par le flux « target » de MI (`@"..."`) :
    elle était diffusée sans être capturée, et monitor() renvoyait "" (avec OpenOCD,
    `monitor tpiu names` vide : « aucun TPIU déclaré », --swo-config inutilisable)."""
    out = []
    session.on_event(lambda kind, data: out.append(data.get("text", "")) if kind == "output" else None)
    version = session.monitor("info version")
    assert version.strip()[:1].isdigit(), version              # « 11.1.3Debian ... »
    assert not any(version.strip() in t for t in out if t)     # capturée, pas diffusée


# ====================================================================== arrêts internes
class FakeGdb:
    def __init__(self):
        self.sent = []

    def command(self, cmd, timeout=None):
        self.sent.append(cmd)
        return {}


def _internal_session(intercept=True):
    from gdbtrace.core.session import Session
    s = Session()
    s.gdb = FakeGdb()
    s.state = "running"
    s._continuing = True                                 # la cible a été lancée par Go
    events, taken = [], []
    s.on_event(lambda kind, data: events.append(kind))

    def interceptor(reason):
        taken.append(reason)
        return intercept
    s.set_stop_interceptor(interceptor)
    return s, events, taken


SIGINT = {"type": "notify", "message": "stopped",
          "payload": {"reason": "signal-received", "signal-name": "SIGINT"}}
RUNNING = {"type": "notify", "message": "running", "payload": {"thread-id": "all"}}


def test_internal_stop_is_invisible():
    """Arrêt non demandé intercepté (MTB en AUTOHALT) : aucun événement, la cible « tourne »
    toujours, wait_stopped() n'est pas réveillé ; la relance est silencieuse."""
    s, events, taken = _internal_session()
    s._on_mi(SIGINT)
    assert taken and s.internal_stop and s.state == "running" and events == []
    assert not s._stopped.is_set()
    s.resume_internal()
    assert s.gdb.sent == ["-exec-continue"] and not s.internal_stop
    s._on_mi(RUNNING)
    assert events == [] and s.state == "running"
    s._on_mi({"type": "notify", "message": "stopped", "payload": {"reason": "breakpoint-hit"}})
    assert events == ["stopped"] and s.state == "stopped"         # un point d'arrêt : visible


def test_requested_stop_is_never_intercepted():
    s, events, taken = _internal_session()
    s._stopped.set()                                    # halt() n'attend pas dans ce test
    s.halt()                                            # Break de l'utilisateur
    assert s.gdb.sent == ["-exec-interrupt"]
    s._on_mi(SIGINT)
    assert taken == [] and events == ["stopped"] and s.state == "stopped"


def test_declined_or_delivered_stop_becomes_visible():
    s, events, _ = _internal_session(intercept=False)   # l'intercepteur refuse : arrêt normal
    s._on_mi(SIGINT)
    assert events == ["stopped"] and s._stopped.is_set()
    s, events, _ = _internal_session()
    s._on_mi(SIGINT)
    s.deliver_stop()                                    # ce n'était pas la MTB
    assert events == ["stopped"] and s.state == "stopped" and s._stopped.is_set()
    assert s.stop_reason["signal-name"] == "SIGINT"


def test_halt_during_internal_stop_delivers_it():
    """Break pendant un arrêt interne : pas d'interruption envoyée, l'arrêt est livré au lieu
    de la relance."""
    import threading
    s, events, _ = _internal_session()
    s._on_mi(SIGINT)
    t = threading.Thread(target=s.halt, kwargs={"wait": 5})
    t.start()
    import time
    time.sleep(0.05)
    assert t.is_alive() and s.gdb.sent == []            # halt() attend la livraison
    s.resume_internal()
    t.join(2)
    assert not t.is_alive() and events == ["stopped"] and s.gdb.sent == []


def test_stop_during_step_is_visible():
    """Un pas à pas interrompu (seuil MTB atteint pendant un `next`) n'est pas intercepté :
    le relancer en Go perdrait le pas."""
    s, events, taken = _internal_session()
    s._continuing = False
    s._on_mi(SIGINT)
    assert taken == [] and events == ["stopped"]


def _until(pred, timeout=2.0):
    import time
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.01)
    return False


def test_interrupt_overtaken_by_a_breakpoint_is_absorbed():
    """halt() doublé par un breakpoint : la cible s'arrête sur lui, mais OpenOCD applique
    l'interruption après la reprise suivante (constaté sur Nucleo-F072RB). Ce SIGINT que
    personne n'a demandé est absorbé : relance silencieuse, une seule fois."""
    s, events, _ = _internal_session()
    s.set_stop_interceptor(None)
    s._stopped.set()
    s.halt()
    s._on_mi({"type": "notify", "message": "stopped", "payload": {"reason": "breakpoint-hit"}})
    assert events == ["stopped"] and s.state == "stopped"
    s.state, s._continuing = "running", True                    # Go suivant
    s.gdb.sent.clear()
    s._on_mi(SIGINT)
    assert _until(lambda: s.gdb.sent == ["-exec-continue"]) and events == ["stopped"]
    s._on_mi(RUNNING)
    s._on_mi(SIGINT)                                            # un autre SIGINT : visible
    assert events == ["stopped", "stopped"]


class WatchGdb(FakeGdb):
    def command(self, cmd, timeout=None):
        self.sent.append(cmd)
        if cmd == "-break-list":
            return {"BreakpointTable": {"body": [{"number": "3", "type": "hw watchpoint",
                                                  "enabled": "y", "what": "n >= 2",
                                                  "cond": "n >= 2"}]}}
        return {}


def test_unattributed_watchpoint_trap():
    """OpenOCD 0.12 sur un Cortex-M0 : un watchpoint arrête la cible en SIGTRAP, sans adresse.
    Session refait le travail de gdb : DFSR.DWTTRAP, valeur de l'expression, condition."""
    from gdbtrace.core.session import DFSR, DFSR_DWTTRAP
    s, events, _ = _internal_session()
    s.set_stop_interceptor(None)
    s.gdb = WatchGdb()
    values, dfsr, writes = iter(["0", "0", "1", "1"]), [DFSR_DWTTRAP], []
    s.evaluate = lambda expr: next(values)
    s.read_u32 = lambda addr: dfsr[0] if addr == DFSR else 0
    s.write_u32 = lambda addr, v: writes.append((addr, v))
    s._arm_watch_check()                                        # avant le Go : n >= 2 vaut 0
    trap = {"type": "notify", "message": "stopped",
            "payload": {"reason": "signal-received", "signal-name": "SIGTRAP",
                        "frame": {"func": "producteur"}}}
    sent = len(s.gdb.sent)
    s._on_mi(trap)                                              # écriture sans changement
    assert _until(lambda: s.gdb.sent[sent:] == ["-exec-continue"]) and events == []
    s._on_mi(RUNNING)
    s._on_mi(trap)                                              # n >= 2 devient vrai
    assert _until(lambda: events == ["stopped"])
    r = s.stop_reason
    assert r["reason"] == "watchpoint-trigger" and r["wpt"]["number"] == "3"
    assert r["value"] == {"old": "0", "new": "1"} and r["frame"]["func"] == "producteur"
    assert (DFSR, DFSR_DWTTRAP) in writes
    # un piège qui ne vient pas du DWT (instruction BKPT) reste un signal
    s.state = "running"
    dfsr[0] = 0x2
    s._on_mi(trap)
    assert _until(lambda: events == ["stopped", "stopped"])
    assert s.stop_reason["reason"] == "signal-received"
