"""Test applicatif sur Nucleo-F746ZG : le GUI de GdbTrace, toutes ses vues, sur la vraie carte.

À passer à chaque release (`pytest --boards=require`). Reproductible : le firmware de test est
construit depuis ce dépôt (examples/nucleo-f746 : la démo FreeRTOS, à 16 MHz) et flashé par
le test ; la sonde est trouvée par tests/boards.py.

Une seule session, par le vrai point d'entrée (`gdbtrace.__main__.main`), avec ce qu'un
utilisateur demanderait : flashage, SVD, ETM et ITM par SWO (stall), trace de données d'une
variable. Trois phases : arrêt sur `worker_step`, marche (profil démarré), arrêt. À chaque
phase, le contenu de toutes les vues est relevé (tests/artifacts/board-f746.json) ; chaque
test vérifie ensuite une vue : un échec dit ce qui ne s'affiche pas.
"""
import os
import subprocess

import pytest
from conftest import ROOT, board_serial

import boards

FW_DIR = os.path.join(ROOT, "examples", "nucleo-f746")
FW = os.path.join(FW_DIR, "nucleo-f746.elf")
TASKS = {"producer", "consumer", "worker", "sleeper", "hog", "IDLE", "Tmr Svc"}

pytestmark = pytest.mark.skipif(not os.environ.get("DISPLAY"), reason="pas de DISPLAY (xvfb-run)")


@pytest.fixture(scope="module")
def run(request, tmp_path_factory):
    """La session GUI complète ; retourne {phase: {vue: contenu}} et le reste du relevé."""
    serial = board_serial(request, boards.NUCLEO_F746)
    svd = boards.find_svd(boards.NUCLEO_F746.svd)
    subprocess.run(["make", "-s", "-C", FW_DIR], check=True)

    def cleanup(app, s):                    # la carte sans ETM ni stall après le test
        from gdbtrace.trace.etm.config import TRCPRGCTLR, TRCSTALLCTLR
        for addr in (TRCPRGCTLR, TRCSTALLCTLR):
            s.write_u32(addr, 0)

    argv = [FW, "--openocd", boards.NUCLEO_F746.openocd_cfg, "--program", "--cpu-hz", "16000000",
            "--etm-config", "--etm-port", "swo", "--etm-stall", "--var-trace", "produced:write",
            "--layout", str(tmp_path_factory.mktemp("layout") / "layout.json")]
    if svd:
        argv += ["--svd", svd]
    old = os.environ.get("STLINK_SERIAL")
    os.environ["STLINK_SERIAL"] = serial
    try:
        return boards.run_gui(argv, "f746", ready=lambda app: app.trace.etm_swo, cleanup=cleanup)
    finally:
        if old is None:
            os.environ.pop("STLINK_SERIAL", None)
        else:
            os.environ["STLINK_SERIAL"] = old


def view(run, phase, kind):
    return run["phases"][phase][kind]


def col(rows, i):
    return [r["values"][i] for r in rows]


# ====================================================================== session
def test_session_and_no_error(run):
    assert run["events"] == {"connecté": True, "mise en route": True, "arrêt": True,
                             "arrêt final": True, "trace décodée": True}
    assert run["errors"] == []


def test_console_traces_the_setup(run):
    text = view(run, "arret", "console")["out"]
    assert "nucleo-f746.elf programmé" in text and "vérifié" in text
    assert "ETM par SWO (ETMv4)" in text and "stall" in text


def test_gui_stays_responsive(run):
    """Trace en marche (ETM, ITM, DWT par SWO saturé) puis décodage du retard : la fenêtre
    répond toujours (une itération de la boucle Tk : dessin, clics)."""
    slow = {p: t for p, t in run["max_update_s"].items() if t > 1.0}
    assert not slow, (run["max_update_s"], run["stalls"][:4])


# ====================================================================== à l'arrêt
def test_source_and_disassembly(run):
    src = view(run, "arret", "source")
    dis = view(run, "arret", "disasm")
    assert "worker_step" in src["text"] and "worker_step" in dis["text"]


def test_registers(run):
    rows = {r["text"].lower(): r["values"] for r in view(run, "arret", "registers")["tree"]}
    assert {"r0", "r12", "sp", "lr", "pc", "xpsr", "msp", "psp", "control", "fpscr"} <= set(rows)
    assert "Thread" in rows["xpsr"][2] or "Handler" in rows["xpsr"][2]      # xPSR décodé
    assert rows["pc"][2].startswith("<worker_step")


def test_stack_shows_file_and_line(run):
    frames = view(run, "arret", "stack")["frames"]
    assert frames[0]["values"][2] == "worker_step"
    assert frames[0]["values"][3].startswith("main.c:")                # ligne visible


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
    assert "STM32F746" in per["dev_label"]
    assert {"RCC", "GPIOB", "TIM2"} <= {r["text"] for r in per["tree"]}


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


def test_itm_text(run):
    assert "consumer: commande" in view(run, "apres", "itm")["out"]


def test_profile_by_swo(run):
    funcs = {r["text"] for r in view(run, "apres", "profile")["funcs"]}
    assert funcs & {"worker_step", "<sommeil>", "prvIdleTask", "vApplicationIdleHook"}, funcs


def test_variable_traced_by_dwt(run):
    rows = view(run, "apres", "vartrace")["tree"]
    produced = next(r for r in rows if r["text"] == "produced")
    assert int(produced["values"][1]) > 0                               # échantillons


def test_task_time_by_itm(run):
    rows = {r["text"] for r in view(run, "apres", "tasktime")["tree"]}
    assert {"worker", "consumer", "IDLE"} <= rows, rows


# ====================================================================== trace ETM, après l'arrêt
def test_trace_listing(run):
    rows = view(run, "apres", "trace")["listing"]
    assert len(rows) > 100
    assert any(r["values"][2] for r in rows)                            # fonctions résolues


def test_timing(run):
    funcs = {r["text"] for r in view(run, "apres", "timing")["table"]}
    assert funcs & {"worker_step", "producer_task", "consumer_task", "[SysTick]"}, funcs


def test_coverage(run):
    rows = {r["text"]: r["values"] for r in view(run, "apres", "coverage")["tree"]}
    # chemins relatifs au projet : les fichiers du dépôt, pas des chemins absolus
    assert {"freertos/main.c", "freertos/FreeRTOS-Kernel/tasks.c", "nucleo-f746/startup.c"} <= set(rows)
    for f in ("freertos/main.c", "freertos/FreeRTOS-Kernel/tasks.c"):
        covered, total = (int(x) for x in rows[f][3].split("/"))
        assert 0 < covered <= total, (f, rows[f])
    assert "capture ETM" in view(run, "apres", "coverage")["info"]
