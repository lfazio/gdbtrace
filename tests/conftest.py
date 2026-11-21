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
