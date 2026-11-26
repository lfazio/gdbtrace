"""Configuration › Nouvelle… / Modifier… : la boîte de dialogue d'une configuration de session.

Cinq onglets : Cible (chemin de l'ELF, SVD, fréquence, programmation), Serveur (QEMU, OpenOCD,
ST-LINK_gdbserver, gdb pour un programme de l'hôte, serveur déjà lancé), OS, Trace, Script
et couverture. OK valide la configuration comme la ligne de commande (`SessionConfig.check`)
et garde la boîte ouverte en cas d'erreur, affichée en bas.

Non modale côté Python (une Toplevel qui prend la saisie) : `on_ok(config)` est appelé à la
validation. Les tests remplissent les variables puis appellent `ok()`.
"""
from __future__ import annotations

import os
import tkinter as tk
from tkinter import filedialog, ttk
from typing import Callable

from ..core.config import (CIBLE_KEYS, OS_TYPES, SERVER_TYPES, ConfigError, SessionConfig)

#: options de trace dans la boîte : (clé, libellé, genre) ; genre : bool, int, str, path, list,
#: choice:a|b|c, opt (vide = non, « auto » = sans valeur, sinon la valeur)
TRACE_FIELDS = {
    "SWO / ITM": [("swo-config", "Programmer ITM, DWT et TPIU, démarrer la capture", "bool"),
                  ("swo-hz", "Débit SWO (Hz)", "int"), ("swo-port", "Port TCP du SWO d'OpenOCD", "int"),
                  ("swo", "Capture SWO servie en TCP (hôte:port)", "str"),
                  ("swo-file", "Rejouer une capture SWO", "path"),
                  ("swo-formatter", "Flux avec formateur TPIU", "bool")],
    "ETM": [("etm-config", "Programmer l'ETM et sa sortie", "bool"),
            ("etm-port", "Sortie de l'ETM", "choice:parallel|swo|etr"),
            ("etm-width", "Largeur du port parallèle", "choice:1|2|4"),
            ("etm-stall", "Ralentir le cœur plutôt que perdre de la trace", "bool"),
            ("stm32", "Broches de trace (DBGMCU_CR des STM32)", "bool"),
            ("etm-cycle-accurate", "Cycle-accurate (durées mesurées)", "bool"),
            ("etm-timestamps", "Horodatage (ETMv4)", "bool"),
            ("etr-buffer", "Tampon de l'ETR (ADRESSE:TAILLE)", "str"),
            ("etr-at-stop", "Lire l'ETR à l'arrêt seulement", "bool"),
            ("trace", "Flux TPIU en TCP (hôte:port)", "str"),
            ("trace-file", "Rejouer une capture TPIU", "path")],
    "Données et filtre": [("var-trace", "Variables tracées (une par ligne, EXPR[:ACCÈS])", "list"),
                          ("etm-filter", "Filtre ETM : plages (une par ligne)", "list"),
                          ("etm-start", "Démarrer la trace en ces lieux", "list"),
                          ("etm-stop", "Arrêter la trace après ces lieux", "list")],
    "Simulation et MTB": [("etm-sim", "QEMU : ETM simulé", "bool"),
                          ("mtb-sim", "QEMU : MTB simulée (vide = non, auto, ou taille)", "opt"),
                          ("mtb", "MTB de la cible (vide = non, auto, ou adresse)", "opt"),
                          ("mtb-buffer", "Tampon de la MTB (ADRESSE:TAILLE)", "str"),
                          ("mtb-autohalt", "AUTOHALT (trace complète)", "bool"),
                          ("mtb-probe", "Lister les composants CoreSight", "bool")],
}
#: valeurs par défaut de la ligne de commande (non écrites dans le YAML)
TRACE_DEFAULTS = {"swo-hz": 2_000_000, "swo-port": 3443, "etm-port": "parallel", "etm-width": 4}
QEMU_MACHINES = ["mps2-an385", "mps2-an500", "microbit"]


class ConfigEditor(tk.Toplevel):
    """Boîte de dialogue d'une configuration. `on_ok(config)` reçoit la configuration validée."""

    def __init__(self, parent: tk.Misc, config: SessionConfig,
                 on_ok: Callable[[SessionConfig], None] | None = None, title: str | None = None):
        super().__init__(parent)
        self.title(title or f"Configuration — {config.name}")
        self.transient(parent.winfo_toplevel())
        self.original = config
        self.on_ok = on_ok
        self.result: SessionConfig | None = None
        self.vars: dict[tuple[str, str], tk.Variable] = {}
        self.texts: dict[tuple[str, str], tk.Text] = {}
        self.error_var = tk.StringVar()

        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=6, pady=6)
        self.notebook = nb
        self._build_target(self._tab(nb, "Cible"))
        self._build_server(self._tab(nb, "Serveur"))
        self._build_os(self._tab(nb, "OS"))
        self._build_trace(self._tab(nb, "Trace"))
        self._build_script(self._tab(nb, "Script et couverture"))

        bottom = ttk.Frame(self)
        bottom.pack(fill="x", padx=6, pady=(0, 6))
        tk.Label(bottom, textvariable=self.error_var, fg="#b00000", anchor="w", justify="left",
                 wraplength=620).pack(side="left", fill="x", expand=True)
        ttk.Button(bottom, text="Annuler", command=self.cancel).pack(side="right")
        ttk.Button(bottom, text="OK", command=self.ok).pack(side="right", padx=4)
        self.bind("<Escape>", lambda _e: self.cancel())
        self.protocol("WM_DELETE_WINDOW", self.cancel)
        self.set_config(config)
        try:
            self.grab_set()                 # modale pour la saisie, sans bloquer l'appelant
        except tk.TclError:
            pass

    # ------------------------------------------------------------------ construction
    @staticmethod
    def _tab(nb: ttk.Notebook, text: str) -> ttk.Frame:
        f = ttk.Frame(nb, padding=8)
        nb.add(f, text=text)
        return f

    def _var(self, section: str, key: str, kind: str = "str") -> tk.Variable:
        v = tk.BooleanVar() if kind == "bool" else tk.StringVar()
        self.vars[(section, key)] = v
        return v

    def _row(self, master, row: int, label: str, section: str, key: str, kind: str = "str",
             browse: str | None = None, width: int = 50) -> None:
        """Une ligne libellé + champ (Entry, case, liste déroulante, zone de texte)."""
        if kind == "bool":
            ttk.Checkbutton(master, text=label, variable=self._var(section, key, "bool")).grid(
                row=row, column=0, columnspan=3, sticky="w", pady=1)
            return
        ttk.Label(master, text=label).grid(row=row, column=0, sticky="nw", padx=(0, 6), pady=1)
        if kind == "list":
            t = tk.Text(master, height=3, width=width - 2, wrap="none")
            t.grid(row=row, column=1, sticky="ew", pady=1)
            self.texts[(section, key)] = t
        elif kind.startswith("choice:"):
            ttk.Combobox(master, textvariable=self._var(section, key), values=kind[7:].split("|"),
                         width=14, state="readonly").grid(row=row, column=1, sticky="w", pady=1)
        else:
            ttk.Entry(master, textvariable=self._var(section, key), width=width).grid(
                row=row, column=1, sticky="ew", pady=1)
        if browse:
            ttk.Button(master, text="Parcourir…", command=lambda: self._browse(section, key, browse)).grid(
                row=row, column=2, padx=(4, 0), pady=1)
        master.columnconfigure(1, weight=1)

    def _build_target(self, f) -> None:
        self._row(f, 0, "Nom", "", "nom")
        self._row(f, 1, "Chemin de l'ELF", "cible", "elf", browse="elf")
        self._row(f, 2, "SVD (fichier, ou nom cherché dans STM32CubeIDE)", "cible", "svd", browse="svd")
        self._row(f, 3, "Fréquence du cœur (Hz)", "cible", "cpu-hz")
        self._row(f, 4, "Programmer l'ELF à la connexion", "cible", "programmer", "bool")
        self._row(f, 5, "Vérifier après programmation", "cible", "verifier", "bool")
        self._row(f, 6, "Lancer le programme après la connexion (Go)", "cible", "go", "bool")
        self._row(f, 7, "Arrêt sur faute (HardFault, BusFault…)", "cible", "break-on-fault", "bool")

    def _build_server(self, f) -> None:
        self.server_var = tk.StringVar(value="qemu")
        types = ttk.LabelFrame(f, text="Serveur gdb", padding=6)
        types.pack(fill="x")
        for kind, label in SERVER_TYPES.items():
            ttk.Radiobutton(types, text=label, value=kind, variable=self.server_var,
                            command=self._show_server).pack(anchor="w")
        self.server_frames: dict[str, ttk.Frame] = {}
        holder = ttk.Frame(f)
        holder.pack(fill="x", pady=6)
        fr = self.server_frames["qemu"] = ttk.LabelFrame(holder, text="QEMU", padding=6)
        ttk.Label(fr, text="Machine").grid(row=0, column=0, sticky="w", padx=(0, 6))
        ttk.Combobox(fr, textvariable=self._var("serveur", "machine"), values=QEMU_MACHINES,
                     width=16).grid(row=0, column=1, sticky="w")
        fr = self.server_frames["openocd"] = ttk.LabelFrame(holder, text="OpenOCD", padding=6)
        ttk.Label(fr, text="Fichiers de configuration (-f), un par ligne").grid(row=0, column=0, sticky="nw")
        t = tk.Text(fr, height=3, width=48, wrap="none")
        t.grid(row=0, column=1, sticky="ew")
        self.texts[("serveur", "openocd")] = t
        ttk.Button(fr, text="Ajouter…", command=lambda: self._browse("serveur", "openocd", "cfg")).grid(
            row=0, column=2, padx=(4, 0), sticky="n")
        fr.columnconfigure(1, weight=1)
        self._row(fr, 1, "Tâches RTOS vues comme threads gdb (--rtos)", "serveur", "rtos", "bool")
        fr = self.server_frames["stlink"] = ttk.LabelFrame(holder, text="ST-LINK_gdbserver", padding=6)
        self._row(fr, 0, "Numéro de série de la sonde (vide : la seule branchée)", "serveur", "stlink")
        self._row(fr, 1, "Port d'accès du cœur (STM32N6 : 1)", "serveur", "stlink-ap")
        self._row(fr, 2, "Image exécutée en RAM (STM32N6)", "serveur", "ram-boot", "bool")
        fr = self.server_frames["gdb"] = ttk.LabelFrame(holder, text="Programme de l'hôte", padding=6)
        self._row(fr, 0, "Arguments (syntaxe shell)", "serveur", "args")
        self._row(fr, 1, "Répertoire de travail", "serveur", "cwd", browse="dir")
        self._row(fr, 2, "S'attacher au processus (PID ; vide : lancer le programme)", "serveur", "pid")
        self._row(fr, 3, "Mode non-stop (un thread s'arrête, les autres continuent)", "serveur",
                  "non-stop", "bool")
        fr = self.server_frames["remote"] = ttk.LabelFrame(holder, text="Serveur déjà lancé", padding=6)
        self._row(fr, 0, "Serveur gdb (hôte:port)", "serveur", "remote")
        self._row(fr, 1, "Port TCL d'un OpenOCD (hôte:port, mémoire en marche)", "serveur", "tcl")
        self._row(fr, 2, "Programme de l'hôte servi par gdbserver --multi : son chemin côté serveur",
                  "serveur", "remote-exec")
        common = ttk.LabelFrame(f, text="Commun", padding=6)
        common.pack(fill="x")
        self._row(common, 0, "Exécutable gdb", "serveur", "gdb")
        self._row(common, 1, "Port du serveur gdb (0 : choisi)", "serveur", "gdb-port")
        self._row(common, 2, "Ne pas charger l'ELF à la connexion", "serveur", "no-load", "bool")

    def _build_os(self, f) -> None:
        self.os_var = tk.StringVar(value="aucun")
        box = ttk.LabelFrame(f, text="OS de la cible", padding=6)
        box.pack(fill="x")
        for kind, label in OS_TYPES.items():
            ttk.Radiobutton(box, text=label, value=kind, variable=self.os_var).pack(anchor="w")
        ttk.Button(f, text="Détecter dans l'ELF", command=self.detect_os).pack(anchor="w", pady=6)
        ttk.Label(f, justify="left", text=(
            "FreeRTOS : vues RTOS et Temps des tâches.\nPthread (programme de l'hôte) : vue Threads.\n"
            "Aucun : ni l'une ni l'autre.")).pack(anchor="w")

    def _build_trace(self, f) -> None:
        for i, (title, fields) in enumerate(TRACE_FIELDS.items()):
            box = ttk.LabelFrame(f, text=title, padding=6)
            box.grid(row=i // 2, column=i % 2, sticky="nsew", padx=3, pady=3)
            for row, (key, label, kind) in enumerate(fields):
                self._row(box, row, label, "trace", key, kind,
                          browse="file" if kind == "path" else None, width=22)
        f.columnconfigure(0, weight=1)
        f.columnconfigure(1, weight=1)

    def _build_script(self, f) -> None:
        self._row(f, 0, "Script Python (objet gdbtrace)", "", "script", browse="py")
        self._row(f, 1, "Couverture fusionnée à la connexion (.json, un par ligne)", "couverture",
                  "fusionner", "list")
        self._row(f, 2, "Couverture écrite à la fin du script (.html, .info, .json)", "couverture",
                  "exporter", "list")

    # ------------------------------------------------------------------ fichiers
    def _browse(self, section: str, key: str, what: str) -> None:
        base = self.original.base_dir
        if what == "dir":
            path = filedialog.askdirectory(parent=self, initialdir=base)
        else:
            types = {"elf": [("Exécutables ELF", "*.elf *.axf *.out"), ("Tous", "*")],
                     "svd": [("CMSIS-SVD", "*.svd *.xml"), ("Tous", "*")],
                     "cfg": [("OpenOCD", "*.cfg"), ("Tous", "*")],
                     "py": [("Python", "*.py"), ("Tous", "*")]}.get(what, [("Tous", "*")])
            path = filedialog.askopenfilename(parent=self, initialdir=base, filetypes=types)
        if not path:
            return
        if (section, key) in self.texts:
            t = self.texts[(section, key)]
            t.insert("end", ("\n" if t.get("1.0", "end-1c").strip() else "") + path)
        else:
            self.vars[(section, key)].set(path)

    # ------------------------------------------------------------------ valeurs
    def _get_list(self, section: str, key: str) -> list[str]:
        t = self.texts[(section, key)]
        return [ln.strip() for ln in t.get("1.0", "end-1c").splitlines() if ln.strip()]

    def _set_list(self, section: str, key: str, values) -> None:
        t = self.texts[(section, key)]
        t.delete("1.0", "end")
        if values:
            t.insert("1.0", "\n".join(str(v) for v in (values if isinstance(values, list) else [values])))

    def set_config(self, config: SessionConfig) -> None:
        d = config.data
        for (section, key), var in self.vars.items():
            holder = d if not section else (d.get(section) or {})
            v = holder.get(key)
            if isinstance(var, tk.BooleanVar):
                var.set(v is not False if key == "verifier" else bool(v))
            elif v is True:                                  # mtb, mtb-sim : sans valeur
                var.set("auto")
            elif v is None:
                var.set(str(TRACE_DEFAULTS.get(key, "")) if section == "trace" else "")
            else:
                var.set(str(v))
        for (section, key) in self.texts:
            holder = d if not section else (d.get(section) or {})
            self._set_list(section, key, holder.get(key))
        srv = d.get("serveur") or {}
        self.server_var.set(srv.get("type", "qemu"))
        for key, default in (("machine", "mps2-an385"), ("gdb", "gdb-multiarch"), ("gdb-port", "0"),
                             ("stlink-ap", "0")):
            if srv.get(key) in (None, ""):
                self.vars[("serveur", key)].set(default)     # valeur par défaut, non écrite
        self.os_var.set(d.get("os") or "aucun")
        self._show_server()

    def _show_server(self) -> None:
        for kind, frame in self.server_frames.items():
            if kind == self.server_var.get():
                frame.pack(fill="x")
            else:
                frame.pack_forget()

    def get_config(self) -> SessionConfig:
        """La configuration saisie (non validée) : seules les valeurs utiles sont écrites."""
        d: dict = {"gdbtrace": 1}
        name = self.vars[("", "nom")].get().strip()
        if name:
            d["nom"] = name
        cible: dict = {}
        for key in CIBLE_KEYS:
            var = self.vars[("cible", key)]
            if isinstance(var, tk.BooleanVar):
                if key == "verifier":
                    if not var.get():
                        cible[key] = False
                elif var.get():
                    cible[key] = True
            elif var.get().strip():
                cible[key] = _number(var.get().strip()) if key == "cpu-hz" else var.get().strip()
        if cible:
            d["cible"] = cible
        kind = self.server_var.get()
        srv: dict = {"type": kind}
        keys = {"qemu": ["machine"], "openocd": [], "stlink": ["stlink", "stlink-ap", "ram-boot"],
                "gdb": ["args", "cwd", "pid", "non-stop"], "remote": ["remote", "tcl", "remote-exec"]}[kind]
        for key in keys + ["gdb", "gdb-port", "no-load"]:
            var = self.vars[("serveur", key)]
            if isinstance(var, tk.BooleanVar):
                if var.get():
                    srv[key] = True
                continue
            v = var.get().strip()
            if not v or (key == "gdb" and v == "gdb-multiarch") or (key in ("gdb-port", "stlink-ap") and v == "0") \
                    or (key == "machine" and v == "mps2-an385"):
                continue
            srv[key] = _number(v) if key in ("gdb-port", "stlink-ap", "pid") else v
        if kind == "openocd":
            srv["openocd"] = self._get_list("serveur", "openocd")
            if self.vars[("serveur", "rtos")].get():
                srv["rtos"] = True
        d["serveur"] = srv
        if self.os_var.get() != "aucun":
            d["os"] = self.os_var.get()
        script = self.vars[("", "script")].get().strip()
        if script:
            d["script"] = script
        cov = {k: self._get_list("couverture", k) for k in ("fusionner", "exporter")}
        cov = {k: v for k, v in cov.items() if v}
        if cov:
            d["couverture"] = cov
        trace: dict = {}
        for fields in TRACE_FIELDS.values():
            for key, _label, kind in fields:
                if kind == "list":
                    values = self._get_list("trace", key)
                    if values:
                        trace[key] = values
                    continue
                var = self.vars[("trace", key)]
                if kind == "bool":
                    if var.get():
                        trace[key] = True
                    continue
                v = var.get().strip()
                if not v or str(TRACE_DEFAULTS.get(key, "")) == v:
                    continue
                if kind == "opt":
                    trace[key] = True if v.lower() in ("auto", "oui") else (_number(v) if key == "mtb-sim" else v)
                elif kind == "int" or kind == "choice:1|2|4":
                    trace[key] = _number(v)
                else:
                    trace[key] = v
        if trace:
            d["trace"] = trace
        return SessionConfig(d, self.original.path, modified=True)

    # ------------------------------------------------------------------ actions
    def detect_os(self) -> None:
        """OS d'après l'ELF : programme de l'hôte → Pthread (et serveur gdb) ; FreeRTOS lié →
        FreeRTOS ; sinon aucun."""
        from ..__main__ import is_native_elf
        elf = self.vars[("cible", "elf")].get().strip()
        path = elf if os.path.isabs(elf) else os.path.join(self.original.base_dir, elf)
        if not elf or not os.path.isfile(path):
            self.error_var.set("Détecter : choisir d'abord un ELF qui existe (onglet Cible)")
            return
        if is_native_elf(path):
            self.os_var.set("pthread")
            self.server_var.set("gdb")
            self._show_server()
        else:
            from ..core.rtos import freertos
            from ..core.symbols import SymbolTable
            self.os_var.set("freertos" if freertos.detect(SymbolTable(path)) else "aucun")
        self.error_var.set(f"OS détecté : {OS_TYPES[self.os_var.get()]}")

    def ok(self) -> bool:
        try:
            cfg = self.get_config()
            cfg.check()
        except (ConfigError, ValueError) as exc:
            self.error_var.set(str(exc))
            return False
        self.result = cfg
        self.destroy()
        if self.on_ok is not None:
            self.on_ok(cfg)
        return True

    def cancel(self) -> None:
        self.result = None
        self.destroy()


def _number(text: str) -> int:
    try:
        return int(text, 0)
    except ValueError:
        raise ConfigError(f"« {text} » : un entier est attendu") from None
