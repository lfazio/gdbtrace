"""Data.dump : dump mémoire hexadécimal, octets modifiés en rouge, édition, rafraîchissement."""
from __future__ import annotations

import tkinter as tk
from tkinter import simpledialog, ttk

from ..gui.view_base import View

WIDTH = 16
HEX_COL = 10  # position de la première colonne hexa dans une ligne


DEFAULT_ADDR = "0x20000000"     # SRAM d'un Cortex-M


class MemoryView(View):
    title = "Mémoire"
    kind = "memory"

    def __init__(self, master, app, **kw):
        super().__init__(master, app, **kw)
        bar = ttk.Frame(self)
        bar.pack(fill="x")
        ttk.Label(bar, text="Adresse :").pack(side="left")
        self.addr_var = tk.StringVar(value=DEFAULT_ADDR)
        e = ttk.Entry(bar, textvariable=self.addr_var, width=24)
        e.pack(side="left", padx=2)
        e.bind("<Return>", lambda _e: self.refresh())
        ttk.Label(bar, text="Taille :").pack(side="left")
        self.len_var = tk.StringVar(value="256")
        e2 = ttk.Entry(bar, textvariable=self.len_var, width=6)
        e2.pack(side="left", padx=2)
        e2.bind("<Return>", lambda _e: self.refresh())
        ttk.Button(bar, text="Lire", command=self.refresh).pack(side="left", padx=2)
        # Live : rafraîchi pendant que la cible tourne, via le canal latéral (OpenOCD/QEMU)
        self.live_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(bar, text="Live", variable=self.live_var,
                        command=lambda: self.app.safe(self._update_subscription)).pack(side="left", padx=6)

        self.text = tk.Text(self, font=self.mono, wrap="none", height=16, width=78,
                            background="#fbfbf6")
        self.text.tag_configure("addr", foreground="#3050a0")
        self.text.tag_configure("changed", foreground="#d00000")
        self.text.tag_configure("ascii", foreground="#606060")
        sb = ttk.Scrollbar(self, orient="vertical", command=self.text.yview)
        self.text.configure(yscrollcommand=sb.set)
        self.text.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.text.bind("<Double-1>", self._edit)
        self.text.configure(state="disabled")
        self.base = 0
        self.prev: bytes = b""
        self.prev_base = -1
        self.status = ""

    # ------------------------------------------------------------------ lecture
    def _resolve(self) -> int:
        s = self.addr_var.get().strip()
        try:
            return int(s, 0)
        except ValueError:
            pass
        # adresses de l'ELF : justes pour un firmware ; un programme natif (PIE) est relogé,
        # seul gdb connaît alors l'adresse réelle
        if self.app.symbols and s in self.app.symbols.objects and not self.app.native:
            return self.app.symbols.objects[s][0]
        if self.stopped:
            return self.session.address_of_expr(s)
        return self.base  # cible en marche : gdb ne peut pas évaluer, on garde l'adresse connue

    def _window(self) -> tuple[int, int]:
        base = self._resolve() & ~(WIDTH - 1)
        n = max(WIDTH, min(int(self.len_var.get() or 256, 0), 65536))
        return base, n

    def refresh(self) -> None:
        if not self.connected:
            return
        base, n = self._window()
        self.base = base
        if self.stopped:
            self._render(base, self.session.read_memory(base, n))
        self._update_subscription()

    def on_program_changed(self) -> None:
        """Programme natif : 0x20000000 (SRAM d'un Cortex-M) n'y est pas lisible ; la vue part
        de la première variable globale du programme, désignée par son nom."""
        sym = self.app.symbols
        if not (self.app.native and sym and self.addr_var.get().strip() == DEFAULT_ADDR):
            return
        names = [n for n, (_a, size) in sym.objects.items() if size and n.isidentifier()
                 and not n.startswith("_")]
        if names:
            self.addr_var.set(min(names, key=lambda n: sym.objects[n][0]))

    # ------------------------------------------------------------------ live (canal latéral)
    def _update_subscription(self) -> None:
        poller = self.app.poller
        if poller is None:
            return
        if self.live_var.get() and self.connected:
            base, n = self._window()
            poller.subscribe(self, [(base, n)], lambda data, b=base: self._on_live(b, data))
        else:
            poller.unsubscribe(self)

    def _on_live(self, base: int, data: dict[int, bytes]) -> None:
        if base in data:
            self._render(base, data[base])

    def on_running(self) -> None:
        self._update_subscription()

    def on_disconnected(self) -> None:
        if self.app.poller:
            self.app.poller.unsubscribe(self)

    def destroy(self) -> None:
        if self.app.poller:
            self.app.poller.unsubscribe(self)
        super().destroy()

    def _render(self, base: int, data: bytes) -> None:
        prev = self.prev if self.prev_base == base else b""
        self.base = base
        t = self.text
        t.configure(state="normal")
        yview = t.yview()
        t.delete("1.0", "end")
        for off in range(0, len(data), WIDTH):
            chunk = data[off:off + WIDTH]
            t.insert("end", f"{base + off:08x}  ", "addr")
            for i, b in enumerate(chunk):
                k = off + i
                tag = "changed" if prev and k < len(prev) and prev[k] != b else ""
                t.insert("end", f"{b:02x}", tag)
                t.insert("end", " ")
            t.insert("end", "   " * (WIDTH - len(chunk)) + " ")
            t.insert("end", "".join(chr(b) if 32 <= b < 127 else "." for b in chunk), "ascii")
            t.insert("end", "\n")
        t.yview_moveto(yview[0])
        t.configure(state="disabled")
        self.prev, self.prev_base = data, base

    # ------------------------------------------------------------------ édition
    def _edit(self, event) -> None:
        if not self.stopped:
            return
        idx = self.text.index(f"@{event.x},{event.y}")
        line, col = (int(x) for x in idx.split("."))
        rel = col - HEX_COL
        if rel < 0 or rel >= WIDTH * 3:
            return
        addr = self.base + (line - 1) * WIDTH + rel // 3
        k = addr - self.base
        cur = self.prev[k] if k < len(self.prev) else 0
        val = simpledialog.askstring("Écrire", f"Octets à {addr:#010x} (hex) :",
                                     initialvalue=f"{cur:02x}", parent=self)
        if val:
            data = bytes.fromhex(val.replace(" ", ""))
            self.app.safe(self.session.write_memory, addr, data)
            self.app.refresh_all()

    def get_state(self) -> dict:
        return {"addr": self.addr_var.get(), "len": self.len_var.get(), "live": self.live_var.get()}

    def set_state(self, state: dict) -> None:
        self.addr_var.set(state.get("addr", self.addr_var.get()))
        self.len_var.set(state.get("len", self.len_var.get()))
        self.live_var.set(state.get("live", True))
