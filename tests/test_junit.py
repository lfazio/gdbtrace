"""Rapport JUnit (core/junit.py, --junit, clé script.junit) : XML valide (cas réussis, en
échec, en erreur, sortie capturée), un vrai --batch sur la démo QEMU et son code de retour,
gdbtrace.check(), script qui ne se charge pas, configuration."""
import os
import subprocess
import sys
import xml.etree.ElementTree as ET

import pytest
from conftest import ROOT, needs_qemu

from gdbtrace.core.junit import JUnitRun


def _cases(path):
    root = ET.parse(path).getroot()
    assert root.tag == "testsuites"
    suite = root.find("testsuite")
    cases = {}
    for tc in suite.findall("testcase"):
        float(tc.get("time"))                               # durée numérique
        kind = ("failure" if tc.find("failure") is not None else
                "error" if tc.find("error") is not None else "ok")
        out = tc.find("system-out")
        cases[tc.get("name")] = (kind, out.text if out is not None else "")
    return root, suite, cases


def test_report_xml(tmp_path):
    run = JUnitRun("essai")
    run.run("test_ok", lambda: print("bonjour"))
    run.run("test_assert", lambda: (_ for _ in ()).throw(AssertionError("compteur faux")))
    run.run("test_exception", lambda: 1 / 0)
    run.check("en ligne", True)
    run.check("en ligne faux", False, "attendu 3")
    path = tmp_path / "r.xml"
    run.write(str(path))
    root, suite, cases = _cases(path)
    assert (suite.get("tests"), suite.get("failures"), suite.get("errors")) == ("5", "2", "1")
    assert root.get("tests") == "5" and suite.get("name") == "essai" and suite.get("timestamp")
    assert cases["test_ok"] == ("ok", "bonjour\n")
    assert cases["test_assert"][0] == "failure" and cases["test_exception"][0] == "error"
    assert cases["en ligne"][0] == "ok" and cases["en ligne faux"][0] == "failure"
    fail = suite.find("testcase[@name='test_assert']/failure")
    assert fail.get("message") == "compteur faux" and "AssertionError" in fail.text
    err = suite.find("testcase[@name='test_exception']/error")
    assert err.get("type") == "ZeroDivisionError"
    assert run.failed and "2 en échec, 1 en erreur" in run.summary()


SCRIPT = '''
import time
gdbtrace.break_("fib")
gdbtrace.go()
gdbtrace.wait()
gdbtrace.check("arrêt dans fib", gdbtrace.s.frame().func == "fib")


def test_arret_dans_fib():
    print("pc", hex(gdbtrace.s.register("pc")))
    assert gdbtrace.s.frame().func == "fib"


def test_compteur_impossible():
    assert int(gdbtrace.s.evaluate("counter")) == -1, "counter ne vaut jamais -1"


def test_erreur():
    gdbtrace.s.evaluate("variable_inconnue")


def test_check_en_ligne():
    gdbtrace.check("capteur 2", int(gdbtrace.s.evaluate("sensors[1].id")) == 2)
    gdbtrace.check("counter négatif", int(gdbtrace.s.evaluate("counter")) < 0, "jamais")


def aide():                     # pas un cas : ne commence pas par test_
    raise RuntimeError("jamais appelée")
'''


def _batch(elf, script_text, tmp_path, *extra):
    script = tmp_path / "essai.py"
    script.write_text(script_text, encoding="utf-8")
    report = tmp_path / "rapport" / "junit.xml"
    r = subprocess.run([sys.executable, "-m", "gdbtrace", elf, "--batch", "--script", str(script),
                        "--junit", str(report), *extra], cwd=ROOT, capture_output=True, text=True,
                       timeout=120, env={**os.environ, "PYTHONPATH": ROOT})
    return r, report


@needs_qemu
def test_batch_cases_on_qemu(elf, tmp_path):
    r, report = _batch(elf, SCRIPT, tmp_path)
    assert r.returncode == 1, r.stdout + r.stderr            # des cas échouent
    assert "JUnit essai : 7 cas, 2 en échec, 1 en erreur" in r.stdout, r.stdout + r.stderr
    _root, suite, cases = _cases(report)
    assert list(cases) == ["arrêt dans fib", "test_arret_dans_fib", "test_compteur_impossible",
                           "test_erreur", "capteur 2", "counter négatif", "test_check_en_ligne"]
    assert cases["test_arret_dans_fib"][0] == "ok" and cases["test_arret_dans_fib"][1].startswith("pc 0x")
    assert cases["test_compteur_impossible"][0] == "failure"
    assert cases["test_erreur"][0] == "error" and cases["counter négatif"][0] == "failure"
    assert cases["capteur 2"][0] == "ok" and "✓ capteur 2" in cases["test_check_en_ligne"][1]
    assert suite.find("testcase[@name='test_compteur_impossible']/failure").get("message") == \
        "counter ne vaut jamais -1"
    assert "aide" not in cases


@needs_qemu
def test_batch_all_pass_and_broken_script(elf, tmp_path):
    ok = "gdbtrace.go(); gdbtrace.halt()\n\ndef test_arretee():\n    assert gdbtrace.s.state == 'stopped'\n"
    r, report = _batch(elf, ok, tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr
    assert _cases(report)[2] == {"test_arretee": ("ok", "")}
    r, report = _batch(elf, "raise RuntimeError('mise en place ratée')\n\ndef test_x():\n    pass\n",
                       tmp_path)
    assert r.returncode == 1
    cases = _cases(report)[2]
    assert list(cases) == ["essai (chargement)"] and cases["essai (chargement)"][0] == "error"


def test_option_and_configuration(tmp_path):
    from gdbtrace.__main__ import build_parser, validate
    from gdbtrace.core.config import ConfigError, SessionConfig
    p = build_parser()
    assert any("--junit" in e for e in validate(p.parse_args(["x.elf", "--junit", "r.xml"])))
    script, report = tmp_path / "s.py", tmp_path / "r.xml"
    script.write_text("pass\n")
    args = p.parse_args([str(tmp_path / "fw.elf"), "--script", str(script), "--junit", str(report)])
    cfg = SessionConfig.from_args(args, name="ci")
    assert cfg.data["script"] == {"fichier": str(script), "junit": str(report)}
    cfg.save(str(tmp_path / "ci.yaml"))
    text = (tmp_path / "ci.yaml").read_text()
    assert "fichier: s.py" in text and "junit: r.xml" in text      # relatifs au YAML
    again = SessionConfig.load(str(tmp_path / "ci.yaml")).parse()
    assert (again.script, again.junit) == (str(script), str(report))
    assert SessionConfig({"script": str(script)}).to_argv()[-2:] == ["--script", str(script)]
    with pytest.raises(ConfigError, match="script.junit"):
        SessionConfig({"script": {"junit": "r.xml"}}).check()
    with pytest.raises(ConfigError, match="script"):
        SessionConfig({"script": {"rapport": "r.xml"}}).check()


def test_dialog_keeps_the_junit_report(tmp_path):
    """Boîte de dialogue de configuration : script.junit lu, puis réécrit en section ; un script
    sans rapport reste un simple chemin."""
    if not os.environ.get("DISPLAY"):
        pytest.skip("pas de DISPLAY")
    import tkinter as tk

    from gdbtrace.core.config import SessionConfig
    from gdbtrace.gui.config_dialog import ConfigEditor
    root = tk.Tk()
    root.withdraw()
    try:
        data = {"cible": {"elf": "fw.elf"}, "script": {"fichier": "ci.py", "junit": "r.xml"}}
        ed = ConfigEditor(root, SessionConfig(data, str(tmp_path / "c.yaml")))
        assert ed.vars[("", "script")].get() == "ci.py" and ed.vars[("script", "junit")].get() == "r.xml"
        assert ed.get_config().data["script"] == {"fichier": "ci.py", "junit": "r.xml"}
        ed.vars[("script", "junit")].set("")
        assert ed.get_config().data["script"] == "ci.py"
        ed.destroy()
        ed = ConfigEditor(root, SessionConfig({"cible": {"elf": "fw.elf"}, "script": "a.py"},
                                              str(tmp_path / "c.yaml")))
        assert ed.vars[("", "script")].get() == "a.py" and ed.vars[("script", "junit")].get() == ""
        ed.destroy()
    finally:
        root.destroy()
