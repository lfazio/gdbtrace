"""sYmbol.Browse : arborescence des fichiers sources du programme et de leurs fonctions.

Les fichiers viennent de la table des lignes DWARF de l'ELF (ceux qui contiennent du code).
Les fichiers du projet sont présentés par dossier à partir de leur racine commune, les autres
(bibliothèques de la chaîne de compilation...) sous « Externes ». Chaque fichier se déplie
sur ses fonctions. Double-clic ou Entrée : la source s'ouvre (à la fonction). Le fichier où
se trouve le PC est en gras.
"""
from __future__ import annotations

import os
import tkinter as tk
from tkinter import font as tkfont
from tkinter import ttk

from ..gui.view_base import View

EXTERNAL = "Externes"


def build_tree(files: list[str], root_hint: str | None = None) -> tuple[str, dict]:
    """Arbre {nom: sous-arbre | chemin du fichier} des fichiers, à partir de leur racine commune
    (les fichiers hors de `root_hint` vont sous « Externes ») ; les dossiers à enfant unique
    sont fusionnés (`portable/GCC/ARM_CM3`)."""
    inside = [f for f in files if root_hint and f.startswith(root_hint.rstrip("/") + "/")]
    outside = [f for f in files if f not in inside]
    if not inside:
        inside, outside = files, []
    root = os.path.commonpath(inside) if len(inside) > 1 else os.path.dirname(inside[0]) if inside else ""
    if inside and os.path.isfile(root):
        root = os.path.dirname(root)
    tree: dict = {}
    for f in inside:
        node = tree
        parts = os.path.relpath(f, root).split(os.sep)
        for d in parts[:-1]:
            node = node.setdefault(d + "/", {})
        node[parts[-1]] = f
    if outside:
        ext = tree.setdefault(EXTERNAL, {})
        for f in outside:
            ext.setdefault(os.path.dirname(f) + "/", {})[os.path.basename(f)] = f
    return root, _merge(tree)


def _merge(node: dict) -> dict:
    out = {}
    for name, sub in node.items():
        if isinstance(sub, dict):
            sub = _merge(sub)
            while name != EXTERNAL and len(sub) == 1:
                (k, v), = sub.items()
                if not isinstance(v, dict):
                    break
                name, sub = name + k, v
        out[name] = sub
    return out


class FilesView(View):
    title = "Fichiers"
    kind = "files"

    def __init__(self, master, app, **kw):
        super().__init__(master, app, **kw)
        bar = ttk.Frame(self)
        bar.pack(fill="x")
        ttk.Label(bar, text="Filtre :").pack(side="left", padx=(2, 0))
        self.filter_var = tk.StringVar()
        e = ttk.Entry(bar, textvariable=self.filter_var, width=18)
        e.pack(side="left", fill="x", expand=True, padx=2, pady=1)
        self.filter_var.trace_add("write", lambda *_a: self.populate())
        e.bind("<Return>", lambda _e: self._open_first())
        body = ttk.Frame(self)
        body.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(body, show="tree", selectmode="browse")
        self.tree.column("#0", width=220, stretch=True)
        sb = ttk.Scrollbar(body, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        bold = tkfont.nametofont("TkDefaultFont").copy()
        bold.configure(weight="bold")
        self._bold = bold
        self.tree.tag_configure("current", font=bold)
        self.tree.tag_configure("dir", foreground="#52514e")
        self.tree.tag_configure("func", foreground="#3050a0")
        self.tree.tag_configure("missing", foreground="#999999")
        self.tree.bind("<Double-1>", self._activate)
        self.tree.bind("<Return>", self._activate)
        self.tree.bind("<<TreeviewOpen>>", self._expand)
        self.target: dict[str, tuple[str, int]] = {}   # item -> (fichier, ligne)
        self.file_item: dict[str, str] = {}            # fichier -> item
        self._funcs: dict[str, list[tuple[int, str]]] | None = None
        self._current: str | None = None
        self._symbols = None
        self.populate()

    # ------------------------------------------------------------------ contenu
    def _functions_by_file(self) -> dict[str, list[tuple[int, str]]]:
        if self._funcs is None:
            sym = self.app.symbols
            out: dict[str, list[tuple[int, str]]] = {}
            for f in sym.functions:
                le = sym.line_at(f.start)
                if le is not None and f.size:
                    out.setdefault(le.file, []).append((le.line, f.name))
            for v in out.values():
                v.sort()
            self._funcs = out
        return self._funcs

    def populate(self) -> None:
        sym = self.app.symbols
        if sym is not self._symbols:
            self._symbols, self._funcs = sym, None
        self.tree.delete(*self.tree.get_children())
        self.target.clear()
        self.file_item.clear()
        if sym is None:
            self.tree.insert("", "end", text="(pas de programme chargé)", tags=("missing",))
            return
        files = sym.source_files()
        flt = self.filter_var.get().strip().lower()
        funcs = self._functions_by_file()
        if flt:   # fichiers dont le nom, ou une fonction, correspond
            files = [f for f in files if flt in os.path.basename(f).lower()
                     or any(flt in n.lower() for _l, n in funcs.get(f, []))]
        if not files:
            self.tree.insert("", "end", text="(aucun fichier)", tags=("missing",))
            return
        elf = self.session.elf if self.session else None
        hint = os.path.dirname(os.path.dirname(os.path.abspath(elf))) if elf else None
        _root, tree = build_tree(files, hint)
        self._insert_node("", tree, open_all=bool(flt), flt=flt)
        self._mark_current()

    def _insert_node(self, parent: str, node: dict, open_all: bool, flt: str) -> None:
        dirs = sorted((k for k, v in node.items() if isinstance(v, dict) and k != EXTERNAL), key=str.lower)
        files = sorted((k for k, v in node.items() if not isinstance(v, dict)), key=str.lower)
        for name in dirs:
            item = self.tree.insert(parent, "end", text=name, tags=("dir",), open=open_all or parent == "")
            self._insert_node(item, node[name], open_all, flt)
        self._insert_files(parent, node, files, flt)
        if EXTERNAL in node:                    # bibliothèques de la chaîne : en dernier, repliées
            item = self.tree.insert(parent, "end", text=EXTERNAL, tags=("dir",), open=open_all)
            self._insert_node(item, node[EXTERNAL], open_all, flt)

    def _insert_files(self, parent: str, node: dict, files: list[str], flt: str) -> None:
        for name in files:
            path = node[name]
            tags = () if os.path.exists(path) else ("missing",)
            item = self.tree.insert(parent, "end", text=name, tags=tags, open=bool(flt))
            self.target[item] = (path, 0)
            self.file_item[os.path.normpath(path)] = item
            funcs = self._functions_by_file().get(path, [])
            if flt and flt not in name.lower():
                self._insert_funcs(item, [x for x in funcs if flt in x[1].lower()])
            elif funcs:
                self.tree.insert(item, "end", iid=f"{item}·", text="…")    # dépliage paresseux

    def _insert_funcs(self, item: str, funcs: list[tuple[int, str]]) -> None:
        path = self.target[item][0]
        for line, name in funcs:
            fi = self.tree.insert(item, "end", text=f"{name}()", tags=("func",))
            self.target[fi] = (path, line)

    def _expand(self, _e) -> None:
        item = self.tree.focus()
        kids = self.tree.get_children(item)
        if len(kids) == 1 and kids[0] == f"{item}·":
            self.tree.delete(kids[0])
            self._insert_funcs(item, self._functions_by_file().get(self.target[item][0], []))

    # ------------------------------------------------------------------ actions
    def _activate(self, _e=None) -> str | None:
        item = self.tree.focus()
        if item in self.target:
            path, line = self.target[item]
            self.app.show_source(path, line or 1)
            return "break"
        return None

    def _open_first(self) -> None:
        if self.target:
            item = next(iter(self.target))
            self.tree.focus(item)
            self.tree.selection_set(item)
            self._activate()

    def _mark_current(self) -> None:
        for path, item in self.file_item.items():
            if self.tree.exists(item):
                tags = tuple(t for t in self.tree.item(item, "tags") if t != "current")
                if path == self._current:
                    tags += ("current",)
                self.tree.item(item, tags=tags)

    def reveal(self, path: str) -> None:
        """Sélectionne et rend visible le fichier `path`."""
        item = self.file_item.get(os.path.normpath(path))
        if item:
            parent = self.tree.parent(item)
            while parent:
                self.tree.item(parent, open=True)
                parent = self.tree.parent(parent)
            self.tree.selection_set(item)
            self.tree.see(item)

    # ------------------------------------------------------------------ cycle de vie
    def refresh(self) -> None:
        if self.app.symbols is not self._symbols:
            self.populate()
        fr = self.app.current_frame
        cur = os.path.normpath(fr.fullname or fr.file) if fr and (fr.fullname or fr.file) else None
        if cur != self._current:
            self._current = cur
            self._mark_current()

    def on_running(self) -> None:
        if self.app.symbols is not self._symbols:
            self.populate()

    def get_state(self) -> dict:
        return {"filter": self.filter_var.get()}

    def set_state(self, state: dict) -> None:
        self.filter_var.set(state.get("filter", ""))
