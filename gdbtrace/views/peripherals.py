"""PER.view : registres périphériques décrits par un fichier CMSIS-SVD.

Seuls les périphériques **dépliés** sont lus. Les registres write-only ou à lecture
destructive (`readAction`) ne sont pas lus automatiquement : clic droit › « Lire
quand même ». Cible en marche, les valeurs sont rafraîchies par le canal live.
"""
from __future__ import annotations

import tkinter as tk
from tkinter import filedialog, simpledialog, ttk

from ..core import svd
from ..gui.view_base import View

PLACEHOLDER = "::loading"


class PeripheralsView(View):
    title = "Périphériques"
    kind = "peripherals"

    def __init__(self, master, app, **kw):
        super().__init__(master, app, **kw)
        bar = ttk.Frame(self)
        bar.pack(fill="x")
        self.dev_label = ttk.Label(bar, text="(pas de SVD)", width=18)
        self.dev_label.pack(side="left", padx=2)
        ttk.Button(bar, text="Charger SVD…", command=self._ask_svd).pack(side="left", padx=2)
        ttk.Label(bar, text="Filtre :").pack(side="left", padx=(8, 0))
        self.filter_var = tk.StringVar()
        self.filter_var.trace_add("write", lambda *_: self._build())
        ttk.Entry(bar, textvariable=self.filter_var, width=14).pack(side="left", padx=2)
        self.live_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(bar, text="Live", variable=self.live_var,
                        command=self._update_subscription).pack(side="left", padx=6)

        body = ttk.Frame(self)
        body.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(body, columns=("addr", "value", "info"), show="tree headings",
                                 selectmode="browse")
        for cid, label, width in (("#0", "Nom", 170), ("addr", "Adresse / bits", 100),
                                  ("value", "Valeur", 130), ("info", "Description", 240)):
            self.tree.heading(cid, text=label)
            self.tree.column(cid, width=width, stretch=cid == "info")
        self.tree.tag_configure("changed", foreground="#d00000")
        self.tree.tag_configure("unread", foreground="#999999")
        self.tree.tag_configure("periph", font=(self.mono.actual("family"), self.mono.actual("size"), "bold"))
        sb = ttk.Scrollbar(body, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.tree.bind("<<TreeviewOpen>>", self._on_open)
        self.tree.bind("<<TreeviewClose>>", self._on_close)
        self.tree.bind("<Double-1>", self._edit)
        self.tree.bind("<Button-3>", self._menu)

        self.open: set[str] = set()                 # périphériques dépliés
        self.values: dict[tuple[str, str], int] = {}
        self._build()

    # ------------------------------------------------------------------ construction
    @property
    def device(self) -> svd.Device | None:
        return self.app.device

    def _ask_svd(self) -> None:
        path = filedialog.askopenfilename(filetypes=[("CMSIS-SVD", "*.svd *.xml"), ("Tous", "*")])
        if path:
            self.app.load_svd(path)

    def on_svd_loaded(self) -> None:
        self.open.clear()
        self.values.clear()
        self._build()

    def _build(self) -> None:
        self.tree.delete(*self.tree.get_children())
        dev = self.device
        self.dev_label.configure(text=dev.name if dev else "(pas de SVD)")
        if dev is None:
            return
        flt = self.filter_var.get().strip().lower()
        for p in dev.peripherals:
            if flt and flt not in p.name.lower() and flt not in p.group.lower():
                continue
            self.tree.insert("", "end", iid=p.name, text=p.name, values=(f"{p.base:08x}", "", p.description),
                             tags=("periph",))
            self.tree.insert(p.name, "end", iid=p.name + PLACEHOLDER, text="…")
            if p.name in self.open:
                self._populate(p)
                self.tree.item(p.name, open=True)
        self._update_subscription()

    def _populate(self, p: svd.Peripheral) -> None:
        if self.tree.exists(p.name + PLACEHOLDER):
            self.tree.delete(p.name + PLACEHOLDER)
        if self.tree.get_children(p.name):
            return
        for r in p.registers:
            rid = f"{p.name}.{r.name}"
            self.tree.insert(p.name, "end", iid=rid, text=r.name,
                             values=(f"{p.address(r):08x}", "", r.description))
            for f in r.fields:
                info = f.description + ("" if f.access == svd.READ_WRITE else f"  ({f.access})")
                self.tree.insert(rid, "end", iid=f"{rid}.{f.name}", text=f.name,
                                 values=(f.bits, "", info))
        self._show_values(p, {})

    # ------------------------------------------------------------------ dépliage
    def _on_open(self, _e=None) -> None:
        item = self.tree.focus()
        p = self.device.peripheral(item) if self.device else None
        if p is None or p.name in self.open:
            return
        self.open.add(p.name)
        self._populate(p)
        if self.stopped:
            self.app.safe(self._read_stopped, p)
        self._update_subscription()

    def _on_close(self, _e=None) -> None:
        item = self.tree.focus()
        if item in self.open:
            self.open.discard(item)
            self._update_subscription()

    def expand(self, name: str) -> None:
        """Déplie un périphérique par programme (scripts, tests)."""
        if self.tree.exists(name):
            self.tree.focus(name)
            self.tree.item(name, open=True)
            self._on_open()

    # ------------------------------------------------------------------ lecture
    def _read_stopped(self, p: svd.Peripheral, regs: list[svd.Register] | None = None,
                      force: bool = False) -> None:
        new: dict[str, int] = {}
        for addr, n, rs in svd.read_blocks(p, regs, force):
            data = self.session.read_memory(addr, n)
            new.update(svd.decode_block(p, addr, data, rs))
        self._show_values(p, new)

    def refresh(self) -> None:
        if not self.stopped or self.device is None:
            return
        for name in list(self.open):
            p = self.device.peripheral(name)
            if p:
                self.app.safe(self._read_stopped, p)

    def _show_values(self, p: svd.Peripheral, new: dict[str, int]) -> None:
        for r in p.registers:
            rid = f"{p.name}.{r.name}"
            if not self.tree.exists(rid):
                continue
            key = (p.name, r.name)
            if r.name in new:
                v = new[r.name]
                changed = key in self.values and self.values[key] != v
                self.values[key] = v
                self.tree.set(rid, "value", f"{v:0{r.nbytes * 2}x}")
                self.tree.item(rid, tags=("changed",) if changed else ())
                for f in r.fields:
                    fid = f"{rid}.{f.name}"
                    old = self.values.get(key + (f.name,))
                    fv = f.extract(v)
                    self.values[key + (f.name,)] = fv
                    self.tree.set(fid, "value", "—" if f.access == svd.WRITE_ONLY else f.format(v))
                    self.tree.item(fid, tags=("changed",) if old is not None and old != fv else ())
            elif key in self.values:
                if not self.tree.set(rid, "value"):  # ligne recréée (filtre) : valeur en cache
                    v = self.values[key]
                    self.tree.set(rid, "value", f"{v:0{r.nbytes * 2}x}")
                    for f in r.fields:
                        self.tree.set(f"{rid}.{f.name}", "value",
                                      "—" if f.access == svd.WRITE_ONLY else f.format(v))
            else:
                txt = "(write-only)" if r.access == svd.WRITE_ONLY else \
                      "(non lu : lecture destructive)" if r.side_effect else ""
                self.tree.set(rid, "value", txt)
                self.tree.item(rid, tags=("unread",))

    # ------------------------------------------------------------------ live
    def _ranges(self) -> list[tuple[int, int]]:
        out = []
        for name in self.open:
            p = self.device.peripheral(name) if self.device else None
            if p:
                out += [(a, n) for a, n, _ in svd.read_blocks(p)]
        return out

    def _update_subscription(self) -> None:
        poller = self.app.poller
        if poller is None:
            return
        ranges = self._ranges()
        if self.live_var.get() and ranges:
            poller.subscribe(self, ranges, self._on_live)
        else:
            poller.unsubscribe(self)

    def on_running(self) -> None:
        self._update_subscription()

    def _on_live(self, data: dict[int, bytes]) -> None:
        for name in self.open:
            p = self.device.peripheral(name) if self.device else None
            if not p:
                continue
            new: dict[str, int] = {}
            for addr, n, rs in svd.read_blocks(p):
                if addr in data:
                    new.update(svd.decode_block(p, addr, data[addr], rs))
            self._show_values(p, new)

    def on_disconnected(self) -> None:
        if self.app.poller:
            self.app.poller.unsubscribe(self)

    def destroy(self) -> None:
        if self.app.poller:
            self.app.poller.unsubscribe(self)
        super().destroy()

    # ------------------------------------------------------------------ édition
    def _locate(self, item: str):
        parts = item.split(".")
        if self.device is None or len(parts) < 2:
            return None, None, None
        p = self.device.peripheral(parts[0])
        r = p.register(".".join(parts[1:2])) if p else None
        # noms de registres de clusters : « CLUSTER.REG »
        if p and r is None:
            for cand in p.registers:
                if item.startswith(f"{p.name}.{cand.name}"):
                    r = cand
        f = None
        if r is not None and item != f"{p.name}.{r.name}":
            fname = item[len(f"{p.name}.{r.name}."):]
            f = next((x for x in r.fields if x.name == fname), None)
        return p, r, f

    def _edit(self, _e) -> str | None:
        item = self.tree.focus()
        p, r, f = self._locate(item)
        if r is None:
            return None
        if not self.stopped:
            self.app.status.set("Arrêter la cible pour écrire un registre")
            return "break"
        if r.access == svd.READ_ONLY or (f is not None and f.access == svd.READ_ONLY):
            self.app.status.set(f"{r.name} est en lecture seule")
            return "break"
        addr = p.address(r)
        if f is None:
            cur = self.values.get((p.name, r.name), r.reset)
            val = simpledialog.askstring("Écrire", f"{p.name}.{r.name} @ {addr:#010x} =",
                                         initialvalue=f"{cur:#0{r.nbytes * 2 + 2}x}", parent=self)
            if val is None:
                return "break"
            new = int(val, 0)
        else:
            val = self._ask_field(f)
            if val is None:
                return "break"
            # lecture-modification-écriture (registre relu juste avant)
            cur = int.from_bytes(self.session.read_memory(addr, r.nbytes), "little") \
                if r.readable else self.values.get((p.name, r.name), r.reset)
            new = f.insert(cur, val)
        self.app.safe(self.session.write_memory, addr, new.to_bytes(r.nbytes, "little"))
        self.app.safe(self._read_stopped, p)
        self._check_readback(p, r, new)
        return "break"

    def _check_readback(self, p: svd.Peripheral, r: svd.Register, written: int) -> None:
        """Signale une écriture qui ne « prend » pas (bits read-only/W1C, ou cible qui
        ignore l'écriture : c'est le cas de QEMU pour les périphériques depuis gdb)."""
        if not r.readable:
            self.app.status.set(f"{p.name}.{r.name} ← {written:#x}")
            return
        got = self.values.get((p.name, r.name))
        if got is not None and got != written:
            self.app.status.set(
                f"⚠ {p.name}.{r.name} : écrit {written:#x}, relu {got:#x} "
                "(bits en lecture seule / W1C, ou écriture ignorée par la cible)")
        else:
            self.app.status.set(f"{p.name}.{r.name} ← {written:#x}")

    def _ask_field(self, f: svd.Field) -> int | None:
        if not f.enums:
            val = simpledialog.askstring("Écrire", f"{f.name} {f.bits} =", parent=self)
            return int(val, 0) if val else None
        dlg = tk.Toplevel(self)
        dlg.title(f.name)
        dlg.transient(self.winfo_toplevel())
        choices = [f"{v}: {n}" for v, n in sorted(f.enums.items())]
        var = tk.StringVar(value=choices[0])
        ttk.Label(dlg, text=f"{f.name} {f.bits}").pack(padx=8, pady=4)
        cb = ttk.Combobox(dlg, textvariable=var, values=choices, width=30)
        cb.pack(padx=8, pady=4)
        result: list[int] = []

        def ok():
            txt = var.get().split(":")[0].strip()
            try:
                result.append(int(txt, 0))
            except ValueError:
                pass
            dlg.destroy()
        ttk.Button(dlg, text="OK", command=ok).pack(pady=4)
        cb.bind("<Return>", lambda _e: ok())
        dlg.grab_set()
        dlg.wait_window()
        return result[0] if result else None

    def _menu(self, event) -> None:
        item = self.tree.identify_row(event.y)
        p, r, _f = self._locate(item)
        if r is None:
            return
        self.tree.focus(item)
        m = tk.Menu(self, tearoff=False)
        if r.access != svd.WRITE_ONLY:
            m.add_command(label=f"Lire {r.name} quand même (effet de bord possible)",
                          command=lambda: self.stopped and self.app.safe(self._read_stopped, p, [r], True))
        m.add_command(label="Écrire…", command=lambda: self._edit(None))
        m.add_command(label="Afficher en mémoire",
                      command=lambda: self.app.show_memory(p.address(r)))
        m.tk_popup(event.x_root, event.y_root)

    # ------------------------------------------------------------------ état
    def get_state(self) -> dict:
        return {"open": sorted(self.open), "filter": self.filter_var.get(),
                "live": self.live_var.get(), "svd": self.app.svd_path}

    def set_state(self, state: dict) -> None:
        if state.get("svd") and not self.app.device:
            self.app.safe(self.app.load_svd, state["svd"])
        self.live_var.set(state.get("live", True))
        self.open = set(state.get("open", []))
        self.filter_var.set(state.get("filter", ""))  # reconstruit l'arbre
