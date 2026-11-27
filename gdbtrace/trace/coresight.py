"""Tables ROM CoreSight : les composants de débogage et de trace qu'a vraiment la puce.

Chaque composant CoreSight occupe une page de 4 Ko dont les derniers mots l'identifient :
CIDR0-3 (0xFF0-0xFFC : préambule 0x0D, classe, 0x05, 0xB1), PIDR0-7 (0xFD0-0xFEC : numéro de
pièce sur 12 bits, fabricant JEP106, révision) et, pour un composant de classe 9, DEVARCH
(0xFBC : architecte, architecture et sa révision) et DEVTYPE (0xFCC : type majeur et
sous-type). Une table ROM (classe 1, ou classe 9 d'architecture 0x0AF7) liste d'autres
composants par des entrées de 32 bits : bit 0 présent, bits [31:12] décalage signé depuis la
table. Le parcours commence aux tables du cœur (0xE00FF000 ; un Cortex-M7 en ajoute à
0xE00FE000 et 0xE00FD000) ; une adresse illisible est simplement sautée.

Nom des composants : par l'architecture (DEVARCH : ITM, DWT, FPB, SCS de l'ARMv8-M, ETMv4,
CTI, PMU), sinon par le numéro de pièce ARM (Cortex-M0 à M55 : SCS, DWT, FPB, ITM, TPIU,
ETM-M3/M4/M7, MTB-M0+), sinon par DEVTYPE (port de trace, tampon, funnel, FIFO…). Valeurs : la
spécification CoreSight et les tables d'OpenOCD (`arm_adi_v5.c`).

QEMU n'a pas de table ROM (lue à 0) : liste vide, et tout ce qui en dépend garde son
comportement sans elle.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import Callable

#: code JEP106 d'ARM (continuation 4, identifiant 0x3B)
ARM = 0x23B
#: tables du cœur : celle de l'architecture, puis celles qu'un Cortex-M7 met devant
CORE_ROOTS = (0xE00FD000, 0xE00FE000, 0xE00FF000)
#: tables explorées pour trouver une MTB : celle du cœur, puis celles de fabricants connus
ROM_ROOTS = (0xE00FF000, 0xF0002000, 0x41003000)
#: entrées lues au plus par table (classe 1 : 960 ; classe 9 en 32 bits : 512)
MAX_ENTRIES = 512
MAX_DEPTH = 4

#: architectures (DEVARCH.ARCHID [11:0]) → type de composant
ARCH_KINDS = {0xA01: "ITM", 0xA02: "DWT", 0xA03: "FPB", 0xA04: "SCS", 0xA10: "PCS",
              0xA13: "ETM", 0xA14: "CTI", 0xA16: "PMU", 0xA17: "MEM-AP", 0xA31: "routeur",
              0xA37: "alimentation", 0xA63: "STM", 0xA75: "ELA", 0xAF7: "ROM"}
#: pièces ARM : numéro → (type, cœur ou famille)
ARM_PARTS = {
    0x000: ("SCS", "Cortex-M3"), 0x001: ("ITM", "Cortex-M3/M4/M7"), 0x002: ("DWT", "Cortex-M3/M4/M7"),
    0x003: ("FPB", "Cortex-M3/M4"), 0x008: ("SCS", "Cortex-M0/M0+"), 0x00A: ("DWT", "Cortex-M0/M0+"),
    0x00B: ("BPU", "Cortex-M0/M0+"), 0x00C: ("SCS", "Cortex-M4/M7"), 0x00E: ("FPB", "Cortex-M7"),
    0x470: ("ROM", "Cortex-M1"), 0x471: ("ROM", "Cortex-M0"), 0x4C0: ("ROM", "Cortex-M0+"),
    0x4C3: ("ROM", "Cortex-M3"), 0x4C4: ("ROM", "Cortex-M4"), 0x4C7: ("ROM", "Cortex-M7 (PPB)"),
    0x4C8: ("ROM", "Cortex-M7"), 0x4C9: ("ROM", "Cortex-M33"),
    0x923: ("TPIU", "Cortex-M3"), 0x924: ("ETM", "ETM-M3"), 0x925: ("ETM", "ETM-M4"),
    0x932: ("MTB", "MTB-M0+"), 0x975: ("ETM", "ETM-M7"), 0x9A1: ("TPIU", "Cortex-M4"),
    0x9A9: ("TPIU", "Cortex-M7"), 0xD21: ("", "Cortex-M33"), 0xD22: ("", "Cortex-M55"),
    0x906: ("CTI", "CoreSight"), 0x907: ("ETB", "CoreSight"), 0x908: ("funnel", "CoreSight"),
    0x912: ("TPIU", "CoreSight"), 0x914: ("SWO", "CoreSight"), 0x961: ("TMC", "CoreSight"),
    0x9E8: ("ETR", "SoC-600"), 0x9E9: ("ETB", "SoC-600"), 0x9EA: ("ETF", "SoC-600"),
    0x193: ("TSGEN", "SoC-600"),
}
#: DEVTYPE (sous-type << 4 | type majeur) → type de composant
DEVTYPE_KINDS = {0x11: "TPIU", 0x21: "ETB", 0x31: "routeur", 0x12: "funnel", 0x22: "filtre",
                 0x32: "ETF", 0x13: "ETM", 0x43: "STM", 0x14: "CTI", 0x24: "authentification",
                 0x15: "débogage", 0x16: "PMU"}
#: libellés des types
KIND_LABELS = {"ROM": "table ROM", "SCS": "contrôle système (SCS)", "DWT": "DWT", "FPB": "FPB",
               "BPU": "BPU", "ITM": "ITM", "TPIU": "TPIU", "ETM": "ETM", "CTI": "CTI",
               "MTB": "MTB", "PMU": "PMU", "ETB": "tampon de trace (ETB)", "ETF": "FIFO de trace (ETF)",
               "ETR": "trace en RAM (ETR)", "TMC": "TMC", "funnel": "funnel", "SWO": "SWO",
               "TSGEN": "horodatage (TSGEN)", "STM": "STM", "PCS": "échantillonnage du PC",
               "MEM-AP": "MEM-AP", "ELA": "ELA"}
#: fabricants courants (JEP106) ; les autres sont donnés par leur code
DESIGNERS = {ARM: "ARM", 0x020: "STMicroelectronics", 0x00E: "Freescale/NXP", 0x015: "NXP",
             0x01F: "Atmel/Microchip", 0x017: "Texas Instruments", 0x244: "Nordic", 0x041: "Infineon",
             0x575: "ARM China"}


@dataclass
class Component:
    addr: int
    part: int
    cls: int                     # classe CoreSight : 1 = table ROM, 9 = composant CoreSight
    designer: int                # JEP106 : (continuation << 7) | identifiant ; ARM = 0x23B
    depth: int = 0
    revision: int = 0
    devarch: int = 0             # classe 9 : DEVARCH (0 sinon)
    devtype: int = 0             # classe 9 : DEVTYPE
    devid: int = 0               # classe 9 : DEVID (TMC : configuration ETB, ETR ou ETF)
    children: list = field(default_factory=list, repr=False)

    @property
    def archid(self) -> int | None:
        """Architecture DEVARCH.ARCHID (bits [15:0]) si le registre est présent (bit 20)."""
        return self.devarch & 0xFFFF if self.devarch & (1 << 20) else None

    @property
    def is_rom(self) -> bool:
        return self.cls == 1 or (self.cls == 9 and self.archid == 0x0AF7)

    @property
    def kind(self) -> str:
        """Type du composant (ROM, SCS, DWT, FPB, ITM, TPIU, ETM, CTI, MTB, PMU, ETF, ETR…), ou
        "" s'il n'est pas reconnu."""
        if self.is_rom:
            return "ROM"
        arch = self.archid
        if arch is not None and (arch & 0xFFF) in ARCH_KINDS:
            return ARCH_KINDS[arch & 0xFFF]
        if self.designer == ARM and self.part in ARM_PARTS and ARM_PARTS[self.part][0]:
            kind = ARM_PARTS[self.part][0]
            if kind == "TMC":                    # DEVID.CONFIGTYPE : ETB, ETR ou ETF
                return {0: "ETB", 1: "ETR", 2: "ETF"}.get(self.devid >> 6 & 3, "TMC")
            return kind
        if self.cls == 9 and self.devtype in DEVTYPE_KINDS:
            return DEVTYPE_KINDS[self.devtype]
        return ""

    @property
    def etm_version(self) -> int | None:
        """ETMv3 (ETM-M3, ETM-M4) ou ETMv4 (DEVARCH 0x4A13, ETM-M7) ; None si ce n'est pas un ETM."""
        if self.kind != "ETM":
            return None
        arch = self.archid
        if arch is not None and arch & 0xFFF == 0xA13:
            return arch >> 12
        if self.designer == ARM and self.part in (0x924, 0x925):
            return 3
        return 4 if self.designer == ARM and self.part == 0x975 else None

    @property
    def designer_name(self) -> str:
        return DESIGNERS.get(self.designer, f"{self.designer:#05x}")

    @property
    def name(self) -> str:
        kind = self.kind
        family = ARM_PARTS.get(self.part, ("", ""))[1] if self.designer == ARM else ""
        if kind == "ROM":
            if self.designer != ARM:
                return f"table ROM (fabricant {self.designer_name})"
            return f"table ROM ({family})" if family else "table ROM"
        if self.designer == ARM:
            if kind == "ETM" and family.startswith("ETM"):
                return family
            if kind == "MTB":
                return family
            label = KIND_LABELS.get(kind, kind) if kind else f"pièce ARM {self.part:#05x}"
            if kind == "ETM":
                label = f"ETMv{self.etm_version}" if self.etm_version else label
            return f"{label} ({family})" if family and family != "CoreSight" else label
        label = KIND_LABELS.get(kind, kind) if kind else f"pièce {self.part:#05x}"
        return f"{label} (fabricant {self.designer_name})"

    def as_dict(self) -> dict:
        return {"adresse": self.addr, "nom": self.name, "type": self.kind, "pièce": self.part,
                "fabricant": self.designer_name, "révision": self.revision, "classe": self.cls,
                "profondeur": self.depth}


# ====================================================================== lecture
Read = Callable[[int, int], bytes]


def _reader_from_u32(read_u32: Callable[[int], int]) -> Read:
    def read(addr: int, n: int) -> bytes:
        return b"".join(struct.pack("<I", read_u32(addr + i) & 0xFFFFFFFF) for i in range(0, n, 4))
    return read


def _words(blob: bytes) -> list[int]:
    return list(struct.unpack(f"<{len(blob) // 4}I", blob[: len(blob) // 4 * 4]))


def identify_block(read: Read, addr: int) -> Component | None:
    """Composant CoreSight à `addr` (sa page de 4 Ko), ou None : rien de lisible, ou pas le
    préambule CoreSight. Une lecture de 0x30 octets, plus 0x14 pour la classe 9."""
    try:
        w = _words(read(addr + 0xFD0, 0x30))   # PIDR4-7, PIDR0-3, CIDR0-3
    except Exception:  # noqa: BLE001 - adresse non mappée, ou bloc en partie illisible
        try:                                   # les seuls mots utiles, un par un
            w = [_words(read(addr + off, 4))[0] if off in (0xFD0, 0xFE0, 0xFE4, 0xFE8) or off >= 0xFF0
                 else 0 for off in range(0xFD0, 0x1000, 4)]
        except Exception:  # noqa: BLE001
            return None
    if len(w) < 12:
        return None
    pidr4 = w[0] & 0xFF
    pidr = [x & 0xFF for x in w[4:8]]
    cidr = [x & 0xFF for x in w[8:12]]
    if cidr[0] != 0x0D or cidr[2] != 0x05 or cidr[3] != 0xB1:
        return None
    part = pidr[0] | (pidr[1] & 0x0F) << 8
    designer = (pidr[1] >> 4) | (pidr[2] & 0x07) << 4 | (pidr4 & 0x0F) << 7
    comp = Component(addr, part, cidr[1] >> 4, designer, revision=pidr[2] >> 4)
    if comp.cls == 9:
        try:
            extra = _words(read(addr + 0xFBC, 0x14))   # DEVARCH, DEVID2, DEVID1, DEVID, DEVTYPE
        except Exception:  # noqa: BLE001
            extra = []
        if len(extra) == 5:
            comp.devarch, comp.devid, comp.devtype = extra[0], extra[3], extra[4] & 0xFF
    return comp


def identify(read_u32: Callable[[int], int], addr: int) -> Component | None:
    """Comme `identify_block`, par mots de 32 bits (compatibilité de trace/mtb.py)."""
    return identify_block(_reader_from_u32(read_u32), addr)


def _entries(read: Read, table: int):
    """Entrées d'une table ROM (32 bits), lues par blocs de 16, jusqu'à l'entrée nulle."""
    for base in range(0, MAX_ENTRIES * 4, 0x40):
        try:
            words = _words(read(table + base, 0x40))
        except Exception:  # noqa: BLE001 - bloc en partie illisible : mot à mot
            words = []
            for off in range(0, 0x40, 4):
                try:
                    words += _words(read(table + base + off, 4))
                except Exception:  # noqa: BLE001
                    break
            if len(words) < 16:
                words.append(0)                # fin de ce qu'on peut lire

        for entry in words:
            if entry == 0:
                return
            yield entry


def walk(read: Read, roots=CORE_ROOTS) -> list[Component]:
    """Parcours récursif des tables ROM à partir de `roots` : tous les composants trouvés, dans
    l'ordre (une table, puis ses entrées). Une table déjà vue (boucle, ou racine listée par une
    autre) n'est parcourue qu'une fois ; les adresses illisibles sont sautées."""
    out: list[Component] = []
    seen: set[int] = set()

    def visit(table: Component, depth: int) -> None:
        if table.addr in seen or depth > MAX_DEPTH:
            return
        seen.add(table.addr)
        for entry in _entries(read, table.addr):
            if not entry & 1:                  # absent (emplacement réservé)
                continue
            offset = entry & 0xFFFFF000
            if offset & 0x80000000:
                offset -= 1 << 32
            addr = (table.addr + offset) & 0xFFFFFFFF
            if addr in seen:
                continue
            comp = identify_block(read, addr)
            if comp is None:
                continue
            comp.depth = depth
            table.children.append(comp)
            out.append(comp)
            if comp.is_rom:
                visit(comp, depth + 1)
            else:
                seen.add(addr)

    for root in roots:
        if root in seen:
            continue
        top = identify_block(read, root)
        if top is not None and top.is_rom:
            out.append(top)
            visit(top, 1)
    return out


def rom_components(read_u32: Callable[[int], int], roots=ROM_ROOTS) -> list[Component]:
    """Parcours par mots de 32 bits (compatibilité de trace/mtb.py)."""
    return walk(_reader_from_u32(read_u32), roots)


def find(components: list[Component], kind: str) -> Component | None:
    return next((c for c in components if c.kind == kind), None)


def describe(components: list[Component]) -> list[str]:
    """Lignes « adresse  nom » indentées par profondeur (gdbtrace.coresight, --mtb-probe)."""
    return [f"{'  ' * c.depth}{c.addr:#010x}  {c.name}" for c in components]
