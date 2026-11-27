"""Vue Système : ce que la puce a vraiment. Onglets :
- CoreSight : composants de débogage et de trace des tables ROM (trace/coresight.py) ;
- MPU : régions de la MPU décodées (core/mpu.py) ; TrustZone : celle de l'état courant ou la
  non sécurisée (alias du SCS, depuis l'état sécurisé) ;
- SAU : attribution de sécurité (core/security.py), cœurs TrustZone ;
- Puce : la puce décrite par son CMSIS-Pack (core/pack.py) et son identité lue sur la cible.

Les tables ROM sont lues à la connexion (TraceManager.discover) et sur demande ; la MPU et le
SAU à chaque arrêt, si l'onglet est visible."""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk

from ..gui.view_base import View


def _tree(parent, columns, first: tuple[str, int]) -> ttk.Treeview:
    frame = ttk.Frame(parent)
    frame.pack(fill="both", expand=True)
    tree = ttk.Treeview(frame, columns=[c[0] for c in columns], show="tree headings", selectmode="browse")
    tree.heading("#0", text=first[0])
    tree.column("#0", width=first[1], stretch=False)
    for cid, label, width in columns:
        tree.heading(cid, text=label)
        tree.column(cid, width=width, stretch=cid == columns[-1][0])
    sb = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
    tree.configure(yscrollcommand=sb.set)
    tree.pack(side="left", fill="both", expand=True)
    sb.pack(side="right", fill="y")
    tree.tag_configure("dim", foreground="#777777")
    tree.tag_configure("off", foreground="#777777")
    return tree


class SystemView(View):
    title = "Système"
    kind = "system"

    def __init__(self, master, app, **kw):
        super().__init__(master, app, **kw)
        self.nb = ttk.Notebook(self)
        self.nb.pack(fill="both", expand=True)
        self.nb.bind("<<NotebookTabChanged>>", lambda _e: self.refresh())

        # --- CoreSight
        cs = ttk.Frame(self.nb)
        self.nb.add(cs, text="CoreSight")
        bar = ttk.Frame(cs)
        bar.pack(fill="x")
        ttk.Button(bar, text="Relire les tables ROM", command=self._reread).pack(side="left")
        self.cs_info = ttk.Label(bar, text="cible non arrêtée")
        self.cs_info.pack(side="left", padx=6)
        self.cs_tree = _tree(cs, [("type", "Type", 80), ("part", "Pièce", 70), ("designer", "Fabricant", 130),
                                  ("rev", "Rév.", 40), ("addr", "Adresse", 100)], ("Composant", 300))

        # --- MPU
        mp = ttk.Frame(self.nb)
        self.nb.add(mp, text="MPU")
        self.mpu_info = ttk.Label(mp, text="cible non arrêtée", wraplength=700, justify="left")
        self.mpu_info.pack(fill="x", padx=4, pady=2)
        self.mpu_info.bind("<Configure>", lambda e: self.mpu_info.configure(wraplength=max(200, e.width - 8)))
        self.mpu_ns = tk.BooleanVar(self, value=False)
        self.mpu_ns_check = ttk.Checkbutton(mp, text="MPU non sécurisée (TrustZone)", variable=self.mpu_ns,
                                            command=lambda: self.refresh_mpu(force=True))
        self.mpu_tree = _tree(mp, [("on", "Active", 50), ("base", "Début", 95), ("end", "Fin", 95),
                                   ("size", "Taille", 80), ("sub", "Sous-régions", 110), ("ap", "Accès", 190),
                                   ("xn", "XN", 40), ("mem", "Type mémoire", 260)], ("Région", 70))
        self.mpu_stop = -1

        # --- SAU (TrustZone)
        sa = ttk.Frame(self.nb)
        self.nb.add(sa, text="SAU")
        self.sau_info = ttk.Label(sa, text="cible non arrêtée", wraplength=700, justify="left")
        self.sau_info.pack(fill="x", padx=4, pady=2)
        self.sau_info.bind("<Configure>", lambda e: self.sau_info.configure(wraplength=max(200, e.width - 8)))
        self.sau_tree = _tree(sa, [("on", "Active", 50), ("base", "Début", 95), ("end", "Fin", 95),
                                   ("size", "Taille", 80), ("attr", "Attribut", 80)], ("Région", 70))
        self.sau_stop = -1

        # --- Puce
        dv = ttk.Frame(self.nb)
        self.nb.add(dv, text="Puce")
        self.dev_info = ttk.Label(dv, text="aucune puce décrite (--pack, --device)", wraplength=700,
                                  justify="left")
        self.dev_info.pack(fill="x", padx=4, pady=2)
        self.dev_info.bind("<Configure>", lambda e: self.dev_info.configure(wraplength=max(200, e.width - 8)))
        self.dev_tree = _tree(dv, [("value", "Valeur", 260), ("info", "Détail", 300)], ("", 180))

    # ------------------------------------------------------------------ cycle de vie
    def _tab(self) -> str:
        try:
            return self.nb.tab(self.nb.select(), "text")
        except tk.TclError:
            return ""

    def refresh(self) -> None:
        tab = self._tab()
        if tab == "CoreSight":
            self.render_coresight()
        elif tab == "MPU":
            self.refresh_mpu()
        elif tab == "SAU":
            self.refresh_sau()
        elif tab == "Puce":
            self.render_device()

    def on_running(self) -> None:
        if self._tab() == "MPU" and not self.stopped:     # événement en retard sur un arrêt
            self.mpu_info.configure(text="cible en marche : la MPU se relit à l'arrêt")
        if self._tab() == "SAU" and not self.stopped:
            self.sau_info.configure(text="cible en marche : le SAU se relit à l'arrêt")

    def on_disconnected(self) -> None:
        for t in (self.cs_tree, self.mpu_tree, self.sau_tree, self.dev_tree):
            t.delete(*t.get_children())
        self.cs_info.configure(text="pas de session")
        self.mpu_info.configure(text="pas de session")
        self.sau_info.configure(text="pas de session")
        self.mpu_stop = self.sau_stop = -1

    # ------------------------------------------------------------------ CoreSight
    def _reread(self) -> None:
        if not self.stopped or self.app.trace is None:
            self.cs_info.configure(text="arrêter la cible pour relire les tables ROM")
            return
        self.app.safe(self.app.trace.discover, self.session)
        self.render_coresight()

    def render_coresight(self) -> None:
        t = self.cs_tree
        t.delete(*t.get_children())
        tm = self.app.trace
        comps = tm.components if tm is not None else []
        if self.session is None:
            self.cs_info.configure(text="pas de session")
            return
        if not comps:
            self.cs_info.configure(text="pas de table ROM CoreSight lisible (QEMU n'en a pas)")
            return
        tables = sum(1 for c in comps if c.kind == "ROM")
        etm = next((c for c in comps if c.kind == "ETM"), None)
        etm_txt = f"ETMv{etm.etm_version}" if etm is not None and etm.etm_version else (
            "ETM" if etm is not None else "pas d'ETM")
        self.cs_info.configure(text=f"{len(comps) - tables} composants, {tables} table(s) ROM ; {etm_txt}")
        parents: dict[int, str] = {}
        for c in comps:
            parent = ""
            if c.depth:                          # sous la dernière table de profondeur inférieure
                parent = parents.get(c.depth - 1, "")
            iid = t.insert(parent, "end", text=c.name, open=True,
                           values=(c.kind or "?", f"{c.part:#05x}", c.designer_name, c.revision,
                                   f"{c.addr:#010x}"), tags=() if c.kind else ("dim",))
            if c.kind == "ROM":
                parents[c.depth] = iid

    # ------------------------------------------------------------------ MPU
    def refresh_mpu(self, force: bool = False) -> None:
        if self.session is None:
            return
        if not self.stopped:
            self.mpu_info.configure(text="cible en marche : la MPU se relit à l'arrêt")
            return
        if getattr(self.app, "native", False):
            self.mpu_info.configure(text="programme de l'hôte : pas de MPU Cortex-M")
            return
        if not force and self.mpu_stop == self.session.stop_count:
            return
        api = self.app.api
        if api is None or not hasattr(api, "mpu"):
            return
        trustzone = getattr(self.app, "security", None) is not None
        if trustzone and not self.mpu_ns_check.winfo_ismapped():
            self.mpu_ns_check.pack(anchor="w", padx=4, before=self.mpu_tree.master)
        elif not trustzone and self.mpu_ns_check.winfo_ismapped():
            self.mpu_ns_check.pack_forget()
        nonsecure = trustzone and self.mpu_ns.get()
        state = self.app.safe(lambda: api.mpu(quiet=True, nonsecure=nonsecure))
        self.mpu_stop = self.session.stop_count
        self.render_mpu(state)

    def render_mpu(self, state) -> None:
        t = self.mpu_tree
        t.delete(*t.get_children())
        if state is None:
            return
        self.mpu_info.configure(text=state.summary())
        for r in state.regions:
            t.insert("", "end", text=str(r.number), tags=() if r.enabled else ("off",),
                     values=r.row())

    # ------------------------------------------------------------------ SAU
    def refresh_sau(self, force: bool = False) -> None:
        if self.session is None:
            return
        if not self.stopped:
            self.sau_info.configure(text="cible en marche : le SAU se relit à l'arrêt")
            return
        if getattr(self.app, "native", False) or getattr(self.app, "security", None) is None:
            self.sau_tree.delete(*self.sau_tree.get_children())
            self.sau_info.configure(text="pas d'extension de sécurité (TrustZone) sur ce cœur")
            return
        if not force and self.sau_stop == self.session.stop_count:
            return
        from ..core.security import session_sau
        state = self.app.safe(lambda: session_sau(self.session, self.app.security))
        self.sau_stop = self.session.stop_count
        self.render_sau(state)

    def render_sau(self, state) -> None:
        t = self.sau_tree
        t.delete(*t.get_children())
        if state is None:
            return
        sec = getattr(self.app, "security", None)
        self.sau_info.configure(text=state.summary() + (f"\ncœur : {sec.text()}" if sec else ""))
        for r in state.regions:
            t.insert("", "end", text=str(r.number), tags=() if r.enabled else ("off",), values=r.row())

    # ------------------------------------------------------------------ Puce
    def render_device(self) -> None:
        t = self.dev_tree
        t.delete(*t.get_children())
        api = self.app.api
        info = api.chip_rows() if api is not None and hasattr(api, "chip_rows") else None
        if not info:
            self.dev_info.configure(text="aucune puce décrite (--pack, --device)")
            return
        title, rows = info
        self.dev_info.configure(text=title)
        for section, items in rows:
            node = t.insert("", "end", text=section, open=True)
            for name, value, detail in items:
                t.insert(node, "end", text=name, values=(value, detail))

    def get_state(self) -> dict:
        return {"tab": self._tab()}

    def set_state(self, state: dict) -> None:
        tab = (state or {}).get("tab")
        for i in range(self.nb.index("end")):
            if self.nb.tab(i, "text") == tab:
                self.nb.select(i)
