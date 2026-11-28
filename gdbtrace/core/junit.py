"""Rapport JUnit d'un script GdbTrace (`--junit FICHIER.xml`, clé de configuration
`script.junit`), pour l'intégration continue.

En `--batch`, le script s'exécute d'abord (mise en place : breakpoints, Go…), puis chacune de
ses fonctions `test_*`, dans l'ordre où il les définit, est un cas : réussi, en **échec** (une
assertion, `AssertionError`) ou en **erreur** (toute autre exception), avec sa durée et sa
sortie (stdout et stderr capturés, aussi recopiés à l'écran pour le journal de la CI).
`gdbtrace.check(nom, condition, message)` ajoute un cas en ligne. Un script qui ne se charge
pas est un cas en erreur, et ses tests ne tournent pas. Le XML suit le schéma JUnit des
outils de CI (testsuites / testsuite / testcase, failure, error, system-out).
"""
from __future__ import annotations

import contextlib
import io
import os
import runpy
import sys
import time
import traceback
import xml.etree.ElementTree as ET
from dataclasses import dataclass


@dataclass
class Case:
    name: str
    classname: str
    seconds: float = 0.0
    status: str = "réussi"           # réussi | échec | erreur
    message: str = ""
    kind: str = ""                   # type de l'exception
    detail: str = ""                 # pile Python
    output: str = ""


class _Tee(io.TextIOBase):
    """Écrit dans plusieurs flux : la sortie d'un cas est capturée et reste affichée."""

    def __init__(self, *streams):
        self.streams = streams

    def write(self, text: str) -> int:
        for st in self.streams:
            st.write(text)
        return len(text)

    def flush(self) -> None:
        for st in self.streams:
            with contextlib.suppress(Exception):
                st.flush()


class JUnitRun:
    """Les cas d'une exécution (une suite par script)."""

    def __init__(self, suite: str):
        self.suite = suite
        self.cases: list[Case] = []
        self.started = time.time()

    def check(self, name: str, ok: bool, message: str = "") -> None:
        self.cases.append(Case(name, self.suite, status="réussi" if ok else "échec",
                               message="" if ok else (message or "condition fausse"),
                               kind="" if ok else "check"))

    def run(self, name: str, fn, keep_success: bool = True) -> Case:
        """Exécute `fn()` comme un cas ; rendu (et gardé, sauf succès avec keep_success faux)."""
        case = Case(name, self.suite)
        buf = io.StringIO()
        t0 = time.monotonic()
        with contextlib.redirect_stdout(_Tee(sys.stdout, buf)), \
                contextlib.redirect_stderr(_Tee(sys.stderr, buf)):
            try:
                fn()
            except AssertionError as exc:
                case.status, case.kind = "échec", "AssertionError"
                case.message = str(exc) or "assertion fausse"
                case.detail = traceback.format_exc()
            except Exception as exc:  # noqa: BLE001 - une erreur du cas, la suite continue
                case.status, case.kind = "erreur", type(exc).__name__
                case.message = f"{type(exc).__name__}: {exc}"
                case.detail = traceback.format_exc()
        case.seconds = time.monotonic() - t0
        case.output = buf.getvalue()
        if keep_success or case.status != "réussi":
            self.cases.append(case)
        return case

    @property
    def failures(self) -> int:
        return sum(1 for c in self.cases if c.status == "échec")

    @property
    def errors(self) -> int:
        return sum(1 for c in self.cases if c.status == "erreur")

    @property
    def failed(self) -> bool:
        return bool(self.failures or self.errors)

    def summary(self) -> str:
        return (f"JUnit {self.suite} : {len(self.cases)} cas, {self.failures} en échec, "
                f"{self.errors} en erreur")

    def element(self) -> ET.Element:
        total = sum(c.seconds for c in self.cases)
        root = ET.Element("testsuites", name="gdbtrace", tests=str(len(self.cases)),
                          failures=str(self.failures), errors=str(self.errors), time=f"{total:.3f}")
        suite = ET.SubElement(root, "testsuite", name=self.suite, tests=str(len(self.cases)),
                              failures=str(self.failures), errors=str(self.errors), skipped="0",
                              time=f"{total:.3f}",
                              timestamp=time.strftime("%Y-%m-%dT%H:%M:%S",
                                                      time.localtime(self.started)))
        for c in self.cases:
            tc = ET.SubElement(suite, "testcase", classname=c.classname, name=c.name,
                               time=f"{c.seconds:.3f}")
            if c.status != "réussi":
                tag = "failure" if c.status == "échec" else "error"
                el = ET.SubElement(tc, tag, message=c.message, type=c.kind)
                el.text = c.detail or c.message
            if c.output:
                ET.SubElement(tc, "system-out").text = c.output
        return root

    def write(self, path: str) -> None:
        tree = ET.ElementTree(self.element())
        ET.indent(tree)
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        tree.write(path, encoding="utf-8", xml_declaration=True)


def _defined_in(fn, path: str) -> bool:
    code = getattr(fn, "__code__", None)
    try:
        return code is not None and os.path.samefile(code.co_filename, path)
    except OSError:
        return False


def run_script(api, path: str, report: str) -> JUnitRun:
    """Le script, puis ses fonctions `test_*` (définies dans le script lui-même), en cas ;
    le rapport est écrit dans `report`."""
    suite = os.path.splitext(os.path.basename(path))[0]
    run = JUnitRun(suite)
    api._junit = run
    try:
        glob: dict = {}
        load = run.run(f"{suite} (chargement)",
                       lambda: glob.update(runpy.run_path(path, init_globals=api.script_globals())),
                       keep_success=False)
        if load.status == "réussi":
            for name, fn in list(glob.items()):
                if name.startswith("test_") and callable(fn) and _defined_in(fn, path):
                    run.run(name, fn)
    finally:
        api._junit = None
    run.write(report)
    print(f"{run.summary()} → {report}")
    return run
