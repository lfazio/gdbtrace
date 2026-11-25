"""Passage sûr entre threads de travail et thread tkinter.

tkinter n'est pas thread-safe : seul le thread principal touche aux widgets.
Les autres threads (lecteur gdb, console, trace) appellent `post(fn, *args)`,
et la boucle tkinter exécute ces appels via `after()`.
"""
from __future__ import annotations

import queue
import threading
import time
import traceback
import tkinter as tk
from typing import Any, Callable


class EventPump:
    def __init__(self, root: tk.Misc, period_ms: int = 20):
        self.root = root
        self.period_ms = period_ms
        self._q: queue.Queue[tuple[Callable, tuple, dict]] = queue.Queue()
        self.on_error: Callable[[str], None] = lambda msg: print(msg)
        #: le thread GUI exécute des rappels (rafraîchissement de vues…) : les fils de calcul
        #: qui le peuvent lui cèdent le GIL (`TraceManager.gui_busy`)
        self.busy = False
        self._running = True
        self._after_id = self.root.after(self.period_ms, self._poll)

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

    def drain(self, budget: float | None = None) -> None:
        """Exécute ce qui est en attente : tout (utile dans les tests), ou pendant `budget`
        secondes au plus. Sans limite, des fils qui postent sans arrêt (trace en marche)
        retiendraient le thread GUI ici : plus de dessin ni de clic."""
        end = None if budget is None else time.monotonic() + budget
        try:
            while end is None or time.monotonic() < end:
                try:
                    fn, args, kw = self._q.get_nowait()
                except queue.Empty:
                    return
                self.busy = True
                try:
                    fn(*args, **kw)
                except Exception:  # noqa: BLE001
                    self.on_error(traceback.format_exc())
        finally:
            self.busy = False

    def stop(self) -> None:
        self._running = False
        try:  # évite « invalid command name ..._poll » après destruction de la fenêtre
            self.root.after_cancel(self._after_id)
        except tk.TclError:
            pass

    def _poll(self) -> None:
        if not self._running:
            return
        self.drain(budget=0.1)
        self._after_id = self.root.after(self.period_ms, self._poll)
