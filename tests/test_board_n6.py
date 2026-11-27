"""Test applicatif sur Nucleo-N657X0-Q (STM32N6, Cortex-M55) : le GUI de GdbTrace, toutes ses
vues, sur la vraie carte, par ST-LINK_gdbserver.

À passer à chaque release (`pytest --boards=require`). Reproductible : le firmware de test est
construit depuis ce dépôt (examples/nucleo-n6 : la démo FreeRTOS à 64 MHz, plus des boucles
ARMv8.1-M à faible coût et Helium dans le tick) et chargé en RAM par le test (démarrage de
développement) ; la sonde est trouvée par tests/boards.py.

Une session par le vrai point d'entrée : ST-LINK_gdbserver derrière le relais RSP (mémoire
en direct), SVD, ITM par le SWO (servi par ST-LINK_gdbserver), ETM écrit en RAM par l'ETR
(cycle-accurate et horodatage), trace de données d'une variable (DWT de l'ARMv8-M). Mêmes phases que la F746 ; le relevé est
dans tests/artifacts/board-n6.json.
"""
import os
import subprocess

import pytest
from conftest import ROOT, board_serial

import boards

FW_DIR = os.path.join(ROOT, "examples", "nucleo-n6")
FW = os.path.join(FW_DIR, "nucleo-n6.elf")
CONFIG = os.path.join(ROOT, "configs", "nucleo-n657x0-q.yaml")
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
    subprocess.run(["make", "-s", "-C", FW_DIR], check=True)

    def cleanup(app, s):                    # la carte sans ETM après le test
        from gdbtrace.trace.etm.config import TRCPRGCTLR
        s.write_u32(TRCPRGCTLR, 0)

    # la session de configs/nucleo-n657x0-q.yaml (port d'accès 1, image en RAM, ITM par le SWO,
    # ETM par l'ETR, une variable tracée), sur la sonde trouvée
    argv = ["--config", CONFIG, "--stlink", serial, "--etm-cycle-accurate", "--etm-timestamps",
            "--layout", str(tmp_path_factory.mktemp("layout") / "layout.json")]
    return boards.run_gui(argv, "n6", ready=lambda app: app.trace.tmc is not None,
                          cleanup=cleanup,
                          extra=lambda app: {**loop_counts(app), "chrono": boards.chrono_passes(app),
                                             "rtt": boards.rtt_live(app),
                                             "compteurs": boards.counters_passes(app),
                                             "systeme": boards.system_passes(app),
                                             "flash": n6_external_flash(app)})


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


def test_cycle_accurate_timing_and_timestamps(run):
    """Durées mesurées par l'ETM (paquets Cycle Count), horodatages dans Trace.List."""
    timing = " ".join(str(v) for v in view(run, "apres", "timing").values() if isinstance(v, str))
    assert "mesuré" in timing, timing[:300]
    labels = [r["values"][1] for r in view(run, "apres", "trace")["listing"]]
    assert any(lb.startswith("── horodatage ") for lb in labels)


FILTER_SCRIPT = r'''
import json, time
from gdbtrace.trace.etm import qemu_log as ql
g = gdbtrace
tm = g.trace
f = g._symbols().function("worker_step")
lo, hi = f.start & ~1, f.end
ret = max(a for a in range(lo, hi, 2) if tm.etm.image.instr(a) and tm.etm.image.instr(a).ret)
g.go(); time.sleep(0.15); g.halt(); tm.wait_etm(60)
ev = tm.etm.last(len(tm.etm.history))
steps = [e for e in ev if isinstance(e, ql.Step)]
calls, cur, bad = [], None, 0
for e in ev:
    if isinstance(e, ql.TraceGap):
        bad += cur is not None
        cur = None
    elif isinstance(e, ql.Step):
        if e.addr == lo:
            cur = 1
        elif cur is not None:
            cur += 1
            if e.addr == ret:
                calls.append(cur)
                cur = None
print("RESULT " + json.dumps({"n": len(steps), "inside": sum(lo <= s.addr < hi for s in steps),
                             "calls": calls, "incomplete": bad, "status": tm.trace_status()}))
'''


def test_etm_filter_by_dwt_on_board(request, tmp_path, run):
    """L'ETM du M55 n'a pas de comparateurs d'adresse : --etm-filter passe par le DWT. Seul
    worker_step est tracé, chaque appel en entier (23 instructions de plus à chaque appel)."""
    import json
    import sys
    serial = board_serial(request, boards.NUCLEO_N6)
    script = tmp_path / "filtre.py"
    script.write_text(FILTER_SCRIPT)
    r = subprocess.run([sys.executable, "-m", "gdbtrace", FW, "--stlink", serial, "--stlink-ap", "1",
                        "--ram-boot", "--program", "--cpu-hz", "64000000", "--etm-config",
                        "--etm-port", "etr", "--etm-filter", "worker_step", "--batch", "--script",
                        str(script)], capture_output=True, text=True, timeout=240, cwd=ROOT)
    line = next((ln for ln in r.stdout.splitlines() if ln.startswith("RESULT ")), None)
    assert line, r.stdout[-2000:] + r.stderr[-2000:]
    res = json.loads(line[7:])
    assert res["n"] > 0 and res["inside"] == res["n"], res
    assert res["incomplete"] == 0 and len(res["calls"]) >= 3, res
    assert all((b - a) % (24 * 23) == 23 for a, b in zip(res["calls"], res["calls"][1:])), res


STREAM_SCRIPT = r'''
import collections, json, time
from gdbtrace.trace.etm import qemu_log as ql
g = gdbtrace
tm = g.trace
tm.etm.history = collections.deque(maxlen=50_000_000)       # toute l'exécution
f = g._symbols().function("lob_count")
le = next(i for a in range(f.start, f.end, 2) if (i := tm.etm.image.instr(a)) is not None and i.op == "le")
g.go(); time.sleep(0.2); g.halt(); tm.wait_etm(60)
tm.etm.history.clear()
s0 = tm.tmc.streamed
g.go(); time.sleep(3.0); g.halt(); tm.wait_etm(180)
counts, cur = [], None
for e in tm.etm.history:
    if isinstance(e, ql.Step):
        if e.addr == f.start:
            if cur is not None:
                counts.append(cur)
            cur = 0
        elif e.addr == le.target and cur is not None:
            cur += 1
print("RESULT " + json.dumps({"counts": counts, "streamed": tm.tmc.streamed - s0, "size": tm.tmc.size,
                             "gaps": tm.etm.gaps, "errors": tm.etm.stats["erreurs"],
                             "status": tm.trace_status()}))
'''


def test_etr_read_while_running_is_complete(request, tmp_path, run):
    """ETR lu en continu par le relais : 3 s d'exécution, deux fois ce que tient le tampon, et
    pas un tick perdu (la boucle LE de lob_count fait un tour de plus à chaque tick)."""
    import json
    import sys
    serial = board_serial(request, boards.NUCLEO_N6)
    script = tmp_path / "continu.py"
    script.write_text(STREAM_SCRIPT)
    r = subprocess.run([sys.executable, "-m", "gdbtrace", FW, "--stlink", serial, "--stlink-ap", "1",
                        "--ram-boot", "--program", "--cpu-hz", "64000000", "--etm-config",
                        "--etm-port", "etr", "--batch", "--script", str(script)],
                       capture_output=True, text=True, timeout=300, cwd=ROOT)
    line = next((ln for ln in r.stdout.splitlines() if ln.startswith("RESULT ")), None)
    assert line, r.stdout[-2000:] + r.stderr[-2000:]
    res = json.loads(line[7:])
    counts = res["counts"]
    assert res["streamed"] > 1.5 * res["size"] and res["gaps"] == 0 and res["errors"] == 0, res["status"]
    assert len(counts) > 2500, len(counts)                    # tick de 1 ms
    assert all((b - a) % 64 == 1 for a, b in zip(counts, counts[1:]))


def test_counters_of_the_pmu(run):
    """PMU de l'ARMv8.1-M (Cortex-M55) : instructions retirées, moins que les cycles."""
    boards.counters_checks(run["extra"]["compteurs"], "PMU")
    assert run["extra"]["compteurs"]["mesure"]["instructions"] > 0


def test_rtt_read_live_by_the_relay(run):
    """RTT lu en marche par le relais RSP (paquets m/M glissés entre ceux de gdb)."""
    boards.rtt_checks(run["extra"]["rtt"])


def test_chrono_by_cyccnt(run):
    """Deux passages par worker_step (vTaskDelay de 5 ms) mesurés par DWT_CYCCNT ; la pause de
    1 s du débogueur entre les deux n'est pas comptée."""
    boards.chrono_checks(run["extra"]["chrono"], "DWT_CYCCNT")


N6_LOADER = "MX66UW1G45G_STM32N6570-NUCLEO.stldr"


def n6_external_flash(app) -> dict:
    """64 Ko de la flash externe XSPI (0x70100000) programmés par le chargeur externe de
    STM32CubeProgrammer, relus par sa fonction Read, puis effacés."""
    from gdbtrace.core.flashalgo import find_external_loader
    if find_external_loader(N6_LOADER) is None:
        return {"absent": f"{N6_LOADER} introuvable (STM32CubeProgrammer, ExternalLoader)"}
    return boards.flash_by_algorithm(app, N6_LOADER, None, 0x70100000)


def test_coresight_and_mpu(run):
    """Tables ROM d'un Cortex-M55 (classe 9, composants nommés par DEVARCH : ETMv4, CTI, PMU) ;
    MPU ARMv8-M lue en entier."""
    boards.system_checks(run["extra"]["systeme"], {"ROM", "SCS", "DWT", "FPB", "ITM", "ETM", "CTI", "PMU"},
                         4, "ARMv8-M Mainline")


def test_external_flash_by_cubeprogrammer_loader(run):
    """Chargeur externe .stldr exécuté par gdb sur la N6 : flash XSPI programmée et relue."""
    res = run["extra"]["flash"]
    if "absent" in res:
        pytest.skip(res["absent"])
    assert not res["différences"] and res["programmé"]["octets"] == 0x10000, res
