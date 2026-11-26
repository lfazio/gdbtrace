"""BMC : compteurs de performance du cœur (DWT ou PMU) pendant la dernière exécution, et leur
cumul. Mesure dans `core/counters.py`."""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk

from ..gui.view_base import View


def _n(v: int | None) -> str:
    return "" if v is None else f"{v:,}".replace(",", " ")


class CountersView(View):
    title = "Compteurs"
    kind = "counters"

    def __init__(self, master, app, **kw):
        super().__init__(master, app, **kw)
        bar = ttk.Frame(self)
        bar.pack(fill="x")
        self.on_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(bar, text="Mesurer (compteurs du DWT ou de la PMU)", variable=self.on_var,
                        command=self._toggle).pack(side="left", padx=2)
        ttk.Button(bar, text="Remettre le cumul à zéro", command=self._reset).pack(side="left", padx=6)
        self.info = ttk.Label(bar, text="désarmés : cocher « Mesurer », cible arrêtée")
        self.info.pack(side="left", padx=6)
        cols = [("last", "Dernière exécution", 130), ("total", "Cumul", 130), ("label", "Sens", 320)]
        self.tree = ttk.Treeview(self, columns=[c[0] for c in cols], show="tree headings", selectmode="browse")
        self.tree.heading("#0", text="Compteur")
        self.tree.column("#0", width=140, stretch=False)
        for cid, label, width in cols:
            self.tree.heading(cid, text=label)
            self.tree.column(cid, width=width, stretch=cid == "label", anchor="w" if cid == "label" else "e")
        self.tree.tag_configure("approx", foreground="#9a5b00")
        self.tree.pack(fill="both", expand=True)

    @property
    def counters(self):
        return self.app.api._counters if self.app.api is not None else None

    def _toggle(self) -> None:
        c = self.counters
        if c is None:
            self.on_var.set(False)
            return
        if not self.stopped:
            self.on_var.set(c.enabled)
            self.app.status.set("compteurs : arrêter la cible d'abord")
            return
        msg = self.app.safe(c.enable, self.on_var.get())
        if msg:
            self.app.write_console(msg + "\n", "gdb")
        self.on_var.set(c.enabled)
        self.render()

    def _reset(self) -> None:
        if self.counters is not None:
            self.counters.reset()
        self.render()

    def refresh(self) -> None:
        c = self.counters
        if c is not None and c.enabled and self.stopped:
            self.app.safe(c.stop)
        self.render()

    def on_disconnected(self) -> None:
        self.on_var.set(False)
        self.tree.delete(*self.tree.get_children())
        self.info.configure(text="pas de session")

    def render(self) -> None:
        c = self.counters
        self.tree.delete(*self.tree.get_children())
        if c is None:
            return
        self.on_var.set(c.enabled)
        if not c.enabled:
            self.info.configure(text=c.reason or "désarmés : cocher « Mesurer », cible arrêtée")
            return
        m = c.last
        if m is None:
            self.info.configure(text=f"armés ({c.kind}) : la mesure vient à l'exécution suivante"
                                + (f" — {c.reason}" if c.reason else ""))
            return
        self.info.configure(text=f"{m.source}, {c.runs} exécution(s) mesurée(s)" + (f" — {m.note}" if m.note else ""))
        for v in m.values:
            approx = "" if v.exact else " ≈"
            self.tree.insert("", "end", text=v.name, values=(_n(v.delta) + approx, _n(c.totals.get(v.name)),
                                                             v.label + (f" ({v.note})" if v.note else "")),
                             tags=() if v.exact else ("approx",))
        if m.instructions is not None and m.source == "DWT":
            self.tree.insert("", "end", text="Instructions", values=(
                _n(m.instructions), _n(c.totals.get("INSTR")), "déduites : CYC − CPI − EXC − SLEEP − LSU + FOLD"))
        if m.ipc is not None:
            self.tree.insert("", "end", text="IPC", values=(f"{m.ipc:.2f}", "", "instructions par cycle"))

    def get_state(self) -> dict:
        return {}
