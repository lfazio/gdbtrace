"""List.Mix / List.Asm : vues source et désassemblage, breakpoints par double-clic."""
from __future__ import annotations

import os
import tkinter as tk
from tkinter import ttk

from ..gui.view_base import View

BP_MARK = "●"
COV_TAG = {"ok": "cov_ok", "partiel": "cov_partial", "jamais": "cov_never"}
COV_MARK = {"ok": "✔", "partiel": "½", "jamais": "✘"}


class _CodeText(tk.Text):
    def __init__(self, master, font, **kw):
        super().__init__(master, font=font, wrap="none", background="#ffffff", **kw)
        self.tag_configure("current", background="#fff2a8")
        self.tag_configure("bp", foreground="#d00000")
        self.tag_configure("gutter", foreground="#909090")
        self.tag_configure("func", foreground="#3050a0", font=(font.actual("family"), font.actual("size"), "bold"))
        self.tag_configure("src", foreground="#2f6f3f", background="#f4f7f2")       # List.Mix : ligne C
        self.tag_configure("srcno", foreground="#7a9a80", background="#f4f7f2")
        # couverture (après les autres : prioritaires) : ok, partiel, jamais ; note d'instruction
        self.tag_configure("cov_ok", background="#d6f2d6")
        self.tag_configure("cov_partial", background="#fbe7b5")
        self.tag_configure("cov_never", background="#f6d6d6")
        self.tag_configure("covnote", foreground="#8a5a00")
        bold = (font.actual("family"), font.actual("size"), "bold")
        self.tag_configure("covmark_ok", foreground="#1d6b2f", font=bold)
        self.tag_configure("covmark_partiel", foreground="#8a5a00", font=bold)
        self.tag_configure("covmark_jamais", foreground="#a01f1f", font=bold)
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
        """Marge : couverture de chaque ligne (✔ ok, ½ partiel, ✘ jamais), d'après la trace ETM
        (vue Couverture, « Annoter Source et Désassemblage »)."""
        t = self.text
        for tag in COV_TAG.values():
            t.tag_remove(tag, "1.0", "end")
        for line in getattr(self, "_cov_marked", []):
            if t.get(f"{line}.1") in COV_MARK.values():
                t.delete(f"{line}.1")
                t.insert(f"{line}.1", " ", "gutter")
        self._cov_marked: list[int] = []
        etm = self.app.trace.etm
        if not getattr(self.app, "coverage_in_source", False) or etm is None or not self.current_file:
            return
        key = (self.current_file, id(etm.cov), etm.cov.generation)
        if getattr(self, "_cov_key", None) != key:
            self._cov_cache = {ln: c.status for ln, c in etm.cov.lines(self.current_file).items()}
            self._cov_key = key
        last = int(t.index("end").split(".")[0]) - 1
        for line, st in self._cov_cache.items():
            if line > last:
                continue
            t.delete(f"{line}.1")
            t.insert(f"{line}.1", COV_MARK[st], ("gutter", COV_TAG[st], f"covmark_{st}"))
            t.tag_add(COV_TAG[st], f"{line}.0", f"{line}.8")
            self._cov_marked.append(line)

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

    def on_program_changed(self) -> None:
        self._populate_files()
        path, self.current_file = self.current_file, None       # relu : il a pu changer
        if path:
            self.show_file(path)

    def get_state(self) -> dict:
        return {"file": self.current_file, "follow": self.follow.get()}

    def set_state(self, state: dict) -> None:
        self.follow.set(state.get("follow", True))
        if state.get("file"):
            self.show_file(state["file"])


_SOURCE_CACHE: dict[str, list[str]] = {}


def clear_source_cache() -> None:
    """Après une recompilation, les sources ont pu changer."""
    _SOURCE_CACHE.clear()


def source_lines(path: str) -> list[str]:
    """Lignes d'un fichier source (mises en cache) ; liste vide s'il est introuvable."""
    if path not in _SOURCE_CACHE:
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                _SOURCE_CACHE[path] = fh.read().splitlines()
        except OSError:
            _SOURCE_CACHE[path] = []
    return _SOURCE_CACHE[path]


class DisasmView(View):
    """List.Asm / List.Mix : désassemblage, avec option source C intercalée."""
    title = "Désassemblage"
    kind = "disasm"
    MAX_GAP = 6          # lignes C sans code (déclarations, commentaires) montrées en plus

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
        self.mixed = tk.BooleanVar(value=True)
        ttk.Checkbutton(bar, text="Source mêlée", variable=self.mixed,
                        command=self._toggle_mixed).pack(side="left", padx=6)
        self.text = _CodeText(self, self.mono)
        sb = ttk.Scrollbar(self, orient="vertical", command=self.text.yview)
        self.text.configure(yscrollcommand=sb.set)
        self.text.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.text.bind("<Double-1>", self._toggle_bp)
        self.line_addr: dict[int, int] = {}
        self.line_src: dict[int, tuple[str, int]] = {}     # ligne de texte -> (fichier, ligne C)
        self.window: tuple[int, int] | None = None
        self._center: int | None = None
        self._pc: int | None = None

    def _toggle_mixed(self) -> None:
        self.window = None
        if self._center is not None and self.stopped:
            self._show(self._center)

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
        self._center = center
        if self.window != (start, end):
            insns = self.session.disassemble(start, end)
            mixed = self.mixed.get() and syms is not None and bool(syms.lines)
            t = self.text
            t.configure(state="normal")
            t.delete("1.0", "end")
            self.line_addr.clear()
            self.line_src.clear()
            last_func = None
            last_src: tuple[str, int] | None = None
            n = 1
            for ins in insns:
                fn = ins.get("func-name")
                if fn and fn != last_func:
                    t.insert("end", f"{fn}:\n", "func")
                    n += 1
                    last_func = fn
                if mixed:
                    le = syms.line_at(ins["address"])
                    if le is not None and (le.file, le.line) != last_src:
                        first = le.line
                        if last_src and last_src[0] == le.file and 0 < le.line - last_src[1] <= self.MAX_GAP:
                            first = last_src[1] + 1          # lignes intermédiaires sans code
                        src = source_lines(le.file)
                        for ln in range(first, le.line + 1):
                            text = src[ln - 1].rstrip() if 0 < ln <= len(src) else ""
                            t.insert("end", f"  {os.path.basename(le.file)}:{ln:<5}", "srcno")
                            t.insert("end", f" {text}\n", "src")
                            self.line_src[n] = (le.file, ln)
                            n += 1
                        last_src = (le.file, le.line)
                t.insert("end", "  ", "gutter")
                t.insert("end", f"{ins['address']:08x}  {ins.get('opcodes', ''):<12} {ins['inst'].replace(chr(9), ' ')}\n")
                self.line_addr[n] = ins["address"]
                n += 1
            t.configure(state="disabled")
            self.window = (start, end)
        self._decorate(center)

    def _decorate(self, pc: int | None) -> None:
        self._pc = pc
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
        self._decorate_coverage()
        t.configure(state="disabled")

    def _decorate_coverage(self) -> None:
        """Couverture : adresse colorée (ok, partiel, jamais) et, pour une instruction partielle,
        l'issue manquante en fin de ligne ; lignes C intercalées colorées comme dans la Source."""
        from ..trace.coverage import I_NEVER, I_OK, NEVER, OK, PARTIAL, TAG_HELP
        t = self.text
        ranges = t.tag_ranges("covnote")
        for i in range(len(ranges) - 2, -1, -2):
            t.delete(ranges[i], ranges[i + 1])
        for tag in COV_TAG.values():
            t.tag_remove(tag, "1.0", "end")
        etm = self.app.trace.etm
        if not getattr(self.app, "coverage_in_source", False) or etm is None:
            return
        cov = etm.cov
        for line, addr in self.line_addr.items():
            if not cov.measured(addr):
                continue
            tag = cov.tag(addr)
            st = OK if tag == I_OK else NEVER if tag == I_NEVER else PARTIAL
            t.tag_add(COV_TAG[st], f"{line}.2", f"{line}.10")
            if st == PARTIAL:
                t.insert(f"{line}.end", f"   ◂ {TAG_HELP[tag]}", "covnote")
        files: dict[str, dict] = {}
        for row, (file, ln) in self.line_src.items():
            if file not in files:
                files[file] = {k: c.status for k, c in cov.lines(file).items()}
            st = files[file].get(ln)
            if st:
                t.tag_add(COV_TAG[st], f"{row}.0", f"{row}.end")

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
        elif line in self.line_src and self.connected:          # ligne C : breakpoint source
            file, ln = self.line_src[line]
            self.app.toggle_breakpoint_at(file=file, line=ln)
        return "break"

    def on_program_changed(self) -> None:
        self.window = None
        self._center = None

    def get_state(self) -> dict:
        return {"mixed": self.mixed.get()}

    def set_state(self, state: dict) -> None:
        self.mixed.set(state.get("mixed", True))
