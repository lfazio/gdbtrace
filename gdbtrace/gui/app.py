"""Fenêtre principale de gdbtrace."""
from __future__ import annotations

import json
import os
import sys
import tkinter as tk
import traceback
from tkinter import filedialog, messagebox, simpledialog, ttk
from typing import Any, Callable

from ..core import svd
from ..core.config import ConfigError, SessionConfig
from ..core.live import LiveMemory, LivePoller
from ..core.scripting import Console, ScriptAPI, stop_summary
from ..core.session import Breakpoint, Frame, Session
from ..core.symbols import SymbolTable
from ..core.target import TargetServer
from ..trace.manager import TraceManager
from .event_pump import EventPump
from .view_base import View, ViewManager


def view_classes() -> dict[str, type[View]]:
    from ..views.code import DisasmView, SourceView
    from ..views.console import ConsoleView
    from ..views.data import BreakpointsView, StackView, WatchView
    from ..views.memory import MemoryView
    from ..views.peripherals import PeripheralsView
    from ..views.registers import RegistersView
    from ..views.itm import ItmView
    from ..views.profile import ProfileView
    from ..views.rtos import RtosView
    from ..views.coverage import CoverageView
    from ..views.files import FilesView
    from ..views.timing import TimingView
    from ..views.threads import ThreadsView
    from ..views.trace import TraceView
    from ..views.vartrace import VarTraceView
    from ..views.tasktime import TaskTimeView
    from ..views.fault import FaultView
    from ..views.symbols import SymbolsView
    from ..views.terminal import TerminalView
    from ..views.counters import CountersView
    from ..views.system import SystemView
    classes = [FilesView, SymbolsView, SourceView, DisasmView, TraceView, TimingView, CoverageView, RegistersView, WatchView,
               PeripheralsView, SystemView, RtosView, ThreadsView, StackView, BreakpointsView, FaultView, MemoryView,
               ConsoleView, TerminalView, ItmView, ProfileView, VarTraceView, TaskTimeView, CountersView]
    return {c.kind: c for c in classes}


#: vues qui suivent l'OS de la configuration (Configuration › Ouvrir…)
OS_VIEWS = {"freertos": {"rtos", "tasktime"}, "pthread": {"threads"}, "aucun": set()}
ALL_OS_VIEWS = set().union(*OS_VIEWS.values())

DEFAULT_LAYOUT = [
    {"kind": "files", "where": "left"},
    {"kind": "symbols", "where": "left"},
    {"kind": "source", "where": "code"},
    {"kind": "disasm", "where": "code"},
    {"kind": "trace", "where": "code"},
    {"kind": "timing", "where": "code"},
    {"kind": "coverage", "where": "code"},
    {"kind": "registers", "where": "data"},
    {"kind": "watch", "where": "data"},
    {"kind": "peripherals", "where": "data"},
    {"kind": "system", "where": "data"},
    {"kind": "rtos", "where": "data"},
    {"kind": "threads", "where": "data"},
    {"kind": "stack", "where": "data"},
    {"kind": "breakpoints", "where": "data"},
    {"kind": "fault", "where": "data"},
    {"kind": "console", "where": "bottom"},
    {"kind": "memory", "where": "bottom"},
    {"kind": "terminal", "where": "bottom"},
    {"kind": "itm", "where": "bottom"},
    {"kind": "profile", "where": "bottom"},
    {"kind": "vartrace", "where": "bottom"},
    {"kind": "tasktime", "where": "bottom"},
    {"kind": "counters", "where": "bottom"},
]


class App:
    def __init__(self, root: tk.Tk | None = None, layout_file: str | None = None):
        # Les fils de décodage (ETM, SWO) calculent en Python sans relâche pendant une capture.
        # Le thread GUI rend le GIL à chaque appel Tk et attend ensuite l'intervalle de bascule
        # pour le reprendre : 5 ms par défaut, soit 10 s pour dessiner 2 000 rectangles.
        sys.setswitchinterval(0.0005)
        self.root = root or tk.Tk()
        self.root.title("gdbtrace")
        self.root.geometry("1400x900")
        self.layout_file = layout_file
        style = ttk.Style(self.root)
        if "clam" in style.theme_names():
            style.theme_use("clam")

        self.pump = EventPump(self.root)
        self.pump.on_error = self.report_error
        # opération qui exclut toute commande de marche (programmation de la flash : un Go
        # relancerait le cœur au milieu du chargement, de la vérification ou du reset)
        self._exclusive: str | None = None
        self.session: Session | None = None
        self.api: ScriptAPI | None = None
        self.console: Console | None = None
        self.symbols: SymbolTable | None = None
        self.current_frame: Frame | None = None
        self.cached_breakpoints: list[Breakpoint] = []
        self.console_views: list = []
        # canal mémoire en direct (OpenOCD TCL / QEMU QMP) et son poller
        self.live_period_ms = 200
        self.live: LiveMemory | None = None
        self.poller: LivePoller | None = None
        self.security = None                   # TrustZone : SecurityState du cœur arrêté
        self._has_security = None
        self.core_set = None                   # plusieurs cœurs (--core) : core/cores.CoreSet
        self.main_session: Session | None = None
        self.current_task: str | None = None   # tâche RTOS courante (vue RTOS)
        self._rtos_layout = None               # disposition FreeRTOS (calculée à l'arrêt)
        # trace SWO/ITM (capture, texte ITM, exceptions) et fréquence du cœur pour la configurer
        self.trace = TraceManager(post=self.pump.post, on_error=self.report_error)
        self.trace.gui_busy = lambda: self.pump.busy
        self.cpu_hz = 25_000_000
        self.coverage_in_source = False        # couverture ETM affichée dans la vue Source
        self.etm_cycle_accurate = False        # ETM matériel en mode cycle-accurate (ETMCR bit 12)
        self.etm_timestamps = False            # horodatage de l'ETM
        # Trace › ETM par SWO… : débit SWO, port TCP servi par OpenOCD, stall, broches STM32
        self.swo_hz = 2_000_000
        self.swo_port = 3443
        self.etm_stall = False
        self.stm32 = False
        self._last_till = ""                    # dernières saisies de Go.Till, Step.Till/Change
        self._last_step: tuple | None = None
        # programmation : exécutable gdb, fabrique de cible (ligne de commande), ELF récents
        self.gdb_path = "gdb-multiarch"
        #: chargeurs de flash d'une session (build_flash_loaders des options), ou None
        self.flash_loader_factory = None
        #: puce décrite par son CMSIS-Pack (--device, core/pack.Device), ou None
        self.chip = None
        self.target_factory: Callable[[str], TargetServer] | None = None
        self.verify_program = True
        self.recent_elfs: list[str] = []
        # configuration de session (menu Configuration) : courante, récentes, et vues fermées
        # parce que son OS ne les utilise pas
        self.config: SessionConfig | None = None
        self.recent_configs: list[str] = []
        self.os_hidden: set[str] = set()
        self._start_token = 0                   # Connecter en attente (annulé par Fermer)
        #: question « Enregistrer la configuration ? » : True, False, ou None (annuler)
        self.ask_save: Callable[[str, str], bool | None] = messagebox.askyesnocancel
        # description des périphériques (CMSIS-SVD)
        self.device: svd.Device | None = None
        self.svd_path: str | None = None
        self._svd_pending: str | None = None      # SVD en cours de lecture (tâche de fond)
        self._svd_token = 0                       # un SVD remplacé ou retiré périme la lecture
        self._pending_output: list[tuple[str, str]] = []

        self.views = ViewManager(self)
        self._build_menu()
        self._build_toolbar()
        self._build_status()  # avant le corps : pack réserve d'abord la barre du bas
        self._build_body()
        self._bind_keys()
        self.root.protocol("WM_DELETE_WINDOW", self.quit)
        self.restore_layout()
        self.update_status()

    # ================================================================== construction
    def _build_menu(self) -> None:
        mb = tk.Menu(self.root)
        m = tk.Menu(mb, tearoff=False)
        m.add_command(label="Programmer un ELF…", command=self._ask_program, accelerator="Ctrl+O")
        m.add_command(label="Reprogrammer l'ELF courant", command=self.reprogram, accelerator="Ctrl+R")
        self.recent_menu = tk.Menu(m, tearoff=False, postcommand=self._fill_recent_menu)
        m.add_cascade(label="ELF récents", menu=self.recent_menu)
        self.verify_var = tk.BooleanVar(value=True)
        self.fault_catch_var = tk.BooleanVar(value=False)      # Run › Arrêt sur faute
        m.add_checkbutton(label="Vérifier après programmation", variable=self.verify_var,
                          command=lambda: setattr(self, "verify_program", self.verify_var.get()))
        m.add_separator()
        m.add_command(label="Exécuter un script…", command=self._ask_script)
        m.add_command(label="Charger un SVD…", command=self._ask_svd)
        m.add_command(label="Ajouter des symboles…", command=self._ask_add_symbols)
        m.add_separator()
        m.add_command(label="Enregistrer le layout", command=self.save_layout)
        m.add_command(label="Déconnecter", command=self.disconnect)
        m.add_command(label="Quitter", command=self.quit)
        mb.add_cascade(label="Fichier", menu=m)

        c = tk.Menu(mb, tearoff=False)
        c.add_command(label="Nouvelle…", command=self.new_config)
        c.add_command(label="Ouvrir…", command=self._ask_open_config)
        self.recent_config_menu = tk.Menu(c, tearoff=False, postcommand=self._fill_recent_configs)
        c.add_cascade(label="Récentes", menu=self.recent_config_menu)
        c.add_command(label="Modifier…", command=self.edit_config)
        c.add_separator()
        c.add_command(label="Enregistrer", command=self.save_config, accelerator="Ctrl+S")
        c.add_command(label="Enregistrer sous…", command=lambda: self.save_config(ask=True))
        c.add_separator()
        c.add_command(label="Connecter", command=self.connect_config)
        c.add_command(label="Fermer", command=self.close_config)
        mb.add_cascade(label="Configuration", menu=c)

        r = tk.Menu(mb, tearoff=False)
        for label, fn, key in [("Go", self.go, "F5"), ("Break", self.halt, "F8"),
                               ("Step", self.step, "F11"), ("Step Over", self.over, "F10"),
                               ("Step Out (Go.Return)", self.out, "Shift+F11"),
                               ("Step instruction", self.stepi, "F7"), ("Reset", self.reset, "")]:
            r.add_command(label=label, command=fn, accelerator=key)
        r.add_command(label="Go (tous les cœurs)", command=self.go_all)
        r.add_command(label="Break (tous les cœurs)", command=self.halt_all)
        r.add_separator()
        r.add_command(label="Go.Till (jusqu'à une condition)…", command=self._ask_go_till)
        r.add_command(label="Step.Till / Step.Change…", command=self._ask_step_loop)
        r.add_separator()
        r.add_checkbutton(label="Arrêt sur faute", variable=self.fault_catch_var,
                          command=self.toggle_fault_catch)
        r.add_command(label="Remettre le chrono à zéro", command=self.reset_chrono)
        mb.add_cascade(label="Run", menu=r)

        t = tk.Menu(mb, tearoff=False)
        t.add_command(label="ETM par SWO…", command=self._ask_etm_swo)
        t.add_command(label="ETM par l'ETR (RAM, STM32N6)", command=lambda: self.start_etm_etr())
        t.add_command(label="Filtre de trace ETM…", command=self._ask_etm_filter)
        t.add_command(label="Arrêter la capture", command=lambda: self.trace.stop())
        mb.add_cascade(label="Trace", menu=t)

        v = tk.Menu(mb, tearoff=False)
        for kind, cls in view_classes().items():
            v.add_command(label=f"{cls.title} (nouvelle fenêtre)",
                          command=lambda c=cls: self.views.open(c, None))
        mb.add_cascade(label="Vues", menu=v)
        self.root.configure(menu=mb)

    def _build_toolbar(self) -> None:
        tb = ttk.Frame(self.root, padding=2)
        tb.pack(side="top", fill="x")
        self.buttons: dict[str, ttk.Button] = {}
        for label, fn in [("▶ Go", self.go), ("■ Break", self.halt), ("↓ Step", self.step),
                          ("→ Over", self.over), ("↑ Out", self.out), ("⇣ Asm", self.stepi),
                          ("⟲ Reset", self.reset)]:
            b = ttk.Button(tb, text=label, command=fn, width=9)
            b.pack(side="left", padx=1)
            self.buttons[label] = b

    def _build_body(self) -> None:
        outer = ttk.PanedWindow(self.root, orient="vertical")
        outer.pack(fill="both", expand=True)
        upper = ttk.PanedWindow(outer, orient="horizontal")
        nb_left, nb_code, nb_data = ttk.Notebook(upper, width=230), ttk.Notebook(upper), ttk.Notebook(upper)
        nb_bottom = ttk.Notebook(outer)
        upper.add(nb_left, weight=0)
        upper.add(nb_code, weight=3)
        upper.add(nb_data, weight=2)
        outer.add(upper, weight=3)
        outer.add(nb_bottom, weight=1)
        self.views.register_notebook("left", nb_left)
        self.views.register_notebook("code", nb_code)
        self.views.register_notebook("data", nb_data)
        self.views.register_notebook("bottom", nb_bottom)

    def _build_status(self) -> None:
        self.status = tk.StringVar()
        bar = ttk.Frame(self.root, relief="sunken")
        bar.pack(side="bottom", fill="x")
        self.state_label = tk.Label(bar, width=12, text="", fg="white", bg="#777")
        self.state_label.pack(side="left")
        # TrustZone : monde du cœur arrêté (S / NS), placé après l'état quand il y en a un
        self.secure_label = tk.Label(bar, width=4, text="", fg="white", bg="#aaaaaa")
        # plusieurs cœurs : le cœur que montrent les vues (placé quand il y en a plusieurs)
        self.core_var = tk.StringVar(self.root)
        self.core_box = ttk.Combobox(bar, textvariable=self.core_var, state="readonly", width=24)
        self.core_box.bind("<<ComboboxSelected>>",
                           lambda _e: self.select_core(self.core_var.get().split(" ")[0]))
        self.live_label = tk.Label(bar, width=6, text="LIVE", fg="white", bg="#aaaaaa")
        self.live_label.pack(side="left", padx=(2, 0))
        self.task_label = ttk.Label(bar, text="", width=22)
        # RunTime : temps de la dernière exécution (reprise → arrêt) et cumul
        self.chrono_label = ttk.Label(bar, text="")
        self.chrono_label.pack(side="right", padx=6)
        self.task_label.pack(side="left", padx=(6, 0))
        ttk.Label(bar, textvariable=self.status).pack(side="left", padx=6)

    def _bind_keys(self) -> None:
        keys = {"<F5>": self.go, "<F8>": self.halt, "<F11>": self.step, "<F10>": self.over,
                "<Shift-F11>": self.out, "<F7>": self.stepi, "<Control-o>": self._ask_program,
                "<Control-r>": self.reprogram, "<Control-s>": self.save_config}
        for k, fn in keys.items():
            self.root.bind_all(k, lambda _e, f=fn: (f(), "break")[1])

    # ================================================================== session
    def attach(self, session: Session) -> None:
        """Associe une session déjà connectée (ou en cours) à la GUI."""
        self.session = session
        self.security = None
        self._has_security = None
        self.api = ScriptAPI(session)
        self.api.cpu_hz = self.cpu_hz
        self.api._config = self.config.data if self.config is not None else None
        self.console = Console(self.api, lambda s: self.pump.post(self.write_console, s, ""))
        self.main_session = session
        self._drop_cores()
        session.on_event(lambda kind, data, s=session: self.pump.post(self._session_event, s, kind, data))
        self.api.device = self.device
        self.api.chip = self.chip
        self.api.trace = self.trace
        self.api.on_program = lambda elf: self.pump.post(self._program_changed, elf)
        self.api.on_var_watch = lambda expr, fmt: self.pump.post(self.add_watch, expr, fmt)
        self._rtos_layout = None
        if session.elf and os.path.exists(session.elf):
            self._program_changed(session.elf, refresh=False)
        if session.target is not None and session.state != "disconnected":
            self._setup_live(session.target)
        if session.state == "stopped":
            self.refresh_all()
        self.update_status()

    def connect(self, elf: str, target: TargetServer, gdb: str = "gdb-multiarch",
                load: bool | None = None, then: Callable[[], None] | None = None) -> None:
        session = Session(gdb)
        self.gdb_path = gdb
        if self.flash_loader_factory is not None:      # --flash-loader : avant la programmation
            for ld in self.safe(self.flash_loader_factory, session) or []:
                self.write_console(f"chargeur de flash : {ld.describe()}\n", "gdb")
        self.attach(session)
        self.status.set(f"Connexion à {target.remote}…")

        def done(_r):
            if session.elf and (self.symbols is None or self.symbols.path != session.elf):
                self._program_changed(session.elf, refresh=False)
            self._setup_live(target)
            self.refresh_all()
            if then:
                then()
        self.pump.background(session.connect, elf, target, load, then=done)

    # ================================================================== programmation
    def program_elf(self, path: str, verify: bool | None = None,
                    then: Callable[[Any], None] | None = None) -> None:
        """Programme l'ELF `path` dans la cible (flash, ou RAM sous QEMU), vérifie, reset ;
        symboles, vues et trace passent au nouveau programme. Sans cible connectée, la cible
        de la ligne de commande est lancée avec cet ELF."""
        from ..core.memtools import is_image, split_address
        image = is_image(path)
        file, at = split_address(path)
        path = os.path.abspath(file) + (f"@{at:#x}" if at is not None else "")
        if not os.path.isfile(os.path.abspath(file)):
            self.report_error(f"fichier introuvable : {file}")
            return
        if self._refuse_if_busy():              # une programmation à la fois
            return
        verify = self.verify_program if verify is None else verify
        if not image:
            self._remember_elf(path)
        s = self.session
        name = os.path.basename(path)
        if s is not None and s.state != "disconnected":
            self.status.set(f"Programmation de {name}…")
            self._exclusive = f"programmation de {name}"

            def job():
                try:
                    return self.api.program(path, verify, quiet=True)
                except BaseException:
                    self.pump.post(setattr, self, "_exclusive", None)
                    raise

            def done(res) -> None:
                self._exclusive = None
                msg = res.summary()
                self.write_console(msg + "\n", "err" if res.mismatched else "gdb")
                self.status.set(msg)
                if res.mismatched:
                    self.report_error(msg)
                if then:
                    then(res)
            self.run_action(job, then=done)
            return
        if self.target_factory is None or image:
            self.status.set("Pas de cible : lancer gdbtrace avec --qemu, --openocd ou --remote"
                            + (" et l'ELF des symboles, puis programmer l'image" if image else ""))
            return
        # cible de la ligne de commande lancée avec cet ELF, puis programmation
        self.connect(path, self.target_factory(path), self.gdb_path, load=False,
                     then=lambda: self.program_elf(path, verify, then))

    def reprogram(self) -> None:
        """Reprogramme l'ELF courant (après une recompilation)."""
        elf = (self.session.elf if self.session and self.session.elf
               else self.recent_elfs[0] if self.recent_elfs else None)
        if elf:
            self.program_elf(elf)
        else:
            self._ask_program()

    def _ask_program(self) -> None:
        cur = self.session.elf if self.session and self.session.elf else \
            (self.recent_elfs[0] if self.recent_elfs else None)
        path = filedialog.askopenfilename(
            title="ELF ou image à programmer dans la cible",
            initialdir=os.path.dirname(cur) if cur else os.getcwd(),
            filetypes=[("Exécutables ELF", "*.elf *.axf *.out"),
                       ("Images (Intel HEX, S-record, binaire)", "*.hex *.ihex *.srec *.s19 *.s28 *.s37 *.mot *.bin"),
                       ("Tous", "*")])
        if not path:
            return
        from ..core.memtools import image_format
        if image_format(path) == "bin":         # binaire brut : l'adresse est à donner
            at = simpledialog.askstring("Binaire brut", "Adresse de chargement :",
                                        initialvalue="0x08000000", parent=self.root)
            if not at:
                return
            path = f"{path}@{at.strip()}"
        self.program_elf(path)

    def _fill_recent_menu(self) -> None:
        self.recent_menu.delete(0, "end")
        if not self.recent_elfs:
            self.recent_menu.add_command(label="(aucun)", state="disabled")
        for path in self.recent_elfs:
            self.recent_menu.add_command(label=path, command=lambda p=path: self.program_elf(p))

    def _elf_name(self) -> str:
        elf = self.session.elf if self.session else None
        return f"{os.path.basename(elf)} : " if elf else ""

    def _remember_elf(self, path: str) -> None:
        self.recent_elfs = [path] + [p for p in self.recent_elfs if p != path][:7]

    def _program_changed(self, elf: str, refresh: bool = True) -> None:
        """Le programme de la cible a changé : symboles, trace ETM, caches des vues."""
        from ..views.code import clear_source_cache
        try:
            self.symbols = SymbolTable(elf, self.session.extra_symbols if self.session else ())
        except Exception as exc:  # noqa: BLE001
            self.symbols = None
            self.report_error(f"lecture des symboles : {exc}")
        if self.api:
            self.api.symbols = self.symbols
        self._rtos_layout = None
        clear_source_cache()
        if self.symbols is not None and not self.native:   # décodage ETM : Cortex-M seulement
            self.trace.retarget(elf, self.symbols, cycle_accurate=self.etm_cycle_accurate)
        for v in list(self.views.views):
            if hasattr(v, "on_program_changed"):
                self.safe(v.on_program_changed)
        if refresh:
            self.refresh_all()
            self.breakpoints_changed()

    def disconnect(self) -> None:
        if self.core_set is not None and self.main_session is not None:
            self.select_core(self.core_set.cores[0].name)   # la session principale d'abord
        if self.session:
            s = self.session
            self.pump.background(s.disconnect)

    def quit(self) -> None:
        if not self._confirm_discard():           # configuration modifiée : enregistrer ?
            return
        try:
            self.save_layout()
        except Exception:  # noqa: BLE001
            pass
        for v in list(self.views.views):          # arrête les échantillonneurs (vue Profil)
            if hasattr(v, "stop") and v.kind == "profile":
                self.safe(v.stop)
        self.trace.close()
        self._teardown_live()
        if self.core_set is not None:
            self.core_set.close()
            self.core_set = None
            self.session = self.main_session
        if self.session and self.session.state != "disconnected":
            try:
                self.session.disconnect()
            except Exception:  # noqa: BLE001
                pass
        self.pump.stop()
        self.root.destroy()

    # ================================================================== live / SVD
    def _setup_live(self, target: TargetServer) -> None:
        """Ouvre le canal mémoire latéral de la cible (s'il existe) et démarre le poller."""
        self._teardown_live()
        self.live = target.live_memory()
        self.trace.live = self.live                # SNOOPer
        if self.live is not None:
            self.poller = LivePoller(
                self.live,
                is_running=lambda: self.session is not None and self.session.state == "running",
                post=self.pump.post, period_ms=self.live_period_ms, on_error=self.report_error)
            self.poller.start()
        if self.api:
            self.api.live = self.live
        from ..core.target import QemuTarget
        if isinstance(target, QemuTarget) and (target.etm_sim or target.mtb_sim is not None) \
                and self.live is not None:
            if self.trace.etm is None and self.session and self.session.elf and self.symbols:
                msg = self.trace.setup_etm(self.session.elf, self.symbols)
                if msg:
                    self.report_error(msg)
        if self.session is not None and self.session.state == "stopped" and not self.native:
            self.safe(self.trace.detect_core, self.session)     # ARMv6-M : ni ITM ni ETM
            self.safe(self.trace.discover, self.session)        # tables ROM CoreSight
            if self.trace.etm is not None:
                self.safe(self.trace.detect_etm, self.session)  # ETMv3 (M3/M4) ou ETMv4 (M7)
        if isinstance(target, QemuTarget) and (target.etm_sim or target.mtb_sim is not None) \
                and self.live is not None:
            if self.trace.etm is not None and target.mtb_sim is not None and self.trace.mtb_sim is None:
                self.trace.enable_mtb_sim(self.live, target.mtb_sim, self.session,
                                          autohalt=target.mtb_autohalt)
                self.trace.attach_session(self.session)
            elif self.trace.etm is not None and target.etm_sim and self.trace.etm_sim is None:
                self.trace.enable_qemu_sim(self.live)
                self.trace.attach_session(self.session)
        if self.session and self.session.state == "running":
            self.views.running_all()  # abonne les vues
        self.update_status()

    def _teardown_live(self) -> None:
        if self.poller is not None:
            self.poller.stop()
        if self.live is not None:
            self.live.close()
        self.poller = self.live = None
        self.trace.live = None
        self.trace.unsnoop()                    # variables échantillonnées de cette session
        if self.api:
            self.api.live = None
            self.api.term.close()               # sockets du semihosting

    def rtos_layout(self):
        """Disposition FreeRTOS partagée (vue RTOS, profilage par tâche), calculée une fois
        via gdb quand la cible est arrêtée ; None sans FreeRTOS."""
        from ..core.rtos import freertos as fr
        if self._rtos_layout is None and fr.detect(self.symbols) and \
                self.session is not None and self.session.state == "stopped":
            self._rtos_layout = fr.FreeRtosLayout.from_session(self.session, self.symbols)
        return self._rtos_layout

    def load_svd(self, path: str, background: bool = False) -> None:
        """Charge un SVD ; `background` : analysé hors du thread GUI (celui d'un STM32N6 fait
        22 Mo, plus d'une seconde), les vues suivent à la fin."""
        self._svd_token += 1
        token = self._svd_token
        if background:
            self.status.set(f"lecture du SVD {os.path.basename(path)}…")
            self._svd_pending = os.path.abspath(path)
            self.pump.background(svd.parse, path, then=lambda dev: self._svd_loaded(dev, path, token))
            return
        self._svd_loaded(svd.parse(path), path, token)

    def wants_svd(self, path: str) -> bool:
        """Ce SVD n'est ni chargé ni en cours de lecture."""
        return os.path.abspath(path) not in (self.svd_path, self._svd_pending)

    def unload_svd(self) -> None:
        """Plus de SVD (configuration fermée, ou sans SVD) : vue Périphériques vide. Une lecture
        encore en cours est oubliée."""
        self._svd_token += 1
        self._svd_pending = None
        if self.device is None:
            return
        self.device = None
        self.svd_path = None
        if self.api:
            self.api.device = None
        for v in list(self.views.views):
            if hasattr(v, "on_svd_loaded"):
                self.safe(v.on_svd_loaded)

    def _svd_loaded(self, device, path: str, token: int | None = None) -> None:
        if token is not None and token != self._svd_token:
            return                                # remplacé ou retiré pendant la lecture
        self._svd_pending = None
        self.device = device
        self.svd_path = os.path.abspath(path)
        if self.api:
            self.api.device = self.device
        for v in list(self.views.views):
            if hasattr(v, "on_svd_loaded"):
                self.safe(v.on_svd_loaded)
        self.status.set(f"SVD chargé : {self.device.name} ({len(self.device.peripherals)} périphériques)")

    def _ask_svd(self) -> None:
        path = filedialog.askopenfilename(filetypes=[("CMSIS-SVD", "*.svd *.xml"), ("Tous", "*")])
        if path:
            self.safe(lambda: self.load_svd(path, background=True))

    def set_current_task(self, name: str | None) -> None:
        """Appelé par la vue RTOS : tâche courante affichée dans la barre d'état."""
        self.current_task = name
        self.task_label.configure(text=f"tâche : {name}" if name else "")

    def add_watch(self, expr: str, fmt: str = "natural") -> None:
        """Var.Watch (`gdbtrace.var_watch`) : expression ajoutée à la vue Watch, au format voulu."""
        from ..views.data import WatchView
        w = self.views.show(WatchView, "data")
        w.add(expr)
        item = w.roots.get(expr.strip())
        if item and w.tree.exists(item) and w.format_of(item) != fmt:
            w.set_format(item, fmt)

    def show_memory(self, addr: int) -> None:
        from ..views.memory import MemoryView
        v = self.views.show(MemoryView, "bottom")
        v.addr_var.set(f"{addr:#010x}")
        self.safe(v.refresh)

    def _on_event(self, kind: str, data: dict) -> None:
        if kind == "stopped":
            self.update_chrono()
            self.refresh_all()
            reason = data.get("reason", "")
            if reason == "signal-received":
                reason = f"signal {data.get('signal-name', '')}"
            self.status.set(f"Arrêt : {stop_summary(data) if 'what' in data else reason}")
            self.report_fault()
        elif kind == "running":
            self.current_frame = None
            self.views.running_all()
            self.status.set("En marche")
        elif kind == "breakpoints":
            self.breakpoints_changed()
        elif kind == "output" and data.get("stream") == "action":   # breakpoints à action
            self.write_console(data.get("text", ""), "err" if data.get("error") else "action")
        elif kind == "exited":
            self.current_frame = None
            code = self.session.exit_code if self.session else None
            sig = data.get("signal-name")
            then = (" — Go le relance, Reset l'arrête au début de main" if self.native
                    else " — reprogrammer l'ELF ou relancer la session")
            self.status.set("Programme terminé" + (f" par {sig}" if sig else f" (code {code})")
                            + then)
            for v in list(self.views.views):
                if hasattr(v, "on_exited"):
                    self.safe(v.on_exited, data)
        elif kind == "threads":
            if data.get("event") == "thread-selected":
                self.refresh_all()              # registres, pile, source : le nouveau thread
            for v in list(self.views.views):
                if hasattr(v, "on_threads"):
                    self.safe(v.on_threads, data)
        elif kind == "output":
            text = data.get("text", "")
            tag = {"target": "target", "log": "err"}.get(data.get("stream", ""), "gdb")
            if data.get("stream") != "log" or "error" in text.lower() or "warning" in text.lower():
                self.write_console(text, tag)
        elif kind == "disconnected":
            self.current_frame = None
            self.security = self._has_security = None
            self.fault_catch_var.set(False)
            self.views.disconnected_all()
            self.trace.close()
            self._rtos_layout = None
            self._teardown_live()
            self.status.set("Déconnecté")
        self.update_status()

    def refresh_all(self) -> None:
        s = self.session
        if s is None or s.state != "stopped":
            return
        try:
            self.current_frame = s.frame()
        except Exception:  # noqa: BLE001
            self.current_frame = None
        self._update_security()
        self._load_breakpoints()
        self.views.refresh_all()
        self.update_status()

    def _update_security(self) -> None:
        """TrustZone : état sécurisé / non sécurisé du cœur arrêté (`self.security`, pour la
        barre d'état et les vues Registres, Pile, Système), avant le rafraîchissement des vues."""
        from ..core import security
        s = self.session
        if self.native or s is None:
            self.security = None
            return
        if self._has_security is None:
            self._has_security = security.has_security(s)
        try:
            self.security = security.security_state(s) if self._has_security else None
        except Exception:  # noqa: BLE001 - registres illisibles : état inconnu
            self.security = None

    def secure_read(self):
        """Lecture de la mémoire sécurisée quand gdb la refuse (QEMU : espace physique)."""
        return getattr(self.live, "read_physical", None)

    def _load_breakpoints(self) -> None:
        try:
            self.cached_breakpoints = self.session.breakpoints() if self.session else []
        except Exception:  # noqa: BLE001
            pass

    def breakpoints_changed(self) -> None:
        if self.session and self.session.state == "stopped":
            self._load_breakpoints()
            self.views.refresh_all()

    def update_status(self) -> None:
        state = self.session.state if self.session else "disconnected"
        colors = {"stopped": ("ARRÊTÉ", "#b03030"), "running": ("RUNNING", "#208020"),
                  "exited": ("TERMINÉ", "#555588"), "disconnected": ("DÉCONNECTÉ", "#777777")}
        text, bg = colors.get(state, (state, "#777"))
        self.state_label.configure(text=text, bg=bg)
        sec = getattr(self, "security", None)
        if sec is not None and state in ("stopped", "running"):
            color = {True: "#8a3c00", False: "#1f5f8b"}.get(sec.secure, "#777777")
            self.secure_label.configure(text=sec.label, bg=color if state == "stopped" else "#aaaaaa")
            if not self.secure_label.winfo_ismapped():
                self.secure_label.pack(side="left", padx=(2, 0), after=self.state_label)
        elif self.secure_label.winfo_ismapped():
            self.secure_label.pack_forget()
        # LIVE vert : canal mémoire latéral disponible (vues rafraîchies en marche)
        self.live_label.configure(bg="#208020" if self.live is not None else "#aaaaaa")
        fr = self.current_frame
        title = "gdbtrace"
        if self.config is not None:
            title += f" — [{self.config.name}{'*' if self.config.modified else ''}]"
        if fr and state == "stopped":
            where = f"{fr.func or '??'}  {os.path.basename(fr.file or '')}:{fr.line or ''}"
            self.root.title(f"{title} — {self._elf_name()}{where}  PC={fr.pc:#010x}")
        else:
            self.root.title(f"{title} — {self._elf_name()}".rstrip(" —:"))

    # ================================================================== actions
    def run_action(self, fn: Callable[[], Any], then: Callable[[Any], None] | None = None) -> None:
        if self.session is None or self.session.state == "disconnected":
            self.status.set("Pas de cible connectée")
            return
        self.pump.background(fn, then=then)

    @property
    def busy(self) -> str | None:
        """Opération en cours qui interdit Go, pas et arrêt (programmation), ou None."""
        return self._exclusive

    def _need(self, state: str) -> bool:
        if self._refuse_if_busy():
            return False
        return self.session is not None and self.session.state == state

    @property
    def native(self) -> bool:
        """La session débogue un programme natif (pas un Cortex-M)."""
        s = self.session
        return s is not None and s.target is not None and s.target.native

    def go(self) -> None:
        if self._need("stopped"):
            self.run_action(self.session.go)
        elif self._need("exited"):              # terminé : seul un programme natif se relance
            if self.native:
                self.run_action(self.session.go)
            else:
                self.status.set("Programme terminé sur la cible : reprogrammer l'ELF ou "
                                "relancer la session")

    # ================================================================== cœurs (--core)
    def setup_cores(self, specs, gdb: str, log) -> None:
        """Ajoute les cœurs décrits (`--core`) à la session connectée, en tâche de fond."""
        from ..core.cores import CoreSet
        session = self.session

        def build():
            return CoreSet.build(session, specs, gdb, lambda line: self.pump.post(log, line))
        self.run_action(build, then=self.set_cores)

    def set_cores(self, core_set) -> None:
        if core_set is None:
            return
        self.core_set = core_set
        if self.api is not None:
            self.api.core_set = core_set
        for c in core_set.cores:
            if c.own_session:
                c.session.on_event(lambda kind, data, s=c.session:
                                   self.pump.post(self._session_event, s, kind, data))
        self._update_cores()

    def _drop_cores(self) -> None:
        cs, self.core_set = self.core_set, None
        if cs is not None:
            self.pump.background(cs.close)
        if hasattr(self, "core_box") and self.core_box.winfo_ismapped():
            self.core_box.pack_forget()

    def _session_event(self, session, kind: str, data: dict) -> None:
        """Événement d'une session : celle des vues le traite ; une autre (cœur d'un autre
        serveur) met à jour le sélecteur de cœur."""
        if kind == "disconnected" and session is self.main_session and session is not self.session:
            self.session = session              # vues sur un autre cœur : retour à la session
            if self.api is not None:
                self.api.s = session
        if session is self.session:
            if kind == "stopped" and self.core_set is not None:
                self.core_set.follow_stop(session)
            self._on_event(kind, data)
            if kind == "disconnected" and session is self.main_session:
                self._drop_cores()
        if self.core_set is not None:
            self._update_cores()

    def _update_cores(self) -> None:
        cs = self.core_set
        if cs is None or len(cs.cores) < 2:
            if self.core_box.winfo_ismapped():
                self.core_box.pack_forget()
            return
        values = [f"{c.name} ({c.state()})" for c in cs.cores]
        self.core_box.configure(values=values)
        if cs.current is not None:
            self.core_var.set(values[cs.cores.index(cs.current)])
        if not self.core_box.winfo_ismapped():
            self.core_box.pack(side="left", padx=(4, 0), after=self.live_label)

    def select_core(self, name: str) -> None:
        """Le cœur `name` devient celui des vues, des pas et de Go / Break. Un cœur d'un autre
        serveur a sa propre session : elle devient la session de l'interface."""
        cs = self.core_set
        if cs is None:
            return
        try:
            core = cs.select(name)
        except Exception as exc:  # noqa: BLE001
            self.report_error(str(exc))
            return
        if core.session is not self.session:
            self.session = core.session
            if self.api is not None:
                self.api.s = core.session
            self.security = self._has_security = None
            self.current_frame = None
        if self.session.state == "stopped":
            self.refresh_all()
        else:
            self.views.running_all()
        self._update_cores()
        self.update_status()
        self.status.set(f"Cœur {core.name} : {core.source}")

    def go_all(self) -> None:
        """Run › Go (tous les cœurs)."""
        if self.core_set is None:
            self.go()
        elif not self._refuse_if_busy():
            self.run_action(self.core_set.go_all)

    def halt_all(self) -> None:
        if self.core_set is None:
            self.halt()
        else:
            self.run_action(self.core_set.halt_all)

    def select_thread(self, thread_id: str) -> None:
        """Le thread devient le thread courant (vues Threads, console). En non-stop, il devient
        le thread suivi, qu'il soit arrêté ou en marche."""
        if self.session is not None and self.session.non_stop:
            self.run_action(lambda: self.session.select_thread(thread_id))
            return
        if not self._need("stopped"):
            self.status.set("Arrêter le programme pour changer de thread")
            return
        self.run_action(lambda: self.session.select_thread(thread_id))

    def stop_thread(self, thread_id: str) -> None:
        """Non-stop : ce thread seul s'arrête (vue Threads)."""
        if self.session is not None:
            self.run_action(lambda: self.session.stop_thread(thread_id))

    def resume_thread(self, thread_id: str) -> None:
        """Non-stop : ce thread seul repart (vue Threads)."""
        if self.session is not None:
            self.run_action(lambda: self.session.resume_thread(thread_id))

    def halt(self) -> None:
        if self._need("running"):
            self.run_action(self.session.halt)

    def step(self) -> None:
        if self._need("stopped"):
            self.run_action(self.session.step)

    def over(self) -> None:
        if self._need("stopped"):
            self.run_action(self.session.step_over)

    def out(self) -> None:
        if self._need("stopped"):
            self.run_action(self.session.step_out)

    def stepi(self) -> None:
        if self._need("stopped"):
            self.run_action(self.session.step_asm)

    def _refuse_if_busy(self) -> bool:
        """Programmation de la flash en cours : la commande attend (vrai = refusée)."""
        if self._exclusive:
            self.status.set(f"{self._exclusive} en cours : patienter")
            return True
        return False

    def reset(self) -> None:
        if not self._refuse_if_busy():
            self.run_action(self.session.reset)

    # ================================================================== trace
    def start_etm_swo(self, cpu_hz: int | None = None, swo_hz: int | None = None,
                      stall: bool | None = None, stm32: bool | None = None) -> list[str] | None:
        """Trace › ETM par SWO… (cible arrêtée) : TPIU formaté sur la broche SWO, capture
        servie par OpenOCD, ETM. Journal dans la console ; None si impossible (la cause est
        aussi affichée par les vues Couverture et Trace)."""
        if not self._need("stopped"):
            self.status.set("Arrêter la cible pour configurer la trace (Break)")
            return None
        self.cpu_hz = cpu_hz or self.cpu_hz
        self.swo_hz = swo_hz or self.swo_hz
        self.etm_stall = self.etm_stall if stall is None else stall
        self.stm32 = self.stm32 if stm32 is None else stm32
        if self.api is not None:
            self.api.cpu_hz = self.cpu_hz
        log = self.safe(lambda: self.trace.start_etm_swo(
            self.session, self.session.target, self.cpu_hz, self.swo_hz, stall=self.etm_stall,
            stm32=self.stm32, port=self.swo_port, cycle_accurate=self.etm_cycle_accurate,
            timestamps=self.etm_timestamps))
        if log:
            self.write_console("\n".join(log) + "\n", "gdb")
            self.status.set(log[-1][:200])
        self.views.refresh_all()
        return log

    def start_etm_etr(self, buffer: tuple[int, int] | None = None) -> list[str] | None:
        """Trace › ETM par l'ETR (cible arrêtée) : la puce écrit la trace en RAM (STM32N6),
        lue à chaque arrêt ; tampon réservé par le firmware (`__etr_buffer__`) ou `buffer`.
        Journal dans la console ; None si impossible (cause aussi dans Couverture et Trace)."""
        if not self._need("stopped"):
            self.status.set("Arrêter la cible pour configurer la trace (Break)")
            return None
        target = self.session.target if self.session else None
        log = self.safe(lambda: self.trace.start_etm_tmc(
            self.session, buffer, stall=self.etm_stall, cycle_accurate=self.etm_cycle_accurate,
            timestamps=self.etm_timestamps,
            live=target.live_memory() if target is not None else None))
        if log:
            self.write_console("\n".join(log) + "\n", "gdb")
            self.status.set(log[-1][:200])
        self.views.refresh_all()
        return log

    def set_etm_filter(self, ranges=(), start=(), stop=()) -> list[str] | None:
        """Trace › Filtre de trace ETM… : plages et start/stop (vides : tout le code)."""
        if self.trace.etm_cfg is not None and not self._need("stopped"):
            self.status.set("Arrêter la cible pour reprogrammer l'ETM (Break)")
            return None
        log = self.safe(lambda: self.trace.set_etm_filter(list(ranges), list(start), list(stop),
                                                          self.session))
        if log:
            self.write_console("\n".join(log) + "\n", "gdb")
            self.status.set(log[-1][:200])
        return log

    def go_till(self, cond: str) -> None:
        """Run › Go.Till : Go jusqu'à ce que la condition C devienne vraie (watchpoint matériel)."""
        cond = cond.strip()
        if not cond:
            return
        if not self._need("stopped"):
            self.status.set("Arrêter la cible d'abord (Break)")
            return
        self._last_till = cond
        self.run_action(lambda: self.session.go_till(cond))

    def step_loop(self, expr: str, change: bool = True, asm: bool = False,
                  max_steps: int = 1000) -> None:
        """Run › Step.Change (`change`) ou Step.Till : pas à pas jusqu'au changement de valeur
        de `expr`, ou jusqu'à ce qu'elle soit vraie ; Break interrompt la boucle."""
        expr = expr.strip()
        if not expr:
            return
        if not self._need("stopped"):
            self.status.set("Arrêter la cible d'abord (Break)")
            return
        self._last_step = (expr, change, asm, max_steps)
        fn = self.session.step_change if change else self.session.step_till
        self.run_action(lambda: fn(expr, asm, max_steps))

    def _ask_go_till(self) -> None:
        cond = simpledialog.askstring("Go.Till", "Go jusqu'à ce que cette condition C devienne vraie :",
                                      initialvalue=self._last_till, parent=self.root)
        if cond:
            self.go_till(cond)

    def _ask_step_loop(self) -> None:
        d = StepTillDialog(self.root, self._last_step)
        if d.values:
            self.step_loop(*d.values)

    def _ask_etm_filter(self) -> None:
        cur = self.trace.etm_filter_specs or ([], [], [])
        d = EtmFilterDialog(self.root, *cur)
        if d.values is not None:
            self.set_etm_filter(*d.values)

    def _ask_etm_swo(self) -> None:
        if not self._need("stopped"):
            self.status.set("Arrêter la cible pour configurer la trace (Break)")
            return
        from ..trace.etm.config import swo_core_limit_hz
        version = self.trace.etm.version if self.trace.etm is not None else 3
        stall = self.etm_stall or self.cpu_hz > swo_core_limit_hz(self.swo_hz, version)
        d = EtmSwoDialog(self.root, self.cpu_hz, self.swo_hz, stall, self.stm32)
        if d.values:
            self.start_etm_swo(*d.values)

    def toggle_breakpoint_at(self, file: str | None = None, line: int | None = None,
                             addr: int | None = None) -> None:
        if not self._need("stopped"):
            self.status.set("Arrêter la cible pour modifier les breakpoints")
            return
        existing = None
        for bp in self.cached_breakpoints:
            if addr is not None and bp.addr and bp.addr.startswith("0x") and int(bp.addr, 16) == addr:
                existing = bp
            elif file and line and bp.line == line and bp.file and \
                    os.path.basename(bp.file) == os.path.basename(file):
                existing = bp
        if existing:
            self.safe(self.session.delete_break, existing.number)
        else:
            loc = f"*{addr:#x}" if addr is not None else f"{file}:{line}"
            self.safe(self.session.break_, loc)
        self.breakpoints_changed()

    def show_source(self, file: str, line: int) -> None:
        from ..views.code import SourceView
        from ..views.files import FilesView
        v = self.views.show(SourceView, "code")
        v.follow.set(False)
        v.show_file(file, line)
        fv = self.views.find(FilesView)
        if fv is not None:
            fv.reveal(file)

    # ================================================================== console
    def execute(self, line: str, done: Callable[[bool], None] | None = None) -> None:
        """Ligne de commande : Python, `!cmd` = CLI gdb, `mon cmd` = monitor."""
        post_done = (lambda more: self.pump.post(done, more)) if done else None
        stripped = line.strip()
        if self.console is None:
            self.write_console("pas de session\n", "err")
            if done:
                done(False)
            return
        if stripped.startswith("!") or stripped.startswith("mon "):
            def job():
                if stripped.startswith("!"):
                    return self.session.cli(stripped[1:])
                return self.session.monitor(stripped[4:])
            def show(out):
                # console() capture la sortie sans la diffuser : on l'affiche ici.
                if out:
                    self.write_console(out if out.endswith("\n") else out + "\n", "gdb")
                if done:
                    done(False)
            self.pump.background(job, then=show)
            return
        self.console.push(line, done=post_done)

    def write_console(self, text: str, tag: str = "") -> None:
        if not self.console_views:
            self._pending_output.append((text, tag))
            return
        for v in self.console_views:
            for t, g in self._pending_output:
                v.write(t, g)
            v.write(text, tag)
        self._pending_output.clear()

    def run_script(self, path: str, then: Callable[[bool], None] | None = None) -> None:
        """Exécute un script dans la console ; `then` est appelé à sa fin (fil de la console)."""
        if self.console:
            self.write_console(f"# script {path}\n", "cmd")
            self.console.run_file(path, then)

    def _ask_script(self) -> None:
        path = filedialog.askopenfilename(filetypes=[("Python", "*.py"), ("Tous", "*")])
        if path:
            self.run_script(path)

    # ================================================================== symboles ajoutés
    def add_symbols(self, spec: str) -> None:
        """Symboles d'un autre ELF (`ELF[@décalage]`) en plus de ceux de la session : gdb, puis
        table des symboles, trace et vues (Fichier › Ajouter des symboles…, `--add-symbols`)."""
        from ..core.memtools import split_address
        if self.session is None or self.session.state == "disconnected":
            self.report_error("ajouter des symboles : pas de session")
            return
        path, offset = split_address(spec)
        if self.safe(self.session.add_symbols, path, offset or 0) is None and \
                os.path.abspath(path) in (p for p, _o in self.session.extra_symbols):
            self.write_console(f"symboles ajoutés : {path}\n", "gdb")
            self._program_changed(self.session.elf)

    def _ask_add_symbols(self) -> None:
        path = filedialog.askopenfilename(title="ELF dont les symboles s'ajoutent",
                                          filetypes=[("Exécutables ELF", "*.elf *.axf *.out"), ("Tous", "*")])
        if path:
            self.add_symbols(path)

    # ================================================================== fautes
    def report_fault(self) -> None:
        """Arrêt dans un gestionnaire de faute : la cause dans la barre d'état et la console,
        la vue Faute au premier plan. Hors faute, une seule lecture (ICSR)."""
        if self.native or self.session is None or self.session.state != "stopped":
            return
        from ..core import fault
        from ..views.fault import FaultView
        view = self.views.find(FaultView)
        report = view.report if view is not None and view.report_stop == self.session.stop_count else None
        try:
            if report is None:
                if not fault.in_fault(self.session):
                    return
                report = fault.analyze(self.session)
        except Exception:  # noqa: BLE001 - registres illisibles : pas d'analyse
            return
        if not report.in_fault:
            return
        self.status.set(f"Faute : {report.summary()}")
        self.write_console(report.text() + "\n", "err")
        if view is not None:
            self.views.show(FaultView, "data")

    def toggle_fault_catch(self) -> None:
        """Run › Arrêt sur faute (aussi la case de la vue Faute, `--break-on-fault`)."""
        want = self.fault_catch_var.get()
        if self.api is None or self.native:
            self.fault_catch_var.set(False)
            return
        if self.session.state != "stopped":
            self.fault_catch_var.set(not want)
            self.report_error("arrêt sur faute : arrêter la cible d'abord")
            return
        msg = self.safe(self.api.break_on_fault, want)
        if msg:
            self.write_console(msg + "\n", "gdb")
        self.fault_catch_var.set(self.api.fault_catch_method() is not None)

    # ================================================================== chrono
    def update_chrono(self) -> None:
        """Arrêt visible : temps de la dernière exécution, lu dans la cible (barre d'état)."""
        if self.api is None or self.session is None or self.session.state != "stopped":
            return
        from ..core.chrono import format_duration
        try:
            m = self.api._chrono.stop()
        except Exception as exc:  # noqa: BLE001 - mesure impossible : pas d'affichage
            self.report_error(f"chrono : {exc}")
            return
        if m is None:
            return
        chrono = self.api._chrono
        last = format_duration(m.seconds(self.cpu_hz)) if m.cycles is not None else "?"
        total = format_duration(chrono.total / self.cpu_hz) if self.cpu_hz else "?"
        approx = " ≈" if m.note else ""
        self.chrono_label.configure(text=f"⏱{approx} {last}  (Σ {total}, {m.source})")

    def reset_chrono(self) -> None:
        """Run › Remettre le chrono à zéro : cumul effacé."""
        if self.api is not None:
            self.api._chrono.reset()
        self.chrono_label.configure(text="⏱ Σ 0")

    # ================================================================== configuration
    def apply_config(self, config: SessionConfig, load_svd: bool = True) -> None:
        """Ouvre une configuration **sans connecter** : titre, réglages (fréquence, options du
        menu Trace), SVD, vues selon l'OS, configurations récentes. Configuration › Connecter
        lance ensuite la session."""
        args = config.parse()
        self.config = config
        if config.path:
            self._remember_config(config.path)
        self.cpu_hz = args.cpu_hz
        self.etm_cycle_accurate = args.etm_cycle_accurate
        self.swo_hz, self.swo_port, self.etm_stall, self.stm32 = (args.swo_hz, args.swo_port,
                                                                  args.etm_stall, args.stm32)
        self.verify_program = not args.no_verify
        self.verify_var.set(self.verify_program)
        if self.api:
            self.api.cpu_hz = self.cpu_hz
            self.api._config = config.data
        self.apply_os_views(config.os)
        if load_svd:
            if args.svd:
                path, warning = svd.locate(args.svd)
                if warning:
                    self.report_error(warning)
                if path and self.wants_svd(path):
                    self.safe(lambda: self.load_svd(path, background=True))
            else:
                self.unload_svd()
        self.update_status()
        self.status.set(f"Configuration {config.name} : Configuration › Connecter lance la session")

    def apply_os_views(self, os_name: str | None) -> None:
        """Vues selon l'OS : FreeRTOS → RTOS et Temps des tâches ; Pthread → Threads ; aucun →
        aucune des trois ; None (sans configuration) → toutes, à leur place par défaut."""
        wanted = ALL_OS_VIEWS if os_name is None else OS_VIEWS.get(os_name, set())
        classes = view_classes()
        place = {e["kind"]: e["where"] for e in DEFAULT_LAYOUT}
        selected = {name: nb.select() for name, nb in self.views.notebooks.items()}
        for kind in sorted(ALL_OS_VIEWS):
            view = self.views.find(classes[kind])
            if kind in wanted and view is None:
                self.views.open(classes[kind], place[kind])
            elif kind not in wanted and view is not None:
                view.destroy()
        for name, nb in self.views.notebooks.items():      # onglet affiché inchangé
            if selected[name] and selected[name] in nb.tabs():
                nb.select(selected[name])
        self.os_hidden = ALL_OS_VIEWS - wanted

    def new_config(self) -> None:
        if not self._confirm_discard():
            return
        self._edit(SessionConfig({"gdbtrace": 1}), "Nouvelle configuration")

    def edit_config(self) -> None:
        if self.config is None:
            self.new_config()
            return
        self._edit(self.config.copy(), f"Configuration — {self.config.name}")

    def _edit(self, config: SessionConfig, title: str):
        from .config_dialog import ConfigEditor
        return ConfigEditor(self.root, config, on_ok=lambda cfg: self.safe(self.apply_config, cfg),
                            title=title)

    def open_config(self, path: str) -> bool:
        """Configuration › Ouvrir… / Récentes : charge le YAML et l'applique, sans connecter."""
        if not self._confirm_discard():
            return False
        try:
            config = SessionConfig.load(path)
        except ConfigError as exc:
            self.report_error(f"configuration {os.path.basename(path)} : {exc}")
            return False
        self.apply_config(config)
        return True

    def _ask_open_config(self) -> None:
        path = filedialog.askopenfilename(
            title="Ouvrir une configuration",
            initialdir=os.path.dirname(self.recent_configs[0]) if self.recent_configs else os.getcwd(),
            filetypes=[("Configurations GdbTrace", "*.yaml *.yml"), ("Tous", "*")])
        if path:
            self.open_config(path)

    def save_config(self, ask: bool = False) -> bool:
        """Configuration › Enregistrer (Ctrl+S) / Enregistrer sous… ; False si annulé."""
        if self.config is None:
            self.status.set("Pas de configuration : Configuration › Nouvelle…")
            return False
        path = self.config.path
        if ask or not path:
            path = filedialog.asksaveasfilename(
                title="Enregistrer la configuration", defaultextension=".yaml",
                initialfile=f"{self.config.name}.yaml",
                initialdir=os.path.dirname(path) if path else os.getcwd(),
                filetypes=[("Configurations GdbTrace", "*.yaml *.yml")])
            if not path:
                return False
        try:
            self.config.save(path)
        except (OSError, ConfigError) as exc:
            self.report_error(f"configuration non enregistrée : {exc}")
            return False
        self._remember_config(self.config.path)
        self.update_status()
        self.status.set(f"Configuration enregistrée : {self.config.path}")
        return True

    def connect_config(self) -> None:
        """Configuration › Connecter : la session décrite par la configuration (la session en
        cours est d'abord déconnectée)."""
        if self.config is None:
            self.status.set("Pas de configuration : Configuration › Ouvrir… ou Nouvelle…")
            return
        try:
            args = self.config.parse()
        except ConfigError as exc:
            self.report_error(str(exc))
            return
        if not args.elf:
            self.report_error(f"configuration {self.config.name} : pas d'ELF (onglet Cible)")
            return
        self.start_session(args)

    def start_session(self, args) -> None:
        """Lance la session `args` ; la session en cours est d'abord déconnectée. Une
        configuration fermée entre-temps annule ce lancement."""
        from ..__main__ import start_gui_session
        self._start_token += 1
        token = self._start_token

        def start(_r=None) -> None:
            if token == self._start_token:
                start_gui_session(self, args)
        s = self.session
        if s is not None and s.state != "disconnected":
            self.status.set("Déconnexion de la session en cours…")
            self.pump.background(s.disconnect, then=start)
        else:
            start()

    def close_config(self) -> None:
        """Configuration › Fermer : déconnecte, retire la configuration et son SVD, rouvre les
        vues fermées par son OS."""
        if not self._confirm_discard():
            return
        self._start_token += 1                  # un Connecter en attente n'aura pas lieu
        s = self.session
        if s is not None and s.state != "disconnected":
            self.pump.background(s.disconnect)
        self.config = None
        if self.api:
            self.api._config = None
        self.unload_svd()
        self.apply_os_views(None)
        self.update_status()
        self.status.set("Configuration fermée")

    def _confirm_discard(self) -> bool:
        """Configuration modifiée et pas enregistrée : la question est posée. False = annulé."""
        cfg = self.config
        if cfg is None or not cfg.modified:
            return True
        answer = self.ask_save("Configuration modifiée",
                               f"Enregistrer la configuration « {cfg.name} » ?")
        if answer is None:
            return False
        return self.save_config() if answer else True

    def _remember_config(self, path: str) -> None:
        path = os.path.abspath(path)
        self.recent_configs = [path] + [p for p in self.recent_configs if p != path][:7]

    def _fill_recent_configs(self) -> None:
        self.recent_config_menu.delete(0, "end")
        if not self.recent_configs:
            self.recent_config_menu.add_command(label="(aucune)", state="disabled")
        for path in self.recent_configs:
            self.recent_config_menu.add_command(label=path, command=lambda p=path: self.open_config(p))

    # ================================================================== erreurs
    def safe(self, fn: Callable, *args: Any) -> Any:
        try:
            return fn(*args)
        except Exception as exc:  # noqa: BLE001
            self.report_error(f"{type(exc).__name__}: {exc}")
            return None

    def safe_refresh(self, fn: Callable[[], Any]) -> Any:
        """Rafraîchissement d'une vue après un arrêt : comme safe(), sauf quand la cible a
        repris entre-temps (un script ou la console relance juste après l'arrêt) ; gdb refuse
        alors de lire (« Selected thread is running »). La vue est périmée, pas en erreur : le
        prochain arrêt la rafraîchira."""
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001
            if self.session is not None and self.session.state != "stopped":
                return None
            self.report_error(f"{type(exc).__name__}: {exc}")
            return None

    def report_error(self, msg: str) -> None:
        self.write_console(msg.rstrip() + "\n", "err")
        self.status.set(msg.splitlines()[-1][:200] if msg else "")

    # ================================================================== layout
    def restore_layout(self) -> None:
        layout = DEFAULT_LAYOUT
        if self.layout_file and os.path.exists(self.layout_file):
            try:
                with open(self.layout_file, encoding="utf-8") as fh:
                    data = json.load(fh)
                layout = list(data.get("views", DEFAULT_LAYOUT))
                self.recent_elfs = [p for p in data.get("recent_elfs", []) if isinstance(p, str)]
                self.recent_configs = [p for p in data.get("recent_configs", []) if isinstance(p, str)]
                # vues apparues depuis la sauvegarde : ajoutées à leur place par défaut
                known = set(data.get("known_kinds", [e.get("kind") for e in layout]))
                layout += [e for e in DEFAULT_LAYOUT if e["kind"] not in known]
                if data.get("geometry"):
                    self.root.geometry(data["geometry"])
            except Exception:  # noqa: BLE001
                self.report_error("layout illisible, layout par défaut\n" + traceback.format_exc())
        classes = view_classes()
        for entry in layout:
            cls = classes.get(entry.get("kind"))
            if cls:
                self.views.open(cls, entry.get("where"), entry.get("state"), entry.get("geometry"))
        for nb in self.views.notebooks.values():
            if nb.tabs():
                nb.select(0)

    def save_layout(self) -> None:
        if not self.layout_file:
            return
        # les vues fermées par l'OS de la configuration ne sont pas « connues » : un lancement
        # sans configuration les rouvre à leur place par défaut
        data = {"geometry": self.root.geometry(), "views": self.views.layout(),
                "known_kinds": sorted(set(view_classes()) - self.os_hidden),
                "recent_elfs": self.recent_elfs, "recent_configs": self.recent_configs}
        with open(self.layout_file, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)

    def mainloop(self) -> None:
        self.root.mainloop()


def ask_yes_no(title: str, msg: str) -> bool:
    return messagebox.askyesno(title, msg)


class StepTillDialog(simpledialog.Dialog):
    """Run › Step.Till / Step.Change… : expression, mode, pas assembleur, pas au plus.
    `values` : (expression, change, asm, max_steps), ou None si annulé."""

    def __init__(self, parent, init: tuple | None):
        self.values = None
        self._init = init or ("", True, False, 1000)
        super().__init__(parent, "Step.Till / Step.Change")

    def body(self, master):
        expr, change, asm, max_steps = self._init
        self.expr_var = tk.StringVar(value=expr)
        self.mode_var = tk.StringVar(value="change" if change else "till")
        self.asm_var = tk.BooleanVar(value=asm)
        self.max_var = tk.StringVar(value=str(max_steps))
        ttk.Label(master, text="Expression C :").grid(row=0, column=0, sticky="w")
        first = ttk.Entry(master, textvariable=self.expr_var, width=36)
        first.grid(row=0, column=1, padx=4, pady=2)
        ttk.Radiobutton(master, text="jusqu'à ce que sa valeur change (Step.Change)", value="change",
                        variable=self.mode_var).grid(row=1, column=0, columnspan=2, sticky="w")
        ttk.Radiobutton(master, text="jusqu'à ce qu'elle soit vraie (Step.Till)", value="till",
                        variable=self.mode_var).grid(row=2, column=0, columnspan=2, sticky="w")
        ttk.Checkbutton(master, text="Pas assembleur (sinon : ligne source)",
                        variable=self.asm_var).grid(row=3, column=0, columnspan=2, sticky="w")
        ttk.Label(master, text="Pas au plus :").grid(row=4, column=0, sticky="w")
        ttk.Entry(master, textvariable=self.max_var, width=10).grid(row=4, column=1, sticky="w", padx=4)
        ttk.Label(master, text="Un aller-retour avec la cible par pas ; Break arrête la boucle.",
                  foreground="#555555").grid(row=5, column=0, columnspan=2, sticky="w", pady=(4, 0))
        return first

    def validate(self) -> bool:
        try:
            n = int(self.max_var.get(), 0)
        except ValueError:
            n = 0
        if not self.expr_var.get().strip() or n <= 0:
            messagebox.showerror("Step.Till / Step.Change",
                                 "Une expression, et un nombre de pas entier positif", parent=self)
            return False
        self.values = (self.expr_var.get().strip(), self.mode_var.get() == "change",
                       self.asm_var.get(), n)
        return True


class EtmFilterDialog(simpledialog.Dialog):
    """Trace › Filtre de trace ETM… : listes séparées par des virgules. `values` :
    (plages, démarrages, arrêts), ou None si annulé."""

    def __init__(self, parent, ranges, start, stop):
        self.values = None
        self._init = (ranges, start, stop)
        super().__init__(parent, "Filtre de trace ETM")

    def body(self, master):
        rows = [("Plages tracées :", "fonctions, fichiers .c, 0x100-0x200"),
                ("Démarrer à :", "fonction, fichier.c:ligne, adresse"),
                ("Arrêter après :", "fonction, fichier.c:ligne, adresse")]
        self.vars = []
        first = None
        for r, ((label, hint), init) in enumerate(zip(rows, self._init)):
            ttk.Label(master, text=label).grid(row=2 * r, column=0, sticky="w")
            var = tk.StringVar(value=", ".join(init))
            e = ttk.Entry(master, textvariable=var, width=40)
            e.grid(row=2 * r, column=1, padx=4, pady=(4, 0))
            ttk.Label(master, text=hint, foreground="#555555").grid(row=2 * r + 1, column=1, sticky="w", padx=4)
            self.vars.append(var)
            first = first or e
        ttk.Label(master, text="Tout vide : tracer tout le code.", foreground="#555555").grid(
            row=6, column=0, columnspan=2, sticky="w", pady=(6, 0))
        return first

    def apply(self) -> None:
        self.values = tuple([x.strip() for x in v.get().split(",") if x.strip()] for v in self.vars)


class EtmSwoDialog(simpledialog.Dialog):
    """Trace › ETM par SWO… : fréquence du cœur, débit SWO, stall, broches STM32. `values` :
    (cpu_hz, swo_hz, stall, stm32), ou None si annulé."""

    def __init__(self, parent, cpu_hz: int, swo_hz: int, stall: bool, stm32: bool):
        self.values = None
        self._init = (cpu_hz, swo_hz, stall, stm32)
        super().__init__(parent, "ETM par SWO")

    def body(self, master):
        cpu, swo, stall, stm32 = self._init
        self.cpu_var, self.swo_var = tk.StringVar(value=str(cpu)), tk.StringVar(value=str(swo))
        self.stall_var, self.stm32_var = tk.BooleanVar(value=stall), tk.BooleanVar(value=stm32)
        ttk.Label(master, text="Fréquence du cœur (Hz) :").grid(row=0, column=0, sticky="w")
        first = ttk.Entry(master, textvariable=self.cpu_var, width=14)
        first.grid(row=0, column=1, padx=4, pady=2)
        ttk.Label(master, text="Débit SWO (Hz) :").grid(row=1, column=0, sticky="w")
        ttk.Entry(master, textvariable=self.swo_var, width=14).grid(row=1, column=1, padx=4, pady=2)
        ttk.Checkbutton(master, text="Ralentir le cœur plutôt que perdre de la trace (stall)",
                        variable=self.stall_var).grid(row=2, column=0, columnspan=2, sticky="w")
        ttk.Checkbutton(master, text="Broches de trace STM32 (DBGMCU_CR)",
                        variable=self.stm32_var).grid(row=3, column=0, columnspan=2, sticky="w")
        ttk.Label(master, text="Capture servie par OpenOCD ; formateur TPIU : ETM (ID 2) et ITM (ID 1).",
                  foreground="#555555").grid(row=4, column=0, columnspan=2, sticky="w", pady=(4, 0))
        return first

    def validate(self) -> bool:
        try:
            cpu, swo = int(self.cpu_var.get(), 0), int(self.swo_var.get(), 0)
        except ValueError:
            cpu = swo = 0
        if cpu <= 0 or swo <= 0:
            messagebox.showerror("ETM par SWO", "Fréquences en Hz, entières et positives", parent=self)
            return False
        self.values = (cpu, swo, self.stall_var.get(), self.stm32_var.get())
        return True
