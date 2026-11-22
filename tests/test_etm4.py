"""Trace ETMv4 (profil M, ETM-M7) : vérité terrain QEMU, encodeur, deux décodeurs.

Même méthode que pour l'ETMv3 : QEMU donne le chemin réel, l'encodeur produit le flux
qu'émettrait un ETM-M7, notre décodeur ET OpenCSD doivent retrouver ce chemin. Les
registres d'identification de l'oracle sont ceux d'un vrai ETM-M7 (STM32F746).
"""
import random

import pytest
from conftest import needs_qemu
from test_etm import as_tuples

import opencsd
from gdbtrace.trace.etm import qemu_log as ql
from gdbtrace.trace.etm.encoder4 import Etm4Encoder, atom_packets, encode
from gdbtrace.trace.etm.etm4 import (ASYNC, Address, ASync, Atoms, Cancel, Commit, Etm4Decoder,
                                     ExceptionPacket, ExceptionReturn, Mispredict, TraceInfo,
                                     atoms_of, exception_number, exception_type)
from gdbtrace.trace.etm.flow4 import Etm4Flow, decode
from gdbtrace.trace.etm.image import DIRECT, INDIRECT, NORMAL, Instr
from gdbtrace.trace.tpiu import frame_bytes

pytest.importorskip("capstone")


# ====================================================================== paquets
def test_atom_formats_like_opencsd():
    assert atoms_of(0xF7) == "E" and atoms_of(0xF6) == "N"                  # format 1
    assert atoms_of(0xD9) == "EN" and atoms_of(0xDA) == "NE"                # format 2, bit 0 = ancien
    assert atoms_of(0xFB) == "EEN" and atoms_of(0xF8) == "NNN"              # format 3
    assert [atoms_of(h) for h in range(0xDC, 0xE0)] == ["NEEE", "NNNN", "NENE", "ENEN"]
    assert atoms_of(0xF5) == "NEEEE" and atoms_of(0xD5) == "NNNNN"          # format 5
    assert atoms_of(0xC0) == "EEEE" and atoms_of(0xE0) == "EEEN"            # format 6
    assert atoms_of(0xD4) == "E" * 24 and atoms_of(0xF4) == "E" * 23 + "N"


def test_atom_packets_roundtrip():
    rnd = random.Random(4)
    for _ in range(300):
        atoms = "".join(rnd.choice("EEEEN") for _ in range(rnd.randrange(1, 120)))
        got = "".join(atoms_of(b) for b in atom_packets(atoms))
        assert got == atoms


def test_exception_type_mapping():
    assert [exception_type(n) for n in (3, 11, 14, 15, 16, 23)] == [3, 11, 14, 15, 0x10, 0x17]
    assert exception_type(24) == 0x208 and exception_type(55) == 0x227       # IRQ8, IRQ39
    for n in range(1, 496):
        assert exception_number(exception_type(n)) == n
    assert exception_number(0x18) is None and exception_number(0x19) is None   # Debug halt, Lazy FP


def test_packets_and_address_compression():
    data = (ASYNC + b"\x01\x00" + b"\x83\x40\x12\x00\x08\x00"   # 0x08001280, avec contexte
            + b"\x96\x10"                                       # courte : bits [7:1]
            + b"\x96\x81\x34"                                   # courte : bits [15:1]
            + b"\x91"                                           # correspondance exacte n° 1
            + b"\x06\xB1\x10"                                   # exception, TYPE 0x218 (IRQ24)
            + b"\x07\x2D\x03\x0F\x01")                          # retour, Commit 3, cycles F1
    d = Etm4Decoder()
    pk = [p for p in d.feed(data)]
    assert isinstance(pk[0], ASync) and pk[0].resync and isinstance(pk[1], TraceInfo)
    addrs = [p.addr for p in pk if isinstance(p, Address)]
    assert addrs == [0x08001280, 0x08001220, 0x08003402, 0x08001220]
    exc = next(p for p in pk if isinstance(p, ExceptionPacket))
    assert exc.number == 40 and exc.addr_interp == 1
    assert d.errors == 0
    # découpé octet par octet : mêmes paquets
    d2, pk2 = Etm4Decoder(), []
    for b in data:
        pk2 += d2.feed(bytes([b]))
    assert pk2 == pk


def test_real_etm_m7_exception_bytes():
    """Octets relevés sur un STM32F746 : Commit 1, Exception SysTick (forme longue),
    adresse de retour par correspondance exacte, handler, Context (mode handler) ; puis
    retour : atomes, 0x07, adresse de reprise avec contexte (mode thread)."""
    d = Etm4Decoder()
    d.feed(ASYNC + b"\x01\x05\x00\x01" + b"\x83\x0a\x76\x00\x08\x00")       # 0x08007614
    pk = d.feed(b"\x2d\x01\x06\x9f\x00\x90\x96\xe4\x5a\x81\x03" + b"\xdb\x07\x83\x0a\x76\x00\x08\x00")
    kinds = [type(p).__name__ for p in pk]
    assert kinds == ["Commit", "ExceptionPacket", "Address", "Address", "Context",
                     "Atoms", "ExceptionReturn", "Address"]
    assert pk[1].number == 15 and pk[2].addr == 0x08007614 and pk[3].addr == 0x08005AC8
    assert pk[4].updated and pk[7].addr == 0x08007614 and pk[7].context and d.errors == 0


def test_speculative_elements_before_the_sync():
    """Trace Info SPEC=1 (vrai ETM-M7) : un élément émis avant la synchro est encore
    spéculatif. Le premier Commit, implicite ou non, le consomme sans rien valider de visible
    (OpenCSD 1.4.1 valide à sa place un élément réel, puis déborde au Commit suivant)."""
    fl = Etm4Flow(TinyImage(), max_spec=1)
    fl.feed([ASync(resync=True), TraceInfo(spec=1)])
    assert fl.unseen == 1 and fl.depth == 1
    out = fl.feed([Address(0x100, context=True), Atoms("E")])     # profondeur 2 : l'invisible part
    assert out == [] and fl.unseen == 0 and fl.depth == 1
    out = fl.feed([Commit(1)])                                     # valide l'atome visible
    assert as_tuples(out) == [("step", 0x100), ("step", 0x102)] and fl.depth == 0
    assert fl.stats.errors == 0 and fl.stats.lost_atoms == 0


def test_garbage_before_sync_and_reserved_header():
    d = Etm4Decoder()
    out = d.feed(b"\x12\x34\x56\x78" + ASYNC + b"\x01\x00\x04")
    assert d.dropped == 4 and isinstance(out[0], ASync) and out[0].resync
    out = d.feed(b"\x50" + ASYNC + b"\x01\x00")            # en-tête réservé : resynchro
    assert d.errors == 1 and isinstance(out[0], ASync) and out[0].resync


# ====================================================================== flot et spéculation
def _instr(addr, text, kind=NORMAL, target=None, cond=False, ret=False):
    return Instr(addr, 2, kind, target, cond, False, ret, text)


class TinyImage:
    """0x100 nop ; 0x102 bne 0x100 ; 0x104 nop ; 0x106 bx lr ;  0x200 (handler) bx lr"""
    code = {0x100: _instr(0x100, "nop"), 0x102: _instr(0x102, "bne #0x100", DIRECT, 0x100, cond=True),
            0x104: _instr(0x104, "nop"), 0x106: _instr(0x106, "bx lr", INDIRECT, ret=True),
            0x200: _instr(0x200, "bx lr", INDIRECT, ret=True)}

    def instr(self, addr):
        return self.code.get(addr & ~1)


def _flow(packets, max_spec=1):
    fl = Etm4Flow(TinyImage(), max_spec)
    events = fl.feed([ASync(resync=True), TraceInfo(), Address(0x100, context=True)] + packets)
    committed = len(events)
    return as_tuples(events + fl.flush()), committed, fl


def test_implicit_commit_at_max_speculation_depth():
    got, committed, _ = _flow([Atoms("EEN")])
    # profondeur 3 > MAXSPEC 1 : les deux premiers atomes sont validés tout de suite
    assert got[:committed] == [("step", 0x100), ("step", 0x102)] * 2
    assert got[committed:] == [("step", 0x100), ("step", 0x102)]
    got0, committed0, _ = _flow([Atoms("EEN")], max_spec=0)                  # sans spéculation
    assert committed0 == len(got0) == 6


def test_cancel_and_mispredict():
    got, _, _ = _flow([Atoms("E"), Atoms("E"), Cancel(1)])
    assert got == [("step", 0x100), ("step", 0x102)]                         # 2e atome annulé
    got, _, fl = _flow([Atoms("E"), Mispredict()])
    assert got == [("step", 0x100), ("step", 0x102)] and fl.addr == 0x104   # E devenu N


def test_exception_return_address_bounds_the_interrupted_range():
    got, _, fl = _flow([Atoms("N"), ExceptionPacket(exception_type(40)), Address(0x106),
                        Address(0x200), Atoms("E"), ExceptionReturn(), Address(0x106)])
    assert got == [("step", 0x100), ("step", 0x102), ("step", 0x104), ("exc", 40),
                   ("step", 0x200), ("ret",)]
    assert fl.stats.errors == 0 and fl.addr == 0x106


def test_encoder_roundtrip_on_synthetic_path():
    events = [ql.Step(0x100), ql.Step(0x102), ql.Step(0x104), ql.ExcEntry(40, 0x200, ret=0x106),
              ql.Step(0x200), ql.ExcReturn(), ql.Step(0x106)]
    data = encode(events, TinyImage())
    decoded, pk, fl = decode(data, TinyImage())
    assert as_tuples(decoded) == as_tuples(events) and pk.errors == 0 and fl.stats.errors == 0


# ====================================================================== chemins réels
def _check_prefix(got, events, image, uncommitted=0):
    """Le décodage est un préfixe exact du chemin. Seules peuvent manquer les instructions
    qui suivent le dernier point de passage (aucun atome ne les couvre), et les
    `uncommitted` derniers éléments P0 qu'un décodeur garde spéculatifs en fin de trace."""
    ref = as_tuples(events)
    assert got == ref[:len(got)]
    tail = ref[len(got):]
    p0 = [t for t in tail if t[0] != "step" or image.instr(t[1]).waypoint]
    assert len(p0) <= uncommitted, tail[:8]
    assert len(tail) < 64


@needs_qemu
@pytest.mark.parametrize("name", ["bare", "rtos"])
def test_roundtrip_matches_qemu(qemu_paths, name):
    img, events = qemu_paths[name]
    if name == "rtos":   # le firmware FreeRTOS exerce SVC, PendSV, SysTick et l'enchaînement
        nums = {e.number for e in events if isinstance(e, ql.ExcEntry)}
        assert {11, 14, 15} <= nums and any(e.tailchain for e in events if isinstance(e, ql.ExcEntry))
    enc = Etm4Encoder(img)
    data = enc.encode(events)
    assert enc.consumed == len(events)
    decoded, pk, fl = decode(data, img)
    assert pk.errors == 0 and fl.stats.errors == 0 and fl.stats.lost_atoms == 0
    assert fl.stats.unknown_code == 0
    _check_prefix(as_tuples(decoded), events, img)
    assert len(data) * 8 / fl.stats.instructions < 0.8      # séquences du vrai ETM-M7 comprises


@needs_qemu
@pytest.mark.parametrize("name", ["bare", "rtos"])
def test_opencsd_reference_decoder_agrees(qemu_paths, name, tmp_path):
    if opencsd.lister() is None:
        pytest.skip("OpenCSD absent : tools/build_opencsd.sh")
    img, events = qemu_paths[name]
    data = encode(events, img)
    opencsd.make_snapshot_etm4(str(tmp_path / "snap"), img, frame_bytes(2, data))
    ocsd_events, errors = opencsd.decode(str(tmp_path / "snap"), img, etm4=True)
    assert errors == []
    _check_prefix(ocsd_events, events, img, uncommitted=1)    # OpenCSD : MAXSPEC = 1


@needs_qemu
def test_resynchronisation_mid_stream(qemu_paths):
    img, events = qemu_paths["rtos"]
    events = events[:200_000]
    data = encode(events, img, sync_period=512)
    full = as_tuples(decode(data, img)[0])
    assert full == as_tuples(events)[:len(full)]
    for cut in (len(data) // 3 + 5, len(data) // 2 + 7, 2 * len(data) // 3 + 3):  # en plein paquet
        decoded, pk, fl = decode(data[cut:], img)
        tail = as_tuples(decoded)
        assert pk.dropped > 0 and len(tail) > 1000 and fl.stats.errors == 0
        # repris à une synchro, il va jusqu'à la fin du flux : suffixe exact du décodage complet
        # (comparer à un tronçon du chemin le ferait coïncider avec un tour de boucle antérieur)
        assert full[len(full) - len(tail):] == tail


# ====================================================================== intégration
def test_detect_etm_version():
    """TRCIDR1 d'un vrai ETM d'abord ; sans ETM lisible (QEMU), le cœur décide (CPUID)."""
    from gdbtrace.trace.etm.config import ETMIDR
    from gdbtrace.trace.manager import CPUID, TraceManager

    class S:
        def __init__(self, regs):
            self.regs = regs

        def read_u32(self, a):
            if a not in self.regs:
                raise RuntimeError("lecture impossible")
            return self.regs[a]
    tm = TraceManager()
    assert tm.detect_etm(S({ETMIDR: 0x4100F400})) == 4                 # ETM-M7
    assert tm.detect_etm(S({ETMIDR: 0x4114F250})) == 3                 # ETM-M4 (ETMv3.5)
    assert tm.detect_etm(S({CPUID: 0x411FC272})) == 4                  # QEMU mps2-an500 (M7)
    assert tm.detect_etm(S({CPUID: 0x410FC231})) == 3                  # QEMU mps2-an385 (M3)


def test_etm_trace_v4_flush_shows_the_last_branch():
    from gdbtrace.trace.etm.trace import EtmTrace
    events = [ql.Step(0x100), ql.Step(0x102), ql.Step(0x104), ql.Step(0x106)]
    t = EtmTrace(TinyImage(), version=4)
    t.feed(encode(events, TinyImage()))
    assert [e.addr for e in t.last(10)] == [0x100, 0x102]             # atome du bx lr spéculatif
    assert t.flush() == 2 and [e.addr for e in t.last(10)] == [0x100, 0x102, 0x104, 0x106]
    assert t.stats["erreurs"] == 0 and t.flush() == 0


@needs_qemu
def test_simulated_etmv4_on_qemu_cortex_m7(elf, tmp_path):
    """--machine mps2-an500 (Cortex-M7) --etm-sim : ETMv4 détecté et décodé jusqu'à l'arrêt."""
    import subprocess
    import sys
    from conftest import ROOT
    script = tmp_path / "s.py"
    script.write_text("g = gdbtrace\nprint('ETMv%d' % g.trace.etm.version)\n"
                      "g.break_('fib'); g.go(); g.wait(); g.trace.wait_etm()\n"
                      "st = g.trace.etm.stats\nprint('instructions', st['instructions'], 'erreurs', st['erreurs'])\n"
                      "print('dernière', g.trace.etm.last(1)[0].addr)\n")
    r = subprocess.run([sys.executable, "-m", "gdbtrace", elf, "--machine", "mps2-an500",
                        "--etm-sim", "--batch", "--script", str(script)],
                       cwd=ROOT, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr
    out = r.stdout.split()
    assert "ETMv4" in out and out[out.index("erreurs") + 1] == "0"
    assert int(out[out.index("instructions") + 1]) > 100
    img = __import__("gdbtrace.trace.etm.image", fromlist=["CodeImage"]).CodeImage(elf)
    assert img.instr(int(out[out.index("dernière") + 1])).call                # le bl vers fib
