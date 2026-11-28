"""Trace sur disque (trace/record.py, --trace-record, --trace-file) : timing par morceaux égal
à l'analyse d'une liste ; enregistrement (flux brut, index) par le TraceManager ; relecture :
couverture, timing et nombre d'événements égaux au décodage en mémoire, pages de Trace.List
depuis les points de reprise (ETMv3 et ETMv4) ; flux ETR avec un trou ; capture relue de
grande taille à mémoire bornée (GDBTRACE_TRACE_MB Mo, défaut 0,1 : le chemin QEMU répété, très
compressible, fait ~20 millions d'événements par Mo, et le timing au fil du décodage coûte
~5 µs par événement en Python ; 300 Mo demandent plusieurs heures)."""
import json
import os
import time

import pytest
from conftest import ROOT, needs_qemu

from gdbtrace.trace import record
from gdbtrace.trace.etm import qemu_log as ql
from gdbtrace.trace.tpiu import frame_bytes

ELF = os.path.join(ROOT, "examples", "firmware", "demo.elf")


def _symbols(elf=ELF):
    from gdbtrace.core.symbols import SymbolTable
    return SymbolTable(elf)


def _encode(events, img, version):
    if version == 4:
        from gdbtrace.trace.etm.encoder4 import Etm4Encoder
        return Etm4Encoder(img).encode(events)
    from gdbtrace.trace.etm.encoder import encode
    return encode(events, img)


def _in_memory(img, sym, etm_bytes, version, gaps=()):
    """Décodage de référence : tout l'historique gardé, timing sur la liste entière."""
    from gdbtrace.trace.etm.trace import EtmTrace
    from gdbtrace.trace.timing import TimingAnalyzer
    etm = EtmTrace(img, sym, history=None, version=version)
    pos = 0
    for g in list(gaps) + [len(etm_bytes)]:
        etm.feed(etm_bytes[pos:g])
        if g < len(etm_bytes):
            etm.discontinuity()
        pos = g
    etm.flush()
    events = list(etm.history)
    return etm, events, TimingAnalyzer(img, sym).analyze(events)


def _same_timing(a, b):
    assert a.total == b.total and a.context_switches == b.context_switches
    assert {k: (s.calls, s.gross, s.net, s.min, s.max) for k, s in a.stats.items()} == \
        {k: (s.calls, s.gross, s.net, s.min, s.max) for k, s in b.stats.items()}
    assert {k: (s.calls, s.gross, s.net) for k, s in a.tree.items()} == \
        {k: (s.calls, s.gross, s.net) for k, s in b.tree.items()}


@needs_qemu
@pytest.mark.parametrize("name", ["bare", "rtos"])
def test_streaming_timing_equals_list_analysis(qemu_paths, name):
    """TimingAnalyzer.feed par morceaux de tailles quelconques (CycleMark comprises, segment
    à cheval sur deux morceaux) = analyze() de toute la liste ; positions absolues."""
    from gdbtrace.core.symbols import SymbolTable
    from gdbtrace.trace.timing import TimingAnalyzer
    img, events = qemu_paths[name]
    events = events[:120_000]
    sym = SymbolTable(img.path) if hasattr(img, "path") else None
    marked = []
    est = 0
    for i, e in enumerate(events):           # une mesure tous les 97 événements : 1,5 × l'estimation
        marked.append(e)
        est += getattr(e, "cycles", 0) or 0
        if i % 97 == 96:
            marked.append(ql.CycleMark(cycles=est * 3 // 2))
            est = 0
    for evs in (events, marked):
        ref = TimingAnalyzer(img, sym).analyze(evs)
        ta = TimingAnalyzer(img, sym)
        ta.start()
        k = 0
        for size in (1, 7, 1000, 33_333) * 50:
            ta.feed(evs[k:k + size])
            k += size
            if k >= len(evs):
                break
        mid = ta.snapshot()                      # l'analyse continue après un instantané
        assert mid.total <= ref.total
        res = ta.finish()
        _same_timing(res, ref)
        pos = [p for r in res.durations.values() for _v, _k, p in r.largest]
        assert all(p is None or 0 <= p < len(evs) for p in pos)   # positions absolues


def _rss() -> int:
    """Mémoire résidente du processus (octets)."""
    with open("/proc/self/status") as fh:
        return next(int(line.split()[1]) * 1024 for line in fh if line.startswith("VmRSS:"))


def _wait(pred, timeout=120):
    deadline = time.monotonic() + timeout
    while not pred():
        assert time.monotonic() < deadline, "délai dépassé"
        time.sleep(0.02)


def _manager(elf, version):
    from gdbtrace.trace.manager import TraceManager
    tm = TraceManager()
    assert tm.setup_etm(elf, _symbols(elf), version=version) is None
    return tm


@needs_qemu
@pytest.mark.parametrize("version", [3, 4])
def test_record_then_replay_equals_in_memory(qemu_paths, tmp_path, monkeypatch, version):
    """Une capture TPIU (ETM + ITM) rejouée en --swo-file est enregistrée telle quelle, avec
    son index ; relue, elle donne la couverture, le timing et les événements du décodage en
    mémoire, et chaque page de Trace.List (points de reprise tous les 20 000 événements) est
    celle de la liste complète."""
    from gdbtrace.trace.etm.listing import listing_rows
    from test_itm import SYNC, stim
    monkeypatch.setattr(record, "POINT_EVENTS", 20_000)
    img, events = qemu_paths["bare"]
    etm_bytes = _encode(events, img, version)
    itm = SYNC + b"".join(stim(0, c) for c in b"bonjour\n")
    half = len(etm_bytes) // 2
    src = tmp_path / "source.bin"
    src.write_bytes(frame_bytes(2, etm_bytes[:half]) + frame_bytes(1, itm)[4:]
                    + frame_bytes(2, etm_bytes[half:])[4:])
    rec = tmp_path / "capture.bin"

    tm = _manager(ELF, version)                          # enregistrement
    tm.set_record(str(rec), ELF)
    source = tm.start_file(str(src), formatter=True)
    _wait(source.done.is_set)
    tm.stop()
    tm.close()
    assert rec.read_bytes() == src.read_bytes()
    idx = record.read_index(str(rec))
    h = idx["header"]
    assert (h["source"], h["formateur"], h["etm"], h["elf"]) == ("tpiu", True, version, ELF)
    assert idx["end"][0] == src.stat().st_size and idx["times"]
    assert idx["syncs"] and idx["syncs"][0][1] == "tpiu"

    tm2 = _manager(ELF, version)                         # relecture
    r = tm2.replay_recording(str(rec))
    _wait(tm2.source.done.is_set)
    assert tm2.source.error is None
    etm = tm2.etm
    ref_etm, ref_events, ref_timing = _in_memory(etm.image, etm.symbols, etm_bytes, version)
    assert etm.events_total == len(ref_events) == r.events_total
    assert (etm.cov.e, etm.cov.n) == (ref_etm.cov.e, ref_etm.cov.n)
    _same_timing(etm.timing_result(), ref_timing)
    assert "bonjour" in tm2.text.text(0)
    pts = r.points()
    assert len(pts) >= 3 and [n for _o, n, _s in pts] == sorted(n for _o, n, _s in pts)
    ref_rows = listing_rows(ref_events, etm.image, etm.symbols)
    for start in (0, 1, 19_999, 20_000, 77_777, len(ref_events) - 50):
        rows = r.rows(start, 40)
        want = [x for x in ref_rows if start <= x.index + len(ref_events) < start + 40]
        assert [(x.kind, x.addr, x.text) for x in rows] == [(x.kind, x.addr, x.text) for x in want]
        assert [x.index for x in rows] == [x.index + len(ref_events) for x in want]
    tm2.close()


@needs_qemu
def test_etr_stream_with_a_gap(qemu_paths, tmp_path):
    """Flux ETM de l'ETR enregistré tel quel, avec un trou (tampon plein) : la relecture
    place la discontinuité au même octet que le décodage en mémoire."""
    from gdbtrace.trace.etm.trace import EtmTrace
    img, events = qemu_paths["bare"]
    data = _encode(events[:60_000], img, 4)
    cut = len(data) // 3
    path = tmp_path / "etr.bin"
    w = record.TraceRecorder(str(path), "etm", etm=4, elf=ELF)
    w.write(data[:cut])
    w.gap()
    w.write(data[cut:])
    w.close()
    assert record.read_index(str(path))["gaps"] == [cut]
    sym = _symbols()
    ref_etm, ref_events, ref_timing = _in_memory(img, sym, data, 4, gaps=[cut])
    r = record.RecordedTrace(str(path), img, sym)
    assert (r.kind, r.version, r.has_etm) == ("etm", 4, True)
    etm = EtmTrace(img, sym, history=10, version=4)
    etm.stream_timing_on()
    r.replay(etm)
    assert etm.events_total == len(ref_events)
    _same_timing(etm.timing_result(), ref_timing)
    assert sum(1 for e in r.events(0, len(ref_events)) if isinstance(e, ql.TraceGap)) == \
        sum(1 for e in ref_events if isinstance(e, ql.TraceGap))


@needs_qemu
def test_large_capture_replayed_with_bounded_memory(qemu_paths, tmp_path):
    """Capture ETMv4 de GDBTRACE_TRACE_MB Mo (le chemin QEMU répété) relue : la mémoire ne
    croît pas avec la taille du fichier (historique, timing et points bornés), le nombre
    d'événements est celui attendu, et une page du milieu se relit."""
    from gdbtrace.trace.etm.trace import EtmTrace
    mb = float(os.environ.get("GDBTRACE_TRACE_MB", "0.1"))
    img, events = qemu_paths["bare"]
    one = _encode(events, img, 4)
    sym = _symbols()
    e1 = _in_memory(img, sym, one, 4)[1]
    e2 = _in_memory(img, sym, one * 2, 4)[1]
    reps = max(2, int(mb * 1e6 / len(one)) + 1)
    path = tmp_path / "grande.bin"
    w = record.TraceRecorder(str(path), "etm", etm=4, elf=ELF)
    for _ in range(reps):
        w.write(one)
    w.close()
    r = record.RecordedTrace(str(path), img, sym)
    etm = EtmTrace(img, sym, history=50_000, version=4)
    etm.stream_timing_on()
    before = _rss()
    r.replay(etm)
    grown = _rss() - before
    per_rep = len(e2) - len(e1)                       # ce qu'ajoute une répétition (jonction comprise)
    assert etm.events_total == len(e1) + (reps - 1) * per_rep
    assert grown < 150e6, f"mémoire +{grown / 1e6:.0f} Mo pour {path.stat().st_size / 1e6:.1f} Mo relus"
    res = etm.timing_result()
    assert res.total > 0 and len(res.intervals) <= 2 * 200_000
    mid = etm.events_total // 2
    page = r.events(mid, 100)
    assert len(page) == 100
    with open(record.points_path(str(path)), encoding="utf-8") as fh:
        assert json.loads(fh.readline())["points"] == record.FORMAT


RECORD_SCRIPT = """
src = gdbtrace.trace.source
src.done.wait(60)
gdbtrace.trace.wait_etm(60)
print("événements", gdbtrace.trace.etm.events_total)
"""
REPLAY_SCRIPT = """
gdbtrace.trace.source.done.wait(60)
gdbtrace.trace_list(5, start=100)
info = gdbtrace.trace_recording()
assert info["relecture"]["fini"] and info["relecture"]["événements"] > 1000, info
gdbtrace.timing(top=3)
"""


@needs_qemu
def test_command_line_record_and_replay(qemu_paths, elf, tmp_path):
    """--swo-file (TPIU formaté) avec --trace-record : enregistré ; puis --trace-file de
    l'enregistrement en --batch : page de Trace.List depuis le fichier, état de la trace sur
    disque, timing de toute la trace. Clé trace.trace-record d'une configuration."""
    import subprocess
    import sys

    from gdbtrace.__main__ import build_parser, validate
    from gdbtrace.core.config import SessionConfig
    img, events = qemu_paths["bare"]
    src, rec = tmp_path / "src.bin", tmp_path / "enregistree.bin"
    src.write_bytes(frame_bytes(2, _encode(events[:60_000], img, 3)))

    def run(*args, script):
        path = tmp_path / "s.py"
        path.write_text(script, encoding="utf-8")
        return subprocess.run([sys.executable, "-m", "gdbtrace", elf, *args, "--batch", "--script",
                               str(path)], cwd=ROOT, capture_output=True, text=True, timeout=180,
                              env={**os.environ, "PYTHONPATH": ROOT})
    r = run("--swo-file", str(src), "--swo-formatter", "--trace-record", str(rec), script=RECORD_SCRIPT)
    assert r.returncode == 0 and f"capture enregistrée dans {rec}" in r.stdout, r.stdout + r.stderr
    assert rec.read_bytes() == src.read_bytes() and os.path.exists(record.index_path(str(rec)))
    r = run("--trace-file", str(rec), script=REPLAY_SCRIPT)
    out = r.stdout + r.stderr
    assert r.returncode == 0 and "capture enregistrée relue" in out, out
    assert "   100  " in out and "relecture : fichier" in out and "analysés" in out, out

    p = build_parser()
    assert any("--trace-record" in e for e in validate(p.parse_args([elf, "--trace-record", "x.bin"])))
    args = p.parse_args([elf, "--swo-file", str(src), "--trace-record", str(rec)])
    assert validate(args) == []
    cfg = SessionConfig.from_args(args)
    assert cfg.data["trace"]["trace-record"] == str(rec)
    assert SessionConfig(cfg.data).parse().trace_record == str(rec)
