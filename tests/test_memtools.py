"""Outils mémoire (core/memtools.py) : images Intel HEX, S-record, binaire, ELF ; Data.SAVE,
Data.LOAD, Data.Set, Data.Find, Data.ComPare ; programmation d'une image ; symboles d'un
second ELF (`--add-symbols`)."""
import os
import subprocess
import sys

import pytest
from conftest import ROOT, needs_qemu

from gdbtrace.core import memtools as mt


# ====================================================================== formats
SEGS = [(0x0800_FFF8, bytes(range(40))), (0x2000_0000, b"\xde\xad\xbe\xef")]


def test_ihex_roundtrip_across_64k():
    text = mt.format_ihex(SEGS)
    assert ":02000004080" in text and text.rstrip().endswith(":00000001FF")
    assert mt.parse_ihex(text) == SEGS


def test_srec_roundtrip():
    text = mt.format_srec(SEGS)
    assert text.startswith("S0") and "\nS3" in text and "\nS7" in text
    assert mt.parse_srec(text) == SEGS


def test_checksums_are_checked():
    bad = mt.format_ihex(SEGS).replace(":10", ":11", 1)
    with pytest.raises(mt.ImageError):
        mt.parse_ihex(bad)
    line = mt.format_srec(SEGS).splitlines()[1]
    with pytest.raises(mt.ImageError, match="somme de contrôle"):
        mt.parse_srec(line[:-2] + ("00" if line[-2:] != "00" else "01"))


def test_image_files(tmp_path):
    for ext in (".hex", ".srec"):
        p = str(tmp_path / f"a{ext}")
        assert mt.write_image(p, SEGS) in ("ihex", "srec")
        assert mt.read_image(p) == SEGS
    b = str(tmp_path / "a.bin")
    mt.write_image(b, SEGS[:1])
    assert mt.read_image(f"{b}@0x08000000") == [(0x0800_0000, SEGS[0][1])]
    with pytest.raises(mt.ImageError, match="adresse à donner"):
        mt.read_image(b)
    assert mt.split_address("fw.bin@0x100") == ("fw.bin", 0x100)
    assert mt.split_address("dir@x/fw.elf") == ("dir@x/fw.elf", None)
    assert mt.is_image("a.hex") and not mt.is_image("a.elf")


class Mem:
    def __init__(self, base=0x1000, size=0x30000):
        self.base, self.data = base, bytearray(size)

    def read(self, addr, n):
        return bytes(self.data[addr - self.base:addr - self.base + n])

    def write(self, addr, data):
        self.data[addr - self.base:addr - self.base + len(data)] = data


def test_fill_find_compare(tmp_path):
    m = Mem()
    mt.fill(m.write, 0x1003, 10, b"\x01\x02\x03")
    assert m.read(0x1003, 10) == b"\x01\x02\x03" * 3 + b"\x01"
    m.write(0x1000 + mt.CHUNK * 16 - 2, b"MARK")            # à cheval sur deux lectures
    m.write(0x2000, b"MARK")
    assert mt.find(m.read, 0x1000, 0x30000, "MARK") == [0x2000, 0x1000 + mt.CHUNK * 16 - 2]
    assert mt.find(m.read, 0x1000, 0x30000, "MARK", align=4) == [0x2000]
    assert mt.find(m.read, 0x1000, 0x30000, 0xEFBE) == []
    img = str(tmp_path / "x.hex")
    mt.write_image(img, [(0x2000, b"MARK"), (0x3000, b"ab")])
    res = mt.compare(m.read, img)
    assert not res.same and res.differing == 2 and res.first[0] == (0x3000, ord("a"), 0)
    assert "2 octet(s) différent(s) sur 6" in res.summary()
    mt.load(m.write, img)
    assert mt.compare(m.read, img).same


# ====================================================================== QEMU
@needs_qemu
def test_memory_tools_on_target(session, tmp_path, elf):
    from gdbtrace.core.scripting import ScriptAPI
    api = ScriptAPI(session)
    session.write_memory(0x20001003, b"GDBTRACE-FIND")
    assert api.mem_find(0x20000000, 0x4000, "GDBTRACE-FIND") == [0x20001003]
    base, code = mt.read_image(elf)[0]                         # début de la flash (vecteurs)
    assert api.mem_find(base, 0x4000, code[4:12])[0] == base + 4
    hexf, binf = str(tmp_path / "s.hex"), str(tmp_path / "s.bin")
    api.mem_save("sensors", None, hexf)                       # taille de l'objet
    assert mt.read_image(hexf) == [(session.address_of("sensors"), session.read_memory(session.address_of("sensors"), 48))]
    api.mem_save(0x20000000, 64, binf)
    assert api.mem_compare(elf).same                          # flash = l'ELF (LMA, .data comprise)
    api.mem_fill("sensors", None, 0xA5)
    assert session.read_memory(session.address_of("sensors"), 48) == b"\xa5" * 48
    assert not api.mem_compare(hexf).same
    api.mem_load(hexf)
    assert api.mem_compare(hexf).same
    assert api.mem_load(f"{binf}@0x20001000") == [(0x20001000, 64)]


@needs_qemu
def test_program_hex_and_bin_images(session, tmp_path, elf):
    """Une image HEX (puis binaire) faite de l'ELF, programmée : écrite et vérifiée par
    relecture, les symboles restent ceux de l'ELF."""
    hexf = str(tmp_path / "demo.hex")
    mt.write_image(hexf, mt.read_image(elf))
    session.write_memory(0x100, b"\0\0\0\0")                  # flash altérée
    res = session.program(hexf)
    assert res.verified and not res.mismatched and res.size > 1000, res.summary()
    assert session.elf == elf and session.address_of("fib")
    segs = mt.read_image(elf)
    binf = str(tmp_path / "flash.bin")
    mt.write_image(binf, segs[:1])
    res = session.program(f"{binf}@{segs[0][0]:#x}")
    assert res.verified, res.summary()


@needs_qemu
def test_cli_program_image_and_add_symbols(elf, faults_elfs, tmp_path):
    """--program IMAGE (symboles de l'ELF) et --add-symbols ELF@décalage : gdb, le profil
    et la trace connaissent les fonctions du second ELF."""
    hexf = str(tmp_path / "demo.hex")
    mt.write_image(hexf, mt.read_image(elf))
    script = tmp_path / "s.py"
    script.write_text(
        "from gdbtrace.core.symbols import SymbolTable\n"
        "sym = gdbtrace._symbols()\n"
        "f = sym.function('read_at')\n"
        "assert f is not None and f.start >= 0x100000, f\n"
        "assert 'read_at' in gdbtrace.s.symbol_at(f.start)\n"
        "assert sym.function('fib').start < 0x100000\n"
        "print('OK', hex(f.start))\n")
    r = subprocess.run([sys.executable, "-m", "gdbtrace", elf, "--batch", "--program", hexf,
                        "--add-symbols", f"{faults_elfs['m3']}@0x100000", "--script", str(script)],
                       cwd=ROOT, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0 and "OK 0x1" in r.stdout, r.stdout + r.stderr
    assert "demo.hex" in r.stdout and "vérifié" in r.stdout.lower(), r.stdout


def test_config_keeps_image_and_extra_symbols(tmp_path):
    from gdbtrace.__main__ import build_parser
    from gdbtrace.core.config import SessionConfig
    hexf, boot = tmp_path / "app.hex", tmp_path / "boot.elf"
    hexf.write_text(":00000001FF\n")
    boot.write_bytes(b"\x7fELF")
    args = build_parser().parse_args([str(tmp_path / "app.elf"), "--program", str(hexf),
                                      "--add-symbols", str(boot)])
    c = SessionConfig.from_args(args)
    assert c.data["cible"]["programmer"] == str(hexf) and c.data["cible"]["add-symbols"] == [str(boot)]
    path = str(tmp_path / "s.yaml")
    c.save(path)
    again = SessionConfig.load(path).parse()
    assert again.program == str(hexf) and again.add_symbols == [str(boot)]


def test_pattern_input():
    from gdbtrace.views.memory import parse_pattern
    assert parse_pattern("de ad be ef") == b"\xde\xad\xbe\xef"
    assert parse_pattern("0x1234") == b"\x34\x12" and parse_pattern('"ok"') == b"ok"
    with pytest.raises(ValueError):
        parse_pattern("zz")


@needs_qemu
def test_gui_add_symbols(elf, faults_elfs, tmp_path):
    if not os.environ.get("DISPLAY"):
        pytest.skip("pas de DISPLAY")
    from test_gui import close_app, pump

    from gdbtrace.core.session import Session
    from gdbtrace.core.target import QemuTarget, free_port
    from gdbtrace.gui.app import App
    a = App(layout_file=str(tmp_path / "layout.json"))
    try:
        s = Session()
        s.connect(elf, QemuTarget(elf=elf, gdb_port=free_port()))
        a.attach(s)
        assert pump(a, lambda: s.state == "stopped")
        a.add_symbols(f"{faults_elfs['m3']}@0x100000")
        f = a.symbols.function("read_at")
        assert f is not None and f.start >= 0x100000 and a.api.symbols is a.symbols
        assert a.symbols.function("fib") is not None
        assert s.extra_symbols == [(faults_elfs["m3"], 0x100000)]
    finally:
        close_app(a)
