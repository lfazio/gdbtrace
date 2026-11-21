"""Break.List, Var.Watch, Frame.view (pile + locales)."""
from __future__ import annotations

import tkinter as tk
from tkinter import simpledialog, ttk

from ..core.live_vars import LeafLayout, compute_layout, decode
from ..gui.view_base import View


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
        ttk.Button(bar, text="Supprimer", command=self._delete).pack(side="left", padx=2)
        body = ttk.Frame(self)
        body.pack(fill="both", expand=True)
        self.tree = _tree(body, [("num", "#", 40), ("type", "Type", 110), ("addr", "Adresse", 90),
                                 ("hits", "Hits", 50), ("loc", "Emplacement", 260)])
        self.tree.bind("<Double-1>", self._goto)

    def refresh(self) -> None:
        if not self.connected:
            return
        self.tree.delete(*self.tree.get_children())
        for bp in self.app.cached_breakpoints:
            loc = f"{bp.func or ''} {bp.file or ''}:{bp.line}" if bp.line else (bp.func or "")
            self.tree.insert("", "end", iid=bp.number, values=(bp.number, bp.type, bp.addr or "",
                                                               bp.hits, loc.strip()),
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
        self.exprs: list[str] = []          # expressions racines (persistées)
        self.roots: dict[str, str] = {}     # expression racine -> item de l'arbre
        self.tree.tag_configure("error", foreground="#b00020")
        self.varobj: dict[str, str] = {}    # item de l'arbre -> nom du varobj gdb
        self.item_of: dict[str, str] = {}   # varobj -> item
        self.numchild: dict[str, int] = {}  # item -> nombre d'enfants
        self.layouts: dict[str, LeafLayout] = {}  # feuille -> disposition mémoire (live)

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
        return item

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
                self.tree.set(item, "value", ch.get("value", ""))
                self.tree.item(item, tags=("changed",))
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

    def _on_live(self, data: dict[int, bytes]) -> None:
        for it, lay in self.layouts.items():
            if not lay.live or lay.addr not in data or not self.tree.exists(it):
                continue
            val = decode(lay, data[lay.addr])
            if val != self.tree.set(it, "value"):
                self.tree.set(it, "value", val)
                self.tree.item(it, tags=("changed",))
            else:
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

    def destroy(self) -> None:
        if self.app.poller:
            self.app.poller.unsubscribe(self)
        super().destroy()

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
        name = self.varobj.get(item)
        for it in self._subtree(item):  # gdb supprime aussi les varobjs enfants
            vo = self.varobj.pop(it, None)
            self.item_of.pop(vo, None)
            self.numchild.pop(it, None)
            self.layouts.pop(it, None)
        self.tree.delete(item)
        if name and self.stopped:
            self.app.safe(self.session.var_delete, name)
        self._update_subscription()

    def _edit(self, _e) -> str | None:
        item = self.tree.focus()
        name = self.varobj.get(item)
        if not name or not self.stopped:
            return None
        val = simpledialog.askstring("Modifier", f"{self.tree.item(item, 'text')} =",
                                     initialvalue=self.tree.set(item, "value"), parent=self)
        if val is not None:
            self.app.safe(self.session.var_assign, name, val)
            self.app.refresh_all()
        return "break"

    def get_state(self) -> dict:
        return {"exprs": list(self.exprs)}

    def set_state(self, state: dict) -> None:
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
        self.bt = []
        self.prev_locals: dict[str, str] = {}

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
            self.locals.insert("", "end", values=(name, val), tags=tags)
        self.prev_locals = cur

    def _goto(self, _e) -> None:
        sel = self.frames.focus()
        if sel and int(sel) < len(self.bt):
            f = self.bt[int(sel)]
            if f.fullname and f.line:
                self.app.show_source(f.fullname, f.line)
