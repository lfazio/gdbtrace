"""PERF : profilage statistique (fonctions, lignes, tâches) et statistiques d'exceptions.

Sources d'échantillons :
- « Live » : PC lu pendant l'exécution par le canal latéral (DWT_PCSR avec OpenOCD,
  vCPU sous QEMU) ;
- « SWO » : paquets « PC sample » du DWT reçus par la capture SWO (vue ITM).
« Auto » choisit SWO si une capture est en cours, sinon Live.
"""
from __future__ import annotations

import os
import tkinter as tk
from tkinter import ttk

from ..trace.profile import PcSampler, Profile
from ..gui.view_base import View

BAR_WIDTH = 30


def _bar(percent: float) -> str:
    n = percent / 100 * BAR_WIDTH
    return "█" * int(n) + ("▌" if n - int(n) >= 0.5 else "")


class ProfileView(View):
    title = "Profil"
    kind = "profile"

    def __init__(self, master, app, **kw):
        super().__init__(master, app, **kw)
        bar = ttk.Frame(self)
        bar.pack(fill="x")
        self.start_btn = ttk.Button(bar, text="▶ Démarrer", command=self.start)
        self.start_btn.pack(side="left", padx=2)
        ttk.Button(bar, text="■ Arrêter", command=self.stop).pack(side="left")
        ttk.Button(bar, text="Réinitialiser", command=self.reset).pack(side="left", padx=2)
        ttk.Label(bar, text="Source :").pack(side="left", padx=(8, 0))
        self.source_var = tk.StringVar(value="Auto")
        ttk.Combobox(bar, textvariable=self.source_var, values=["Auto", "Live", "SWO"],
                     state="readonly", width=6).pack(side="left", padx=2)
        self.per_task_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(bar, text="Par tâche (RTOS)", variable=self.per_task_var).pack(side="left", padx=6)
        self.info = ttk.Label(bar, text="")
        self.info.pack(side="left", padx=8)

        self.tabs = ttk.Notebook(self)
        self.tabs.pack(fill="both", expand=True)
        frames = {name: ttk.Frame(self.tabs) for name in ("Fonctions", "Lignes", "Tâches", "Exceptions")}
        for name, f in frames.items():
            self.tabs.add(f, text=name)
        self.funcs = self._tree(frames["Fonctions"], [("n", "Échant.", 70), ("pct", "%", 60),
                                                      ("bar", "", 260)], ("Fonction", 220))
        self.funcs.bind("<<TreeviewSelect>>", lambda _e: self._show_lines())
        self.funcs.bind("<Double-1>", lambda _e: self._goto_function())
        self.lines = self._tree(frames["Lignes"], [("n", "Échant.", 70), ("pct", "% fonction", 80),
                                                   ("src", "Source", 380)], ("Ligne", 160))
        self.lines.bind("<Double-1>", lambda _e: self._goto_line())
        self.tasks = self._tree(frames["Tâches"], [("n", "Échant.", 70), ("pct", "%", 60),
                                                   ("bar", "", 260)], ("Tâche", 160))
        self.excs = self._tree(frames["Exceptions"], [("n", "Entrées", 70), ("total", "Temps total", 100),
                                                      ("max", "Temps max", 90)], ("Exception", 160))

        self.profile = Profile(app.symbols)
        self.sampler: PcSampler | None = None
        self.running_source: str | None = None
        self._line_items: dict[str, tuple[str, int]] = {}
        self.redraw_when_shown(lambda: self.render() if self.running_source is not None else None)
        self._tick()

    @staticmethod
    def _tree(master, cols, first):
        tv = ttk.Treeview(master, columns=[c[0] for c in cols], show="tree headings", selectmode="browse")
        tv.heading("#0", text=first[0])
        tv.column("#0", width=first[1], stretch=False)
        for cid, label, width in cols:
            tv.heading(cid, text=label)
            tv.column(cid, width=width, stretch=cid == cols[-1][0],
                      anchor="w" if cid in ("bar", "src") else "e")
        sb = ttk.Scrollbar(master, orient="vertical", command=tv.yview)
        tv.configure(yscrollcommand=sb.set)
        tv.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        return tv

    # ------------------------------------------------------------------ démarrage / arrêt
    def _choose_source(self) -> str:
        choice = self.source_var.get()
        if choice == "Auto":
            return "SWO" if self.app.trace.active else "Live"
        return choice

    def start(self) -> None:
        self.stop()
        if self.profile.symbols is None:
            self.profile.symbols = self.app.symbols
        source = self._choose_source()
        if source == "SWO":
            if not self.app.trace.active:
                self.app.status.set("Pas de capture SWO en cours (vue ITM)")
                return
            self.app.trace.set_profile(self.profile)
        else:
            if self.app.live is None:
                self.app.status.set("Pas de canal live pour échantillonner le PC")
                return
            task_of = None
            if self.per_task_var.get():
                layout = self.app.rtos_layout()
                if layout is not None:
                    from ..core.rtos.freertos import CurrentTaskReader
                    task_of = CurrentTaskReader(layout)
            self.sampler = PcSampler(
                self.app.live, self.profile,
                is_running=lambda: self.app.session is not None and self.app.session.state == "running",
                task_of=task_of, on_error=lambda m: self.app.pump.post(self.app.report_error, m))
            self.sampler.start()
        self.running_source = source
        self.start_btn.state(["disabled"])

    def stop(self) -> None:
        if self.sampler is not None:
            self.sampler.stop()
            self.sampler = None
        if self.running_source == "SWO":
            self.app.trace.set_profile(None)
        self.running_source = None
        if self.winfo_exists():
            self.start_btn.state(["!disabled"])
            self.render()

    def reset(self) -> None:
        self.profile.reset()
        self.app.trace.exceptions.reset()
        self.render()

    def on_disconnected(self) -> None:
        self.stop()

    def destroy(self) -> None:
        self.stop()
        super().destroy()

    # ------------------------------------------------------------------ affichage
    def _tick(self) -> None:
        if not self.winfo_exists():
            return
        if self.running_source is not None and self.shown:
            self.render()
        self.after(500, self._tick)

    def render(self) -> None:
        p = self.profile
        src = self.running_source or "arrêté"
        self.info.configure(text=f"{p.total} échantillons, {p.rate:.0f}/s — {src}")
        sel = self.funcs.focus()
        self.funcs.delete(*self.funcs.get_children())
        for r in p.top_functions(200):
            self.funcs.insert("", "end", iid=r.name, text=r.name,
                              values=(r.samples, f"{r.percent:.1f}", _bar(r.percent)))
        if sel and self.funcs.exists(sel):
            self.funcs.focus(sel)
            self.funcs.selection_set(sel)
        self.tasks.delete(*self.tasks.get_children())
        for r in p.task_rows():
            self.tasks.insert("", "end", text=r.name, values=(r.samples, f"{r.percent:.1f}", _bar(r.percent)))
        self.excs.delete(*self.excs.get_children())
        for r in self.app.trace.exceptions.rows():
            self.excs.insert("", "end", text=f"{r.name} ({r.number})",
                             values=(r.entries, r.total_time or "", r.max_time or ""))
        self._show_lines()

    def _show_lines(self) -> None:
        func = self.funcs.focus()
        self.lines.delete(*self.lines.get_children())
        self._line_items.clear()
        if not func:
            return
        for r in self.profile.lines_of(func):
            iid = self.lines.insert("", "end", text=f"{os.path.basename(r.file)}:{r.line}",
                                    values=(r.samples, f"{r.percent:.1f}", _source_line(r.file, r.line)))
            self._line_items[iid] = (r.file, r.line)

    def _goto_line(self) -> None:
        loc = self._line_items.get(self.lines.focus())
        if loc:
            self.app.show_source(*loc)

    def _goto_function(self) -> None:
        func = self.funcs.focus()
        rows = self.profile.lines_of(func) if func else []
        if rows:
            self.app.show_source(rows[0].file, rows[0].line)

    def get_state(self) -> dict:
        return {"source": self.source_var.get(), "per_task": self.per_task_var.get()}

    def set_state(self, state: dict) -> None:
        self.source_var.set(state.get("source", "Auto"))
        self.per_task_var.set(state.get("per_task", True))


_SRC_CACHE: dict[str, list[str]] = {}


def _source_line(path: str, line: int) -> str:
    if path not in _SRC_CACHE:
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                _SRC_CACHE[path] = fh.read().splitlines()
        except OSError:
            _SRC_CACHE[path] = []
    lines = _SRC_CACHE[path]
    return lines[line - 1].strip() if 0 < line <= len(lines) else ""
