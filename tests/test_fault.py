"""Analyse de faute (core/fault.py, vue Faute, `gdbtrace.fault()`, `--break-on-fault`) sur la
démo `examples/faults` : Cortex-M3 (QEMU mps2-an385 : CFSR, HFSR, BFAR décodés) et Cortex-M0
(QEMU microbit : pas de CFSR, causes déduites de l'instruction et du cadre)."""
import os
import subprocess
import sys
import time

import pytest
from conftest import ROOT, needs_qemu

from gdbtrace.core import fault


# ====================================================================== unitaires
def test_core_arch():
    assert fault.core_arch(0x410FC231) == "ARMv7-M"            # Cortex-M3
    assert fault.core_arch(0x411FC272) == "ARMv7-M"            # Cortex-M7
    assert fault.core_arch(0x410CC200) == "ARMv6-M"            # Cortex-M0
    assert fault.core_arch(0x410CC601) == "ARMv6-M"            # Cortex-M0+
    assert fault.core_arch(0x410FD212) == "ARMv8-M Mainline"   # Cortex-M33
    assert fault.core_arch(0x411FD220) == "ARMv8-M Mainline"   # Cortex-M55
    assert fault.core_arch(0x410CD200) == "ARMv8-M Baseline"   # Cortex-M23
    assert not fault.has_cfsr("ARMv6-M") and not fault.has_cfsr("ARMv8-M Baseline")


def test_summary_names_the_root_cause_of_an_escalation():
    r = fault.FaultReport(arch="ARMv7-M", active=3)
    r.registers = {"CFSR": 1 << 25, "HFSR": 1 << 30, "MMFAR": 0, "BFAR": 0}
    r.causes = fault._bits(1 << 30, fault.HFSR_BITS) + fault._bits(1 << 25, fault.CFSR_BITS)
    assert r.summary().startswith("HardFault (escaladée) : division par zéro")
    idle = fault.FaultReport(arch="ARMv7-M", active=0, registers={"CFSR": 0x8200})
    assert "faute passée" in idle.summary() and not idle.in_fault


def test_exc_return():
    f = fault.StackedFrame(base=0, exc_return=0xFFFFFFED, regs={})
    assert f.stack == "PSP" and f.thread_mode and f.fp_context
    assert fault.is_exc_return(0xFFFFFFF9) and fault.is_exc_return(0xFEFFFFFF)
    assert not fault.is_exc_return(0x08000101)


# ====================================================================== QEMU
def faulted(elf, machine, kind, catch=False):
    """Session arrêtée dans le gestionnaire de la faute `kind` de la démo."""
    from gdbtrace.core.session import Session
    from gdbtrace.core.target import QemuTarget, free_port
    s = Session()
    s.connect(elf, QemuTarget(elf=elf, machine=machine, gdb_port=free_port()))
    if catch:
        fault.FaultCatch(s).enable()
    s.go()
    time.sleep(0.1)
    s.halt()
    s.set_var("fault_kind", str(kind))
    s.go()
    if catch:
        s.wait_stopped(10)
    else:
        time.sleep(0.3)
        s.halt()
    return s


M3_CASES = [   # (faute, gestionnaire, bit de cause, adresse fautive, fonction fautive)
    (1, "BusFault", "PRECISERR", 0x30000000, "read_at"),
    (2, "UsageFault", "DIVBYZERO", None, "divide"),
    (3, "UsageFault", "UNALIGNED", None, "trigger"),
    (4, "UsageFault", "UNDEFINSTR", None, "undefined_instruction"),
    (5, "HardFault", "FORCED", None, "undefined_instruction"),
    (6, "UsageFault", "INVSTATE", None, None),
]


@needs_qemu
@pytest.mark.parametrize("kind,exc,bit,addr,func", M3_CASES)
def test_cortex_m3_faults(faults_elfs, kind, exc, bit, addr, func):
    s = faulted(faults_elfs["m3"], "mps2-an385", kind)
    try:
        r = fault.analyze(s)
        assert r.in_fault and r.exception == exc and r.arch == "ARMv7-M", r.text()
        assert bit in [n for n, _ in r.causes] and not r.deduced, r.text()
        assert r.address == addr, r.text()
        if func:
            assert r.fault_func == func and r.fault_line and r.fault_file == "main.c", r.text()
        f = r.frame
        assert f is not None and f.exact and f.stack == "MSP" and f.base % 4 == 0, r.text()
        assert r.fault_pc == f.regs["pc"] and f.regs["xpsr"] & 0x1FF == 0      # mode Thread
        if kind == 5:
            assert "UNDEFINSTR" in [n for n, _ in r.causes]       # la cause d'origine aussi
            assert r.summary().startswith("HardFault (escaladée) : instruction indéfinie")
        if kind == 6:
            assert any("jump_even" in n for n in r.notes), r.notes   # d'où venait le saut
    finally:
        s.disconnect()


M0_CASES = [   # (faute, cause déduite, adresse, fonction)
    (1, "ACCÈS", 0x30000000, "read_at"),
    (3, "NON ALIGNÉ", None, "trigger"),
    (4, "UDF", None, "undefined_instruction"),
    (6, "T=0", None, None),
]


@needs_qemu
@pytest.mark.parametrize("kind,cause,addr,func", M0_CASES)
def test_cortex_m0_faults_are_deduced(faults_elfs, kind, cause, addr, func):
    """Cortex-M0 : tout monte en HardFault, sans registre de cause ; la cause est déduite."""
    s = faulted(faults_elfs["m0"], "microbit", kind)
    try:
        r = fault.analyze(s)
        assert r.in_fault and r.exception == "HardFault" and r.arch == "ARMv6-M", r.text()
        assert r.deduced and not r.registers and r.causes[0][0] == cause, r.text()
        if addr is not None:
            assert r.address == addr
        if kind == 3:
            assert r.address % 4 and "non alignée" in r.causes[0][1]
        if func:
            assert r.fault_func == func, r.text()
    finally:
        s.disconnect()


@needs_qemu
@pytest.mark.parametrize("which", ["m3", "m0"])
def test_break_on_fault_stops_at_handler_entry(faults_elfs, which):
    """QEMU n'émule pas le vector catch : breakpoints sur les gestionnaires de la table des
    vecteurs. L'arrêt a lieu à l'entrée du gestionnaire, avant son prologue."""
    elf, machine = faults_elfs[which], ("mps2-an385" if which == "m3" else "microbit")
    s = faulted(elf, machine, 1, catch=True)
    try:
        bp = {b.func for b in s.breakpoints()}
        want = {"HardFault_Handler", "MemManage_Handler", "BusFault_Handler",
                "UsageFault_Handler"} if which == "m3" else {"HardFault_Handler"}
        assert want <= bp, bp
        r = fault.analyze(s)
        assert r.exception == ("BusFault" if which == "m3" else "HardFault")
        assert r.fault_func == "read_at" and r.address == 0x30000000, r.text()
        assert s.frame().func.endswith("_Handler")
    finally:
        s.disconnect()


SCRIPT = """
gdbtrace.go(until="main"); gdbtrace.wait()          # .bss remise à zéro au reset
gdbtrace.set("fault_kind", 2)
gdbtrace.go(); gdbtrace.wait()
r = gdbtrace.fault()
assert r["en_faute"] and r["exception"] == "UsageFault" and r["fonction"] == "divide", r
print("OK", r["causes"][0]["bit"])
"""


@needs_qemu
def test_batch_break_on_fault(faults_elfs, tmp_path):
    script = tmp_path / "s.py"
    script.write_text(SCRIPT)
    r = subprocess.run([sys.executable, "-m", "gdbtrace", faults_elfs["m3"], "--batch",
                        "--break-on-fault", "--script", str(script)],
                       cwd=ROOT, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0 and "OK DIVBYZERO" in r.stdout, r.stdout + r.stderr
    assert "breakpoint(s) sur les gestionnaires de faute" in r.stdout


@needs_qemu
def test_fault_view_and_status_bar(faults_elfs, tmp_path):
    if not os.environ.get("DISPLAY"):
        pytest.skip("pas de DISPLAY")
    from test_gui import close_app, pump

    from gdbtrace.core.session import Session
    from gdbtrace.core.target import QemuTarget, free_port
    from gdbtrace.gui.app import App
    from gdbtrace.views.fault import FaultView
    elf = faults_elfs["m3"]
    a = App(layout_file=str(tmp_path / "layout.json"))
    try:
        s = Session()
        s.connect(elf, QemuTarget(elf=elf, gdb_port=free_port()))
        s.break_("main", temporary=True)    # .bss remise à zéro au reset : fault_kind écrit après
        s.go()
        s.wait_stopped(10)
        a.attach(s)
        assert pump(a, lambda: s.state == "stopped")
        statuses = []
        set_status = a.status.set
        a.status.set = lambda text: (statuses.append(text), set_status(text))
        a.fault_catch_var.set(True)
        a.toggle_fault_catch()
        assert a.fault_catch_var.get() and a.api.fault_catch_method() == "breakpoints"
        s.set_var("fault_kind", "1")
        a.go()
        assert pump(a, lambda: any(t.startswith("Faute :") for t in statuses), timeout=10), statuses
        shown = next(t for t in statuses if t.startswith("Faute :"))
        assert "BusFault" in shown and "0x30000000" in shown and "read_at" in shown, shown
        v = a.views.find(FaultView)
        assert a.views.where(v) == "data" and a.views.notebooks["data"].select() == str(v)
        assert v is not None and v.report is not None and v.report.fault_func == "read_at"
        texts = {v.tree.item(i, "text") for p in v.tree.get_children() for i in v.tree.get_children(p)}
        assert {"PRECISERR", "BFARVALID", "pc", "CFSR", "BFAR", "r0", "xpsr"} <= texts, texts
        assert str(v.goto.cget("state")) == "normal"
        a.fault_catch_var.set(False)
        a.toggle_fault_catch()
        assert a.api.fault_catch_method() is None and not s.breakpoints()
    finally:
        close_app(a)
