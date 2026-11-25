"""Configurations de session (menu Configuration, `--config FICHIER.yaml`).

Une configuration décrit une session comme la ligne de commande : la cible (ELF, SVD,
fréquence du cœur, programmation), le serveur gdb (QEMU, OpenOCD, ST-LINK_gdbserver, gdb seul
pour un programme de l'hôte, ou un serveur déjà lancé), l'OS (FreeRTOS, Pthread ou aucun),
le script et la couverture, et les options de trace. Elle **se traduit en arguments** de
`build_parser()` (`to_argv`), si bien que le GUI, le mode `--batch` et la ligne de commande
partagent les mêmes options, la même mise en route et les mêmes erreurs.

Format YAML (version 1) : sections en français ; à l'intérieur, les noms des options de la
ligne de commande sans leurs tirets de tête (`cpu-hz`, `swo-config`, `var-trace`…) :

    gdbtrace: 1
    nom: Nucleo-G070RB
    cible: {elf: ../examples/nucleo-g070/nucleo-g070.elf, svd: STM32G070.svd,
            cpu-hz: 16000000, programmer: true, verifier: true, go: false}
    serveur: {type: openocd, openocd: [nucleo-g070rb.cfg]}
    os: freertos
    script: ../examples/scripts/rtos.py
    couverture: {fusionner: [], exporter: [couverture.html]}
    trace: {swo-config: true, var-trace: [produced:write]}

Un chemin relatif l'est au fichier YAML (s'il existe à cet endroit ; sinon il est gardé tel
quel : fichier de configuration OpenOCD de son dossier de scripts, SVD donné par son seul nom
et cherché dans STM32CubeIDE). À l'enregistrement, un chemin proche du fichier (au plus deux
`..`) est écrit relatif, les autres absolus.
"""
from __future__ import annotations

import copy
import os
from dataclasses import dataclass, field
from typing import Any

FORMAT_VERSION = 1
SERVER_TYPES = {
    "qemu": "QEMU (sans carte)",
    "openocd": "OpenOCD",
    "stlink": "ST-LINK_gdbserver (STMicroelectronics)",
    "gdb": "gdb (programme de l'hôte)",
    "remote": "serveur gdb déjà lancé",
}
OS_TYPES = {"aucun": "Aucun", "freertos": "FreeRTOS", "pthread": "Pthread (programme de l'hôte)"}

#: clés propres à chaque type de serveur, et clés communes
SERVER_KEYS = {
    "qemu": ["machine"],
    "openocd": ["openocd", "rtos"],
    "stlink": ["stlink", "stlink-ap", "ram-boot"],
    "gdb": ["args", "cwd", "pid", "non-stop"],
    "remote": ["remote", "tcl", "remote-exec", "args", "cwd", "non-stop"],
}
SERVER_COMMON = ["gdb", "gdb-port", "no-load"]
CIBLE_KEYS = ["elf", "svd", "cpu-hz", "programmer", "verifier", "go"]
COUVERTURE_KEYS = ["fusionner", "exporter"]
#: options de trace de la ligne de commande, dans l'ordre de --help
TRACE_KEYS = ["swo", "swo-file", "swo-formatter", "swo-config", "swo-hz", "swo-port", "var-trace",
              "etm-sim", "trace", "trace-file", "etm-config", "etm-cycle-accurate", "etm-timestamps",
              "etm-width", "etm-port", "etr-buffer", "etr-at-stop", "etm-stall", "stm32", "etm-filter",
              "etm-start", "etm-stop",
              "mtb", "mtb-buffer", "mtb-sim", "mtb-autohalt", "mtb-probe"]
TOP_KEYS = ["gdbtrace", "nom", "cible", "serveur", "os", "script", "couverture", "trace"]

#: clés dont la valeur est un chemin de fichier (relatif au YAML)
PATH_KEYS = {("cible", "elf"), ("cible", "svd"), ("serveur", "openocd"), ("serveur", "cwd"),
             ("", "script"), ("couverture", "fusionner"), ("couverture", "exporter"),
             ("trace", "swo-file"), ("trace", "trace-file")}
#: clé → option de la ligne de commande (quand le nom diffère)
OPTION_OF = {("cible", "programmer"): "--program", ("couverture", "fusionner"): "--coverage-merge",
             ("couverture", "exporter"): "--coverage"}
#: une option qui choisit le serveur (groupe exclusif de la ligne de commande)
SERVER_DESTS = ("qemu", "openocd", "stlink", "remote", "native", "pid")


class ConfigError(ValueError):
    """Configuration invalide : le message nomme la clé fautive."""


def _parser():
    from ..__main__ import build_parser
    return build_parser()


def _action(parser, option: str):
    try:
        return parser._option_string_actions[option]
    except KeyError:
        raise ConfigError(f"option inconnue : {option}") from None


def _where(section: str, key: str) -> str:
    return f"{section}.{key}" if section else key


# ====================================================================== chemins
def _resolve(value: str, base: str) -> str:
    """Fichier de la session (ELF, script, captures, couverture) : relatif au YAML."""
    if not isinstance(value, str) or not value or os.path.isabs(value):
        return value
    return os.path.normpath(os.path.join(base, value))


def _resolve_found(value: str, base: str) -> str:
    """Fichier relatif au YAML s'il y est, sinon la valeur telle quelle : fichier du dossier de
    scripts d'OpenOCD (`board/st_nucleo_f4.cfg`), SVD donné par son seul nom (STM32CubeIDE)."""
    if not isinstance(value, str) or not value or os.path.isabs(value):
        return value
    candidate = os.path.normpath(os.path.join(base, value))
    if os.path.exists(candidate):
        return candidate
    return value


def _relativize(value: str, base: str) -> str:
    """Chemin absolu proche du YAML (au plus deux `..`) : écrit relatif."""
    if not isinstance(value, str) or not os.path.isabs(value):
        return value
    rel = os.path.relpath(value, base)
    ups = 0
    for part in rel.split(os.sep):
        if part != os.pardir:
            break
        ups += 1
    return rel if ups <= 2 else value


def _map_paths(data: dict, fn) -> dict:
    out = copy.deepcopy(data)
    for section, key in PATH_KEYS:
        holder = out if not section else out.get(section)
        if not isinstance(holder, dict) or key not in holder:
            continue
        v = holder[key]
        holder[key] = [fn(section, key, x) for x in v] if isinstance(v, list) else fn(section, key, v)
    return out


# ====================================================================== configuration
@dataclass
class SessionConfig:
    """Une configuration de session : le contenu du YAML (`data`), son fichier (`path`, None
    tant qu'elle n'est pas enregistrée) et son état (`modified`). Les chemins de `data` sont
    tels qu'écrits ; `to_argv()` les résout par rapport au dossier du fichier."""
    data: dict = field(default_factory=dict)
    path: str | None = None
    modified: bool = False

    # ------------------------------------------------------------------ accès
    @property
    def base_dir(self) -> str:
        return os.path.dirname(os.path.abspath(self.path)) if self.path else os.getcwd()

    @property
    def name(self) -> str:
        if self.data.get("nom"):
            return str(self.data["nom"])
        if self.path:
            return os.path.splitext(os.path.basename(self.path))[0]
        return "sans nom"

    @property
    def os(self) -> str:
        return self.data.get("os") or "aucun"

    @property
    def server(self) -> str:
        return (self.data.get("serveur") or {}).get("type", "qemu")

    def section(self, name: str) -> dict:
        return self.data.setdefault(name, {})

    def resolved(self) -> dict:
        """`data` aux chemins résolus (absolus quand le fichier existe à côté du YAML)."""
        base = self.base_dir

        def fn(section, key, v):
            if (section, key) == ("serveur", "openocd"):
                return _resolve_found(v, base)
            if (section, key) == ("cible", "svd") and isinstance(v, str) and "/" not in v \
                    and os.sep not in v:
                return _resolve_found(v, base)
            return _resolve(v, base)
        return _map_paths(self.data, fn)

    # ------------------------------------------------------------------ fichier
    @classmethod
    def load(cls, path: str) -> "SessionConfig":
        import yaml
        try:
            with open(path, encoding="utf-8") as fh:
                data = yaml.safe_load(fh)
        except OSError as exc:
            raise ConfigError(f"configuration illisible : {exc}") from None
        except yaml.YAMLError as exc:
            raise ConfigError(f"{os.path.basename(path)} : YAML invalide : {exc}") from None
        cfg = cls(data or {}, os.path.abspath(path))
        cfg.check()
        return cfg

    def save(self, path: str | None = None) -> None:
        """Écrit le YAML (dans `path`, qui devient son fichier). Les chemins proches du
        nouveau dossier s'écrivent relatifs."""
        import yaml
        resolved = self.resolved()
        target = os.path.abspath(path or self.path or "gdbtrace.yaml")
        base = os.path.dirname(target)
        data = _map_paths(resolved, lambda s, k, v: _relativize(v, base))
        data = {"gdbtrace": FORMAT_VERSION, **{k: v for k, v in data.items() if k != "gdbtrace"}}
        data = _clean(data)
        with open(target, "w", encoding="utf-8") as fh:
            fh.write(f"# Configuration GdbTrace : {self.name}\n")
            yaml.safe_dump(data, fh, allow_unicode=True, sort_keys=False, default_flow_style=False)
        self.data, self.path, self.modified = data, target, False

    # ------------------------------------------------------------------ validation
    def check(self) -> None:
        """Clés, types et cohérence ; puis les règles de la ligne de commande (`validate`).
        Lève ConfigError avec un message qui nomme la clé."""
        d = self.data
        if not isinstance(d, dict):
            raise ConfigError("la configuration doit être un dictionnaire YAML (clé: valeur)")
        version = d.get("gdbtrace", FORMAT_VERSION)
        if version != FORMAT_VERSION:
            raise ConfigError(f"gdbtrace: {version} : format inconnu (version {FORMAT_VERSION} attendue)")
        _known(d, TOP_KEYS, "")
        for name, keys in (("cible", CIBLE_KEYS), ("couverture", COUVERTURE_KEYS), ("trace", TRACE_KEYS)):
            sec = d.get(name)
            if sec is None:
                continue
            if not isinstance(sec, dict):
                raise ConfigError(f"{name} : un dictionnaire est attendu")
            _known(sec, keys, name)
        srv = d.get("serveur") or {}
        if not isinstance(srv, dict):
            raise ConfigError("serveur : un dictionnaire est attendu")
        kind = srv.get("type", "qemu")
        if kind not in SERVER_TYPES:
            raise ConfigError(f"serveur.type : « {kind} » inconnu ({', '.join(SERVER_TYPES)})")
        for key in srv:
            if key == "type" or key in SERVER_COMMON or key in SERVER_KEYS[kind]:
                continue
            owner = next((t for t, keys in SERVER_KEYS.items() if key in keys), None)
            raise ConfigError(f"serveur.{key} : " + (f"option du serveur {owner}, pas de {kind}"
                                                      if owner else "clé inconnue"))
        if self.os not in OS_TYPES:
            raise ConfigError(f"os : « {self.os} » inconnu ({', '.join(OS_TYPES)})")
        from ..__main__ import validate
        args = self.parse()
        errors = validate(args)
        if errors:
            raise ConfigError(errors[0])

    # ------------------------------------------------------------------ ligne de commande
    def to_argv(self, server: bool = True) -> list[str]:
        """Arguments de `build_parser()` équivalents (chemins résolus). `server=False` : sans
        le serveur (la ligne de commande en donne un autre)."""
        p = _parser()
        d = self.resolved()
        argv: list[str] = []
        cible = d.get("cible") or {}
        if cible.get("elf"):
            argv.append(str(cible["elf"]))
        if server:
            srv = dict(d.get("serveur") or {})
            kind = srv.pop("type", "qemu")
            if kind == "qemu":
                argv.append("--qemu")
            elif kind == "openocd":
                argv += ["--openocd", *[str(c) for c in _as_list(srv.pop("openocd", []), "serveur.openocd")]]
            elif kind == "stlink":
                sn = srv.pop("stlink", None)
                argv += ["--stlink"] + ([str(sn)] if sn not in (None, "", True) else [])
            elif kind == "gdb" and not srv.get("pid"):   # --pid : programme de l'hôte déjà
                argv.append("--native")                  # lancé (exclusif de --native)
            elif kind == "remote":
                if not srv.get("remote"):
                    raise ConfigError("serveur.remote : hôte:port du serveur gdb déjà lancé")
            for key, v in srv.items():
                argv += _option(p, "serveur", key, v)
        else:
            srv = {k: v for k, v in (d.get("serveur") or {}).items() if k in SERVER_COMMON}
            for key, v in srv.items():
                argv += _option(p, "serveur", key, v)
        for key in ("svd", "cpu-hz", "go"):
            if key in cible:
                argv += _option(p, "cible", key, cible[key])
        if cible.get("programmer"):
            argv.append("--program")
        if cible.get("verifier") is False:
            argv.append("--no-verify")
        if d.get("os") and d["os"] != "aucun":
            argv += ["--os", d["os"]]
        if d.get("script"):
            argv += ["--script", str(d["script"])]
        for key, v in (d.get("couverture") or {}).items():
            argv += _option(p, "couverture", key, v)
        for key, v in (d.get("trace") or {}).items():
            argv += _option(p, "trace", key, v)
        return argv

    def parse(self):
        """Les arguments de la configuration analysés par `build_parser()`."""
        p = _parser()
        try:
            return p.parse_args(self.to_argv())
        except SystemExit:
            raise ConfigError(f"configuration {self.name} : arguments refusés ({' '.join(self.to_argv())})") from None

    @classmethod
    def from_args(cls, args, name: str | None = None) -> "SessionConfig":
        """Configuration d'une session lancée en ligne de commande (Enregistrer sous…)."""
        from ..__main__ import is_native_elf
        p = _parser()
        defaults = vars(p.parse_args([]))

        def changed(dest: str) -> bool:
            return getattr(args, dest, None) != defaults.get(dest)
        d: dict[str, Any] = {"gdbtrace": FORMAT_VERSION}
        if name:
            d["nom"] = name
        cible: dict[str, Any] = {}
        if args.elf:
            cible["elf"] = os.path.abspath(args.elf)
        if args.svd:
            cible["svd"] = os.path.abspath(args.svd) if os.path.exists(args.svd) else args.svd
        if changed("cpu_hz"):
            cible["cpu-hz"] = args.cpu_hz
        if args.program is not None:
            cible["programmer"] = True
        if args.no_verify:
            cible["verifier"] = False
        if args.go:
            cible["go"] = True
        if cible:
            d["cible"] = cible
        srv: dict[str, Any] = {}
        if args.openocd is not None:
            srv = {"type": "openocd", "openocd": [os.path.abspath(c) if os.path.exists(c) else c
                                                  for c in args.openocd]}
        elif args.stlink is not None:
            srv = {"type": "stlink"}
            if args.stlink:
                srv["stlink"] = args.stlink
        elif args.remote:
            srv = {"type": "remote", "remote": args.remote}
        elif args.native or args.pid or (not args.qemu and args.elf and is_native_elf(args.elf)):
            srv = {"type": "gdb"}
        else:
            srv = {"type": "qemu"}
        for key in SERVER_KEYS[srv["type"]] + SERVER_COMMON:
            if key in ("openocd", "stlink", "remote"):
                continue
            dest = key.replace("-", "_")
            if changed(dest):
                v = getattr(args, dest)
                srv[key] = os.path.abspath(v) if key == "cwd" else v
        d["serveur"] = srv
        if getattr(args, "os", None):
            d["os"] = args.os
        if args.script:
            d["script"] = os.path.abspath(args.script)
        cov = {}
        if args.coverage_merge:
            cov["fusionner"] = [os.path.abspath(x) for x in args.coverage_merge]
        if args.coverage:
            cov["exporter"] = [os.path.abspath(x) for x in args.coverage]
        if cov:
            d["couverture"] = cov
        trace = {}
        for key in TRACE_KEYS:
            dest = key.replace("-", "_")
            if changed(dest):
                v = getattr(args, dest)
                trace[key] = os.path.abspath(v) if key in ("swo-file", "trace-file") else v
        if trace:
            d["trace"] = trace
        return cls(d)

    def copy(self) -> "SessionConfig":
        return SessionConfig(copy.deepcopy(self.data), self.path, self.modified)


# ====================================================================== outils
def _known(section: dict, keys: list[str], name: str) -> None:
    for key in section:
        if key not in keys:
            where = _where(name, key)
            raise ConfigError(f"{where} : clé inconnue (attendues : {', '.join(keys)})")


def _as_list(v: Any, where: str) -> list:
    if v is None:
        return []
    if isinstance(v, (list, tuple)):
        return list(v)
    if isinstance(v, (str, int)):
        return [v]
    raise ConfigError(f"{where} : une liste est attendue")


def _option(parser, section: str, key: str, value: Any) -> list[str]:
    """Arguments d'une clé, d'après l'action argparse de l'option correspondante : drapeau
    (booléen), liste (append ou nargs='*'), valeur facultative (nargs='?' : `true` = sans
    valeur), valeur simple."""
    option = OPTION_OF.get((section, key), f"--{key}")
    act = _action(parser, option)
    where = _where(section, key)
    if value is None:
        return []
    if act.nargs == 0:                                     # drapeau (store_true)
        if not isinstance(value, bool):
            raise ConfigError(f"{where} : true ou false attendu, pas « {value} »")
        return [option] if value else []
    if type(act).__name__ == "_AppendAction":
        return [x for v in _as_list(value, where) for x in (option, str(v))]
    if act.nargs == "*":
        return [option, *[str(v) for v in _as_list(value, where)]]
    if act.nargs == "?":                                   # true : l'option seule
        if value is False:
            return []
        return [option] if value is True else [option, str(value)]
    if isinstance(value, (bool, list, dict)):
        raise ConfigError(f"{where} : une valeur simple est attendue, pas « {value} »")
    if act.type in (int,) or getattr(act.type, "__name__", "") == "int_auto":
        try:
            int(str(value), 0)
        except ValueError:
            raise ConfigError(f"{where} : entier attendu, pas « {value} »") from None
    if act.choices and str(value) not in [str(c) for c in act.choices]:
        raise ConfigError(f"{where} : « {value} » hors de {', '.join(map(str, act.choices))}")
    return [option, str(value)]


def _clean(d: Any) -> Any:
    """Sans sections vides, pour un YAML lisible."""
    if isinstance(d, dict):
        out = {k: _clean(v) for k, v in d.items()}
        return {k: v for k, v in out.items() if v not in ({}, None)}
    return d
