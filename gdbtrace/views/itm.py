"""TERM / ITM : texte et valeurs écrits par le firmware sur les ports ITM, reçus par SWO."""
from __future__ import annotations

import tkinter as tk
from tkinter import filedialog, simpledialog, ttk

from ..trace.config import SwoConfig
from ..gui.view_base import View

PORT_COLORS = ["#000000", "#2050a0", "#107010", "#a05000", "#800080", "#007070"]


def parse_ports(text: str) -> set[int] | None:
    """« 0,1,5 » → {0, 1, 5} ; « tous » ou vide → None (tous les ports)."""
    text = text.strip().lower()
    if not text or text in ("tous", "all", "*"):
        return None
    return {int(x, 0) for x in text.replace(" ", "").split(",") if x}


class ItmView(View):
    title = "ITM"
    kind = "itm"

    def __init__(self, master, app, **kw):
        super().__init__(master, app, **kw)
        src = ttk.Frame(self)
        src.pack(fill="x")
        ttk.Label(src, text="SWO TCP :").pack(side="left")
        self.addr_var = tk.StringVar(value="127.0.0.1:3443")
        ttk.Entry(src, textvariable=self.addr_var, width=18).pack(side="left", padx=2)
        ttk.Button(src, text="Connecter", command=self._connect).pack(side="left")
        ttk.Button(src, text="Fichier…", command=self._open_file).pack(side="left", padx=2)
        ttk.Button(src, text="Arrêter", command=self._stop).pack(side="left")
        self.formatter_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(src, text="Formateur TPIU", variable=self.formatter_var).pack(side="left", padx=4)
        ttk.Button(src, text="Configurer la cible…", command=self._configure).pack(side="left", padx=4)

        bar = ttk.Frame(self)
        bar.pack(fill="x")
        ttk.Label(bar, text="Ports :").pack(side="left")
        self.ports_var = tk.StringVar(value="tous")
        ttk.Entry(bar, textvariable=self.ports_var, width=10).pack(side="left", padx=2)
        ttk.Label(bar, text="texte :").pack(side="left")
        self.text_ports_var = tk.StringVar(value="0")
        e = ttk.Entry(bar, textvariable=self.text_ports_var, width=8)
        e.pack(side="left", padx=2)
        e.bind("<Return>", lambda _e: self._apply_text_ports())
        self.ts_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(bar, text="Horodatage", variable=self.ts_var).pack(side="left", padx=4)
        ttk.Button(bar, text="Effacer", command=self.clear).pack(side="left", padx=2)
        ttk.Button(bar, text="Enregistrer…", command=self._save).pack(side="left")

        self.out = tk.Text(self, font=self.mono, wrap="word", height=10, background="#fcfcf8")
        for i, c in enumerate(PORT_COLORS):
            self.out.tag_configure(f"p{i}", foreground=c)
        self.out.tag_configure("meta", foreground="#888888")
        sb = ttk.Scrollbar(self, orient="vertical", command=self.out.yview)
        self.out.configure(yscrollcommand=sb.set, state="disabled")
        self.status = ttk.Label(self, text="", foreground="#555555")
        self.status.pack(side="bottom", fill="x")
        self.out.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

        self.app.trace.line_listeners.append(self.on_line)
        self._tick()

    def destroy(self) -> None:
        if self.on_line in self.app.trace.line_listeners:
            self.app.trace.line_listeners.remove(self.on_line)
        super().destroy()

    # ------------------------------------------------------------------ réception
    def on_line(self, port: int, text: str, ts: int) -> None:
        wanted = parse_ports(self.ports_var.get())
        if wanted is not None and port not in wanted:
            return
        self.out.configure(state="normal")
        if self.ts_var.get():
            self.out.insert("end", f"{ts:>10} ", "meta")
        self.out.insert("end", f"[{port}] ", "meta")
        self.out.insert("end", text + "\n", f"p{port % len(PORT_COLORS)}")
        if int(self.out.index("end").split(".")[0]) > 20_000:
            self.out.delete("1.0", "2000.0")
        self.out.see("end")
        self.out.configure(state="disabled")

    def contents(self) -> str:
        return self.out.get("1.0", "end-1c")

    def clear(self) -> None:
        self.out.configure(state="normal")
        self.out.delete("1.0", "end")
        self.out.configure(state="disabled")

    def _tick(self) -> None:
        if not self.winfo_exists():
            return
        self.status.configure(text=self.app.trace.status())
        self.after(500, self._tick)

    # ------------------------------------------------------------------ actions
    def _apply_text_ports(self) -> None:
        ports = parse_ports(self.text_ports_var.get())
        self.app.trace.set_text_ports(ports if ports is not None else set(range(32)))

    def _connect(self) -> None:
        host, _, port = self.addr_var.get().rpartition(":")
        self._apply_text_ports()
        # ETM par SWO configuré : le flux est formaté (ETM ID 2, ITM ID 1)
        self.app.safe(self.app.trace.start_tcp, host or "127.0.0.1", int(port),
                      self.formatter_var.get() or self.app.trace.etm_swo)

    def _open_file(self) -> None:
        path = filedialog.askopenfilename(filetypes=[("Capture SWO", "*.bin *.swo *.raw"), ("Tous", "*")])
        if path:
            self._apply_text_ports()
            self.app.safe(self.app.trace.start_file, path, self.formatter_var.get())

    def _stop(self) -> None:
        self.app.trace.stop()

    def _save(self) -> None:
        path = filedialog.asksaveasfilename(defaultextension=".txt")
        if path:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(self.contents())

    def _configure(self) -> None:
        if not self.stopped:
            self.app.status.set("Arrêter la cible pour configurer la trace")
            return
        cpu = simpledialog.askinteger("Trace SWO", "Fréquence du cœur (Hz) :",
                                      initialvalue=self.app.cpu_hz, parent=self)
        if not cpu:
            return
        swo = simpledialog.askinteger("Trace SWO", "Débit SWO (Hz) :", initialvalue=2_000_000, parent=self)
        if not swo:
            return
        self.app.cpu_hz = cpu
        if getattr(self.app, "api", None) is not None:
            self.app.api.cpu_hz = cpu
        port = int(self.addr_var.get().rpartition(":")[2] or 3443)
        # avec l'ETM par SWO, le formateur TPIU reste actif : sinon l'ETM ne passerait plus
        cfg = SwoConfig(cpu_hz=cpu, swo_hz=swo, ports=0xFFFFFFFF,
                        formatter=self.formatter_var.get() or self.app.trace.etm_swo)
        log = self.app.safe(self.app.trace.configure_target, cfg, self.session,
                            self.session.target, port)
        if log:
            self.app.write_console("\n".join(log) + "\n", "gdb")
            self.app.status.set(cfg.summary())

    def get_state(self) -> dict:
        return {"addr": self.addr_var.get(), "ports": self.ports_var.get(),
                "text_ports": self.text_ports_var.get(), "ts": self.ts_var.get(),
                "formatter": self.formatter_var.get()}

    def set_state(self, state: dict) -> None:
        self.addr_var.set(state.get("addr", self.addr_var.get()))
        self.ports_var.set(state.get("ports", "tous"))
        self.text_ports_var.set(state.get("text_ports", "0"))
        self.ts_var.set(state.get("ts", False))
        self.formatter_var.set(state.get("formatter", False))
