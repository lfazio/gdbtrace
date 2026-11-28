"""Trace ETM (ETMv3, Cortex-M) : vérité terrain QEMU, encodeur, deux décodeurs indépendants.

1. QEMU exécute le firmware et journalise chaque instruction (chemin réel) ;
2. l'encodeur produit le flux ETMv3 qu'émettrait un ETM-M ;
3. notre décodeur ET OpenCSD (référence ARM) doivent retrouver exactement le chemin.
"""
import os
import random
import shutil

import pytest
from conftest import needs_qemu

import opencsd
from gdbtrace.trace.etm import qemu_log as ql
from gdbtrace.trace.etm.encoder import branch_packet, encode, isync_packet
from gdbtrace.trace.etm.etm3 import ASync, Atoms, Branch, Etm3Decoder, ExceptionExit, ISync
from gdbtrace.trace.etm.flow import decode
from gdbtrace.trace.etm.image import DIRECT, INDIRECT, NORMAL, CodeImage
from gdbtrace.trace.tpiu import frame_bytes

needs_capstone = pytest.mark.skipif(pytest.importorskip("capstone") is None, reason="capstone")


# ====================================================================== journal QEMU
LOG = """\
Trace 0: 0x7f00 [00800400/0000000000000100/00000110/ff020201] f
Trace 0: 0x7f01 [00800400/0000000000000102/00000110/ff020201] f
Stopped execution of TB chain before 0x7f01 [0000000000000102] f
Taking exception 5 [IRQ] on CPU 0
...taking pending nonsecure exception 15
...loaded new PC 0x295
Trace 0: 0x7f02 [00800401/0000000000000294/00000110/ff020201] h
Trace 0: 0x7f03 [00800401/0000000000000296/00000110/ff020201] h
Taking exception 8 [QEMU v7M exception exit] on CPU 0
Exception return: magic PC fffffff9 previous exception 15
...tailchaining to pending exception
...taking pending nonsecure exception 14
...loaded new PC 0x301
Trace 0: 0x7f04 [00800401/0000000000000300/00000110/ff020201] p
Taking exception 8 [QEMU v7M exception exit] on CPU 0
Exception return: magic PC fffffff9 previous exception 14
...successful exception return
Trace 0: 0x7f05 [00800400/0000000000000102/00000110/ff020201] f
Trace 0: 0x7f06 [00800400/0000000000000104/00000110/ff020201] f
"""


def test_qemu_log_parser():
    ev = list(ql.parse(LOG.splitlines()))
    assert ev == [ql.Step(0x100),                     # 0x102 annoncée mais NON exécutée
                  ql.ExcEntry(15, 0x294), ql.Step(0x294), ql.Step(0x296),
                  ql.ExcEntry(14, 0x300, tailchain=True),   # retour annulé par l'enchaînement
                  ql.Step(0x300), ql.ExcReturn(),
                  ql.Step(0x102),
                  ql.Step(0x104)]                     # fin du journal sans « Stopped » : exécutée


# ====================================================================== image du code
def test_instruction_classification(elf):
    from gdbtrace.core.symbols import SymbolTable
    img, sym = CodeImage(elf), SymbolTable(elf)
    fib = sym.function("fib")
    kinds = {}
    addr = fib.start
    while addr < fib.end:
        ins = img.instr(addr)
        kinds.setdefault(ins.text.split()[0], ins)
        addr += ins.size
    assert kinds["bx"].kind == INDIRECT and kinds["bx"].ret            # pop/bx lr en fin
    branch = next(i for i in kinds.values() if i.kind == DIRECT)
    assert fib.start <= branch.target < fib.end                         # boucle for
    assert kinds["adds"].kind == NORMAL
    main = sym.function("main")
    calls = [img.instr(a) for a in range(main.start, main.end, 2) if img.instr(a) and img.instr(a).call]
    assert all(c.kind == DIRECT for c in calls)
    assert {sym.function_at(c.target).name for c in calls} == \
        {"peripherals_init", "update_sensor", "animate", "delay"}
    assert img.instr(0x10000000) is None                                # hors image


# ====================================================================== paquets
def test_branch_packet_compression_and_decode():
    rnd = random.Random(3)
    last = 0x00000100
    for _ in range(2000):
        addr = rnd.choice([last + rnd.randrange(-200, 200) * 2, rnd.randrange(0, 1 << 31) & ~1,
                           0x20000000 | rnd.randrange(1 << 20) & ~1]) & 0xFFFFFFFE
        exc = rnd.choice([None, None, None, 15, 14, 3, 16 + rnd.randrange(240)])
        pkt = branch_packet(addr, last, exc)
        d = Etm3Decoder()
        d.synced, d.last_addr = True, last
        out = d.feed(pkt)
        assert len(out) == 1 and not d.buf, (hex(addr), hex(last), pkt.hex())
        assert out[0].addr == addr and out[0].exception == exc
        if exc is None and (addr ^ last) >> 7 == 0 and not (addr >> 1) & 0x20:
            assert len(pkt) == 1                                        # compression maximale
        last = addr


def test_packets_split_across_reads():
    data = (b"\x00" * 5 + b"\x80" + isync_packet(0x1234) + bytes([0xBC, 0x86, 0xC4])
            + branch_packet(0x08001000, 0x1234, 15) + b"\x76" + branch_packet(0x1238, 0x08001000)
            + b"\x00" * 5 + b"\x80")
    whole = Etm3Decoder().feed(data)
    # la 1re A-sync sert à la synchronisation initiale ; les suivantes sont des paquets
    assert [type(p) for p in whole] == [ISync, Atoms, Atoms, Atoms, Branch, ExceptionExit, Branch, ASync]
    assert whole[1].atoms == "E" * 15                  # 0xBC : format 1, 15 × E
    assert whole[2].atoms == "EN"                      # 0x86 : format 2, E puis N
    assert whole[3].atoms == "EN"                      # 0xC4 : format 1, 1 × E puis N
    assert whole[0].addr == 0x1234 and whole[4].exception == 15 and whole[6].addr == 0x1238
    for chunk in (1, 2, 3, 5):
        d, got = Etm3Decoder(), []
        for i in range(0, len(data), chunk):
            got += d.feed(data[i:i + chunk])
        assert got == whole


def test_garbage_before_sync_is_dropped():
    d = Etm3Decoder()
    out = d.feed(b"\x12\x00\x34\xff" + b"\x00" * 7 + b"\x80" + isync_packet(0x100))
    assert [type(p) for p in out] == [ISync] and d.dropped == 4


# ====================================================================== chemins réels
def as_tuples(events):
    out = []
    for e in events:
        if isinstance(e, ql.Step):
            out.append(("step", e.addr))
        elif isinstance(e, ql.ExcEntry):
            out.append(("exc", e.number))
        elif isinstance(e, ql.TraceGap):
            out.append(("gap",))
        else:
            out.append(("ret",))
    return out


@needs_qemu
@pytest.mark.parametrize("name", ["bare", "rtos"])
def test_roundtrip_matches_qemu(qemu_paths, name):
    img, events = qemu_paths[name]
    assert len(events) > 100_000
    if name == "rtos":   # le firmware FreeRTOS exerce SVC, PendSV et l'enchaînement
        nums = {e.number for e in events if isinstance(e, ql.ExcEntry)}
        assert {11, 14, 15} <= nums and any(e.tailchain for e in events if isinstance(e, ql.ExcEntry))
    data = encode(events, img)
    decoded, pk, fl = decode(data, img)
    assert pk.errors == 0 and fl.stats.lost_atoms == 0 and fl.stats.unknown_code == 0
    assert as_tuples(decoded) == as_tuples(events)
    assert len(data) * 8 / fl.stats.instructions < 2.0                 # < 2 bits / instruction


@needs_qemu
@pytest.mark.parametrize("name", ["bare", "rtos"])
def test_opencsd_reference_decoder_agrees(qemu_paths, name, tmp_path):
    if opencsd.lister() is None:
        pytest.skip("OpenCSD absent : tools/build_opencsd.sh")
    img, events = qemu_paths[name]
    events = events[:150_000]
    data = encode(events, img)
    opencsd.make_snapshot(str(tmp_path / "snap"), img, frame_bytes(2, data))
    ocsd_events, errors = opencsd.decode(str(tmp_path / "snap"), img)
    assert errors == []
    ref = as_tuples(events)
    # OpenCSD ne sort la dernière instruction qu'une fois confirmée : on compare le préfixe
    assert ocsd_events == ref[:len(ocsd_events)] and len(ocsd_events) >= len(ref) - 2


@needs_qemu
def test_resynchronisation_mid_stream(qemu_paths):
    img, events = qemu_paths["bare"]
    events = events[:50_000]
    data = encode(events, img, sync_period=512)
    cut = len(data) // 2 + 7                                            # au milieu d'un paquet
    decoded, pk, _ = decode(data[cut:], img)
    assert pk.dropped > 0 and decoded
    ref = as_tuples(events)
    tail = as_tuples(decoded)
    assert ref[len(ref) - len(tail):] == tail                           # suffixe exact du chemin


# ====================================================================== capture et configuration
def test_etm_config_registers():
    from gdbtrace.trace.etm.config import (DBGMCU_CR, ETMCR, ETMTEEVR, ETMTECR1, ETMTRACEIDR,
                                           TPIU_CSPSR, TPIU_FFCR, TPIU_SPPR, EtmConfig)
    cfg = EtmConfig(port_width=4, trace_id=2, stm32=True)
    w = cfg.register_writes()
    names = [r.name for r in w]
    by = {r.name: r for r in w}
    assert by["TPIU_CSPSR"].value == 0x8 and by["TPIU_SPPR"].value == 0 and by["TPIU_FFCR"].value == 0x102
    assert by["ETMTRACEIDR"].value == 2 and by["ETMTEEVR"].value == 0x6F and by["ETMTECR1"].value == 1 << 24
    assert by["DBGMCU_CR.TRACE"].value == 0xE0 and by["DBGMCU_CR.TRACE"].mask == 0xE0   # IOEN + 4 bits
    # ETMCR : programmation (bit 10) avant la configuration, effacée en dernier
    assert names.index("ETMCR.prog") < names.index("ETMTRACEIDR") < names.index("ETMCR") == len(w) - 1
    assert by["ETMCR"].value == 0 and by["ETMCR.prog"].value == 1 << 10
    assert {r.addr for r in w} >= {ETMCR, ETMTEEVR, ETMTECR1, ETMTRACEIDR, TPIU_CSPSR, DBGMCU_CR}
    assert EtmConfig(port_width=1).register_writes()[1].value == 0x1        # CSPSR, sans STM32
    with pytest.raises(ValueError):
        EtmConfig(port_width=3).register_writes()


@needs_qemu
def test_tpiu_routing_itm_and_etm(qemu_paths):
    """Port parallèle : ITM (ID 1) et ETM (ID 2) multiplexés dans les mêmes trames TPIU."""
    from gdbtrace.trace.etm.trace import EtmTrace
    from gdbtrace.trace.itm import ItmTextSink
    from gdbtrace.trace.swo import TraceSession
    from test_itm import SYNC, stim
    img, events = qemu_paths["bare"]
    events = events[:20_000]
    etm_bytes = encode(events, img)
    itm_bytes = SYNC + b"".join(stim(0, c) for c in b"trace ok\n")
    half = len(etm_bytes) // 2
    raw = (frame_bytes(2, etm_bytes[:half]) + frame_bytes(1, itm_bytes)[4:]
           + frame_bytes(2, etm_bytes[half:])[4:])
    trace = EtmTrace(img)
    sess = TraceSession(formatter=True, etm_sink=trace.feed)
    text = ItmTextSink()
    sess.add_sink(lambda pkts: [text.add(p) for p in pkts])
    for i in range(0, len(raw), 64):
        sess.feed(raw[i:i + 64])
    assert text.text(0) == "trace ok"
    assert sess.stats["octets ETM"] == len(etm_bytes)
    trace.flush()                   # fin du flux : la dernière instruction, retenue (Cancel)
    assert as_tuples(trace.last(len(events))) == as_tuples(events)


@needs_qemu
def test_coverage_and_call_flow(qemu_paths, elf):
    from gdbtrace.core.symbols import SymbolTable
    from gdbtrace.trace.etm.trace import EtmTrace
    img, events = qemu_paths["bare"]
    sym = SymbolTable(elf)
    trace = EtmTrace(img, sym)
    trace.feed(encode(events, img))
    cov = {c.name: c for c in trace.coverage()}
    for name in ("fib", "delay", "main", "update_sensor", "peripherals_init"):
        assert cov[name].percent == 100, name                 # literal pools exclus
    assert cov["HardFault_Handler"].executed == 0
    assert "__addsf3 = __aeabi_fadd" in cov or "__aeabi_fadd = __addsf3" in cov   # alias fusionnés
    main_c = sym.line_at(sym.function("fib").start).file
    lines = trace.line_coverage(main_c)
    assert lines and all(lines.values())                      # toutes les lignes de main.c exécutées
    flow = trace.call_flow(50_000)          # > une itération de la boucle principale (~7000 instr.)
    kinds = {r.kind for r in flow}
    assert {"call", "return", "exception", "exc-return"} <= kinds
    calls = [r for r in flow if r.kind == "call" and r.func == "fib"]
    assert calls and all(r.depth >= 2 for r in calls)         # main → update_sensor → fib


@needs_qemu
def test_qemu_simulated_etm(elf):
    """ETM simulé : journal QEMU activé à chaque exécution, converti en trace à l'arrêt."""
    import time
    from gdbtrace.core.session import Session
    from gdbtrace.core.symbols import SymbolTable
    from gdbtrace.core.target import QemuTarget, free_port
    from gdbtrace.trace.manager import TraceManager
    t = QemuTarget(elf=elf, gdb_port=free_port(), etm_sim=True)
    s = Session()
    s.connect(elf, t)
    lm = t.live_memory()
    tm = TraceManager()
    try:
        assert tm.setup_etm(elf, SymbolTable(elf)) is None
        tm.enable_qemu_sim(lm)
        s.break_("animate")
        for _ in range(2):
            tm.on_target_running()
            s.go()
            s.wait_stopped(10)
            tm.on_target_stopped()
            time.sleep(0.1)
            while tm.etm_busy:
                time.sleep(0.05)
        last = [e for e in tm.etm.last(10) if isinstance(e, ql.Step)]
        # dernière instruction tracée : juste avant le point d'arrêt dans animate()
        sym = SymbolTable(elf)
        assert sym.function_at(last[-1].addr).name == "animate"
        assert last[-1].addr + img_size(tm, last[-1].addr) == s.frame().pc
        assert tm.etm.stats["erreurs"] == 0 and tm.etm_sim.runs == 2
    finally:
        tm.close()
        lm.close()
        s.disconnect()


def img_size(tm, addr):
    return tm.etm.image.instr(addr).size


# ====================================================================== cycles (analyse temporelle)
def _ins(code: bytes, addr: int = 0x1000):
    from gdbtrace.trace.etm.image import _classify
    img = CodeImage.__new__(CodeImage)
    import capstone
    cs = capstone.Cs(capstone.CS_ARCH_ARM, capstone.CS_MODE_THUMB | capstone.CS_MODE_MCLASS)
    cs.detail = True
    return _classify(next(cs.disasm(code, addr)))


def test_cycle_model():
    from gdbtrace.trace.etm.cycles import P, cycles_of
    assert cycles_of(_ins(b"\x80\xb5")) == 3                         # push {r7, lr} : 1 + 2
    assert cycles_of(_ins(b"\x80\xbd"), True, True) == 3 + P         # pop {r7, pc} : 1 + 2 + P
    assert cycles_of(_ins(b"\x3b\x68")) == 2                         # ldr r3, [r7]
    assert cycles_of(_ins(b"\x3b\x60")) == 1                         # str r3, [r7]
    assert cycles_of(_ins(b"\x01\x30")) == 1                         # adds r0, #1
    beq = _ins(b"\x00\xd0")                                          # beq
    assert cycles_of(beq, True, True) == 1 + P and cycles_of(beq, True, False) == 1
    assert cycles_of(beq, False) == 1                                # condition fausse
    assert cycles_of(_ins(b"\x90\xfb\xf1\xf0")) == 7                 # sdiv r0, r0, r1 (moyenne)
    assert cycles_of(_ins(b"\x70\x47"), True, True) == 1 + P         # bx lr


def test_cycle_accurate_pheaders():
    from gdbtrace.trace.etm.etm3 import Wait
    d = Etm3Decoder(cycle_accurate=True)
    d.synced = True
    out = d.feed(bytes([0xA8 | 0x40, 0xBC, 0xC0, 0x92, 0x96, 0x86, 0x8C, 0x04, 0x85, 0x01])
                 + b"\x70\x05" + isync_packet(0x2000)[1:])
    assert out[0] == Atoms("E", (3,))          # format 3 : 3 cycles puis E
    assert out[1] == Wait(8)                   # format 3 sans atome : 8 cycles
    assert out[2] == Atoms("N", (1,))          # format 1 CA : N
    assert out[3] == Atoms("E", (0,)) and out[4] == Atoms("N", (0,))   # format 4
    assert out[5] == Atoms("EN", (1, 0))       # format 2 : 1 cycle sur le premier atome
    assert out[6] == Atoms("EEE", (1, 1, 1))   # format 1 CA : E sur 3 bits
    assert out[7] == Wait(5 + (1 << 7))        # paquet de cycle count
    assert isinstance(out[8], ISync) and out[8].cycles == 5 and out[8].addr == 0x2000


def as_timed(events):
    out = []
    for e in events:
        if isinstance(e, ql.Step):
            out.append(("step", e.addr, e.cycles))
        elif isinstance(e, ql.ExcEntry):
            out.append(("exc", e.number, e.tailchain, e.cycles))
        else:
            out.append(("ret", e.cycles))
    return out


@needs_qemu
@pytest.mark.parametrize("name", ["bare", "rtos"])
def test_cycle_accurate_roundtrip(qemu_paths, name):
    from gdbtrace.trace.etm.cycles import annotate
    img, events = qemu_paths[name]
    timed = annotate(events[:200_000], img)
    data = encode(timed, img, cycle_accurate=True)
    decoded, pk, fl = decode(data, img, cycle_accurate=True)
    assert pk.errors == 0 and fl.timing.startswith("mesuré")
    assert as_timed(decoded) == as_timed(timed)                     # cycles et enchaînements
    assert fl.stats.cycles == sum(e.cycles for e in timed)
    # sans cycle-accurate : mêmes durées, estimées par le décodeur (enchaînements détectés)
    est, _, fl2 = decode(encode(timed, img), img)
    assert fl2.timing.startswith("estimé") and as_timed(est) == as_timed(timed)


@needs_qemu
@pytest.mark.parametrize("name", ["bare", "rtos"])
def test_opencsd_agrees_on_cycles(qemu_paths, name, tmp_path):
    if opencsd.lister() is None:
        pytest.skip("OpenCSD absent : tools/build_opencsd.sh")
    from gdbtrace.trace.etm.cycles import annotate
    img, events = qemu_paths[name]
    timed = annotate(events[:100_000], img)
    opencsd.make_snapshot(str(tmp_path / "snap"), img,
                          frame_bytes(2, encode(timed, img, cycle_accurate=True)), cycle_accurate=True)
    ocsd_events, errors = opencsd.decode(str(tmp_path / "snap"), img, with_cycles=True)
    assert errors == []
    ref = [t if t[0] != "exc" else ("exc", t[1], t[3]) for t in as_timed(timed)]
    assert ocsd_events == ref[:len(ocsd_events)] and len(ocsd_events) >= len(ref) - 2


def test_etm_config_cycle_accurate():
    from gdbtrace.trace.etm.config import EtmConfig
    w = {r.name: r for r in EtmConfig(cycle_accurate=True).register_writes()}
    assert w["ETMCR"].value == 1 << 12


def test_it_block_branch_is_conditional(elf):
    """`bxeq lr` s'encode comme `bx lr` : décodé seul, il paraît inconditionnel. L'image
    balaie la fonction d'un seul tenant et lui rend sa condition."""
    import capstone
    from capstone import arm as cs_arm
    from gdbtrace.trace.etm.cycles import cycles_of, outcome
    img = CodeImage(elf)
    found = []
    for start, end in img._funcs:
        addr = start
        while addr < end:
            ins = img.instr(addr)
            if ins is None:
                addr += 2
                continue
            if ins.op == "bx" and ins.cond:
                found.append(ins)
            addr += ins.size
    assert found, "la démo doit contenir un `bx<c> lr` dans un bloc IT (libgcc soft-float)"
    cs = capstone.Cs(capstone.CS_ARCH_ARM, capstone.CS_MODE_THUMB | capstone.CS_MODE_MCLASS)
    cs.detail = True
    for ins in found:
        alone = next(cs.disasm(img.read(ins.addr, 2), ins.addr, 1))
        assert alone.mnemonic == "bx" and alone.cc == cs_arm.ARM_CC_AL     # le piège
        assert ins.kind == INDIRECT and ins.ret and ins.text.startswith("bx") and ins.text != "bx lr"
        seq = ins.addr + ins.size
        assert outcome(ins, ins.addr, seq) == (False, False)               # non pris : atome N
        assert outcome(ins, ins.addr, 0x2000_0000) == (True, True)
        assert cycles_of(ins, executed=False) == 1


@needs_qemu
def test_simulated_etm_not_taken_it_branch(qemu_paths):
    """ETM simulé : un `bx<c> lr` non pris d'un bloc IT est encodé et décodé « non exécuté »."""
    from gdbtrace.trace.etm.flow import decode
    img, events = qemu_paths["bare"]
    steps = [e for e in events if isinstance(e, ql.Step)]
    # par passage, pas par adresse : un même `bx<c> lr` peut être pris à un passage et non
    # pris à un autre (selon le chemin de QEMU), et décodé « exécuté » à juste titre
    skipped = [k for k, (s, n) in enumerate(zip(steps, steps[1:]))
               if (i := img.instr(s.addr)).op == "bx" and i.cond and n.addr == s.addr + i.size]
    assert skipped, "le chemin doit traverser un `bx<c> lr` non pris"
    decoded, pk, _ = decode(encode(events, img), img)
    assert pk.errors == 0 and as_tuples(decoded) == as_tuples(events)
    dsteps = [e for e in decoded if isinstance(e, ql.Step)]
    assert {dsteps[k].executed for k in skipped if k < len(dsteps)} == {False}


def test_exception_cancels_the_last_traced_instruction(elf):
    """Adresse de branchement d'exception avec le bit Cancel : la dernière instruction tracée
    n'a pas été exécutée (interrompue, reprise au retour), OpenCSD la retire aussi (constaté
    sur une vraie trace de STM32F446 : un SysTick sur un `str`). Elle est retenue jusqu'au
    paquet suivant, même lu par un autre appel, et `flush()` l'émet en fin de flux."""
    from gdbtrace.core.symbols import SymbolTable
    from gdbtrace.trace.etm.flow import FlowDecoder
    img = CodeImage(elf)
    f = SymbolTable(elf).function("main")
    a = f.start
    while not (img.instr(a).kind == NORMAL and img.instr(a + img.instr(a).size).kind == NORMAL):
        a += img.instr(a).size
        assert a < f.end
    b = a + img.instr(a).size
    head = [ISync(addr=a, reason=0, thumb=True, cycles=0), Atoms(atoms="EE", cycles=())]
    exc = lambda cancel: Branch(addr=f.start, bits=32, exception=15, cancel=cancel)  # noqa: E731
    steps = lambda evs: [e.addr for e in evs if isinstance(e, ql.Step)]  # noqa: E731

    fl = FlowDecoder(img)
    evs = fl.feed(head + [exc(True)]) + fl.flush()
    assert steps(evs) == [a] and any(isinstance(e, ql.ExcEntry) for e in evs)
    assert fl.stats.instructions == 1 and fl.stats.cancelled == 1

    fl = FlowDecoder(img)
    assert steps(fl.feed(head + [exc(False)]) + fl.flush()) == [a, b]

    fl = FlowDecoder(img)                       # l'annulation arrive dans l'appel suivant
    assert steps(fl.feed(head)) == [a]
    assert steps(fl.feed([exc(True)])) == [] and fl.flush() == []

    fl = FlowDecoder(img)                       # fin du flux : la dernière émise par flush()
    assert steps(fl.feed(head)) == [a] and steps(fl.flush()) == [b]
