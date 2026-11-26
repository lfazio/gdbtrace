"""Data.dump : dump mémoire hexadécimal, octets modifiés en rouge, édition, rafraîchissement.
Menu Outils : Data.SAVE, Data.LOAD, Data.Set, Data.Find, Data.ComPare (`core/memtools.py`)."""
from __future__ import annotations

import os
import tkinter as tk
from tkinter import filedialog, simpledialog, ttk

from ..core import memtools

IMAGE_TYPES = [("Binaire", "*.bin"), ("Intel HEX", "*.hex *.ihex"), ("S-record", "*.srec *.s19 *.s28 *.s37 *.mot"),
               ("ELF", "*.elf *.axf *.out"), ("Tous", "*")]

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
        tools = ttk.Menubutton(bar, text="Outils")
        menu = tk.Menu(tools, tearoff=False)
        menu.add_command(label="Enregistrer la plage… (bin, hex, srec)", command=self._ask_save)
        menu.add_command(label="Charger une image en RAM…", command=self._ask_load)
        menu.add_command(label="Remplir la plage…", command=self._ask_fill)
        menu.add_command(label="Chercher…", command=self._ask_find)
        menu.add_command(label="Comparer à une image…", command=self._ask_compare)
        tools.configure(menu=menu)
        tools.pack(side="left", padx=2)

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
        """0x20000000 (SRAM d'un Cortex-M3/M4/M7) n'est pas lisible partout : programme natif,
        puce dont la RAM est ailleurs (STM32N6 : AXISRAM en 0x34000000). La vue part alors de
        la première variable globale du programme, désignée par son nom."""
        sym = self.app.symbols
        if not (sym and self.addr_var.get().strip() == DEFAULT_ADDR):
            return
        sram = int(DEFAULT_ADDR, 16)
        if not self.app.native and any(sram <= a < sram + (1 << 24) for a, _n in sym.objects.values()):
            return                              # la RAM du programme est bien là
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
        args: list = []                     # un seul insert : (texte, étiquette) en paires
        for off in range(0, len(data), WIDTH):
            chunk = data[off:off + WIDTH]
            args += [f"{base + off:08x}  ", "addr"]
            for i, b in enumerate(chunk):
                k = off + i
                tag = "changed" if prev and k < len(prev) and prev[k] != b else ""
                args += [f"{b:02x}", tag, " ", ""]
            args += ["   " * (WIDTH - len(chunk)) + " ", "",
                     "".join(chr(b) if 32 <= b < 127 else "." for b in chunk), "ascii", "\n", ""]
        if args:
            t.insert("end", *args)
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

    # ------------------------------------------------------------------ outils
    def _log(self, text: str, error: bool = False) -> None:
        self.app.write_console(text + "\n", "err" if error else "gdb")
        self.app.status.set(text)

    def _read(self, addr: int, n: int) -> bytes:
        """Cible arrêtée : gdb ; en marche : canal mémoire (fil de travail, jamais le GUI)."""
        if self.stopped:
            return self.session.read_memory(addr, n)
        live = self.app.api.live if self.app.api is not None else None
        if live is None:
            raise RuntimeError("cible en marche sans canal mémoire : arrêter d'abord")
        return live.read(addr, n)

    def _ask_range(self, title: str) -> tuple[int, int] | None:
        """Plage « adresse taille » (adresse : nombre, variable, expression) ; la fenêtre de
        la vue par défaut."""
        base, n = self._window()
        text = simpledialog.askstring(title, "Adresse et taille (ex. 0x20000000 256, ou une variable) :",
                                      initialvalue=f"{base:#x} {n}", parent=self)
        if not text:
            return None
        parts = text.split()
        try:
            addr = int(parts[0], 0)
        except ValueError:
            if not self.stopped:
                self._log("cible en marche : adresse numérique attendue", True)
                return None
            addr = self.session.address_of_expr(parts[0])
            if len(parts) == 1:
                return addr, self.session.sizeof(parts[0])
        return addr, int(parts[1], 0) if len(parts) > 1 else n

    def _ask_save(self) -> None:
        rng = self._ask_range("Enregistrer la plage")
        if rng is None:
            return
        path = filedialog.asksaveasfilename(parent=self, title="Fichier", defaultextension=".bin",
                                            filetypes=IMAGE_TYPES[:3] + IMAGE_TYPES[4:])
        if not path:
            return
        addr, n = rng
        self.app.run_action(lambda: memtools.save(self._read, addr, n, path),
                            then=lambda fmt: self._log(f"{n} octets de {addr:#x} écrits dans "
                                                       f"{os.path.basename(path)} ({fmt})"))

    def _ask_load(self) -> None:
        if not self.stopped:
            self._log("charger une image : arrêter la cible d'abord", True)
            return
        path = filedialog.askopenfilename(parent=self, title="Image à charger en RAM", filetypes=IMAGE_TYPES)
        if not path:
            return
        at = None
        if memtools.image_format(path) == "bin":
            text = simpledialog.askstring("Binaire brut", "Adresse de chargement :",
                                          initialvalue=f"{self.base:#x}", parent=self)
            if not text:
                return
            at = int(text, 0)

        def done(segs) -> None:
            self._log(", ".join(f"{n} octets à {a:#x}" for a, n in segs) + f" depuis {os.path.basename(path)}")
            self.refresh()
        self.app.run_action(lambda: memtools.load(self.session.write_memory, path, at), then=done)

    def _ask_fill(self) -> None:
        if not self.stopped:
            self._log("remplir : arrêter la cible d'abord", True)
            return
        rng = self._ask_range("Remplir la plage")
        if rng is None:
            return
        pat = simpledialog.askstring("Remplir", "Motif (octets hexadécimaux « de ad be ef », ou "
                                     "nombre 0x…, ou \"texte\") :", initialvalue="00", parent=self)
        if pat is None:
            return
        try:
            pattern = parse_pattern(pat)
        except ValueError as exc:
            self._log(f"motif : {exc}", True)
            return
        addr, n = rng
        self.app.run_action(lambda: memtools.fill(self.session.write_memory, addr, n, pattern),
                            then=lambda _r: (self._log(f"{n} octets remplis à {addr:#x}"), self.refresh()))

    def _ask_find(self) -> None:
        rng = self._ask_range("Chercher dans la plage")
        if rng is None:
            return
        pat = simpledialog.askstring("Chercher", "Motif (octets hexadécimaux « de ad be ef », ou "
                                     "nombre 0x… petit-boutiste, ou \"texte\") :", parent=self)
        if not pat:
            return
        try:
            pattern = parse_pattern(pat)
        except ValueError as exc:
            self._log(f"motif : {exc}", True)
            return
        addr, n = rng

        def done(found: list[int]) -> None:
            if not found:
                self._log(f"motif absent de [{addr:#x}, {addr + n:#x})")
                return
            more = " …" if len(found) >= 100 else ""
            self._log(f"{len(found)} occurrence(s) : " + ", ".join(f"{a:#x}" for a in found[:16]) + more)
            self.addr_var.set(f"{found[0]:#x}")
            self.refresh()
        self.app.run_action(lambda: memtools.find(self._read, addr, n, pattern), then=done)

    def _ask_compare(self) -> None:
        path = filedialog.askopenfilename(parent=self, title="Image à comparer à la mémoire", filetypes=IMAGE_TYPES)
        if not path:
            return
        at = None
        if memtools.image_format(path) == "bin":
            text = simpledialog.askstring("Binaire brut", "Adresse de l'image :",
                                          initialvalue=f"{self.base:#x}", parent=self)
            if not text:
                return
            at = int(text, 0)

        def done(res) -> None:
            self._log(f"{os.path.basename(path)} : {res.summary()}", not res.same)
            if res.first:
                self.addr_var.set(f"{res.first[0][0] & ~(WIDTH - 1):#x}")
                self.refresh()
        self.app.run_action(lambda: memtools.compare(self._read, path, at), then=done)


def parse_pattern(text: str) -> bytes:
    """Motif saisi : "texte" (UTF-8), 0x… (entier petit-boutiste), ou octets hexadécimaux."""
    text = text.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        return text[1:-1].encode("utf-8")
    if text.lower().startswith("0x") and " " not in text:
        return memtools.to_pattern(int(text, 16))
    try:
        return bytes.fromhex(text)
    except ValueError:
        raise ValueError("octets hexadécimaux, 0x…, ou \"texte\" attendus") from None
