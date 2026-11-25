"""ETMv4 cycle-accurate et horodatage : paquets Cycle Count et Timestamp, marques dans le flot,
durées ramenées à la mesure.

Même méthode que le reste de l'ETM : le chemin réel de QEMU, annoté par le modèle de cycles,
est encodé avec un paquet Cycle Count par groupe d'éléments P0 ; notre décodeur doit placer
chaque marque juste après les événements qu'elle couvre (la marque vaut la somme de leurs
cycles), et OpenCSD doit compter les mêmes instructions entre les mêmes comptages.
"""
import pytest
from conftest import needs_qemu

import opencsd
from gdbtrace.trace.etm import qemu_log as ql
from gdbtrace.trace.etm.cycles import annotate
from gdbtrace.trace.etm.encoder4 import Etm4Encoder
from gdbtrace.trace.etm.etm4 import CycleCount, Etm4Decoder, Timestamp
from gdbtrace.trace.etm.flow4 import decode
from gdbtrace.trace.timing import TimingAnalyzer, measured_cycles
from gdbtrace.trace.tpiu import frame_bytes

pytest.importorskip("capstone")

M55_IDR0 = 0x280006E1          # TRCCCI, COMMOPT = 1 : paquets de cycles sans commit


def segments(events):
    """[(instructions, cycles estimés, cycles de la marque)] de chaque segment clos."""
    out, n, est = [], 0, 0
    for e in events:
        if isinstance(e, ql.CycleMark):
            out.append((n, est, e.cycles))
            n = est = 0
        elif isinstance(e, ql.Step):
            n += 1
            est += e.cycles
        elif not isinstance(e, ql.MARKS):
            est += e.cycles
    return out


@needs_qemu
@pytest.mark.parametrize("name", ["bare", "rtos"])
def test_cycle_counts_close_the_segments_they_measure(qemu_paths, name):
    img, events = qemu_paths[name]
    path = annotate(events[:200_000], img)
    data = Etm4Encoder(img, cycle_accurate=True).encode(path)
    decoded, pk, fl = decode(data, img, commit_opt1=True)
    assert pk.errors == 0 and fl.stats.errors == 0 and fl.stats.lost_atoms == 0
    plain = [e for e in decoded if not isinstance(e, ql.MARKS)]
    assert [(type(e), getattr(e, "addr", None)) for e in plain] == \
        [(type(e), getattr(e, "addr", None)) for e in path[:len(plain)]]
    segs = segments(decoded)
    assert len(segs) > 1000 and fl.stats.cycle_counts == len(segs)
    # chaque marque = les cycles du chemin pour les événements de son segment (ceux du chemin :
    # le modèle du décodeur suit l'atome réel, l'annotation devine l'issue d'un branchement
    # interrompu par la tâche suivante, et peut différer)
    j, acc = 0, 0
    for e in decoded:
        if isinstance(e, ql.CycleMark):
            assert e.cycles == acc, j
            acc = 0
        elif not isinstance(e, ql.MARKS):
            acc += path[j].cycles
            j += 1
    assert fl.stats.measured == sum(mark for *_x, mark in segs)
    assert "mesuré" in fl.timing


@needs_qemu
@pytest.mark.parametrize("name", ["bare", "rtos"])
def test_opencsd_counts_the_same_cycles(qemu_paths, name, tmp_path):
    if opencsd.lister() is None:
        pytest.skip("OpenCSD absent : tools/build_opencsd.sh")
    img, events = qemu_paths[name]
    path = annotate(events[:100_000], img)
    data = Etm4Encoder(img, cycle_accurate=True).encode(path)
    opencsd.make_snapshot_etm4(str(tmp_path / "snap"), img, frame_bytes(2, data), configr=0x11,
                               idr={"TRCIDR0(0x078)": M55_IDR0})
    ref, errors = opencsd.cycle_segments(str(tmp_path / "snap"))
    assert errors == []
    ours = [(n, mark) for n, _est, mark in segments(decode(data, img, commit_opt1=True)[0])]
    assert len(ref) > 500
    assert ours[:len(ref)] == ref                               # OpenCSD : un élément en attente


def test_measured_cycles_rescale_each_segment():
    s = [ql.Step(0x100, cycles=1), ql.Step(0x102, cycles=1), ql.Step(0x104, cycles=2)]
    events = [ql.Step(0x0F0, cycles=5), ql.CycleMark(99)] + s + [ql.CycleMark(12), ql.TimeMark(7),
                                                                ql.Step(0x106, cycles=3)]
    out = measured_cycles(events)
    assert [e.cycles for e in out] == [5, 3, 3, 6, 3]           # 1er : estimé ; puis ×3 ; fin : estimé
    assert not any(isinstance(e, ql.MARKS) for e in out)
    gap = [ql.CycleMark(1), ql.Step(0x100, cycles=2), ql.TraceGap(), ql.Step(0x200, cycles=2),
           ql.CycleMark(1000)]
    out = measured_cycles(gap)
    assert [e.cycles for e in out] == [2, 996, 2]              # le temps non tracé va au trou
    assert measured_cycles(s) == s                              # sans marque : inchangé


@needs_qemu
def test_timing_uses_the_measured_cycles(qemu_paths):
    """Durées mesurées : l'ETM compte 3 fois les cycles du modèle → durées triplées, mêmes
    appels et même répartition (premier segment mis à part)."""
    img, events = qemu_paths["bare"]
    path = annotate(events[:60_000], img)
    data = Etm4Encoder(img, cycle_accurate=True).encode(path)
    decoded = decode(data, img, commit_opt1=True)[0]
    tripled = [ql.CycleMark(e.cycles * 3) if isinstance(e, ql.CycleMark) else e for e in decoded]
    from gdbtrace.core.symbols import SymbolTable
    sym = SymbolTable(img.path)
    a = TimingAnalyzer(img, sym).analyze(decoded)
    b = TimingAnalyzer(img, sym).analyze(tripled)
    first = next(i for i, e in enumerate(decoded) if isinstance(e, ql.CycleMark))
    head = sum(e.cycles for e in decoded[:first] if not isinstance(e, ql.MARKS))
    assert b.total - head == 3 * (a.total - head)
    assert {n: s.calls for n, s in a.stats.items()} == {n: s.calls for n, s in b.stats.items()}


def test_cycle_count_and_timestamp_packets_reach_the_flow():
    from gdbtrace.trace.etm.flow4 import Etm4Flow
    from gdbtrace.trace.etm.image import CodeImage
    img = CodeImage.from_segments([(0, b"\x00\xbf" * 8)])
    from gdbtrace.trace.etm.etm4 import ASYNC
    pk = Etm4Decoder(max_spec=0, commit_opt1=True)
    packets = pk.feed(ASYNC + bytes([0x01, 0x08, 0x02,          # Trace Info : seuil 2
                                     0x0E, 0x03, 0x02, 0x81, 0x01]))  # CC = 3 + 2 ; horodatage
    packets = [p for p in packets if isinstance(p, (CycleCount, Timestamp))]
    assert isinstance(packets[0], CycleCount) and isinstance(packets[1], Timestamp)
    fl = Etm4Flow(img, max_spec=0)
    out = fl.feed(packets) + fl.flush()
    assert out == [ql.CycleMark(5), ql.TimeMark(0x81)] and fl.stats.timestamps == 1


def test_etm4_configuration_for_cycles_and_timestamps():
    from gdbtrace.trace.etm import config as c
    cfg = c.EtmConfig(version=4, port="tmc", cycle_accurate=True, timestamps=True, cc_threshold=8)
    w = {r.name: r.value for r in cfg.register_writes()}
    assert w["TRCCONFIGR"] == c.TRCCONFIGR_RES1 | c.TRCCONFIGR_CCI | c.TRCCONFIGR_TS
    assert w["TRCCCCTLR"] == 8 and w["TRCTSCTLR"] == 0
    assert "cycle-accurate (seuil 8)" in cfg.summary() and "horodatage" in cfg.summary()

    class Etm:                              # registres lus par apply()
        def __init__(self, idr0, ccitmin=4):
            self.regs = {c.ETMIDR: 0x4100F454, c.TRCIDR0: idr0, c.TRCIDR3: ccitmin, c.TRCSTATR: 1}

        def read_u32(self, a):
            return self.regs.get(a, 0)

        def write_u32(self, a, v):
            self.regs[a] = v
    t = Etm(0x280006E1, ccitmin=12)          # M55 : cycles et horodatage 64 bits
    c.EtmConfig(port="tmc", cycle_accurate=True, timestamps=True).apply(t)
    assert t.regs[c.TRCCCCTLR] == 12          # seuil relevé au minimum de l'ETM
    with pytest.raises(ValueError, match="TRCCCI"):
        c.EtmConfig(port="tmc", cycle_accurate=True).apply(Etm(0x00000661))
    with pytest.raises(ValueError, match="TSSIZE"):
        c.EtmConfig(port="tmc", timestamps=True).apply(Etm(0x000006E1))
