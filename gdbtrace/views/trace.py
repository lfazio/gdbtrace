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
from tkinter import ttk

from ..trace.etm.qemu_log import ExcEntry, ExcReturn, Step, TraceGap
from ..trace.itm import exception_name
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
        self.listing.bind("<Double-1>", lambda _e: self._goto(self.listing))
        self.flow = self._tree(f_flow, [("func", "Fonction", 320), ("addr", "Adresse", 90)],
                               ("#", 70), stretch="func")
        self.flow.tag_configure("exception", foreground="#a05000")
        self.flow.bind("<Double-1>", lambda _e: self._goto(self.flow))

        self._locs: dict[str, tuple[str, int]] = {}
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
            if isinstance(e, TraceGap):
                tv.insert("", "end", text=idx, tags=("exc",),
                          values=("", "── trace interrompue (filtre) ──", "", ""))
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
            label = {"exc-return": "retour d'exception", "gap": "trace interrompue (filtre)"}.get(r.kind, r.func)
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
