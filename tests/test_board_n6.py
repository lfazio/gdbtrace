"""Test applicatif sur Nucleo-N657X0-Q (STM32N6, Cortex-M55) : le GUI de GdbTrace, toutes ses
vues, sur la vraie carte, par ST-LINK_gdbserver.

À passer à chaque release (`pytest --boards=require`). Reproductible : le firmware de test est
construit depuis ce dépôt (examples/nucleo-n6 : la démo FreeRTOS à 64 MHz, plus des boucles
ARMv8.1-M à faible coût et Helium dans le tick) et chargé en RAM par le test (démarrage de
développement) ; la sonde est trouvée par tests/boards.py.

Une session par le vrai point d'entrée : ST-LINK_gdbserver derrière le relais RSP (mémoire
en direct), SVD, ITM par le SWO (servi par ST-LINK_gdbserver), ETM écrit en RAM par l'ETR,
trace de données d'une variable (DWT de l'ARMv8-M). Mêmes phases que la F746 ; le relevé est
dans tests/artifacts/board-n6.json.
"""
import os
import subprocess

import pytest
from conftest import ROOT, board_serial

import boards

FW_DIR = os.path.join(ROOT, "examples", "nucleo-n6")
FW = os.path.join(FW_DIR, "nucleo-n6.elf")
TASKS = {"producer", "consumer", "worker", "sleeper", "hog", "IDLE", "Tmr Svc"}

pytestmark = pytest.mark.skipif(not os.environ.get("DISPLAY"), reason="pas de DISPLAY (xvfb-run)")


def loop_counts(app) -> dict:
    """Tours de la boucle LE de `lob_count` à chaque appel, d'après la trace décodée : le
    firmware l'appelle à chaque tick avec n = 1 + (tick % 64), d'un tour de plus à chaque
    fois. Vérifie le décodage des boucles à faible coût sur une vraie trace."""
    from gdbtrace.trace.etm.flow import Step
    etm = app.trace.etm
    sym = app.symbols
    f = sym.function("lob_count")
    le = next(i for a in range(f.start, f.end, 2)
              if (i := etm.image.instr(a)) is not None and i.op == "le")
    counts, cur = [], None
    for e in etm.last(len(etm.history)):
        if not isinstance(e, Step):
            continue
        if e.addr == f.start:
            if cur is not None:
                counts.append(cur)
            cur = 0
        elif e.addr == le.target and cur is not None:
            cur += 1
    return {"lob_count": counts, "le": f"{le.addr:#x} → {le.target:#x}"}


@pytest.fixture(scope="module")
def run(request, tmp_path_factory):
    serial = board_serial(request, boards.NUCLEO_N6)
    svd = boards.find_svd(boards.NUCLEO_N6.svd)
    subprocess.run(["make", "-s", "-C", FW_DIR], check=True)

    def cleanup(app, s):                    # la carte sans ETM après le test
        from gdbtrace.trace.etm.config import TRCPRGCTLR
        s.write_u32(TRCPRGCTLR, 0)

    argv = [FW, "--stlink", serial, "--stlink-ap", str(boards.NUCLEO_N6.apid), "--ram-boot",
            "--program", "--cpu-hz", "64000000", "--swo-config", "--etm-config", "--etm-port", "etr",
            "--var-trace", "produced:write",
            "--layout", str(tmp_path_factory.mktemp("layout") / "layout.json")]
    if svd:
        argv += ["--svd", svd]
    return boards.run_gui(argv, "n6", ready=lambda app: app.trace.tmc is not None,
                          cleanup=cleanup, extra=loop_counts)


def view(run, phase, kind):
    return run["phases"][phase][kind]


# ====================================================================== session
def test_session_and_no_error(run):
    assert run["events"] == {"connecté": True, "mise en route": True, "arrêt": True,
                             "arrêt final": True, "trace décodée": True}
    assert run["errors"] == []


def test_console_traces_the_setup(run):
    text = view(run, "arret", "console")["out"]
    assert "nucleo-n6.elf programmé" in text
    assert "capture SWO de ST-LINK_gdbserver" in text and "CODR=3" in text
    assert "ETR : tampon circulaire de 256 Ko" in text and "ETM (ETMv4) vers l'ETR" in text


def test_gui_stays_responsive(run):
    slow = {p: t for p, t in run["max_update_s"].items() if t > 1.0}
    assert not slow, (run["max_update_s"], run["stalls"][:4])


# ====================================================================== à l'arrêt
def test_source_and_disassembly(run):
    assert "worker_step" in view(run, "arret", "source")["text"]
    assert "worker_step" in view(run, "arret", "disasm")["text"]


def test_registers(run):
    rows = {r["text"].lower(): r["values"] for r in view(run, "arret", "registers")["tree"]}
    assert {"r0", "r12", "sp", "lr", "pc", "xpsr", "msp", "psp", "fpscr"} <= set(rows), rows.keys()
    assert "Thread" in rows["xpsr"][2] or "Handler" in rows["xpsr"][2]
    assert rows["pc"][2].startswith("<worker_step")


def test_stack_shows_file_and_line(run):
    frames = view(run, "arret", "stack")["frames"]
    assert frames[0]["values"][2] == "worker_step"
    assert frames[0]["values"][3].startswith("main.c:")


def test_watch_values(run):
    rows = {r["text"]: r["values"] for r in view(run, "arret", "watch")["tree"]}
    assert all(rows[n][0].isdigit() for n in ("produced", "consumed", "worked"))


def test_rtos_tasks_and_stacks(run):
    tasks = {r["text"]: r["values"] for r in view(run, "arret", "rtos")["tasks"]}
    assert TASKS <= set(tasks), tasks.keys()
    for name, v in tasks.items():
        used, size = (int(x.lstrip("~")) for x in v[2].split("/"))
        assert 0 < used <= size, (name, v)
    assert tasks["worker"][0] == "Running"


def test_peripherals_from_svd(run):
    per = view(run, "arret", "peripherals")
    assert "STM32N657" in per["dev_label"]
    assert {"RCC", "GPIOB", "TIM2"} <= {r["text"] for r in per["tree"]}


def test_memory_dump(run):
    assert len(view(run, "arret", "memory")["text"].splitlines()) >= 8


# ====================================================================== en marche (relais RSP)
def test_watch_live(run):
    before = {r["text"]: r["values"][0] for r in view(run, "arret", "watch")["tree"]}
    live = {r["text"]: r["values"][0] for r in view(run, "marche", "watch")["tree"]}
    assert int(live["consumed"]) > int(before["consumed"])


def test_rtos_live(run):
    tick = lambda p: int(view(run, p, "rtos")["info"].split("tick ")[1].split()[0])  # noqa: E731
    assert tick("marche") > tick("arret")


# ====================================================================== ITM par le SWO
def test_itm_text(run):
    assert "consumer: commande" in view(run, "apres", "itm")["out"]


def test_profile_by_swo(run):
    funcs = {r["text"] for r in view(run, "apres", "profile")["funcs"]}
    assert funcs & {"worker_step", "prvTaskCheckFreeStackSpace", "prvIdleTask"}, funcs


def test_variable_traced_by_dwt_v8m(run):
    rows = view(run, "apres", "vartrace")["tree"]
    produced = next(r for r in rows if r["text"] == "produced")
    assert int(produced["values"][1]) > 0


def test_task_time_by_itm(run):
    rows = {r["text"] for r in view(run, "apres", "tasktime")["tree"]}
    assert {"worker", "consumer", "IDLE"} <= rows, rows


# ====================================================================== ETM par l'ETR
def test_trace_listing(run):
    rows = view(run, "apres", "trace")["listing"]
    assert len(rows) > 100 and any(r["values"][2] for r in rows)


def test_trace_status_without_errors(run):
    assert "0 erreurs" in run["trace_status"], run["trace_status"]


def test_low_overhead_loops_decoded(run):
    """LE est un branchement : le nombre de tours relevé dans la trace suit le firmware."""
    counts = run["extra"]["lob_count"]
    assert len(counts) > 10, run["extra"]
    assert all((b - a) % 64 == 1 for a, b in zip(counts, counts[1:])), counts


def test_timing(run):
    funcs = {r["text"] for r in view(run, "apres", "timing")["table"]}
    assert funcs & {"worker_step", "lob_count", "[SysTick]"}, funcs


def test_coverage(run):
    rows = {r["text"]: r["values"] for r in view(run, "apres", "coverage")["tree"]}
    assert {"freertos/main.c", "nucleo-n6/lob.c", "nucleo-n6/startup.c"} <= set(rows), rows.keys()
    for f in ("freertos/main.c", "nucleo-n6/lob.c"):
        covered, total = (int(x) for x in rows[f][3].split("/"))
        assert 0 < covered <= total, (f, rows[f])
    assert "ETM par l'ETR" in view(run, "apres", "coverage")["info"]
