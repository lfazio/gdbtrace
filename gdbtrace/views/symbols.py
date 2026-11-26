"""sYmbol.Browse : fonctions et variables globales de l'ELF (et des ELF ajoutés), filtrées par
nom ; adresse, taille, fichier et ligne. Double clic : la source d'une fonction, la mémoire
d'une variable ; menu : Watch, désassemblage, mémoire, type (`ptype`, cible arrêtée)."""
from __future__ import annotations

import os
import tkinter as tk
from tkinter import ttk

from ..core.symbols import symbol_rows
from ..gui.view_base import View

MAX_ROWS = 2000          # au-delà, le filtre doit resserrer (un appel Tk par ligne)


class SymbolsView(View):
    title = "Symboles"
    kind = "symbols"

    def __init__(self, master, app, **kw):
        super().__init__(master, app, **kw)
        bar = ttk.Frame(self)
        bar.pack(fill="x")
        ttk.Label(bar, text="Filtre :").pack(side="left")
        self.filter_var = tk.StringVar(value="")
        e = ttk.Entry(bar, textvariable=self.filter_var, width=24)
        e.pack(side="left", padx=2)
        e.bind("<KeyRelease>", lambda _e: self._schedule())
        self.kind_var = tk.StringVar(value="tout")
        cb = ttk.Combobox(bar, textvariable=self.kind_var, values=("tout", "fonctions", "variables"),
                          width=10, state="readonly")
        cb.pack(side="left", padx=4)
        cb.bind("<<ComboboxSelected>>", lambda _e: self.render())
        self.info = ttk.Label(bar, text="")
        self.info.pack(side="left", padx=6)
        self.tree = ttk.Treeview(self, columns=("kind", "addr", "size", "where"), show="tree headings",
                                 selectmode="browse")
        for col, text, width, stretch in (("#0", "Nom", 200, True), ("kind", "Genre", 70, False),
                                          ("addr", "Adresse", 90, False), ("size", "Taille", 60, False),
                                          ("where", "Fichier:ligne", 220, True)):
            self.tree.heading(col, text=text)
            self.tree.column(col, width=width, stretch=stretch)
        sb = ttk.Scrollbar(self, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.tree.bind("<Double-1>", lambda _e: self._open())
        self.menu = tk.Menu(self, tearoff=False)
        self.menu.add_command(label="Source / mémoire", command=self._open)
        self.menu.add_command(label="Ajouter au Watch", command=self._watch)
        self.menu.add_command(label="Désassembler", command=self._disasm)
        self.menu.add_command(label="Afficher en mémoire", command=self._memory)
        self.menu.add_command(label="Type (ptype)", command=self._ptype)
        self.tree.bind("<Button-3>", self._popup)
        self.rows: dict[str, dict] = {}
        self._pending: str | None = None
        self.redraw_when_shown(self.render)

    # ------------------------------------------------------------------ données
    def on_program_changed(self) -> None:
        self.render()

    def refresh(self) -> None:
        if not self.tree.get_children() and self.shown:
            self.render()

    def _schedule(self) -> None:
        """Filtre saisi : un seul rendu après la dernière touche."""
        if self._pending is not None:
            self.after_cancel(self._pending)
        self._pending = self.after(200, self._run_pending)

    def _run_pending(self) -> None:
        self._pending = None
        self.render()

    def render(self) -> None:
        rows = symbol_rows(self.app.symbols, self.filter_var.get(), self.kind_var.get())
        self.tree.delete(*self.tree.get_children())
        self.rows = {}
        for r in rows[:MAX_ROWS]:
            where = f"{os.path.basename(r['fichier'])}:{r['ligne']}" if r["fichier"] else ""
            iid = self.tree.insert("", "end", text=r["nom"], values=(
                r["genre"], f"{r['adresse']:#010x}", r["taille"] or "", where))
            self.rows[iid] = r
        more = f" (les {MAX_ROWS} premiers : resserrer le filtre)" if len(rows) > MAX_ROWS else ""
        self.info.configure(text=f"{len(rows)} symbole(s){more}" if self.app.symbols else "pas de symboles")

    def get_state(self) -> dict:
        return {"filter": self.filter_var.get(), "kind": self.kind_var.get()}

    def set_state(self, state: dict) -> None:
        self.filter_var.set(state.get("filter", ""))
        self.kind_var.set(state.get("kind", "tout"))

    # ------------------------------------------------------------------ actions
    def _selected(self) -> dict | None:
        sel = self.tree.selection()
        return self.rows.get(sel[0]) if sel else None

    def _popup(self, event) -> None:
        iid = self.tree.identify_row(event.y)
        if iid:
            self.tree.selection_set(iid)
            self.menu.tk_popup(event.x_root, event.y_root)

    def _open(self) -> None:
        r = self._selected()
        if r is None:
            return
        if r["genre"] == "fonction" and r["fichier"]:
            self.app.show_source(r["fichier"], r["ligne"])
        elif r["genre"] == "fonction":
            self._disasm()
        else:
            self._memory()

    def _watch(self) -> None:
        r = self._selected()
        if r is not None:
            self.app.add_watch(r["nom"])

    def _memory(self) -> None:
        r = self._selected()
        if r is not None:
            self.app.show_memory(r["adresse"])

    def _disasm(self) -> None:
        from .code import DisasmView
        r = self._selected()
        if r is None or not self.stopped:
            return
        v = self.app.views.show(DisasmView, "code")
        v.addr_var.set(f"{r['adresse']:#x}")
        self.app.safe(v._goto)

    def _ptype(self) -> None:
        r = self._selected()
        if r is None:
            return
        if not self.stopped:
            self.app.status.set("ptype : arrêter la cible d'abord")
            return
        text = self.app.safe(self.session.ptype, r["nom"])
        if text:
            self.app.write_console(f"{r['nom']} : {text}\n", "gdb")
