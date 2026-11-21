"""Trace.List / couverture : trace d'instructions ETM (capture TPIU ou ETM simulé sous QEMU).

Onglets :
- Listing : les dernières instructions exécutées avant l'arrêt, avec fonction, désassemblage
  et ligne source ; exceptions et retours intercalés ;
- Appels : flot des appels/retours/exceptions, indenté par profondeur ;
- Couverture : instructions exécutées par fonction ; option d'annotation de la vue Source.
"""
from __future__ import annotations

import os
import tkinter as tk
from tkinter import ttk

from ..trace.etm.qemu_log import ExcEntry, ExcReturn, Step
from ..trace.itm import exception_name
from ..gui.view_base import View
from .profile import _bar, _source_line

FLOW_ICONS = {"call": "→", "return": "←", "exception": "⚡", "exc-return": "↩", "start": "•",
              "branch": "↷"}


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
        self.cov_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(bar, text="Couverture dans la source", variable=self.cov_var,
                        command=self._toggle_coverage).pack(side="left", padx=6)
        self.info = ttk.Label(bar, text="")
        self.info.pack(side="left", padx=6)

        self.tabs = ttk.Notebook(self)
        self.tabs.pack(fill="both", expand=True)
        f_list, f_flow, f_cov = ttk.Frame(self.tabs), ttk.Frame(self.tabs), ttk.Frame(self.tabs)
        self.tabs.add(f_list, text="Listing")
        self.tabs.add(f_flow, text="Appels")
        self.tabs.add(f_cov, text="Couverture")
        self.listing = self._tree(f_list, [("addr", "Adresse", 80), ("func", "Fonction", 150),
                                           ("instr", "Instruction", 200), ("src", "Source", 300)],
                                  ("#", 70), stretch="src")
        self.listing.tag_configure("exc", foreground="#a05000")
        self.listing.tag_configure("func", foreground="#3050a0")
        self.listing.bind("<Double-1>", lambda _e: self._goto(self.listing))
        self.flow = self._tree(f_flow, [("func", "Fonction", 320), ("addr", "Adresse", 90)],
                               ("#", 70), stretch="func")
        self.flow.tag_configure("exception", foreground="#a05000")
        self.flow.bind("<Double-1>", lambda _e: self._goto(self.flow))
        self.cov = self._tree(f_cov, [("n", "Exécutées", 80), ("pct", "%", 60), ("bar", "", 260)],
                              ("Fonction", 260), stretch="bar")
        self.cov.tag_configure("zero", foreground="#b00000")
        self.cov.bind("<Double-1>", lambda _e: self._goto_func())

        self._locs: dict[str, tuple[str, int]] = {}
        self._generation = -1
        app.trace.etm_listeners.append(self._on_new_trace)
        self.render(force=True)

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
        if self.winfo_exists():
            self.render()
            if self.cov_var.get():
                self._refresh_source()

    def refresh(self) -> None:
        self.render()

    def _clear(self) -> None:
        if self.etm is not None:
            self.etm.reset()
        self.render(force=True)
        self._refresh_source()

    def _source_of(self, addr: int) -> tuple[str, int] | None:
        sym = self.app.symbols
        le = sym.line_at(addr) if sym else None
        f = sym.function_at(addr) if sym else None
        return (le.file, le.line) if le and f else None

    def render(self, force: bool = False) -> None:
        etm = self.etm
        if etm is None:
            self.info.configure(text="trace ETM indisponible (capstone requis, ou pas d'ELF)")
            return
        if not force and etm.generation == self._generation:
            return
        self._generation = etm.generation
        src = ("ETM simulé (QEMU)" if self.app.trace.etm_sim is not None
               else "capture TPIU" if self.app.trace.active else "aucune source")
        st = etm.stats
        self.info.configure(text=f"{src} — {st['instructions']} instructions décodées, "
                                 f"{st['octets']} octets, {st['erreurs']} erreurs")
        try:
            n = max(10, min(int(self.count_var.get()), 50_000))
        except ValueError:
            n = 2000
        self._locs.clear()
        self._render_listing(etm.last(n))
        self._render_flow(etm.call_flow(max(n, 20_000))[-n:])
        self._render_coverage(etm.coverage())

    def _render_listing(self, events) -> None:
        tv = self.listing
        tv.delete(*tv.get_children())
        sym = self.app.symbols
        total = len(events)
        prev_func = None
        for i, e in enumerate(events):
            idx = str(i - total)
            if isinstance(e, ExcEntry):
                tv.insert("", "end", text=idx, tags=("exc",), values=(
                    f"{e.handler:08x}", f"── exception {exception_name(e.number)} ({e.number}) ──", "", ""))
                prev_func = None
                continue
            if isinstance(e, ExcReturn):
                tv.insert("", "end", text=idx, tags=("exc",), values=("", "── retour d'exception ──", "", ""))
                prev_func = None
                continue
            ins = self.etm.image.instr(e.addr)
            f = sym.function_at(e.addr) if sym else None
            name = f.name if f else ""
            loc = self._source_of(e.addr)
            src = f"{os.path.basename(loc[0])}:{loc[1]}  {_source_line(*loc)}" if loc else ""
            iid = tv.insert("", "end", text=idx, tags=("func",) if name != prev_func else (), values=(
                f"{e.addr:08x}", name if name != prev_func else "", ins.text if ins else "?", src))
            if loc:
                self._locs[iid] = loc
            prev_func = name
        kids = tv.get_children()
        if kids:
            tv.see(kids[-1])

    def _render_flow(self, rows) -> None:
        tv = self.flow
        tv.delete(*tv.get_children())
        for r in rows:
            icon = FLOW_ICONS.get(r.kind, "")
            label = "retour d'exception" if r.kind == "exc-return" else r.func
            iid = tv.insert("", "end", text=str(r.index), values=(
                "    " * r.depth + f"{icon} {label}", f"{r.addr:08x}" if r.addr else ""),
                tags=("exception",) if r.kind.startswith("exc") else ())
            loc = self._source_of(r.addr) if r.addr else None
            if loc:
                self._locs[iid] = loc
        kids = tv.get_children()
        if kids:
            tv.see(kids[-1])

    def _render_coverage(self, rows) -> None:
        tv = self.cov
        tv.delete(*tv.get_children())
        for c in rows:
            tv.insert("", "end", iid=c.name, text=c.name, tags=("zero",) if not c.executed else (),
                      values=(f"{c.executed}/{c.total}", f"{c.percent:.0f}", _bar(c.percent)))

    # ------------------------------------------------------------------ navigation
    def _goto(self, tv) -> None:
        loc = self._locs.get(tv.focus())
        if loc:
            self.app.show_source(*loc)

    def _goto_func(self) -> None:
        name = self.cov.focus().split(" = ")[0]
        f = self.app.symbols.function(name) if self.app.symbols and name else None
        loc = self._source_of(f.start) if f else None
        if loc:
            self.app.show_source(*loc)

    # ------------------------------------------------------------------ couverture dans la source
    def _toggle_coverage(self) -> None:
        self.app.coverage_in_source = self.cov_var.get()
        self._refresh_source()

    def _refresh_source(self) -> None:
        from .code import SourceView
        for v in self.app.views.views:
            if isinstance(v, SourceView):
                self.app.safe(v._decorate)

    def get_state(self) -> dict:
        return {"count": self.count_var.get(), "coverage": self.cov_var.get(),
                "tab": self.tabs.index("current")}

    def set_state(self, state: dict) -> None:
        self.count_var.set(state.get("count", "2000"))
        self.cov_var.set(state.get("coverage", False))
        self.app.coverage_in_source = self.cov_var.get()
        if "tab" in state:
            self.tabs.select(state["tab"])
