"""Fenêtre principale de gdbtrace."""
from __future__ import annotations

import json
import os
import tkinter as tk
import traceback
from tkinter import filedialog, messagebox, ttk
from typing import Any, Callable

from ..core import svd
from ..core.live import LiveMemory, LivePoller
from ..core.scripting import Console, ScriptAPI
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
    from ..views.files import FilesView
    from ..views.timing import TimingView
    from ..views.trace import TraceView
    classes = [FilesView, SourceView, DisasmView, TraceView, TimingView, RegistersView, WatchView, PeripheralsView,
               RtosView, StackView, BreakpointsView, MemoryView, ConsoleView, ItmView, ProfileView]
    return {c.kind: c for c in classes}


DEFAULT_LAYOUT = [
    {"kind": "files", "where": "left"},
    {"kind": "source", "where": "code"},
    {"kind": "disasm", "where": "code"},
    {"kind": "trace", "where": "code"},
    {"kind": "timing", "where": "code"},
    {"kind": "registers", "where": "data"},
    {"kind": "watch", "where": "data"},
    {"kind": "peripherals", "where": "data"},
    {"kind": "rtos", "where": "data"},
    {"kind": "stack", "where": "data"},
    {"kind": "breakpoints", "where": "data"},
    {"kind": "console", "where": "bottom"},
    {"kind": "memory", "where": "bottom"},
    {"kind": "itm", "where": "bottom"},
    {"kind": "profile", "where": "bottom"},
]


class App:
    def __init__(self, root: tk.Tk | None = None, layout_file: str | None = None):
        self.root = root or tk.Tk()
        self.root.title("gdbtrace")
        self.root.geometry("1400x900")
        self.layout_file = layout_file
        style = ttk.Style(self.root)
        if "clam" in style.theme_names():
            style.theme_use("clam")

        self.pump = EventPump(self.root)
        self.pump.on_error = self.report_error
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
        self.current_task: str | None = None   # tâche RTOS courante (vue RTOS)
        self._rtos_layout = None               # disposition FreeRTOS (calculée à l'arrêt)
        # trace SWO/ITM (capture, texte ITM, exceptions) et fréquence du cœur pour la configurer
        self.trace = TraceManager(post=self.pump.post, on_error=self.report_error)
        self.cpu_hz = 25_000_000
        self.coverage_in_source = False        # couverture ETM affichée dans la vue Source
        self.etm_cycle_accurate = False        # ETM matériel en mode cycle-accurate (ETMCR bit 12)
        # description des périphériques (CMSIS-SVD)
        self.device: svd.Device | None = None
        self.svd_path: str | None = None
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
        m.add_command(label="Exécuter un script…", command=self._ask_script)
        m.add_command(label="Charger un SVD…", command=self._ask_svd)
        m.add_command(label="Recharger l'ELF (load)", command=lambda: self.run_action(self.session.load))
        m.add_separator()
        m.add_command(label="Enregistrer le layout", command=self.save_layout)
        m.add_command(label="Déconnecter", command=self.disconnect)
        m.add_command(label="Quitter", command=self.quit)
        mb.add_cascade(label="Fichier", menu=m)

        r = tk.Menu(mb, tearoff=False)
        for label, fn, key in [("Go", self.go, "F5"), ("Break", self.halt, "F8"),
                               ("Step", self.step, "F11"), ("Step Over", self.over, "F10"),
                               ("Step Out (Go.Return)", self.out, "Shift+F11"),
                               ("Step instruction", self.stepi, "F7"), ("Reset", self.reset, "")]:
            r.add_command(label=label, command=fn, accelerator=key)
        mb.add_cascade(label="Run", menu=r)

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
        self.live_label = tk.Label(bar, width=6, text="LIVE", fg="white", bg="#aaaaaa")
        self.live_label.pack(side="left", padx=(2, 0))
        self.task_label = ttk.Label(bar, text="", width=22)
        self.task_label.pack(side="left", padx=(6, 0))
        ttk.Label(bar, textvariable=self.status).pack(side="left", padx=6)

    def _bind_keys(self) -> None:
        keys = {"<F5>": self.go, "<F8>": self.halt, "<F11>": self.step, "<F10>": self.over,
                "<Shift-F11>": self.out, "<F7>": self.stepi}
        for k, fn in keys.items():
            self.root.bind_all(k, lambda _e, f=fn: (f(), "break")[1])

    # ================================================================== session
    def attach(self, session: Session) -> None:
        """Associe une session déjà connectée (ou en cours) à la GUI."""
        self.session = session
        self.api = ScriptAPI(session)
        self.api.cpu_hz = self.cpu_hz
        self.console = Console(self.api, lambda s: self.pump.post(self.write_console, s, ""))
        session.on_event(lambda kind, data: self.pump.post(self._on_event, kind, data))
        if session.elf and os.path.exists(session.elf):
            try:
                self.symbols = SymbolTable(session.elf)
            except Exception as exc:  # noqa: BLE001
                self.report_error(f"lecture des symboles : {exc}")
        self.api.device = self.device
        self.api.trace = self.trace
        self.api.symbols = self.symbols
        self._rtos_layout = None
        if session.elf and self.symbols is not None:
            self.trace.setup_etm(session.elf, self.symbols, cycle_accurate=self.etm_cycle_accurate)
        if session.target is not None and session.state != "disconnected":
            self._setup_live(session.target)
        if session.state == "stopped":
            self.refresh_all()
        self.update_status()

    def connect(self, elf: str, target: TargetServer, gdb: str = "gdb-multiarch",
                load: bool | None = None, then: Callable[[], None] | None = None) -> None:
        session = Session(gdb)
        self.attach(session)
        self.status.set(f"Connexion à {target.remote}…")

        def done(_r):
            if session.elf and not self.symbols:
                self.symbols = SymbolTable(session.elf)
            self._setup_live(target)
            self.refresh_all()
            if then:
                then()
        self.pump.background(session.connect, elf, target, load, then=done)

    def disconnect(self) -> None:
        if self.session:
            s = self.session
            self.pump.background(s.disconnect)

    def quit(self) -> None:
        try:
            self.save_layout()
        except Exception:  # noqa: BLE001
            pass
        for v in list(self.views.views):          # arrête les échantillonneurs (vue Profil)
            if hasattr(v, "stop") and v.kind == "profile":
                self.safe(v.stop)
        self.trace.close()
        self._teardown_live()
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
        if self.live is not None:
            self.poller = LivePoller(
                self.live,
                is_running=lambda: self.session is not None and self.session.state == "running",
                post=self.pump.post, period_ms=self.live_period_ms, on_error=self.report_error)
            self.poller.start()
        if self.api:
            self.api.live = self.live
        from ..core.target import QemuTarget
        if isinstance(target, QemuTarget) and target.etm_sim and self.live is not None:
            if self.trace.etm is None and self.session and self.session.elf and self.symbols:
                msg = self.trace.setup_etm(self.session.elf, self.symbols)
                if msg:
                    self.report_error(msg)
            if self.trace.etm is not None and self.trace.etm_sim is None:
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
        if self.api:
            self.api.live = None

    def rtos_layout(self):
        """Disposition FreeRTOS partagée (vue RTOS, profilage par tâche), calculée une fois
        via gdb quand la cible est arrêtée ; None sans FreeRTOS."""
        from ..core.rtos import freertos as fr
        if self._rtos_layout is None and fr.detect(self.symbols) and \
                self.session is not None and self.session.state == "stopped":
            self._rtos_layout = fr.FreeRtosLayout.from_session(self.session, self.symbols)
        return self._rtos_layout

    def load_svd(self, path: str) -> None:
        self.device = svd.parse(path)
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
            self.safe(self.load_svd, path)

    def set_current_task(self, name: str | None) -> None:
        """Appelé par la vue RTOS : tâche courante affichée dans la barre d'état."""
        self.current_task = name
        self.task_label.configure(text=f"tâche : {name}" if name else "")

    def show_memory(self, addr: int) -> None:
        from ..views.memory import MemoryView
        v = self.views.show(MemoryView, "bottom")
        v.addr_var.set(f"{addr:#010x}")
        self.safe(v.refresh)

    def _on_event(self, kind: str, data: dict) -> None:
        if kind == "stopped":
            self.refresh_all()
            reason = data.get("reason", "")
            if reason == "signal-received":
                reason = f"signal {data.get('signal-name', '')}"
            self.status.set(f"Arrêt : {reason}")
        elif kind == "running":
            self.current_frame = None
            self.views.running_all()
            self.status.set("En marche")
        elif kind == "breakpoints":
            self.breakpoints_changed()
        elif kind == "output":
            text = data.get("text", "")
            tag = {"target": "target", "log": "err"}.get(data.get("stream", ""), "gdb")
            if data.get("stream") != "log" or "error" in text.lower() or "warning" in text.lower():
                self.write_console(text, tag)
        elif kind == "disconnected":
            self.current_frame = None
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
        self._load_breakpoints()
        self.views.refresh_all()
        self.update_status()

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
                  "disconnected": ("DÉCONNECTÉ", "#777777")}
        text, bg = colors.get(state, (state, "#777"))
        self.state_label.configure(text=text, bg=bg)
        # LIVE vert : canal mémoire latéral disponible (vues rafraîchies en marche)
        self.live_label.configure(bg="#208020" if self.live is not None else "#aaaaaa")
        fr = self.current_frame
        if fr and state == "stopped":
            where = f"{fr.func or '??'}  {os.path.basename(fr.file or '')}:{fr.line or ''}"
            self.root.title(f"gdbtrace — {where}  PC={fr.pc:#010x}")
        else:
            self.root.title("gdbtrace")

    # ================================================================== actions
    def run_action(self, fn: Callable[[], Any], then: Callable[[Any], None] | None = None) -> None:
        if self.session is None or self.session.state == "disconnected":
            self.status.set("Pas de cible connectée")
            return
        self.pump.background(fn, then=then)

    def _need(self, state: str) -> bool:
        return self.session is not None and self.session.state == state

    def go(self) -> None:
        if self._need("stopped"):
            self.run_action(self.session.go)

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

    def reset(self) -> None:
        self.run_action(self.session.reset)

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

    def run_script(self, path: str) -> None:
        if self.console:
            self.write_console(f"# script {path}\n", "cmd")
            self.console.run_file(path)

    def _ask_script(self) -> None:
        path = filedialog.askopenfilename(filetypes=[("Python", "*.py"), ("Tous", "*")])
        if path:
            self.run_script(path)

    # ================================================================== erreurs
    def safe(self, fn: Callable, *args: Any) -> Any:
        try:
            return fn(*args)
        except Exception as exc:  # noqa: BLE001
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
        data = {"geometry": self.root.geometry(), "views": self.views.layout(),
                "known_kinds": sorted(view_classes())}
        with open(self.layout_file, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)

    def mainloop(self) -> None:
        self.root.mainloop()


def ask_yes_no(title: str, msg: str) -> bool:
    return messagebox.askyesno(title, msg)
