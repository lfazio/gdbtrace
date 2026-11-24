"""Breakpoints à action et à compteur (Break.Set /COUNT, /CMD), Go.Till, Step.Till, Step.Change.

Les actions s'exécutent sur un arrêt interne (aucun événement, la cible « tourne » vue de
l'extérieur) ; les boucles de pas sont une seule reprise (un `running`, un `stopped`)."""
import threading
import time

import pytest
from conftest import needs_qemu, source_line

from gdbtrace.core.scripting import ScriptAPI, StopRecord
from gdbtrace.core.session import expand_log

B_LINE = source_line("b = t;")


def events(session):
    seen = []
    session.on_event(lambda kind, _d: seen.append(kind) if kind in ("running", "stopped") else None)
    return seen


def raw_breaks(session):
    return session.gdb.command("-break-list")["BreakpointTable"].get("body", [])


def test_expand_log():
    vals = {"x": "10", "a ? b : c": "2", "p->q": "5", "a?b": "7"}

    def value(expr, fmt):
        if expr == "boom":
            raise RuntimeError("No symbol \"boom\"")
        return vals[expr] if fmt == "natural" else f"{vals[expr]}/{fmt}"
    assert expand_log("x={x:hex} t={a ? b : c} {{lit}} {p->q}", value) == "x=10/hexadecimal t=2 {lit} 5"
    assert expand_log("{x:dec}|{x:b}|{x: hex}", value).split("|")[:2] == ["10/decimal", "10/binary"]
    assert expand_log("{boom}", value) == "<erreur : No symbol \"boom\">"
    assert expand_log("sans champ", value) == "sans champ"


@needs_qemu
def test_count(session):
    api = ScriptAPI(session)
    b = api.break_("animate", count=4)
    bp = api.breaks()[0]
    assert (bp.count, bp.ignore, bp.hits) == (4, 3, 0)
    api.go()
    api.wait(10)
    assert api.eval("counter") == "4"
    bp = api.breaks()[0]
    assert (bp.number, bp.hits, bp.ignore) == (b.number, 4, 0)
    api.go()
    api.wait(5)                                     # compteur épuisé : arrêt à chaque passage
    assert api.eval("counter") == "5"
    api.break_options(b.number, count=None)
    assert api.breaks()[0].count is None


@needs_qemu
def test_condition_options(session):
    api = ScriptAPI(session)
    b = api.break_("animate")
    api.break_options(b.number, condition="tick == 3")
    assert api.breaks()[0].condition == "tick == 3"
    api.go()
    api.wait(10)
    assert api.eval("tick") == "3"
    api.break_options(b.number, condition="")
    assert api.breaks()[0].condition is None


@needs_qemu
def test_log_action_continues(session):
    api = ScriptAPI(session)
    seen = events(session)
    out, console = [], []
    session.on_event(lambda k, d: out.append(d["text"]) if k == "output" and d.get("stream") == "action" else None)
    session.on_event(lambda k, d: console.append(d["text"]) if k == "output" and d.get("stream") == "console" else None)
    b = api.break_("update_sensor", log="n={counter} tick={tick:hex}")
    stop = api.break_("animate", condition="counter == 6")
    api.go()
    rec = api.wait(10)
    assert rec["bkptno"] == stop.number
    expected = [f"n={i} tick={i:#x}" for i in range(1, 7)]
    assert api.break_log() == expected and out == [e + "\n" for e in expected]
    assert seen == ["running", "stopped"]           # arrêts internes invisibles
    text = "".join(console)                         # bannière gdb : arrêt visible seulement
    assert f"Breakpoint {stop.number}, animate" in text and "update_sensor" not in text
    bp = next(x for x in api.breaks() if x.number == b.number)
    assert bp.hits == 6 and "n={counter}" in bp.action and "continue" in bp.action
    assert api.break_log(2) == expected[-2:]


@needs_qemu
def test_python_action_decides(session):
    api = ScriptAPI(session)
    seen = []

    def action(arret):
        assert isinstance(arret, StopRecord) and arret["frame"]["func"] == "animate"
        tick = int(api.eval("tick", "dec"))
        seen.append(tick)
        if tick == 3:
            return f"tick={tick}"                   # un texte : journalisé, la cible repart
        return tick == 7                            # vrai : arrêt
    api.break_("animate", action=action)
    api.go()
    rec = api.wait(10)
    assert rec["frame"]["func"] == "animate" and api.eval("tick") == "7"
    assert seen == list(range(1, 8)) and api.break_log() == ["tick=3"]
    assert "script Python" in api.breaks()[0].action


@needs_qemu
def test_action_error_stops_visibly(session):
    api = ScriptAPI(session)
    out = []
    session.on_event(lambda k, d: out.append(d) if k == "output" and d.get("stream") == "action" else None)
    api.break_("animate", action=lambda _r: 1 / 0)
    api.go()
    rec = api.wait(5)
    assert rec["frame"]["func"] == "animate" and session.state == "stopped"
    assert out and out[-1].get("error") and "ZeroDivisionError" in out[-1]["text"]


@needs_qemu
def test_halt_during_actions(session):
    api = ScriptAPI(session)
    api.break_("fib", log="{n}")
    for _ in range(3):
        api.go()
        time.sleep(0.3)
        api.halt()
        assert session.state == "stopped" and not session.internal_stop
    assert len(api.break_log(None)) > 3


@needs_qemu
def test_watchpoint_action(session):
    api = ScriptAPI(session)
    w = api.watch("counter")
    api.break_options(w, log="counter={counter}")
    api.break_("animate", condition="counter == 3")
    api.go()
    rec = api.wait(10)
    assert rec["frame"]["func"] == "animate"
    assert api.break_log() == ["counter=1", "counter=2", "counter=3"]
    assert next(b for b in api.breaks() if b.number == w).expr == "counter"


@needs_qemu
def test_go_till(session):
    api = ScriptAPI(session)
    seen = events(session)
    api.go_till("counter >= 20")
    rec = api.wait(10)
    assert api.eval("counter") == "20" and rec["reason"] == "watchpoint-trigger"
    assert seen == ["running", "stopped"]
    assert api.breaks() == [] and len(raw_breaks(session)) == 1     # masqué jusqu'à la reprise
    api.step()
    assert raw_breaks(session) == []
    with pytest.raises(ValueError, match="step_till"):
        api.go_till("$r0 == 5")                     # registre : watchpoint logiciel refusé
    assert raw_breaks(session) == [] and session.state == "stopped"


@needs_qemu
def test_step_change_and_till(session):
    api = ScriptAPI(session)
    api.break_("fib", condition="n == 5")
    api.go()
    api.wait(10)
    for b in api.breaks():
        api.delete(b.number)
    api.over()                                      # a = 0, b = 1
    seen = events(session)
    rec = api.step_change("a")
    assert (rec["old"], rec["new"], rec["met"]) == ("0", "1", True)
    assert rec["frame"]["line"] == str(B_LINE) and rec["steps"] >= 2
    assert seen == ["running", "stopped"] and session.state == "stopped"
    assert "Step.Change" in repr(rec) and "0 → 1" in repr(rec)
    rec = api.step_till("i == 3")
    assert rec["met"] and api.eval("i") == "3"
    rec = api.step_change("$r3", asm=True)
    assert rec["met"] and rec["old"] != rec["new"]
    rec = api.step_till("0", max_steps=5)
    assert (rec["met"], rec["steps"]) == (False, 5) and "limite" in repr(rec)


@needs_qemu
def test_halt_during_step_loop(session):
    api = ScriptAPI(session)
    api.break_("fib")
    api.go()
    api.wait(10)
    api.delete(api.breaks()[0].number)
    res = {}
    th = threading.Thread(target=lambda: res.setdefault("r", api.step_till("0", max_steps=10**6)))
    th.start()
    time.sleep(0.5)
    assert session.state == "running"
    api.halt()
    th.join(10)
    assert not th.is_alive() and res["r"]["met"] is False and 0 < res["r"]["steps"] < 10**6
    assert session.state == "stopped"
    api.step()                                      # la session reste utilisable
    assert session.state == "stopped"
