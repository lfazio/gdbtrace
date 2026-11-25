"""Cartes de test branchées : détection, session GUI pilotée, relevé du contenu des vues.

Une carte est reconnue à sa sonde ST-LINK (numéro de série lu dans /sys) puis à l'identité de
sa puce (DBGMCU_IDCODE), lue par OpenOCD sans reset, ou par ST-LINK_gdbserver pour une puce
qu'OpenOCD ne connaît pas (STM32N6). `GDBTRACE_<CARTE>_SERIAL` impose la sonde. L'option
pytest `--boards` choisit : `auto` (tests ignorés sans la carte), `require` (absente =
échec : à utiliser pour une release), `skip`.
"""
from __future__ import annotations

import glob
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import traceback
from dataclasses import dataclass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: produits ST-LINK (USB 0483:xxxx)
STLINK_PIDS = {"3748": "ST-LINK/V2", "374b": "ST-LINK/V2-1", "3752": "ST-LINK/V2-1",
               "374e": "STLINK-V3", "374f": "STLINK-V3", "3753": "STLINK-V3", "3754": "STLINK-V3"}


@dataclass(frozen=True)
class BoardSpec:
    name: str                     # nucleo-f746zg
    env: str                      # variable imposant la sonde : GDBTRACE_F746_SERIAL
    openocd_cfg: str | None       # configs/… ; None : ST-LINK_gdbserver
    idcode_addr: int              # DBGMCU_IDCODE
    dev_id: int                   # DEV_ID attendu (bits 11:0)
    svd: str                      # nom du fichier SVD (STM32CubeIDE)
    apid: int = 0                 # port d'accès du cœur (ST-LINK_gdbserver -m)


NUCLEO_F746 = BoardSpec("nucleo-f746zg", "GDBTRACE_F746_SERIAL", "configs/nucleo-f746zg.cfg",
                        0xE0042000, 0x449, "STM32F746.svd")
NUCLEO_N6 = BoardSpec("nucleo-n657x0-q", "GDBTRACE_N6_SERIAL", None,
                      0x44001000, 0x486, "STM32N657.svd", apid=1)
NUCLEO_G070 = BoardSpec("nucleo-g070rb", "GDBTRACE_G070_SERIAL", "configs/nucleo-g070rb.cfg",
                        0x40015800, 0x460, "STM32G070.svd")
NUCLEO_F072 = BoardSpec("nucleo-f072rb", "GDBTRACE_F072_SERIAL", "configs/nucleo-f072rb.cfg",
                        0x40015800, 0x448, "STM32F072.svd")


def stlink_serials() -> list[tuple[str, str]]:
    """[(numéro de série, produit)] des sondes ST-LINK branchées."""
    out = []
    for dev in glob.glob("/sys/bus/usb/devices/*"):
        try:
            vid = open(os.path.join(dev, "idVendor")).read().strip()
            pid = open(os.path.join(dev, "idProduct")).read().strip()
            serial = open(os.path.join(dev, "serial")).read().strip()
        except OSError:
            continue
        if vid == "0483" and pid in STLINK_PIDS:
            out.append((serial, STLINK_PIDS[pid]))
    return sorted(out)


def read_idcode(spec: BoardSpec, serial: str) -> int | None:
    """DBGMCU_IDCODE de la puce derrière la sonde, lu par OpenOCD sans reset, ou None."""
    if spec.openocd_cfg is None:
        return read_idcode_stlink(spec, serial)
    if not shutil.which("openocd"):
        return None
    env = dict(os.environ, STLINK_SERIAL=serial, CONNECT_UNDER_RESET="0")
    try:
        r = subprocess.run(["openocd", "-f", os.path.join(ROOT, spec.openocd_cfg),
                            "-c", "gdb_port disabled", "-c", "tcl_port disabled",
                            "-c", "telnet_port disabled", "-c", "init",
                            "-c", f"mdw {spec.idcode_addr:#x}", "-c", "shutdown"],
                           capture_output=True, text=True, timeout=30, env=env)
    except (OSError, subprocess.TimeoutExpired):
        return None
    m = re.search(rf"{spec.idcode_addr:#x}: ([0-9a-f]+)", r.stdout + r.stderr)
    return int(m.group(1), 16) if m else None


def read_idcode_stlink(spec: BoardSpec, serial: str) -> int | None:
    """DBGMCU_IDCODE lu par ST-LINK_gdbserver (paquets gdb-remote bruts : connexion, lecture,
    détachement ; le cœur est arrêté le temps de la lecture), ou None."""
    sys.path.insert(0, ROOT)
    from gdbtrace.core.rsp_relay import frame, payload_of, take_token
    from gdbtrace.core.target import StLinkTarget, find_stlink_gdbserver
    if find_stlink_gdbserver() is None:
        return None
    t = StLinkTarget(serial=serial, apid=spec.apid, gdb_port=0)
    try:
        t.start()
    except (OSError, RuntimeError):
        return None
    try:
        with socket.create_connection((t.host, t.server_port), timeout=5) as c:
            buf = bytearray()

            def ask(packet: str) -> bytes:
                c.sendall(frame(packet))
                while True:
                    while (tok := take_token(buf)) is not None:
                        if tok[:1] == b"$":
                            c.sendall(b"+")
                            p = payload_of(tok)
                            if p[:1] != b"O" or p == b"OK":
                                return p
                    data = c.recv(4096)
                    if not data:
                        raise OSError("connexion fermée")
                    buf.extend(data)
            ask("qSupported")
            ask("?")
            reply = ask(f"m{spec.idcode_addr:x},4")
            ask("D")
            return int.from_bytes(bytes.fromhex(reply.decode()), "little") if len(reply) == 8 else None
    except (OSError, ValueError):
        return None
    finally:
        t.stop()


_found: dict[str, str | None] = {}


def find_board(spec: BoardSpec) -> str | None:
    """Numéro de série de la sonde de la carte `spec`, ou None si elle n'est pas branchée."""
    if spec.name not in _found:
        serials = [os.environ[spec.env]] if os.environ.get(spec.env) else [s for s, _p in stlink_serials()]
        _found[spec.name] = next((s for s in serials
                                  if (read_idcode(spec, s) or 0) & 0xFFF == spec.dev_id), None)
    return _found[spec.name]


def find_svd(name: str) -> str | None:
    """SVD de ST livré avec STM32CubeIDE (`GDBTRACE_SVD_DIR` pour un autre dossier)."""
    dirs = [os.environ["GDBTRACE_SVD_DIR"]] if os.environ.get("GDBTRACE_SVD_DIR") else glob.glob(
        "/opt/st/stm32cubeide*/plugins/*productdb.debug*/resources/cmsis/STMicroelectronics_CMSIS_SVD")
    for d in dirs:
        if os.path.exists(os.path.join(d, name)):
            return os.path.join(d, name)
    return None


# ====================================================================== relevé des vues
def dump_views(app) -> dict:
    """Contenu de toutes les vues ouvertes : lignes des tableaux (texte + colonnes, et les
    enfants des lignes dépliées), textes, libellés. Ce que l'utilisateur voit, en données."""
    import tkinter as tk
    from tkinter import ttk

    def rows(tv, parent="", depth=0):
        out = []
        for i in tv.get_children(parent):
            out.append({"text": tv.item(i, "text"), "values": [str(v) for v in tv.item(i, "values")],
                        "tags": list(tv.item(i, "tags"))})
            if depth < 1 and tv.item(i, "open"):
                out[-1]["children"] = rows(tv, i, depth + 1)
        return out
    views = {}
    for v in app.views.views:
        d = {}
        for name, w in vars(v).items():
            if isinstance(w, ttk.Treeview):
                d[name] = rows(w)
            elif isinstance(w, tk.Text):
                d[name] = w.get("1.0", "end-1c")[-20000:]
            elif isinstance(w, (ttk.Label, tk.Label)):
                d[name] = str(w.cget("text"))
        views[v.kind] = d
    return views


# ====================================================================== session GUI pilotée
def run_gui(argv: list[str], name: str, ready, cleanup=None, extra=None) -> dict:
    """La session GUI complète, par le vrai point d'entrée (`main(argv)`), `App.mainloop`
    remplacé par un scénario : connexion, mise en route (`ready(app)`), arrêt sur
    `worker_step`, marche (profil démarré), arrêt ; contenu de toutes les vues relevé à chaque
    phase. `extra(app)` : relevé propre à la carte (dans result["extra"], à la fin).
    `cleanup(app, session)` : remettre la carte en état (cible arrêtée). Le relevé est
    aussi écrit dans tests/artifacts/board-<name>.json. Retourne {"phases": {phase: {vue:
    contenu}}, "events", "errors", "max_update_s", "stalls", "etm_wait_s"}."""
    import json
    sys.path.insert(0, ROOT)
    from gdbtrace.__main__ import main
    from gdbtrace.gui import app as appmod
    result: dict = {"phases": {}, "errors": [], "events": {}}

    def scenario(app):
        root = app.root
        report = app.report_error

        def on_error(msg):
            result["errors"].append(msg.strip())
            report(msg)
        app.report_error = on_error

        lag = result["max_update_s"] = {}       # plus longue itération de la boucle Tk, par phase
        stalls = result["stalls"] = []          # où le thread GUI était bloqué (> 1 s)
        phase = ["connexion"]
        busy = {"since": None, "seen": False}
        main_id = threading.get_ident()
        done = threading.Event()

        def watchdog():
            while not done.wait(0.1):
                since = busy["since"]
                if since is not None and not busy["seen"] and time.monotonic() - since > 1.0:
                    busy["seen"] = True
                    frame = sys._current_frames().get(main_id)
                    stalls.append({"phase": phase[0], "pile": [ln.strip() for ln in
                                   traceback.format_stack(frame)[-14:]] if frame else []})
        threading.Thread(target=watchdog, daemon=True).start()

        def pump(seconds=0.0, until=None, timeout=30.0):
            end = time.monotonic() + (timeout if until else seconds)
            while time.monotonic() < end:
                t = time.monotonic()
                busy["since"], busy["seen"] = t, False
                root.update()
                busy["since"] = None
                lag[phase[0]] = round(max(lag.get(phase[0], 0.0), time.monotonic() - t), 3)
                if until is not None and until():
                    return True
                time.sleep(0.02)
            return until is None

        def snapshot(phase):
            # chaque onglet affiché une fois : certaines vues ne se remplissent que visibles
            for nb in app.views.notebooks.values():
                for tab in nb.tabs():
                    nb.select(tab)
                    pump(0.3)
            result["phases"][phase] = dump_views(app)

        def decoded():
            tm = app.trace
            return not tm.etm_busy and not tm.etm_pending() and tm.etm_backlog == 0

        from gdbtrace.views.data import WatchView
        from gdbtrace.views.profile import ProfileView
        s = None
        try:
            result["events"]["connecté"] = pump(
                until=lambda: app.session is not None and app.session.state == "stopped"
                and app.current_frame is not None, timeout=60)
            s = app.session
            # programmation puis mise en route de la trace (Go est refusé pendant la programmation)
            result["events"]["mise en route"] = pump(
                until=lambda: app.busy is None and ready(app) and s.state == "stopped", timeout=90)
            pump(1.0)
            wv = app.views.find(WatchView)
            for expr in ("produced", "consumed", "worked"):
                app.safe(wv.add, expr)
            bp = s.break_("worker_step")
            app.go()
            phase[0] = "arret"
            result["events"]["arrêt"] = pump(until=lambda: s.state == "stopped" and app.current_frame
                                             is not None and app.current_frame.func == "worker_step")
            pump(until=decoded, timeout=60)
            pump(1.0)
            snapshot("arret")
            s.delete_break(bp.number)
            app.breakpoints_changed()
            pv = app.views.find(ProfileView)
            phase[0] = "marche"
            app.go()
            pump(0.5)
            app.safe(pv.start)
            pump(4.0)
            snapshot("marche")
            app.safe(pv.stop)
            app.halt()
            phase[0] = "apres"
            result["events"]["arrêt final"] = pump(until=lambda: s.state == "stopped", timeout=10)
            # la trace ETM se décode (ou se lit) après l'arrêt : on attend la fin, GUI vivant
            t0 = time.monotonic()
            result["events"]["trace décodée"] = pump(until=decoded, timeout=300)
            result["etm_wait_s"] = round(time.monotonic() - t0, 1)
            pump(1.5)
            snapshot("apres")
            result["trace_status"] = app.trace.trace_status()
            if extra is not None:
                result["extra"] = extra(app)
        finally:
            if s is not None and s.state == "stopped" and cleanup is not None:
                app.safe(cleanup, app, s)
            done.set()
            app.quit()

    mainloop, appmod.App.mainloop = appmod.App.mainloop, scenario
    try:
        main(argv)
    finally:
        appmod.App.mainloop = mainloop
    artifacts = os.path.join(ROOT, "tests", "artifacts")
    os.makedirs(artifacts, exist_ok=True)
    with open(os.path.join(artifacts, f"board-{name}.json"), "w") as fh:
        json.dump(result, fh, indent=1, ensure_ascii=False)
    return result
