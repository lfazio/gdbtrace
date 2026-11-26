"""Var.TRACE / Var.DRAW : variables suivies cible en marche, par la trace de données du DWT
(chaque accès, par l'ITM) ou échantillonnées par le canal mémoire (SNOOPer, sans trace :
Cortex-M0/M0+, QEMU, carte sans capture SWO).

En haut : la variable à tracer et l'accès (écriture, lecture, les deux). À gauche : les
variables tracées, leur nombre d'échantillons et leur dernière valeur (vue tableau, au format
de la variable). À droite : un petit graphe par variable (petits multiples, chacun sa propre
échelle : jamais deux grandeurs sur un même axe), temps en x, valeur en paliers ; survol :
réticule et valeurs à cet instant.
"""
from __future__ import annotations

import bisect
import tkinter as tk
from tkinter import ttk

from ..gui.view_base import View
from .timing import GRID, INK, INK2, MUTED, SLOTS, SURFACE, nice_step

LANE, GAP, PAD_L, PAD_R, AXIS = 64, 10, 8, 84, 20     # PAD_R : bornes min/max et unité
DWT, SAMPLED = "DWT (trace)", "échantillonné"
ACCESS_NAMES = {"write": "écriture", "read": "lecture", "rw": "les deux", "sample": "échantillonné"}
WINDOW = 5000                           # échantillons dessinés (les plus récents)


class VarTraceView(View):
    title = "Variables tracées"
    kind = "vartrace"

    def __init__(self, master, app, **kw):
        super().__init__(master, app, **kw)
        bar = ttk.Frame(self)
        bar.pack(fill="x")
        self.expr_var = tk.StringVar()
        e = ttk.Entry(bar, textvariable=self.expr_var, width=24)
        e.pack(side="left", padx=2, pady=1)
        e.bind("<Return>", lambda _e: self.trace_expr())
        self.access_var = tk.StringVar(value="écriture")
        ttk.Combobox(bar, textvariable=self.access_var, values=["écriture", "lecture", "les deux"],
                     state="readonly", width=9).pack(side="left", padx=2)
        self.pc_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(bar, text="PC", variable=self.pc_var).pack(side="left", padx=2)
        self.source_var = tk.StringVar(value=DWT)
        src = ttk.Combobox(bar, textvariable=self.source_var, values=[DWT, SAMPLED], state="readonly", width=14)
        src.pack(side="left", padx=2)
        src.bind("<<ComboboxSelected>>", lambda _e: setattr(self, "_source_chosen", True))
        self._source_chosen = False
        ttk.Label(bar, text="toutes les").pack(side="left")
        self.period_var = tk.StringVar(value="10")
        ttk.Entry(bar, textvariable=self.period_var, width=5).pack(side="left", padx=2)
        ttk.Label(bar, text="ms").pack(side="left")
        ttk.Button(bar, text="Tracer", command=self.trace_expr).pack(side="left", padx=2)
        ttk.Button(bar, text="Retirer", command=self._remove).pack(side="left")
        ttk.Button(bar, text="Effacer", command=self._clear).pack(side="left", padx=2)
        self.info = ttk.Label(bar, text="", foreground=INK2)
        self.info.pack(side="left", padx=8)

        pw = ttk.PanedWindow(self, orient="horizontal")
        pw.pack(fill="both", expand=True)
        left, right = ttk.Frame(pw), ttk.Frame(pw)
        pw.add(left, weight=1)
        pw.add(right, weight=3)
        cols = [("access", "Accès", 70), ("n", "Échant.", 70), ("last", "Dernière valeur", 140)]
        self.tree = ttk.Treeview(left, columns=[c[0] for c in cols], show="tree headings", selectmode="browse")
        self.tree.heading("#0", text="Variable")
        self.tree.column("#0", width=140)
        for cid, label, width in cols:
            self.tree.heading(cid, text=label)
            self.tree.column(cid, width=width, anchor="w" if cid == "last" else "e")
        self.tree.pack(fill="both", expand=True)
        self.canvas = tk.Canvas(right, background=SURFACE, highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<Configure>", lambda _e: self.render(force=True))
        self.canvas.bind("<Motion>", self._motion)
        self.canvas.bind("<Leave>", lambda _e: self.canvas.delete("hover"))
        self.wanted: list[tuple] = []     # (expr, accès, PC, source, période) : layout, reprise
        self._gen = None
        self._lanes: list[tuple] = []                     # (variable, y0, y1, points) pour le survol
        self._span = (0.0, 1.0)
        self.redraw_when_shown(self.render)
        self.after(500, self._tick)

    # ------------------------------------------------------------------ actions
    @property
    def sink(self):
        return self.app.trace.datatrace

    def trace_expr(self, expr: str | None = None, access: str | None = None, pc: bool | None = None) -> None:
        expr = (expr if expr is not None else self.expr_var.get()).strip()
        if not expr:
            return
        access = access or {"écriture": "write", "lecture": "read", "les deux": "rw"}[self.access_var.get()]
        pc = self.pc_var.get() if pc is None else pc
        sampled = self.source_var.get() == SAMPLED
        try:
            period = max(1, int(self.period_var.get()))
        except ValueError:
            period = 10
        if not self.stopped:
            self.app.status.set("Arrêter la cible pour ajouter une variable (Break)")
            return
        if sampled:
            log = self.app.safe(lambda: self.app.trace.snoop(self.session, expr, period))
        else:
            log = self.app.safe(lambda: self.app.trace.trace_variable(self.session, expr, access, pc,
                                                                      cpu_hz=self.app.cpu_hz))
        if log:
            self.app.write_console("\n".join(log) + "\n", "gdb")
            self.wanted = [w for w in self.wanted if w[0] != expr] + [
                (expr, access, pc, "sample" if sampled else "dwt", period)]
            self.expr_var.set("")
            self.render(force=True)

    def _remove(self) -> None:
        item = self.tree.focus()
        if not item:
            return
        expr = self.tree.item(item, "text")
        var = next((v for v in self.sink.vars.values() if v.expr == expr), None)
        if var is not None and var.comparator < 0:
            self.app.safe(self.app.trace.unsnoop, expr)
        else:
            self.app.safe(self.app.trace.untrace_variable, self.session, expr)
        self.wanted = [w for w in self.wanted if w[0] != expr]
        self.render(force=True)

    def _clear(self) -> None:
        self.sink.reset()
        self.render(force=True)

    # ------------------------------------------------------------------ affichage
    def _tick(self) -> None:
        if not self.winfo_exists():
            return
        if self.shown:
            self.render()
        self.after(500, self._tick)

    def refresh(self) -> None:
        """Cible arrêtée : variables du layout pas encore tracées (reconnexion) reprises."""
        tm = self.app.trace
        traced = {v.expr for v in self.sink.vars.values()}
        no_itm = tm.no_trace_reason() is not None    # dit une fois, dans info
        if not self._source_chosen:                  # sans ITM ni capture SWO : échantillonné
            self.source_var.set(SAMPLED if no_itm or tm.source is None else DWT)
        for w in self.wanted:
            expr, access, pc = w[0], w[1], w[2]
            source, period = (w[3], w[4]) if len(w) >= 5 else ("dwt", 10)
            if expr in traced:
                continue
            if source == "sample":
                if tm.live is not None:
                    self.app.safe(lambda e=expr, p=period: tm.snoop(self.session, e, p))
            elif not no_itm:
                self.app.safe(lambda e=expr, a=access, p=pc: tm.trace_variable(
                    self.session, e, a, p, cpu_hz=self.app.cpu_hz))
        self.render(force=True)

    def render(self, force: bool = False) -> None:
        sink = self.sink
        reason = self.app.trace.no_trace_reason()      # cœur sans ITM (Cortex-M0/M0+)
        key = (sink.generation, reason, self.canvas.winfo_width(), self.canvas.winfo_height())
        if not force and key == self._gen:
            return
        self._gen = key
        vars_ = sorted(sink.vars.values(), key=lambda v: v.comparator, reverse=True)
        total = sum(v.count for v in vars_)
        self.info.configure(text=f"{len(vars_)} variable(s), {total} échantillon(s)"
                                 + (f", {sink.lost} paquet(s) d'un comparateur inconnu" if sink.lost else "")
                                 if vars_ else f"pas de trace de données — {reason} ; « {SAMPLED} » lit "
                                 "la variable par le canal mémoire" if reason
                                 else "aucune variable tracée : saisir une globale, choisir la source, puis Tracer")
        self.tree.delete(*self.tree.get_children())
        for v in vars_:
            last = v.text(v.samples[-1]) if v.samples else ""
            self.tree.insert("", "end", text=v.expr, values=(ACCESS_NAMES.get(v.access, v.access), v.count, last))
        self._draw(vars_)

    def _draw(self, vars_) -> None:
        c = self.canvas
        c.delete("all")
        w = max(c.winfo_width(), 200)
        self._lanes = []
        series = []
        for v in vars_:
            pts = []                                           # (t, valeur, échantillon)
            for smp in list(v.samples)[-WINDOW:]:
                val = v.number(smp)
                if val is not None:
                    pts.append((smp.t, val, smp))
            series.append((v, pts))
        times = [p[0] for _v, pts in series for p in pts]
        if not times:
            c.create_text(w / 2, 40, text="Les valeurs s'affichent ici pendant que la cible tourne",
                          fill=MUTED)
            return
        t0, t1 = min(times), max(times)
        if t1 <= t0:
            t1 = t0 + 1e-3
        self._span = (t0, t1)
        x0, x1 = PAD_L, w - PAD_R
        X = lambda t: x0 + (t - t0) / (t1 - t0) * (x1 - x0)  # noqa: E731
        # axe du temps (récessif), partagé par tous les graphes
        unit, scale = ("ms", 1e3) if (t1 - t0) < 2 else ("s", 1.0)
        step = nice_step((t1 - t0) * scale)
        k = int(t0 * scale / step)
        while k * step <= t1 * scale + 1e-12:
            t = k * step / scale
            if t >= t0:
                x = X(t)
                c.create_line(x, AXIS, x, AXIS + len(series) * (LANE + GAP), fill=GRID)
                if x < x1 - 24:                      # pas de libellé collé au bord droit
                    c.create_text(x + 2, AXIS - 4, anchor="sw", text=f"{k * step:g}", fill=MUTED)
            k += 1
        c.create_text(x1 + 6, AXIS - 4, anchor="sw", text=unit, fill=MUTED)
        for i, (v, pts) in enumerate(series):
            y0 = AXIS + i * (LANE + GAP) + 16
            y1 = y0 + LANE - 22
            color = SLOTS[i % len(SLOTS)]
            c.create_text(x0, y0 - 4, anchor="sw", text=v.expr, fill=INK, font=("TkDefaultFont", 9, "bold"))
            if not pts:
                c.create_text(x0, (y0 + y1) / 2, anchor="w", text="pas encore de valeur", fill=MUTED)
                continue
            lo, hi = min(p[1] for p in pts), max(p[1] for p in pts)
            span = (hi - lo) or 1.0
            Y = lambda val, lo=lo, span=span, y0=y0, y1=y1: y1 - (val - lo) / span * (y1 - y0)  # noqa: E731
            coords = []
            for j, (t, val, _smp) in enumerate(pts):              # paliers : la valeur tient
                x, y = X(t), Y(val)
                if j:
                    coords += [x, coords[-1]]
                coords += [x, y]
            coords += [x1, coords[-1]]
            if len(coords) >= 4:
                c.create_line(*coords, fill=color, width=2)
            c.create_line(x0, y1 + 3, x1, y1 + 3, fill=GRID)
            c.create_text(x1 + 6, y0, anchor="nw", text=_short(hi), fill=MUTED)
            c.create_text(x1 + 6, y1, anchor="sw", text=_short(lo), fill=MUTED)
            self._lanes.append((v, y0, y1, pts, [p[0] for p in pts]))

    def _motion(self, ev) -> None:
        c = self.canvas
        c.delete("hover")
        if not self._lanes:
            return
        w = max(c.winfo_width(), 200)
        x0, x1 = PAD_L, w - PAD_R
        if not x0 <= ev.x <= x1:
            return
        t0, t1 = self._span
        t = t0 + (ev.x - x0) / (x1 - x0) * (t1 - t0)
        lines = [f"t = {t * 1e3:,.3f} ms".replace(",", " ")]
        for v, _y0, _y1, pts, ts in self._lanes:
            k = bisect.bisect_right(ts, t) - 1
            if k >= 0:
                lines.append(f"{v.expr} = {v.text(pts[k][2])}")
        c.create_line(ev.x, AXIS, ev.x, c.winfo_height(), fill=INK2, tags=("hover",), dash=(2, 2))
        tx = ev.x + 10 if ev.x < w / 2 else ev.x - 10
        tid = c.create_text(tx, AXIS + 4, anchor="nw" if ev.x < w / 2 else "ne", text="\n".join(lines),
                            fill=INK, tags=("hover",))
        bx = c.bbox(tid)
        if bx:
            bg = c.create_rectangle(bx[0] - 6, bx[1] - 4, bx[2] + 6, bx[3] + 4, fill="#ffffff",
                                    outline=GRID, tags=("hover",))
            c.tag_lower(bg, tid)

    # ------------------------------------------------------------------ état
    def on_program_changed(self) -> None:
        self.render(force=True)

    def get_state(self) -> dict:
        return {"vars": [list(w) for w in self.wanted]}

    def set_state(self, state: dict) -> None:
        self.wanted = [tuple(w) for w in state.get("vars", [])]


def _short(x: float) -> str:
    """Borne d'un graphe, courte : 4 chiffres significatifs, séparateur de milliers."""
    if x == int(x) and abs(x) < 1e7:
        return f"{int(x):,}".replace(",", " ")
    return f"{x:.4g}"
