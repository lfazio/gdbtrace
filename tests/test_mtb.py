"""Trace MTB (Cortex-M0+) : paquets, registres, décodage, chemins QEMU réels."""
import struct

import pytest
from conftest import needs_qemu

from gdbtrace.trace import mtb
from gdbtrace.trace.etm.image import DIRECT, INDIRECT, NORMAL, Instr
from gdbtrace.trace.etm.qemu_log import ExcEntry, ExcReturn, Step


# ====================================================================== programme factice
def I(addr, kind=NORMAL, size=2, target=None, cond=False, text="adds r0, #1", call=False, ret=False):
    return Instr(addr, size, kind, target, cond, call, ret, text)


class FakeImage:
    """Table des vecteurs à 0 (SP, Reset, NMI, HardFault, ... SysTick = 15) puis du code."""

    def __init__(self, instrs, vectors):
        self.m = {i.addr: i for i in instrs}
        words = [0x20004000] + [vectors.get(n, 0) | 1 if vectors.get(n) else 0 for n in range(1, 16)]
        self.vt = struct.pack("<16I", *words)

    def instr(self, a):
        return self.m.get(a & ~1)

    def read(self, addr, n):
        return self.vt[addr:addr + n] if addr + n <= len(self.vt) else None


# main : 0x100..0x10A ; boucle avec bne ; handler SysTick à 0x200 ; Reset à 0x80
CODE = [I(0x80, DIRECT, 2, 0x100, text="b #0x100"),
        I(0x100), I(0x102), I(0x104, DIRECT, 2, 0x100, cond=True, text="bne #0x100"),
        I(0x106, DIRECT, 4, 0x300, call=True, text="bl #0x300"), I(0x10A, DIRECT, 2, 0x100, text="b #0x100"),
        I(0x200), I(0x202, INDIRECT, text="bx lr", ret=True),
        I(0x220), I(0x222, INDIRECT, text="bx lr", ret=True),
        I(0x300), I(0x302, INDIRECT, text="bx lr", ret=True)]
IMG = FakeImage(CODE, {1: 0x80, 14: 0x220, 15: 0x200})


def P(src, dst, a=False, s=False):
    return mtb.Packet(src, dst, a, s)


def steps(events):
    return [(e.addr, e.executed) for e in events if isinstance(e, Step)]


def test_packet_bits_roundtrip():
    raw = P(0x104, 0x100, a=False, s=True).pack() + P(0x102, 0x200, a=True).pack()
    assert struct.unpack("<4I", raw) == (0x104, 0x101, 0x103, 0x200)
    assert mtb.parse_packets(raw) == [P(0x104, 0x100, s=True), P(0x102, 0x200, a=True)]


def test_branches_loop_and_call():
    # 0x100 → bne pris (boucle), puis non pris, bl, retour, b
    pk = [P(0x104, 0x100, s=True), P(0x106, 0x300), P(0x302, 0x10A), P(0x10A, 0x100)]
    d = mtb.MtbDecoder(IMG)
    ev = d.decode(pk, end_pc=0x104)
    assert steps(ev) == [(0x104, True), (0x100, True), (0x102, True), (0x104, False),
                         (0x106, True), (0x300, True), (0x302, True), (0x10A, True),
                         (0x100, True), (0x102, True)]
    assert d.stats.errors == 0 and d.stats.sessions == 1


def test_exception_entry_return_and_tail_chain():
    pk = [P(0x104, 0x100, s=True),
          P(0x102, 0x200, a=True),                       # SysTick avant 0x102 (non exécutée)
          P(0x202, mtb.EXC_RETURN), P(mtb.EXC_RETURN, 0x220, a=True),   # enchaînement PendSV
          P(0x222, mtb.EXC_RETURN), P(mtb.EXC_RETURN, 0x102, a=True)]   # retour au code
    d = mtb.MtbDecoder(IMG)
    ev = d.decode(pk, end_pc=0x104)
    kinds = [(type(e).__name__, getattr(e, "addr", getattr(e, "handler", None)),
              getattr(e, "tailchain", None)) for e in ev]
    assert kinds == [("Step", 0x104, None), ("Step", 0x100, None),
                     ("ExcEntry", 0x200, False), ("Step", 0x200, None), ("Step", 0x202, None),
                     ("ExcEntry", 0x220, True), ("Step", 0x220, None), ("Step", 0x222, None),
                     ("ExcReturn", None, None), ("Step", 0x102, None)]
    entries = [e for e in ev if isinstance(e, ExcEntry)]
    assert [e.number for e in entries] == [15, 14] and d.stats.exceptions == 2


def test_debugger_pc_change_and_resync():
    d = mtb.MtbDecoder(IMG)
    # PC réécrit par le débogueur vers Reset_Handler (pas une exception), puis paquet impossible
    ev = d.decode([P(0x104, 0x100, s=True), P(0x102, 0x80, a=True), P(0x80, 0x100),
                   P(0x300, 0x302)])                                    # 0x100 → 0x300 : faux
    assert not any(isinstance(e, ExcEntry) for e in ev) and d.stats.debugger == 1
    assert d.stats.errors == 1 and steps(ev)[-1] == (0x300, True)      # resynchronisé


def test_new_session_resets_flow():
    d = mtb.MtbDecoder(IMG)
    ev = d.decode([P(0x104, 0x100, s=True), P(0x10A, 0x100, s=True)])  # trace redémarrée
    assert steps(ev) == [(0x104, True), (0x10A, True)] and d.stats.sessions == 2


# ====================================================================== registres
class FakeMtb:
    """Registres et SRAM d'une MTB à 0xF0000000 (SRAM tracée à 0x1FFFF000)."""

    def __init__(self):
        self.mem = {0xF0000000 + mtb.BASE: 0x1FFFF000}
        self.ram = bytearray(4096)

    def read_u32(self, a):
        return self.mem.get(a, 0)

    def write_u32(self, a, v):
        self.mem[a] = v

    def read_mem(self, a, n):
        off = a - 0x1FFFF000
        return bytes(self.ram[off:off + n])

    def hw_write(self, packets):
        """La MTB matérielle : écrit les paquets en circulaire et met POSITION à jour."""
        st = mtb.read_state(self.read_u32, 0xF0000000)
        start, idx, wrap = st.start - st.sram, st.index, st.wrapped
        for p in packets:
            self.ram[start + idx:start + idx + 8] = p.pack()
            idx += 8
            if idx == st.size:
                idx, wrap = 0, True
        self.write_u32(0xF0000000, start + idx | (mtb.POSITION_WRAP if wrap else 0))


def test_configure_read_wrap_restart():
    hw = FakeMtb()
    st = mtb.configure(hw.write_u32, hw.read_u32, 0xF0000000, 0x1FFFF400, 64)
    assert st.enabled and st.size == 64 and st.start == 0x1FFFF400 and st.index == 0
    assert hw.mem[0xF0000000 + mtb.MASTER] == mtb.MASTER_EN | 2           # 2^(2+4) = 64
    pk = [P(0x100 + 2 * i, 0x200 + 2 * i, s=i == 0) for i in range(11)]  # 11 paquets, 8 places
    hw.hw_write(pk)
    got, st = mtb.read_packets(hw.read_u32, hw.read_mem, 0xF0000000)
    assert st.wrapped and got == pk[-8:]                                  # les 8 derniers, dans l'ordre
    mtb.restart(hw.write_u32, 0xF0000000, st)
    assert mtb.read_packets(hw.read_u32, hw.read_mem, 0xF0000000)[0] == []
    hw.hw_write(pk[:3])
    assert mtb.read_packets(hw.read_u32, hw.read_mem, 0xF0000000)[0] == pk[:3]
    with pytest.raises(ValueError, match="aligné"):
        mtb.configure(hw.write_u32, hw.read_u32, 0xF0000000, 0x1FFFF410, 64)
    with pytest.raises(ValueError, match="puissance de 2"):
        mtb.configure(hw.write_u32, hw.read_u32, 0xF0000000, 0x1FFFF400, 48)


def test_find_mtb_in_rom_table():
    regs = {}

    def component(addr, part, cls, designer=0x43B):
        for i, b in enumerate((0x0D, cls << 4, 0x05, 0xB1)):
            regs[addr + 0xFF0 + 4 * i] = b
        regs[addr + 0xFE0] = part & 0xFF
        regs[addr + 0xFE4] = (part >> 8) | (designer & 0xF) << 4
        regs[addr + 0xFE8] = (designer >> 4) & 0x7
        regs[addr + 0xFD0] = designer >> 7

    def read(a):
        if a not in regs:
            raise OSError("non mappé")
        return regs[a]
    # table du fabricant à 0xF0002000 → MTB à 0xF0000000 (décalage négatif) et table du cœur
    component(0xF0002000, 0, 1, designer=0x00E)
    regs[0xF0002000] = (0xF0000000 - 0xF0002000) & 0xFFFFF000 | 3
    regs[0xF0002004] = 0
    component(0xF0000000, mtb.PART_MTB_M0P, 9)
    found = mtb.rom_components(read)
    assert [c.name for c in found if c.depth] == ["MTB-M0+"]
    assert mtb.find_mtb(read) == 0xF0000000
    assert mtb.find_mtb(lambda a: (_ for _ in ()).throw(OSError())) is None


# ====================================================================== chemins réels
def _events_equal(a, b):
    def key(e):
        if isinstance(e, Step):
            return ("S", e.addr, e.executed)
        if isinstance(e, ExcEntry):
            return ("E", e.handler, e.tailchain)
        return ("R",)
    return [key(e) for e in a] == [key(e) for e in b]


@needs_qemu
@pytest.mark.parametrize("name", ["bare", "rtos"])
def test_real_paths_roundtrip(qemu_paths, name):
    """Cortex-M3 (Thumb-2, blocs IT) : démo bare-metal et FreeRTOS."""
    _roundtrip(*qemu_paths[name])


@needs_qemu
def test_cortex_m0plus_path_roundtrip(m0_path):
    """Code ARMv6-M réel (démo compilée pour Cortex-M0+, QEMU microbit)."""
    img, events = m0_path
    sizes = {img.instr(e.addr).size for e in events if isinstance(e, Step)}
    assert sizes == {2, 4}                       # Thumb-1 + BL / MSR / DMB : pas de Thumb-2 large
    texts = {img.instr(e.addr).text.split()[0] for e in events if isinstance(e, Step)}
    assert not any(t.startswith("it") for t in texts)
    _roundtrip(img, events)


def _roundtrip(img, events):
    """Chemin QEMU → paquets MTB → décodage == chemin réel, issues des branchements comprises ;
    tampon circulaire : le décodage des derniers paquets est la fin du décodage complet."""
    from gdbtrace.trace.etm.cycles import successors
    pk = mtb.encode(events, img)
    first = next(i for i, e in enumerate(events)
                 if isinstance(e, Step) and e.addr == pk[0].src)    # source du 1er paquet
    last_step = max(i for i, e in enumerate(events) if isinstance(e, Step))
    d = mtb.MtbDecoder(img)
    got = d.decode(pk, end_pc=events[last_step].addr)
    nxt = successors(events)

    def taken(i, e):
        """Issue réelle : un branchement est pris si l'instruction suivante du même contexte
        n'est pas la suivante en séquence (`bxeq lr` d'un bloc IT compris). None quand une
        exception suit : le journal QEMU ne dit pas où le contexte reprendra."""
        ins = img.instr(e.addr)
        if ins.kind not in (DIRECT, INDIRECT) or (ins.kind == DIRECT and ins.target == e.addr + ins.size):
            return True
        if isinstance(events[i + 1], (ExcEntry, ExcReturn)):
            return None
        return nxt.get(i, e.addr + ins.size) != e.addr + ins.size
    want = []
    for i, e in enumerate(events[first:last_step], first):
        if isinstance(e, ExcEntry) and want and isinstance(want[-1], ExcReturn):
            want[-1] = ExcEntry(e.number, e.handler, tailchain=True)   # retour + entrée = enchaînement
        else:
            want.append(Step(e.addr, executed=taken(i, e)) if isinstance(e, Step) else e)
    assert d.stats.errors == 0 and d.stats.exceptions > 10
    assert len(got) == len(want)
    known = [k for k, w in enumerate(want) if not isinstance(w, Step) or w.executed is not None]
    assert _events_equal([got[k] for k in known], [want[k] for k in known])
    assert [getattr(e, "addr", None) for e in got] == [getattr(e, "addr", None) for e in want]
    assert sum(1 for e in got if isinstance(e, Step) and not e.executed) > 100   # branches non prises
    # enregistreur de vol : 1 Ko = 128 paquets
    tail = mtb.last(pk, 1024)
    d2 = mtb.MtbDecoder(img)
    end = d2.decode(tail, end_pc=events[last_step].addr)
    assert d2.stats.errors == 0 and len(steps(end)) > 128
    assert steps(end) == steps(got)[-len(steps(end)):]


# ====================================================================== capture sur cible
class FakeSession(FakeMtb):
    """Session gdb factice : registres MTB et SRAM (FakeMtb), PC courant."""

    def __init__(self, pc):
        super().__init__()
        self.pc = pc

    def register(self, name):
        assert name == "pc"
        return self.pc

    read_memory = FakeMtb.read_mem


def test_capture_collect_restart_and_continuity():
    """MtbCapture : programmation, lecture à chaque arrêt, remise à zéro ; sans débordement,
    la reprise part du PC de l'arrêt précédent (aucune instruction perdue)."""
    from gdbtrace.trace.mtb_capture import MtbCapture
    s = FakeSession(pc=0x100)
    cap = MtbCapture(s, IMG, 0xF0000000, 0x1FFFF400, 64)
    assert s.mem[0xF0000000 + mtb.MASTER] & mtb.MASTER_EN
    # 1re exécution : 0x100, 0x102, bne pris (0x104 → 0x100), arrêt avant 0x102
    s.hw_write([P(0x104, 0x100, s=True)])
    s.pc = 0x102
    ev = cap.collect()
    assert steps(ev) == [(0x100, True), (0x102, True), (0x104, True), (0x100, True)]
    assert all(e.cycles > 0 for e in ev)                         # durées du modèle (estimées)
    assert mtb.read_state(s.read_u32, 0xF0000000).index == 0      # tampon remis à zéro
    # 2e exécution : reprend à 0x102 ; bne non pris, bl, retour
    s.hw_write([P(0x106, 0x300), P(0x302, 0x10A)])
    s.pc = 0x10A
    assert steps(cap.collect()) == [(0x102, True), (0x104, False), (0x106, True), (0x300, True),
                                    (0x302, True)]
    assert cap.last_packets == [P(0x106, 0x300), P(0x302, 0x10A)]


@needs_qemu
def test_simulated_mtb_feeds_trace_and_coverage(m0_path):
    """QemuMtbSim (sans QEMU en marche : conversion d'un chemin enregistré) → EtmTrace :
    instructions, couverture des branches, durées estimées."""
    from gdbtrace.core.symbols import SymbolTable
    from gdbtrace.trace.etm.trace import EtmTrace
    from gdbtrace.trace.mtb_capture import QemuMtbSim
    img, events = m0_path
    trace = EtmTrace(img, SymbolTable(img.path))
    sim = QemuMtbSim(qmp=None, image=img, etm_trace=trace, size=0)
    sim.runs = 1
    sim._convert(list(events), end_pc=None)
    st = trace.stats
    assert st["instructions"] > 200_000 and st["erreurs"] == 0
    funcs = {f.name: f for f in trace.cov.functions()}
    assert funcs["fib"].status == "ok" and funcs["delay"].branches >= 1
    small = EtmTrace(img, SymbolTable(img.path))
    sim2 = QemuMtbSim(qmp=None, image=img, etm_trace=small, size=1024)
    sim2.runs = 1
    sim2._convert(list(events), end_pc=None)
    assert 128 * 2 < small.stats["instructions"] < st["instructions"] // 10   # 128 derniers branchements
    import shutil
    shutil.rmtree(sim.dir), shutil.rmtree(sim2.dir)
