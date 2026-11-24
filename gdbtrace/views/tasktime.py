"""Temps des tâches (TASK.STATistic) : temps par tâche RTOS mesuré par l'ITM.

Le firmware écrit l'adresse du TCB entrant sur le port ITM 31 à chaque changement de tâche
(FreeRTOS : `traceTASK_SWITCHED_IN`, voir examples/freertos) ; avec l'horodatage ITM, chaque
tranche d'exécution est mesurée exactement (pas d'échantillonnage).

En haut, le tableau : activations, temps brut, net (sans les exceptions), part du temps,
tranches maximale et moyenne. En bas, le chronogramme (Gantt) des dernières tranches : une
rangée par tâche, temps en x. Couleur = identité, comme la vue Timing : les 7 tâches au plus
gros temps reçoivent les 7 teintes de la palette catégorielle, dans l'ordre, les autres le
gris « Autres » ; les noms sont toujours écrits (le tableau et l'info-bulle servent de relief).
"""
from __future__ import annotations

import bisect
import tkinter as tk
from tkinter import ttk

from ..gui.view_base import View
from .timing import GRID, INK, INK2, MUTED, OTHER, SLOTS, SURFACE, nice_step

ROW, TOP, LEFT, NAME_W = 20, 22, 6, 110
WINDOW = 2000                           # tranches dessinées (les plus récentes)


class TaskTimeView(View):
    title = "Temps des tâches"
    kind = "tasktime"

    def __init__(self, master, app, **kw):
        super().__init__(master, app, **kw)
        bar = ttk.Frame(self)
        bar.pack(fill="x")
        ttk.Label(bar, text="Unité :").pack(side="left", padx=(2, 0))
        self.unit_var = tk.StringVar(value="µs")
        cb = ttk.Combobox(bar, textvariable=self.unit_var, values=["µs", "cycles"], state="readonly", width=7)
        cb.pack(side="left", padx=2)
        cb.bind("<<ComboboxSelected>>", lambda _e: self.render(force=True))
        ttk.Button(bar, text="Effacer", command=self._clear).pack(side="left", padx=4)
        self.info = ttk.Label(bar, text="", foreground=INK2)
        self.info.pack(side="left", padx=8)
        pw = ttk.PanedWindow(self, orient="vertical")
        pw.pack(fill="both", expand=True)
        top, bottom = ttk.Frame(pw), ttk.Frame(pw)
        pw.add(top, weight=1)
        pw.add(bottom, weight=2)
        cols = [("act", "Activations", 80), ("gross", "Brut", 100), ("net", "Net", 100), ("pct", "%", 60),
                ("max", "Tranche max", 100), ("avg", "Moyenne", 90)]
        self.tree = ttk.Treeview(top, columns=[c[0] for c in cols], show="tree headings", selectmode="browse")
        self.tree.heading("#0", text="Tâche")
        self.tree.column("#0", width=140)
        for cid, label, width in cols:
            self.tree.heading(cid, text=label)
            self.tree.column(cid, width=width, anchor="e")
        self.tree.pack(fill="both", expand=True)
        self.canvas = tk.Canvas(bottom, background=SURFACE, highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<Configure>", lambda _e: self.render(force=True))
        self.canvas.bind("<Motion>", self._motion)
        self.canvas.bind("<Leave>", lambda _e: self.canvas.delete("hover"))
        self._gen = None
        self._bars: list[tuple[float, float, float, object]] = []   # (x0, x1, y, tranche)
        self._colors: dict[int, str] = {}
        self.after(500, self._tick)

    @property
    def sink(self):
        return self.app.trace.tasktrace

    # ------------------------------------------------------------------ noms des tâches
    def _ensure_namer(self) -> None:
        """Noms relus dans les TCB : disposition FreeRTOS (calculée cible arrêtée), lectures
        par gdb à l'arrêt, par le canal live en marche."""
        if self.sink.namer is not None or self.session is None:
            return
        layout = self.app.rtos_layout()
        if layout is None:
            return
        from ..trace.tasktrace import freertos_namer

        def read(addr: int, n: int) -> bytes:
            if self.session.state == "stopped":
                return self.session.read_memory(addr, n)
            if self.app.live is None:
                raise RuntimeError("pas de canal live")
            return self.app.live.read(addr, n)
        self.sink.namer = freertos_namer(layout, read)

    # ------------------------------------------------------------------ affichage
    def _tick(self) -> None:
        if not self.winfo_exists():
            return
        self.render()
        self.after(500, self._tick)

    def refresh(self) -> None:
        self.render(force=True)

    def on_program_changed(self) -> None:
        self.sink.namer = None
        self.sink.names.clear()
        self.render(force=True)

    def _clear(self) -> None:
        self.sink.reset()
        self.render(force=True)

    def _conv(self):
        if self.unit_var.get() == "cycles":
            return (lambda c: c), "cycles"
        hz = self.app.cpu_hz or 1
        return (lambda c: c * 1e6 / hz), "µs"

    def render(self, force: bool = False) -> None:
        sink = self.sink
        key = (sink.generation, self.unit_var.get(), self.canvas.winfo_width(), self.canvas.winfo_height())
        if not force and key == self._gen:
            return
        self._gen = key
        self._ensure_namer()
        rows = sink.rows()
        conv, unit = self._conv()
        if not rows:
            self.info.configure(text="aucun changement de tâche reçu : le firmware écrit le TCB entrant "
                                     "sur le port ITM 31 (traceTASK_SWITCHED_IN) ; capture SWO ou de trace")
        else:
            warn = "" if sink.timestamps else " — horodatage ITM absent : durées inconnues"
            self.info.configure(text=f"{sink.switches} changements de tâche sur {conv(sink.span):,.1f} {unit}, "
                                     f"{conv(sink.exception_cycles):,.1f} {unit} en exceptions{warn}".replace(",", " "))
        # couleur = identité : les 7 premières par temps brut, ordre fixe ; les autres « Autres »
        self._colors = {t.tcb: SLOTS[i] if i < len(SLOTS) else OTHER for i, t in enumerate(rows)}
        self.tree.delete(*self.tree.get_children())
        span = sink.span or 1
        fmt = (lambda c: f"{conv(c):,.1f}".replace(",", " ")) if unit == "µs" else (lambda c: f"{c:,}".replace(",", " "))
        for t in rows:
            self.tree.insert("", "end", text=t.name, values=(
                t.activations, fmt(t.gross), fmt(t.net), f"{100 * t.gross / span:.1f}",
                fmt(t.max_slice), fmt(t.avg_slice)))
        self._draw(rows)

    def _draw(self, rows) -> None:
        c = self.canvas
        c.delete("all")
        self._bars = []
        slices = self.sink.timeline(WINDOW)
        if not slices or not self.sink.timestamps:
            return
        w = max(c.winfo_width(), 300)
        t0, t1 = slices[0].start, max(s.end for s in slices)
        if t1 <= t0:
            return
        x0, x1 = LEFT + NAME_W, w - LEFT
        X = lambda t: x0 + (t - t0) / (t1 - t0) * (x1 - x0)  # noqa: E731
        lane = {t.tcb: i for i, t in enumerate(rows)}
        conv, unit = self._conv()
        h = TOP + len(rows) * ROW
        step = nice_step(conv(t1 - t0))
        k = 0
        while k * step <= conv(t1 - t0) + 1e-12:               # axe récessif
            x = x0 + k * step / conv(t1 - t0) * (x1 - x0)
            c.create_line(x, TOP - 4, x, h, fill=GRID)
            if x < x1 - 30:
                c.create_text(x + 2, TOP - 6, anchor="sw", text=f"{k * step:,.10g}".replace(",", " "), fill=MUTED)
            k += 1
        c.create_text(x1, TOP - 6, anchor="se", text=unit, fill=MUTED)
        for t in rows:
            y = TOP + lane[t.tcb] * ROW
            c.create_text(LEFT, y + ROW / 2, anchor="w", text=t.name, fill=INK)
        for sl in slices:
            i = lane.get(sl.tcb)
            if i is None:
                continue
            y = TOP + i * ROW
            xa, xb = X(sl.start), max(X(sl.end), X(sl.start) + 1)
            c.create_rectangle(xa, y + 3, xb, y + ROW - 3, fill=self._colors.get(sl.tcb, OTHER), width=0)
            self._bars.append((xa, xb, y, sl))
        self._bars.sort(key=lambda b: b[0])

    def _motion(self, ev) -> None:
        c = self.canvas
        c.delete("hover")
        if not self._bars:
            return
        starts = [b[0] for b in self._bars]
        k = bisect.bisect_right(starts, ev.x) - 1
        hit = None
        for b in self._bars[max(0, k - 50):k + 1]:              # quelques tranches autour
            if b[0] <= ev.x <= b[1] + 1 and b[2] <= ev.y <= b[2] + ROW:
                hit = b
        if hit is None:
            return
        sl = hit[3]
        conv, unit = self._conv()
        text = (f"{self.sink.name(sl.tcb)}\n{conv(sl.end - sl.start):,.2f} {unit}"
                + (f" dont {conv(sl.isr):,.2f} en exceptions" if sl.isr else "")).replace(",", " ")
        tid = c.create_text(ev.x + 12, ev.y + 12, anchor="nw", text=text, fill=INK, tags=("hover",))
        bx = c.bbox(tid)
        if bx:
            bg = c.create_rectangle(bx[0] - 6, bx[1] - 4, bx[2] + 6, bx[3] + 4, fill="#ffffff",
                                    outline=GRID, tags=("hover",))
            c.tag_lower(bg, tid)

    def get_state(self) -> dict:
        return {"unit": self.unit_var.get()}

    def set_state(self, state: dict) -> None:
        self.unit_var.set(state.get("unit", "µs"))
