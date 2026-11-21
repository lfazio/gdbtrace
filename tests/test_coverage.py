"""Couverture de code (COVerage) : base E/N, branches, lignes, fonctions, sauvegarde/fusion."""
import pytest
from conftest import needs_qemu

from gdbtrace.core.symbols import Function, LineEntry
from gdbtrace.trace.coverage import (I_COND_FALSE, I_NEVER, I_NOT_TAKEN, I_OK, I_TAKEN, NEVER, OK,
                                     PARTIAL, CoverageDB)
from gdbtrace.trace.etm.image import DIRECT, INDIRECT, NORMAL, Instr
from gdbtrace.trace.etm.qemu_log import Step


# ====================================================================== programme factice
def I(addr, kind=NORMAL, size=2, target=None, cond=False, text="nop", ret=False):
    return Instr(addr, size, kind, target, cond, False, ret, text)


class FakeImage:
    path = "/fake.elf"

    def __init__(self, instrs):
        self.m = {i.addr: i for i in instrs}
        self.segments = [(0x100, bytes(range(32)))]

    def instr(self, a):
        return self.m.get(a & ~1)


class FakeSymbols:
    def __init__(self, funcs, lines):
        self.functions = funcs
        self.lines = sorted(lines, key=lambda e: e.address)

    def is_data(self, a):
        return False

    def line_at(self, a):
        best = None
        for e in self.lines:
            if e.address <= a:
                best = e
        return best


# f (lignes 10 à 13) : boucle avec branchement conditionnel, bloc IT, retour
CODE = [I(0x100), I(0x102, DIRECT, 2, 0x100, cond=True, text="bne #0x100"),
        I(0x104, text="it eq"), I(0x106, text="moveq r0, #1"),
        I(0x108, INDIRECT, text="bx lr", ret=True),
        I(0x10A, DIRECT, 2, 0x100, cond=True, text="beq #0x100"), I(0x10C)]
LINES = [LineEntry(0x100, "f.c", 10), LineEntry(0x104, "f.c", 11), LineEntry(0x108, "f.c", 12),
         LineEntry(0x10A, "f.c", 13)]


def db_for(events):
    db = CoverageDB(FakeImage(CODE), FakeSymbols([Function("f", 0x100, 0x0E)], LINES))
    db.add(events)
    return db


def S(a, executed=True):
    return Step(a, executed=executed)


def test_instruction_tags_and_branches():
    db = db_for([S(0x100), S(0x102), S(0x100), S(0x102, False),     # bne : pris puis non pris
                 S(0x104), S(0x106, False), S(0x108),               # moveq : condition fausse
                 S(0x10A)])                                         # beq : toujours pris
    assert [db.tag(a) for a in (0x100, 0x102, 0x104, 0x106, 0x108, 0x10A, 0x10C)] == \
        [I_OK, I_OK, I_OK, I_COND_FALSE, I_OK, I_TAKEN, I_NEVER]
    lines = db.lines("f.c")
    assert lines[10].status == OK and lines[10].branch_percent == 100
    assert lines[11].status == PARTIAL                      # moveq jamais exécutée
    assert lines[13].status == PARTIAL and lines[13].branch_percent == 50
    (f,) = db.functions()
    # instructions : 0x10C jamais atteinte, moveq condition fausse -> 5 couvertes sur 7
    assert (f.instrs, f.covered, f.branches, f.outcomes, f.entries) == (7, 5, 2, 3, 2)
    assert f.status == PARTIAL and f.file == "f.c" and f.line == 10
    db.add([S(0x10A, False)])
    assert db.tag(0x10A) == I_OK and db.tag(0x102) == I_OK


def test_it_block_branch_is_conditional():
    """Un branchement dans un bloc IT est conditionnel, même décodé seul par capstone."""
    code = [I(0x100, text="it eq"), I(0x102, INDIRECT, text="bxeq lr", ret=True), I(0x104)]
    db = CoverageDB(FakeImage(code), FakeSymbols([Function("g", 0x100, 6)],
                                                 [LineEntry(0x100, "g.c", 1)]))
    db.add([S(0x100), S(0x102, False), S(0x104)])
    assert db.tag(0x102) == I_NOT_TAKEN
    (f,) = db.functions()
    assert (f.branches, f.outcomes, f.status) == (1, 1, PARTIAL)


def test_never_and_reset():
    db = db_for([])
    (f,) = db.functions()
    assert f.status == NEVER and f.percent == 0 and db.lines("f.c")[12].status == NEVER
    db.add([S(0x100)])
    assert db.functions()[0].covered == 1
    db.reset()
    assert db.functions()[0].covered == 0


def test_save_load_merge(tmp_path):
    a = db_for([S(0x100), S(0x102)])
    b = db_for([S(0x100), S(0x102, False), S(0x10A, False)])
    b.save(tmp_path / "b.json")
    a.load(tmp_path / "b.json")                             # COVerage.LOAD /Add
    assert (a.e[0x100], a.e[0x102], a.n[0x102], a.n[0x10A]) == (2, 1, 1, 1)
    assert a.tag(0x102) == I_OK and a.tag(0x10A) == I_NOT_TAKEN
    a.load(tmp_path / "b.json", merge=False)                # remplace
    assert a.e.get(0x102, 0) == 0 and a.e[0x100] == 1
    other = db_for([])
    other.image.segments = [(0x100, b"autre code")]
    with pytest.raises(ValueError, match="autre programme"):
        other.load(tmp_path / "b.json")


# ====================================================================== chemins réels
@needs_qemu
def test_real_trace_coverage(qemu_paths, elf):
    """Trace ETM décodée du firmware de démo : atomes E/N, branches, soft-float partiel."""
    from gdbtrace.core.symbols import SymbolTable
    from gdbtrace.trace.etm.cycles import outcome, successors
    from gdbtrace.trace.etm.encoder import encode
    from gdbtrace.trace.etm.flow import decode
    img, events = qemu_paths["bare"]
    sym = SymbolTable(elf)
    decoded, pk, _ = decode(encode(events, img), img)
    assert pk.errors == 0
    # l'atome décodé est l'issue réelle de chaque instruction du chemin QEMU
    nxt = successors(events)                             # indexé par position d'événement
    want = [outcome(img.instr(s.addr), s.addr, nxt.get(i))[0]
            for i, s in enumerate(events) if isinstance(s, Step)]
    got = [e.executed for e in decoded if isinstance(e, Step)]
    assert got == want and not all(got)
    db = CoverageDB(img, sym)
    db.add(decoded)
    funcs = {f.name: f for f in db.functions()}
    for name in ("fib", "delay", "update_sensor", "main"):
        assert funcs[name].status in (OK, PARTIAL) and funcs[name].percent == 100, name
    assert funcs["fib"].status == OK and funcs["fib"].branches >= 1     # boucle : deux issues vues
    assert funcs["HardFault_Handler"].status == NEVER
    fadd = next(f for n, f in funcs.items() if "__addsf3" in n)
    assert fadd.status == PARTIAL and fadd.branch_percent < 100       # cas NaN/infini jamais vus
    t, called, nfuncs = db.total()
    assert 0 < t.percent < 100 and 0 < called < nfuncs
    assert "instructions" in db.summary() and "branches" in db.summary()
    # cohérence avec l'API historique d'EtmTrace
    from gdbtrace.trace.etm.trace import EtmTrace
    trace = EtmTrace(img, sym)
    trace.feed(encode(events, img))
    assert {c.name: c.executed for c in trace.coverage()} == {f.name: f.covered for f in funcs.values()}
