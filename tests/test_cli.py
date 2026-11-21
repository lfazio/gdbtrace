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
    script.write_text("print('pc', gdbtrace.s.frame().func)\nt32.program(verify=False)\n")
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


@needs_qemu
def test_coverage_campaigns(elf, tmp_path):
    """--coverage écrit la couverture à la fin d'un --batch ; --coverage-merge cumule les
    campagnes (COVerage.LOAD /Add)."""
    import json
    script = os.path.join(ROOT, "examples", "scripts", "coverage.py")
    run1, merged, html = tmp_path / "run1.json", tmp_path / "all.json", tmp_path / "cov.html"
    r = run(elf, "--etm-sim", "--batch", "--script", script, "--coverage", str(run1),
            "--coverage", str(html), timeout=180)
    assert r.returncode == 0, r.stderr
    assert "couverture exportée (json)" in r.stdout and "instructions" in r.stdout
    assert html.exists() and "Couverture de code" in html.read_text()
    r = run(elf, "--etm-sim", "--batch", "--script", script, "--coverage-merge", str(run1),
            "--coverage", str(merged), timeout=180)
    assert r.returncode == 0, r.stderr
    one, both = json.load(open(run1)), json.load(open(merged))
    reset = "0x50"                                        # Reset_Handler : une fois par campagne
    assert int(both["e"][reset]) == 2 * int(one["e"][reset])
    assert set(one["e"]) <= set(both["e"])


def test_coverage_needs_etm(elf, tmp_path):
    r = run(elf, "--batch", "--coverage", str(tmp_path / "c.html"))
    assert r.returncode != 0 and "pas de trace ETM" in r.stderr
