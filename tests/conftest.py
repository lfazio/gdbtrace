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


@pytest.fixture(scope="session")
def qemu_paths(tmp_path_factory, elf, rtos_elf):
    """Chemins d'exécution réels (journal QEMU instruction par instruction) des deux
    firmwares de démo, enregistrés une fois par session : {nom: (CodeImage, événements)}."""
    if not shutil.which("qemu-system-arm"):
        pytest.skip("QEMU absent")
    pytest.importorskip("capstone")
    from gdbtrace.trace.etm import qemu_log as ql
    from gdbtrace.trace.etm.image import CodeImage
    out = {}
    for name, path, secs in (("bare", elf, 0.4), ("rtos", rtos_elf, 1.0)):
        log = str(tmp_path_factory.mktemp("qemu") / f"{name}.log")
        ql.record(path, secs, log)
        out[name] = (CodeImage(path), ql.parse_file(log))
    return out
