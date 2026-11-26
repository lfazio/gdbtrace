"""TERM.view : le texte du programme (semihosting, RTT) et une ligne pour lui répondre.
Sources et lecture dans `core/terminal.py`."""
from __future__ import annotations

import threading
import tkinter as tk
from tkinter import ttk

from ..gui.view_base import View

MAX_LINES = 5000
COLORS = {"rtt": "#1d4f91", "semihosting": "#000000"}


class TerminalView(View):
    title = "Terminal"
    kind = "terminal"

    def __init__(self, master, app, **kw):
        super().__init__(master, app, **kw)
        bar = ttk.Frame(self)
        bar.pack(fill="x")
        self.info = ttk.Label(bar, text="aucune source (--semihosting, --rtt)")
        self.info.pack(side="left", padx=4)
        ttk.Button(bar, text="Effacer", command=self._clear).pack(side="right")
        entry = ttk.Frame(self)
        entry.pack(side="bottom", fill="x")
        self.input_var = tk.StringVar()
        e = ttk.Entry(entry, textvariable=self.input_var)
        e.pack(side="left", fill="x", expand=True, padx=2, pady=2)
        e.bind("<Return>", lambda _e: self._send())
        ttk.Button(entry, text="Envoyer", command=self._send).pack(side="left", padx=2)
        self.text = tk.Text(self, font=self.mono, wrap="char", height=10, state="disabled",
                            background="#fbfbf6")
        for source, color in COLORS.items():
            self.text.tag_configure(source, foreground=color)
        sb = ttk.Scrollbar(self, orient="vertical", command=self.text.yview)
        self.text.configure(yscrollcommand=sb.set)
        self.text.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self._term = None
        self._lock = threading.Lock()
        self._pending: list[tuple[str, str]] = []
        self._posted = False
        self._hook()

    # ------------------------------------------------------------------ source
    def _hook(self) -> None:
        """S'abonne au terminal de la session courante (un par ScriptAPI)."""
        term = self.app.api.term if self.app.api is not None else None
        if term is self._term:
            return
        if self._term is not None and self._on_text in self._term.listeners:
            self._term.listeners.remove(self._on_text)
        self._term = term
        if term is not None:
            term.listeners.append(self._on_text)
            if term.text():
                self._append([("", term.text())], replace=True)

    def _on_text(self, text: str, source: str) -> None:
        """Fil d'une source : regroupé, un seul rappel posté à la fois (flot continu)."""
        with self._lock:
            self._pending.append((source, text))
            if self._posted:
                return
            self._posted = True
        self.app.pump.post(self._flush)

    def _flush(self) -> None:
        with self._lock:
            chunks, self._pending, self._posted = self._pending, [], False
        if chunks and self.winfo_exists():
            self._append(chunks)

    def _append(self, chunks: list[tuple[str, str]], replace: bool = False) -> None:
        t = self.text
        at_end = t.yview()[1] >= 0.999
        t.configure(state="normal")
        if replace:
            t.delete("1.0", "end")
        args: list = []
        for source, text in chunks:
            args += [text, source]
        t.insert("end", *args)
        lines = int(t.index("end-1c").split(".")[0])
        if lines > MAX_LINES:
            t.delete("1.0", f"{lines - MAX_LINES}.0")
        t.configure(state="disabled")
        if at_end:
            t.see("end")

    # ------------------------------------------------------------------ cycle de vie
    def refresh(self) -> None:
        """Arrêt : le RTT lu par gdb (la fin de ce qui a été écrit en marche)."""
        self._hook()
        term = self._term
        if term is None:
            return
        self.info.configure(text=term.describe())
        if term.rtt is not None:
            self.app.safe(term.poll_rtt, False)

    def on_running(self) -> None:
        self._hook()
        term, poller = self._term, self.app.poller
        if term is None or poller is None:
            return
        if term.rtt is not None and term.live_io is not None:
            poller.subscribe_job(self, lambda _read: term.poll_rtt(True), lambda _n: None)
        else:
            poller.unsubscribe(self)

    def on_disconnected(self) -> None:
        if self.app.poller is not None:
            self.app.poller.unsubscribe(self)
        self.info.configure(text="pas de session")

    def destroy(self) -> None:
        if self.app.poller is not None:
            self.app.poller.unsubscribe(self)
        if self._term is not None and self._on_text in self._term.listeners:
            self._term.listeners.remove(self._on_text)
        super().destroy()

    # ------------------------------------------------------------------ actions
    def _send(self) -> None:
        text = self.input_var.get()
        if self._term is None:
            return
        where = self.app.safe(self._term.send, text + "\n")
        if where:
            self.input_var.set("")
            self.app.status.set(f"envoyé par {where}")

    def _clear(self) -> None:
        if self._term is not None:
            self._term.clear()
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.configure(state="disabled")
