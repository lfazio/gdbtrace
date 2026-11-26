import os
import shutil
import subprocess

import pytest

from gdbtrace.core.session import Session
from gdbtrace.core.target import QemuTarget, free_port

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FW = os.path.join(ROOT, "examples", "firmware")
ELF = os.path.join(FW, "demo.elf")
SVD = os.path.join(ROOT, "examples", "svd", "mps2-an385.svd")


def source_line(text: str, file: str = "main.c") -> int:
    """Numéro de la première ligne du firmware de démo contenant `text`."""
    with open(os.path.join(FW, file), encoding="utf-8") as fh:
        for i, line in enumerate(fh, 1):
            if text in line:
                return i
    raise LookupError(text)


FIB_LINE = source_line("uint32_t a = 0, b = 1;")  # première ligne exécutable de fib()

needs_qemu = pytest.mark.skipif(
    not (shutil.which("qemu-system-arm") and shutil.which("gdb-multiarch")),
    reason="qemu-system-arm / gdb-multiarch absents",
)


@pytest.fixture(scope="session")
def elf():
    if not shutil.which("arm-none-eabi-gcc"):
        pytest.skip("arm-none-eabi-gcc absent")
    subprocess.run(["make", "-s", "-C", FW], check=True)
    return ELF


@pytest.fixture
def session(elf):
    s = Session()
    s.connect(elf, QemuTarget(elf=elf, gdb_port=free_port()))
    yield s
    s.disconnect()


RTOS_DIR = os.path.join(ROOT, "examples", "freertos")
RTOS_ELF = os.path.join(RTOS_DIR, "freertos.elf")


@pytest.fixture(scope="session")
def rtos_elf():
    if not shutil.which("arm-none-eabi-gcc"):
        pytest.skip("arm-none-eabi-gcc absent")
    subprocess.run(["make", "-s", "-C", RTOS_DIR], check=True)
    return RTOS_ELF


@pytest.fixture
def rtos_session(rtos_elf):
    """Firmware FreeRTOS arrêté dans worker_step(), ordonnanceur démarré depuis ~0,3 s."""
    import time
    s = Session()
    s.connect(rtos_elf, QemuTarget(elf=rtos_elf, gdb_port=free_port()))
    s.go()
    time.sleep(0.3)
    s.halt()
    s.break_("worker_step", temporary=True)
    s.go()
    s.wait_stopped(5)
    yield s
    s.disconnect()


FAULTS_DIR = os.path.join(ROOT, "examples", "faults")


@pytest.fixture(scope="session")
def faults_elfs():
    """Démo des fautes : {"m3": faults-m3.elf (mps2-an385), "m0": faults-m0.elf (microbit)}."""
    if not shutil.which("arm-none-eabi-gcc"):
        pytest.skip("arm-none-eabi-gcc absent")
    subprocess.run(["make", "-s", "-C", FAULTS_DIR], check=True)
    return {"m3": os.path.join(FAULTS_DIR, "faults-m3.elf"), "m0": os.path.join(FAULTS_DIR, "faults-m0.elf")}


TERMINAL_DIR = os.path.join(ROOT, "examples", "terminal")


@pytest.fixture(scope="session")
def terminal_elf():
    """Démo de la vue Terminal (semihosting, RTT), Cortex-M3 sous QEMU mps2-an385."""
    if not shutil.which("arm-none-eabi-gcc"):
        pytest.skip("arm-none-eabi-gcc absent")
    subprocess.run(["make", "-s", "-C", TERMINAL_DIR], check=True)
    return os.path.join(TERMINAL_DIR, "terminal.elf")


M0_DIR = os.path.join(ROOT, "examples", "m0plus")
M0_ELF = os.path.join(M0_DIR, "demo.elf")


@pytest.fixture(scope="session")
def m0_elf():
    """Démo compilée pour Cortex-M0+ (ARMv6-M), pour QEMU `-M microbit`."""
    if not shutil.which("arm-none-eabi-gcc"):
        pytest.skip("arm-none-eabi-gcc absent")
    subprocess.run(["make", "-s", "-C", M0_DIR], check=True)
    return M0_ELF


@pytest.fixture(scope="session")
def m0_path(tmp_path_factory, m0_elf):
    """Chemin d'exécution réel de la démo M0+ sous QEMU microbit : (CodeImage, événements)."""
    if not shutil.which("qemu-system-arm"):
        pytest.skip("QEMU absent")
    pytest.importorskip("capstone")
    from gdbtrace.trace.etm import qemu_log as ql
    from gdbtrace.trace.etm.image import CodeImage
    count = 250_000
    log = str(tmp_path_factory.mktemp("qemu") / "m0.log")
    ql.record(m0_elf, 60, log, machine="microbit", instructions=count)
    events = ql.head(ql.parse_file(log), count)
    assert len(ql.steps(events)) == count, "m0 : journal QEMU trop court"
    return CodeImage(m0_elf), events


@pytest.fixture(scope="session")
def qemu_paths(tmp_path_factory, elf, rtos_elf):
    """Chemins d'exécution réels (journal QEMU instruction par instruction) des deux
    firmwares de démo, enregistrés une fois par session : {nom: (CodeImage, événements)}.

    Longueur fixée en instructions, pas en secondes : les seuils des tests (appels de
    `delay`, de `prvTaskCheckFreeStackSpace`…) ne dépendent plus de la charge de la machine.
    ~44 appels de `delay` en bare-metal, ~16 de `prvTaskCheckFreeStackSpace` sous FreeRTOS."""
    if not shutil.which("qemu-system-arm"):
        pytest.skip("QEMU absent")
    pytest.importorskip("capstone")
    from gdbtrace.trace.etm import qemu_log as ql
    from gdbtrace.trace.etm.image import CodeImage
    out = {}
    for name, path, count in (("bare", elf, 250_000), ("rtos", rtos_elf, 700_000)):
        log = str(tmp_path_factory.mktemp("qemu") / f"{name}.log")
        ql.record(path, 60, log, instructions=count)
        events = ql.head(ql.parse_file(log), count)
        assert len(ql.steps(events)) == count, f"{name} : journal QEMU trop court"
        out[name] = (CodeImage(path), events)
    return out


# ---------------------------------------------------------------------- cartes de test
def pytest_addoption(parser):
    parser.addoption("--boards", choices=("auto", "require", "skip"), default="auto",
                     help="tests sur cartes : auto (ignorés si la carte est absente), require "
                          "(carte absente = échec, pour une release), skip")


def board_serial(request, spec):
    """Sonde de la carte `spec` (tests/boards.py), selon --boards."""
    import boards
    mode = request.config.getoption("--boards")
    if mode == "skip":
        pytest.skip("tests sur cartes désactivés (--boards=skip)")
    serial = boards.find_board(spec)
    if serial is None:
        msg = f"{spec.name} non branchée (sonde ST-LINK + {spec.openocd_cfg}, ou {spec.env})"
        if mode == "require":
            pytest.fail(msg)
        pytest.skip(msg)
    return serial
