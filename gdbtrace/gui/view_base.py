"""Vues détachables de GdbTrace.

Chaque vue est un `ttk.Frame`. Le `ViewManager` peut l'afficher dans un onglet d'un
`ttk.Notebook` ou la détacher dans un `tk.Toplevel` (fenêtre flottante). Comme un
widget tkinter ne peut pas changer de parent, détacher = détruire et recréer la vue
avec le nouveau parent ; l'état utile est transmis via `get_state()`/`set_state()`.
"""
from __future__ import annotations

import tkinter as tk
from tkinter import font as tkfont
from tkinter import ttk
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from .app import App


def mono_font() -> tkfont.Font:
    f = tkfont.nametofont("TkFixedFont").copy()
    f.configure(size=10)
    return f


class View(ttk.Frame):
    title = "Vue"
    #: identifiant stable pour la sauvegarde du layout
    kind = "view"

    def __init__(self, master: tk.Misc, app: "App", **kw: Any):
        super().__init__(master, **kw)
        self.app = app
        self.mono = mono_font()

    # ---- cycle de vie, à surcharger
    @property
    def trace_settling(self) -> bool:
        """La trace d'instructions change encore : cible en marche, ou trace reçue en cours de
        décodage (le décodage rattrape un SWO saturé). Les vues qui la dessinent en entier
        (liste, Timing, couverture) attendent : redessiner à chaque paquet gèle le GUI."""
        if self.session is not None and self.session.state == "running":
            return True
        trace = getattr(self.app, "trace", None)
        return trace is not None and trace.etm_pending()

    @property
    def shown(self) -> bool:
        """La vue est à l'écran (onglet sélectionné, fenêtre ouverte). Les rendus périodiques
        des vues cachées sont du temps GUI perdu : ils attendent `<Map>` (`redraw_when_shown`)."""
        try:
            return bool(self.winfo_ismapped())
        except tk.TclError:
            return False

    def redraw_when_shown(self, render: Callable[[], object]) -> None:
        """`render()` dès que la vue redevient visible (onglet choisi)."""
        self.bind("<Map>", lambda e: render() if e.widget is self else None, add="+")

    def refresh(self) -> None:
        """Relit l'état de la cible. Appelé dans le thread GUI quand la cible est arrêtée."""

    def on_running(self) -> None:
        """La cible repart."""

    def on_disconnected(self) -> None:
        """Session fermée."""

    def get_state(self) -> dict:
        return {}

    def set_state(self, state: dict) -> None:
        pass

    def destroy(self) -> None:
        self.app.views.forget(self)
        super().destroy()

    # ---- utilitaires
    @property
    def session(self):
        return self.app.session

    @property
    def connected(self) -> bool:
        return self.app.session is not None and self.app.session.state != "disconnected"

    @property
    def stopped(self) -> bool:
        return self.app.session is not None and self.app.session.state == "stopped"


class ViewManager:
    def __init__(self, app: "App"):
        self.app = app
        self.views: list[View] = []
        self.toplevels: dict[View, tk.Toplevel] = {}
        self.notebooks: dict[str, ttk.Notebook] = {}

    def register_notebook(self, name: str, nb: ttk.Notebook) -> None:
        self.notebooks[name] = nb
        nb.bind("<Button-3>", lambda e, n=nb: self._tab_menu(e, n))
        nb.bind("<Double-Button-1>", lambda e, n=nb: self._detach_at(e, n))

    # ---- création
    def open(self, cls: type[View], where: str | None = None, state: dict | None = None,
             geometry: str | None = None) -> View:
        """where = nom d'un notebook, ou None pour une fenêtre flottante."""
        if where in self.notebooks:
            nb = self.notebooks[where]
            view = cls(nb, self.app)
            nb.add(view, text=view.title)
            nb.select(view)
        else:
            top = tk.Toplevel(self.app.root)
            top.title(f"{cls.title} — gdbtrace")
            if geometry:
                top.geometry(geometry)
            view = cls(top, self.app)
            view.pack(fill="both", expand=True)
            self.toplevels[view] = top
            top.protocol("WM_DELETE_WINDOW", view.destroy)
            menu = tk.Menu(top, tearoff=False)
            for name in self.notebooks:
                menu.add_command(label=f"Ancrer dans « {name} »",
                                 command=lambda v=view, n=name: self.move(v, n))
            top.bind("<Button-3>", lambda e, m=menu: m.tk_popup(e.x_root, e.y_root))
        if state:
            view.set_state(state)
        self.views.append(view)
        if self.app.session and self.app.session.state == "stopped":
            self.app.safe(view.refresh)
        return view

    def forget(self, view: View) -> None:
        if view in self.views:
            self.views.remove(view)
        top = self.toplevels.pop(view, None)
        if top is not None:
            self.app.root.after_idle(top.destroy)

    def move(self, view: View, where: str | None) -> View:
        cls, state = type(view), view.get_state()
        view.destroy()
        return self.open(cls, where, state)

    def where(self, view: View) -> str | None:
        for name, nb in self.notebooks.items():
            if str(view) in nb.tabs():
                return name
        return None

    def find(self, cls: type[View]) -> View | None:
        return next((v for v in self.views if type(v) is cls), None)

    def show(self, cls: type[View], where: str | None = None) -> View:
        """Ouvre la vue si elle n'existe pas encore, sinon la met au premier plan."""
        v = self.find(cls)
        if v is None:
            return self.open(cls, where)
        name = self.where(v)
        if name:
            self.notebooks[name].select(v)
        elif v in self.toplevels:
            self.toplevels[v].deiconify()
            self.toplevels[v].lift()
        return v

    # ---- diffusion d'événements
    def refresh_all(self) -> None:
        for v in list(self.views):
            self.app.safe(v.refresh)

    def running_all(self) -> None:
        for v in list(self.views):
            self.app.safe(v.on_running)

    def disconnected_all(self) -> None:
        for v in list(self.views):
            self.app.safe(v.on_disconnected)

    # ---- layout
    def layout(self) -> list[dict]:
        out = []
        for v in self.views:
            entry = {"kind": v.kind, "where": self.where(v), "state": v.get_state()}
            if v in self.toplevels:
                entry["geometry"] = self.toplevels[v].geometry()
            out.append(entry)
        return out

    # ---- menus d'onglets
    def _tab_at(self, event, nb: ttk.Notebook) -> View | None:
        try:
            idx = nb.index(f"@{event.x},{event.y}")
        except tk.TclError:
            return None
        widget = self.app.root.nametowidget(nb.tabs()[idx])
        return widget if isinstance(widget, View) else None

    def _detach_at(self, event, nb: ttk.Notebook) -> None:
        v = self._tab_at(event, nb)
        if v is not None:
            self.move(v, None)

    def _tab_menu(self, event, nb: ttk.Notebook) -> None:
        v = self._tab_at(event, nb)
        if v is None:
            return
        m = tk.Menu(nb, tearoff=False)
        m.add_command(label="Détacher (fenêtre flottante)", command=lambda: self.move(v, None))
        for name in self.notebooks:
            if self.notebooks[name] is not nb:
                m.add_command(label=f"Déplacer vers « {name} »", command=lambda n=name: self.move(v, n))
        m.add_separator()
        m.add_command(label="Fermer", command=v.destroy)
        m.tk_popup(event.x_root, event.y_root)
