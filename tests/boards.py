"""Cartes de test branchées : détection, et relevé du contenu des vues du GUI.

Une carte est reconnue à sa sonde ST-LINK (numéro de série lu dans /sys) puis à l'identité de
sa puce, lue par OpenOCD sans reset (DBGMCU_IDCODE). `GDBTRACE_<CARTE>_SERIAL` impose la
sonde. L'option pytest `--boards` choisit : `auto` (tests ignorés sans la carte), `require`
(absente = échec : à utiliser pour une release), `skip`.
"""
from __future__ import annotations

import glob
import os
import re
import shutil
import subprocess
from dataclasses import dataclass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: produits ST-LINK (USB 0483:xxxx)
STLINK_PIDS = {"3748": "ST-LINK/V2", "374b": "ST-LINK/V2-1", "3752": "ST-LINK/V2-1",
               "374e": "STLINK-V3", "374f": "STLINK-V3", "3753": "STLINK-V3", "3754": "STLINK-V3"}


@dataclass(frozen=True)
class BoardSpec:
    name: str                     # nucleo-f746zg
    env: str                      # variable imposant la sonde : GDBTRACE_F746_SERIAL
    openocd_cfg: str              # configs/…
    idcode_addr: int              # DBGMCU_IDCODE
    dev_id: int                   # DEV_ID attendu (bits 11:0)
    svd: str                      # nom du fichier SVD (STM32CubeIDE)


NUCLEO_F746 = BoardSpec("nucleo-f746zg", "GDBTRACE_F746_SERIAL", "configs/nucleo-f746zg.cfg",
                        0xE0042000, 0x449, "STM32F746.svd")


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
