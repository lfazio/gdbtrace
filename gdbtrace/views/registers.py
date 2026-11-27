"""Register.view : registres du cœur, valeurs modifiées en rouge, décodage xPSR et CONTROL.
TrustZone : état du cœur (S / NS) et registres bancaires groupés par monde."""
from __future__ import annotations

from tkinter import simpledialog, ttk

from ..gui.view_base import View

#: registres montrés d'office (les autres : « tous les registres »), comparés sans la casse :
#: le serveur gdb choisit l'écriture (OpenOCD : « xPSR », QEMU : « xpsr »)
CORE = ["r0", "r1", "r2", "r3", "r4", "r5", "r6", "r7", "r8", "r9", "r10", "r11",
        "r12", "sp", "lr", "pc", "xpsr", "msp", "psp", "primask", "basepri",
        "faultmask", "control", "fpscr"]


def decode_xpsr(v: int) -> str:
    flags = "".join(c if v & (1 << b) else c.lower()
                    for c, b in (("N", 31), ("Z", 30), ("C", 29), ("V", 28), ("Q", 27)))
    t = "T" if v & (1 << 24) else "t"
    exc = v & 0x1FF
    mode = "Thread" if exc == 0 else f"Handler #{exc}"
    return f"{flags} {t}  {mode}"


def decode_control(v: int) -> str:
    """CONTROL : nPRIV, SPSEL, FPCA, SFPA (ARMv8-M sécurisé)."""
    parts = ["non privilégié" if v & 1 else "privilégié", "PSP" if v & 2 else "MSP"]
    parts += [n for bit, n in ((2, "FPCA"), (3, "SFPA")) if v >> bit & 1]
    return ", ".join(parts)


def banked_groups(by_lower: dict[str, str]) -> list[tuple[str, list[str]]]:
    """TrustZone : [(« Sécurisé », [msp_s, …]), (« Non sécurisé », [msp_ns, …])] d'après les
    registres bancaires que gdb expose ; [] sans extension de sécurité."""
    from ..core.security import BANKED
    out = []
    for title, suffix in (("Sécurisé", "_s"), ("Non sécurisé", "_ns")):
        names = [by_lower[b + suffix] for b in BANKED if b + suffix in by_lower]
        if names:
            out.append((title, names))
    return out if len(out) == 2 else []


class RegistersView(View):
    title = "Registres"
    kind = "registers"

    def __init__(self, master, app, **kw):
        super().__init__(master, app, **kw)
        self.tree = ttk.Treeview(self, columns=("hex", "dec", "info"), show="tree headings",
                                 selectmode="browse")
        self.tree.heading("#0", text="Reg")
        self.tree.heading("hex", text="Hex")
        self.tree.heading("dec", text="Déc")
        self.tree.heading("info", text="")
        self.tree.column("#0", width=70, stretch=False)
        self.tree.column("hex", width=90, stretch=False)
        self.tree.column("dec", width=100, stretch=False)
        self.tree.column("info", width=160)
        self.tree.tag_configure("changed", foreground="#d00000")
        self.tree.tag_configure("dim", foreground="#888888")
        sb = ttk.Scrollbar(self, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.tree.bind("<Double-1>", self._edit)
        self.show_all = False
        self.prev: dict[str, int] = {}

    def refresh(self) -> None:
        if not self.stopped:
            return
        regs = self.session.registers()
        by_lower = {n.lower(): n for n, _ in regs}
        core = [by_lower[c] for c in CORE if c in by_lower]
        groups = banked_groups(by_lower)
        banked = {n for _title, names in groups for n in names}
        order = core + ([n for n, _ in regs if n not in core and n not in banked]
                        if self.show_all else [])
        values = dict(regs)
        self.tree.delete(*self.tree.get_children())
        for name in order:
            self._row("", name, values[name])
        sec = getattr(self.app, "security", None)
        if groups:                       # TrustZone : état, puis registres bancaires par monde
            if sec is not None:
                self.tree.insert("", "end", iid="#state", text="état",
                                 values=(sec.label, "", sec.text()))
            for title, names in groups:
                parent = self.tree.insert("", "end", iid=f"#{title}", text=title, open=True,
                                          values=("", "", ""))
                for name in names:
                    self._row(parent, name, values[name])
        self.prev = values

    def _row(self, parent: str, name: str, v: int) -> None:
        signed = v - (1 << 32) if v & 0x80000000 else v
        low = name.lower()
        info = decode_xpsr(v) if low == "xpsr" else ""
        if low.startswith("control"):
            info = decode_control(v)
        if name in ("pc", "lr") and self.app.symbols:
            f = self.app.symbols.function_at(v & ~1)
            info = f"<{f.name}+{(v & ~1) - f.start:#x}>" if f else ""
        tags = ("changed",) if self.prev and self.prev.get(name) != v else ()
        self.tree.insert(parent, "end", iid=name, text=name, values=(f"{v:08x}", signed, info),
                         tags=tags)

    def on_running(self) -> None:
        for iid in self.tree.get_children():
            self.tree.item(iid, tags=("dim",))
            for child in self.tree.get_children(iid):
                self.tree.item(child, tags=("dim",))

    def _edit(self, _event) -> None:
        iid = self.tree.focus()
        if not iid or iid.startswith("#") or not self.stopped:
            return
        cur = self.prev.get(iid, 0)
        val = simpledialog.askstring("Modifier", f"{iid} =", initialvalue=f"{cur:#010x}",
                                     parent=self)
        if val:
            self.app.safe(self.session.write_register, iid, int(val, 0))
            self.app.refresh_all()
