"""Variables « en direct » (Var.view pendant que la cible tourne).

Pendant que la cible tourne, gdb ne peut plus évaluer d'expression. On calcule donc,
cible arrêtée, la disposition mémoire de chaque variable feuille (adresse, taille,
catégorie de type) ; ensuite les octets lus par le canal live (OpenOCD/QEMU) sont
décodés ici, sans gdb, au format d'affichage de gdb.
"""
from __future__ import annotations

import re
import struct
from dataclasses import dataclass, field

from .gdb_backend import GdbError

MAX_LIVE_SIZE = 4096
_QUALIFIERS = re.compile(r"\b(volatile|const|restrict|register|static)\b")
_IDENT = re.compile(r"[A-Za-z_]\w*")


@dataclass
class LeafLayout:
    addr: int = 0
    size: int = 0
    kind: str = ""                   # int, uint, char, schar, bool, float, ptr, enum
    enum_names: dict[int, str] = field(default_factory=dict)
    live: bool = False
    reason: str = ""                 # pourquoi la variable n'est pas lisible en direct


def _parse_enum(body: str) -> dict[int, str]:
    names: dict[int, str] = {}
    nxt = 0
    for part in body.split(","):
        part = part.strip()
        if not part:
            continue
        if "=" in part:
            name, _, val = part.partition("=")
            try:
                nxt = int(val.strip(), 0)
            except ValueError:
                continue
            part = name.strip()
        names[nxt] = part
        nxt += 1
    return names


def classify(ptype: str, size: int) -> tuple[str, dict[int, str]] | None:
    """Catégorie de décodage d'après la sortie de `ptype` (typedefs déjà résolus par gdb)."""
    t = ptype.strip()
    if t.startswith("type ="):
        t = t[len("type ="):]
    t = " ".join(_QUALIFIERS.sub(" ", t).split())
    if t.endswith("*") or "(*" in t:
        return "ptr", {}
    if t.startswith("enum"):
        m = re.search(r"\{(.*)\}", t)
        return "enum", _parse_enum(m.group(1)) if m else {}
    if "{" in t or t.endswith("]"):
        return None  # struct, union, tableau : pas une feuille scalaire
    if t in ("float", "double", "long double"):
        return ("float", {}) if size in (4, 8) else None
    if t in ("_Bool", "bool"):
        return "bool", {}
    if t == "char":
        return "char", {}          # char non signé sur ARM (AAPCS)
    if t == "signed char":
        return "schar", {}
    if t == "unsigned char":
        return "char", {}
    words = set(t.split())
    if words & {"int", "short", "long", "unsigned", "signed"}:
        if size not in (1, 2, 4, 8):
            return None
        return ("uint" if "unsigned" in words else "int"), {}
    return None


_ESCAPES = {7: "\\a", 8: "\\b", 9: "\\t", 10: "\\n", 11: "\\v", 12: "\\f", 13: "\\r",
            39: "\\'", 92: "\\\\"}


def _char_repr(v: int) -> str:
    if v in _ESCAPES:
        c = _ESCAPES[v]
    elif 32 <= v < 127:
        c = chr(v)
    else:
        c = f"\\{v & 0xFF:03o}"
    return f"{v} '{c}'"


def decode(layout: LeafLayout, data: bytes) -> str:
    """Convertit les octets bruts en texte, comme gdb l'afficherait."""
    if len(data) < layout.size:
        return "<?>"
    raw = data[:layout.size]
    k = layout.kind
    if k == "float":
        v = struct.unpack("<f" if layout.size == 4 else "<d", raw)[0]
        return f"{v:.9g}" if layout.size == 4 else f"{v:.17g}"
    signed = k in ("int", "schar")
    if k == "enum":
        signed = False
    v = int.from_bytes(raw, "little", signed=signed)
    if k == "ptr":
        return f"{v:#x}"
    if k == "bool":
        return "true" if v else "false"
    if k in ("char", "schar"):
        return _char_repr(v)
    if k == "enum":
        if v in layout.enum_names:
            return layout.enum_names[v]
        sv = int.from_bytes(raw, "little", signed=True)
        return layout.enum_names.get(sv, str(v))
    return str(v)


def root_identifier(expr: str) -> str | None:
    """Premier identifiant d'une expression C (`sensors[1].value` → `sensors`)."""
    m = _IDENT.search(expr)
    return m.group(0) if m else None


def compute_layout(session, varobj: str, local_names: set[str]) -> LeafLayout:
    """Calcule la disposition d'une feuille de varobj. La cible doit être arrêtée."""
    try:
        path = session.path_expression(varobj)
    except GdbError as exc:
        return LeafLayout(reason=str(exc))
    ident = root_identifier(path)
    if ident in local_names:
        return LeafLayout(reason="variable locale (la frame change en marche)")
    try:
        addr = session.address_of_expr(path)
        size = session.sizeof(path)
    except (GdbError, ValueError) as exc:
        return LeafLayout(reason=f"pas en mémoire ({exc})")
    if size <= 0 or size > MAX_LIVE_SIZE:
        return LeafLayout(addr=addr, size=size, reason="taille non gérée")
    try:
        kind = classify(session.ptype(path), size)
    except GdbError as exc:
        return LeafLayout(addr=addr, size=size, reason=str(exc))
    if kind is None:
        return LeafLayout(addr=addr, size=size, reason="type non géré")
    return LeafLayout(addr=addr, size=size, kind=kind[0], enum_names=kind[1], live=True)
