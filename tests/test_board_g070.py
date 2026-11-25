"""Test applicatif sur Nucleo-G070RB (STM32G070RB, Cortex-M0+) : le GUI de GdbTrace, toutes ses
vues, sur la vraie carte, par OpenOCD.

À passer à chaque release (`pytest --boards=require`). Reproductible : le firmware de test est
construit depuis ce dépôt (examples/nucleo-g070 : la démo FreeRTOS, port ARM_CM0, à 16 MHz)
et flashé par le test ; la sonde est trouvée par tests/boards.py.

Le M0+ du STM32G0 n'a ni ITM, ni SWO, ni ETM, ni MTB (RM0444 §40) : la session n'a aucune
trace, et les vues de trace doivent dire pourquoi. Tout le reste est vérifié : vues à l'arrêt,
vues en direct (mémoire lue par OpenOCD, cible en marche), profil par DWT_PCSR, RTOS (cadre
de pile du port ARM_CM0), et le run-control qui use des 4 breakpoints et 2 watchpoints du
cœur (`extra`). Mêmes phases que la F746 ; le relevé est dans tests/artifacts/board-g070.json.
"""
import os
import subprocess
import time

import pytest
from conftest import ROOT, board_serial

import boards

FW_DIR = os.path.join(ROOT, "examples", "nucleo-g070")
FW = os.path.join(FW_DIR, "nucleo-g070.elf")
TASKS = {"producer", "consumer", "worker", "sleeper", "hog", "IDLE", "Tmr Svc"}
NO_TRACE = "Cortex-M0+ (ARMv6-M) : ni ITM, ni SWO, ni ETM"

pytestmark = pytest.mark.skipif(not os.environ.get("DISPLAY"), reason="pas de DISPLAY (xvfb-run)")


def run_control(app) -> dict:
    """Run-control sur la carte, cible arrêtée au départ : breakpoint à compteur (gdb ignore
    les 2 premiers passages), breakpoint à message (arrêts internes : un message par passage,
    sans arrêt visible), Go.Till sur une globale (watchpoint du DWT). Chaque essai relève son
    résultat, ou son erreur."""
    s = app.session
    out: dict = {}

    def attempt(name, fn):
        try:
            out[name] = fn()
        except Exception as exc:  # noqa: BLE001 - un essai en échec n'empêche pas les suivants
            out[name] = {"erreur": f"{type(exc).__name__}: {exc}"}
            if s.state == "running":
                s.halt()
                s.wait_stopped(10)

    def counted():
        bp = s.break_("worker_step", count=3)
        s.go()
        s.wait_stopped(10)
        hits = next(b.hits for b in s.breakpoints() if b.number == bp.number)
        func = s.frame().func
        s.delete_break(bp.number)
        return {"hits": hits, "func": func}

    def logged():
        msgs: list[str] = []
        s.on_event(lambda kind, data: msgs.append(data["text"].strip())
                   if kind == "output" and data.get("stream") == "action" else None)
        bp = s.break_("worker_step", log="worked={worked}")
        s.go()
        time.sleep(1.5)
        state = s.state                                 # « running » : arrêts internes invisibles
        s.halt()
        s.wait_stopped(10)
        s.delete_break(bp.number)
        values = [int(m.split("worked=")[1].split()[0]) for m in msgs if "worked=" in m]
        return {"messages": msgs[:5], "n": len(values), "etat": state,
                "ecarts": sorted({b - a for a, b in zip(values, values[1:])})}

    def till():
        target = int(s.evaluate("produced")) + 3
        s.go_till(f"produced >= {target}")
        reason = s.wait_stopped(10)
        return {"produced": int(s.evaluate("produced")), "attendu": target,
                "func": s.frame().func, "raison": (reason or {}).get("reason")}

    attempt("compteur", counted)
    attempt("message", logged)
    attempt("go_till", till)
    return out


@pytest.fixture(scope="module")
def run(request, tmp_path_factory):
    """La session GUI complète ; retourne {phase: {vue: contenu}} et le reste du relevé."""
    serial = board_serial(request, boards.NUCLEO_G070)
    svd = boards.find_svd(boards.NUCLEO_G070.svd)
    subprocess.run(["make", "-s", "-C", FW_DIR], check=True)
    argv = [FW, "--openocd", boards.NUCLEO_G070.openocd_cfg, "--program", "--cpu-hz", "16000000",
            "--layout", str(tmp_path_factory.mktemp("layout") / "layout.json")]
    if svd:
        argv += ["--svd", svd]
    old = os.environ.get("STLINK_SERIAL")
    os.environ["STLINK_SERIAL"] = serial
    try:
        return boards.run_gui(argv, "g070", ready=lambda app: True, extra=run_control)
    finally:
        if old is None:
            os.environ.pop("STLINK_SERIAL", None)
        else:
            os.environ["STLINK_SERIAL"] = old


def view(run, phase, kind):
    return run["phases"][phase][kind]


# ====================================================================== session
def test_session_and_no_error(run):
    assert run["events"] == {"connecté": True, "mise en route": True, "arrêt": True,
                             "arrêt final": True, "trace décodée": True}
    assert run["errors"] == []


def test_console_traces_the_setup(run):
    text = view(run, "arret", "console")["out"]
    assert "nucleo-g070.elf programmé" in text and "vérifié" in text


def test_gui_stays_responsive(run):
    slow = {p: t for p, t in run["max_update_s"].items() if t > 1.0}
    assert not slow, (run["max_update_s"], run["stalls"][:4])


# ====================================================================== à l'arrêt
def test_source_and_disassembly(run):
    src = view(run, "arret", "source")
    dis = view(run, "arret", "disasm")
    assert "worker_step" in src["text"] and "worker_step" in dis["text"]


def test_registers(run):
    """Registres d'un ARMv6-M : pas de FPU."""
    rows = {r["text"].lower(): r["values"] for r in view(run, "arret", "registers")["tree"]}
    assert {"r0", "r12", "sp", "lr", "pc", "xpsr", "msp", "psp", "primask", "control"} <= set(rows)
    assert "fpscr" not in rows and "s0" not in rows
    assert "Thread" in rows["xpsr"][2] or "Handler" in rows["xpsr"][2]
    assert rows["pc"][2].startswith("<worker_step")


def test_stack_shows_file_and_line(run):
    frames = view(run, "arret", "stack")["frames"]
    assert frames[0]["values"][2] == "worker_step"
    assert frames[0]["values"][3].startswith("main.c:")


def test_watch_values(run):
    rows = {r["text"]: r["values"] for r in view(run, "arret", "watch")["tree"]}
    assert {"produced", "consumed", "worked"} <= set(rows)
    assert all(rows[n][0].isdigit() for n in ("produced", "consumed", "worked"))


def test_breakpoint_listed(run):
    rows = view(run, "arret", "breakpoints")["tree"]
    assert any("worker_step" in " ".join(r["values"]) for r in rows)


def test_rtos_tasks_and_stacks(run):
    rtos = view(run, "arret", "rtos")
    tasks = {r["text"]: r["values"] for r in rtos["tasks"]}
    assert TASKS <= set(tasks), tasks.keys()
    for name, v in tasks.items():                                       # « utilisée/taille »
        used, size = (int(x.lstrip("~")) for x in v[2].split("/"))
        assert 0 < used <= size, (name, v)
    assert tasks["worker"][0] == "Running"


def test_peripherals_from_svd(run):
    per = view(run, "arret", "peripherals")
    assert "STM32G070" in per["dev_label"]
    assert {"RCC", "GPIOA", "TIM3"} <= {r["text"] for r in per["tree"]}


def test_memory_dump(run):
    assert len(view(run, "arret", "memory")["text"].splitlines()) >= 8


def test_threads(run):
    rows = view(run, "arret", "threads")["tree"]
    assert len(rows) == 1 and "worker_step" in rows[0]["values"][4]


# ====================================================================== en marche
def test_watch_live(run):
    before = {r["text"]: r["values"][0] for r in view(run, "arret", "watch")["tree"]}
    live = {r["text"]: r["values"][0] for r in view(run, "marche", "watch")["tree"]}
    assert int(live["consumed"]) > int(before["consumed"])


def test_rtos_live(run):
    tick = lambda p: int(view(run, p, "rtos")["info"].split("tick ")[1].split()[0])  # noqa: E731
    assert tick("marche") > tick("arret")


def test_profile_by_dwt_pcsr(run):
    """Sans SWO, le profil échantillonne DWT_PCSR par OpenOCD, cible en marche (« Live »)."""
    assert "Live" in view(run, "marche", "profile")["info"]
    funcs = {r["text"] for r in view(run, "apres", "profile")["funcs"]}
    assert funcs & {"worker_step", "prvIdleTask", "vApplicationIdleHook", "<sommeil>"}, funcs


# ====================================================================== trace : le cœur n'en a pas
@pytest.mark.parametrize("kind,label", [("itm", "status"), ("trace", "info"), ("coverage", "info"),
                                        ("vartrace", "info"), ("tasktime", "info")])
def test_trace_views_say_why(run, kind, label):
    assert NO_TRACE in view(run, "apres", kind)[label]
    assert NO_TRACE in run["trace_status"]


# ====================================================================== run-control
def test_count_breakpoint(run):
    assert run["extra"]["compteur"] == {"hits": 3, "func": "worker_step"}


def test_log_breakpoint_internal_stops(run):
    """Un message par passage, sans arrêt visible : pas un passage perdu ni compté deux fois
    (`worked` avance de 1 entre deux messages)."""
    m = run["extra"]["message"]
    assert m.get("etat") == "running" and m.get("n", 0) >= 5 and m.get("ecarts") == [1], m


def test_go_till_watchpoint(run):
    g = run["extra"]["go_till"]
    assert g.get("produced") == g.get("attendu") and g.get("func") == "producer_task", g
