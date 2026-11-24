"""Trace de données du DWT (Var.TRACE) : programmation des comparateurs, paquets ITM →
échantillons typés, chemin complet côté hôte (QEMU n'émule ni DWT ni ITM : flux synthétiques)."""
import os
import subprocess
import sys

import pytest
from conftest import ROOT, needs_qemu
from test_itm import SYNC, hw

from gdbtrace.trace.datatrace import (FUNCTION, DataTraceSink, Sample, TracedVar, comparator_writes,
                                      mask_for)
from gdbtrace.core.live_vars import LeafLayout


def lts(delta: int) -> bytes:
    """Horodatage local (format 1) : 7 bits par octet, bit 7 = suite."""
    out = bytearray([0xC0])
    while True:
        byte, delta = delta & 0x7F, delta >> 7
        out.append(byte | (0x80 if delta else 0))
        if not delta:
            return bytes(out)


def data(comp: int, value: int, size: int, write: bool = True) -> bytes:
    return hw(16 + 2 * comp + (1 if write else 0), value, size)


def pc(comp: int, value: int) -> bytes:
    return hw(8 + 2 * comp, value, 4)


def test_comparator_programming():
    assert mask_for(0x20000000, 4) == 2 and mask_for(0x20000002, 2) == 1 and mask_for(0x20000001, 1) == 0
    assert mask_for(0x20000002, 4) == 3                          # à cheval : bloc de 8
    assert (FUNCTION[("write", False)], FUNCTION[("read", False)], FUNCTION[("rw", True)]) == (0xD, 0xC, 0x3)
    var = TracedVar("ticks", 0x20000040, 4, 3, "rw", pc=True)
    w = {name: (addr, val) for name, addr, val in comparator_writes(var)}
    assert w["DWT_COMP3 (ticks)"] == (0xE0001050, 0x20000040)
    assert w["DWT_MASK3"] == (0xE0001054, 2) and w["DWT_FUNCTION3"] == (0xE0001058, 0x3)


def test_sink_samples_times_and_pc():
    sink = DataTraceSink(cpu_hz=1_000_000)                       # 1 cycle = 1 µs
    v = TracedVar("offset", 0x20000038, 2, 3, "write", pc=True,
                  layout=LeafLayout(0x20000038, 2, "int", live=True))
    sink.add(v)
    from gdbtrace.trace.itm import ItmDecoder
    dec = ItmDecoder()
    stream = (SYNC + lts(100) + pc(3, 0x1F4) + data(3, 0xFFFD, 2) + lts(250) + data(3, 0xFFFB, 2)
              + data(1, 7, 4))                                   # comparateur 1 : inconnu
    sink(dec.feed(stream))
    assert v.count == 2 and sink.lost == 1
    a, b = v.samples
    assert (a.t, b.t) == (0.0, 250e-6) and a.pc == 0x1F4 and b.pc is None
    assert (v.text(a), v.text(b), v.number(b)) == ("-3", "-5", -5.0)
    assert v.text(a, "hex") == "0xfffd"
    assert v.text(Sample(0, 0x12, 1, "write")) == "0x12"         # accès partiel : brut
    sink.reset()
    assert v.count == 0 and not v.samples


@needs_qemu
def test_trace_variable_host_path(session, tmp_path):
    """Cible arrêtée : adresse, taille et type par gdb, comparateur choisi par le haut ; puis
    une capture SWO rejouée → échantillons typés ; locale et structure refusées."""
    from gdbtrace.core.symbols import SymbolTable
    from gdbtrace.trace.manager import TraceManager
    tm = TraceManager()
    tm.setup_etm(session.elf, SymbolTable(session.elf))
    session.break_("update_sensor")
    session.go()
    session.wait_stopped(5)
    try:
        log = tm.trace_variable(session, "offset", "write")
        assert "comparateur 3" in log[-2] and "pas de capture" in log[-1]
        tm.trace_variable(session, "temperature", "rw", pc=True)
        assert sorted(tm.datatrace.vars) == [2, 3]
        with pytest.raises(ValueError, match="locale"):
            tm.trace_variable(session, "tick")
        with pytest.raises(ValueError, match="1, 2 ou 4 octets"):
            tm.trace_variable(session, "sensors[0]")
        import struct
        cap = tmp_path / "swo.bin"
        cap.write_bytes(SYNC + data(3, 0xFFFD, 2) + lts(1000) + pc(2, 0x200)
                        + data(2, struct.unpack("<I", struct.pack("<f", 21.5))[0], 4))
        tm.start_file(str(cap)).done.wait(5)
        off = next(v for v in tm.datatrace.vars.values() if v.expr == "offset")
        tmp = next(v for v in tm.datatrace.vars.values() if v.expr == "temperature")
        assert off.text(off.samples[0]) == "-3" and tmp.text(tmp.samples[0]) == "21.5"
        assert tmp.samples[0].pc == 0x200
        assert tm.untrace_variable(session, "offset") and sorted(tm.datatrace.vars) == [2]
        with pytest.raises(KeyError):
            tm.untrace_variable(session, "offset")
    finally:
        tm.close()


@needs_qemu
def test_var_trace_cli(elf, tmp_path):
    """--var-trace counter:rw+pc avec une capture rejouée ; gdbtrace.var_samples."""
    cap = tmp_path / "swo.bin"
    cap.write_bytes(SYNC + pc(3, 0x15E) + data(3, 41, 4) + lts(2500) + pc(3, 0x15E) + data(3, 42, 4))
    script = tmp_path / "s.py"
    script.write_text("import time\n"
                      "for _ in range(100):\n"
                      "    if gdbtrace.trace.datatrace.vars[3].count == 2: break\n"
                      "    time.sleep(0.05)\n"
                      "rows = gdbtrace.var_samples('counter')\n"
                      "assert [r.raw for r in rows] == [41, 42]\n")
    r = subprocess.run([sys.executable, "-m", "gdbtrace", elf, "--swo-file", str(cap), "--cpu-hz",
                        "25000000", "--var-trace", "counter:rw+pc", "--batch", "--script", str(script)],
                       cwd=ROOT, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "trace de counter (4 octets" in r.stdout and "100.0 µs" in r.stdout and "main" in r.stdout
    r = subprocess.run([sys.executable, "-m", "gdbtrace", elf, "--var-trace", "counter", "--batch"],
                       cwd=ROOT, capture_output=True, text=True, timeout=60)
    assert r.returncode == 2 and "capture SWO ou de trace" in r.stderr
    assert os.path.exists(cap)
