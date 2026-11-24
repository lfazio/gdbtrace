"""Break.List, Var.Watch, Frame.view (pile + locales).

Clic droit sur une variable (Watch, locales) : format d'affichage de sa valeur (Naturel,
Décimal, Hexadécimal, Octal, Binaire, ASCII), gardé en marche et dans le layout.
Options d'un breakpoint (condition, arrêt au Nᵉ passage, message à chaque passage) : bouton
Options… ou clic droit de la vue Breakpoints.
"""
from __future__ import annotations

import tkinter as tk
from tkinter import messagebox, simpledialog, ttk

from ..core.formats import FORMATS, GDB_FORMAT, from_zero_hex, normalize
from ..core.live_vars import LeafLayout, compute_layout, decode
from ..gui.view_base import View


class FormatMenu:
    """Menu contextuel « Format d'affichage » d'une vue : un bouton radio par format. Une seule
    variable tkinter par vue (créée dans le thread GUI), un seul menu affiché à la fois."""

    def __init__(self, view: View):
        self.view = view
        self.var = tk.StringVar(view, "natural")
        self.menu: tk.Menu | None = None

    def build(self, current: str, choose) -> tk.Menu:
        if self.menu is not None:
            self.menu.destroy()
        self.var.set(current)
        m = self.menu = tk.Menu(self.view, tearoff=False)
        m.add_command(label="Format d'affichage", state="disabled")
        for key, label in FORMATS.items():
            m.add_radiobutton(label=label, value=key, variable=self.var,
                              command=lambda k=key: choose(k))
        return m


def _tree(master, columns: list[tuple[str, str, int]], tree_col: tuple[str, int] | None = None):
    show = "tree headings" if tree_col else "headings"
    tv = ttk.Treeview(master, columns=[c[0] for c in columns], show=show, selectmode="browse")
    if tree_col:
        tv.heading("#0", text=tree_col[0])
        tv.column("#0", width=tree_col[1])
    for cid, label, width in columns:
        tv.heading(cid, text=label)
        tv.column(cid, width=width, stretch=cid == columns[-1][0])
    tv.tag_configure("changed", foreground="#d00000")
    tv.tag_configure("disabled", foreground="#999999")
    sb = ttk.Scrollbar(master, orient="vertical", command=tv.yview)
    tv.configure(yscrollcommand=sb.set)
    tv.pack(side="left", fill="both", expand=True)
    sb.pack(side="right", fill="y")
    return tv


def options_text(bp) -> str:
    """Colonne « Condition / action » : si COND · au Nᵉ passage (reste k) · action."""
    parts = [f"si {bp.condition}"] if bp.condition else []
    if bp.count:
        parts.append(f"au {bp.count}ᵉ passage" + (f" (reste {bp.ignore})" if bp.ignore else ""))
    elif bp.ignore:
        parts.append(f"{bp.ignore} passage(s) ignoré(s)")
    if bp.action:
        parts.append(bp.action)
    return " · ".join(parts)


class BreakOptionsDialog(simpledialog.Dialog):
    """Options d'un breakpoint ou d'un watchpoint. `values` : dict des options modifiées
    (condition, count, log, stop), à passer à Session.break_options(), ou None si annulé."""

    def __init__(self, parent, bp, action):
        self.values = None
        self.bp, self.act = bp, action
        super().__init__(parent, f"Options du breakpoint {bp.number}")

    def body(self, master):
        bp, act = self.bp, self.act
        self.init = {"condition": bp.condition or "", "count": bp.count or 1,
                     "log": (act.log if act else None) or "", "stop": bool(act and act.stop)}
        self.cond_var = tk.StringVar(value=self.init["condition"])
        self.count_var = tk.StringVar(value=str(self.init["count"]))
        self.log_var = tk.StringVar(value=self.init["log"])
        self.stop_var = tk.BooleanVar(value=self.init["stop"])
        rows = [("Condition C :", self.cond_var, "s'arrêter seulement si elle est vraie (vide : toujours)"),
                ("Arrêt au passage n° :", self.count_var, "1 : à chaque passage ; compté à partir de maintenant"),
                ("Message :", self.log_var, "à chaque passage, puis la cible repart : n={counter} t={tick:hex}")]
        first = None
        for r, (label, var, hint) in enumerate(rows):
            ttk.Label(master, text=label).grid(row=2 * r, column=0, sticky="w")
            e = ttk.Entry(master, textvariable=var, width=40)
            e.grid(row=2 * r, column=1, padx=4, pady=(4, 0), sticky="w")
            ttk.Label(master, text=hint, foreground="#555555").grid(row=2 * r + 1, column=1, sticky="w", padx=4)
            first = first or e
        ttk.Checkbutton(master, text="S'arrêter après le message", variable=self.stop_var).grid(
            row=6, column=1, sticky="w", padx=4, pady=(4, 0))
        if act is not None and act.fn is not None:
            ttk.Label(master, text="Action Python posée par un script : conservée.",
                      foreground="#555555").grid(row=7, column=0, columnspan=2, sticky="w", pady=(6, 0))
        return first

    def validate(self) -> bool:
        try:
            count = int(self.count_var.get() or "1", 0)
        except ValueError:
            count = 0
        if count < 1:
            messagebox.showerror(self.title(), "Numéro de passage : entier ≥ 1", parent=self)
            return False
        new = {"condition": self.cond_var.get().strip(), "count": count,
               "log": self.log_var.get().strip(), "stop": self.stop_var.get()}
        self.values = {k: v for k, v in new.items() if v != self.init[k]}
        return True


class BreakpointsView(View):
    title = "Breakpoints"
    kind = "breakpoints"

    def __init__(self, master, app, **kw):
        super().__init__(master, app, **kw)
        bar = ttk.Frame(self)
        bar.pack(fill="x")
        self.loc = tk.StringVar()
        e = ttk.Entry(bar, textvariable=self.loc, width=30)
        e.pack(side="left", padx=2, pady=1)
        e.bind("<Return>", lambda _e: self._add())
        self.hw = tk.BooleanVar(value=False)
        ttk.Checkbutton(bar, text="Matériel", variable=self.hw).pack(side="left")
        ttk.Button(bar, text="Ajouter", command=self._add).pack(side="left", padx=2)
        ttk.Button(bar, text="Watchpoint", command=self._add_watch).pack(side="left", padx=2)
        ttk.Button(bar, text="On/Off", command=self._toggle).pack(side="left", padx=2)
        ttk.Button(bar, text="Options…", command=self._edit_options).pack(side="left", padx=2)
        ttk.Button(bar, text="Supprimer", command=self._delete).pack(side="left", padx=2)
        body = ttk.Frame(self)
        body.pack(fill="both", expand=True)
        self.tree = _tree(body, [("num", "#", 40), ("type", "Type", 110), ("addr", "Adresse", 100),
                                 ("hits", "Hits", 50), ("loc", "Emplacement", 220),
                                 ("opts", "Condition / action", 260)])
        self.tree.bind("<Double-1>", self._goto)
        self.tree.bind("<Button-3>", self._menu)
        self.menu: tk.Menu | None = None

    def refresh(self) -> None:
        if not self.connected:
            return
        self.tree.delete(*self.tree.get_children())
        for bp in self.app.cached_breakpoints:
            loc = f"{bp.func or ''} {bp.file or ''}:{bp.line}" if bp.line else (bp.func or bp.expr or "")
            self.tree.insert("", "end", iid=bp.number, values=(bp.number, bp.type, bp.addr or "",
                                                               bp.hits, loc.strip(), options_text(bp)),
                             tags=() if bp.enabled else ("disabled",))

    def on_running(self) -> None:
        pass

    def _add(self) -> None:
        loc = self.loc.get().strip()
        if loc:
            self.app.run_action(lambda: self.session.break_(loc, hw=self.hw.get()),
                                then=lambda _r: self.app.breakpoints_changed())
            self.loc.set("")

    def _add_watch(self) -> None:
        expr = self.loc.get().strip() or simpledialog.askstring("Watchpoint", "Expression :", parent=self)
        if expr:
            self.app.run_action(lambda: self.session.watch(expr),
                                then=lambda _r: self.app.breakpoints_changed())
            self.loc.set("")

    def _toggle(self) -> None:
        num = self.tree.focus()
        bp = next((b for b in self.app.cached_breakpoints if b.number == num), None)
        if bp:
            self.app.run_action(lambda: self.session.enable_break(num, not bp.enabled),
                                then=lambda _r: self.app.breakpoints_changed())

    def _menu(self, event) -> None:
        item = self.tree.identify_row(event.y)
        if not item:
            return
        self.tree.focus(item)
        self.tree.selection_set(item)
        if self.menu is not None:
            self.menu.destroy()
        m = self.menu = tk.Menu(self, tearoff=False)
        m.add_command(label="Options…", command=self._edit_options)
        m.add_command(label="Activer / désactiver", command=self._toggle)
        m.add_command(label="Supprimer", command=self._delete)
        m.tk_popup(event.x_root, event.y_root)

    def _edit_options(self) -> None:
        bp = next((b for b in self.app.cached_breakpoints if b.number == self.tree.focus()), None)
        if bp is None:
            return
        d = BreakOptionsDialog(self, bp, self.session.break_action(bp.number))
        if d.values:
            self.apply_options(bp.number, **d.values)

    def apply_options(self, number: str, **options) -> None:
        """Condition, arrêt au Nᵉ passage, message (`log`), arrêt après le message (`stop`)."""
        self.app.run_action(lambda: self.session.break_options(number, **options),
                            then=lambda _r: self.app.breakpoints_changed())

    def _delete(self) -> None:
        num = self.tree.focus()
        if num:
            self.app.run_action(lambda: self.session.delete_break(num),
                                then=lambda _r: self.app.breakpoints_changed())

    def _goto(self, _e) -> None:
        bp = next((b for b in self.app.cached_breakpoints if b.number == self.tree.focus()), None)
        if bp and bp.file and bp.line:
            self.app.show_source(bp.file, bp.line)


class WatchView(View):
    """Var.Watch : expressions C surveillées, structures dépliables, édition par double-clic."""
    title = "Watch"
    kind = "watch"
    PLACEHOLDER = "__loading__"

    def __init__(self, master, app, **kw):
        super().__init__(master, app, **kw)
        bar = ttk.Frame(self)
        bar.pack(fill="x")
        self.expr = tk.StringVar()
        e = ttk.Entry(bar, textvariable=self.expr, width=36)
        e.pack(side="left", padx=2, pady=1)
        e.bind("<Return>", lambda _e: self.add(self.expr.get()))
        ttk.Button(bar, text="Ajouter", command=lambda: self.add(self.expr.get())).pack(side="left")
        ttk.Button(bar, text="Retirer", command=self._remove).pack(side="left", padx=2)
        self.live_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(bar, text="Live", variable=self.live_var,
                        command=self._update_subscription).pack(side="left", padx=6)
        body = ttk.Frame(self)
        body.pack(fill="both", expand=True)
        self.tree = _tree(body, [("value", "Valeur", 200), ("type", "Type", 140)],
                          tree_col=("Expression", 180))
        self.tree.bind("<<TreeviewOpen>>", self._expand)
        self.tree.bind("<Double-1>", self._edit)
        self.tree.bind("<Button-3>", self._menu)
        self.exprs: list[str] = []          # expressions racines (persistées)
        self.roots: dict[str, str] = {}     # expression racine -> item de l'arbre
        self.tree.tag_configure("error", foreground="#b00020")
        self.varobj: dict[str, str] = {}    # item de l'arbre -> nom du varobj gdb
        self.item_of: dict[str, str] = {}   # varobj -> item
        self.numchild: dict[str, int] = {}  # item -> nombre d'enfants
        self.layouts: dict[str, LeafLayout] = {}  # feuille -> disposition mémoire (live)
        # formats d'affichage : chemin (libellés depuis la racine, séparés par \t) -> format,
        # hérité par les membres (persisté) ; format posé sur chaque varobj ; dernière valeur
        # `zero-hexadecimal` (binaire, ASCII) ; derniers octets lus en marche
        self.formats: dict[str, str] = {}
        self.gdb_fmt: dict[str, str] = {}
        self.hex_value: dict[str, str] = {}
        self.last_live: dict[int, bytes] = {}
        self.format_menu = FormatMenu(self)

    def add(self, expr: str) -> None:
        expr = expr.strip()
        if not expr:
            return
        self.expr.set("")
        if expr in self.exprs and expr in self.roots and self.tree.exists(self.roots[expr]):
            self.tree.selection_set(self.roots[expr])
            self.tree.see(self.roots[expr])
            return
        if expr not in self.exprs:
            self.exprs.append(expr)
        if self.stopped:
            self._ensure_roots()
            self.app.safe(self._compute_layouts)  # suivie en direct dès le prochain Go
        else:
            # cible en marche (ou pas encore connectée) : gdb ne peut pas évaluer maintenant
            self._set_root_row(expr, "<évaluée au prochain arrêt>", "", ("disabled",))

    def _set_root_row(self, expr: str, value: str, typ: str, tags: tuple) -> str:
        item = self.roots.get(expr)
        if item and self.tree.exists(item):
            self.tree.item(item, values=(value, typ), tags=tags)
        else:
            item = self.tree.insert("", self.exprs.index(expr), text=expr, values=(value, typ), tags=tags)
            self.roots[expr] = item
        return item

    def _ensure_roots(self) -> None:
        """Cible arrêtée : crée le varobj de chaque expression qui n'en a pas encore (ajoutée
        en marche, ou invalide jusqu'ici : locale hors de portée, symbole absent...)."""
        for expr in self.exprs:
            item = self.roots.get(expr)
            if item and item in self.varobj and self.tree.exists(item):
                continue
            try:
                v = self.session.var_create(expr)
            except Exception as exc:  # noqa: BLE001 - message de gdb affiché dans la ligne
                msg = str(exc)
                if "unable to create variable object" in msg:
                    msg = "inconnue ici (locale hors de portée ?) — réévaluée à chaque arrêt"
                else:
                    msg = msg.rsplit(": ", 1)[-1]
                self._set_root_row(expr, f"<{msg}>", "", ("error",))
                continue
            index = self.exprs.index(expr)
            if item and self.tree.exists(item):
                index = self.tree.index(item)
                self.tree.delete(item)
            self.roots[expr] = self._insert("", expr, v, index)

    def _insert(self, parent: str, label: str, v: dict, index: int | str = "end") -> str:
        item = self.tree.insert(parent, index, text=label, values=(v.get("value", ""), v.get("type", "")))
        self.varobj[item] = v["name"]
        self.item_of[v["name"]] = item
        self.numchild[item] = int(v.get("numchild", 0))
        if self.numchild[item] > 0:
            self.tree.insert(item, "end", iid=f"{item}{self.PLACEHOLDER}", text="…")
        if self.format_of(item) != "natural":      # varobj neuf : format naturel chez gdb
            self.app.safe(self._apply_format, item)
        return item

    # ------------------------------------------------------------------ formats d'affichage
    def _path(self, item: str) -> str:
        labels = []
        while item:
            labels.append(self.tree.item(item, "text"))
            item = self.tree.parent(item)
        return "\t".join(reversed(labels))

    def format_of(self, item: str) -> str:
        """Format effectif : celui de la ligne, sinon de l'ancêtre le plus proche, sinon naturel."""
        path = self._path(item)
        while True:
            if path in self.formats:
                return self.formats[path]
            if "\t" not in path:
                return "natural"
            path = path.rsplit("\t", 1)[0]

    def set_format(self, item: str, fmt: str) -> None:
        """Format d'affichage de la ligne et de tous ses membres (structure, tableau). Cible
        arrêtée : appliqué par gdb tout de suite ; en marche : aux valeurs suivies en direct,
        les autres à l'arrêt suivant."""
        fmt = normalize(fmt)
        path = self._path(item)
        for p in [p for p in self.formats if p == path or p.startswith(path + "\t")]:
            del self.formats[p]
        parent = self.tree.parent(item)
        if fmt != (self.format_of(parent) if parent else "natural"):
            self.formats[path] = fmt
        if self.stopped:
            for it in self._subtree(item):
                if it in self.varobj:
                    self.app.safe(self._apply_format, it)
        elif self.last_live:
            self._on_live(dict(self.last_live), mark=False)

    def _apply_format(self, item: str) -> None:
        """Cible arrêtée : pose le format sur le varobj de la ligne et affiche sa valeur."""
        gfmt = GDB_FORMAT[self.format_of(item)]
        if self.gdb_fmt.get(item, "natural") != gfmt:
            value = self.session.var_set_format(self.varobj[item], gfmt)
            self.gdb_fmt[item] = gfmt
        elif gfmt == "zero-hexadecimal" and item in self.hex_value:
            value = self.hex_value[item]             # binaire ↔ ASCII : même valeur gdb
        else:
            return
        self._show(item, value)

    def _show(self, item: str, value: str) -> None:
        """Valeur de gdb (au format posé sur le varobj) → texte affiché."""
        fmt = self.format_of(item)
        if fmt in ("binary", "ascii") and self.gdb_fmt.get(item) == "zero-hexadecimal":
            self.hex_value[item] = value
            value = from_zero_hex(value, fmt) or value
        self.tree.set(item, "value", value)

    def _menu(self, event) -> None:
        item = self.tree.identify_row(event.y)
        if not item or item.endswith(self.PLACEHOLDER):
            return
        self.tree.focus(item)
        self.tree.selection_set(item)
        self._format_menu(item).tk_popup(event.x_root, event.y_root)

    def _format_menu(self, item: str) -> tk.Menu:
        return self.format_menu.build(self.format_of(item), lambda f: self.set_format(item, f))

    def _expand(self, _e) -> None:
        item = self.tree.focus()
        kids = self.tree.get_children(item)
        if len(kids) == 1 and kids[0].endswith(self.PLACEHOLDER) and self.stopped:
            self.tree.delete(kids[0])
            for c in self.session.var_children(self.varobj[item]):
                label = c.get("exp", c["name"])
                if label.isdigit():
                    label = f"[{label}]"
                self._insert(item, label, c)
            self.app.safe(self._compute_layouts)

    def refresh(self) -> None:
        if not self.stopped:
            return
        for item in self.varobj:
            if self.tree.exists(item):
                self.tree.item(item, tags=())
        for ch in self.session.var_update():
            item = self.item_of.get(ch.get("name"))
            if item and self.tree.exists(item) and ch.get("in_scope", "true") == "true":
                self._show(item, ch.get("value", ""))       # au format posé sur le varobj
                self.tree.item(item, tags=("changed",))
        # formats choisis cible en marche : appliqués maintenant (valeur inchangée comprise)
        for item in list(self.varobj):
            if self.tree.exists(item) and (self.gdb_fmt.get(item, "natural") != "natural"
                                           or self.format_of(item) != "natural"):
                self.app.safe(self._apply_format, item)
        self._ensure_roots()
        self.app.safe(self._compute_layouts)

    # ------------------------------------------------------------------ live
    def _compute_layouts(self) -> None:
        """Cible arrêtée : adresse/taille/type des feuilles, pour le décodage en marche."""
        leaves = [it for it, n in self.numchild.items()
                  if n == 0 and it not in self.layouts and self.tree.exists(it)]
        if not leaves:
            return
        try:
            local_names = {v.get("name") for v in self.session.locals()}
        except Exception:  # noqa: BLE001 - pas de frame : aucune locale
            local_names = set()
        for it in leaves:
            self.layouts[it] = compute_layout(self.session, self.varobj[it], local_names)

    def _update_subscription(self) -> None:
        poller = self.app.poller
        if poller is None:
            return
        ranges = [(lay.addr, lay.size) for lay in self.layouts.values() if lay.live]
        if self.live_var.get() and ranges:
            poller.subscribe(self, ranges, self._on_live)
        else:
            poller.unsubscribe(self)

    def on_running(self) -> None:
        live = self.live_var.get() and self.app.poller is not None
        for it, lay in self.layouts.items():
            if self.tree.exists(it):
                # valeur gelée (grisée) si elle ne peut pas être suivie en marche
                self.tree.item(it, tags=() if (live and lay.live) else ("disabled",))
        self._update_subscription()

    def _on_live(self, data: dict[int, bytes], mark: bool = True) -> None:
        """Octets lus en marche → valeurs, au format de chaque ligne. `mark=False` : simple
        changement de format (pas de surlignage)."""
        self.last_live.update(data)
        for it, lay in self.layouts.items():
            if not lay.live or lay.addr not in data or not self.tree.exists(it):
                continue
            val = decode(lay, data[lay.addr], self.format_of(it))
            if val != self.tree.set(it, "value"):
                self.tree.set(it, "value", val)
                if mark:
                    self.tree.item(it, tags=("changed",))
            elif mark:
                self.tree.item(it, tags=())

    def on_disconnected(self) -> None:
        if self.app.poller:
            self.app.poller.unsubscribe(self)
        self.tree.delete(*self.tree.get_children())
        self.roots.clear()
        self.varobj.clear()
        self.item_of.clear()
        self.numchild.clear()
        self.layouts.clear()
        self.gdb_fmt.clear()                # varobjs disparus ; les formats (par chemin) restent
        self.hex_value.clear()
        self.last_live.clear()

    def destroy(self) -> None:
        if self.app.poller:
            self.app.poller.unsubscribe(self)
        super().destroy()

    def on_program_changed(self) -> None:
        """Nouveau programme : les varobjs (types, adresses) sont recréés au prochain arrêt."""
        for item, name in list(self.varobj.items()):
            if not self.tree.parent(item) and self.stopped:
                try:
                    self.session.var_delete(name)
                except Exception:  # noqa: BLE001 - varobj déjà invalide
                    pass
        exprs = list(self.exprs)
        self.on_disconnected()
        self.exprs = exprs
        for e in exprs:
            self._set_root_row(e, "<évaluée au prochain arrêt>", "", ("disabled",))

    def _subtree(self, item: str) -> list[str]:
        out = [item]
        for c in self.tree.get_children(item):
            out += self._subtree(c)
        return out

    def _remove(self) -> None:
        item = self.tree.focus()
        if not item or self.tree.parent(item):
            return
        label = self.tree.item(item, "text")
        if label in self.exprs:
            self.exprs.remove(label)
        self.roots.pop(label, None)
        for p in [p for p in self.formats if p == label or p.startswith(label + "\t")]:
            del self.formats[p]
        name = self.varobj.get(item)
        for it in self._subtree(item):  # gdb supprime aussi les varobjs enfants
            vo = self.varobj.pop(it, None)
            self.item_of.pop(vo, None)
            self.numchild.pop(it, None)
            self.layouts.pop(it, None)
            self.gdb_fmt.pop(it, None)
            self.hex_value.pop(it, None)
        self.tree.delete(item)
        if name and self.stopped:
            self.app.safe(self.session.var_delete, name)
        self._update_subscription()

    def _edit(self, _e) -> str | None:
        item = self.tree.focus()
        name = self.varobj.get(item)
        if not name or not self.stopped:
            return None
        current = self.tree.set(item, "value")
        if self.format_of(item) in ("binary", "ascii"):     # valeur que gdb sait relire
            current = self.hex_value.get(item, current)
        val = simpledialog.askstring("Modifier", f"{self.tree.item(item, 'text')} =",
                                     initialvalue=current, parent=self)
        if val is not None:
            self.app.safe(self.session.var_assign, name, val)
            self.app.refresh_all()
        return "break"

    def get_state(self) -> dict:
        return {"exprs": list(self.exprs), "formats": dict(self.formats)}

    def set_state(self, state: dict) -> None:
        self.formats.update(state.get("formats", {}))      # avant les racines : appliqués à leur création
        for e in state.get("exprs", []):
            self.add(e)


class StackView(View):
    """Frame.view : pile d'appels et variables locales de la frame courante."""
    title = "Pile"
    kind = "stack"

    def __init__(self, master, app, **kw):
        super().__init__(master, app, **kw)
        pw = ttk.PanedWindow(self, orient="vertical")
        pw.pack(fill="both", expand=True)
        top, bottom = ttk.Frame(pw), ttk.Frame(pw)
        pw.add(top, weight=1)
        pw.add(bottom, weight=1)
        self.frames = _tree(top, [("lvl", "#", 30), ("pc", "PC", 80), ("func", "Fonction", 120),
                                  ("loc", "Source", 200)])
        self.frames.bind("<Double-1>", self._goto)
        self.locals = _tree(bottom, [("name", "Locale", 120), ("value", "Valeur", 200)])
        self.locals.bind("<Button-3>", self._menu)
        self.bt = []
        self.prev_locals: dict[str, str] = {}   # valeurs naturelles (détection des changements)
        #: formats d'affichage des locales : « fonction/nom » -> format (persisté)
        self.formats: dict[str, str] = {}
        self.format_menu = FormatMenu(self)

    def _key(self, name: str) -> str:
        func = self.bt[0].func if self.bt else ""
        return f"{func or '??'}/{name}"

    def _value(self, name: str, natural: str) -> str:
        fmt = self.formats.get(self._key(name), "natural")
        if fmt == "natural":
            return natural
        try:
            return self.session.format_value(name, fmt)
        except Exception:  # noqa: BLE001 - valeur indisponible dans ce format : naturelle
            return natural

    def refresh(self) -> None:
        if not self.stopped:
            return
        self.bt = self.session.backtrace()
        self.frames.delete(*self.frames.get_children())
        for i, f in enumerate(self.bt):
            loc = f"{f.file}:{f.line}" if f.file else ""
            self.frames.insert("", "end", iid=str(i), values=(i, f"{f.pc:08x}", f.func or "??", loc))
        self.locals.delete(*self.locals.get_children())
        cur = {}
        for v in self.session.locals():
            name, val = v.get("name", "?"), v.get("value", "")
            cur[name] = val
            tags = ("changed",) if name in self.prev_locals and self.prev_locals[name] != val else ()
            self.locals.insert("", "end", values=(name, self._value(name, val)), tags=tags)
        self.prev_locals = cur

    def _menu(self, event) -> None:
        item = self.locals.identify_row(event.y)
        if not item:
            return
        self.locals.focus(item)
        self.locals.selection_set(item)
        self._format_menu(item).tk_popup(event.x_root, event.y_root)

    def _format_menu(self, item: str) -> tk.Menu:
        name = self.locals.set(item, "name")
        return self.format_menu.build(self.formats.get(self._key(name), "natural"),
                                      lambda f: self.set_format(name, f))

    def set_format(self, name: str, fmt: str) -> None:
        """Format d'affichage d'une locale de la fonction courante (cible arrêtée : tout de suite)."""
        fmt = normalize(fmt)
        if fmt == "natural":
            self.formats.pop(self._key(name), None)
        else:
            self.formats[self._key(name)] = fmt
        if not self.stopped:
            return
        for item in self.locals.get_children():
            if self.locals.set(item, "name") == name:
                self.locals.set(item, "value", self._value(name, self.prev_locals.get(name, "")))

    def get_state(self) -> dict:
        return {"formats": dict(self.formats)}

    def set_state(self, state: dict) -> None:
        self.formats.update(state.get("formats", {}))

    def _goto(self, _e) -> None:
        sel = self.frames.focus()
        if sel and int(sel) < len(self.bt):
            f = self.bt[int(sel)]
            if f.fullname and f.line:
                self.app.show_source(f.fullname, f.line)
