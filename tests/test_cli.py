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
    script.write_text("print('pc', gdbtrace.s.frame().func)\ngdbtrace.program(verify=False)\n")
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
    assert r.returncode != 0 and "pas de trace d'instructions" in r.stderr


@needs_qemu
def test_mtb_simulated_campaign(m0_elf, tmp_path):
    """--mtb-sim sur la démo Cortex-M0+ (QEMU microbit) : couverture et paquets bruts."""
    import json
    script = tmp_path / "s.py"
    script.write_text("gdbtrace.break_('animate', temporary=True)\ngdbtrace.go()\ngdbtrace.wait()\n"
                      "rows = gdbtrace.mtb(5)\nprint('paquets', len(rows))\n")
    out = tmp_path / "cov.json"
    r = run(m0_elf, "--machine", "microbit", "--mtb-sim", "0", "--batch", "--script", str(script),
            "--coverage", str(out), timeout=180)
    assert r.returncode == 0, r.stderr
    assert "paquets 5" in r.stdout and "→ 000001d4 animate" in r.stdout
    cov = json.load(open(out))
    assert len(cov["e"]) > 50                                  # Reset_Handler, main, fib, animate...


@needs_qemu
def test_mtb_absent_on_target(m0_elf):
    """--mtb sur une cible sans MTB (QEMU) : erreur explicite ; --mtb-probe ne plante pas."""
    r = run(m0_elf, "--machine", "microbit", "--mtb", "--mtb-probe", "--batch", timeout=120)
    assert r.returncode == 1 and "MTB introuvable" in r.stderr
    assert "CoreSight" in r.stdout


AUTOHALT_SCRIPT = """import time
gdbtrace.go()
states = []
for _ in range(10):
    time.sleep(0.1)
    states.append(gdbtrace.s.state)
gdbtrace.halt()
st = gdbtrace.mtb_stats()
sim = gdbtrace.trace.mtb_sim
print("états", sorted(set(states)))
print("vérité", sim.logged, "décodé", gdbtrace.trace.etm.stats["instructions"])
"""


@needs_qemu
def test_mtb_autohalt_campaign(m0_elf, tmp_path):
    """--mtb-sim 1024 --mtb-autohalt : arrêts internes invisibles (la cible reste « en marche »
    pour le script), trace complète (décodé == exécuté d'après QEMU), couverture comparable à
    un tampon illimité."""
    import json
    import re
    script = tmp_path / "s.py"
    script.write_text(AUTOHALT_SCRIPT)
    auto, full = tmp_path / "auto.json", tmp_path / "full.json"
    r = run(m0_elf, "--machine", "microbit", "--mtb-sim", "1024", "--mtb-autohalt", "--batch",
            "--script", str(script), "--coverage", str(auto), timeout=240)
    assert r.returncode == 0, r.stderr
    assert "états ['running']" in r.stdout
    halts = int(re.search(r"arrêts AUTOHALT\s+(\d+)", r.stdout).group(1))
    errors = int(re.search(r"erreurs\s+(\d+)", r.stdout).group(1))
    truth, decoded = map(int, re.search(r"vérité (\d+) décodé (\d+)", r.stdout).groups())
    assert halts >= 2 and errors == 0 and decoded == truth > 50_000
    r = run(m0_elf, "--machine", "microbit", "--mtb-sim", "0", "--batch", "--script", str(script),
            "--coverage", str(full), timeout=240)
    assert r.returncode == 0, r.stderr
    covered_auto, covered_full = (len(json.load(open(f))["e"]) for f in (auto, full))
    assert covered_auto >= 0.9 * covered_full


def test_mtb_autohalt_needs_mtb(elf):
    r = run(elf, "--mtb-autohalt", "--batch")
    assert r.returncode != 0 and "--mtb-autohalt" in r.stderr


def test_example_session_script(elf):
    """examples/scripts/session.py en --batch : programmation, breakpoints (temporaire,
    conditionnel, matériel désactivé puis réactivé), watchpoint, Watch avec formats, pas à pas."""
    r = run(elf, "--batch", "--script", os.path.join("examples", "scripts", "session.py"))
    assert r.returncode == 0, r.stderr
    out = r.stdout
    for expected in ("vérifié", "4 hw breakpoint  non", "main (main.c:", "offset = 0b1111_1111_1111_1101",
                     "last_char = 'A'", "temperature = 21.5", "step → fib", "out  → update_sensor",
                     "tick = 6, last_char = 'F'", "watchpoint-trigger : mode MODE_IDLE → MODE_RUN",
                     "sensors[0].value en hexadécimal : 0x4d2", "fib(n = ", "== Fin : 0 breakpoint"):
        assert expected in out, (expected, out)
    assert "Traceback" not in out + r.stderr


def test_go_until_anywhere(elf, tmp_path):
    """gdbtrace.go(until=…) : Go jusqu'à n'importe où (breakpoint temporaire), même depuis le
    vecteur de reset ; arrêté avant par un autre breakpoint, le temporaire est retiré."""
    script = tmp_path / "s.py"
    script.write_text(
        "r = gdbtrace.wait(); gdbtrace.go(until='main'); r = gdbtrace.wait(5)\n"
        "assert (r['reason'], r['frame']['func']) == ('breakpoint-hit', 'main'), r\n"
        "gdbtrace.break_('update_sensor')\n"
        "gdbtrace.go(until='animate'); r = gdbtrace.wait(5)\n"
        "assert r['frame']['func'] == 'update_sensor', r\n"
        "assert [b.func for b in gdbtrace.breaks()] == ['update_sensor'], gdbtrace.breaks()\n"
        "print(repr(gdbtrace.out()))\n")
    r = run(elf, "--batch", "--script", str(script))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "<arrêt : function-finished — main (main.c:" in r.stdout
