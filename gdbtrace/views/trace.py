"""Trace.List / couverture : trace d'instructions ETM (capture TPIU ou ETM simulé sous QEMU).

Onglets :
- Listing : les dernières instructions exécutées avant l'arrêt, avec fonction, désassemblage
  et ligne source ; exceptions et retours intercalés ;
- Appels : flot des appels/retours/exceptions, indenté par profondeur ;
La couverture de code a sa propre vue (views/coverage.py).
"""
from __future__ import annotations

import os
import tkinter as tk
from tkinter import filedialog, ttk

from ..trace.etm.listing import export_csv, find_rows, listing_rows
from ..gui.view_base import View
from .profile import _source_line

FLOW_ICONS = {"call": "→", "return": "←", "exception": "⚡", "exc-return": "↩", "start": "•",
              "branch": "↷", "gap": "┄"}


class TraceView(View):
    title = "Trace"
    kind = "trace"

    def __init__(self, master, app, **kw):
        super().__init__(master, app, **kw)
        bar = ttk.Frame(self)
        bar.pack(fill="x")
        ttk.Label(bar, text="Dernières :").pack(side="left")
        self.count_var = tk.StringVar(value="2000")
        e = ttk.Entry(bar, textvariable=self.count_var, width=7)
        e.pack(side="left", padx=2)
        e.bind("<Return>", lambda _e: self.render(force=True))
        ttk.Button(bar, text="Rafraîchir", command=lambda: self.render(force=True)).pack(side="left", padx=2)
        ttk.Button(bar, text="Effacer", command=self._clear).pack(side="left")
        self.info = ttk.Label(bar, text="")
        self.info.pack(side="left", padx=6)

        ttk.Button(bar, text="Exporter…", command=self._export).pack(side="right")
        find = ttk.Frame(self)
        find.pack(fill="x")
        ttk.Label(find, text="Chercher :").pack(side="left")
        self.find_var = tk.StringVar(value="")
        fe = ttk.Entry(find, textvariable=self.find_var, width=28)
        fe.pack(side="left", padx=2)
        fe.bind("<Return>", lambda _e: self.find_next(1))
        ttk.Button(find, text="◀", width=3, command=lambda: self.find_next(-1)).pack(side="left")
        ttk.Button(find, text="▶", width=3, command=lambda: self.find_next(1)).pack(side="left")
        ttk.Button(find, text="Tout trouver", command=self.find_all).pack(side="left", padx=2)
        self.find_info = ttk.Label(find, text="adresse, fonction, « exception SysTick », texte")
        self.find_info.pack(side="left", padx=6)

        self.tabs = ttk.Notebook(self)
        self.tabs.pack(fill="both", expand=True)
        f_list, f_flow = ttk.Frame(self.tabs), ttk.Frame(self.tabs)
        self.tabs.add(f_list, text="Listing")
        self.tabs.add(f_flow, text="Appels")
        self.listing = self._tree(f_list, [("addr", "Adresse", 80), ("func", "Fonction", 150),
                                           ("instr", "Instruction", 200), ("src", "Source", 300)],
                                  ("#", 70), stretch="src")
        self.listing.tag_configure("exc", foreground="#a05000")
        self.listing.tag_configure("func", foreground="#3050a0")
        self.listing.tag_configure("hit", background="#fff1a8")
        self.listing.bind("<Double-1>", lambda _e: self._goto(self.listing))
        self.flow = self._tree(f_flow, [("func", "Fonction", 320), ("addr", "Adresse", 90)],
                               ("#", 70), stretch="func")
        self.flow.tag_configure("exception", foreground="#a05000")
        self.flow.bind("<Double-1>", lambda _e: self._goto(self.flow))

        self._locs: dict[str, tuple[str, int]] = {}
        self.rows = []                    # lignes du listing (TraceRow), dans l'ordre affiché
        self._row_iids: list[str] = []
        self._hits: list[int] = []
        self._hit_pos = -1
        self._generation = -1
        app.trace.etm_listeners.append(self._on_new_trace)
        self.render(force=True)
        self.after(1000, self._tick)

    @staticmethod
    def _tree(master, cols, first, stretch):
        tv = ttk.Treeview(master, columns=[c[0] for c in cols], show="tree headings", selectmode="browse")
        tv.heading("#0", text=first[0])
        tv.column("#0", width=first[1], stretch=False, anchor="e")
        for cid, label, width in cols:
            tv.heading(cid, text=label)
            tv.column(cid, width=width, stretch=cid == stretch)
        sb = ttk.Scrollbar(master, orient="vertical", command=tv.yview)
        tv.configure(yscrollcommand=sb.set)
        tv.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        return tv

    def destroy(self) -> None:
        if self._on_new_trace in self.app.trace.etm_listeners:
            self.app.trace.etm_listeners.remove(self._on_new_trace)
        super().destroy()

    # ------------------------------------------------------------------ données
    @property
    def etm(self):
        return self.app.trace.etm

    def _on_new_trace(self) -> None:
        # trace qui change encore (marche, décodage en retard) : la liste changerait sans cesse ;
        # dessinée à la notification qui suit la fin du décodage, ou par refresh() à l'arrêt
        if not self.winfo_exists() or self.etm is None:
            return
        if self.trace_settling:
            self._update_info()
        else:
            self.render()

    def refresh(self) -> None:
        if self.etm is not None and self.trace_settling:
            self._update_info()
            return
        self.render()

    def _clear(self) -> None:
        if self.etm is not None:
            self.etm.reset()             # la trace ; la couverture a sa propre remise à zéro
        self.render(force=True)

    def _source_of(self, addr: int) -> tuple[str, int] | None:
        sym = self.app.symbols
        le = sym.line_at(addr) if sym else None
        f = sym.function_at(addr) if sym else None
        return (le.file, le.line) if le and f else None

    def _update_info(self) -> None:
        etm, tm = self.etm, self.app.trace
        st = etm.stats
        if not st["instructions"] or tm.etm_pending():
            # rien de décodé : d'où cela devrait venir, et pourquoi ; ou le retard du décodage
            self.info.configure(text=tm.trace_status())
            return
        self.info.configure(text=f"{tm.source_label()} — {st['instructions']} instructions décodées, "
                                 f"{st['octets']} octets, {st['erreurs']} erreurs")

    def _tick(self) -> None:
        """Tant que rien n'est décodé, l'état de la capture est suivi (connexion, octets)."""
        if not self.winfo_exists():
            return
        if self.etm is not None and not self.etm.stats["instructions"]:
            self._update_info()
        self.after(1000, self._tick)

    def render(self, force: bool = False) -> None:
        etm = self.etm
        if etm is None:
            self.info.configure(text="trace ETM indisponible (capstone requis, ou pas d'ELF)")
            return
        self._update_info()
        if not force and etm.generation == self._generation:
            return
        self._generation = etm.generation
        try:
            n = max(10, min(int(self.count_var.get()), 50_000))
        except ValueError:
            n = 2000
        self._locs.clear()
        self._render_listing(etm.last(n))
        self._render_flow(etm.call_flow(max(n, 20_000))[-n:])

    def _render_listing(self, events) -> None:
        tv = self.listing
        tv.delete(*tv.get_children())
        self.rows = listing_rows(events, self.etm.image, self.app.symbols)
        self._row_iids = []
        self._hits, self._hit_pos = [], -1
        prev_func = None
        for r in self.rows:
            if r.kind != "instruction":
                label = r.text if r.kind != "exception" else f"{r.text} ──"
                values = (f"{r.addr:08x}" if r.addr is not None else "", f"── {label}" + (
                    "" if r.kind == "exception" else " ──"), "", "")
                self._row_iids.append(tv.insert("", "end", text=str(r.index), tags=("exc",), values=values))
                if r.kind != "horodatage":
                    prev_func = None
                continue
            loc = (r.file, r.line) if r.file else None
            src = f"{os.path.basename(r.file)}:{r.line}  {_source_line(*loc)}" if loc else ""
            iid = tv.insert("", "end", text=str(r.index), tags=("func",) if r.func != prev_func else (), values=(
                f"{r.addr:08x}", r.func if r.func != prev_func else "", r.text, src))
            self._row_iids.append(iid)
            if loc:
                self._locs[iid] = loc
            prev_func = r.func
        kids = tv.get_children()
        if kids:
            tv.see(kids[-1])

    # ------------------------------------------------------------------ Trace.Find
    def _search(self) -> list[int]:
        query = self.find_var.get()
        hits = find_rows(self.rows, query)
        for i in self._hits:
            if i < len(self._row_iids):
                tags = [t for t in self.listing.item(self._row_iids[i], "tags") if t != "hit"]
                self.listing.item(self._row_iids[i], tags=tags)
        self._hits = hits
        return hits

    def find_next(self, step: int = 1) -> int | None:
        """Trace.Find : occurrence suivante (ou précédente) dans le listing ; rend sa position."""
        if not self._hits or self._hits != find_rows(self.rows, self.find_var.get()):
            self._search()
            self._hit_pos = -1 if step > 0 else len(self._hits)
        if not self._hits:
            self.find_info.configure(text="aucune occurrence" if self.find_var.get().strip() else "")
            return None
        self._hit_pos = (self._hit_pos + step) % len(self._hits)
        pos = self._hits[self._hit_pos]
        iid = self._row_iids[pos]
        self.tabs.select(0)
        self.listing.selection_set(iid)
        self.listing.focus(iid)
        self.listing.see(iid)
        self.find_info.configure(text=f"{self._hit_pos + 1} / {len(self._hits)} (index {self.rows[pos].index})")
        return pos

    def find_all(self) -> list[int]:
        """Trace.FindAll : toutes les occurrences surlignées ; la première choisie."""
        hits = self._search()
        for i in hits:
            iid = self._row_iids[i]
            self.listing.item(iid, tags=tuple(self.listing.item(iid, "tags")) + ("hit",))
        self._hit_pos = -1
        if hits:
            self.find_next(1)
        self.find_info.configure(text=f"{len(hits)} occurrence(s)" if hits else "aucune occurrence")
        return hits

    def _export(self) -> None:
        if not self.rows:
            self.app.status.set("rien à exporter : la trace est vide")
            return
        path = filedialog.asksaveasfilename(parent=self, title="Exporter la trace (CSV)",
                                            defaultextension=".csv", filetypes=[("CSV", "*.csv")])
        if path:
            n = export_csv(self.rows, path)
            self.app.status.set(f"{n} lignes de trace exportées dans {os.path.basename(path)}")

    def _render_flow(self, rows) -> None:
        tv = self.flow
        tv.delete(*tv.get_children())
        for r in rows:
            icon = FLOW_ICONS.get(r.kind, "")
            label = {"exc-return": "retour d'exception", "gap": "trace interrompue"}.get(r.kind, r.func)
            iid = tv.insert("", "end", text=str(r.index), values=(
                "    " * r.depth + f"{icon} {label}", f"{r.addr:08x}" if r.addr else ""),
                tags=("exception",) if r.kind.startswith("exc") else ())
            loc = self._source_of(r.addr) if r.addr else None
            if loc:
                self._locs[iid] = loc
        kids = tv.get_children()
        if kids:
            tv.see(kids[-1])

    # ------------------------------------------------------------------ navigation
    def _goto(self, tv) -> None:
        loc = self._locs.get(tv.focus())
        if loc:
            self.app.show_source(*loc)

    def get_state(self) -> dict:
        return {"count": self.count_var.get(), "tab": self.tabs.index("current")}

    def set_state(self, state: dict) -> None:
        self.count_var.set(state.get("count", "2000"))
        if "tab" in state and state["tab"] < len(self.tabs.tabs()):
            self.tabs.select(state["tab"])
