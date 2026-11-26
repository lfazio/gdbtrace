"""Arbre d'appels (Trace.STATistic.TREE) : chemins d'appel de `TimingAnalyzer`, sur des
scénarios faits main puis sur les chemins réels de QEMU ; onglet de la vue Timing et
`gdbtrace.call_tree()`."""
import os

import pytest
from conftest import needs_qemu
from test_timing import CODE, FUNCS, FakeImage, FakeSymbols, S, run

from gdbtrace.core.symbols import Function, SymbolTable
from gdbtrace.trace.etm.image import DIRECT, INDIRECT
from gdbtrace.trace.etm.qemu_log import ExcEntry, ExcReturn
from gdbtrace.trace.timing import ELLIPSIS, MAX_DEPTH, TimingAnalyzer
from test_timing import I


def test_paths_of_nested_calls_and_interrupt():
    r = run([S(0x100, 1), S(0x102, 3), S(0x200, 2), S(0x202, 3), S(0x300, 1),
             ExcEntry(15, 0x500, cycles=12), S(0x500, 4), S(0x502, 3), ExcReturn(cycles=10),
             S(0x302, 3), S(0x206, 1), S(0x208, 3), S(0x106, 1)])
    t = r.tree
    assert set(t) == {("main",), ("main", "f"), ("main", "f", "g"), ("[SysTick]",),
                      ("[SysTick]", "handler")}
    assert (t[("main", "f", "g")].calls, t[("main", "f", "g")].gross) == (1, 4)   # IT déduite
    assert t[("[SysTick]",)].gross == 29 and t[("[SysTick]", "handler")].net == 7
    assert sum(x.net for x in t.values()) == r.total == 47
    assert [p for p, _ in r.children()] == [("[SysTick]",), ("main",)]      # brut 29, puis 18
    assert [p[-1] for p, _ in r.children(("main",))] == ["f"] and r.has_children(("main", "f"))


def test_same_function_on_two_paths():
    """f appelée depuis main, puis g depuis f et depuis main : deux nœuds g, sommés à plat."""
    code = CODE + [I(0x112, DIRECT, 4, 0x300, call=True), I(0x116)]
    a = TimingAnalyzer(FakeImage(code), FakeSymbols(FUNCS))
    r = a.analyze([S(0x100, 1), S(0x102, 1), S(0x200, 1), S(0x202, 1), S(0x300, 2), S(0x302, 1),
                   S(0x206, 1), S(0x208, 1), S(0x106, 1), S(0x112, 1), S(0x300, 5), S(0x302, 1),
                   S(0x116, 1)])
    g1, g2 = r.tree[("main", "f", "g")], r.tree[("main", "g")]
    assert (g1.calls, g1.gross, g2.calls, g2.gross) == (1, 3, 1, 6)
    assert (r.stats["g"].calls, r.stats["g"].gross) == (2, 9)


def test_depth_cap_folds_deep_recursion():
    """Récursion plus profonde que MAX_DEPTH : un nœud « … » sous le plafond."""
    rec = Function("rec", 0x700, 0x10)
    code = [I(0x700), I(0x702, DIRECT, 4, 0x700, call=True), I(0x706, INDIRECT, ret=True)]
    a = TimingAnalyzer(FakeImage(code), FakeSymbols([rec]))
    depth = MAX_DEPTH + 10
    events = [S(0x700, 1)]
    for _ in range(depth):
        events += [S(0x702, 1), S(0x700, 1)]
    events += [S(0x706, 1)] * (depth + 1)
    r = a.analyze(events)
    assert max(len(p) for p in r.tree) == MAX_DEPTH + 1
    assert any(p[-1] == ELLIPSIS for p in r.tree)
    assert sum(x.net for x in r.tree.values()) == sum(x.net for x in r.stats.values())


def test_node_cap():
    r = TimingAnalyzer(FakeImage(CODE), FakeSymbols(FUNCS), max_nodes=2).analyze(
        [S(0x100, 1), S(0x102, 3), S(0x200, 2), S(0x202, 3), S(0x300, 1), S(0x302, 3),
         S(0x206, 1), S(0x208, 3), S(0x106, 1)])
    assert ("main", "f", ELLIPSIS) in r.tree and ("main", "f", "g") not in r.tree
    assert sum(x.net for x in r.tree.values()) == r.total


@needs_qemu
@pytest.mark.parametrize("name", ["bare", "rtos"])
def test_tree_invariants_on_qemu_paths(qemu_paths, name, elf, rtos_elf):
    from gdbtrace.trace.etm.cycles import annotate
    img, events = qemu_paths[name]
    sym = SymbolTable(elf if name == "bare" else rtos_elf)
    r = TimingAnalyzer(img, sym).analyze(annotate(events, img))
    assert sum(x.net for x in r.tree.values()) == sum(x.net for x in r.stats.values())
    by_name: dict[str, list[int]] = {}
    for st in r.tree.values():
        acc = by_name.setdefault(st.name, [0, 0, 0])
        acc[0] += st.calls
        acc[1] += st.partial
        acc[2] += st.gross
    for n, st in r.stats.items():
        assert by_name[n] == [st.calls, st.partial, st.gross], n
    roots = [p[0] for p, _ in r.children()]
    assert "[SysTick]" in roots
    if name == "bare":
        assert ("Reset_Handler", "main", "update_sensor", "fib") in r.tree


@needs_qemu
def test_call_tree_api_and_view(elf, tmp_path):
    if not os.environ.get("DISPLAY"):
        pytest.skip("pas de DISPLAY")
    from test_gui import close_app, pump

    from gdbtrace.core.session import Session
    from gdbtrace.core.target import QemuTarget, free_port
    from gdbtrace.gui.app import App
    from gdbtrace.views.timing import TimingView
    a = App(layout_file=str(tmp_path / "layout.json"))
    try:
        s = Session()
        s.connect(elf, QemuTarget(elf=elf, gdb_port=free_port(), etm_sim=True))
        a.attach(s)
        v = a.views.show(TimingView)
        s.break_("fib")
        a.go()
        assert pump(a, lambda: a.current_frame is not None and a.current_frame.func == "fib")
        assert pump(a, lambda: a.trace.etm.stats["instructions"] > 100 and not a.trace.etm_busy)
        assert pump(a, lambda: v.result is not None and v.result.tree, timeout=15)
        roots = {v.calltree.item(i, "text"): i for i in v.calltree.get_children()}
        assert roots, roots
        main_root = next(i for name, i in roots.items() if not name.startswith("["))
        v.calltree.focus(main_root)
        v._open_node(main_root)                               # chargement paresseux
        kids = [v.calltree.item(i, "text") for i in v.calltree.get_children(main_root)]
        assert kids and "…" not in kids, kids
        name = v.calltree.item(main_root, "text")
        v.calltree.item(main_root, open=True)
        v._render_tree()                                       # nouvelle analyse : reste ouvert
        again = {v.calltree.item(i, "text"): i for i in v.calltree.get_children()}
        assert v.calltree.item(again[name], "open") and v.calltree.get_children(again[name])
        tree = a.api.call_tree(depth=3, min_pct=0)
        assert tree and all("enfants" in n for n in tree)
    finally:
        close_app(a)
