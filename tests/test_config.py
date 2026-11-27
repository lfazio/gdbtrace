"""Configurations de session (core/config.py, `--config`) : format YAML, chemins, erreurs,
traduction en ligne de commande, priorité de la ligne de commande, exemples de configs/."""
import os
import subprocess
import sys

import pytest
from conftest import ROOT, needs_qemu

from gdbtrace.__main__ import build_parser, parse_command_line
from gdbtrace.core import svd
from gdbtrace.core.config import ConfigError, SessionConfig

CONFIGS = os.path.join(ROOT, "configs")


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return str(path)


def args_of(cfg):
    return vars(cfg.parse())


# ====================================================================== format, chemins
def test_save_load_roundtrip_and_relative_paths(tmp_path):
    """Les chemins proches du YAML s'écrivent relatifs ; relus, ils pointent au même endroit."""
    elf = tmp_path / "fw" / "demo.elf"
    elf.parent.mkdir()
    elf.write_bytes(b"\x7fELF")
    cfg = SessionConfig({"nom": "essai", "cible": {"elf": str(elf), "cpu-hz": 8_000_000,
                                                   "programmer": True},
                         "serveur": {"type": "openocd", "openocd": ["board/st_nucleo_f4.cfg"]},
                         "os": "freertos", "trace": {"swo-config": True, "var-trace": ["n:rw+pc"]}})
    path = tmp_path / "cfg" / "essai.yaml"
    path.parent.mkdir()
    cfg.save(str(path))
    text = path.read_text()
    assert "elf: ../fw/demo.elf" in text and "board/st_nucleo_f4.cfg" in text
    assert text.startswith("# Configuration GdbTrace : essai\ngdbtrace: 1\n")
    again = SessionConfig.load(str(path))
    a = args_of(again)
    assert a["elf"] == str(elf) and a["openocd"] == ["board/st_nucleo_f4.cfg"]   # chemin OpenOCD gardé
    assert a["cpu_hz"] == 8_000_000 and a["program"] == "" and a["os"] == "freertos"
    assert a["swo_config"] and a["var_trace"] == ["n:rw+pc"]
    assert again.name == "essai" and not again.modified
    far = SessionConfig({"cible": {"elf": "/opt/firmware/x.elf"}, "serveur": {"type": "qemu"}})
    far.save(str(tmp_path / "far.yaml"))
    assert "elf: /opt/firmware/x.elf" in (tmp_path / "far.yaml").read_text()  # loin : absolu


def test_missing_elf_is_relative_to_the_yaml(tmp_path):
    """Un ELF pas encore construit reste relatif au YAML, pas au dossier courant."""
    path = write(tmp_path / "c" / "x.yaml", "cible: {elf: ../build/fw.elf}\nserveur: {type: qemu}\n")
    assert SessionConfig.load(path).parse().elf == str(tmp_path / "build" / "fw.elf")


def test_svd_by_name(tmp_path, monkeypatch):
    """Un SVD donné par son seul nom est cherché dans STM32CubeIDE (ou GDBTRACE_SVD_DIR)."""
    (tmp_path / "svd").mkdir()
    (tmp_path / "svd" / "STM32G070.svd").write_text("<device/>")
    monkeypatch.setenv("GDBTRACE_SVD_DIR", str(tmp_path / "svd"))
    assert svd.locate("STM32G070.svd") == (str(tmp_path / "svd" / "STM32G070.svd"), None)
    path, warning = svd.locate("STM32F072.svd")
    assert path is None and "STM32F072.svd introuvable" in warning and "GDBTRACE_SVD_DIR" in warning
    cfg = SessionConfig.load(write(tmp_path / "g.yaml", "cible: {svd: STM32G070.svd}\n"))
    assert cfg.parse().svd == "STM32G070.svd"                 # nom gardé : cherché à la connexion


# ====================================================================== erreurs
@pytest.mark.parametrize("text,message", [
    ("cible: {elfe: a.elf}", "cible.elfe : clé inconnue"),
    ("couleur: rouge", "couleur : clé inconnue"),
    ("serveur: {type: jlink}", "serveur.type : « jlink » inconnu"),
    ("serveur: {type: openocd, stlink-ap: 1}", "serveur.stlink-ap : option du serveur stlink, pas de openocd"),
    ("os: zephyr", "os : « zephyr » inconnu"),
    ("os: pthread\nserveur: {type: openocd}", "--os pthread : un programme de l'hôte"),
    ("os: freertos\nserveur: {type: gdb}", "--os freertos : une cible Cortex-M"),
    ("trace: {swo-config: oui}", "trace.swo-config : true ou false attendu"),
    ("trace: {etm-port: usb, etm-config: true}", "trace.etm-port : « usb » hors de parallel, swo, etr"),
    ("trace: {swo-hz: vite}", "trace.swo-hz : entier attendu"),
    ("trace: {etr-buffer: '0x0:64'}", "--etr-buffer / --etr-at-stop : avec --etm-config --etm-port etr"),
    ("gdbtrace: 2", "gdbtrace: 2 : format inconnu"),
    ("- a\n- b", "doit être un dictionnaire"),
])
def test_errors_name_the_key(tmp_path, text, message):
    with pytest.raises(ConfigError, match=message.replace("(", r"\(").replace(")", r"\)").replace("+", r"\+")):
        SessionConfig.load(write(tmp_path / "x.yaml", text))


def test_yaml_syntax_error(tmp_path):
    with pytest.raises(ConfigError, match="x.yaml : YAML invalide"):
        SessionConfig.load(write(tmp_path / "x.yaml", "cible: {elf: [a\n"))


# ====================================================================== ligne de commande
@pytest.mark.parametrize("server,expected", [
    ({"type": "qemu", "machine": "microbit"}, ["--qemu", "--machine", "microbit"]),
    ({"type": "qemu", "machine": "mps2-an505", "qemu-load": ["/a/ns.elf", "/a/boot.elf"]},
     ["--qemu", "--machine", "mps2-an505", "--qemu-load", "/a/ns.elf", "--qemu-load", "/a/boot.elf"]),
    ({"type": "openocd", "openocd": ["a.cfg", "b.cfg"], "rtos": True}, ["--openocd", "a.cfg", "b.cfg", "--rtos"]),
    ({"type": "stlink", "stlink": "0042", "stlink-ap": 1, "ram-boot": True},
     ["--stlink", "0042", "--stlink-ap", "1", "--ram-boot"]),
    ({"type": "stlink"}, ["--stlink"]),
    ({"type": "gdb", "args": "1 2"}, ["--native", "--args", "1 2"]),
    ({"type": "remote", "remote": "127.0.0.1:3333", "tcl": "127.0.0.1:6666", "gdb": "arm-gdb"},
     ["--remote", "127.0.0.1:3333", "--tcl", "127.0.0.1:6666", "--gdb", "arm-gdb"]),
])
def test_servers_to_argv(server, expected):
    argv = SessionConfig({"serveur": server}).to_argv()
    assert argv == expected
    build_parser().parse_args(argv)                          # accepté tel quel


def test_command_line_overrides_config(tmp_path):
    """La ligne de commande l'emporte : une valeur remplace, une liste s'ajoute, un serveur d'un
    autre type remplace celui de la configuration avec ses options propres."""
    path = write(tmp_path / "n6.yaml", "cible: {elf: fw.elf, cpu-hz: 64000000}\n"
                                       "serveur: {type: stlink, stlink-ap: 1, ram-boot: true}\n"
                                       "trace: {swo-config: true, var-trace: [a]}\n")
    _p, a, cfg = parse_command_line(["--config", path, "--cpu-hz", "8000000", "--var-trace", "b"])
    assert cfg.name == "n6" and a.cpu_hz == 8_000_000 and a.var_trace == ["a", "b"]
    assert a.stlink == "" and a.stlink_ap == 1 and a.ram_boot and a.elf == str(tmp_path / "fw.elf")
    _p, a, _c = parse_command_line(["--config", path, "--stlink", "SN42"])   # même serveur : complété
    assert a.stlink == "SN42" and a.stlink_ap == 1 and a.ram_boot
    _p, a, _c = parse_command_line(["--config", path, "--qemu"])            # autre serveur : remplacé
    assert a.qemu and a.stlink is None and a.stlink_ap == 0 and not a.ram_boot
    _p, a, _c = parse_command_line(["--config", path, str(tmp_path / "autre.elf")])
    assert a.elf == str(tmp_path / "autre.elf")


def test_from_args_roundtrip(tmp_path):
    """Une session lancée en ligne de commande s'enregistre (Enregistrer sous…) et se relit à
    l'identique."""
    elf = tmp_path / "fw.elf"
    elf.write_bytes(b"\x7fELF")
    argv = [str(elf), "--openocd", "board/x.cfg", "--rtos", "--program", "--cpu-hz", "16000000",
            "--etm-config", "--etm-port", "swo", "--etm-stall", "--var-trace", "produced:write",
            "--os", "freertos", "--no-verify"]
    args = build_parser().parse_args(argv)
    cfg = SessionConfig.from_args(args, name="essai")
    cfg.save(str(tmp_path / "s.yaml"))
    again = vars(SessionConfig.load(str(tmp_path / "s.yaml")).parse())
    assert {k: v for k, v in again.items() if k != "config"} == \
        {k: v for k, v in vars(args).items() if k != "config"}


# ====================================================================== exemples
BOARD_ARGV = {   # sessions des tests sur carte, avant les configurations (ELF, serveur, options)
    "nucleo-f746zg.yaml": ["examples/nucleo-f746/nucleo-f746.elf", "--openocd", "configs/nucleo-f746zg.cfg",
                           "--program", "--cpu-hz", "16000000", "--etm-config", "--etm-port", "swo",
                           "--etm-stall", "--var-trace", "produced:write"],
    "nucleo-n657x0-q.yaml": ["examples/nucleo-n6/nucleo-n6.elf", "--stlink", "--stlink-ap", "1",
                             "--ram-boot", "--program", "--cpu-hz", "64000000", "--swo-config",
                             "--etm-config", "--etm-port", "etr", "--var-trace", "produced:write"],
    "nucleo-g070rb.yaml": ["examples/nucleo-g070/nucleo-g070.elf", "--openocd", "configs/nucleo-g070rb.cfg",
                           "--program", "--cpu-hz", "16000000"],
    "nucleo-f446re.yaml": ["examples/nucleo-f446/nucleo-f446.elf", "--openocd", "configs/nucleo-f446re.cfg",
                           "--program", "--cpu-hz", "16000000", "--etm-config", "--etm-port", "swo",
                           "--etm-stall", "--etm-start", "worker_step", "--etm-stop", "0x080002ac",
                           "--var-trace", "produced:write"],
    "nucleo-f072rb.yaml": ["examples/nucleo-f072/nucleo-f072.elf", "--openocd", "configs/nucleo-f072rb.cfg",
                           "--program", "--cpu-hz", "8000000"],
}


def _normal(args: dict) -> dict:
    out = {}
    for k, v in args.items():
        if k in ("config", "os", "svd"):
            continue
        if isinstance(v, str) and v and not v.startswith("-") and os.path.exists(os.path.join(ROOT, v)):
            v = os.path.normpath(os.path.join(ROOT, v))
        if isinstance(v, list):
            v = [os.path.normpath(os.path.join(ROOT, x)) if isinstance(x, str) and
                 os.path.exists(os.path.join(ROOT, x)) else x for x in v]
        out[k] = v
    return out


@pytest.mark.parametrize("name", sorted(f for f in os.listdir(CONFIGS) if f.endswith(".yaml")))
def test_example_configs(name, monkeypatch):
    monkeypatch.chdir(ROOT)
    cfg = SessionConfig.load(os.path.join(CONFIGS, name))
    args = cfg.parse()
    assert os.path.isabs(args.elf) and args.elf.startswith(ROOT)
    if name in BOARD_ARGV:
        """La configuration redonne exactement la session des tests sur carte (plus l'OS)."""
        board = vars(build_parser().parse_args(BOARD_ARGV[name]))
        board["elf"] = os.path.join(ROOT, board["elf"])
        assert _normal(vars(args)) == _normal(board)
        assert args.os == "freertos" and args.svd.endswith(".svd")


# ====================================================================== batch
def run(*args, timeout=180):
    return subprocess.run([sys.executable, "-m", "gdbtrace", *args], cwd=ROOT, capture_output=True,
                          text=True, timeout=timeout)


@needs_qemu
def test_batch_with_config(elf, tmp_path):
    """--batch --config : la configuration (QEMU, SVD, ETM simulé) avec un script de la ligne
    de commande et la couverture écrite à la fin ; gdbtrace.config la montre au script."""
    script = write(tmp_path / "s.py", "print('config', gdbtrace.config['nom'], gdbtrace.config['os'])\n"
                                      "gdbtrace.break_('fib', temporary=True)\ngdbtrace.go()\ngdbtrace.wait()\n")
    html = tmp_path / "cov.html"
    r = run("--config", os.path.join(CONFIGS, "qemu-demo.yaml"), "--batch", "--script", script,
            "--coverage", str(html))
    assert r.returncode == 0, r.stderr
    assert "config Démo QEMU (Cortex-M3) aucun" in r.stdout
    assert html.exists() and "Couverture de code" in html.read_text()


def test_bad_config_on_the_command_line(tmp_path):
    r = run("--config", write(tmp_path / "x.yaml", "serveur: {type: jlink}\n"), "--batch")
    assert r.returncode == 2 and "serveur.type : « jlink » inconnu" in r.stderr


def test_native_and_trace_options_of_later_milestones():
    """--pid, --non-stop, --remote-exec, --etm-timestamps, --etr-at-stop : une option de la
    ligne de commande marche aussi dans une configuration (aller et retour)."""
    native = os.path.join(ROOT, "examples", "native", "pthreads")
    n6 = os.path.join(ROOT, "examples", "nucleo-n6", "nucleo-n6.elf")
    cases = [
        ({"cible": {"elf": native}, "serveur": {"type": "gdb", "pid": 1234, "non-stop": True},
          "os": "pthread"}, ["--pid", "1234", "--non-stop"], "--native"),
        ({"cible": {"elf": native}, "serveur": {"type": "remote", "remote": "127.0.0.1:2345",
                                                "remote-exec": "/srv/prog", "non-stop": True}},
         ["--remote", "127.0.0.1:2345", "--remote-exec", "/srv/prog", "--non-stop"], None),
        ({"cible": {"elf": n6}, "serveur": {"type": "stlink", "stlink-ap": 1, "ram-boot": True},
          "trace": {"etm-config": True, "etm-port": "etr", "etm-timestamps": True, "etr-at-stop": True}},
         ["--etm-timestamps", "--etr-at-stop"], None),
    ]
    for data, wanted, absent in cases:
        cfg = SessionConfig(data)
        argv = cfg.to_argv()
        assert all(w in argv for w in wanted), argv
        assert absent is None or absent not in argv, argv
        cfg.check()
        back = SessionConfig.from_args(cfg.parse()).data
        for section in ("serveur", "trace"):
            if section in data:
                assert back[section] == data[section], (section, back[section])
