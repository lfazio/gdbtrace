"""Parseur CMSIS-SVD (description des registres périphériques d'un microcontrôleur).

Volontairement autonome (xml.etree) : gère `derivedFrom` (périphériques, clusters,
registres), les tableaux `dim/dimIncrement/dimIndex`, les clusters (aplatis),
l'héritage des propriétés `size/access/resetValue`, les trois notations de champs
(`bitOffset+bitWidth`, `lsb+msb`, `bitRange`), les énumérations et `readAction`.
"""
from __future__ import annotations

import copy
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

READ_ONLY, WRITE_ONLY, READ_WRITE = "read-only", "write-only", "read-write"


_SCALE = {"k": 1 << 10, "m": 1 << 20, "g": 1 << 30, "t": 1 << 40}


def parse_int(text: str | None, default: int | None = None) -> int | None:
    """Entier SVD (scaledNonNegativeInteger) : 0x.. hexadécimal, #binaire
    (x = indifférent, pris à 0), sinon décimal -- zéros en tête permis :
    le SVD STM32F746 de ST écrit `00000010`, que int(t, 0) refusait --
    et un suffixe k/m/g/t optionnel."""
    if text is None:
        return default
    t = text.strip().lower()
    if not t:
        return default
    if t.startswith("#"):
        return int(t[1:].replace("x", "0"), 2)
    if t.startswith("0b"):
        return int(t[2:].replace("x", "0"), 2)
    if t.startswith("+"):
        t = t[1:]
    scale = 1
    if t[-1] in _SCALE and not t.startswith("0x"):
        scale, t = _SCALE[t[-1]], t[:-1]
    return (int(t, 16) if t.startswith("0x") else int(t, 10)) * scale


@dataclass
class Field:
    name: str
    lsb: int
    width: int
    description: str = ""
    access: str = READ_WRITE
    read_action: str | None = None
    enums: dict[int, str] = field(default_factory=dict)

    @property
    def msb(self) -> int:
        return self.lsb + self.width - 1

    @property
    def mask(self) -> int:
        return ((1 << self.width) - 1) << self.lsb

    def extract(self, reg_value: int) -> int:
        return (reg_value >> self.lsb) & ((1 << self.width) - 1)

    def insert(self, reg_value: int, value: int) -> int:
        return (reg_value & ~self.mask) | ((value << self.lsb) & self.mask)

    def format(self, reg_value: int) -> str:
        v = self.extract(reg_value)
        txt = f"{v:#x}" if self.width > 4 else str(v)
        if v in self.enums:
            txt += f"  {self.enums[v]}"
        return txt

    @property
    def bits(self) -> str:
        return f"[{self.msb}]" if self.width == 1 else f"[{self.msb}:{self.lsb}]"


@dataclass
class Register:
    name: str
    offset: int
    size: int = 32                  # en bits
    access: str = READ_WRITE
    reset: int = 0
    description: str = ""
    read_action: str | None = None
    fields: list[Field] = field(default_factory=list)

    @property
    def nbytes(self) -> int:
        return max(1, self.size // 8)

    @property
    def readable(self) -> bool:
        """Lisible sans effet de bord (pas write-only, pas de readAction)."""
        if self.access == WRITE_ONLY or self.read_action:
            return False
        return not any(f.read_action for f in self.fields)

    @property
    def side_effect(self) -> bool:
        return bool(self.read_action or any(f.read_action for f in self.fields))


@dataclass
class Peripheral:
    name: str
    base: int
    description: str = ""
    group: str = ""
    registers: list[Register] = field(default_factory=list)

    def register(self, name: str) -> Register | None:
        return next((r for r in self.registers if r.name == name), None)

    def address(self, reg: Register) -> int:
        return self.base + reg.offset


@dataclass
class Device:
    name: str
    description: str = ""
    peripherals: list[Peripheral] = field(default_factory=list)

    def peripheral(self, name: str) -> Peripheral | None:
        return next((p for p in self.peripherals if p.name.lower() == name.lower()), None)


# ====================================================================== lecture
def _text(el: ET.Element | None, tag: str, default: str | None = None) -> str | None:
    if el is None:
        return default
    child = el.find(tag)
    if child is None or child.text is None:
        return default
    return " ".join(child.text.split())


@dataclass
class _Props:
    size: int = 32
    access: str = READ_WRITE
    reset: int = 0

    def child(self, el: ET.Element) -> "_Props":
        return _Props(
            size=parse_int(_text(el, "size"), self.size),
            access=_text(el, "access", self.access),
            reset=parse_int(_text(el, "resetValue"), self.reset),
        )


def _dim_names(el: ET.Element, name: str) -> list[tuple[str, int]]:
    """Expanse un élément `dim` : [(nom, index)]. Sans dim : [(nom, 0)]."""
    dim = parse_int(_text(el, "dim"))
    if not dim:
        return [(name, 0)]
    idx_text = _text(el, "dimIndex")
    if idx_text:
        m = re.fullmatch(r"(\d+)-(\d+)", idx_text)
        if m:
            indexes = [str(i) for i in range(int(m.group(1)), int(m.group(2)) + 1)]
        else:
            indexes = [s.strip() for s in idx_text.split(",")]
    else:
        indexes = [str(i) for i in range(dim)]
    out = []
    for i, idx in enumerate(indexes[:dim]):
        if "[%s]" in name:
            out.append((name.replace("[%s]", f"[{idx}]"), i))
        else:
            out.append((name.replace("%s", idx), i))
    return out


def _parse_fields(reg_el: ET.Element, reg_access: str) -> list[Field]:
    out: list[Field] = []
    fields_el = reg_el.find("fields")
    if fields_el is None:
        return out
    for f in fields_el.findall("field"):
        name = _text(f, "name", "?")
        if _text(f, "bitOffset") is not None:
            lsb = parse_int(_text(f, "bitOffset"))
            width = parse_int(_text(f, "bitWidth"), 1)
        elif _text(f, "lsb") is not None:
            lsb = parse_int(_text(f, "lsb"))
            width = parse_int(_text(f, "msb")) - lsb + 1
        else:
            m = re.fullmatch(r"\[(\d+):(\d+)\]", _text(f, "bitRange", "[0:0]"))
            msb_, lsb = (int(m.group(1)), int(m.group(2))) if m else (0, 0)
            width = msb_ - lsb + 1
        enums: dict[int, str] = {}
        for ev_group in f.findall("enumeratedValues"):
            for ev in ev_group.findall("enumeratedValue"):
                val = parse_int(_text(ev, "value"))
                if val is not None:
                    enums[val] = _text(ev, "name", str(val))
        for fname, _ in _dim_names(f, name):
            out.append(Field(fname, lsb, width, _text(f, "description", ""),
                             _text(f, "access", reg_access), _text(f, "readAction"), dict(enums)))
    return sorted(out, key=lambda x: -x.lsb)


def _parse_register(el: ET.Element, props: _Props, base_offset: int,
                    known: dict[str, ET.Element]) -> list[Register]:
    if el.get("derivedFrom"):
        src = known.get(el.get("derivedFrom").split(".")[-1])
        if src is not None:
            merged = copy.deepcopy(src)
            for child in el:  # les éléments présents surchargent ceux du registre source
                old = merged.find(child.tag)
                if old is not None:
                    merged.remove(old)
                merged.append(child)
            el = merged
    p = props.child(el)
    name = _text(el, "name", "?")
    offset = parse_int(_text(el, "addressOffset"), 0)
    inc = parse_int(_text(el, "dimIncrement"), p.size // 8)
    fields = _parse_fields(el, p.access)
    regs = []
    for rname, i in _dim_names(el, name):
        regs.append(Register(rname, base_offset + offset + i * inc, p.size, p.access, p.reset,
                             _text(el, "description", ""), _text(el, "readAction"),
                             list(fields)))      # champs partagés : jamais modifiés après analyse
    return regs


def _parse_block(block: ET.Element, props: _Props, base_offset: int, prefix: str) -> list[Register]:
    """Registres et clusters d'un élément `registers` ou `cluster` (clusters aplatis)."""
    regs: list[Register] = []
    known = {(_text(r, "name") or ""): r for r in block.findall("register")}
    known.update({(_text(c, "name") or ""): c for c in block.findall("cluster")})
    for el in block:
        if el.tag == "register":
            for r in _parse_register(el, props, base_offset, known):
                r.name = prefix + r.name
                regs.append(r)
        elif el.tag == "cluster":
            if el.get("derivedFrom") and known.get(el.get("derivedFrom")) is not None:
                src = copy.deepcopy(known[el.get("derivedFrom")])
                for child in el:
                    old = src.find(child.tag)
                    if old is not None:
                        src.remove(old)
                    src.append(child)
                el = src
            cp = props.child(el)
            off = parse_int(_text(el, "addressOffset"), 0)
            inc = parse_int(_text(el, "dimIncrement"), 0)
            for cname, i in _dim_names(el, _text(el, "name", "?")):
                regs += _parse_block(el, cp, base_offset + off + i * inc, f"{prefix}{cname}.")
    return regs


#: éléments d'un périphérique dérivé qui ne touchent pas à ses registres
_IDENTITY = {"name", "baseAddress", "description", "groupName", "interrupt", "addressBlock",
             "alternatePeripheral", "prependToName", "appendToName", "headerStructName", "version"}


def parse(source: str | bytes) -> Device:
    """`source` : chemin de fichier, ou contenu XML (str/bytes commençant par '<')."""
    if isinstance(source, bytes) or source.lstrip().startswith("<"):
        root = ET.fromstring(source)
    else:
        root = ET.parse(source).getroot()
    dev_props = _Props().child(root)
    dev = Device(_text(root, "name", "?"), _text(root, "description", ""))
    periph_els = root.find("peripherals")
    if periph_els is None:
        return dev
    by_name = {(_text(p, "name") or ""): p for p in periph_els.findall("peripheral")}
    parsed: dict[str, list[Register]] = {}      # registres analysés, par périphérique source

    def registers_of(el: ET.Element) -> list[Register]:
        regs_el = el.find("registers")
        regs = _parse_block(regs_el, dev_props.child(el), 0, "") if regs_el is not None else []
        regs.sort(key=lambda r: r.offset)
        return regs

    for pel in periph_els.findall("peripheral"):
        el = pel
        src_name = pel.get("derivedFrom")
        src = by_name.get(src_name) if src_name else None
        regs = None
        if src is not None and src.find("dim") is None and all(c.tag in _IDENTITY for c in pel):
            # dérivé qui ne change que son identité (doubles sécurisé / non sécurisé du N6…) :
            # registres de la source, analysés une seule fois et partagés (jamais modifiés)
            if src_name not in parsed:
                parsed[src_name] = registers_of(src)
            regs = parsed[src_name]
            el = copy.copy(src)                 # copie de surface : enfants de pel en tête
            el[:] = list(pel) + [c for c in src if pel.find(c.tag) is None]
        elif src is not None:
            el = copy.deepcopy(src)
            for child in pel:
                old = el.find(child.tag)
                if old is not None:
                    el.remove(old)
                el.append(child)
        if regs is None:
            regs = registers_of(el)
            if src_name is None:
                parsed[_text(pel, "name") or ""] = regs
        base = parse_int(_text(el, "baseAddress"), 0)
        for pname, i in _dim_names(el, _text(el, "name", "?")):
            inc = parse_int(_text(el, "dimIncrement"), 0)
            dev.peripherals.append(Peripheral(pname, base + i * inc, _text(el, "description", ""),
                                              _text(el, "groupName", ""), list(regs)))
    dev.peripherals.sort(key=lambda p: p.base)
    return dev


# ====================================================================== lecture mémoire
def read_blocks(periph: Peripheral, regs: list[Register] | None = None,
                force: bool = False) -> list[tuple[int, int, list[Register]]]:
    """Groupe les registres lisibles en blocs **contigus** : [(adresse, octets, registres)].

    On ne fusionne jamais au-dessus d'un trou (adresses réservées : bus fault possible).
    `force=True` inclut les registres à effet de bord (pas les write-only).
    """
    candidates = [r for r in (regs if regs is not None else periph.registers)
                  if (r.readable or (force and r.access != WRITE_ONLY))]
    blocks: list[tuple[int, int, list[Register]]] = []
    for r in sorted(candidates, key=lambda r: r.offset):
        addr = periph.address(r)
        if blocks and blocks[-1][0] + blocks[-1][1] == addr:
            a, n, rs = blocks[-1]
            blocks[-1] = (a, n + r.nbytes, rs + [r])
        elif blocks and addr < blocks[-1][0] + blocks[-1][1]:
            continue  # registres qui se recouvrent (alternateRegister) : déjà couverts
        else:
            blocks.append((addr, r.nbytes, [r]))
    return blocks


def decode_block(periph: Peripheral, addr: int, data: bytes, regs: list[Register]) -> dict[str, int]:
    out = {}
    for r in regs:
        off = periph.address(r) - addr
        out[r.name] = int.from_bytes(data[off:off + r.nbytes], "little")
    return out
