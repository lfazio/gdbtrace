"""Ligne de commande : programmation automatisée (`--program`, `--batch`)."""
import os
import subprocess
import sys

from conftest import ROOT, needs_qemu


def run(*args, timeout=120):
    return subprocess.run([sys.executable, "-m", "gdbtrace", *args], cwd=ROOT, capture_output=True,
                          text=True, timeout=timeout)


@needs_qemu
def test_program_and_exit(rtos_elf):
    """--program ELF --batch : programme, vérifie, puis quitte (outil de flash)."""
    r = run("--program", rtos_elf, "--batch")
    assert r.returncode == 0, r.stderr
    assert "freertos.elf programmé" in r.stdout and "vérifié" in r.stdout


@needs_qemu
def test_program_then_script(elf, tmp_path):
    """--program sans argument : l'ELF de la ligne de commande ; le script voit la cible au reset."""
    script = tmp_path / "s.py"
    script.write_text("print('pc', t32.s.frame().func)\nt32.program(verify=False)\n")
    r = run(elf, "--program", "--no-verify", "--batch", "--script", str(script))
    assert r.returncode == 0, r.stderr
    lines = r.stdout.splitlines()
    assert "non vérifié" in lines[0] and "pc Reset_Handler" in lines
    assert sum("demo.elf programmé" in ln for ln in lines) == 2


def test_program_argument_errors(elf, rtos_elf):
    r = run(elf, "--program", rtos_elf, "--batch")
    assert r.returncode != 0 and "deux ELF différents" in r.stderr
    r = run("--batch")
    assert r.returncode != 0 and "un ELF est nécessaire" in r.stderr
    r = run(os.path.join(ROOT, "nope.elf"), "--batch")
    assert r.returncode != 0 and "ELF introuvable" in r.stderr
