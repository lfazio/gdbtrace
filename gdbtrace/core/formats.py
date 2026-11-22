"""Formats d'affichage des valeurs (Watch, locales, `gdbtrace.eval`) : Naturel, Décimal,
Hexadécimal, Octal, Binaire, ASCII.

Cible arrêtée, gdb formate lui-même (`-var-set-format`, `output/x`) ; en marche, les octets lus
par le canal live sont formatés ici (`format_bytes`), à l'identique, pour que la valeur ne
change pas d'aspect entre l'arrêt et la marche. Comme gdb (15), un flottant en décimal,
hexadécimal, octal ou binaire montre ses bits bruts. gdb n'a ni binaire groupé ni ASCII
multi-octets : ils sont tirés du format `zero-hexadecimal` (largeur du type conservée).
"""
from __future__ import annotations

import re

#: clé → libellé du menu (ordre du menu)
FORMATS = {"natural": "Naturel", "decimal": "Décimal", "hexadecimal": "Hexadécimal",
           "octal": "Octal", "binary": "Binaire", "ascii": "ASCII"}

#: format posé sur le varobj gdb (`-var-set-format`)
GDB_FORMAT = {"natural": "natural", "decimal": "decimal", "hexadecimal": "hexadecimal",
              "octal": "octal", "binary": "zero-hexadecimal", "ascii": "zero-hexadecimal"}

#: lettre de `output/F expr` (locales, `gdbtrace.eval`) ; repli sur un agrégat (structure)
OUTPUT_LETTER = {"decimal": "d", "hexadecimal": "x", "octal": "o", "binary": "z", "ascii": "z"}
AGGREGATE_LETTER = {"binary": "t", "ascii": "c"}

_ALIASES = {
    "natural": ("natural", "naturel", "n", ""),
    "decimal": ("decimal", "décimal", "dec", "d"),
    "hexadecimal": ("hexadecimal", "hexadécimal", "hex", "x"),
    "octal": ("octal", "oct", "o"),
    "binary": ("binary", "binaire", "bin", "b", "t"),
    "ascii": ("ascii", "a", "c", "char"),
}
_BY_ALIAS = {a: key for key, names in _ALIASES.items() for a in names}
_HEX_TOKEN = re.compile(r"0x([0-9a-fA-F]+)$")
_ESCAPES = {0: "\\0", 7: "\\a", 8: "\\b", 9: "\\t", 10: "\\n", 11: "\\v", 12: "\\f", 13: "\\r",
            39: "\\'", 92: "\\\\"}


def normalize(fmt: str | None) -> str:
    """Clé de format depuis un nom ou un alias (« hex », « x », « binaire »...)."""
    key = _BY_ALIAS.get((fmt or "").strip().lower())
    if key is None:
        raise ValueError(f"format inconnu : {fmt!r} (naturel, décimal, hex, octal, binaire, ascii)")
    return key


def ascii_repr(raw: bytes) -> str:
    """Octets dans l'ordre de la mémoire, en caractères (échappements C) : 'AB\\0\\0'."""
    out = []
    for i, b in enumerate(raw):
        nxt = raw[i + 1] if i + 1 < len(raw) else None
        if b == 0 and nxt is not None and 0x30 <= nxt <= 0x37:
            out.append("\\x00")                  # « \0 » suivi d'un chiffre octal serait ambigu
        elif b in _ESCAPES:
            out.append(_ESCAPES[b])
        elif 32 <= b < 127:
            out.append(chr(b))
        else:
            out.append(f"\\x{b:02x}")
    return "'" + "".join(out) + "'"


def binary_repr(raw: bytes) -> str:
    """Bits sur toute la largeur du type, groupés par 4 : 0b1111_1111_1111_1101."""
    bits = format(int.from_bytes(raw, "little"), f"0{8 * len(raw)}b")
    return "0b" + "_".join(bits[i:i + 4] for i in range(0, len(bits), 4))


def format_bytes(raw: bytes, fmt: str, signed: bool = False) -> str:
    """Valeur scalaire (octets petit-boutistes de la cible) au format `fmt`, comme gdb.
    `signed` ne compte qu'en décimal (entier signé) ; le format naturel n'est pas traité ici
    (il dépend du type : voir `live_vars.decode`)."""
    fmt = normalize(fmt)
    if fmt == "decimal":
        return str(int.from_bytes(raw, "little", signed=signed))
    v = int.from_bytes(raw, "little")
    if fmt == "octal":
        return f"0{v:o}" if v else "0"
    if fmt == "binary":
        return binary_repr(raw)
    if fmt == "ascii":
        return ascii_repr(raw)
    return f"{v:#x}"


def from_zero_hex(text: str, fmt: str) -> str | None:
    """Valeur donnée par gdb en `zero-hexadecimal` (`0x0041`) → binaire ou ASCII ; None si ce
    n'est pas un scalaire (`[4]`, `{...}`) : la valeur de gdb reste alors affichée."""
    m = _HEX_TOKEN.match(text.strip())
    if m is None:
        return None
    digits = m.group(1)
    if len(digits) % 2:
        digits = "0" + digits
    return format_bytes(bytes.fromhex(digits)[::-1], fmt)
