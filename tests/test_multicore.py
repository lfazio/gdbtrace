"""Plusieurs cœurs : QEMU mps2-an521 (deux Cortex-M33, le second processus 2 du gdbstub) et un
cœur servi par un autre serveur gdb (un second QEMU), en script, en batch, par le MCP et dans
le GUI."""
import os
import shutil
import subprocess
import sys
import time

import pytest
from conftest import ROOT, needs_qemu

from gdbtrace.__main__ import build_parser, validate
from gdbtrace.core.config import SessionConfig
from gdbtrace.core.cores import CoreError, CoreSet, check_cores, parse_core
from gdbtrace.core.scripting import ScriptAPI
from gdbtrace.core.session import Session
from gdbtrace.core.target import QemuTarget, free_port

MC = os.path.join(ROOT, "examples", "multicore-an521")
MC_ELF = os.path.join(MC, "multicore.elf")
DEMO = os.path.join(ROOT, "examples", "firmware", "demo.elf")


def test_core_options():
    assert parse_core("cpu0").main
    sp = parse_core("cpu1=2")
    assert (sp.name, sp.process, sp.remote, sp.elf) == ("cpu1", 2, None, None)
    sp = parse_core("cm4=localhost:3334,fw/cm4.elf")
    assert (sp.remote, sp.elf) == ("localhost:3334", "fw/cm4.elf") and sp.text() == "cm4=localhost:3334,fw/cm4.elf"
    assert parse_core("b=:3334").remote == "127.0.0.1:3334"
    with pytest.raises(CoreError):
        parse_core("x=nulle-part")
    assert check_cores(["a", "b"]) == ["--core : un seul cœur sans source (celui de la session)"]
    assert any("deux fois" in e for e in check_cores(["a", "a=2"]))
    assert any("processus 1" in e for e in check_cores(["b=1"]))
    assert any("introuvable" in e for e in check_cores(["b=2,/nulle/part.elf"]))
    args = build_parser().parse_args([MC_ELF, "--native", "--core", "b=2"])
    assert any("programme de l'hôte" in e for e in validate(args))


def test_config_cores_relative_elf(tmp_path):
    (tmp_path / "cm4.elf").write_bytes(b"\x7fELF")
    path = tmp_path / "s.yaml"
    path.write_text("cible: {elf: fw.elf, coeurs: [cm7, 'cm4=127.0.0.1:3334,cm4.elf', 'c2=2']}\n")
    argv = SessionConfig.load(str(path)).to_argv()
    assert argv[argv.index("--core") + 1] == "cm7"
    assert f"cm4=127.0.0.1:3334,{tmp_path / 'cm4.elf'}" in argv and "c2=2" in argv
    args = build_parser().parse_args(argv)
    again = SessionConfig.from_args(args)
    assert again.data["cible"]["coeurs"][1] == f"cm4=127.0.0.1:3334,{tmp_path / 'cm4.elf'}"
    again.save(str(tmp_path / "t.yaml"))
    assert "cm4=127.0.0.1:3334,cm4.elf" in (tmp_path / "t.yaml").read_text()   # relatif au YAML


def test_qemu_log_of_one_cpu():
    """Journal de QEMU à deux CPU : les lignes de chacun s'entremêlent ; l'ETM simulé ne garde
    que celles du cœur tracé (« Trace 0: »), exceptions de l'autre comprises."""
    from gdbtrace.trace.etm.qemu_log import ExcEntry, ExcReturn, Step, parse
    log = ["Trace 0: 0x1 [0/00000100/0/0] a", "Trace 1: 0x2 [0/00000200/0/0] b",
           "Taking exception 15 [IRQ]", "...taking pending secure exception 15",
           "...loaded new PC 0x00000301", "Trace 1: 0x3 [0/00000300/0/0] h",
           "Trace 0: 0x4 [0/00000102/0/0] a", "Taking exception 11 [SVC]",
           "...taking pending secure exception 11", "...loaded new PC 0x00000401",
           "Trace 0: 0x5 [0/00000400/0/0] svc", "Exception return",
           "Trace 0: 0x6 [0/00000104/0/0] a"]
    assert list(parse(log)) == [Step(0x100), Step(0x102), ExcEntry(11, 0x400), Step(0x400),
                                ExcReturn(), Step(0x104)]
    assert list(parse(log, cpu=1)) == [Step(0x200), ExcEntry(15, 0x300), Step(0x300)]
    assert len(list(parse(log, cpu=None))) > 6                  # tous mêlés


def test_cti_programming_on_fake_memory():
    from gdbtrace.core import cti
    mem = {}
    cti.program(lambda a, v: mem.__setitem__(a, v), 0xE0042000)
    assert mem[0xE0042000 + cti.CTILAR] == cti.UNLOCK and mem[0xE0042000 + cti.CTICONTROL] == 1
    assert mem[0xE0042000 + cti.CTIINEN0] == mem[0xE0042000 + cti.CTIOUTEN0] == mem[0xE0042000 + cti.CTIGATE] == 1

    class Fake:                                  # QEMU : pas de table ROM
        state = "stopped"

        def __init__(self):
            self.hooks = []

        def read_memory(self, addr, n):
            return bytes(n)

        def write_u32(self, addr, v):
            mem[addr] = v

        def add_resume_hook(self, fn):
            self.hooks.append(fn)
    f = Fake()
    res = cti.setup([("a", f), ("b", f)])
    assert res.ctis == {"a": []} and "pas de table ROM" in res.summary() and len(f.hooks) == 1


@pytest.fixture(scope="module")
def mc_elf():
    if not shutil.which("arm-none-eabi-gcc"):
        pytest.skip("arm-none-eabi-gcc absent")
    subprocess.run(["make", "-s", "-C", MC], check=True)
    subprocess.run(["make", "-s", "-C", os.path.dirname(DEMO)], check=True)
    return MC_ELF


@pytest.fixture
def an521(mc_elf):
    s = Session()
    s.connect(mc_elf, QemuTarget(elf=mc_elf, machine="mps2-an521", gdb_port=free_port()))
    yield s
    s.disconnect()


@pytest.fixture
def other_qemu(mc_elf):
    """Un second serveur gdb (QEMU mps2-an385, démo) : le cœur « d'un autre serveur »."""
    t = QemuTarget(elf=DEMO, gdb_port=free_port())
    t.start()
    yield t
    t.stop()


def value(s, expr):
    return int(s.evaluate(expr))


@needs_qemu
def test_an521_two_cores_same_server(an521):
    s = an521
    cs = CoreSet.build(s, [parse_core("cpu0"), parse_core("cpu1=2")], log=lambda _l: None)
    cpu0, cpu1 = cs.cores
    assert (cpu0.thread, cpu1.thread) == ("1", "2") and cpu1.source == "processus 2 du même serveur"
    s.break_("core1_step")
    s.go()                                       # le cœur 0 libère le cœur 1 ; arrêt dans le cœur 1
    s.wait_stopped(10)
    assert cs.follow_stop(s) is cpu1 and s.frame().func == "core1_step"
    assert value(s, "core1_started") == 1
    cs.select("cpu0")
    assert s.backtrace()[0].func in ("core0_step", "core0_main")
    for b in s.breakpoints():
        s.delete_break(b.number)
    s.step()                                     # un pas : le cœur 0 seul (scheduler-locking step)
    assert s.frame().func in ("core0_step", "core0_main") and cs.follow_stop(s) is cpu0
    n0, n1 = value(s, "core0_count"), value(s, "core1_count")
    s.go()
    time.sleep(0.3)
    s.halt()                                     # Break : le cœur choisi le reste
    assert cs.follow_stop(s) is cpu0 and s.current_thread() == "1"
    assert value(s, "core0_count") > n0 and value(s, "core1_count") > n1   # les deux ont tourné
    assert value(s, "core1_seen") > 0                                      # mailbox lue


@needs_qemu
def test_core_of_another_server(an521, other_qemu):
    s = an521
    cs = CoreSet.build(s, [parse_core(f"m3=127.0.0.1:{other_qemu.gdb_port},{DEMO}")],
                       log=lambda _l: None)
    try:
        main, m3 = cs.cores
        assert main.name == "cœur 0" and m3.own_session and m3.session is not s
        api = ScriptAPI(s)
        api.core_set = cs
        api.core("m3")
        assert api.s is m3.session and m3.session.backtrace()[0].func == "Reset_Handler"
        api.break_("fib")
        cs.go_all()                              # les deux sessions repartent
        m3.session.wait_stopped(10)
        assert m3.session.frame().func == "fib" and s.state == "running"
        cs.halt_all()
        assert s.state == "stopped" and m3.session.state == "stopped"
        api.core(0)
        assert api.s is s and [r[0] for r in cs.rows()] == ["cœur 0", "m3"]
        with pytest.raises(CoreError):
            cs.find("inconnu")
    finally:
        cs.close()
    assert m3.session.state == "disconnected"


@needs_qemu
def test_batch_and_mcp_core_tool(mc_elf, tmp_path):
    script = tmp_path / "mc.py"
    script.write_text(
        "gdbtrace.break_('core1_step')\n"
        "gdbtrace.go(); gdbtrace.wait(10)\n"
        "cores = gdbtrace.cores()\n"
        "gdbtrace.core('cpu0')\n"
        "assert [c.name for c in cores] == ['cpu0', 'cpu1']\n"
        "gdbtrace.trace.wait_etm(10)\n"
        "sym = gdbtrace._symbols()\n"
        "funcs = {sym.function_at(e.addr).name for e in gdbtrace.trace_list(200)\n"
        "         if hasattr(e, 'addr') and sym.function_at(e.addr)}\n"
        "print('fonctions tracées', sorted(funcs))\n"
        "assert 'core0_step' in funcs and 'core1_step' not in funcs\n")
    out = subprocess.run([sys.executable, "-m", "gdbtrace", MC_ELF, "--machine", "mps2-an521", "--core", "cpu0",
                          "--core", "cpu1=2", "--etm-sim", "--batch", "--script", str(script)],
                         cwd=ROOT, capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stdout + out.stderr
    assert "* cpu1         arrêté     processus 2 du même serveur" in out.stdout
    assert "cœur cpu0 (session) : arrêté" in out.stdout and "core0_" in out.stdout

    from test_mcp import Client
    c = Client()
    try:
        c("connect", args=f"{MC_ELF} --machine mps2-an521 --core cpu0 --core cpu1=2")
        assert "cpu1" in c("core")
        c("break", location="core1_step")
        c("go")
        assert "* cpu1" in c("core")
        text = c("core", name="cpu0")
        assert "cœur cpu0" in text and "core0_" in text
        assert "cœur inconnu" in c("core", ok=False, name="cpu9")
    finally:
        c.close()


@needs_qemu
def test_gui_core_selector(mc_elf, other_qemu, tmp_path):
    if not os.environ.get("DISPLAY"):
        pytest.skip("pas de DISPLAY")
    from test_gui import close_app, pump

    from gdbtrace.gui.app import App
    from gdbtrace.views.registers import RegistersView
    app = App(layout_file=str(tmp_path / "layout.json"))
    try:
        s = Session()
        s.connect(mc_elf, QemuTarget(elf=mc_elf, machine="mps2-an521", gdb_port=free_port()))
        app.attach(s)
        app.setup_cores([parse_core("cpu0"), parse_core("cpu1=2"),
                         parse_core(f"m3=127.0.0.1:{other_qemu.gdb_port},{DEMO}")], "gdb-multiarch",
                        lambda _l: None)
        assert pump(app, lambda: app.core_set is not None and app.core_box.winfo_ismapped())
        assert [v.split(" ")[0] for v in app.core_box.cget("values")] == ["cpu0", "cpu1", "m3"]
        s.break_("core1_step")
        app.go()
        assert pump(app, lambda: s.state == "stopped" and app.current_frame is not None
                    and app.current_frame.func == "core1_step")
        assert app.core_var.get().startswith("cpu1")            # le cœur arrêté
        app.select_core("m3")
        assert pump(app, lambda: app.current_frame is not None and app.current_frame.func == "Reset_Handler")
        assert app.session is app.core_set.cores[2].session and app.api.s is app.session
        rv = app.views.show(RegistersView)
        rv.refresh()
        assert rv.tree.set("pc", "hex") == f"{app.current_frame.pc:08x}"
        app.select_core("cpu0")
        assert app.session is s
        assert pump(app, lambda: app.current_frame is not None
                    and app.current_frame.func in ("core0_step", "core0_main"))
    finally:
        close_app(app)
