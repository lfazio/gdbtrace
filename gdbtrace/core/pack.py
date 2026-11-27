"""CMSIS-Pack : une puce décrite par son pack (fichier .pdsc) : cœur, horloge, mémoires,
algorithmes de flash (FLM), description des périphériques (SVD).

Un pack est un zip (`.pack`) ou un dossier dépaqueté qui contient un `.pdsc` (XML). Ses puces
sont rangées en famille → sous-famille → puce → variante ; chaque niveau hérite des propriétés
du précédent : processeur (attributs fusionnés), mémoires et algorithmes (cumulés), `debug svd`
(le plus bas l'emporte). Les mémoires s'écrivent à l'ancienne (`id="IROM1"`, `"IRAM1"`) ou
avec `name` et `access`.

Où sont les packs : `--pack` (fichier ou dossier), sinon les packs installés par cmsis-toolbox
(`CMSIS_PACK_ROOT`, `~/.cache/arm/packs`), l'ancien dossier `~/.arm/Packs`, pyOCD
(`~/.local/share/cmsis-pack-manager`) ; la dernière version de chaque pack. GdbTrace ne
télécharge rien. Les fichiers d'un pack zippé (SVD, FLM) sont extraits dans
`~/.cache/gdbtrace/packs/<pack>/`.

Ignorés (dit par `notes`) : séquences de débogage, debugvars, puces à plusieurs processeurs
(le premier est pris).
"""
from __future__ import annotations

import os
import re
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field


class PackError(ValueError):
    """Pack illisible, ou puce introuvable / ambiguë."""


def _int(text, default: int = 0) -> int:
    if text is None or str(text).strip() == "":
        return default
    return int(str(text).strip(), 0)


def _true(text) -> bool:
    return str(text).strip().lower() in ("1", "true")


@dataclass
class Memory:
    name: str
    start: int
    size: int
    access: str                  # « rx », « rwx »…
    default: bool = False
    startup: bool = False

    @property
    def is_ram(self) -> bool:
        return "w" in self.access

    @property
    def end(self) -> int:
        return self.start + self.size


@dataclass
class Algorithm:
    path: str                    # chemin dans le pack
    start: int
    size: int
    ram_start: int | None = None
    ram_size: int | None = None
    default: bool = False


@dataclass
class Device:
    name: str
    vendor: str = ""
    family: str = ""
    sub_family: str = ""
    variant_of: str = ""         # variante : le nom de la puce dont elle dérive
    core: str = ""
    fpu: str = ""
    mpu: str = ""
    endian: str = ""
    clock: int = 0               # Dclock : fréquence maximale (Hz)
    memories: list[Memory] = field(default_factory=list)
    algorithms: list[Algorithm] = field(default_factory=list)
    svd: str | None = None       # chemin dans le pack
    description: str = ""
    pack: "Pack | None" = field(default=None, repr=False)
    notes: list[str] = field(default_factory=list)

    # ------------------------------------------------------------------ fichiers
    def svd_file(self) -> str | None:
        return self.pack.file(self.svd) if self.svd and self.pack else None

    def default_ram(self) -> tuple[int, int] | None:
        rams = [m for m in self.memories if m.is_ram]
        if not rams:
            return None
        ram = next((m for m in rams if m.default), rams[0])
        return ram.start, ram.size

    def flash_loaders(self, session, ram: tuple[int, int] | None = None) -> list:
        """Un FlashLoader par algorithme par défaut (tous s'il n'y en a pas), RAM de
        l'algorithme (RAMstart/RAMsize), sinon `ram`, sinon la RAM par défaut de la puce."""
        from .flashalgo import FlashLoader, FlmAlgo
        algos = [a for a in self.algorithms if a.default] or list(self.algorithms)
        if not algos:
            raise PackError(f"{self.name} : aucun algorithme de flash dans le pack")
        out = []
        for a in algos:
            where = (a.ram_start, a.ram_size) if a.ram_start is not None and a.ram_size else (
                ram or self.default_ram())
            if where is None:
                raise PackError(f"{self.name} : pas de RAM connue pour {a.path} (--flash-ram)")
            out.append(FlashLoader(session, FlmAlgo(self.pack.file(a.path)), where, clock=self.clock))
        return out

    # ------------------------------------------------------------------ affichage
    @property
    def title(self) -> str:
        pk = self.pack
        src = f" — {pk.vendor}.{pk.name} {pk.version}" if pk else ""
        return f"{self.name} ({self.vendor}){src}"

    def rows(self) -> list[tuple[str, list[tuple[str, str, str]]]]:
        """Sections de la vue Système (onglet Puce) : (titre, [(nom, valeur, détail)])."""
        proc = [("cœur", self.core or "?", ", ".join(x for x in (self.fpu, self.mpu, self.endian) if x)),
                ("horloge", f"{self.clock / 1e6:g} MHz" if self.clock else "?", "fréquence maximale (Dclock)"),
                ("famille", self.family, self.sub_family)]
        if self.variant_of:
            proc.append(("variante de", self.variant_of, ""))
        mems = [(m.name, f"{m.start:#010x}-{m.end - 1:#010x}",
                 f"{_size(m.size)}, {m.access}" + (", par défaut" if m.default else "")
                 + (", démarrage" if m.startup else "")) for m in self.memories]
        algos = [(os.path.basename(a.path), f"{a.start:#010x}+{a.size:#x}",
                  (f"RAM {a.ram_start:#010x}+{a.ram_size:#x}" if a.ram_start is not None and a.ram_size else
                   "RAM par défaut") + (", par défaut" if a.default else "")) for a in self.algorithms]
        out = [("Processeur", proc), ("Mémoires", mems), ("Algorithmes de flash", algos)]
        if self.svd:
            out.append(("Périphériques", [("SVD", os.path.basename(self.svd), self.svd)]))
        if self.notes:
            out.append(("Ignoré", [(n, "", "") for n in self.notes]))
        return out


def _size(n: int) -> str:
    for unit, k in (("Mo", 1 << 20), ("Ko", 1 << 10)):
        if n >= k and n % k == 0:
            return f"{n // k} {unit}"
    return f"{n} o"


# ====================================================================== pack
class Pack:
    """Un pack ouvert : son .pdsc, ses puces, ses fichiers."""

    def __init__(self, path: str, pdsc_text: bytes, member_root: str = "", zipped: bool = False,
                 base_dir: str | None = None):
        self.path = path
        self.zipped = zipped
        self.member_root = member_root          # dossier du .pdsc dans le zip
        self.base_dir = base_dir                # dossier du .pdsc dépaqueté
        try:
            root = ET.fromstring(pdsc_text)
        except ET.ParseError as exc:
            raise PackError(f"{path} : .pdsc illisible ({exc})") from exc
        self.vendor = (root.findtext("vendor") or "").strip()
        self.name = (root.findtext("name") or "").strip()
        rel = root.find("releases/release")
        self.version = (rel.get("version") if rel is not None else "") or ""
        self.description = " ".join((root.findtext("description") or "").split())
        self.devices = _devices(root, self)
        self._cache_dir: str | None = None

    @classmethod
    def open(cls, path: str) -> "Pack":
        """`.pack` (zip), `.pdsc`, ou dossier qui en contient un."""
        path = os.path.abspath(os.path.expanduser(path))
        if os.path.isdir(path):
            pdscs = sorted(f for f in os.listdir(path) if f.lower().endswith(".pdsc"))
            if not pdscs:
                raise PackError(f"{path} : aucun .pdsc dans ce dossier")
            path = os.path.join(path, pdscs[0])
        if not os.path.isfile(path):
            raise PackError(f"pack introuvable : {path}")
        if path.lower().endswith(".pdsc"):
            with open(path, "rb") as fh:
                return cls(path, fh.read(), base_dir=os.path.dirname(path))
        try:
            z = zipfile.ZipFile(path)
        except zipfile.BadZipFile as exc:
            raise PackError(f"{path} : ni .pack (zip) ni .pdsc") from exc
        with z:
            names = [n for n in z.namelist() if n.lower().endswith(".pdsc")]
            if not names:
                raise PackError(f"{path} : pas de .pdsc dans le pack")
            name = min(names, key=lambda n: n.count("/"))
            return cls(path, z.read(name), member_root=os.path.dirname(name), zipped=True)

    def file(self, member: str) -> str:
        """Fichier du pack (chemin du .pdsc, `\\` ou `/`) en local : extrait d'un zip dans le
        cache, ou chemin du dossier dépaqueté."""
        rel = member.replace("\\", "/").lstrip("/")
        if not self.zipped:
            p = os.path.join(self.base_dir or "", *rel.split("/"))
            if not os.path.isfile(p):
                raise PackError(f"{self.vendor}.{self.name} : fichier absent du pack : {member}")
            return p
        if self._cache_dir is None:
            base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
            self._cache_dir = os.path.join(base, "gdbtrace", "packs",
                                           f"{self.vendor}.{self.name}.{self.version or 'x'}")
        target = os.path.join(self._cache_dir, *rel.split("/"))
        src_name = f"{self.member_root}/{rel}" if self.member_root else rel
        with zipfile.ZipFile(self.path) as z:
            info = next((i for i in z.infolist() if i.filename.lower() == src_name.lower()), None)
            if info is None:
                raise PackError(f"{self.vendor}.{self.name} : fichier absent du pack : {member}")
            if not os.path.isfile(target) or os.path.getsize(target) != info.file_size:
                os.makedirs(os.path.dirname(target), exist_ok=True)
                with z.open(info) as src, open(target + ".part", "wb") as dst:
                    dst.write(src.read())
                os.replace(target + ".part", target)
        return target

    def find(self, name: str) -> Device:
        return find_device(name, [self])


def _merge_processor(acc: dict, el) -> None:
    for p in el.findall("processor"):
        for k, v in p.attrib.items():
            acc[k] = v
        if p.get("Pname"):
            acc.setdefault("_pnames", []).append(p.get("Pname"))


def _memories(el) -> list[Memory]:
    out = []
    for m in el.findall("memory"):
        ident = m.get("name") or m.get("id") or "?"
        access = m.get("access")
        if access is None:                      # ancien style : IROMn / IRAMn
            access = "rwx" if "RAM" in (m.get("id") or "").upper() else "rx"
        out.append(Memory(ident, _int(m.get("start")), _int(m.get("size")), access,
                          _true(m.get("default", "0")), _true(m.get("startup", "0"))))
    return out


def _algorithms(el) -> list[Algorithm]:
    out = []
    for a in el.findall("algorithm"):
        ram_start = a.get("RAMstart")
        out.append(Algorithm(a.get("name", ""), _int(a.get("start")), _int(a.get("size")),
                             _int(ram_start) if ram_start else None,
                             _int(a.get("RAMsize")) if a.get("RAMsize") else None,
                             _true(a.get("default", "0"))))
    return out


def _svd(el, current: str | None) -> str | None:
    for d in el.findall("debug"):
        if d.get("svd"):
            current = d.get("svd")
    return current


def _devices(root, pack: Pack) -> list[Device]:
    out: list[Device] = []
    sequences = root.find("sequences") is not None

    def build(name, chain: list, family, sub, variant_of="") -> Device:
        proc: dict = {}
        mems, algos, svd, desc = [], [], None, ""
        for el in chain:
            _merge_processor(proc, el)
            mems += _memories(el)
            algos += _algorithms(el)
            svd = _svd(el, svd)
            desc = " ".join((el.findtext("description") or desc).split())
        vendor = (chain[0].get("Dvendor") or "").split(":")[0]
        d = Device(name, vendor, family, sub, variant_of, proc.get("Dcore", ""), proc.get("Dfpu", ""),
                   proc.get("Dmpu", ""), proc.get("Dendian", ""), _int(proc.get("Dclock")), mems, algos,
                   svd, desc, pack)
        if len(proc.get("_pnames", [])) > 1:
            d.notes.append(f"plusieurs processeurs ({', '.join(proc['_pnames'])}) : le premier décrit")
        if sequences or any(el.find("debugvars") is not None for el in chain):
            d.notes.append("séquences de débogage et debugvars du pack")
        return d

    for fam in root.findall("devices/family"):
        fname = fam.get("Dfamily", "")
        groups = [(None, fam)] + [(s, s) for s in fam.findall("subFamily")]
        for sub, holder in groups:
            chain0 = [fam] if sub is None else [fam, sub]
            sname = sub.get("DsubFamily", "") if sub is not None else ""
            for dev in holder.findall("device"):
                chain = chain0 + [dev]
                out.append(build(dev.get("Dname", "?"), chain, fname, sname))
                for var in dev.findall("variant"):
                    out.append(build(var.get("Dvariant", "?"), chain + [var], fname, sname,
                                     variant_of=dev.get("Dname", "")))
    return out


# ====================================================================== recherche
def pack_roots() -> list[str]:
    """Dossiers des packs installés, dans l'ordre de recherche."""
    roots = []
    if os.environ.get("CMSIS_PACK_ROOT"):
        roots.append(os.environ["CMSIS_PACK_ROOT"])
    home = os.path.expanduser("~")
    roots += [os.path.join(home, ".cache", "arm", "packs"), os.path.join(home, ".arm", "Packs"),
              os.path.join(home, ".local", "share", "cmsis-pack-manager")]
    if os.environ.get("LOCALAPPDATA"):
        roots.append(os.path.join(os.environ["LOCALAPPDATA"], "Arm", "Packs"))
    return [r for r in roots if os.path.isdir(r)]


def _version_key(v: str) -> tuple:
    return tuple(int(x) if x.isdigit() else -1 for x in re.split(r"[.\-+]", v or "0"))


def installed_packs(roots: list[str] | None = None) -> list[str]:
    """.pdsc et .pack des packs installés (dossiers cachés ignorés : index de cmsis-toolbox) ;
    la dernière version de chaque pack."""
    found: dict[str, tuple[tuple, str]] = {}
    for root in roots if roots is not None else pack_roots():
        for dirpath, dirnames, files in os.walk(root):
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            if dirpath[len(root):].count(os.sep) > 4:
                dirnames[:] = []
            for f in files:
                low = f.lower()
                if not (low.endswith(".pdsc") or low.endswith(".pack")):
                    continue
                full = os.path.join(dirpath, f)
                parts = os.path.relpath(full, root).split(os.sep)
                if low.endswith(".pdsc"):
                    key = f[:-5]
                    version = parts[-2] if len(parts) >= 4 else "0"
                else:
                    stem = f[:-5]
                    m = re.match(r"(.+?)\.(\d+\.\d+.*)$", stem)
                    if m:
                        key, version = m.group(1), m.group(2)
                    else:                           # <Vendor>/<Name>/<version>.pack
                        key = ".".join(parts[-3:-1]) if len(parts) >= 3 else stem
                        version = stem
                k = key.lower()
                if k not in found or _version_key(version) > found[k][0]:
                    found[k] = (_version_key(version), full)
    return [p for _v, p in sorted(found.values(), key=lambda x: x[1])]


def find_device(name: str, packs: list[Pack]) -> Device:
    """Puce par son nom (sans casse) ou sa variante, sinon par un préfixe unique
    (STM32F446RE → STM32F446RETx). Plusieurs candidats : erreur qui les nomme."""
    want = name.strip().lower()
    devices = [d for p in packs for d in p.devices]
    exact = [d for d in devices if d.name.lower() == want]
    if exact:
        return exact[0]
    pref = [d for d in devices if d.name.lower().startswith(want)]
    if len(pref) == 1:
        return pref[0]
    if pref:
        names = ", ".join(sorted({d.name for d in pref})[:12])
        raise PackError(f"puce « {name} » ambiguë : {names}{' …' if len(pref) > 12 else ''}")
    near = sorted({d.name for d in devices if want[:7] and want[:7] in d.name.lower()})[:8]
    where = f" dans {packs[0].vendor}.{packs[0].name}" if len(packs) == 1 else " dans les packs installés"
    raise PackError(f"puce « {name} » introuvable{where}" + (f" ; proches : {', '.join(near)}" if near else ""))


def resolve(pack: str | None, device: str) -> Device:
    """La puce `device` du pack `pack` (fichier, dossier) ou des packs installés."""
    if pack:
        return find_device(device, [Pack.open(pack)])
    paths = installed_packs()
    if not paths:
        raise PackError("aucun pack installé (CMSIS_PACK_ROOT, ~/.cache/arm/packs…) : --pack FICHIER")
    packs = []
    for p in paths:
        try:
            packs.append(Pack.open(p))
        except PackError:
            continue
    return find_device(device, packs)


def list_devices(pack: str | None = None, pattern: str = "") -> list[Device]:
    """Puces d'un pack, ou de tous les packs installés, dont le nom contient `pattern`."""
    packs = [Pack.open(pack)] if pack else []
    if not pack:
        for p in installed_packs():
            try:
                packs.append(Pack.open(p))
            except PackError:
                continue
    low = pattern.lower()
    return [d for pk in packs for d in pk.devices if low in d.name.lower()]
