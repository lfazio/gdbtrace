"""COVerage.ListModule / ListFunc : couverture de code mesurée par la trace ETM.

Arbre fichiers → fonctions : état (ok / partiel / jamais, écrit en toutes lettres), part des
instructions exécutées, des issues de branchements vues, nombre d'entrées. La base s'accumule
d'exécution en exécution ; elle se remet à zéro, s'enregistre, se fusionne avec une campagne
enregistrée et s'exporte (HTML, LCOV, JSON). « Annoter Source et Désassemblage » colore les
lignes et les instructions (✔ ok, ½ partiel, ✘ jamais) et nomme l'issue manquante des
branchements.
"""
from __future__ import annotations

import os
import tkinter as tk
from tkinter import filedialog, ttk

from ..gui.view_base import View
from ..trace.coverage import NEVER, OK, PARTIAL


def _bar(percent: float, width: int = 10) -> str:
    n = percent / 100 * width
    return "█" * int(n) + ("▌" if n - int(n) >= 0.5 else "")

STATUS_INK = {OK: "#1d6b2f", PARTIAL: "#8a5a00", NEVER: "#a01f1f"}
MARK = {OK: "✔", PARTIAL: "½", NEVER: "✘"}


class CoverageView(View):
    title = "Couverture"
    kind = "coverage"

    def __init__(self, master, app, **kw):
        super().__init__(master, app, **kw)
        bar = ttk.Frame(self)
        bar.pack(fill="x")
        ttk.Button(bar, text="RàZ", width=5, command=self._reset).pack(side="left", padx=2)
        ttk.Button(bar, text="Charger…", command=self._load).pack(side="left")
        ttk.Button(bar, text="Enregistrer…", command=self._save).pack(side="left", padx=2)
        ttk.Button(bar, text="Exporter…", command=self._export).pack(side="left")
        opts = ttk.Frame(self)
        opts.pack(fill="x")
        self.annotate_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(opts, text="Annoter Source et Désassemblage", variable=self.annotate_var,
                        command=self._toggle_annotate).pack(side="left", padx=2)
        self.hide_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(opts, text="Masquer les fonctions couvertes", variable=self.hide_var,
                        command=lambda: self.render(force=True)).pack(side="left", padx=8)
        self.info = ttk.Label(self, text="", anchor="w")
        self.info.pack(fill="x", padx=4, pady=(2, 0))
        body = ttk.Frame(self)
        body.pack(fill="both", expand=True)
        cols = [("status", "État", 78), ("pct", "Instr.", 62), ("bar", "", 70),
                ("n", "", 64), ("br", "Branches", 66), ("issues", "Issues", 58), ("entries", "Entrées", 60)]
        self.tree = ttk.Treeview(body, columns=[c[0] for c in cols], show="tree headings", selectmode="browse")
        self.tree.heading("#0", text="Fichier / fonction")
        self.tree.column("#0", width=200, minwidth=120, stretch=True)
        for cid, label, width in cols:
            self.tree.heading(cid, text=label)
            self.tree.column(cid, width=width, stretch=False, anchor="w" if cid in ("status", "bar") else "e")
        for st, ink in STATUS_INK.items():
            self.tree.tag_configure(st, foreground=ink)
        self.tree.tag_configure("file", font=("TkDefaultFont", 9, "bold"))
        sb = ttk.Scrollbar(body, orient="vertical", command=self.tree.yview)
        hb = ttk.Scrollbar(body, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=sb.set, xscrollcommand=hb.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        sb.grid(row=0, column=1, sticky="ns")
        hb.grid(row=1, column=0, sticky="ew")
        body.rowconfigure(0, weight=1)
        body.columnconfigure(0, weight=1)
        self.tree.bind("<Double-1>", lambda _e: self._goto())
        self.tree.bind("<Return>", lambda _e: self._goto())
        self._loc: dict[str, tuple[str, int]] = {}
        self._gen = None
        app.trace.etm_listeners.append(self._on_new_trace)
        self.render(force=True)
        self.after(1000, self._tick)

    def destroy(self) -> None:
        if self._on_new_trace in self.app.trace.etm_listeners:
            self.app.trace.etm_listeners.remove(self._on_new_trace)
        super().destroy()

    # ------------------------------------------------------------------ données
    @property
    def db(self):
        etm = self.app.trace.etm
        return etm.cov if etm is not None else None

    def _on_new_trace(self) -> None:
        if self.winfo_exists():
            self.render()
            if self.annotate_var.get():
                self._annotate_views()

    def refresh(self) -> None:
        self.render()

    def on_program_changed(self) -> None:
        self._gen = None
        self.render(force=True)

    def _update_info(self) -> None:
        db, tm = self.db, self.app.trace
        # rien de décodé : dire d'où la trace devrait venir et pourquoi elle n'arrive pas
        src = tm.trace_status() if not tm.etm.stats["instructions"] else tm.source_label()
        self.info.configure(text=f"{db.summary()} — {src}")

    def _tick(self) -> None:
        """Tant que rien n'est décodé, l'état de la capture est suivi (connexion, octets)."""
        if not self.winfo_exists():
            return
        if self.db is not None and not self.app.trace.etm.stats["instructions"]:
            self._update_info()
        self.after(1000, self._tick)

    def render(self, force: bool = False) -> None:
        db = self.db
        if db is None:
            self.info.configure(text="Pas de trace d'instructions : --etm-sim / --mtb-sim sous QEMU ; "
                                     "ETM par SWO, --trace / --trace-file ou --mtb sur matériel")
            self.tree.delete(*self.tree.get_children())
            return
        self._update_info()
        key = (id(db), db.generation, self.hide_var.get())
        if not force and key == self._gen:
            return
        self._gen = key
        opened = {self.tree.item(i, "text") for i in self.tree.get_children() if self.tree.item(i, "open")}
        first = not self.tree.get_children()
        self.tree.delete(*self.tree.get_children())
        self._loc.clear()
        sym = self.app.symbols
        hint = os.path.dirname(os.path.dirname(os.path.abspath(db.image.path))) + os.sep
        files = sorted(db.files(), key=lambda m: (not m.path.startswith(hint), m.path))
        for m in files:
            label = os.path.relpath(m.path, hint) if m.path.startswith(hint) else os.path.basename(m.path)
            fid = self.tree.insert("", "end", text=label, values=self._values(m, ""),
                                   tags=("file", m.status),
                                   open=label in opened or (first and m.path.startswith(hint)))
            for f in m.functions:
                if self.hide_var.get() and f.status == OK:
                    continue
                iid = self.tree.insert(fid, "end", text=f.name, values=self._values(f, f.entries),
                                       tags=(f.status,))
                le = sym.line_at(f.start) if sym else None
                if le is not None:
                    self._loc[iid] = (le.file, le.line)

    @staticmethod
    def _values(c, entries) -> tuple:
        br = f"{c.branch_percent:.0f} %" if c.branch_percent is not None else "—"
        issues = f"{c.outcomes}/{2 * c.branches}" if c.branches else ""
        return (f"{MARK[c.status]} {c.status}", f"{c.percent:.1f} %", _bar(c.percent, 6),
                f"{c.covered}/{c.instrs}", br, issues, entries)

    # ------------------------------------------------------------------ actions
    def _goto(self) -> None:
        loc = self._loc.get(self.tree.focus())
        if loc:
            if not self.annotate_var.get():
                self.annotate_var.set(True)
                self._toggle_annotate()
            self.app.show_source(*loc)

    def _reset(self) -> None:
        if self.db is not None:
            self.db.reset()
            self.render(force=True)
            self._annotate_views()

    def _load(self) -> None:
        if self.db is None:
            return
        path = filedialog.askopenfilename(title="Ajouter une couverture enregistrée",
                                          filetypes=[("Couverture gdbtrace", "*.json"), ("Tous", "*")])
        if path:
            self.app.safe(self.db.load, path)
            self.render(force=True)
            self._annotate_views()

    def _save(self) -> None:
        if self.db is None:
            return
        path = filedialog.asksaveasfilename(title="Enregistrer la couverture", defaultextension=".json",
                                            filetypes=[("Couverture gdbtrace", "*.json")])
        if path:
            self.app.safe(self.db.save, path)
            self.app.status.set(f"Couverture enregistrée : {path}")

    def _export(self) -> None:
        if self.db is None:
            return
        path = filedialog.asksaveasfilename(
            title="Exporter la couverture", defaultextension=".html",
            filetypes=[("Rapport HTML", "*.html"), ("LCOV (genhtml, CI)", "*.info"),
                       ("Base gdbtrace", "*.json")])
        if path:
            self.export(path)

    def export(self, path: str) -> None:
        from ..trace.coverage_report import export
        tm = self.app.trace
        note = tm.source_label()
        fmt = self.app.safe(export, self.db, path, note)
        if fmt:
            self.app.status.set(f"Couverture exportée ({fmt}) : {path}")

    # ------------------------------------------------------------------ annotation
    def _toggle_annotate(self) -> None:
        self.app.coverage_in_source = self.annotate_var.get()
        self._annotate_views()

    def _annotate_views(self) -> None:
        from .code import DisasmView, SourceView
        for v in self.app.views.views:
            if isinstance(v, SourceView):
                self.app.safe(v._decorate)
            elif isinstance(v, DisasmView):
                self.app.safe(v._decorate, v._pc)

    def get_state(self) -> dict:
        return {"annotate": self.annotate_var.get(), "hide": self.hide_var.get()}

    def set_state(self, state: dict) -> None:
        self.annotate_var.set(state.get("annotate", False))
        self.hide_var.set(state.get("hide", False))
        self.app.coverage_in_source = self.annotate_var.get()
