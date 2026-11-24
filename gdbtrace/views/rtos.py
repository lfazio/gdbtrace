"""TASK.List / TASK.Objects : vue FreeRTOS (tâches, piles, objets du noyau), en direct.

Cible arrêtée : lecture via gdb. En marche : le même parcours est exécuté dans le thread
du LivePoller avec le canal mémoire latéral ; un instantané incohérent (listes modifiées
pendant la lecture) est ignoré et le précédent reste affiché.
"""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk

from ..core.rtos import freertos as fr
from ..gui.view_base import View

STATE_COLORS = {"Running": "#107010", "Ready": "#000000", "Blocked": "#2050a0",
                "Suspended": "#888888", "Deleted": "#b00000"}
LIVE_ATTEMPTS = 4


def _fmt_prio(t: fr.Task) -> str:
    if t.base_priority is not None and t.base_priority != t.priority:
        return f"{t.priority} (base {t.base_priority})"   # héritage de priorité (mutex)
    return str(t.priority)


class RtosView(View):
    title = "RTOS"
    kind = "rtos"

    def __init__(self, master, app, **kw):
        super().__init__(master, app, **kw)
        bar = ttk.Frame(self)
        bar.pack(fill="x")
        self.info = ttk.Label(bar, text="")
        self.info.pack(side="left", padx=4)
        self.live_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(bar, text="Live", variable=self.live_var,
                        command=self._update_subscription).pack(side="right", padx=6)

        self.tabs = ttk.Notebook(self)
        self.tabs.pack(fill="both", expand=True)
        tf, of = ttk.Frame(self.tabs), ttk.Frame(self.tabs)
        self.tabs.add(tf, text="Tâches")
        self.tabs.add(of, text="Objets noyau")

        self.tasks = self._tree(tf, [("state", "État", 95), ("prio", "Prio", 70),
                                     ("stack", "Pile utilisée", 95), ("pct", "Max %", 55),
                                     ("free", "Libre min.", 75), ("cpu", "CPU", 60),
                                     ("wait", "Attend", 150), ("tcb", "TCB", 80)],
                                ("Tâche", 110), stretch="wait")
        for state, color in STATE_COLORS.items():
            self.tasks.tag_configure(state, foreground=color)
        bold = (self.mono.actual("family"), self.mono.actual("size"), "bold")
        self.tasks.tag_configure("current", font=bold)
        self.tasks.tag_configure("stackwarn", background="#ffd8d8")
        self.tasks.bind("<Double-1>", self._open_context)

        self.objects = self._tree(of, [("kind", "Type", 120), ("fill", "État", 110),
                                       ("wsend", "Attente envoi", 120),
                                       ("wrecv", "Attente réception / prise", 160),
                                       ("handle", "Handle", 80)], ("Objet", 110), stretch="wrecv")

        self.layout: fr.FreeRtosLayout | None = None
        self.snap: fr.RtosSnapshot | None = None
        self._status("")

    @staticmethod
    def _tree(master, cols, first, stretch: str | None = None):
        """Treeview + barre de défilement. `stretch` : colonne qui absorbe la largeur
        (par défaut la dernière)."""
        stretch = stretch or cols[-1][0]
        tv = ttk.Treeview(master, columns=[c[0] for c in cols], show="tree headings",
                          selectmode="browse")
        tv.heading("#0", text=first[0])
        tv.column("#0", width=first[1], stretch=False)
        for cid, label, width in cols:
            tv.heading(cid, text=label)
            tv.column(cid, width=width, stretch=cid == stretch)
        sb = ttk.Scrollbar(master, orient="vertical", command=tv.yview)
        tv.configure(yscrollcommand=sb.set)
        tv.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        return tv

    # ------------------------------------------------------------------ disposition
    def _ensure_layout(self) -> bool:
        if self.layout is not None:
            return True
        if not fr.detect(self.app.symbols):
            self._status("pas de FreeRTOS dans l'ELF")
            return False
        # disposition partagée avec le profilage par tâche ; calculée via gdb, cible arrêtée
        self.layout = self.app.rtos_layout()
        return self.layout is not None

    def _status(self, extra: str) -> None:
        s = self.snap
        txt = "FreeRTOS" if self.layout else "RTOS"
        if s and not s.incoherent:
            txt += f" — {len(s.tasks)} tâches, tick {s.tick}"
            if s.scheduler_running is False:
                txt += " (ordonnanceur non démarré)"
        self.info.configure(text=f"{txt}  {extra}".strip())

    # ------------------------------------------------------------------ rafraîchissement
    def refresh(self) -> None:
        if not self.stopped or not self._ensure_layout():
            return
        sp = self.session.task_sp()
        snap = fr.snapshot(self.layout, self.session.read_memory, prev=self.snap, current_sp=sp)
        self._show(snap)

    def on_running(self) -> None:
        self._update_subscription()

    def _update_subscription(self) -> None:
        poller = self.app.poller
        if poller is None:
            return
        if self.live_var.get() and self.layout is not None:
            layout = self.layout
            poller.subscribe_job(
                self, lambda read: fr.snapshot(layout, read, prev=self.snap, attempts=LIVE_ATTEMPTS),
                self._show)
        else:
            poller.unsubscribe(self)

    def _show(self, snap: fr.RtosSnapshot) -> None:
        if snap.incoherent:
            # lu pendant un changement de contexte : on garde l'instantané précédent
            self._status(f"(instantané ignoré : {snap.incoherent})")
            return
        self.snap = snap
        sel = self.tasks.focus()
        self.tasks.delete(*self.tasks.get_children())
        for t in snap.tasks:
            pct = t.stack_percent
            stack = f"{t.stack_used}/{t.stack_size}" if t.stack_used is not None and t.stack_size else "?"
            if t.current and not self.stopped:
                stack = "~" + stack   # SP de la tâche courante inconnu en marche
            tags = [t.state] + (["current"] if t.current else []) + \
                   (["stackwarn"] if pct is not None and pct > 90 else [])
            self.tasks.insert("", "end", iid=f"{t.tcb:x}", text=t.name, tags=tags, values=(
                t.state, _fmt_prio(t), stack,
                f"{pct:.0f} %" if pct is not None else "?",
                f"{t.stack_free_min} o" if t.stack_free_min is not None else "?",
                f"{t.cpu:.1f} %" if t.cpu is not None else "",
                t.waiting_on or "", f"{t.tcb:08x}"))
        if sel and self.tasks.exists(sel):
            self.tasks.focus(sel)
            self.tasks.selection_set(sel)
        self.objects.delete(*self.objects.get_children())
        for o in snap.objects:
            if o.kind in ("mutex", "mutex récursif"):
                fill = f"pris par {o.holder}" if o.holder else "libre"
            elif o.kind == "queue":
                fill = f"{o.count}/{o.length} messages"
            else:
                fill = f"{o.count}/{o.length}"
            self.objects.insert("", "end", text=o.name, values=(
                o.kind, fill, ", ".join(o.waiting_send), ", ".join(o.waiting_receive),
                f"{o.handle:08x}"))
        self._status("")
        self.app.set_current_task(snap.current.name if snap.current else None)

    def on_program_changed(self) -> None:
        self.on_disconnected()

    def on_disconnected(self) -> None:
        if self.app.poller:
            self.app.poller.unsubscribe(self)
        self.layout = self.snap = None
        self.tasks.delete(*self.tasks.get_children())
        self.objects.delete(*self.objects.get_children())
        self.app.set_current_task(None)
        self._status("")

    def destroy(self) -> None:
        if self.app.poller:
            self.app.poller.unsubscribe(self)
        super().destroy()

    # ------------------------------------------------------------------ contexte d'une tâche
    def _open_context(self, _e=None) -> None:
        iid = self.tasks.focus()
        if not iid or self.snap is None:
            return
        task = next((t for t in self.snap.tasks if f"{t.tcb:x}" == iid), None)
        if task is None:
            return
        if not self.stopped:
            self.app.status.set("Arrêter la cible pour afficher le contexte d'une tâche")
            return
        self.app.safe(self.show_context, task)

    def show_context(self, task: fr.Task) -> "TaskContextWindow":
        regs, frames = fr.task_context(self.session, self.layout, task)
        return TaskContextWindow(self, task, regs, frames)

    def get_state(self) -> dict:
        return {"live": self.live_var.get(), "tab": self.tabs.index("current")}

    def set_state(self, state: dict) -> None:
        self.live_var.set(state.get("live", True))
        if "tab" in state:
            self.tabs.select(state["tab"])


class TaskContextWindow(tk.Toplevel):
    """Registres et pile d'appels d'une tâche ; double-clic sur une frame → source."""

    def __init__(self, view: RtosView, task: fr.Task, regs: dict[str, int], frames: list):
        super().__init__(view)
        self.view = view
        self.title(f"Contexte de la tâche « {task.name} » — gdbtrace")
        self.geometry("720x380")
        head = f"{task.name} : {task.state}, priorité {task.priority}"
        if task.waiting_on:
            head += f", attend {task.waiting_on}"
        if not task.current:
            head += "  —  registres relus sur la pile de la tâche"
        ttk.Label(self, text=head).pack(fill="x", padx=6, pady=4)
        pw = ttk.PanedWindow(self, orient="horizontal")
        pw.pack(fill="both", expand=True)
        left, right = ttk.Frame(pw), ttk.Frame(pw)
        pw.add(left, weight=1)
        pw.add(right, weight=2)
        self.regs = RtosView._tree(left, [("value", "Valeur", 100)], ("Registre", 70))
        for name in fr.CORE_REGS + ["xpsr"]:
            if name in regs:
                self.regs.insert("", "end", text=name, values=(f"{regs[name]:08x}",))
        self.frames_tv = RtosView._tree(right, [("func", "Fonction", 150), ("loc", "Source", 200)],
                                        ("#", 40))
        self.frames = frames
        for i, f in enumerate(frames):
            loc = f"{f.file}:{f.line}" if f.file else ""
            self.frames_tv.insert("", "end", iid=str(i), text=str(i),
                                  values=(f.func or f"{f.pc:#x}", loc))
        self.frames_tv.bind("<Double-1>", self._goto)

    def _goto(self, _e=None) -> None:
        sel = self.frames_tv.focus()
        if sel:
            f = self.frames[int(sel)]
            if f.fullname and f.line:
                self.view.app.show_source(f.fullname, f.line)
