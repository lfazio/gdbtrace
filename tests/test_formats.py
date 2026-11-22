"""Formats d'affichage des valeurs (Watch, locales, gdbtrace.eval) : sorties identiques à gdb."""
import struct

import pytest

from gdbtrace.core.formats import FORMATS, ascii_repr, format_bytes, from_zero_hex, normalize


def test_int16_in_every_format():
    raw = (-3).to_bytes(2, "little", signed=True)
    assert format_bytes(raw, "decimal", signed=True) == "-3"
    assert format_bytes(raw, "decimal") == "65533"
    assert format_bytes(raw, "hexadecimal") == "0xfffd"               # gdb : complément à deux
    assert format_bytes(raw, "octal") == "0177775"
    assert format_bytes(raw, "binary") == "0b1111_1111_1111_1101"     # largeur du type, par 4
    assert format_bytes(raw, "ascii") == "'\\xfd\\xff'"


def test_char_float_and_memory_order():
    assert [format_bytes(b"A", f) for f in ("decimal", "hexadecimal", "octal", "binary", "ascii")] \
        == ["65", "0x41", "0101", "0b0100_0001", "'A'"]
    bits = struct.pack("<f", 21.5)                                    # gdb 15 : bits bruts
    assert format_bytes(bits, "hexadecimal") == "0x41ac0000"
    assert format_bytes(bits, "decimal") == "1101791232" and format_bytes(bits, "octal") == "010153000000"
    assert format_bytes(b"ABCD", "ascii") == "'ABCD'"                   # ordre de la mémoire
    assert format_bytes(b"ABCD", "hexadecimal") == "0x44434241"
    assert format_bytes(b"\0\0", "octal") == "0"


def test_ascii_escapes():
    assert ascii_repr(b"a\n\t'\\\0") == "'a\\n\\t\\'\\\\\\0'"
    assert ascii_repr(b"\x001") == "'\\x001'"                            # « \01 » serait ambigu


def test_from_zero_hex():
    assert from_zero_hex("0x00000041", "ascii") == "'A\\0\\0\\0'"
    assert from_zero_hex("0x0041", "binary") == "0b0000_0000_0100_0001"
    assert from_zero_hex("0x41", "binary") == "0b0100_0001"
    assert from_zero_hex("[4]", "ascii") is None and from_zero_hex("{...}", "binary") is None


def test_normalize_aliases():
    assert [normalize(a) for a in ("hex", "X", "Binaire", "dec", "oct", "char", "", None)] == \
        ["hexadecimal", "hexadecimal", "binary", "decimal", "octal", "ascii", "natural", "natural"]
    assert all(normalize(k) == k for k in FORMATS)
    with pytest.raises(ValueError, match="format inconnu"):
        normalize("romain")
