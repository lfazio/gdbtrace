"""Test applicatif sur Nucleo-F446RE (STM32F446, Cortex-M4F) : le GUI de GdbTrace, toutes ses
vues, sur la vraie carte, et la trace ETMv3 matérielle par la broche SWO.

À passer à chaque release (`pytest --boards=require`). Reproductible : le firmware de test est
construit depuis ce dépôt (examples/nucleo-f446 : la démo FreeRTOS, à 16 MHz) et flashé par
le test ; la sonde est trouvée par tests/boards.py.

Une session par le vrai point d'entrée, comme sur la F746 : flashage, SVD, ETM et ITM par SWO
(stall), trace de données d'une variable. L'ETMv3 n'est démarré qu'à l'entrée de
`worker_step` et arrêté à son retour (deux comparateurs du DWT, logique start/stop de l'ETM) :
sans filtre, sa trace occupe toute la broche et l'ITM n'y passe plus — le firmware, qui
attend la FIFO de l'ITM à chaque changement de tâche, resterait bloqué. Un filtre par plage
(événement du DWT) est vérifié à part, en mode batch, et comparé à OpenCSD. Le relevé est
dans tests/artifacts/board-f446.json.
"""
import os
import subprocess

import pytest
from conftest import ROOT, board_serial

import boards

FW_DIR = os.path.join(ROOT, "examples", "nucleo-f446")
FW = os.path.join(FW_DIR, "nucleo-f446.elf")
CONFIG = os.path.join(ROOT, "configs", "nucleo-f446re.yaml")
TASKS = {"producer", "consumer", "worker", "sleeper", "hog", "IDLE", "Tmr Svc"}

pytestmark = pytest.mark.skipif(not os.environ.get("DISPLAY"), reason="pas de DISPLAY (xvfb-run)")


def worker_loops(app) -> dict:
    """Appels de `worker_step(n)` relevés dans la trace décodée : le firmware l'appelle avec
    n = 0, 1, 2…, et la boucle tourne n % 24 fois (branchement conditionnel arrière pris
    autant de fois). Un appel est complet s'il est tracé de l'entrée à l'épilogue sans trou (une
    interruption au milieu ne change pas le compte). Deux appels complets consécutifs doivent
    compter k puis (k + 1) % 24 tours : vérifie le chemin reconstruit sur une vraie trace."""
    from gdbtrace.trace.etm.image import DIRECT
    from gdbtrace.trace.etm.qemu_log import Step, TraceGap
    etm = app.trace.etm
    f = app.symbols.function("worker_step")
    instrs = [i for a in range(f.start, f.end, 2) if (i := etm.image.instr(a)) is not None]
    back = next(i for i in instrs if i.kind == DIRECT and i.cond and i.target < i.addr)
    ret = next(i for i in instrs if i.ret)
    # l'instruction d'arrêt (le retour) peut manquer à la trace : l'épilogue suffit
    epilogue = {ret.addr, max(i.addr for i in instrs if i.addr < ret.addr)}
    prologue = range(f.start, f.start + 8)
    calls, cur, prev = [], None, None
    for e in etm.last(len(etm.history)):
        if isinstance(e, Step):
            if e.addr in prologue and (prev is None or prev not in prologue):
                cur = {"k": 0, "clean": True, "done": False}
                calls.append(cur)
            elif cur is not None and not cur["done"]:
                if e.addr == back.addr and e.executed:
                    cur["k"] += 1
                elif e.addr in epilogue:
                    cur["done"] = True
            prev = e.addr
        elif isinstance(e, TraceGap):
            if cur is not None and not cur["done"]:
                cur["clean"] = False
            prev = None
    complete = [c["k"] if c["clean"] and c["done"] else None for c in calls]
    pairs = [(a, b) for a, b in zip(complete, complete[1:]) if a is not None and b is not None]
    return {"appels": len(calls), "complets": sum(k is not None for k in complete),
            "paires": len(pairs), "paires fausses": [p for p in pairs if p[1] != (p[0] + 1) % 24],
            "tours": complete[:60], "stats": etm.stats, "boucle": f"{back.addr:#x} → {back.target:#x}"}


def worker_return() -> int:
    """Adresse de l'instruction de retour de `worker_step` (arrêt de la trace)."""
    from gdbtrace.core.symbols import SymbolTable
    from gdbtrace.trace.etm.image import CodeImage
    f, img = SymbolTable(FW).function("worker_step"), CodeImage(FW)
    return next(i.addr for a in range(f.start, f.end, 2) if (i := img.instr(a)) is not None and i.ret)


@pytest.fixture(scope="module")
def run(request, tmp_path_factory):
    """La session GUI complète ; retourne {phase: {vue: contenu}} et le reste du relevé."""
    serial = board_serial(request, boards.NUCLEO_F446)
    subprocess.run(["make", "-s", "-C", FW_DIR], check=True)
    from gdbtrace.core.config import SessionConfig
    stop = SessionConfig.load(CONFIG).data["trace"]["etm-stop"]
    assert [int(a, 0) for a in stop] == [worker_return()], "etm-stop de la configuration à refaire"

    def cleanup(app, s):                    # la carte sans ETM ni stall après le test
        from gdbtrace.trace.etm.config import ETMCR, ETMCR_POWERDOWN, ETMCR_PROGRAMMING
        s.write_u32(ETMCR, ETMCR_PROGRAMMING | ETMCR_POWERDOWN)

    # la session de configs/nucleo-f446re.yaml : flashage, SVD, ETM (start/stop) et ITM par
    # SWO (stall), une variable tracée ; FreeRTOS (vues RTOS et Temps des tâches)
    argv = ["--config", CONFIG, "--layout", str(tmp_path_factory.mktemp("layout") / "layout.json")]
    old = os.environ.get("STLINK_SERIAL")
    os.environ["STLINK_SERIAL"] = serial
    try:
        return boards.run_gui(argv, "f446", ready=lambda app: app.trace.etm_swo, cleanup=cleanup,
                              extra=lambda app: {**worker_loops(app),
                                                 "compteurs": boards.counters_passes(app),
                                                 "chrono": boards.chrono_passes(app),
                                                 "rtt": boards.rtt_live(app),
                                                 "systeme": boards.system_passes(app),
                                                 "flash": f446_pack_flash(app)})
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
    assert "nucleo-f446.elf programmé" in text and "vérifié" in text
    assert "ETM par SWO (ETMv3)" in text and "stall" in text and "start 0x" in text
    assert "ETMFFLR" in text                                           # stall effectif


def test_gui_stays_responsive(run):
    slow = {p: t for p, t in run["max_update_s"].items() if t > 1.0}
    assert not slow, (run["max_update_s"], run["stalls"][:4])


# ====================================================================== à l'arrêt
def test_source_and_disassembly(run):
    assert "worker_step" in view(run, "arret", "source")["text"]
    assert "worker_step" in view(run, "arret", "disasm")["text"]


def test_registers(run):
    rows = {r["text"].lower(): r["values"] for r in view(run, "arret", "registers")["tree"]}
    assert {"r0", "r12", "sp", "lr", "pc", "xpsr", "msp", "psp", "control", "fpscr"} <= set(rows)
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
    assert "STM32F446" in per["dev_label"]
    assert {"RCC", "GPIOB", "TIM2"} <= {r["text"] for r in per["tree"]}


def test_memory_dump(run):
    assert len(view(run, "arret", "memory")["text"].splitlines()) >= 8


# ====================================================================== en marche
def test_watch_live(run):
    before = {r["text"]: r["values"][0] for r in view(run, "arret", "watch")["tree"]}
    live = {r["text"]: r["values"][0] for r in view(run, "marche", "watch")["tree"]}
    assert int(live["consumed"]) > int(before["consumed"])


def test_rtos_live(run):
    tick = lambda p: int(view(run, p, "rtos")["info"].split("tick ")[1].split()[0])  # noqa: E731
    assert tick("marche") > tick("arret")


def test_itm_text(run):
    """L'ITM passe à côté de l'ETM filtré."""
    assert "consumer: commande" in view(run, "apres", "itm")["out"]


def test_profile_by_swo(run):
    funcs = {r["text"] for r in view(run, "apres", "profile")["funcs"]}
    assert funcs & {"worker_step", "<sommeil>", "prvIdleTask", "vApplicationIdleHook"}, funcs


def test_variable_traced_by_dwt(run):
    rows = view(run, "apres", "vartrace")["tree"]
    produced = next(r for r in rows if r["text"] == "produced")
    assert int(produced["values"][1]) > 0


def test_task_time_by_itm(run):
    rows = {r["text"] for r in view(run, "apres", "tasktime")["tree"]}
    assert {"worker", "consumer", "IDLE"} <= rows, rows


# ====================================================================== trace ETMv3, après l'arrêt
def test_trace_listing(run):
    rows = view(run, "apres", "trace")["listing"]
    assert len(rows) > 100
    assert any(r["values"][1] == "worker_step" for r in rows)


def test_etm3_decoded_without_error_or_overflow(run):
    """Stall effectif (ETMFFLR) : pas de débordement de la FIFO de l'ETM."""
    st = run["extra"]["stats"]
    assert st["erreurs"] == 0 and st["débordements"] == 0 and st["code inconnu"] == 0, st
    assert st["instructions"] > 10_000, st


def test_worker_loops_follow_the_firmware(run):
    ex = run["extra"]
    assert ex["paires"] >= 20, ex
    assert not ex["paires fausses"], ex


def test_timing(run):
    funcs = {r["text"] for r in view(run, "apres", "timing")["table"]}
    assert "worker_step" in funcs, funcs


def test_coverage(run):
    rows = {r["text"]: r["values"] for r in view(run, "apres", "coverage")["tree"]}
    assert "freertos/main.c" in rows, rows.keys()
    covered, total = (int(x) for x in rows["freertos/main.c"][3].split("/"))
    assert 0 < covered <= total
    assert "capture ETM" in view(run, "apres", "coverage")["info"]


RANGE_SCRIPT = r'''
import json, time
g = gdbtrace
tm = g.trace
raw = bytearray()
feed = tm.etm.feed
tm.etm.feed = lambda d: (raw.extend(d), feed(d))[1]
g.go(); time.sleep(1.0); g.halt(); tm.wait_etm(120)
open(OUT, "wb").write(bytes(raw))
print("RESULT " + json.dumps({"stats": tm.etm.stats, "ranges": tm.etm.heal_ranges,
                             "status": tm.trace_status()}))
'''


def test_etm3_range_filter_by_dwt_on_board(request, tmp_path, run):
    """Filtre par plage d'un ETMv3 (événement TraceEnable = comparateur du DWT) : l'événement
    est en retard d'une instruction, la trace se coupe et reprend (un I-sync chaque fois) ; ces
    trous-là sont comblés. Sans débordement (stall effectif) et sans erreur ; la trace brute
    est décodée exactement comme par OpenCSD, et le chemin comblé la contient dans l'ordre."""
    import json
    import sys

    import opencsd
    from gdbtrace.trace.etm.flow import decode
    from gdbtrace.trace.etm.image import CodeImage
    from gdbtrace.trace.etm.qemu_log import Step
    serial = board_serial(request, boards.NUCLEO_F446)
    raw_path = tmp_path / "etm.bin"
    script = tmp_path / "plage.py"
    script.write_text(f"OUT = {str(raw_path)!r}\n" + RANGE_SCRIPT)
    env = dict(os.environ, STLINK_SERIAL=serial)
    r = subprocess.run([sys.executable, "-m", "gdbtrace", FW, "--openocd",
                        boards.NUCLEO_F446.openocd_cfg, "--program", "--cpu-hz", "16000000",
                        "--etm-config", "--etm-port", "swo", "--etm-stall", "--etm-filter",
                        "worker_step", "--batch", "--script", str(script)],
                       capture_output=True, text=True, timeout=240, cwd=ROOT, env=env)
    line = next((ln for ln in r.stdout.splitlines() if ln.startswith("RESULT ")), None)
    assert line, r.stdout[-2000:] + r.stderr[-2000:]
    res = json.loads(line[7:])
    st = res["stats"]
    assert st["erreurs"] == 0 and st["débordements"] == 0 and st["code inconnu"] == 0, res
    assert st["instructions"] > 1000 and st["trous comblés"] > 0, res
    from gdbtrace.core.symbols import SymbolTable
    f, size = SymbolTable(FW).function("worker_step"), 2
    while f.start // size != (f.end - 1) // size:                 # plus petit bloc aligné
        size *= 2
    base = f.start - f.start % size
    assert res["ranges"] == [[base, base + size]], res           # bloc aligné du DWT

    img = CodeImage(FW)
    data = raw_path.read_bytes()
    ours = [e.addr for e in decode(data, img)[0] if isinstance(e, Step)]
    if opencsd.lister() is None:
        pytest.skip("OpenCSD absent (tools/build_opencsd.sh)")
    snap = tmp_path / "ocsd"
    from gdbtrace.trace.tpiu import frame_bytes
    opencsd.make_snapshot(str(snap), img, frame_bytes(2, data))
    events, errors = opencsd.decode(str(snap), img)
    theirs = [e[1] for e in events if e[0] == "step"]
    assert not errors and ours == theirs and len(ours) > 1000
    # chemin comblé : celui d'OpenCSD, plus les instructions des trous de latence
    from gdbtrace.trace.etm.etm3 import Etm3Decoder
    from gdbtrace.trace.etm.flow import FlowDecoder
    flow = FlowDecoder(img)
    flow.heal_ranges = [tuple(r) for r in res["ranges"]]
    healed = [e.addr for e in flow.feed(Etm3Decoder().feed(data)) + flow.flush()
              if isinstance(e, Step)]
    it = iter(healed)
    assert all(a in it for a in theirs)                          # sous-suite, dans l'ordre
    assert len(healed) > len(theirs) and flow.stats.healed > 0


def f446_pack_flash(app) -> dict:
    """Le dernier secteur de 128 Ko de la flash (hors du firmware de test) programmé par le
    FLM du pack Keil.STM32F4xx_DFP, s'il est installé."""
    from gdbtrace.core.pack import PackError, resolve
    try:
        chip = resolve(None, "STM32F446RE")
        algo = next(a for a in chip.algorithms if a.default)
        flm = chip.pack.file(algo.path)
    except (PackError, StopIteration) as exc:
        return {"absent": str(exc)}
    ram = f"{algo.ram_start:#x}:{algo.ram_size:#x}" if algo.ram_start is not None else "0x20000000:0x4000"
    return boards.flash_by_algorithm(app, flm, ram, 0x08060000)


def test_coresight_and_mpu(run):
    """Tables ROM d'un Cortex-M4 (ETM-M4 en ETMv3, TPIU) ; MPU ARMv7-M lue en entier."""
    boards.system_checks(run["extra"]["systeme"], {"ROM", "SCS", "DWT", "FPB", "ITM", "TPIU", "ETM"},
                         3, "ARMv7-M")


def test_flash_by_the_pack_algorithm(run):
    """FLM du CMSIS-Pack exécuté par gdb sur la cible : programmé, vérifié par sa fonction Verify."""
    res = run["extra"]["flash"]
    if "absent" in res:
        pytest.skip(f"pack Keil.STM32F4xx_DFP non installé ({res['absent']})")
    assert not res["différences"] and res["programmé"]["octets"] == 0x10000, res


def test_counters_of_the_dwt(run):
    """Compteurs du DWT d'un Cortex-M4 entre deux passages par worker_step."""
    boards.counters_checks(run["extra"]["compteurs"], "DWT")


def test_chrono_by_cyccnt(run):
    """Deux passages par worker_step (vTaskDelay de 5 ms) mesurés par DWT_CYCCNT, ETMv3 et ITM
    en marche sur le SWO ; la pause de 1 s du débogueur entre les deux n'est pas comptée."""
    boards.chrono_checks(run["extra"]["chrono"], "DWT_CYCCNT")


def test_rtt_read_live_by_openocd(run):
    """RTT lu en marche par le port TCL d'OpenOCD (lecture, puis RdOff écrit), pendant que
    l'ETMv3 et l'ITM occupent le SWO."""
    boards.rtt_checks(run["extra"]["rtt"])
