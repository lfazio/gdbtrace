"""Chargeur de flash générique (core/flashalgo.py) : algorithmes FLM et .stldr de test
(examples/flashalgo), qui émulent une flash dans la SSRAM1 de QEMU mps2-an385 (0x00300000,
1 Mo : effacement à 0xFF, programmation qui ne fait que baisser des bits)."""
import os
import shutil
import subprocess
import sys

import pytest
from conftest import ROOT, needs_qemu

from gdbtrace.core import flashalgo as fa

ALGO_DIR = os.path.join(ROOT, "examples", "flashalgo")
FLM = os.path.join(ALGO_DIR, "ram-flash.FLM")
STLDR = os.path.join(ALGO_DIR, "ram-flash.stldr")
RAM = (0x20100000, 0x10000)
DEV = 0x00300000


@pytest.fixture(scope="module")
def algos():
    if not shutil.which("arm-none-eabi-gcc"):
        pytest.skip("arm-none-eabi-gcc absent")
    subprocess.run(["make", "-s", "-C", ALGO_DIR, "ram-flash.FLM", "ram-flash.stldr"], check=True)
    return FLM, STLDR


def test_flm_parsed(algos):
    a = fa.load_algo(FLM)
    assert a.kind == "FLM" and a.ok == 0
    d = a.device
    assert (d.name, d.start, d.size, d.page, d.erased) == ("GdbTrace RAM-flash (QEMU mps2-an385)", DEV,
                                                           0x100000, 0x400, 0xFF)
    assert len(d.sector_list()) == 256 and d.sector_list()[1] == (DEV + 0x1000, 0x1000)
    assert d.sectors_for(DEV + 0xFFF, DEV + 0x1001) == [(DEV, 0x1000), (DEV + 0x1000, 0x1000)]
    assert {"Init", "UnInit", "EraseSector", "ProgramPage", "Verify", "BlankCheck"} <= set(a.funcs)
    p = a.place(RAM)
    assert p.return_addr == RAM[0] and p.image[0][1][:4] == fa.BKPT_RETURN
    assert p.static_base == RAM[0] + 4 + a.data_offset      # r9 : début de PrgData
    assert p.funcs["Init"] & 1 and p.buffer_size == 0x400 and p.buffer + 0x400 <= sum(RAM)
    with pytest.raises(fa.FlashAlgoError, match="RAM trop petite"):
        a.place((0x20100000, 0x200))


def test_stldr_parsed(algos):
    a = fa.load_algo(STLDR)
    assert a.kind == "stldr" and a.ok == 1
    d = a.device
    assert (d.start, d.size, d.page, d.kind) == (DEV, 0x100000, 256, "NOR")
    assert len(d.sector_list()) == 16 and d.sector_list()[0] == (DEV, 0x10000)
    assert all(addr < 0xF0000000 for addr, _ in a.segments)   # .Dev_info jamais chargé
    p = a.place(None)
    assert p.return_addr >= a.image_end and p.static_base is None
    assert p.funcs["Write"] & 1


def test_split_segments(algos):
    class L:
        device = fa.FlashDevice("x", 0x1000, 0x1000, 256)
    ld = L()
    covered, rest = fa.split_segments([(0x0F00, b"a" * 0x200), (0x3000, b"b" * 4)], [ld])
    assert covered[ld] == [(0x1000, b"a" * 0x100)]
    assert rest == [(0x0F00, b"a" * 0x100), (0x3000, b"b" * 4)]
    assert fa.parse_ram("0x20000000:16K") == (0x20000000, 0x4000)
    assert fa.parse_ram("0x20000000+0x100") == (0x20000000, 0x100)


@pytest.fixture
def qemu_session(elf):
    from gdbtrace.core.session import Session
    from gdbtrace.core.target import QemuTarget, free_port
    s = Session()
    s.connect(elf, QemuTarget(elf=elf, gdb_port=free_port()))
    yield s
    s.disconnect()


@needs_qemu
@pytest.mark.parametrize("which", ["FLM", "stldr"])
def test_program_through_algorithm(algos, qemu_session, tmp_path, which):
    """Zone préremplie à 0 : si l'algorithme n'effaçait pas avant de programmer, la flash
    émulée (et bit à bit) garderait des 0. Aucun resume hook, aucun événement visible pendant
    les appels ; la cible reste arrêtée."""
    s = qemu_session
    events, hooks = [], []
    s.on_event(lambda k, d: events.append(k))
    s.add_resume_hook(lambda: hooks.append(1))
    path = FLM if which == "FLM" else STLDR
    s.flash_loaders = [fa.FlashLoader(s, fa.load_algo(path), RAM if which == "FLM" else None)]
    s.write_memory(DEV, b"\0" * 0x4000)
    data = bytes((i * 7 + 3) & 0xFF for i in range(0x2345))
    image = tmp_path / "motif.bin"
    image.write_bytes(data)
    res = s.program(f"{image}@{DEV + 0x100:#x}")
    assert res.verified and not res.mismatched, res.summary()
    assert res.loaders and os.path.basename(path) in res.loaders[0]
    assert s.read_memory(DEV + 0x100, len(data)) == data
    assert s.read_memory(DEV, 0x100) == b"\xff" * 0x100      # début du secteur effacé
    assert hooks == [] and s.state == "stopped"
    assert events.count("running") == 0 and events.count("stopped") >= 1   # reset, programmed
    assert s.breakpoints() == []                             # breakpoint de retour retiré
    n = s.flash_loaders[0].erase(DEV, DEV + 1)
    assert n == 1 and s.read_memory(DEV + 0x100, 16) == b"\xff" * 16


@needs_qemu
def test_mixed_image_ram_and_flash(algos, qemu_session, tmp_path):
    """Une image en partie dans la « flash » (algorithme) et en partie en RAM (load de gdb)."""
    from gdbtrace.core.memtools import write_image
    s = qemu_session
    s.flash_loaders = [fa.FlashLoader(s, fa.load_algo(FLM), RAM)]
    hexf = tmp_path / "mixte.hex"
    write_image(str(hexf), [(DEV + 0x2000, b"\x11" * 300), (0x20200000, b"\x22" * 64)])
    res = s.program(str(hexf))
    assert res.verified, res.summary()
    assert s.read_memory(DEV + 0x2000, 300) == b"\x11" * 300
    assert s.read_memory(0x20200000, 64) == b"\x22" * 64


@needs_qemu
def test_stuck_and_faulting_algorithm(algos, qemu_session):
    """Effacer le secteur STUCK ne rend pas la main : délai dépassé, cible interrompue ;
    programmer la page FAULTING fait une faute : arrêt hors du retour, dit."""
    s = qemu_session
    ld = fa.FlashLoader(s, fa.load_algo(FLM), RAM)
    ld.device.erase_timeout = 0.5
    with pytest.raises(TimeoutError, match="sans retour"):
        ld.erase(DEV + 0xF0000, DEV + 0xF0001)
    assert s.state == "stopped" and s.breakpoints() == []
    with pytest.raises((TimeoutError, fa.FlashAlgoError, Exception)) as exc:
        ld.device.prog_timeout = 0.5
        ld.program([(DEV + 0xE0000, b"\0" * 16)])
    assert "sans retour" in str(exc.value) or "hors de son retour" in str(exc.value)
    assert s.state == "stopped"


@needs_qemu
def test_batch_option_and_api(algos, elf, tmp_path):
    """--flash-loader et --flash-ram en batch ; gdbtrace.flash_info / flash_erase."""
    data = os.urandom(5000)
    image = tmp_path / "motif.bin"
    image.write_bytes(data)
    script = tmp_path / "s.py"
    script.write_text(
        f"assert gdbtrace.mem({DEV:#x}, {len(data)}) == open({str(image)!r}, 'rb').read()\n"
        "gdbtrace.flash_info()\n"
        f"gdbtrace.flash_erase({DEV:#x}, 16)\n"
        f"assert gdbtrace.mem({DEV:#x}, 16) == b'\\xff' * 16\n"
        "print('OK')\n")
    r = subprocess.run([sys.executable, "-m", "gdbtrace", elf, "--batch", "--program",
                        f"{image}@{DEV:#x}", "--flash-loader", FLM, "--flash-ram", "0x20100000:0x10000",
                        "--script", str(script)], cwd=ROOT, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0 and "OK" in r.stdout, r.stdout + r.stderr
    assert "chargeur de flash : ram-flash.FLM (FLM)" in r.stdout and "par ram-flash.FLM" in r.stdout
    assert "1 secteur(s) effacé(s)" in r.stdout and "256 secteurs" in r.stdout


def test_options_validated():
    from gdbtrace.__main__ import build_parser, validate
    p = build_parser()
    errs = validate(p.parse_args(["x.elf", "--flash-loader", "algo.FLM"]))
    assert any("--flash-ram" in e for e in errs)
    assert validate(p.parse_args(["x.elf", "--flash-loader", "algo.stldr"])) == []
    errs = validate(p.parse_args(["x.elf", "--flash-loader", "a.FLM", "--flash-ram", "0x2000"]))
    assert any("ADRESSE:TAILLE" in e for e in errs)


def test_config_roundtrip(tmp_path):
    from gdbtrace.__main__ import build_parser
    from gdbtrace.core.config import SessionConfig
    p = build_parser()
    args = p.parse_args(["x.elf", "--flash-loader", "MX66UW1G45G_STM32N6570-NUCLEO.stldr",
                         "--flash-loader", FLM, "--flash-ram", "0x34000000:0x10000"])
    cfg = SessionConfig.from_args(args)
    assert cfg.data["cible"]["flash-ram"] == "0x34000000:0x10000"
    cfg.path = str(tmp_path / "c.yaml")
    cfg.save(cfg.path)
    again = p.parse_args(SessionConfig.load(cfg.path).to_argv())
    assert again.flash_loader == ["MX66UW1G45G_STM32N6570-NUCLEO.stldr", FLM]
    assert again.flash_ram == "0x34000000:0x10000"
