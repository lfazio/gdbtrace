"""MPU des Cortex-M : régions relues et décodées (vue Système, onglet MPU ; gdbtrace.mpu()).

Registres (SCB, cible arrêtée) : MPU_TYPE (0xE000ED90, DREGION = nombre de régions), MPU_CTRL
(ENABLE, HFNMIENA, PRIVDEFENA), MPU_RNR (région choisie), puis :

- ARMv7-M et ARMv6-M (PMSAv7) : MPU_RBAR (base) et MPU_RASR : taille 2^(SIZE+1), 8
  sous-régions désactivables (SRD, régions de 256 octets et plus), AP (accès privilégié et non
  privilégié), XN, et TEX/S/C/B pour le type mémoire ;
- ARMv8-M (PMSAv8) : MPU_RBAR (BASE, SH, AP, XN) et MPU_RLAR (LIMIT, AttrIndx, EN, PXN en
  v8.1-M) ; le type mémoire est l'attribut AttrIndx de MPU_MAIR0/1. Les alias MPU_RBAR_A1..A3 /
  MPU_RLAR_A1..A3 donnent les régions (RNR & ~3) + 1..3 sans écrire RNR.

Chaque région se lit en écrivant RNR ; RNR est remis à sa valeur à la fin. Un serveur qui
ignore cette écriture (le gdbstub de QEMU 8.2, pour tout registre de périphérique) : seule la
région déjà choisie est lisible (ARMv7-M), ou son groupe de quatre par les alias (ARMv8-M) ;
la note le dit, rien n'est inventé. MPU sécurisée et non sécurisée de TrustZone : la MPU vue
est celle de l'état courant du cœur.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

MPU_TYPE = 0xE000ED90
MPU_CTRL = 0xE000ED94
MPU_RNR = 0xE000ED98
MPU_RBAR = 0xE000ED9C
MPU_RASR = 0xE000EDA0            # ARMv8-M : MPU_RLAR à la même adresse
MPU_MAIR0 = 0xE000EDC0
MPU_MAIR1 = 0xE000EDC4

QEMU_NOTE = ("sélection de région ignorée par le serveur (le gdbstub de QEMU 8.2 n'écrit pas "
             "dans les registres du cœur) : seules les régions lisibles sans écrire MPU_RNR sont "
             "décodées")

#: ARMv7-M : AP → (privilégié, non privilégié)
AP_V7 = {0: ("aucun", "aucun"), 1: ("RW", "aucun"), 2: ("RW", "RO"), 3: ("RW", "RW"),
         4: ("réservé", "réservé"), 5: ("RO", "aucun"), 6: ("RO", "RO"), 7: ("RO", "RO")}
#: ARMv8-M : AP[2:1] → (privilégié, non privilégié)
AP_V8 = {0: ("RW", "aucun"), 1: ("RW", "RW"), 2: ("RO", "aucun"), 3: ("RO", "RO")}
SHARE_V8 = {0: "", 1: "réservé", 2: "partagé (extérieur)", 3: "partagé (intérieur)"}


def _cache_policy(bits: int) -> str:
    """TEX[1:0] ou C:B d'une politique de cache normale (TEX = 1BB)."""
    return {0: "non cacheable", 1: "WB-WA", 2: "WT", 3: "WB"}[bits & 3]


def memory_type_v7(tex: int, s: int, c: int, b: int) -> str:
    """Type mémoire d'une région PMSAv7 (ARM ARM v7-M, B3.5.8)."""
    share = ", partagé" if s else ""
    if tex == 0:
        if not c and not b:
            return "fortement ordonné"
        if not c and b:
            return "Device partagé"
        return f"Normal {'WT' if not b else 'WB'}, sans allocation en écriture{share}"
    if tex == 1:
        if not c and not b:
            return f"Normal non cacheable{share}"
        if c and b:
            return f"Normal WB-WA{share}"
        return "réservé"
    if tex == 2 and not c and not b:
        return "Device non partagé"
    if tex & 4:
        return f"Normal, extérieur {_cache_policy(tex)}, intérieur {_cache_policy(c << 1 | b)}{share}"
    return "réservé"


def mair_attr(attr: int) -> str:
    """Attribut mémoire d'un octet de MAIR (ARMv8-M)."""
    hi, lo = attr >> 4, attr & 0xF
    if hi == 0:
        return {0x0: "Device-nGnRnE", 0x4: "Device-nGnRE", 0x8: "Device-nGRE",
                0xC: "Device-GRE"}.get(lo, f"Device réservé ({attr:#04x})")

    def policy(x: int) -> str:
        if x == 0b0100:
            return "non cacheable"
        kind = {0: "WT transitoire", 1: "WB transitoire", 2: "WT", 3: "WB"}[x >> 2]
        alloc = "".join(n for bit, n in ((2, "R"), (1, "W")) if x & bit)
        return kind + (f" {alloc}A" if alloc else "")
    if lo == 0:
        return f"réservé ({attr:#04x})"
    return f"Normal, extérieur {policy(hi)}, intérieur {policy(lo)}"


@dataclass
class MpuRegion:
    number: int
    enabled: bool
    base: int
    limit: int                   # dernière adresse comprise
    priv: str                    # accès privilégié : RW, RO, aucun
    unpriv: str                  # accès non privilégié
    xn: bool
    memory: str                  # type mémoire décodé
    subregions_off: list[int] = field(default_factory=list)   # PMSAv7 : sous-régions SRD
    pxn: bool = False            # ARMv8.1-M : pas d'exécution privilégiée
    raw: tuple = ()

    @property
    def size(self) -> int:
        return self.limit - self.base + 1

    def contains(self, addr: int) -> bool:
        """L'adresse est-elle couverte par cette région (sous-régions désactivées exclues) ?"""
        if not self.enabled or not self.base <= addr <= self.limit:
            return False
        if self.subregions_off:
            sub = self.size // 8
            return (addr - self.base) // sub not in self.subregions_off
        return True

    def access(self) -> str:
        return f"privilégié {self.priv}, non privilégié {self.unpriv}"

    def row(self) -> tuple:
        """Ligne de la vue : active, début, fin, taille, sous-régions, accès, XN, type."""
        subs = ("désactivées : " + ",".join(map(str, self.subregions_off))) if self.subregions_off else ""
        xn = "XN" + (" PXN" if self.pxn else "") if self.xn else ("PXN" if self.pxn else "")
        return ("oui" if self.enabled else "non", f"{self.base:#010x}", f"{self.limit:#010x}",
                _size_text(self.size), subs, self.access(), xn, self.memory)

    def as_dict(self) -> dict:
        return {"région": self.number, "active": self.enabled, "début": self.base, "fin": self.limit,
                "accès privilégié": self.priv, "accès non privilégié": self.unpriv, "XN": self.xn,
                "type mémoire": self.memory, "sous-régions désactivées": self.subregions_off}


def _size_text(n: int) -> str:
    for unit, k in (("Go", 1 << 30), ("Mo", 1 << 20), ("Ko", 1 << 10)):
        if n >= k and n % k == 0:
            return f"{n // k} {unit}"
    return f"{n} o"


@dataclass
class MpuState:
    arch: str
    dregion: int
    ctrl: int
    regions: list[MpuRegion] = field(default_factory=list)
    note: str = ""
    separate: bool = False       # MPU_TYPE.SEPARATE (toujours 0 sur un Cortex-M)

    @property
    def enabled(self) -> bool:
        return bool(self.ctrl & 1)

    def summary(self) -> str:
        if self.dregion == 0:
            return f"{self.arch} : pas de MPU (MPU_TYPE.DREGION = 0)"
        flags = [n for bit, n in ((1, "HFNMIENA"), (2, "PRIVDEFENA")) if self.ctrl >> bit & 1]
        state = "activée" if self.enabled else "désactivée (carte mémoire par défaut)"
        on = sum(1 for r in self.regions if r.enabled)
        text = (f"MPU {self.arch}, {self.dregion} régions, {state}"
                + (f" ({', '.join(flags)})" if flags else "")
                + f" ; {len(self.regions)} lue(s), {on} active(s)")
        return text + (f" — {self.note}" if self.note else "")

    def region_at(self, addr: int) -> MpuRegion | None:
        """Région qui décide pour `addr` : la plus haute qui la couvre (PMSAv7 : la dernière
        l'emporte ; PMSAv8 : un recouvrement est une faute, la plus haute est donnée)."""
        hits = [r for r in self.regions if r.contains(addr)]
        return max(hits, key=lambda r: r.number) if hits else None


def decode_v7(number: int, rbar: int, rasr: int) -> MpuRegion:
    size_field = (rasr >> 1) & 0x1F
    size = 1 << (size_field + 1)
    base = rbar & ~(size - 1) & 0xFFFFFFE0 if size_field >= 4 else rbar & 0xFFFFFFE0
    srd = (rasr >> 8) & 0xFF
    subs = [i for i in range(8) if srd >> i & 1] if size >= 256 else []
    priv, unpriv = AP_V7[(rasr >> 24) & 7]
    tex, s, c, b = (rasr >> 19) & 7, (rasr >> 18) & 1, (rasr >> 17) & 1, (rasr >> 16) & 1
    return MpuRegion(number, bool(rasr & 1), base, (base + size - 1) & 0xFFFFFFFF, priv, unpriv,
                     bool(rasr >> 28 & 1), memory_type_v7(tex, s, c, b), subs, raw=(rbar, rasr))


def decode_v8(number: int, rbar: int, rlar: int, mair: int) -> MpuRegion:
    base = rbar & 0xFFFFFFE0
    limit = rlar & 0xFFFFFFE0 | 0x1F
    priv, unpriv = AP_V8[(rbar >> 1) & 3]
    index = (rlar >> 1) & 7
    mem = mair_attr((mair >> (8 * index)) & 0xFF)
    share = SHARE_V8[(rbar >> 3) & 3]
    if share and not mem.startswith("Device"):
        mem += f", {share}"
    return MpuRegion(number, bool(rlar & 1), base, limit, priv, unpriv, bool(rbar & 1),
                     f"attribut {index} : {mem}", pxn=bool(rlar >> 4 & 1), raw=(rbar, rlar))


def read_mpu(read_u32: Callable[[int], int], write_u32: Callable[[int, int], None] | None,
             arch: str) -> MpuState:
    """MPU de la cible arrêtée. `arch` : « ARMv6-M », « ARMv7-M », « ARMv8-M Mainline »,
    « ARMv8-M Baseline » (core/fault.core_arch). `write_u32` None : pas de sélection de région
    (seules les régions lisibles sans écrire RNR)."""
    mtype = read_u32(MPU_TYPE)
    dregion = (mtype >> 8) & 0xFF
    state = MpuState(arch, dregion, read_u32(MPU_CTRL) if dregion else 0, separate=bool(mtype & 1))
    if dregion == 0:
        return state
    v8 = arch.startswith("ARMv8-M")
    mair = 0
    if v8:
        mair = read_u32(MPU_MAIR0) | read_u32(MPU_MAIR1) << 32
    rnr0 = read_u32(MPU_RNR) & 0xFF

    def one(n: int, rbar_addr: int = MPU_RBAR) -> MpuRegion:
        rbar, second = read_u32(rbar_addr), read_u32(rbar_addr + 4)
        return decode_v8(n, rbar, second, mair) if v8 else decode_v7(n, rbar, second)

    selectable = write_u32 is not None
    if selectable:                               # le serveur applique-t-il l'écriture de RNR ?
        probe = 1 if rnr0 != 1 and dregion > 1 else 0
        try:
            write_u32(MPU_RNR, probe)
            selectable = (read_u32(MPU_RNR) & 0xFF) == probe
        except Exception:  # noqa: BLE001 - écriture refusée
            selectable = False
    try:
        if selectable:
            state.regions = []
            for n in range(dregion):
                write_u32(MPU_RNR, n)
                state.regions.append(one(n))
        else:
            seen = {rnr0: one(rnr0)} if rnr0 < dregion else {}
            if v8:                               # alias : (RNR & ~3) + 1..3
                group = rnr0 & ~3
                for k in (1, 2, 3):
                    if group + k < dregion and group + k not in seen:
                        seen[group + k] = one(group + k, MPU_RBAR + 8 * k)
            state.regions = [seen[n] for n in sorted(seen)]
            state.note = QEMU_NOTE if write_u32 is not None else "sélection de région non demandée"
    finally:
        if write_u32 is not None and selectable:
            try:
                write_u32(MPU_RNR, rnr0)         # le firmware retrouve sa région choisie
            except Exception:  # noqa: BLE001
                pass
    return state


def session_mpu(session) -> MpuState:
    """MPU d'une session arrêtée, architecture d'après CPUID."""
    from .fault import core_arch
    arch = core_arch(session.read_u32(0xE000ED00))
    if arch == "ARMv6-M":
        try:
            dregion = (session.read_u32(MPU_TYPE) >> 8) & 0xFF
        except Exception:  # noqa: BLE001 - pas de MPU : zone réservée
            dregion = 0
        if dregion == 0:
            return MpuState(arch, 0, 0)
    return read_mpu(session.read_u32, session.write_u32, arch)
