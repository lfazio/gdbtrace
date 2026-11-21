"""List.Mix / List.Asm : vues source et désassemblage, breakpoints par double-clic."""
from __future__ import annotations

import os
import tkinter as tk
from tkinter import ttk

from ..gui.view_base import View

BP_MARK = "●"


class _CodeText(tk.Text):
    def __init__(self, master, font, **kw):
        super().__init__(master, font=font, wrap="none", background="#ffffff", **kw)
        self.tag_configure("cov_hit", background="#d6f2d6")     # couverture ETM : exécutée
        self.tag_configure("cov_miss", background="#f6d6d6")    # code jamais exécuté
        self.tag_configure("current", background="#fff2a8")
        self.tag_configure("bp", foreground="#d00000")
        self.tag_configure("gutter", foreground="#909090")
        self.tag_configure("func", foreground="#3050a0", font=(font.actual("family"), font.actual("size"), "bold"))
        self.configure(state="disabled", cursor="arrow")


class SourceView(View):
    title = "Source"
    kind = "source"

    def __init__(self, master, app, **kw):
        super().__init__(master, app, **kw)
        bar = ttk.Frame(self)
        bar.pack(fill="x")
        self.file_var = tk.StringVar()
        self.files = ttk.Combobox(bar, textvariable=self.file_var, state="readonly", width=60)
        self.files.pack(side="left", padx=2, pady=1)
        self.files.bind("<<ComboboxSelected>>", lambda _e: self.show_file(self.file_var.get()))
        self.follow = tk.BooleanVar(value=True)
        ttk.Checkbutton(bar, text="Suivre PC", variable=self.follow).pack(side="left", padx=6)
        self.text = _CodeText(self, self.mono)
        sb = ttk.Scrollbar(self, orient="vertical", command=self.text.yview)
        self.text.configure(yscrollcommand=sb.set)
        self.text.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.text.bind("<Double-1>", self._toggle_bp)
        self.current_file: str | None = None
        self.pc_line: tuple[str, int] | None = None
        self._populate_files()

    def _populate_files(self) -> None:
        if self.app.symbols:
            self.files["values"] = self.app.symbols.source_files()

    def show_file(self, path: str, line: int | None = None) -> None:
        if path != self.current_file:
            try:
                with open(path, encoding="utf-8", errors="replace") as fh:
                    lines = fh.read().splitlines()
            except OSError:
                lines = [f"<{path} introuvable>"]
            t = self.text
            t.configure(state="normal")
            t.delete("1.0", "end")
            for i, src in enumerate(lines, 1):
                t.insert("end", f"  {i:5d}  ", "gutter")
                t.insert("end", src + "\n")
            t.configure(state="disabled")
            self.current_file = path
            self.file_var.set(path)
            self.title = f"Source: {os.path.basename(path)}"
        self._decorate()
        if line:
            self.text.see(f"{line}.0")

    def _decorate(self) -> None:
        t = self.text
        t.configure(state="normal")
        t.tag_remove("current", "1.0", "end")
        t.tag_remove("bp", "1.0", "end")
        # retirer les marques précédentes (colonne 0)
        for i in range(1, int(t.index("end").split(".")[0])):
            if t.get(f"{i}.0") == BP_MARK:
                t.delete(f"{i}.0")
                t.insert(f"{i}.0", " ", "gutter")
        if self.current_file and self.connected:
            for bp in self.app.cached_breakpoints:
                if bp.line and bp.file and self._same(bp.file):
                    t.delete(f"{bp.line}.0")
                    t.insert(f"{bp.line}.0", BP_MARK, "bp")
                    t.tag_add("bp", f"{bp.line}.0", f"{bp.line}.end")
        if self.pc_line and self.pc_line[0] == self.current_file:
            n = self.pc_line[1]
            t.tag_add("current", f"{n}.0", f"{n + 1}.0")
        self._decorate_coverage()
        t.configure(state="disabled")

    def _decorate_coverage(self) -> None:
        """Marge colorée selon la couverture ETM (vue Trace, « Couverture dans la source »)."""
        t = self.text
        t.tag_remove("cov_hit", "1.0", "end")
        t.tag_remove("cov_miss", "1.0", "end")
        etm = self.app.trace.etm
        if not getattr(self.app, "coverage_in_source", False) or etm is None or not self.current_file:
            return
        key = (self.current_file, etm.generation)
        if getattr(self, "_cov_key", None) != key:
            self._cov_cache = etm.line_coverage(self.current_file)
            self._cov_key = key
        for line, hit in self._cov_cache.items():
            t.tag_add("cov_hit" if hit else "cov_miss", f"{line}.0", f"{line}.8")

    def _same(self, name: str) -> bool:
        return bool(self.current_file) and (name == self.current_file
                                            or os.path.basename(name) == os.path.basename(self.current_file))

    def refresh(self) -> None:
        self._populate_files()
        if not self.stopped:
            return
        fr = self.app.current_frame
        if fr and fr.fullname and fr.line:
            self.pc_line = (fr.fullname, fr.line)
            if self.follow.get() or self.current_file is None:
                self.show_file(fr.fullname, fr.line)
                return
        else:
            self.pc_line = None
        self._decorate()

    def on_running(self) -> None:
        self.pc_line = None
        self._decorate()

    def _toggle_bp(self, event) -> str:
        if not self.current_file or not self.connected:
            return "break"
        line = int(self.text.index(f"@{event.x},{event.y}").split(".")[0])
        self.app.toggle_breakpoint_at(file=self.current_file, line=line)
        return "break"

    def get_state(self) -> dict:
        return {"file": self.current_file, "follow": self.follow.get()}

    def set_state(self, state: dict) -> None:
        self.follow.set(state.get("follow", True))
        if state.get("file"):
            self.show_file(state["file"])


class DisasmView(View):
    title = "Désassemblage"
    kind = "disasm"

    def __init__(self, master, app, **kw):
        super().__init__(master, app, **kw)
        bar = ttk.Frame(self)
        bar.pack(fill="x")
        ttk.Label(bar, text="Adresse :").pack(side="left")
        self.addr_var = tk.StringVar(value="")
        e = ttk.Entry(bar, textvariable=self.addr_var, width=24)
        e.pack(side="left", padx=2, pady=1)
        e.bind("<Return>", lambda _e: self._goto())
        self.follow = tk.BooleanVar(value=True)
        ttk.Checkbutton(bar, text="Suivre PC", variable=self.follow).pack(side="left", padx=6)
        self.text = _CodeText(self, self.mono)
        sb = ttk.Scrollbar(self, orient="vertical", command=self.text.yview)
        self.text.configure(yscrollcommand=sb.set)
        self.text.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.text.bind("<Double-1>", self._toggle_bp)
        self.line_addr: dict[int, int] = {}
        self.window: tuple[int, int] | None = None

    def _goto(self) -> None:
        s = self.addr_var.get().strip()
        if not s or not self.stopped:
            return
        try:
            addr = int(s, 0)
        except ValueError:
            addr = self.session.address_of(s)
        self.follow.set(False)
        self._show(addr)

    def _show(self, center: int) -> None:
        syms = self.app.symbols
        f = syms.function_at(center) if syms else None
        if f and f.size and f.size < 4096:
            start, end = f.start, f.end
        else:
            start, end = max(0, center - 32), center + 64
        if self.window != (start, end):
            insns = self.session.disassemble(start, end)
            t = self.text
            t.configure(state="normal")
            t.delete("1.0", "end")
            self.line_addr.clear()
            last_func = None
            n = 1
            for ins in insns:
                fn = ins.get("func-name")
                if fn and fn != last_func:
                    t.insert("end", f"{fn}:\n", "func")
                    n += 1
                    last_func = fn
                t.insert("end", "  ", "gutter")
                t.insert("end", f"{ins['address']:08x}  {ins.get('opcodes', ''):<12} {ins['inst'].replace(chr(9), ' ')}\n")
                self.line_addr[n] = ins["address"]
                n += 1
            t.configure(state="disabled")
            self.window = (start, end)
        self._decorate(center)

    def _decorate(self, pc: int | None) -> None:
        t = self.text
        t.configure(state="normal")
        t.tag_remove("current", "1.0", "end")
        bp_addrs = set()
        for bp in self.app.cached_breakpoints:
            if bp.addr and bp.addr.startswith("0x"):
                bp_addrs.add(int(bp.addr, 16))
        for line, addr in self.line_addr.items():
            mark = BP_MARK if addr in bp_addrs else " "
            t.delete(f"{line}.0")
            t.insert(f"{line}.0", mark, "bp" if addr in bp_addrs else "gutter")
            if addr == pc:
                t.tag_add("current", f"{line}.0", f"{line + 1}.0")
                t.see(f"{line}.0")
        t.configure(state="disabled")

    def refresh(self) -> None:
        if not self.stopped:
            return
        fr = self.app.current_frame
        if fr is None:
            return
        if self.follow.get() or self.window is None:
            self._show(fr.pc)
        else:
            self._decorate(fr.pc)

    def on_running(self) -> None:
        self._decorate(None)

    def _toggle_bp(self, event) -> str:
        line = int(self.text.index(f"@{event.x},{event.y}").split(".")[0])
        if line in self.line_addr and self.connected:
            self.app.toggle_breakpoint_at(addr=self.line_addr[line])
        return "break"
