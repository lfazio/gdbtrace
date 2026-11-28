"""Trace.STATistic.Func / Trace.Chart.Func : durées par fonction et chronogramme d'appels ;
Trace.STATistic.DISTribution : distribution des durées d'une fonction (onglet Distribution).

Calculé depuis la trace ETM : cycles **mesurés** (ETM cycle-accurate) ou **estimés** (modèle
de cycles, cas de l'ETM simulé sous QEMU) — l'origine est toujours affichée.

Chronogramme (flame chart) : temps en x, profondeur d'appel en y, un rectangle par appel.
Couleur = identité : les 7 entités au plus gros temps brut (la surface qu'elles occupent dans
le chronogramme) reçoivent les 7 premières teintes de la palette catégorielle validée (ordre
fixe, attribution mémorisée : une couleur suit son entité d'une analyse à l'autre) ; le reste est replié en « Autres » (gris neutre). Trois
teintes ont un contraste < 3:1 sur le fond : noms inscrits dans les rectangles, info-bulle
et onglet Fonctions (vue tableau) servent de relief.
"""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk

from ..gui.view_base import View
from ..trace.timing import EXCEPTION, TimingAnalyzer, TimingResult

# Palette catégorielle de référence (slots 1 à 7, validés : scripts/validate_palette.js)
SLOTS = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6"]
OTHER = "#c3c2b7"                       # « Autres » : gris neutre (jamais une teinte générée)
SURFACE, INK, INK2, MUTED, GRID = "#fcfcfb", "#0b0b0b", "#52514e", "#898781", "#e1e0d9"
LABEL_INK = {SLOTS[3]: INK, OTHER: INK}  # texte noir sur le jaune et le gris, blanc ailleurs

ROW, TOP, LEFT = 22, 26, 6               # hauteur de rangée, place de l'axe, marge


def nice_step(span: float, target: int = 8) -> float:
    """Pas « 1-2-5 » pour environ `target` graduations."""
    if span <= 0:
        return 1.0
    raw = span / target
    mag = 10 ** int(f"{raw:e}".split("e")[1])
    for m in (1, 2, 5, 10):
        if m * mag >= raw:
            return m * mag
    return 10 * mag


class TimingView(View):
    title = "Timing"
    kind = "timing"

    def __init__(self, master, app, **kw):
        super().__init__(master, app, **kw)
        bar = ttk.Frame(self)
        bar.pack(fill="x")
        ttk.Button(bar, text="Analyser", command=self.analyze).pack(side="left", padx=2)
        ttk.Label(bar, text="Unité :").pack(side="left", padx=(8, 0))
        self.unit_var = tk.StringVar(value="µs")
        cb = ttk.Combobox(bar, textvariable=self.unit_var, values=["µs", "cycles"], state="readonly", width=7)
        cb.pack(side="left", padx=2)
        cb.bind("<<ComboboxSelected>>", lambda _e: self._render())
        self.info = ttk.Label(bar, text="")
        self.info.pack(side="left", padx=8)

        self.tabs = ttk.Notebook(self)
        self.tabs.pack(fill="both", expand=True)
        f_tab, f_tree, f_irq, f_dist, f_chart = (ttk.Frame(self.tabs) for _ in range(5))
        self.tabs.add(f_tab, text="Fonctions")
        self.tabs.add(f_tree, text="Arbre d'appels")
        self.tabs.add(f_irq, text="Exceptions")
        self.tabs.add(f_dist, text="Distribution")
        self.tabs.add(f_chart, text="Chronogramme")

        cols = [("kind", "Type", 72), ("calls", "Appels", 56), ("partial", "Partiels", 60),
                ("gross", "Brut total", 80), ("pct", "%", 48), ("min", "Min", 64),
                ("max", "Max", 64), ("avg", "Moyenne", 66), ("net", "Net", 80)]
        self.table = ttk.Treeview(f_tab, columns=[c[0] for c in cols], show="tree headings", selectmode="browse")
        self.table.heading("#0", text="Fonction", command=lambda: self._sort("name"))
        self.table.column("#0", width=160, minwidth=100, stretch=True)
        for cid, label, width in cols:
            self.table.heading(cid, text=label, command=lambda c=cid: self._sort(c))
            self.table.column(cid, width=width, stretch=False, anchor="e" if cid != "kind" else "w")
        sb = ttk.Scrollbar(f_tab, orient="vertical", command=self.table.yview)
        hb = ttk.Scrollbar(f_tab, orient="horizontal", command=self.table.xview)
        self.table.configure(yscrollcommand=sb.set, xscrollcommand=hb.set)
        self.table.grid(row=0, column=0, sticky="nsew")
        sb.grid(row=0, column=1, sticky="ns")
        hb.grid(row=1, column=0, sticky="ew")
        f_tab.rowconfigure(0, weight=1)
        f_tab.columnconfigure(0, weight=1)
        self.table.bind("<Double-1>", lambda _e: self._goto(self.table.focus()))

        # arbre d'appels : enfants insérés à l'ouverture d'un nœud (peu d'appels Tk)
        tcols = [("calls", "Appels", 56), ("partial", "Partiels", 60), ("gross", "Brut", 80),
                 ("pct", "%", 48), ("min", "Min", 64), ("max", "Max", 64), ("avg", "Moyenne", 66),
                 ("net", "Net", 80)]
        self.calltree = ttk.Treeview(f_tree, columns=[c[0] for c in tcols], show="tree headings",
                                     selectmode="browse")
        self.calltree.heading("#0", text="Chemin d'appel")
        self.calltree.column("#0", width=260, minwidth=120, stretch=True)
        for cid, label, width in tcols:
            self.calltree.heading(cid, text=label)
            self.calltree.column(cid, width=width, stretch=False, anchor="e")
        tsb = ttk.Scrollbar(f_tree, orient="vertical", command=self.calltree.yview)
        self.calltree.configure(yscrollcommand=tsb.set)
        self.calltree.pack(side="left", fill="both", expand=True)
        tsb.pack(side="right", fill="y")
        self.calltree.bind("<<TreeviewOpen>>", lambda _e: self._open_node(self.calltree.focus()))
        self.calltree.bind("<Double-1>", lambda _e: self._goto_node(self.calltree.focus()))
        self._node_path: dict[str, tuple] = {}

        # statistiques d'exceptions (Trace.STATistic.InterruptIsr)
        icols = [("n", "Entrées", 60), ("dmin", "Durée min", 72), ("davg", "moy", 66), ("dmax", "max", 66),
                 ("pmin", "Période min", 80), ("pavg", "moy", 70), ("pmax", "max", 70), ("jit", "Gigue", 66),
                 ("pre", "Préemptée", 70), ("pre2", "Préempte", 66), ("tail", "Enchaînées", 70),
                 ("ent", "Entrée", 56)]
        self.irqs = ttk.Treeview(f_irq, columns=[c[0] for c in icols], show="tree headings", selectmode="browse")
        self.irqs.heading("#0", text="Exception")
        self.irqs.column("#0", width=130, stretch=True)
        for cid, label, width in icols:
            self.irqs.heading(cid, text=label)
            self.irqs.column(cid, width=width, stretch=False, anchor="e")
        ttk.Label(f_irq, foreground=MUTED, wraplength=700, justify="left",
                  text="Durée : de l'entrée au retour, sans les exceptions plus prioritaires. Période : entre "
                       "deux entrées ; gigue = plus longue − plus courte. Entrée : cycles de l'empilement. La "
                       "latence entre la demande d'interruption et l'entrée ne se voit pas dans la trace."
                  ).pack(side="bottom", fill="x", padx=4, pady=2)
        isb = ttk.Scrollbar(f_irq, orient="vertical", command=self.irqs.yview)
        self.irqs.configure(yscrollcommand=isb.set)
        self.irqs.pack(side="left", fill="both", expand=True)
        isb.pack(side="right", fill="y")

        self.dist = DistributionPanel(f_dist, self)
        self.chart = FlameChart(f_chart, self)
        self.result: TimingResult | None = None
        self.color_of: dict[str, str] = {}
        self._sort_key, self._sort_rev = "gross", True
        self._generation = -1
        self._busy = False
        app.trace.etm_listeners.append(self._on_new_trace)

    def on_program_changed(self) -> None:
        self.result = None
        self.color_of.clear()
        self._generation = -1
        self.table.delete(*self.table.get_children())
        self.chart.result = None
        self.chart.draw()
        self.info.configure(text="")

    def destroy(self) -> None:
        if self._on_new_trace in self.app.trace.etm_listeners:
            self.app.trace.etm_listeners.remove(self._on_new_trace)
        super().destroy()

    # ------------------------------------------------------------------ analyse
    def _on_new_trace(self) -> None:
        # cible en marche : l'analyse de tout l'historique est coûteuse ; refresh() la refait à
        # l'arrêt si la trace a changé
        if self.winfo_exists() and not self.trace_settling:
            self.analyze()

    def refresh(self) -> None:
        etm = self.app.trace.etm
        if etm is not None and etm.generation != self._generation and not self.trace_settling:
            self.analyze()

    def analyze(self) -> None:
        etm = self.app.trace.etm
        if etm is None:
            self.info.configure(text="trace d'instructions indisponible (--etm-sim / --mtb-sim sous QEMU, "
                                     "--mtb ou capture ETM sur cible)")
            return
        if self._busy:
            return
        if not etm.history:
            self.info.configure(text="trace vide : lancez la cible puis arrêtez-la")
            return
        self._busy = True
        self._generation = etm.generation
        symbols = self.app.symbols

        def work():
            whole = etm.timing_result()         # trace sur disque : toute la trace
            if whole is not None:
                return whole, etm.timing
            events = etm.last(etm.history.maxlen or 200_000)
            return TimingAnalyzer(etm.image, symbols).analyze(events), etm.timing

        def done(res):
            self._busy = False
            if self.winfo_exists():
                self.show(*res)
        self.info.configure(text="analyse…")
        self.app.pump.background(work, then=done)

    def show(self, result: TimingResult, timing: str = "") -> None:
        self.result = result
        self.timing = timing
        self._assign_colors()
        self._render()

    def _assign_colors(self) -> None:
        """Teintes par identité, par ordre de temps brut ; une entité garde sa teinte d'une
        analyse à l'autre tant qu'elle est présente ; au-delà des 7 slots, « Autres »."""
        stats = self.result.stats
        self.color_of = {n: c for n, c in self.color_of.items() if n in stats}
        used = set(self.color_of.values())
        for st in sorted(stats.values(), key=lambda s: (-s.gross, s.name)):
            if len(used) == len(SLOTS):
                break
            if st.name in self.color_of or st.gross <= 0:
                continue
            free = next(c for c in SLOTS if c not in used)
            self.color_of[st.name] = free
            used.add(free)

    def color(self, name: str) -> str:
        return self.color_of.get(name, OTHER)

    # ------------------------------------------------------------------ unités
    def fmt(self, cycles: float | None) -> str:
        if cycles is None:
            return ""
        if self.unit_var.get() == "cycles":
            return f"{cycles:,.0f}".replace(",", " ")
        us = cycles * 1e6 / self.app.cpu_hz
        return f"{us:,.2f}".replace(",", " ") if us < 1000 else f"{us:,.0f}".replace(",", " ")

    @property
    def unit(self) -> str:
        return self.unit_var.get()

    # ------------------------------------------------------------------ rendu
    def _render(self) -> None:
        r = self.result
        if r is None:
            return
        self.info.configure(text=f"{self.fmt(r.total)} {self.unit} analysés — temps {self.timing}"
                                 + (f" — {r.context_switches} changements de tâche" if r.context_switches else ""))
        self._render_table()
        self._render_tree()
        self._render_irqs()
        self.dist.set_result(r)
        self.chart.set_data(r)

    def _sort(self, key: str) -> None:
        self._sort_rev = not self._sort_rev if key == self._sort_key else key != "name"
        self._sort_key = key
        self._render_table()

    def _render_table(self) -> None:
        r = self.result
        keyf = {"name": lambda s: s.name, "kind": lambda s: s.kind, "calls": lambda s: s.calls,
                "partial": lambda s: s.partial, "gross": lambda s: s.gross, "pct": lambda s: s.gross,
                "min": lambda s: s.min or 0, "max": lambda s: s.max, "avg": lambda s: s.avg or 0,
                "net": lambda s: s.net}[self._sort_key]
        rows = sorted(r.stats.values(), key=keyf, reverse=self._sort_rev)
        self.table.delete(*self.table.get_children())
        for s in rows:
            self.table.insert("", "end", iid=s.name, text=s.name, values=(
                "exception" if s.kind == EXCEPTION else "fonction", s.calls, s.partial or "",
                self.fmt(s.gross), f"{100 * s.gross / r.total:.1f}" if r.total else "",
                self.fmt(s.min), self.fmt(s.max) if s.calls else "", self.fmt(s.avg), self.fmt(s.net)))

    def _render_irqs(self) -> None:
        from ..trace.irqstats import sorted_rows
        self.irqs.delete(*self.irqs.get_children())
        for st in sorted_rows(self.result.irq):
            self.irqs.insert("", "end", text=st.name.strip("[]"), values=(
                st.count, self.fmt(st.min), self.fmt(st.avg), self.fmt(st.max) if st.complete else "",
                self.fmt(st.period_min), self.fmt(st.period_avg), self.fmt(st.period_max) if st.period_n else "",
                self.fmt(st.jitter), st.preempted or "", st.preempting or "", st.tail or "",
                f"{st.entry_avg:.0f}" if st.entry_avg else ""))

    # ------------------------------------------------------------------ arbre d'appels
    def _node_values(self, st) -> tuple:
        total = self.result.total if self.result else 0
        return (st.calls, st.partial or "", self.fmt(st.gross),
                f"{100 * st.gross / total:.1f}" if total else "", self.fmt(st.min),
                self.fmt(st.max) if st.calls else "", self.fmt(st.avg), self.fmt(st.net))

    def _insert_children(self, parent_iid: str, path: tuple) -> None:
        r = self.result
        for child, st in r.children(path):
            iid = self.calltree.insert(parent_iid, "end", text=st.name, values=self._node_values(st),
                                       tags=("exception",) if st.kind == EXCEPTION else ())
            self._node_path[iid] = child
            if r.has_children(child):
                self.calltree.insert(iid, "end", text="…", tags=("dummy",))   # à déplier

    def _render_tree(self) -> None:
        opened = {self._node_path.get(i) for i in self._open_iids()}
        self.calltree.delete(*self.calltree.get_children())
        self._node_path = {}
        if self.result is None:
            return
        self._insert_children("", ())
        queue = list(self._node_path.items())                 # nœuds ouverts avant l'analyse
        while queue:
            iid, path = queue.pop(0)
            if path in opened:
                before = set(self._node_path)
                self._open_node(iid)
                self.calltree.item(iid, open=True)
                queue += [(i, self._node_path[i]) for i in self._node_path if i not in before]

    def _open_iids(self) -> list[str]:
        out, todo = [], list(self.calltree.get_children())
        while todo:
            iid = todo.pop()
            if self.calltree.item(iid, "open"):
                out.append(iid)
                todo += self.calltree.get_children(iid)
        return out

    def _open_node(self, iid: str) -> None:
        kids = self.calltree.get_children(iid) if iid else ()
        if len(kids) == 1 and "dummy" in self.calltree.item(kids[0], "tags"):
            self.calltree.delete(kids[0])
            self._insert_children(iid, self._node_path[iid])

    def _goto_node(self, iid: str) -> None:
        path = self._node_path.get(iid)
        if path and self.result is not None:
            self._goto(path[-1], self.result.tree[path].entry)

    def _goto(self, name: str | None, entry: int | None = None) -> None:
        if entry is None and name and self.result and name in self.result.stats:
            entry = self.result.stats[name].entry
        sym = self.app.symbols
        le = sym.line_at(entry) if (sym and entry is not None) else None
        if le:
            self.app.show_source(le.file, le.line)

    def get_state(self) -> dict:
        return {"unit": self.unit_var.get(), "tab": self.tabs.index("current"),
                "distribution": self.dist.get_state()}

    def set_state(self, state: dict) -> None:
        self.unit_var.set(state.get("unit", "µs"))
        self.dist.set_state(state.get("distribution", {}))
        if "tab" in state:
            self.tabs.select(state["tab"])


class DistributionPanel:
    """Onglet Distribution (Trace.STATistic.DISTribution) : histogramme des durées brutes des
    appels complets d'une fonction ou d'une exception, centiles, appels les plus longs ;
    double-clic sur un appel : la vue Trace à sa position. Une seule série : une teinte (slot
    1), barres fines séparées de 2 px, posées sur la ligne de base ; axes et textes en encre
    de texte, grille récessive."""

    PAD_L, PAD_R, PAD_T, PAD_B = 52, 12, 14, 30

    def __init__(self, master, view: "TimingView"):
        self.view = view
        top = ttk.Frame(master)
        top.pack(fill="x")
        ttk.Label(top, text="Fonction :").pack(side="left", padx=(2, 0))
        self.name_var = tk.StringVar(master)
        self.names = ttk.Combobox(top, textvariable=self.name_var, state="readonly", width=28)
        self.names.pack(side="left", padx=2)
        self.names.bind("<<ComboboxSelected>>", lambda _e: self.render())
        ttk.Label(top, text="Classes :").pack(side="left", padx=(8, 0))
        self.bins_var = tk.StringVar(master, value="20")
        sp = ttk.Spinbox(top, from_=4, to=60, textvariable=self.bins_var, width=4, command=self.render)
        sp.pack(side="left", padx=2)
        sp.bind("<Return>", lambda _e: self.render())
        ttk.Label(top, text="Échelle :").pack(side="left", padx=(8, 0))
        self.scale_var = tk.StringVar(master, value="auto")
        cb = ttk.Combobox(top, textvariable=self.scale_var, values=["auto", "linéaire", "log"],
                          state="readonly", width=8)
        cb.pack(side="left", padx=2)
        cb.bind("<<ComboboxSelected>>", lambda _e: self.render())
        self.info = ttk.Label(top, text="", foreground=MUTED)
        self.info.pack(side="left", padx=8)
        body = ttk.PanedWindow(master, orient="horizontal")
        body.pack(fill="both", expand=True)
        left = ttk.Frame(body)
        self.canvas = tk.Canvas(left, background=SURFACE, highlightthickness=0, height=220)
        self.canvas.pack(fill="both", expand=True)
        self.tip = ttk.Label(left, text="", foreground=INK2)
        self.tip.pack(fill="x", padx=4)
        right = ttk.Frame(body)
        body.add(left, weight=3)
        body.add(right, weight=1)
        self.table = ttk.Treeview(right, columns=("value", "where"), show="tree headings", selectmode="browse")
        self.table.heading("#0", text="")
        self.table.heading("value", text="Durée")
        self.table.heading("where", text="Index")
        self.table.column("#0", width=110, stretch=False)
        self.table.column("value", width=90, anchor="e", stretch=False)
        self.table.column("where", width=80, anchor="e", stretch=True)
        self.table.pack(fill="both", expand=True)
        self.table.bind("<Double-1>", lambda _e: self._goto_call())
        self.canvas.bind("<Configure>", lambda _e: self.draw())
        self.canvas.bind("<Motion>", self._motion)
        self.canvas.bind("<Leave>", lambda _e: self.tip.configure(text=""))
        self.result: TimingResult | None = None
        self.dist = None
        self._where: dict[str, int] = {}

    # ------------------------------------------------------------------ données
    def set_result(self, result: TimingResult) -> None:
        self.result = result
        names = sorted(result.durations, key=lambda n: (-result.stats[n].gross if n in result.stats else 0, n))
        self.names.configure(values=names)
        if self.name_var.get() not in names:
            self.name_var.set(names[0] if names else "")
        self.render()

    def render(self) -> None:
        r, name = self.result, self.name_var.get()
        if r is None or not name:
            self.dist = None
            self.draw()
            return
        try:
            bins = max(4, min(60, int(self.bins_var.get())))
        except ValueError:
            bins = 20
        log = {"auto": None, "linéaire": False, "log": True}[self.scale_var.get()]
        v = self.view
        cycles = v.unit == "cycles"
        self.dist = r.distribution(name, bins, log, 1.0 if cycles else 1e6 / v.app.cpu_hz,
                                   "cycles" if cycles else "µs")
        d = self.dist
        self.info.configure(text=f"{d.count} appels complets" + (" — échelle logarithmique" if d.log else "")
                                 + (" — centiles sur un échantillon" if d.sampled else ""))
        self._fill_table()
        self.draw()

    def _fill_table(self) -> None:
        t, d = self.table, self.dist
        t.delete(*t.get_children())
        self._where.clear()
        fmt = self._fmt
        for label, value in (("minimum", d.min), ("p50", d.percentiles.get(50)), ("p90", d.percentiles.get(90)),
                             ("p99", d.percentiles.get(99)), ("maximum", d.max), ("moyenne", d.mean)):
            t.insert("", "end", text=label, values=(fmt(value), ""))
        if d.longest:
            node = t.insert("", "end", text="plus longs", open=True, values=("", ""))
            for value, where in d.longest:
                iid = t.insert(node, "end", text="", values=(fmt(value), "" if where is None else where))
                if where is not None:
                    self._where[iid] = where

    def _fmt(self, v) -> str:
        if v is None:
            return ""
        return f"{v:,.0f}".replace(",", " ") if abs(v) >= 1000 or self.view.unit == "cycles" else f"{v:.2f}"

    def _goto_call(self) -> None:
        where = self._where.get(self.table.focus())
        if where is None:
            return
        from .trace import TraceView
        tv = self.view.app.views.show(TraceView, "bottom")
        if tv is not None:
            tv.goto_index(where)

    # ------------------------------------------------------------------ dessin
    def draw(self) -> None:
        c = self.canvas
        c.delete("all")
        d = self.dist
        if d is None or not d.count:
            c.create_text(8, 16, anchor="w", fill=MUTED,
                          text="Pas d'appel complet : lancez la cible, arrêtez-la, puis Analyser.")
            return
        w, h = max(c.winfo_width(), 200), max(c.winfo_height(), 120)
        x0, x1, y0, y1 = self.PAD_L, w - self.PAD_R, self.PAD_T, h - self.PAD_B
        peak = max(b.count for b in d.bins) or 1
        step = nice_step(peak, 4)
        k = 1
        while k * step <= peak:                       # grille horizontale récessive
            y = y1 - (y1 - y0) * k * step / peak
            c.create_line(x0, y, x1, y, fill=GRID)
            c.create_text(x0 - 4, y, anchor="e", text=f"{k * step:g}", fill=MUTED, font=("TkDefaultFont", 8))
            k += 1
        n = len(d.bins)
        slot = (x1 - x0) / n
        for i, b in enumerate(d.bins):                # barres : 1 px de fond de chaque côté
            if not b.count:
                continue
            top = y1 - (y1 - y0) * b.count / peak
            c.create_rectangle(x0 + i * slot + 1, min(top, y1 - 1), x0 + (i + 1) * slot - 1, y1,
                               fill=SLOTS[0], outline="")
        c.create_line(x0, y1, x1, y1, fill=INK2)       # ligne de base
        for i in sorted({0, n // 2, n}):              # bornes : début, milieu, fin
            edge = d.bins[i].low if i < n else d.bins[-1].high
            c.create_text(x0 + i * slot, y1 + 4, anchor="n" if 0 < i < n else ("nw" if i == 0 else "ne"),
                          text=self._fmt(edge), fill=INK2, font=("TkDefaultFont", 8))
        c.create_text(x1, h - 2, anchor="se", text=d.unit, fill=MUTED, font=("TkDefaultFont", 8))
        c.create_text(x0 - 4, y0 - 4, anchor="se", text="appels", fill=MUTED, font=("TkDefaultFont", 8))

    def bin_at(self, x: float):
        d = self.dist
        if d is None or not d.bins:
            return None
        w = max(self.canvas.winfo_width(), 200)
        x0, x1 = self.PAD_L, w - self.PAD_R
        if not x0 <= x < x1:
            return None
        return d.bins[min(len(d.bins) - 1, int((x - x0) / ((x1 - x0) / len(d.bins))))]

    def _motion(self, e) -> None:
        b = self.bin_at(e.x)
        self.tip.configure(text="" if b is None else
                           f"{self._fmt(b.low)} – {self._fmt(b.high)} {self.dist.unit} : {b.count} appel(s)")

    def get_state(self) -> dict:
        return {"name": self.name_var.get(), "bins": self.bins_var.get(), "scale": self.scale_var.get()}

    def set_state(self, state: dict) -> None:
        if state.get("name"):
            self.name_var.set(state["name"])
        if state.get("bins"):
            self.bins_var.set(state["bins"])
        if state.get("scale") in ("auto", "linéaire", "log"):
            self.scale_var.set(state["scale"])


class FlameChart:
    """Chronogramme sur tk.Canvas : zoom molette, glisser pour déplacer, survol = info-bulle,
    clic = source de la fonction."""

    def __init__(self, master, view: TimingView):
        self.view = view
        top = ttk.Frame(master)
        top.pack(fill="x")
        ttk.Button(top, text="Tout afficher", command=self.reset_zoom).pack(side="left", padx=2, pady=2)
        ttk.Label(top, text="molette : zoom — glisser : déplacer — clic : source — pointillés : appel partiel",
                  foreground=MUTED).pack(side="left", padx=8)
        self.canvas = tk.Canvas(master, background=SURFACE, highlightthickness=0, height=260)
        self.canvas.pack(fill="both", expand=True)
        self.legend = ttk.Frame(master)
        self.legend.pack(fill="x", pady=2)
        self.result: TimingResult | None = None
        self.v0, self.v1 = 0.0, 1.0
        self.items: dict[int, int] = {}          # id du rectangle → indice d'intervalle
        self._drag: tuple[int, float, float] | None = None
        self._moved = False
        self._hover: int | None = None
        c = self.canvas
        c.bind("<Configure>", lambda _e: self.draw())
        c.bind("<Motion>", self._motion)
        c.bind("<Leave>", lambda _e: self._hide_tip())
        c.bind("<ButtonPress-1>", self._press)
        c.bind("<B1-Motion>", self._drag_move)
        c.bind("<ButtonRelease-1>", self._release)
        c.bind("<MouseWheel>", lambda e: self.zoom(e.x, 1.25 if e.delta > 0 else 0.8))
        c.bind("<Button-4>", lambda e: self.zoom(e.x, 1.25))
        c.bind("<Button-5>", lambda e: self.zoom(e.x, 0.8))

    # ------------------------------------------------------------------ données et vue
    def set_data(self, result: TimingResult) -> None:
        old = self.result
        self.result = result
        if old is None or old.total != result.total or self.v1 <= self.v0:
            self.reset_zoom(draw=False)           # autre fenêtre de trace : vue d'ensemble
        self.draw()
        self._draw_legend()

    def reset_zoom(self, draw: bool = True) -> None:
        self.v0, self.v1 = 0.0, float(max(1, self.result.total if self.result else 1))
        if draw:
            self.draw()

    def _scale(self) -> float:
        w = max(1, self.canvas.winfo_width() - 2 * LEFT)
        return w / max(1e-9, self.v1 - self.v0)

    def x_of(self, t: float) -> float:
        return LEFT + (t - self.v0) * self._scale()

    def t_of(self, x: float) -> float:
        return self.v0 + (x - LEFT) / self._scale()

    def zoom(self, x: float, factor: float) -> None:
        if self.result is None:
            return
        t = self.t_of(x)
        span = max(4.0, (self.v1 - self.v0) / factor)
        self.v0 = max(0.0, t - (t - self.v0) / (self.v1 - self.v0) * span)
        self.v1 = min(float(self.result.total), self.v0 + span)
        self.draw()

    # ------------------------------------------------------------------ dessin
    def draw(self) -> None:
        c = self.canvas
        c.delete("all")
        self.items.clear()
        self._hover = None
        r = self.result
        if r is None or not r.intervals:
            c.create_text(LEFT + 4, TOP, anchor="w", fill=MUTED,
                          text="Pas encore de trace : lancez la cible puis arrêtez-la.")
            return
        h = c.winfo_height()
        self._draw_axis(h)
        scale = self._scale()
        for idx, iv in enumerate(r.intervals):
            if iv.end <= self.v0 or iv.start >= self.v1:
                continue
            x0, x1 = self.x_of(max(iv.start, self.v0)), self.x_of(min(iv.end, self.v1))
            if x1 - x0 < 1.0:
                continue                       # invisible à ce niveau de zoom
            y0 = TOP + iv.depth * ROW + 1
            if y0 > h:
                continue
            fill = self.view.color(iv.name)
            # 1 px de fond de chaque côté : 2 px d'espace entre deux appels voisins
            rid = c.create_rectangle(x0 + 1, y0, max(x0 + 2, x1 - 1), y0 + ROW - 2, fill=fill,
                                     outline=INK if (iv.partial_start or iv.partial_end) else "",
                                     dash=(2, 2) if (iv.partial_start or iv.partial_end) else (),
                                     width=1)
            self.items[rid] = idx
            if x1 - x0 > 34:
                chars = int((x1 - x0 - 8) / 7)
                label = iv.name if len(iv.name) <= chars else iv.name[:max(1, chars - 1)] + "…"
                c.create_text(x0 + 5, y0 + (ROW - 2) / 2, anchor="w", text=label,
                              fill=LABEL_INK.get(fill, "#ffffff"), font=("TkDefaultFont", 8),
                              tags=("label",))

    def _draw_axis(self, h: int) -> None:
        c = self.canvas
        v = self.view
        to_unit = (lambda t: t) if v.unit == "cycles" else (lambda t: t * 1e6 / v.app.cpu_hz)
        from_unit = (lambda u: u) if v.unit == "cycles" else (lambda u: u * v.app.cpu_hz / 1e6)
        u0, u1 = to_unit(self.v0), to_unit(self.v1)
        step = nice_step(u1 - u0)
        k = int(u0 / step)
        right = c.winfo_width() - LEFT - 48          # place réservée à l'unité
        while k * step <= u1:
            u = k * step
            if u >= u0:
                x = self.x_of(from_unit(u))
                c.create_line(x, TOP - 4, x, h, fill=GRID)                 # grille récessive
                if x < right:
                    c.create_text(x + 2, TOP - 6, anchor="sw", text=f"{u:,.10g}".replace(",", " "),
                                  fill=MUTED, font=("TkDefaultFont", 8))
            k += 1
        c.create_text(c.winfo_width() - LEFT, TOP - 6, anchor="se", text=v.unit, fill=MUTED,
                      font=("TkDefaultFont", 8))
        c.create_line(LEFT, TOP - 1, c.winfo_width() - LEFT, TOP - 1, fill="#c3c2b7")

    def _draw_legend(self) -> None:
        for w in self.legend.winfo_children():
            w.destroy()
        names = [n for n, col in sorted(self.view.color_of.items(), key=lambda kv: SLOTS.index(kv[1]))
                 if self.result and n in self.result.stats]
        for n in names + ["Autres"]:
            col = self.view.color(n) if n != "Autres" else OTHER
            sw = tk.Canvas(self.legend, width=12, height=12, background=SURFACE, highlightthickness=0)
            sw.create_rectangle(1, 1, 11, 11, fill=col, outline="")
            sw.pack(side="left", padx=(8, 2))
            ttk.Label(self.legend, text=n, foreground=INK2).pack(side="left")

    # ------------------------------------------------------------------ interaction
    def _item_at(self, x: float, y: float) -> int | None:
        for item in reversed(self.canvas.find_overlapping(x - 1, y - 1, x + 1, y + 1)):
            if item in self.items:
                return item
        return None

    def interval_at(self, x: float, y: float):
        item = self._item_at(x, y)
        return self.result.intervals[self.items[item]] if item is not None else None

    def _motion(self, e) -> None:
        if self._drag is not None:
            return
        item = self._item_at(e.x, e.y)
        if item is None:
            self._hide_tip()
            return
        if item != self._hover:
            self._hide_tip()
            self._hover = item
            self.canvas.itemconfigure(item, outline=INK, width=2)      # la marque survolée réagit
        iv = self.result.intervals[self.items[item]]
        v = self.view
        dur = f"{v.fmt(iv.duration)} {v.unit}"
        detail = f"{iv.name}   début {v.fmt(iv.start)} {v.unit}"
        if iv.partial_start or iv.partial_end:
            detail += "   (partiel)"
        elif iv.suspended or iv.resumed:
            detail += "   (morceau : tâche préemptée)"
        self._show_tip(e.x, e.y, dur, detail)

    def _show_tip(self, x: float, y: float, value: str, detail: str) -> None:
        c = self.canvas
        c.delete("tip")
        tx = min(x + 14, max(LEFT, c.winfo_width() - 260))
        ty = y + 16 if y < c.winfo_height() - 50 else y - 46
        t1 = c.create_text(tx + 8, ty + 6, anchor="nw", text=value, fill=INK,
                           font=("TkDefaultFont", 10, "bold"), tags=("tip",))   # la valeur d'abord
        t2 = c.create_text(tx + 8, ty + 24, anchor="nw", text=detail, fill=INK2,
                           font=("TkDefaultFont", 8), tags=("tip",))
        x0, y0, x1, y1 = c.bbox(t1)
        _, _, x2, y2 = c.bbox(t2)
        bg = c.create_rectangle(tx, ty, max(x1, x2) + 8, y2 + 6, fill="#ffffff", outline=GRID, tags=("tip",))
        c.tag_lower(bg, t1)

    def _hide_tip(self) -> None:
        self.canvas.delete("tip")
        if self._hover is not None and self._hover in self.items:
            iv = self.result.intervals[self.items[self._hover]]
            partial = iv.partial_start or iv.partial_end
            self.canvas.itemconfigure(self._hover, outline=INK if partial else "", width=1)
        self._hover = None

    def _press(self, e) -> None:
        self._drag = (e.x, self.v0, self.v1)
        self._moved = False

    def _drag_move(self, e) -> None:
        if self._drag is None or self.result is None:
            return
        x, v0, v1 = self._drag
        dt = (e.x - x) / self._scale()
        if abs(e.x - x) > 3:
            self._moved = True
        span = v1 - v0
        self.v0 = min(max(0.0, v0 - dt), max(0.0, self.result.total - span))
        self.v1 = self.v0 + span
        self.draw()

    def _release(self, e) -> None:
        drag, self._drag = self._drag, None
        if drag is not None and not self._moved:
            iv = self.interval_at(e.x, e.y)
            if iv is not None:
                self.view._goto(iv.name, iv.entry)
