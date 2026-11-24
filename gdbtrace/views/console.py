"""Console Python (remplaçant de la ligne de commande PRACTICE) + sortie gdb/cible.

- Ligne normale : Python, avec l'objet `gdbtrace` (voir core/scripting.py).
- Ligne commençant par `!` : commande CLI gdb brute (`!info registers`).
- Ligne commençant par `mon ` : commande `monitor` vers le serveur gdb.
"""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk

from ..gui.view_base import View


class ConsoleView(View):
    title = "Console"
    kind = "console"

    def __init__(self, master, app, **kw):
        super().__init__(master, app, **kw)
        self.out = tk.Text(self, font=self.mono, wrap="word", height=10, background="#f7f7f7")
        self.out.tag_configure("cmd", foreground="#3050a0")
        self.out.tag_configure("gdb", foreground="#707070")
        self.out.tag_configure("target", foreground="#007000")
        self.out.tag_configure("err", foreground="#d00000")
        self.out.tag_configure("action", foreground="#8a5a00")    # messages des breakpoints à action
        sb =ttk.Scrollbar(self, orient="vertical", command=self.out.yview)
        self.out.configure(yscrollcommand=sb.set, state="disabled")
        line = ttk.Frame(self)
        line.pack(side="bottom", fill="x")
        self.prompt = ttk.Label(line, text=">>>", font=self.mono, width=4)
        self.prompt.pack(side="left")
        self.entry_var = tk.StringVar()
        self.entry = ttk.Entry(line, textvariable=self.entry_var, font=self.mono)
        self.entry.pack(side="left", fill="x", expand=True)
        self.entry.bind("<Return>", self._submit)
        self.entry.bind("<Up>", lambda _e: self._hist(-1))
        self.entry.bind("<Down>", lambda _e: self._hist(1))
        self.out.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.history: list[str] = []
        self.hpos = 0
        app.console_views.append(self)

    def destroy(self) -> None:
        if self in self.app.console_views:
            self.app.console_views.remove(self)
        super().destroy()

    # ------------------------------------------------------------------ sortie
    def write(self, text: str, tag: str = "") -> None:
        self.out.configure(state="normal")
        self.out.insert("end", text, tag)
        # borne la taille du tampon
        if int(self.out.index("end").split(".")[0]) > 5000:
            self.out.delete("1.0", "1000.0")
        self.out.see("end")
        self.out.configure(state="disabled")

    # ------------------------------------------------------------------ entrée
    def _hist(self, d: int) -> str:
        if self.history:
            self.hpos = max(0, min(len(self.history), self.hpos + d))
            self.entry_var.set(self.history[self.hpos] if self.hpos < len(self.history) else "")
            self.entry.icursor("end")
        return "break"

    def _submit(self, _e=None) -> None:
        line = self.entry_var.get()
        self.entry_var.set("")
        if line.strip():
            self.history.append(line)
        self.hpos = len(self.history)
        self.write(f"{self.prompt.cget('text')} {line}\n", "cmd")
        self.app.execute(line, done=self._done)

    def _done(self, more: bool) -> None:
        self.prompt.configure(text="..." if more else ">>>")

    def focus_entry(self) -> None:
        self.entry.focus_set()
