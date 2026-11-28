"""MPU (core/mpu.py) : décodage ARMv7-M et ARMv8-M sur des registres factices, serveur qui
ignore l'écriture de MPU_RNR, puis QEMU (mps2-an385 : Cortex-M3 ; mps3-an547 : Cortex-M55)
avec la démo des fautes, qui pose ses régions au démarrage. Le gdbstub de QEMU 8.2 ignore
l'écriture de MPU_RNR par le débogueur (seule la région choisie par le firmware, ou son groupe
d'alias, est lue, et la note le dit) ; QEMU 11 l'applique (toutes les régions). Les tests QEMU
acceptent les deux, chacun avec ce qu'il doit lire."""
import os
import time

import pytest
from conftest import ROOT, needs_qemu

from gdbtrace.core import mpu

FAULTS = os.path.join(ROOT, "examples", "faults")


class FakeMpu:
    """Registres d'une MPU : RNR choisit la région, RBAR/RASR (ou RLAR) et les alias v8."""

    def __init__(self, regions, v8=False, mair=0, ignore_rnr=False, rnr=0, ctrl=5):
        self.regions = regions               # n → (rbar, rasr ou rlar)
        self.v8, self.mair, self.ignore, self.rnr, self.ctrl = v8, mair, ignore_rnr, rnr, ctrl
        self.writes = []

    def read_u32(self, addr):
        if addr == mpu.MPU_TYPE:
            return len(self.regions) << 8
        if addr == mpu.MPU_CTRL:
            return self.ctrl
        if addr == mpu.MPU_RNR:
            return self.rnr
        if addr == mpu.MPU_MAIR0:
            return self.mair & 0xFFFFFFFF
        if addr == mpu.MPU_MAIR1:
            return self.mair >> 32
        off = addr - mpu.MPU_RBAR
        if 0 <= off < 32:
            k, second = divmod(off, 8)[0], off % 8 == 4
            n = self.rnr if k == 0 else ((self.rnr & ~3) + k if self.v8 else self.rnr)
            return self.regions.get(n, (0, 0))[1 if second else 0]
        raise AssertionError(f"lecture inattendue {addr:#x}")

    def write_u32(self, addr, value):
        assert addr == mpu.MPU_RNR, hex(addr)
        self.writes.append(value)
        if not self.ignore:
            self.rnr = value


def v7_rasr(size_field, ap, xn=0, tex=0, s=0, c=0, b=0, srd=0, en=1):
    return (xn << 28 | ap << 24 | tex << 19 | s << 18 | c << 17 | b << 16 | srd << 8
            | size_field << 1 | en)


def test_armv7m_regions_decoded_and_rnr_restored():
    regs = {0: (0x00000000, v7_rasr(21, 6, c=1)),                         # 4 Mo RO, WT
            1: (0x20000000, v7_rasr(21, 3, xn=1, tex=1, c=1, b=1)),       # RW, XN, WB-WA
            2: (0x20000020, v7_rasr(4, 0, xn=1)),                         # 32 o, aucun accès
            3: (0x40000000, v7_rasr(28, 3, xn=1, s=1, b=1)),              # Device partagé
            4: (0x60000000, v7_rasr(11, 3, srd=0b10000001)),              # 4 Ko, SRD 0 et 7
            5: (0, 0), 6: (0, 0), 7: (0, 0)}
    hw = FakeMpu(regs, rnr=5)
    st = mpu.read_mpu(hw.read_u32, hw.write_u32, "ARMv7-M")
    assert hw.rnr == 5 and hw.writes[-1] == 5             # RNR remis à sa valeur
    assert st.dregion == 8 and st.enabled and len(st.regions) == 8 and not st.note
    r0, r1, r2, r3, r4 = st.regions[:5]
    assert (r0.base, r0.limit, r0.priv, r0.unpriv, r0.xn) == (0, 0x3FFFFF, "RO", "RO", False)
    assert r0.memory.startswith("Normal WT")
    assert (r1.priv, r1.unpriv, r1.xn, r1.memory) == ("RW", "RW", True, "Normal WB-WA")
    assert (r2.size, r2.priv, r2.unpriv) == (32, "aucun", "aucun")
    assert (r3.limit, r3.memory) == (0x5FFFFFFF, "Device partagé")
    assert r4.subregions_off == [0, 7] and r4.contains(0x60000200) and not r4.contains(0x60000000)
    assert not st.regions[5].enabled
    assert st.region_at(0x20000024).number == 2            # la plus haute l'emporte
    assert st.region_at(0x20000100).number == 1 and st.region_at(0x80000000) is None
    assert "8 régions, activée (PRIVDEFENA)" in st.summary()
    assert r1.row()[3] == "4 Mo" and r2.row()[3] == "32 o"


def test_armv8m_regions_with_mair():
    mair = 0xFF | 0x04 << 8 | 0x44 << 16                  # WB RA/WA, Device-nGnRE, non cacheable
    regs = {0: (0x10000000 | 3 << 1, 0x1007FFE0 | 1),
            1: (0x30000000 | 3 << 3 | 1 << 1 | 1, 0x3007FFE0 | 1),
            2: (0x40000000 | 1 << 1 | 1, 0x4FFFFFE0 | 1 << 1 | 1),
            3: (0x50000000, 0x50000FE0 | 2 << 1 | 1 << 4 | 1)}
    hw = FakeMpu(regs, v8=True, mair=mair)
    st = mpu.read_mpu(hw.read_u32, hw.write_u32, "ARMv8-M Mainline")
    r0, r1, r2, r3 = st.regions
    assert (r0.base, r0.limit, r0.priv, r0.unpriv) == (0x10000000, 0x1007FFFF, "RO", "RO")
    assert r0.memory == "attribut 0 : Normal, extérieur WB RWA, intérieur WB RWA"
    assert (r1.priv, r1.unpriv, r1.xn) == ("RW", "RW", True) and "partagé (intérieur)" in r1.memory
    assert r2.memory == "attribut 1 : Device-nGnRE"
    assert r3.pxn and "non cacheable" in r3.memory and r3.size == 4096
    assert mpu.mair_attr(0x00) == "Device-nGnRnE" and mpu.mair_attr(0xBB).startswith("Normal")


def test_server_ignoring_rnr_writes():
    """QEMU 8.2 : RNR ne change pas. ARMv7-M : la seule région choisie ; ARMv8-M : elle et les
    alias de son groupe de quatre ; la note le dit."""
    regs = {n: (0x20000000 + 0x100 * n, v7_rasr(7, 3)) for n in range(8)}
    hw = FakeMpu(regs, rnr=3, ignore_rnr=True)
    st = mpu.read_mpu(hw.read_u32, hw.write_u32, "ARMv7-M")
    assert [r.number for r in st.regions] == [3] and "QEMU" in st.note and hw.rnr == 3
    regs = {n: (0x30000000 + 0x100 * n | 1 << 1, 0x30000000 + 0x100 * n + 0xE0 | 1) for n in range(16)}
    hw = FakeMpu(regs, v8=True, rnr=6, ignore_rnr=True)
    st = mpu.read_mpu(hw.read_u32, hw.write_u32, "ARMv8-M Mainline")
    assert [r.number for r in st.regions] == [5, 6, 7]     # RNR, puis (RNR & ~3) + 1..3
    assert st.regions[0].base == 0x30000500 and "3 lue(s)" in st.summary()


def test_no_mpu():
    st = mpu.read_mpu(FakeMpu({}).read_u32, None, "ARMv7-M")
    assert st.dregion == 0 and st.regions == [] and "pas de MPU" in st.summary()


# ====================================================================== QEMU
def _rnr_ignored(st) -> bool:
    """QEMU 8.2 : l'écriture de MPU_RNR par le débogueur est ignorée (régions lues < DREGION,
    note) ; QEMU 11 : appliquée, toutes les régions sont lues, sans note."""
    if len(st.regions) < st.dregion:
        assert "QEMU" in st.note, st.note
        return True
    assert len(st.regions) == st.dregion and not st.note, (st.regions, st.note)
    return False


def _demo(elf, machine):
    from gdbtrace.core.session import Session
    from gdbtrace.core.target import QemuTarget, free_port
    s = Session()
    s.connect(elf, QemuTarget(elf=elf, machine=machine, gdb_port=free_port()))
    s.go()
    time.sleep(0.2)                              # main a posé ses régions
    s.halt()
    return s


@needs_qemu
def test_qemu_m3_mpu_and_memmanage(faults_elfs):
    from gdbtrace.core import fault
    from gdbtrace.core.scripting import ScriptAPI
    s = _demo(faults_elfs["m3"], "mps2-an385")
    try:
        st = ScriptAPI(s).mpu(quiet=True)
        assert st.arch == "ARMv7-M" and st.dregion == 8 and st.enabled and st.ctrl == 5
        guard = s.address_of("mpu_guard")
        regions = {r.number: r for r in st.regions}
        ignored = _rnr_ignored(st)
        if ignored:                                             # QEMU 8.2 : RNR laissé à 3
            assert list(regions) == [3]
        else:                                                   # QEMU 11 : les 8 régions
            assert [n for n, r in regions.items() if r.enabled] == [0, 1, 2, 3]
            g = regions[2]
            assert (g.base, g.limit, g.priv, g.unpriv, g.xn) == (guard, guard + 31, "aucun", "aucun", True)
            assert (regions[0].base, regions[0].priv, regions[1].base) == (0, "RO", 0x20000000)
        r = regions[3]
        assert (r.base, r.limit, r.priv, r.unpriv, r.xn, r.memory) == (
            0x40000000, 0x5FFFFFFF, "RW", "RW", True, "Device partagé")
        s.set_var("fault_kind", "7")
        s.go()
        time.sleep(0.3)
        s.halt()
        rep = fault.analyze(s)
        assert rep.handler == "MemManage" if hasattr(rep, "handler") else True
        names = [n for n, _ in rep.causes]
        assert "DACCVIOL" in names and rep.address == guard + 4
        mpu_cause = dict(rep.causes)["MPU"]
        if ignored:
            assert "hors des régions lues" in mpu_cause        # QEMU 8.2 : région 2 illisible
        else:
            assert mpu_cause.startswith("région 2 ") and "aucun" in mpu_cause
    finally:
        s.disconnect()


@pytest.fixture(scope="module")
def m55_elf():
    import shutil
    import subprocess
    if not shutil.which("arm-none-eabi-gcc"):
        pytest.skip("arm-none-eabi-gcc absent")
    subprocess.run(["make", "-s", "-C", FAULTS, "faults-m55.elf"], check=True)
    return os.path.join(FAULTS, "faults-m55.elf")


@needs_qemu
def test_qemu_m55_mpu_v8_by_aliases(m55_elf):
    from gdbtrace.core import fault
    from gdbtrace.core.scripting import ScriptAPI
    s = _demo(m55_elf, "mps3-an547")
    try:
        st = ScriptAPI(s).mpu(quiet=True)
        assert st.arch == "ARMv8-M Mainline" and st.dregion == 16 and st.enabled
        regions = {r.number: r for r in st.regions}
        if _rnr_ignored(st):                                    # QEMU 8.2 : RNR = 3, alias A1..A3
            assert list(regions) == [1, 2, 3]
        else:                                                   # QEMU 11 : les 16 régions
            assert [n for n, r in regions.items() if r.enabled] == [0, 1, 2, 3]
            r0 = regions[0]
            assert (r0.base, r0.limit, r0.priv) == (0x10000000, 0x1007FFFF, "RO")
        r1, r2, r3 = regions[1], regions[2], regions[3]
        guard = s.address_of("mpu_guard")
        assert (r1.base, r1.limit, r1.priv, r1.xn) == (0x30000000, 0x3007FFFF, "RW", True)
        assert (r2.base, r2.limit, r2.priv, r2.unpriv) == (guard, guard + 31, "RO", "aucun")
        assert r3.memory == "attribut 1 : Device-nGnRE" and "Normal" in r1.memory
        s.set_var("fault_kind", "7")
        s.go()
        time.sleep(0.3)
        s.halt()
        rep = fault.analyze(s)
        assert "DACCVIOL" in [n for n, _ in rep.causes] and rep.address == guard + 4
        assert dict(rep.causes)["MPU"].startswith("région 2 ")
    finally:
        s.disconnect()


@needs_qemu
def test_system_view_mpu_tab(faults_elfs):
    if not os.environ.get("DISPLAY"):
        pytest.skip("pas de DISPLAY")
    from test_gui import close_app, pump

    from gdbtrace.core.session import Session
    from gdbtrace.core.target import QemuTarget, free_port
    from gdbtrace.gui.app import App
    from gdbtrace.views.system import SystemView
    elf = faults_elfs["m3"]
    app = App(layout_file=os.devnull)
    try:
        s = Session()
        s.connect(elf, QemuTarget(elf=elf, gdb_port=free_port()))
        app.attach(s)
        pump(app, lambda: app.views.find(SystemView) is not None)
        view = app.views.find(SystemView)
        view.set_state({"tab": "MPU"})
        app.api.go()
        time.sleep(0.2)
        app.api.halt()
        pump(app, lambda: view.mpu_tree.get_children())
        rows = {view.mpu_tree.item(i)["text"]: view.mpu_tree.item(i) for i in view.mpu_tree.get_children()}
        assert rows["3"]["values"][-1] == "Device partagé"
        assert list(rows) in (["3"], [str(n) for n in range(8)])  # QEMU 8.2 : RNR ignoré ; 11 : toutes
        assert "8 régions, activée" in view.mpu_info.cget("text")
    finally:
        close_app(app)
