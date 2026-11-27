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
            if st.autohalt and start + idx == st.watermark:      # seuil atteint : HALTREQ
                self.mem[0xF0000000 + mtb.MASTER] |= mtb.MASTER_HALTREQ
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

    def component(addr, part, cls, designer=0x23B):
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


def test_head_ends_with_an_instruction():
    """Un chemin coupé après N instructions finit par la Nᵉ : l'exception qui la suit appartient
    à la suite (gardée, elle rendait la fin du chemin aléatoire selon les interruptions)."""
    from gdbtrace.trace.etm.qemu_log import head
    ev = [Step(0x10), Step(0x12), ExcReturn(), Step(0x14), ExcEntry(15, 0x40), Step(0x40)]
    assert head(ev, 2) == ev[:2] and head(ev, 3) == ev[:4]
    assert head(ev, 0) == [] and head(ev, 10) == ev


@needs_qemu
@pytest.mark.parametrize("kind", [ExcReturn, ExcEntry])
def test_roundtrip_cut_where_an_exception_follows(qemu_paths, kind):
    """Coupe du chemin juste avant une exception (retour, entrée) : l'encodage et le décodage
    s'accordent. Avant, la coupe gardait l'exception : une instruction de trop (retour) ou une
    erreur de décodage (entrée), au gré du minutage des SysTick (échec intermittent sous
    charge)."""
    from gdbtrace.trace.etm.qemu_log import head
    img, events = qemu_paths["bare"]
    k = next(i for i in range(len(events) - 2, 0, -1)
             if isinstance(events[i], Step) and isinstance(events[i + 1], kind))
    n = sum(1 for e in events[:k + 1] if isinstance(e, Step))
    cut = head(events, n)
    assert cut[-1] is events[k] and n > 200_000
    _roundtrip(img, cut)


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


@needs_qemu
@pytest.mark.parametrize("name", ["bare", "rtos", "m0"])
def test_etm_and_mtb_agree(qemu_paths, m0_path, name):
    """Deux chaînes indépendantes (ETMv3 : atomes ; MTB : paquets de branchement) donnent,
    instruction par instruction, les mêmes issues sur le même chemin réel — blocs IT compris."""
    from gdbtrace.trace.etm.encoder import encode as etm_encode
    from gdbtrace.trace.etm.flow import decode as etm_decode
    img, events = m0_path if name == "m0" else qemu_paths[name]
    etm, pk, _ = etm_decode(etm_encode(events, img), img)
    assert pk.errors == 0
    packets = mtb.encode(events, img)
    first = next(i for i, e in enumerate(events) if isinstance(e, Step) and e.addr == packets[0].src)
    last_step = max(i for i, e in enumerate(events) if isinstance(e, Step))
    d = mtb.MtbDecoder(img)
    got = steps(d.decode(packets, end_pc=events[last_step].addr))
    skip = sum(isinstance(e, Step) for e in events[:first])
    assert d.stats.errors == 0 and len(got) > 100_000
    assert steps(etm)[skip:skip + len(got)] == got



# ====================================================================== AUTOHALT
def test_autohalt_configure_collect_and_clear():
    """Seuil = fin du tampon moins la marge ; HALTREQ lu par collect() puis effacé (sinon le
    cœur s'arrêterait de nouveau dès la reprise), MASTER.EN conservé."""
    from gdbtrace.trace.mtb_capture import MtbCapture
    s = FakeSession(pc=0x100)
    cap = MtbCapture(s, IMG, 0xF0000000, 0x1FFFF400, 256, autohalt=True)
    flow = s.mem[0xF0000000 + mtb.FLOW]
    assert flow == (0x400 + 256 - 32) | mtb.FLOW_AUTOHALT and cap.state.autohalt
    assert cap.state.watermark == 0x400 + 224
    s.hw_write([P(0x104, 0x100, s=True)])
    s.pc = 0x102
    cap.collect()
    assert not cap.halted and cap.autohalts == 0
    loop = [P(0x104, 0x100)] * 28                       # 28 paquets : seuil (224 o) atteint
    s.hw_write(loop)
    assert s.mem[0xF0000000 + mtb.MASTER] & mtb.MASTER_HALTREQ
    s.pc = 0x100
    ev = cap.collect()
    assert cap.halted and cap.autohalts == 1 and cap.wraps == 0
    assert len(steps(ev)) == 2 + 3 * 27                  # reprise en 0x102, puis 27 tours
    master = s.mem[0xF0000000 + mtb.MASTER]
    assert not master & mtb.MASTER_HALTREQ and master & mtb.MASTER_EN


def test_encode_resume_and_end_pc():
    """Encodeur (simulation) : exception prise à la reprise avant toute instruction (source =
    PC de reprise) ; branchement pris juste avant l'arrêt (paquet déjà écrit)."""
    pk = mtb.encode([ExcEntry(15, 0x200), Step(0x200), Step(0x202), ExcReturn()], IMG,
                    start=False, resume_pc=0x102, end_pc=0x102)
    assert pk == [P(0x102, 0x200, a=True), P(0x202, mtb.EXC_RETURN), P(mtb.EXC_RETURN, 0x102, a=True)]
    pk = mtb.encode([Step(0x100), Step(0x102), Step(0x104)], IMG, start=False, end_pc=0x100)
    assert pk == [P(0x104, 0x100)]                       # bne pris, arrêt sur sa cible


@needs_qemu
def test_autohalt_chunks_equal_one_block(m0_path):
    """Tampons successifs vidés à chaque arrêt AUTOHALT (reprise au PC de l'arrêt précédent) :
    même suite d'instructions que le décodage du flux d'un seul bloc."""
    from gdbtrace.trace.mtb_capture import MtbCapture
    img, events = m0_path
    packets = mtb.encode(events, img)
    whole = steps(mtb.MtbDecoder(img).decode(packets))
    s = FakeSession(pc=packets[0].src)
    cap = MtbCapture(s, img, 0xF0000000, 0x1FFFF000, 1024, autohalt=True)
    per_halt = cap.state.watermark // 8                  # paquets avant le seuil
    got, i = [], 0
    while i < len(packets):
        j = min(i + per_halt, len(packets))
        while j < len(packets) and mtb.is_exc_return(packets[j - 1].dst):
            j += 1                                       # pas de coupure au milieu d'un retour
        s.hw_write(packets[i:j])
        s.pc = packets[j - 1].dst                        # le cœur s'arrête sur la destination
        got += steps(cap.collect())
        i = j
    assert cap.autohalts >= len(packets) // per_halt - 1 and cap.wraps == 0
    assert cap.decoder.stats.errors == 0 and got == whole
