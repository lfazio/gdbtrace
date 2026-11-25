"""Filtre de la trace ETM (plages, start/stop) : sémantique, aller-retour par l'encodeur et
le décodeur (ETMv3 et ETMv4) sur les chemins réels de QEMU, programmation des registres."""
import pytest
from conftest import needs_qemu
from test_etm import as_tuples

from gdbtrace.trace.etm import qemu_log as ql
from gdbtrace.trace.etm.filter import EtmFilter, build_filter, resolve_address, resolve_range

S, G = ql.Step, ql.TraceGap


def test_apply_ranges_start_stop():
    path = [S(0x100), S(0x102), S(0x200), S(0x202), ql.ExcEntry(15, 0x300), S(0x300),
            ql.ExcReturn(), S(0x204), S(0x104)]
    # plage 0x200-0x210 : l'exception interrompt du code tracé (gardée), son handler est
    # hors plage, la reprise est un trou
    got = EtmFilter(ranges=[(0x200, 0x210)]).apply(path)
    assert as_tuples(got) == [("step", 0x200), ("step", 0x202), ("exc", 15), ("gap",), ("step", 0x204)]
    assert got[3].next_addr == 0x300
    # start à 0x102, stop à 0x202 (tracée, puis arrêt)
    got = EtmFilter(start=[0x102], stop=[0x202]).apply(path)
    assert as_tuples(got) == [("step", 0x102), ("step", 0x200), ("step", 0x202)]
    # exception gardée quand elle est entourée d'instructions tracées
    got = EtmFilter(ranges=[(0x200, 0x400)]).apply(path)
    assert as_tuples(got) == [("step", 0x200), ("step", 0x202), ("exc", 15), ("step", 0x300),
                              ("ret",), ("step", 0x204)]
    assert EtmFilter().apply(path) == path and not EtmFilter().active


def test_specs(elf):
    from gdbtrace.core.symbols import SymbolTable
    sym = SymbolTable(elf)
    fib = sym.function("fib")
    assert resolve_range("fib", sym) == ([(fib.start, fib.end)], "fib")
    assert resolve_range("0x100-0x200", None)[0] == [(0x100, 0x200)]
    assert resolve_range("0x100+64", None)[0] == [(0x100, 0x140)]
    main_c, _ = resolve_range("main.c", sym)
    assert any(lo <= fib.start < hi for lo, hi in main_c)
    assert resolve_address("fib", sym) == fib.start and resolve_address("0x1f5", None) == 0x1f4
    assert resolve_address("main.c:59", sym) in sym.addresses_for_line(
        next(f for f in sym.source_files() if f.endswith("main.c")), 59)
    with pytest.raises(ValueError, match="ni fonction"):
        resolve_range("nexiste_pas", sym)
    flt = build_filter(["fib"], start=["update_sensor"], stop=["animate"], symbols=sym)
    assert flt.active and "fib" in flt.describe() and "start" in flt.describe()


# ====================================================================== aller-retour
FILTERS = {
    "bare": [dict(ranges=["fib"]), dict(ranges=["update_sensor", "delay"]),
             dict(start=["update_sensor"], stop=["animate"])],
    "rtos": [dict(ranges=["prvTaskCheckFreeStackSpace"]),
             dict(start=["vTaskSwitchContext"], stop=["xTaskIncrementTick"])],
}


def _filters(qemu_paths, name, elf, rtos_elf):
    from gdbtrace.core.symbols import SymbolTable
    sym = SymbolTable(elf if name == "bare" else rtos_elf)
    return [build_filter(f.get("ranges", ()), f.get("start", ()), f.get("stop", ()), sym)
            for f in FILTERS[name]]


@needs_qemu
@pytest.mark.parametrize("name", ["bare", "rtos"])
def test_filtered_roundtrip_etm3(qemu_paths, name, elf, rtos_elf):
    from gdbtrace.trace.etm.encoder import encode
    from gdbtrace.trace.etm.flow import decode
    img, events = qemu_paths[name]
    for flt in _filters(qemu_paths, name, elf, rtos_elf):
        traced = flt.apply(events)
        gaps = sum(isinstance(e, G) for e in traced)
        assert gaps > 2, flt.describe()
        decoded, pk, fl = decode(encode(traced, img), img)
        assert pk.errors == 0 and fl.stats.lost_atoms == 0 and fl.stats.unknown_code == 0
        assert as_tuples(decoded) == as_tuples(traced), flt.describe()


@needs_qemu
@pytest.mark.parametrize("name", ["bare", "rtos"])
def test_filtered_roundtrip_etm4(qemu_paths, name, elf, rtos_elf):
    from test_etm4 import _check_prefix
    from gdbtrace.trace.etm.encoder4 import encode
    from gdbtrace.trace.etm.flow4 import decode
    img, events = qemu_paths[name]
    for flt in _filters(qemu_paths, name, elf, rtos_elf):
        traced = flt.apply(events)
        decoded, pk, fl = decode(encode(traced, img), img)
        assert pk.errors == 0 and fl.stats.errors == 0 and fl.stats.unknown_code == 0
        # tronçon par tronçon : un atome par point de passage, les instructions qui suivent
        # le dernier d'un tronçon (avant un arrêt de la trace) ne sont pas datées
        got, ref = _segments(as_tuples(decoded)), _segments(as_tuples(traced))
        assert len(got) == len(ref), flt.describe()
        for g, r in zip(got, ref):
            _check_prefix(g, [_event(t) for t in r], img)


def _segments(tuples):
    out = [[]]
    for t in tuples:
        if t == ("gap",):
            out.append([])
        else:
            out[-1].append(t)
    return out


def _event(t):
    return S(t[1]) if t[0] == "step" else ql.ExcEntry(t[1], 0) if t[0] == "exc" else ql.ExcReturn()


# ====================================================================== registres
class FakeTarget:
    """Registres d'une cible arrêtée ; `mask_bits` : largeur maximale de DWT_MASK."""

    def __init__(self, etmidr, numcomp=4, ac_pairs=2, mask_bits=31):
        from gdbtrace.trace.etm import config as ec
        self.ec, self.mask_bits = ec, mask_bits
        self.regs = {ec.ETMIDR: etmidr, ec.DWT_CTRL: numcomp << 28, ec.TRCIDR4: ac_pairs}

    def read_u32(self, a):
        return self.ec.TRCSTATR_IDLE if a == self.ec.TRCSTATR else self.regs.get(a, 0)

    def write_u32(self, a, v):
        if 0xE0001024 <= a < 0xE0001060 and (a - 0xE0001024) % 16 == 0:     # DWT_MASKn
            v = min(v, self.mask_bits)
        self.regs[a] = v


def _cfg(flt, **kw):
    from gdbtrace.trace.etm.config import EtmConfig
    return EtmConfig(port="swo", cpu_hz=16_000_000, filter=flt, **kw)


def test_etmv3_filter_uses_dwt_comparators():
    from gdbtrace.trace.etm import config as ec
    t = FakeTarget(0x4114F250)                                         # ETM-M4 (ETMv3.5)
    cfg = _cfg(EtmFilter(ranges=[(0x1A4, 0x1C0)], start=[0x300]))
    done = dict(cfg.apply(t))
    assert cfg.version == 3
    # plage → bloc aligné 0x1A0-0x1C0 (2^5) sur le comparateur 3 ; start sur le 2
    assert (t.regs[ec.DWT_COMP0 + 48], t.regs[ec.DWT_COMP0 + 52], t.regs[ec.DWT_COMP0 + 56]) == (0x1A0, 5, 8)
    assert (t.regs[ec.DWT_COMP0 + 32], t.regs[ec.DWT_COMP0 + 36], t.regs[ec.DWT_COMP0 + 40]) == (0x300, 0, 8)
    assert done["ETMTEEVR"] == 0x20 + 3                                # comparateur EmbeddedICE 3
    assert done["ETMTECR1"] == 1 << 24 | 1 << 25 and done["ETMTESSEICR"] == 1 << 2
    assert cfg.effective_filter().ranges == [(0x1A0, 0x1C0)] and "blocs alignés" in cfg.summary()
    assert _cfg(None).dwt_block(0x1FC, 0x204) == (0x0, 0x400, 10)          # à cheval sur 0x200
    # deux plages : A OU B ; un arrêt : bits 19:16
    cfg = _cfg(EtmFilter(ranges=[(0x100, 0x110), (0x400, 0x480)], start=[0x500], stop=[0x600]))
    done = dict(cfg.apply(FakeTarget(0x4114F250)))
    assert done["ETMTEEVR"] == 0b101 << 14 | (0x20 + 2) << 7 | (0x20 + 3)
    assert done["ETMTESSEICR"] == 1 << 1 | 1 << 16
    # sans filtre : tout le code, comme avant
    done = dict(_cfg(None).apply(FakeTarget(0x4114F250)))
    assert (done["ETMTEEVR"], done["ETMTECR1"], done["ETMTESSEICR"]) == (0x6F, 1 << 24, 0)
    with pytest.raises(ValueError, match="deux plages"):
        _cfg(EtmFilter(ranges=[(0, 2), (4, 6), (8, 10)])).apply(FakeTarget(0x4114F250))
    with pytest.raises(ValueError, match="arrêt sans démarrage"):
        _cfg(EtmFilter(stop=[0x100])).apply(FakeTarget(0x4114F250))
    with pytest.raises(ValueError, match="comparateurs du DWT nécessaires"):
        _cfg(EtmFilter(ranges=[(0, 2)], start=[4, 8])).apply(FakeTarget(0x4114F250, numcomp=2))
    with pytest.raises(ValueError, match="2\\^4 au plus"):             # masque plafonné
        _cfg(EtmFilter(ranges=[(0x0, 0x1000)])).apply(FakeTarget(0x4114F250, mask_bits=4))


def test_etmv4_filter_uses_address_comparators():
    from gdbtrace.trace.etm import config as ec
    t = FakeTarget(0x4100F400, ac_pairs=2)                             # ETM-M7 (ETMv4)
    cfg = _cfg(EtmFilter(ranges=[(0x1A4, 0x1C0)], start=[0x300], stop=[0x320]))
    done = dict(cfg.apply(t))
    assert cfg.version == 4 and cfg.effective_filter().ranges == [(0x1A4, 0x1C0)]
    assert [t.regs[ec.TRCACVR0 + 8 * n] for n in range(4)] == [0x1A4, 0x1BF, 0x300, 0x320]
    assert done["TRCVIIECTLR"] == 1 and done["TRCVISSCTLR"] == 1 << 2 | 1 << 19
    assert done["TRCVICTLR"] == 0x01                                   # attend son démarrage
    done = dict(_cfg(EtmFilter(ranges=[(0x100, 0x200)])).apply(FakeTarget(0x4100F400)))
    assert done["TRCVICTLR"] == 0x201 and done["TRCVISSCTLR"] == 0      # démarrée d'office
    with pytest.raises(ValueError, match="comparateurs d'adresse nécessaires"):
        _cfg(EtmFilter(ranges=[(0, 2), (4, 6)], start=[8])).apply(FakeTarget(0x4100F400, ac_pairs=2))


# ====================================================================== bout en bout
@needs_qemu
def test_filtered_simulated_trace_cli(elf, tmp_path):
    """--etm-sim --etm-filter fib : seules les instructions du bloc tracé arrivent, avec des
    trous ; la couverture ne porte que sur la plage ; etm_filter() retire le filtre."""
    import subprocess
    import sys
    from conftest import ROOT
    script = tmp_path / "s.py"
    script.write_text(
        "from gdbtrace.trace.etm.qemu_log import Step, TraceGap\n"
        "fib = gdbtrace._symbols().function('fib')\n"
        "eff = gdbtrace.trace.etm_filter.ranges[0]\n"
        "assert eff[0] <= fib.start and fib.end <= eff[1], eff\n"
        "gdbtrace.break_('animate', temporary=True); gdbtrace.go(); gdbtrace.wait(10)\n"
        "ev = gdbtrace._etm().last(10**6)\n"
        "steps = [e for e in ev if isinstance(e, Step)]\n"
        "assert steps and all(eff[0] <= e.addr < eff[1] for e in steps)\n"
        "assert sum(isinstance(e, TraceGap) for e in ev) >= 3\n"
        "names = {f.name for f in gdbtrace.cov.db.functions()}\n"
        "assert 'fib' in names and 'main' not in names, names\n"
        "print('filtré', len(steps), gdbtrace.trace.source_label())\n"
        "gdbtrace.etm_filter()\n"
        # QEMU 8.2 chargé : le pas qui enjambe le breakpoint revient parfois sans avoir avancé,
        # et la cible se rarrête aussitôt sur animate (CLAUDE.md, limites) : on relance
        "for _ in range(3):\n"
        "    gdbtrace.break_('animate', temporary=True); gdbtrace.go(); gdbtrace.wait(10)\n"
        "    if any(not (eff[0] <= e.addr < eff[1]) for e in gdbtrace._etm().last(5000) if isinstance(e, Step)):\n"
        "        break\n"
        "else:\n"
        "    raise AssertionError('filtre retiré : rien tracé hors de fib')\n"
        "print('OK')\n")
    r = subprocess.run([sys.executable, "-m", "gdbtrace", elf, "--etm-sim", "--etm-filter", "fib",
                        "--batch", "--script", str(script)], cwd=ROOT, capture_output=True, text=True,
                       timeout=120)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "filtre : plages fib" in r.stdout and "OK" in r.stdout
    r = subprocess.run([sys.executable, "-m", "gdbtrace", elf, "--etm-filter", "fib", "--batch"],
                       cwd=ROOT, capture_output=True, text=True, timeout=60)
    assert r.returncode == 2 and "avec une trace ETM" in r.stderr
