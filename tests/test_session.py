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
    t32 = ScriptAPI(session)
    t32.break_("update_sensor")
    t32.go()
    t32.wait()
    assert t32.eval("tick") == "1"
    t32.d("sensors", 16)
    t32.bt()
    out = capsys.readouterr().out
    assert "20000000  01 00 00 00" in out
    assert "update_sensor" in out and "main" in out


def test_hexdump():
    assert hexdump(b"AB\x00", 0x10) == "00000010  41 42 00" + " " * 39 + "  AB."
