"""Passage sûr entre threads de travail et thread tkinter.

tkinter n'est pas thread-safe : seul le thread principal touche aux widgets.
Les autres threads (lecteur gdb, console, trace) appellent `post(fn, *args)`,
et la boucle tkinter exécute ces appels via `after()`.
"""
from __future__ import annotations

import queue
import threading
import traceback
import tkinter as tk
from typing import Any, Callable


class EventPump:
    def __init__(self, root: tk.Misc, period_ms: int = 20):
        self.root = root
        self.period_ms = period_ms
        self._q: queue.Queue[tuple[Callable, tuple, dict]] = queue.Queue()
        self.on_error: Callable[[str], None] = lambda msg: print(msg)
        self._running = True
        self.root.after(self.period_ms, self._poll)

    def post(self, fn: Callable, *args: Any, **kw: Any) -> None:
        """Planifie fn(*args) dans le thread GUI. Utilisable depuis n'importe quel thread."""
        self._q.put((fn, args, kw))

    def background(self, fn: Callable, *args: Any,
                   then: Callable[[Any], None] | None = None) -> None:
        """Exécute fn hors du thread GUI ; `then(résultat)` revient dans le thread GUI."""
        def worker():
            try:
                res = fn(*args)
            except Exception as exc:  # noqa: BLE001 - on remonte tout à l'utilisateur
                self.post(self.on_error, f"{type(exc).__name__}: {exc}")
                return
            if then is not None:
                self.post(then, res)
        threading.Thread(target=worker, daemon=True).start()

    def drain(self) -> None:
        """Exécute tout ce qui est en attente (utile dans les tests)."""
        while True:
            try:
                fn, args, kw = self._q.get_nowait()
            except queue.Empty:
                return
            try:
                fn(*args, **kw)
            except Exception:  # noqa: BLE001
                self.on_error(traceback.format_exc())

    def stop(self) -> None:
        self._running = False

    def _poll(self) -> None:
        if not self._running:
            return
        self.drain()
        self.root.after(self.period_ms, self._poll)
