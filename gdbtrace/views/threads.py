"""Vue Threads : threads du programme — pthreads d'un programme natif, ou tâches RTOS exposées
comme threads par le serveur gdb (OpenOCD `--rtos`).

Pour chaque thread : numéro gdb, nom (`pthread_setname_np`), LWP, état, cœur, et où il en
est (fonction, fichier:ligne) quand le programme est arrêté. Le thread courant est en gras ;
celui qui a provoqué l'arrêt est rappelé en tête. Double-clic (programme arrêté) : il devient
le thread courant, et registres, pile, source et pas à pas portent sur lui. La liste suit la
création et la fin des threads, même programme en marche.
"""
from __future__ import annotations

import os
from tkinter import ttk

from ..gui.view_base import View


class ThreadsView(View):
    title = "Threads"
    kind = "threads"

    COLS = [("name", "Nom", 120), ("lwp", "LWP", 75), ("state", "État", 70),
            ("core", "Cœur", 45), ("where", "Où", 260)]

    def __init__(self, master, app, **kw):
        super().__init__(master, app, **kw)
        self.info = ttk.Label(self, text="", anchor="w")
        self.info.pack(fill="x", padx=4)
        body = ttk.Frame(self)
        body.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(body, columns=[c[0] for c in self.COLS], show="tree headings",
                                 selectmode="browse")
        self.tree.heading("#0", text="N°")
        self.tree.column("#0", width=45, stretch=False)
        for cid, label, width in self.COLS:
            self.tree.heading(cid, text=label)
            self.tree.column(cid, width=width, stretch=cid == "where")
        sb = ttk.Scrollbar(body, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        bold = (self.mono.actual("family"), self.mono.actual("size"), "bold")
        self.tree.tag_configure("current", font=bold)
        self.tree.tag_configure("running", foreground="#208020")
        self.tree.bind("<Double-1>", self._select)
        self.threads = []

    # ------------------------------------------------------------------ contenu
    def _load(self) -> None:
        s = self.session
        if s is None or s.state not in ("stopped", "running"):
            self._clear("pas de programme en cours" if s is not None and s.state == "exited" else "")
            return
        self.threads = s.thread_list()
        self.tree.delete(*self.tree.get_children())
        for t in self.threads:
            f = t.frame
            where = ""
            if f is not None:
                where = f.func or f"{f.pc:#x}"
                if f.file:
                    where += f"  {os.path.basename(f.file)}:{f.line}"
            tags = (("current",) if t.current else ()) + (("running",) if t.state == "running" else ())
            self.tree.insert("", "end", iid=t.id, text=t.id, tags=tags,
                             values=(t.name, t.lwp or "", t.state, "" if t.core is None else t.core, where))
        stopper = (s.stop_reason or {}).get("thread-id") if s.state == "stopped" else None
        by_id = {t.id: t for t in self.threads}
        msg = f"{len(self.threads)} thread(s)"
        if stopper in by_id:
            msg += f" — arrêt dans {stopper} « {by_id[stopper].name} »"
        self.info.configure(text=msg)

    def _clear(self, msg: str) -> None:
        self.threads = []
        self.tree.delete(*self.tree.get_children())
        self.info.configure(text=msg)

    def _select(self, _e=None) -> str | None:
        item = self.tree.focus()
        if item:
            self.app.select_thread(item)
            return "break"
        return None

    # ------------------------------------------------------------------ cycle de vie
    def refresh(self) -> None:
        self._load()

    def on_running(self) -> None:
        self.app.safe(self._load)

    def on_threads(self, _data: dict) -> None:
        """Thread créé, terminé ou sélectionné (appelé dans le thread GUI)."""
        self.app.safe(self._load)

    def on_exited(self, _data: dict) -> None:
        self._clear("programme terminé")

    def on_disconnected(self) -> None:
        self._clear("")
