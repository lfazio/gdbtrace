"""TrustZone (ARMv8-M) sous QEMU mps2-an505 (Cortex-M33), démo examples/tz-an505 : état de
sécurité, pile à travers les deux mondes, SAU, MPU non sécurisée, SecureFault, vues, MCP."""
import os
import shutil
import subprocess
import time

import pytest
from conftest import ROOT, needs_qemu

from gdbtrace.core import fault, security
from gdbtrace.core.live import QemuQmp
from gdbtrace.core.scripting import ScriptAPI
from gdbtrace.core.session import Session
from gdbtrace.core.target import QemuTarget, free_port

TZ = os.path.join(ROOT, "examples", "tz-an505")
SECURE = os.path.join(TZ, "secure.elf")
NONSECURE = os.path.join(TZ, "nonsecure.elf")


def tz_line(text: str) -> int:
    with open(os.path.join(TZ, "secure.c"), encoding="utf-8") as fh:
        return next(i for i, line in enumerate(fh, 1) if text in line)


@pytest.fixture(scope="module")
def tz_elfs():
    if not shutil.which("arm-none-eabi-gcc"):
        pytest.skip("arm-none-eabi-gcc absent")
    subprocess.run(["make", "-s", "-C", TZ], check=True)
    return SECURE, NONSECURE


def tz_session(tz_elfs):
    secure, nonsecure = tz_elfs
    target = QemuTarget(elf=secure, machine="mps2-an505", gdb_port=free_port(), loads=[nonsecure])
    s = Session()
    s.connect(secure, target)
    s.add_symbols(nonsecure)
    return s, target


def stop_at(s: Session, location: str) -> None:
    for b in s.breakpoints():
        s.delete_break(b.number)
    s.break_(location)
    s.go()
    s.wait_stopped(10)


@pytest.fixture
def tz(tz_elfs):
    s, target = tz_session(tz_elfs)
    live = target.live_memory()
    yield s, live
    live.close()
    s.disconnect()


def test_qemu_command_line_loads_other_images():
    t = QemuTarget(elf="/a/s.elf", machine="mps2-an505", loads=["/a/ns.elf", "/a/boot.elf"])
    cmd = t.command_line()
    assert cmd[cmd.index("-kernel") + 1] == "/a/s.elf"
    assert [cmd[i + 1] for i, a in enumerate(cmd) if a == "-device"][-2:] == \
        ["loader,file=/a/ns.elf", "loader,file=/a/boot.elf"]


def test_sau_decoding_and_attribution():
    regs = {security.SAU_TYPE: 3, security.SAU_CTRL: 1, security.SAU_RNR: 0}
    table = {0: (0x00200000, 0x003FFFE1), 1: (0x28100000, 0x281FFFE1), 2: (0x10100000, 0x101003E3)}

    def read(addr):
        if addr in (security.SAU_RBAR, security.SAU_RLAR):
            return table[regs[security.SAU_RNR]][addr == security.SAU_RLAR]
        return regs[addr]

    def write(addr, value):
        regs[addr] = value

    regs[security.SAU_RNR] = 2
    st = security.read_sau(read, write)
    assert [(r.number, r.base, r.limit, r.attribute) for r in st.regions] == [
        (0, 0x00200000, 0x003FFFFF, "NS"), (1, 0x28100000, 0x281FFFFF, "NS"),
        (2, 0x10100000, 0x101003FF, "NSC")]
    assert regs[security.SAU_RNR] == 2                          # sélection du firmware remise
    assert (st.attribution(0x00200100), st.attribution(0x10100004), st.attribution(0x10000000)) == \
        ("NS", "NSC", "S")
    assert security.read_sau(read, write, secure=False).summary().startswith("SAU : SAU illisible")
    regs[security.SAU_CTRL] = 2
    assert security.read_sau(read, write).attribution(0x10000000) == "NS"   # ALLNS
    assert security.is_fnc_return(0xFEFFFFFF) and not security.is_fnc_return(0xFFFFFFF9)


@needs_qemu
def test_state_and_stack_from_the_nonsecure_world(tz):
    """Arrêt dans le monde non sécurisé : NS d'après la pile ; pile non sécurisée, puis le
    pseudo-cadre FNC_RETURN et l'appelant sécurisé lu dans la pile sécurisée (gdb n'y lit pas
    depuis l'état non sécurisé sous QEMU : le moniteur la lit dans l'espace physique)."""
    s, live = tz
    stop_at(s, "ns_step")
    st = security.security_state(s)
    assert st.secure is False and st.label == "NS" and st.sp_name == "msp_ns"
    assert st.how == "pile : sp = msp_ns" and "msp_s" in st.banked
    before = s.registers()
    frames = security.world_backtrace(s, st, secure_read=live.read_physical)
    assert s.registers() == before                               # registres remis
    names = [(w.world, w.frame.func) for w in frames]
    assert names[:3] == [("NS", "ns_step"), ("NS", "ns_main"), ("NS", "NS_Reset_Handler")]
    assert security.is_fnc_return(frames[3].frame.pc) and "FNC_RETURN" in frames[3].note
    assert names[4] == ("S", "__gnu_cmse_nonsecure_call") and "lue hors de gdb" in frames[4].note
    without = security.world_backtrace(s, st)                   # sans lecture physique : dit
    assert len(without) == 4 and "pile sécurisée illisible" in without[-1].note
    assert not security.session_sau(s, st).readable


@needs_qemu
def test_state_and_stack_from_the_secure_world(tz):
    """Arrêt dans la fonction d'entrée sécurisée appelée par le non sécurisé : la pile de gdb
    s'arrête au premier cadre non sécurisé ; elle se poursuit jusqu'au main sécurisé."""
    s, _live = tz
    stop_at(s, f"secure.c:{tz_line('secure_calls++')}")
    st = security.security_state(s)
    assert st.secure is True and st.label == "S" and st.sp_name == "msp_s"
    assert [f.func for f in s.backtrace()] == ["secure_add", "ns_step"]   # gdb seul
    before = s.registers()
    frames = security.world_backtrace(s, st)
    assert s.registers() == before
    worlds = [(w.world, w.frame.func) for w in frames if not security.is_fnc_return(w.frame.pc)]
    assert worlds == [("S", "secure_add"), ("NS", "ns_step"), ("NS", "ns_main"),
                      ("NS", "NS_Reset_Handler"), ("S", "__gnu_cmse_nonsecure_call"),
                      ("S", "start_nonsecure"), ("S", "main")]
    assert "SG" in frames[1].note
    sau = security.session_sau(s, st)
    assert sau.readable and sau.enabled and sau.sregion == 8
    got = [(r.number, r.base, r.attribute) for r in sau.regions if r.enabled]
    if len(sau.regions) < sau.sregion:          # QEMU 8.2 : SAU_RNR ignoré, région choisie seule
        assert got == [(2, 0x10100000, "NSC")] and "sélection de région ignorée" in sau.note
    else:                                       # QEMU 11 : SAU_RNR écrit, les 8 régions lues
        assert got == [(0, 0x00200000, "NS"), (1, 0x28100000, "NS"), (2, 0x10100000, "NSC")]
        assert "ignorée" not in sau.note
    api = ScriptAPI(s)
    assert api.mpu(quiet=True).world == "sécurisée"
    assert api.mpu(quiet=True, nonsecure=True).world == "non sécurisée"
    s.go()                                                      # la cible repart normalement
    s.wait_stopped(10)
    assert int(s.evaluate("secure_calls")) == 1


@needs_qemu
def test_securefault_invep(tz):
    """Appel non sécurisé d'une adresse sécurisée qui n'est pas une entrée : SecureFault INVEP,
    cadre empilé sur la pile non sécurisée (EXC_RETURN.S = 0), origine de l'appel."""
    s, live = tz
    stop_at(s, "ns_main")
    s.set_var("ns_fault_kind", "1")
    stop_at(s, "SecureFault_Handler")
    rep = fault.analyze(s)
    assert rep.exception == "SecureFault" and "INVEP" in [n for n, _ in rep.causes]
    f = rep.frame
    assert f.world == "NS" and f.stack == "MSP_NS" and f.base == s.register("msp_ns")
    assert f.regs["pc"] == 0x10000000 and rep.instruction is None
    assert any("ns_main" in n for n in rep.notes) and "sécurisé, Handler" in rep.security
    frames = security.world_backtrace(s, secure_read=live.read_physical)
    assert [(w.world, w.frame.func) for w in frames][:4:3] == [("S", "SecureFault_Handler"),
                                                               ("NS", "ns_main")]


@needs_qemu
def test_mpu_offset_reads_the_nonsecure_alias():
    from gdbtrace.core.mpu import read_mpu
    seen = []

    def read(addr):
        seen.append(addr)
        return 0
    read_mpu(read, None, "ARMv8-M Mainline", offset=security.SCS_NS_OFFSET)
    assert seen[0] == 0xE002ED90


@needs_qemu
def test_batch_script_and_mcp_tool(tz_elfs, tmp_path):
    """--qemu-load et --add-symbols en batch ; gdbtrace.security(), sau(), bt() ; outil MCP."""
    import sys
    script = tmp_path / "tz.py"
    script.write_text(
        "gdbtrace.break_('secure.c:%d')\n"
        "gdbtrace.go(); gdbtrace.wait(10)\n"
        "st = gdbtrace.security()\n"
        "gdbtrace.sau()\n"
        "gdbtrace.bt()\n"
        "assert st.label == 'S'\n" % tz_line("secure_calls++"))
    out = subprocess.run(
        [sys.executable, "-m", "gdbtrace", SECURE, "--machine", "mps2-an505", "--qemu-load", NONSECURE,
         "--add-symbols", NONSECURE, "--batch", "--script", str(script)],
        cwd=ROOT, capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stdout + out.stderr
    assert "état : sécurisé, Thread, pile MSP_S" in out.stdout
    assert "#1 [NS] 0020005a ns_step" in out.stdout and "[S] " in out.stdout
    assert "2 oui 0x10100000-0x101003ff" in out.stdout

    from test_mcp import Client
    c = Client()
    try:
        c("connect", args=f"{SECURE} --machine mps2-an505 --qemu-load {NONSECURE} "
                                  f"--add-symbols {NONSECURE}")
        c("break", location=f"secure.c:{tz_line('secure_calls++')}")
        c("go")
        text = c("security")
        assert "état : sécurisé" in text and "SAU 8 régions" in text and "[NS]" in text
        assert "MPU non sécurisée" in c("mpu", nonsecure=True)
    finally:
        c.close()


@needs_qemu
def test_gui_registers_status_stack_and_system(tz_elfs, tmp_path):
    if not os.environ.get("DISPLAY"):
        pytest.skip("pas de DISPLAY")
    from test_gui import close_app, pump

    from gdbtrace.gui.app import App
    from gdbtrace.views.data import StackView
    from gdbtrace.views.registers import RegistersView
    from gdbtrace.views.system import SystemView
    app = App(layout_file=str(tmp_path / "layout.json"))
    try:
        s, _target = tz_session(tz_elfs)
        app.attach(s)
        s.break_(f"secure.c:{tz_line('secure_calls++')}")
        app.go()
        assert pump(app, lambda: s.state == "stopped" and app.current_frame is not None
                    and app.current_frame.func == "secure_add")
        assert app.security is not None and app.security.label == "S"
        assert app.secure_label.cget("text") == "S" and app.secure_label.winfo_ismapped()
        rv = app.views.show(RegistersView)
        rv.refresh()
        tree = rv.tree
        assert tree.set("#state", "hex") == "S"
        assert set(tree.get_children("#Sécurisé")) >= {"msp_s", "psp_s", "control_s"}
        assert "msp_ns" in tree.get_children("#Non sécurisé")
        assert "privilégié, MSP" in tree.set("control_s", "info")
        sv = app.views.show(StackView)
        sv.refresh()
        rows = [sv.frames.item(i, "values") for i in sv.frames.get_children()]
        assert [r[4] for r in rows][:2] == ["S", "NS"] and rows[1][2] == "ns_step"
        assert "world" in sv.frames.cget("displaycolumns")
        sys_view = app.views.show(SystemView)
        for i in range(sys_view.nb.index("end")):
            if sys_view.nb.tab(i, "text") == "SAU":
                sys_view.nb.select(i)
        app.root.update()
        sys_view.refresh()
        assert sys_view.sau_info.cget("text").startswith("SAU 8 régions, activé")
        assert len(sys_view.sau_tree.get_children()) in (1, 8)  # QEMU 8.2 : RNR ignoré ; 11 : toutes
        n = s.stop_count
        app.go()                                            # repart, s'arrête au tour suivant
        assert pump(app, lambda: s.stop_count > n and s.state == "stopped")
    finally:
        close_app(app)
