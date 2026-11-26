"""Vue Faute : la faute en cours décodée (cause en clair, adresse, instruction et ligne
fautives, registres du SCB, cadre empilé). Analyse dans `core/fault.py`."""
from __future__ import annotations

from tkinter import ttk

from ..core import fault
from ..gui.view_base import View


class FaultView(View):
    title = "Faute"
    kind = "fault"

    def __init__(self, master, app, **kw):
        super().__init__(master, app, **kw)
        bar = ttk.Frame(self)
        bar.pack(fill="x")
        ttk.Button(bar, text="Analyser", command=lambda: self.refresh(force=True)).pack(side="left")
        self.goto = ttk.Button(bar, text="Aller à la ligne fautive", command=self._goto, state="disabled")
        self.goto.pack(side="left", padx=4)
        ttk.Checkbutton(bar, text="Arrêt sur faute", variable=app.fault_catch_var,
                        command=app.toggle_fault_catch).pack(side="left", padx=8)
        self.info = ttk.Label(self, text="Cible non arrêtée", wraplength=600, justify="left",
                              font=("TkDefaultFont", 10, "bold"))
        self.info.pack(fill="x", padx=4, pady=4)
        self.info.bind("<Configure>", lambda e: self.info.configure(wraplength=max(200, e.width - 8)))
        self.tree = ttk.Treeview(self, columns=("value", "info"), show="tree headings", selectmode="browse")
        self.tree.heading("#0", text="")
        self.tree.heading("value", text="Valeur")
        self.tree.heading("info", text="Signification")
        self.tree.column("#0", width=150, stretch=False)
        self.tree.column("value", width=110, stretch=False)
        self.tree.column("info", width=420)
        self.tree.tag_configure("cause", foreground="#b00000")
        self.tree.tag_configure("dim", foreground="#777777")
        sb = ttk.Scrollbar(self, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.tree.bind("<Double-1>", lambda _e: self._goto())
        self.report: fault.FaultReport | None = None
        self.report_stop = -1              # Session.stop_count de l'analyse

    # ------------------------------------------------------------------ cycle de vie
    def refresh(self, force: bool = False) -> None:
        if not self.stopped or getattr(self.app, "native", False):
            return
        s = self.session
        if not force and self.report_stop == s.stop_count:
            return
        self.report = fault.analyze(s)
        self.report_stop = s.stop_count
        self.render()

    def on_running(self) -> None:
        self.info.configure(text="Cible en marche")

    def on_disconnected(self) -> None:
        self.report = None
        self.report_stop = -1
        self.tree.delete(*self.tree.get_children())
        self.info.configure(text="Pas de session")

    # ------------------------------------------------------------------ affichage
    def render(self) -> None:
        r = self.report
        self.tree.delete(*self.tree.get_children())
        if r is None:
            return
        self.info.configure(text=r.summary(), foreground="#b00000" if r.in_fault else "")
        self.goto.configure(state="normal" if r.fault_file and r.fault_line else "disabled")
        t = self.tree
        if r.in_fault:
            label = "Causes déduites" if r.deduced else "Causes"
            causes = t.insert("", "end", text=label, open=True)
            for name, text in r.causes:
                t.insert(causes, "end", text=name, values=("", text), tags=("cause",))
            if r.deduced:
                t.insert(causes, "end", text="", values=("", f"{r.arch} : pas de registre de cause, "
                                                          "déduction d'après l'instruction et le cadre"),
                         tags=("dim",))
            where = t.insert("", "end", text="Instruction fautive", open=True)
            if r.fault_pc is not None:
                t.insert(where, "end", text="pc", values=(f"{r.fault_pc:#010x}", r.where()))
            if r.instruction:
                t.insert(where, "end", text="instruction", values=("", r.instruction))
            if r.address is not None:
                t.insert(where, "end", text="adresse", values=(f"{r.address:#010x}", "donnée fautive"))
            for note in r.notes:
                t.insert(where, "end", text="note", values=("", note), tags=("dim",))
        if r.registers:
            regs = t.insert("", "end", text="Registres du SCB", open=True)
            tables = {"CFSR": fault.CFSR_BITS, "HFSR": fault.HFSR_BITS, "SFSR": fault.SFSR_BITS}
            for name, value in r.registers.items():
                bits = ", ".join(n for n, _ in fault._bits(value, tables.get(name, [])))
                t.insert(regs, "end", text=name, values=(f"{value:#010x}", bits),
                         tags=() if value else ("dim",))
        if r.frame is not None:
            f = r.frame
            fr = t.insert("", "end", text="Cadre empilé", open=True)
            t.insert(fr, "end", text="base", values=(f"{f.base:#010x}", f.describe()))
            for name in fault.STACKED:
                t.insert(fr, "end", text=name, values=(f"{f.regs[name]:#010x}", ""))
        if not r.in_fault:
            t.insert("", "end", text="", values=("", f"{r.arch} ; exception active : "
                                                 f"{r.exception or 'aucune (mode Thread)'}"), tags=("dim",))

    # ------------------------------------------------------------------ actions
    def _goto(self) -> None:
        r = self.report
        if r is not None and r.fault_fullname and r.fault_line:
            self.app.show_source(r.fault_fullname, r.fault_line)
