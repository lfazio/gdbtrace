"""Analyse temporelle : scénarios synthétiques calculés à la main, puis chemins QEMU réels."""
import pytest
from conftest import needs_qemu

from gdbtrace.core.symbols import Function
from gdbtrace.trace.etm.image import DIRECT, INDIRECT, NORMAL, Instr
from gdbtrace.trace.etm.qemu_log import ExcEntry, ExcReturn, Step
from gdbtrace.trace.timing import EXCEPTION, TimingAnalyzer, to_us


# ====================================================================== programme factice
def I(addr, kind=NORMAL, size=2, target=None, call=False, ret=False, cond=False):
    return Instr(addr, size, kind, target, cond, call, ret, "x")


class FakeImage:
    def __init__(self, instrs):
        self.m = {i.addr: i for i in instrs}

    def instr(self, a):
        return self.m.get(a & ~1)


class FakeSymbols:
    def __init__(self, funcs):
        self.functions = sorted(funcs, key=lambda f: f.start)

    def function_at(self, a):
        for f in reversed(self.functions):
            if f.start <= a < f.end:
                return f
        return None


FUNCS = [Function("main", 0x100, 0x20), Function("f", 0x200, 0x20), Function("t", 0x240, 0x20),
         Function("g", 0x300, 0x10), Function("h", 0x400, 0x10), Function("handler", 0x500, 0x10),
         Function("handler2", 0x520, 0x10), Function("s", 0x600, 0x10), Function("u", 0x610, 0x20)]
CODE = [
    I(0x100), I(0x102, DIRECT, 4, 0x200, call=True), I(0x106),
    I(0x108, DIRECT, 4, 0x240, call=True), I(0x10C), I(0x110, DIRECT, 4, 0x600, call=True), I(0x114),
    I(0x200), I(0x202, DIRECT, 4, 0x300, call=True), I(0x206), I(0x208, INDIRECT, ret=True),
    I(0x240), I(0x242, DIRECT, 2, 0x400),                       # t : appel terminal vers h
    I(0x300), I(0x302, INDIRECT, ret=True),
    I(0x400), I(0x402, INDIRECT, ret=True),
    I(0x500), I(0x502, INDIRECT, ret=True), I(0x520), I(0x522, INDIRECT, ret=True),
    I(0x600, DIRECT, 2, 0x614),                                 # s saute au milieu de u
    I(0x614), I(0x616, INDIRECT, ret=True, cond=True), I(0x618), I(0x61A, INDIRECT, ret=True),
]


def run(events):
    return TimingAnalyzer(FakeImage(CODE), FakeSymbols(FUNCS)).analyze(events)


def S(a, c):
    return Step(a, cycles=c)


def test_nesting_and_interrupt_exclusion():
    r = run([S(0x100, 1), S(0x102, 3), S(0x200, 2), S(0x202, 3), S(0x300, 1),
             ExcEntry(15, 0x500, cycles=12), S(0x500, 4), S(0x502, 3), ExcReturn(cycles=10),
             S(0x302, 3), S(0x206, 1), S(0x208, 3), S(0x106, 1)])
    s = r.stats
    assert r.total == 47 and sum(x.net for x in s.values()) == 47      # chaque cycle une fois
    assert (s["g"].calls, s["g"].gross, s["g"].net) == (1, 4, 4)      # 29 cycles d'IT déduits
    assert (s["f"].calls, s["f"].gross, s["f"].net) == (1, 13, 9)     # 13 = 9 + g
    assert (s["main"].calls, s["main"].partial, s["main"].net) == (0, 1, 5)
    exc = s["[SysTick]"]
    assert exc.kind == EXCEPTION and (exc.calls, exc.gross, exc.net) == (1, 29, 22)  # 12 + 10
    assert (s["handler"].gross, s["handler"].net) == (7, 7)          # sans la latence de sortie
    iv = {i.name: i for i in r.intervals}
    assert iv["g"].depth == 2 and iv["[SysTick]"].depth == 3 and iv["handler"].depth == 4
    assert (iv["[SysTick]"].start, iv["[SysTick]"].end) == (10, 39)


def test_tail_call():
    r = run([S(0x108, 3), S(0x240, 1), S(0x242, 3), S(0x400, 1), S(0x402, 3), S(0x10C, 1)])
    s = r.stats
    assert (s["t"].calls, s["t"].gross) == (1, 4)       # fermé par l'appel terminal
    assert (s["h"].calls, s["h"].gross) == (1, 4)       # hérite de l'adresse de retour de t


def test_shared_code_and_conditional_return():
    # s saute au milieu de u, y croise un `bx lr` conditionnel NON exécuté, puis retourne
    r = run([S(0x110, 3), S(0x600, 3), S(0x614, 1), S(0x616, 1), S(0x618, 1), S(0x61A, 3), S(0x114, 1)])
    s = r.stats
    assert (s["s"].calls, s["s"].partial, s["s"].gross) == (1, 0, 9)
    assert "u" not in s                                 # pas de cadre fantôme


def test_context_switch_closes_frames_as_partial():
    r = run([S(0x100, 1), S(0x102, 3), S(0x200, 2),
             ExcEntry(14, 0x500, cycles=12), S(0x500, 4), S(0x502, 3), ExcReturn(cycles=10),
             S(0x300, 1), S(0x302, 3), S(0x206, 1)])   # reprise ailleurs : autre tâche
    s = r.stats
    assert r.context_switches == 1
    assert (s["f"].calls, s["f"].partial) == (0, 2)     # coupé au changement, puis reprise partielle
    assert s["g"].partial == 1 and s["g"].min is None
    assert sum(x.net for x in s.values()) == r.total


def test_tail_chained_exceptions():
    r = run([S(0x100, 1), ExcEntry(15, 0x500, cycles=12), S(0x500, 4), S(0x502, 3),
             ExcEntry(14, 0x520, tailchain=True, cycles=6), S(0x520, 2), S(0x522, 3),
             ExcReturn(cycles=10), S(0x102, 3)])
    s = r.stats
    assert (s["[SysTick]"].calls, s["[SysTick]"].gross) == (1, 19)    # 12 + 7, sans dépilement
    assert (s["[PendSV]"].calls, s["[PendSV]"].gross) == (1, 21)      # 6 + 5 + 10
    assert r.context_switches == 0 and sum(x.net for x in s.values()) == r.total


def test_units():
    assert to_us(25_000, 25_000_000) == pytest.approx(1000.0)


# ====================================================================== chemins réels
def _analyze(name, qemu_paths, elf_path):
    from gdbtrace.core.symbols import SymbolTable
    from gdbtrace.trace.etm.cycles import annotate
    img, events = qemu_paths[name]
    timed = annotate(events, img)
    sym = SymbolTable(elf_path)
    return img, sym, timed, TimingAnalyzer(img, sym).analyze(timed)


@needs_qemu
def test_bare_metal_identities(qemu_paths, elf):
    img, sym, timed, r = _analyze("bare", qemu_paths, elf)
    s = r.stats
    assert sum(x.net for x in s.values()) == r.total == sum(e.cycles for e in timed)
    assert r.context_switches == 0
    # delay() et le handler SysTick ont une durée constante une fois les interruptions déduites
    assert s["delay"].calls > 10 and s["delay"].min == s["delay"].max
    assert s["SysTick_Handler"].min == s["SysTick_Handler"].max
    # brut = net + brut des appelés
    assert s["update_sensor"].gross == s["update_sensor"].net + s["fib"].gross
    assert s["[SysTick]"].gross == s["[SysTick]"].net + s["SysTick_Handler"].gross
    assert s["fib"].calls == s["update_sensor"].calls
    # seuls les cadres sans fin (Reset_Handler, main) ou coupés par la fin de trace sont partiels
    assert {n for n, x in s.items() if x.partial} <= {"Reset_Handler", "main", "delay", "animate",
                                                      "update_sensor", "fib"}
    assert all(x.partial <= 1 for x in s.values())


@needs_qemu
def test_freertos_identities(qemu_paths, rtos_elf):
    img, sym, timed, r = _analyze("rtos", qemu_paths, rtos_elf)
    s = r.stats
    assert sum(x.net for x in s.values()) == r.total
    assert r.context_switches > 10
    assert s["[PendSV]"].calls > 10 and s["[SysTick]"].calls > 10
    assert s["vTaskSwitchContext"].calls == s["[PendSV]"].calls    # un par PendSV


@needs_qemu
def test_decoded_trace_gives_same_timing(qemu_paths, elf):
    """Statistiques tirées de la trace ETM cycle-accurate décodée == vérité terrain."""
    from gdbtrace.trace.etm.encoder import encode
    from gdbtrace.trace.etm.flow import decode
    img, sym, timed, ref = _analyze("bare", qemu_paths, elf)
    decoded, pk, _ = decode(encode(timed, img, cycle_accurate=True), img, cycle_accurate=True)
    got = TimingAnalyzer(img, sym).analyze(decoded)
    key = lambda r: {n: (x.calls, x.partial, x.gross, x.net, x.min, x.max) for n, x in r.stats.items()}
    assert pk.errors == 0 and key(got) == key(ref) and got.total == ref.total
