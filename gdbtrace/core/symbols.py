"""Lecture directe de l'ELF/DWARF (pyelftools), sans passer par gdb.

Sert aux vues qui ont besoin de beaucoup de résolutions adresse → symbole/ligne
(profilage, couverture, trace) : interroger gdb pour chaque échantillon serait
trop lent.
"""
from __future__ import annotations

import bisect
import fnmatch
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
    """Symboles et lignes de l'ELF, et de ceux ajoutés (`extra` : [(chemin, décalage)], par
    exemple un bootloader : `Session.add_symbols`)."""

    def __init__(self, elf_path: str, extra=()):
        self.path = elf_path
        self.extra: list[tuple[str, int]] = list(extra)
        self.functions: list[Function] = []
        self.objects: dict[str, tuple[int, int]] = {}
        self.lines: list[LineEntry] = []
        #: symboles de correspondance ARM ($t code Thumb, $a code ARM, $d données), triés :
        #: les « literal pools » ($d) au milieu du code ne sont pas des instructions
        self.mapping: list[tuple[int, str]] = []
        self._load()
        self._func_starts = [f.start for f in self.functions]
        self._line_addrs = [e.address for e in self.lines]
        self._map_addrs = [a for a, _ in self.mapping]

    def is_data(self, addr: int) -> bool:
        """Vrai si `addr` est dans une zone de données ($d) : constantes, tables de saut."""
        i = bisect.bisect_right(self._map_addrs, addr) - 1
        return i >= 0 and self.mapping[i][1] == "d"

    def _load(self) -> None:
        for path, offset in [(self.path, 0)] + self.extra:
            self._load_elf(path, offset)
        # à adresse égale, l'alias qui a une taille en dernier : c'est lui que trouve
        # function_at() (ex. __aeabi_uidiv, taille 0, alias de __udivsi3)
        self.functions.sort(key=lambda f: (f.start, f.size))
        self.mapping.sort()
        self.lines.sort(key=lambda e: e.address)

    def _load_elf(self, path: str, offset: int) -> None:
        with open(path, "rb") as fh:
            elf = ELFFile(fh)
            for sec in elf.iter_sections():
                if not isinstance(sec, SymbolTableSection):
                    continue
                for sym in sec.iter_symbols():
                    kind = sym["st_info"]["type"]
                    if not sym.name or sym["st_shndx"] == "SHN_UNDEF":
                        continue
                    value = sym["st_value"] + offset
                    if sym.name[:2] in ("$t", "$a", "$d") and (len(sym.name) == 2 or sym.name[2] == "."):
                        self.mapping.append((value, sym.name[1]))
                        continue
                    if kind == "STT_FUNC":
                        # bit 0 = Thumb sur ARM, ce n'est pas une partie de l'adresse
                        self.functions.append(Function(sym.name, value & ~1, sym["st_size"]))
                    elif kind == "STT_OBJECT" and sym.name not in self.objects:
                        self.objects[sym.name] = (value, sym["st_size"])
            if elf.has_dwarf_info():
                self._load_lines(elf, offset)

    def _load_lines(self, elf: ELFFile, offset: int = 0) -> None:
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
                entries[st.address + offset] = LineEntry(st.address + offset, name, st.line)
        self.lines.extend(entries.values())

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


def symbol_rows(sym, pattern: str = "", kind: str = "tout") -> list[dict]:
    """Lignes du navigateur : [{nom, genre, adresse, taille, fichier, ligne}], triées par nom.
    `pattern` : sous-chaîne, ou motif avec * et ? ; sans casse. `kind` : tout, fonctions,
    variables."""
    if sym is None:
        return []
    pat = pattern.strip().lower()
    glob = any(c in pat for c in "*?[")

    def match(name: str) -> bool:
        n = name.lower()
        return not pat or (fnmatch.fnmatchcase(n, pat) if glob else pat in n)
    rows: list[dict] = []
    if kind in ("tout", "fonctions"):
        seen: set[str] = set()
        for f in sym.functions:
            if f.name in seen or not match(f.name):
                continue
            seen.add(f.name)
            line = sym.line_at(f.start)
            rows.append({"nom": f.name, "genre": "fonction", "adresse": f.start, "taille": f.size,
                         "fichier": line.file if line else None, "ligne": line.line if line else None})
    if kind in ("tout", "variables"):
        for name, (addr, size) in sym.objects.items():
            if match(name):
                rows.append({"nom": name, "genre": "variable", "adresse": addr, "taille": size,
                             "fichier": None, "ligne": None})
    rows.sort(key=lambda r: (r["nom"].lower(), r["adresse"]))
    return rows
