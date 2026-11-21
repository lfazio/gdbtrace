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
                  ql.Step(0x102)]                     # 0x104 non confirmée (fin du journal)


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
@pytest.fixture(scope="module")
def qemu_paths(tmp_path_factory, elf, rtos_elf):
    if not shutil.which("qemu-system-arm"):
        pytest.skip("QEMU absent")
    out = {}
    for name, path, secs in (("bare", elf, 0.4), ("rtos", rtos_elf, 1.0)):
        log = str(tmp_path_factory.mktemp("qemu") / f"{name}.log")
        ql.record(path, secs, log)
        out[name] = (CodeImage(path), ql.parse_file(log))
    return out


def as_tuples(events):
    out = []
    for e in events:
        if isinstance(e, ql.Step):
            out.append(("step", e.addr))
        elif isinstance(e, ql.ExcEntry):
            out.append(("exc", e.number))
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
