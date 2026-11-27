"""Tables ROM CoreSight (trace/coresight.py) : parcours sur des mémoires factices de puces
réelles (M4, M7, M55, M0+), effets sur la trace, et QEMU (sans table)."""
import os
import struct
import sys

import pytest
from conftest import needs_qemu

from gdbtrace.trace import coresight as cs
from gdbtrace.trace.manager import TraceManager

ARM = cs.ARM


class FakeMemory:
    """Pages de composants CoreSight ; toute autre adresse est illisible."""

    def __init__(self):
        self.words: dict[int, int] = {}

    def component(self, addr, part, cls, designer=ARM, rev=0, devarch=0, devtype=0, devid=0):
        w = self.words
        for i, b in enumerate((0x0D, cls << 4, 0x05, 0xB1)):
            w[addr + 0xFF0 + 4 * i] = b
        w[addr + 0xFE0] = part & 0xFF
        w[addr + 0xFE4] = (part >> 8) | (designer & 0xF) << 4
        w[addr + 0xFE8] = (designer >> 4) & 0x7 | 0x8 | rev << 4
        w[addr + 0xFEC] = 0
        w[addr + 0xFD0] = designer >> 7
        for off in (0xFD4, 0xFD8, 0xFDC):
            w[addr + off] = 0
        if cls == 9:
            w[addr + 0xFBC] = devarch
            w[addr + 0xFC0] = w[addr + 0xFC4] = 0
            w[addr + 0xFC8] = devid
            w[addr + 0xFCC] = devtype

    def table(self, addr, part, targets, cls=1, designer=ARM):
        if cls == 9:
            self.component(addr, part, 9, designer, devarch=0x47700AF7)
        else:
            self.component(addr, part, 1, designer)
        for i, t in enumerate(targets):
            off = (t - addr) & 0xFFFFF000 if t is not None else 0
            self.words[addr + 4 * i] = off | (3 if t is not None else 2)   # absente : bit 0 nul
        self.words[addr + 4 * len(targets)] = 0

    def read(self, addr, n):
        out = b""
        for a in range(addr, addr + n, 4):
            if a not in self.words:
                raise OSError(f"{a:#x} non mappée")
            out += struct.pack("<I", self.words[a])
        return out


def devarch(archid):
    return 0x47700000 | archid          # architecte ARM, présent


def cortex_m4():
    m = FakeMemory()
    m.table(0xE00FF000, 0x4C4, [0xE000E000, 0xE0001000, 0xE0002000, 0xE0000000, 0xE0040000, 0xE0041000])
    m.component(0xE000E000, 0x00C, 0xE)
    m.component(0xE0001000, 0x002, 0xE)
    m.component(0xE0002000, 0x003, 0xE)
    m.component(0xE0000000, 0x001, 0xE)
    m.component(0xE0040000, 0x9A1, 9, devtype=0x11)
    m.component(0xE0041000, 0x925, 9, devtype=0x13)
    return m


def cortex_m7():
    """Table du fabricant (ST, 0xE00FD000) → table du M7 → table du PPB (SCS, DWT, FPB, ITM)
    et, à part, ETM-M7 et CTI."""
    m = FakeMemory()
    m.table(0xE00FD000, 0x449, [0xE00FE000], designer=0x020)
    m.table(0xE00FE000, 0x4C8, [0xE00FF000, 0xE0041000, 0xE0042000])
    m.table(0xE00FF000, 0x4C7, [0xE000E000, 0xE0001000, 0xE0002000, 0xE0000000, 0xE0040000, None])
    m.component(0xE000E000, 0x00C, 0xE)
    m.component(0xE0001000, 0x002, 0xE)
    m.component(0xE0002000, 0x00E, 0xE)
    m.component(0xE0000000, 0x001, 0xE)
    m.component(0xE0040000, 0x9A9, 9, devtype=0x11)
    m.component(0xE0041000, 0x975, 9, devarch=devarch(0x4A13), devtype=0x13)
    m.component(0xE0042000, 0x906, 9, devarch=devarch(0x1A14), devtype=0x14)
    return m


def cortex_m55():
    """Table de classe 9 ; tous les composants du cœur sont la pièce 0xD22, nommés par DEVARCH."""
    m = FakeMemory()
    m.table(0xE00FF000, 0x4C9, [0xE000E000, 0xE0001000, 0xE0002000, 0xE0000000, 0xE0041000,
                                0xE0042000, 0xE0003000], cls=9)
    for addr, arch in ((0xE000E000, 0x2A04), (0xE0001000, 0x1A02), (0xE0002000, 0x1A03),
                       (0xE0000000, 0x1A01), (0xE0041000, 0x4A13 | 0x50000), (0xE0042000, 0x1A14),
                       (0xE0003000, 0x2A16)):
        m.component(addr, 0xD22, 9, devarch=devarch(arch))
    return m


def test_cortex_m4_classic_table():
    comps = cs.walk(cortex_m4().read)
    assert [c.kind for c in comps] == ["ROM", "SCS", "DWT", "FPB", "ITM", "TPIU", "ETM"]
    etm = cs.find(comps, "ETM")
    assert etm.etm_version == 3 and etm.name == "ETM-M4" and etm.designer_name == "ARM"
    assert comps[0].name == "table ROM (Cortex-M4)" and comps[0].depth == 0
    assert all(c.depth == 1 for c in comps[1:])
    assert "TPIU (Cortex-M4)" in [c.name for c in comps]


def test_cortex_m7_nested_tables_and_absent_entry():
    comps = cs.walk(cortex_m7().read)
    names = [c.name for c in comps]
    assert names[:3] == ["table ROM (fabricant STMicroelectronics)", "table ROM (Cortex-M7)",
                         "table ROM (Cortex-M7 (PPB))"]
    assert [c.depth for c in comps if c.kind == "ROM"] == [0, 1, 2]
    etm = cs.find(comps, "ETM")
    assert etm.etm_version == 4 and etm.addr == 0xE0041000
    assert cs.find(comps, "CTI").addr == 0xE0042000
    assert cs.find(comps, "FPB").name == "FPB (Cortex-M7)"
    assert len(comps) == 10                      # 3 tables, 7 composants ; l'entrée absente sautée


def test_cortex_m55_class9_table_named_by_devarch():
    comps = cs.walk(cortex_m55().read)
    kinds = [c.kind for c in comps]
    assert kinds == ["ROM", "SCS", "DWT", "FPB", "ITM", "ETM", "CTI", "PMU"]
    assert cs.find(comps, "ETM").etm_version == 4 and comps[0].cls == 9
    assert cs.find(comps, "PMU").name == "PMU (Cortex-M55)"


def test_negative_offset_loop_and_vendor_table():
    """Table d'un fabricant au-dessus de sa MTB (décalage négatif) ; une table qui se liste
    elle-même n'est parcourue qu'une fois."""
    m = FakeMemory()
    m.table(0xF0002000, 0x000, [0xF0000000, 0xF0002000], designer=0x00E)
    m.component(0xF0000000, 0x932, 9, devtype=0x31)
    comps = cs.walk(m.read, roots=(0xF0002000,))
    assert [(c.addr, c.name) for c in comps] == [(0xF0002000, "table ROM (fabricant Freescale/NXP)"),
                                                 (0xF0000000, "MTB-M0+")]
    assert cs.walk(m.read) == []                 # rien aux tables du cœur
    from gdbtrace.trace import mtb
    assert mtb.find_mtb(lambda a: struct.unpack("<I", m.read(a, 4))[0]) == 0xF0000000


def test_tmc_configuration_and_unknown_parts():
    m = FakeMemory()
    m.table(0xE00FF000, 0x4C4, [0xE0044000, 0xE0045000, 0xE0046000])
    m.component(0xE0044000, 0x961, 9, devtype=0x32, devid=2 << 6)     # TMC en ETF
    m.component(0xE0045000, 0x961, 9, devtype=0x21, devid=1 << 6)     # TMC en ETR
    m.component(0xE0046000, 0x123, 9, designer=0x020, devtype=0x12)   # funnel d'un fabricant
    kinds = [c.kind for c in cs.walk(m.read)][1:]
    assert kinds == ["ETF", "ETR", "funnel"]
    assert cs.walk(m.read)[3].name == "funnel (fabricant STMicroelectronics)"


class _Session:
    def __init__(self, mem, cpuid):
        self.mem, self.cpuid = mem, cpuid

    def read_memory(self, addr, n):
        return self.mem.read(addr, n)

    def read_u32(self, addr):
        if addr == 0xE000ED00:
            return self.cpuid
        return struct.unpack("<I", self.mem.read(addr, 4))[0]


def test_trace_manager_uses_the_table():
    """ETM de la table : sa version passe au décodeur ; une table sans ETM refuse toute capture
    ETM en le disant ; sans table, rien ne change."""
    tm = TraceManager()
    s = _Session(cortex_m7(), 0x410FC270)
    tm.discover(s)
    assert tm.has_component("ETM") and tm.no_etm_reason() is None
    m = cortex_m4()
    del m.words[0xE00FF000 + 4 * 5]               # ETM retiré de la table
    m.words[0xE00FF000 + 4 * 5] = 0
    s = _Session(m, 0x410FC241)
    tm.discover(s)
    tm.detect_core(s)
    assert tm.has_component("ETM") is False and tm.has_component("ITM")
    reason = tm.no_etm_reason()
    assert "pas d'ETM" in reason and "Cortex-M4" in reason
    with pytest.raises(RuntimeError, match="pas d'ETM"):
        tm._require_etm()
    assert tm.setup_error == reason
    tm.discover(_Session(FakeMemory(), 0x410FC241))  # QEMU : aucune table
    assert tm.components == [] and tm.has_component("ETM") is None and tm.no_etm_reason() is None


@needs_qemu
def test_qemu_has_no_rom_table(session):
    from gdbtrace.core.scripting import ScriptAPI
    api = ScriptAPI(session)
    api.trace = TraceManager()
    assert api.coresight() == []
    assert api.trace.components == [] and api.trace.no_etm_reason() is None


@needs_qemu
def test_system_view_coresight_tab(elf):
    if not os.environ.get("DISPLAY"):
        pytest.skip("pas de DISPLAY")
    from test_gui import close_app, pump

    from gdbtrace.core.session import Session
    from gdbtrace.core.target import QemuTarget, free_port
    from gdbtrace.gui.app import App
    from gdbtrace.views.system import SystemView
    app = App(layout_file=os.devnull)
    try:
        s = Session()
        s.connect(elf, QemuTarget(elf=elf, gdb_port=free_port()))
        app.attach(s)
        pump(app, lambda: app.views.find(SystemView) is not None)
        view = app.views.find(SystemView)
        assert view is not None
        view.render_coresight()
        assert "pas de table ROM" in view.cs_info.cget("text")
        app.trace.components = cs.walk(cortex_m7().read)    # table d'une carte, vue rendue
        view.render_coresight()
        assert "ETMv4" in view.cs_info.cget("text")
        top = view.cs_tree.get_children()
        assert len(top) == 1                                # une racine, les tables imbriquées
        assert view.get_state() == {"tab": "CoreSight"}
    finally:
        close_app(app)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
