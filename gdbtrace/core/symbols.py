"""Lecture directe de l'ELF/DWARF (pyelftools), sans passer par gdb.

Sert aux vues qui ont besoin de beaucoup de résolutions adresse → symbole/ligne
(profilage, couverture, trace) : interroger gdb pour chaque échantillon serait
trop lent.
"""
from __future__ import annotations

import bisect
import os
from dataclasses import dataclass

from elftools.elf.elffile import ELFFile
from elftools.elf.sections import SymbolTableSection


@dataclass(frozen=True)
class Function:
    name: str
    start: int
    size: int

    @property
    def end(self) -> int:
        return self.start + self.size


@dataclass(frozen=True)
class LineEntry:
    address: int
    file: str
    line: int


class SymbolTable:
    def __init__(self, elf_path: str):
        self.path = elf_path
        self.functions: list[Function] = []
        self.objects: dict[str, tuple[int, int]] = {}
        self.lines: list[LineEntry] = []
        self._load()
        self._func_starts = [f.start for f in self.functions]
        self._line_addrs = [e.address for e in self.lines]

    def _load(self) -> None:
        with open(self.path, "rb") as fh:
            elf = ELFFile(fh)
            for sec in elf.iter_sections():
                if not isinstance(sec, SymbolTableSection):
                    continue
                for sym in sec.iter_symbols():
                    kind = sym["st_info"]["type"]
                    if not sym.name or sym["st_shndx"] == "SHN_UNDEF":
                        continue
                    if kind == "STT_FUNC":
                        # bit 0 = Thumb sur ARM, ce n'est pas une partie de l'adresse
                        self.functions.append(Function(sym.name, sym["st_value"] & ~1, sym["st_size"]))
                    elif kind == "STT_OBJECT":
                        self.objects[sym.name] = (sym["st_value"], sym["st_size"])
            self.functions.sort(key=lambda f: f.start)
            if elf.has_dwarf_info():
                self._load_lines(elf)

    def _load_lines(self, elf: ELFFile) -> None:
        dwarf = elf.get_dwarf_info()
        entries: dict[int, LineEntry] = {}
        for cu in dwarf.iter_CUs():
            lp = dwarf.line_program_for_CU(cu)
            if lp is None:
                continue
            comp_dir = cu.get_top_DIE().attributes.get("DW_AT_comp_dir")
            comp_dir = comp_dir.value.decode() if comp_dir else ""
            files = lp["file_entry"]
            dirs = lp["include_directory"]
            v5 = lp.header.version >= 5
            for ent in lp.get_entries():
                st = ent.state
                if st is None or st.end_sequence:
                    continue
                idx = st.file if v5 else st.file - 1
                if not 0 <= idx < len(files):
                    continue
                fe = files[idx]
                name = fe.name.decode()
                d = fe.dir_index if v5 else fe.dir_index - 1
                if not os.path.isabs(name):
                    base = dirs[d].decode() if 0 <= d < len(dirs) else ""
                    if not os.path.isabs(base):
                        base = os.path.join(comp_dir, base)
                    name = os.path.normpath(os.path.join(base, name))
                entries[st.address] = LineEntry(st.address, name, st.line)
        self.lines = sorted(entries.values(), key=lambda e: e.address)

    # ------------------------------------------------------------------ requêtes
    def function_at(self, addr: int) -> Function | None:
        i = bisect.bisect_right(self._func_starts, addr) - 1
        if i >= 0:
            f = self.functions[i]
            if f.start <= addr < max(f.end, f.start + 1):
                return f
        return None

    def line_at(self, addr: int) -> LineEntry | None:
        i = bisect.bisect_right(self._line_addrs, addr) - 1
        return self.lines[i] if i >= 0 else None

    def function(self, name: str) -> Function | None:
        return next((f for f in self.functions if f.name == name), None)

    def source_files(self) -> list[str]:
        return sorted({e.file for e in self.lines})

    def addresses_for_line(self, file: str, line: int) -> list[int]:
        return [e.address for e in self.lines if e.file == file and e.line == line]
