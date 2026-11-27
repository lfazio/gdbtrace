"""CMSIS-Pack (core/pack.py) : lecture du .pdsc (héritage famille → sous-famille → puce →
variante, deux styles de mémoire), recherche d'une puce, packs installés ; pack de
démonstration (examples/flashalgo) sous QEMU : SVD du pack, programmation par son algorithme."""
import os
import shutil
import subprocess
import sys
import zipfile

import pytest
from conftest import ROOT, needs_qemu

from gdbtrace.core import pack as pk

PDSC = """<?xml version="1.0" encoding="UTF-8"?>
<package schemaVersion="1.7.7">
  <vendor>Acme</vendor>
  <name>M4_DFP</name>
  <description>Pack de test</description>
  <releases><release version="2.3.1">x</release><release version="2.3.0">y</release></releases>
  <devices>
    <family Dfamily="ACME32F4" Dvendor="Acme:99">
      <processor Dcore="Cortex-M4" Dfpu="SP_FPU" Dmpu="MPU" Dendian="Little-endian"/>
      <debug svd="SVD/family.svd"/>
      <subFamily DsubFamily="ACME32F44">
        <processor Dclock="180000000"/>
        <debug svd="SVD/f44.svd"/>
        <memory id="IRAM1" start="0x20000000" size="0x20000" default="1"/>
        <device Dname="ACME32F446RETx">
          <memory id="IROM1" start="0x08000000" size="0x80000" startup="1" default="1"/>
          <algorithm name="Flash/F4_512.FLM" start="0x08000000" size="0x80000" RAMstart="0x20000000"
                     RAMsize="0x1000" default="1"/>
          <algorithm name="Flash/OTP.FLM" start="0x1FFF7800" size="0x210"/>
          <variant Dvariant="ACME32F446RETx-BGA">
            <processor Dclock="168000000"/>
          </variant>
        </device>
        <device Dname="ACME32F446ZETx">
          <memory name="Flash" access="rx" start="0x08000000" size="0x80000" default="1"/>
        </device>
      </subFamily>
      <device Dname="ACME32F401">
        <processor Dcore="Cortex-M4" Dclock="84000000"/>
      </device>
    </family>
  </devices>
  <sequences><sequence name="ResetSystem"/></sequences>
</package>
"""


@pytest.fixture
def pdsc(tmp_path):
    p = tmp_path / "Acme.M4_DFP.pdsc"
    p.write_text(PDSC, encoding="utf-8")
    (tmp_path / "SVD").mkdir()
    (tmp_path / "SVD" / "f44.svd").write_text("<device/>")
    return str(p)


def test_inheritance_and_memories(pdsc):
    p = pk.Pack.open(pdsc)
    assert (p.vendor, p.name, p.version) == ("Acme", "M4_DFP", "2.3.1")
    d = p.find("acme32f446retx")
    assert (d.core, d.fpu, d.mpu, d.clock, d.vendor) == ("Cortex-M4", "SP_FPU", "MPU", 180_000_000, "Acme")
    assert d.svd == "SVD/f44.svd" and d.family == "ACME32F4" and d.sub_family == "ACME32F44"
    assert [(m.name, m.access, m.start) for m in d.memories] == [("IRAM1", "rwx", 0x20000000),
                                                               ("IROM1", "rx", 0x08000000)]
    assert d.default_ram() == (0x20000000, 0x20000)
    a = d.algorithms[0]
    assert (a.path, a.ram_start, a.ram_size, a.default) == ("Flash/F4_512.FLM", 0x20000000, 0x1000, True)
    assert not d.algorithms[1].default
    v = p.find("ACME32F446RETx-BGA")                       # variante : hérite, Dclock remplacé
    assert v.variant_of == "ACME32F446RETx" and v.clock == 168_000_000 and len(v.algorithms) == 2
    z = p.find("ACME32F446ZE")                             # préfixe unique, nouveau style de mémoire
    assert z.memories[-1].name == "Flash" and z.memories[-1].access == "rx"
    f401 = p.find("ACME32F401")                            # directement sous la famille
    assert f401.clock == 84_000_000 and f401.svd == "SVD/family.svd"
    assert any("séquences" in n for n in d.notes)
    assert d.svd_file().endswith(os.path.join("SVD", "f44.svd"))
    rows = dict(d.rows())
    assert ("cœur", "Cortex-M4", "SP_FPU, MPU, Little-endian") in rows["Processeur"]


def test_device_errors(pdsc):
    p = pk.Pack.open(pdsc)
    with pytest.raises(pk.PackError, match="ambiguë.*ACME32F446RETx, ACME32F446RETx-BGA, ACME32F446ZETx"):
        p.find("ACME32F446")
    with pytest.raises(pk.PackError, match="introuvable.*proches : ACME32F401"):
        p.find("ACME32F40X")
    with pytest.raises(pk.PackError, match="fichier absent"):
        p.find("ACME32F401").pack.file("SVD/family.svd")
    with pytest.raises(pk.PackError):
        pk.Pack.open(os.path.dirname(pdsc) + "/absent.pack")


def test_zipped_pack_and_installed_roots(pdsc, tmp_path, monkeypatch):
    """Un .pack (zip) se lit et s'extrait dans le cache ; les packs installés (cmsis-toolbox :
    <racine>/<Vendor>/<Name>/<version>/) donnent la dernière version, sans les index cachés."""
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    zpath = tmp_path / "Acme.M4_DFP.2.3.1.pack"
    with zipfile.ZipFile(zpath, "w") as z:
        z.write(pdsc, "Acme.M4_DFP.pdsc")
        z.writestr("SVD/f44.svd", "<device/>")
    d = pk.Pack.open(str(zpath)).find("ACME32F446RETx")
    svd = d.svd_file()
    assert svd.startswith(str(tmp_path / "cache" / "gdbtrace" / "packs" / "Acme.M4_DFP.2.3.1"))
    assert open(svd).read() == "<device/>"
    root = tmp_path / "packs"
    for version in ("2.2.0", "2.3.1", "2.10.0"):
        dst = root / "Acme" / "M4_DFP" / version
        dst.mkdir(parents=True)
        shutil.copy(pdsc, dst / "Acme.M4_DFP.pdsc")
    (root / ".Web").mkdir()
    shutil.copy(pdsc, root / ".Web" / "Acme.Other.pdsc")
    found = pk.installed_packs([str(root)])
    assert found == [str(root / "Acme" / "M4_DFP" / "2.10.0" / "Acme.M4_DFP.pdsc")]
    monkeypatch.setenv("CMSIS_PACK_ROOT", str(root))
    assert pk.resolve(None, "ACME32F401").name == "ACME32F401"
    assert [d.name for d in pk.list_devices(None, "ZE")] == ["ACME32F446ZETx"]


def test_options_and_config(pdsc, tmp_path):
    from gdbtrace.__main__ import build_parser, validate
    from gdbtrace.core.config import SessionConfig
    p = build_parser()
    assert any("--device" in e for e in validate(p.parse_args(["x.elf", "--pack", pdsc])))
    assert any("--device" in e for e in validate(p.parse_args(["x.elf", "--flash-loader", "pack"])))
    args = p.parse_args(["x.elf", "--pack", pdsc, "--device", "ACME32F401", "--flash-loader", "pack"])
    assert validate(args) == []
    cfg = SessionConfig.from_args(args)
    assert cfg.data["cible"]["pack"] == pdsc and cfg.data["cible"]["device"] == "ACME32F401"
    cfg.path = str(tmp_path / "c.yaml")
    cfg.save(cfg.path)
    again = p.parse_args(SessionConfig.load(cfg.path).to_argv())
    assert (again.pack, again.device, again.flash_loader) == (pdsc, "ACME32F401", ["pack"])


# ====================================================================== pack de démonstration
DEMO = os.path.join(ROOT, "examples", "flashalgo", "GdbTrace.QEMU_DFP.1.0.0.pack")


@pytest.fixture(scope="module")
def demo_pack():
    if not shutil.which("arm-none-eabi-gcc"):
        pytest.skip("arm-none-eabi-gcc absent")
    subprocess.run(["make", "-s", "-C", os.path.dirname(DEMO)], check=True)
    return DEMO


def test_demo_pack(demo_pack):
    d = pk.Pack.open(demo_pack).find("GDBTRACE-AN385")
    assert d.core == "Cortex-M3" and d.clock == 25_000_000 and d.svd == "SVD/mps2-an385.svd"
    assert d.algorithms[0].ram_start == 0x20100000


@needs_qemu
def test_pack_session_on_qemu(demo_pack, elf, tmp_path):
    """--pack --device --flash-loader pack : SVD du pack chargé (périphériques lisibles),
    image programmée par l'algorithme du pack, puce et cœur lus dans le script."""
    data = os.urandom(3000)
    image = tmp_path / "motif.bin"
    image.write_bytes(data)
    script = tmp_path / "s.py"
    script.write_text(
        f"assert gdbtrace.mem(0x00300000, {len(data)}) == open({str(image)!r}, 'rb').read()\n"
        "assert gdbtrace.device is not None and gdbtrace.device.peripheral('TIMER0')\n"
        "chip = gdbtrace.chip_info()\n"
        "assert chip.name == 'GDBTRACE-AN385'\n"
        "print('OK')\n")
    r = subprocess.run([sys.executable, "-m", "gdbtrace", elf, "--batch", "--pack", demo_pack,
                        "--device", "gdbtrace-an385", "--flash-loader", "pack",
                        "--program", f"{image}@0x00300000", "--script", str(script)],
                       cwd=ROOT, capture_output=True, text=True, timeout=120)
    out = r.stdout + r.stderr
    assert r.returncode == 0 and "OK" in r.stdout, out
    assert "puce : GDBTRACE-AN385 (ARM) — GdbTrace.QEMU_DFP 1.0.0" in out
    assert "par ram-flash.FLM" in out and "conforme au pack" in out
    assert "attention" not in out


@needs_qemu
def test_core_mismatch_warned(demo_pack, faults_elfs, tmp_path):
    """Le pack décrit un Cortex-M3 ; une cible Cortex-M0 (QEMU microbit) : dit."""
    r = subprocess.run([sys.executable, "-m", "gdbtrace", faults_elfs["m0"], "--machine", "microbit",
                        "--batch", "--pack", demo_pack, "--device", "GDBTRACE-AN385"],
                       cwd=ROOT, capture_output=True, text=True, timeout=120)
    assert "attention : le pack décrit GDBTRACE-AN385 avec un Cortex-M3, la cible répond Cortex-M0" \
        in r.stdout + r.stderr, r.stdout + r.stderr


def test_gui_chip_tab_and_device_chooser(pdsc, tmp_path):
    """Onglet Puce de la vue Système ; bouton « Choisir… » de la boîte de dialogue."""
    if not os.environ.get("DISPLAY"):
        pytest.skip("pas de DISPLAY")
    from test_gui import close_app

    from gdbtrace.core.config import SessionConfig
    from gdbtrace.gui.app import App
    from gdbtrace.gui.config_dialog import ConfigEditor
    from gdbtrace.views.system import SystemView
    app = App(layout_file=os.devnull)
    orig = app.api
    ed = None
    try:
        view = app.views.find(SystemView) or app.views.open(SystemView, "data")
        view.render_device()
        assert "aucune puce" in view.dev_info.cget("text")

        class Api:                                  # sans session : la puce seule
            def chip_rows(self):
                from gdbtrace.core.scripting import ScriptAPI
                return ScriptAPI.chip_rows(self)
            chip = pk.Pack.open(pdsc).find("ACME32F446RETx")
            trace = None
        app.api = Api()
        view.render_device()
        assert view.dev_info.cget("text").startswith("ACME32F446RETx (Acme)")
        sections = [view.dev_tree.item(i)["text"] for i in view.dev_tree.get_children()]
        assert sections[:3] == ["Processeur", "Mémoires", "Algorithmes de flash"]
        ed = ConfigEditor(app.root, SessionConfig({"cible": {"pack": pdsc}}, str(tmp_path / "c.yaml")))
        ed._choose_device()
        top = ed.choose_window
        lb = next(w for w in top.winfo_children() if w.winfo_class() == "Listbox")
        assert lb.size() == 4
        filt = next(w for w in top.winfo_children() if w.winfo_class() == "TEntry")
        filt.insert(0, "ZE")
        app.root.update()
        assert lb.size() == 1
        lb.selection_set(0)
        btn = next(w for w in top.winfo_children() if w.winfo_class() == "TButton")
        btn.invoke()
        assert ed.vars[("cible", "device")].get() == "ACME32F446ZETx"
    finally:
        if ed is not None:                      # rien ne doit survivre à l'App (tests suivants)
            if getattr(ed, "choose_window", None) is not None and ed.choose_window.winfo_exists():
                ed.choose_window.destroy()
            ed.destroy()
        app.api = orig
        close_app(app)
