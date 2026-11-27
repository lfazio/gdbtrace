"""Arrêt synchrone des cœurs par leurs CTI (Cross Trigger Interface, CoreSight) :
`gdbtrace.cti_sync()`.

Sans CTI, gdb arrête les autres cœurs quand l'un s'arrête, mais par logiciel : ils exécutent
encore des milliers d'instructions. Avec les CTI, l'arrêt d'un cœur (entrée « halted » de son
CTI) passe sur le canal 0 de la matrice et devient une demande d'arrêt (EDBGRQ) pour les
autres : ils s'arrêtent à quelques cycles près.

Programmation de chaque CTI trouvé dans les tables ROM (trace/coresight.py) :
CTILAR déverrouillé, CTIINEN[0] = canal 0 (déclencheur d'entrée 0 : cœur arrêté),
CTIOUTEN[0] = canal 0 (déclencheur de sortie 0 : demande d'arrêt), CTIGATE = canal 0 vers la
matrice, CTICONTROL.GLBEN. La demande d'arrêt reste levée tant qu'elle n'est pas acquittée :
avant chaque reprise, CTIINTACK bit 0 (resume hook), sinon le cœur s'arrêterait aussitôt.
Déclencheurs 0 / 0 : ceux des Cortex-M7, M33 et M55 (manuels de référence ARM).

Prêt pour une puce à deux cœurs (STM32H7 : Cortex-M7 et Cortex-M4), pas encore passé sur
carte. QEMU n'a ni table ROM ni CTI : `cti_sync` le dit.
"""
from __future__ import annotations

from dataclasses import dataclass, field

CTICONTROL = 0x000
CTIINTACK = 0x010
CTIINEN0 = 0x020
CTIOUTEN0 = 0x0A0
CTITRIGINSTATUS = 0x130
CTIGATE = 0x140
CTILAR = 0xFB0
UNLOCK = 0xC5ACCE55
CHANNEL = 1 << 0                     # canal 0 de la matrice


@dataclass
class CtiSync:
    """CTI programmés par session de cœur : {nom du cœur : [adresses]}."""
    ctis: dict[str, list[int]] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def summary(self) -> str:
        done = {k: v for k, v in self.ctis.items() if v}
        if not done:
            return "arrêt synchrone : aucun CTI trouvé" + (f" ({'; '.join(self.notes)})" if self.notes else "")
        parts = [f"{name} : " + ", ".join(f"{a:#010x}" for a in addrs) for name, addrs in done.items()]
        text = "arrêt synchrone par CTI (canal 0) — " + " ; ".join(parts)
        return text + (f" ({'; '.join(self.notes)})" if self.notes else "")


def program(write_u32, base: int, enable: bool = True) -> None:
    """Programme un CTI : arrêt du cœur → canal 0 → demande d'arrêt ; `enable` False : coupé."""
    write_u32(base + CTILAR, UNLOCK)
    write_u32(base + CTICONTROL, 0)
    write_u32(base + CTIINEN0, CHANNEL if enable else 0)
    write_u32(base + CTIOUTEN0, CHANNEL if enable else 0)
    write_u32(base + CTIGATE, CHANNEL if enable else 0)
    write_u32(base + CTIINTACK, 1)
    if enable:
        write_u32(base + CTICONTROL, 1)


def acknowledge(write_u32, base: int) -> None:
    """Acquitte la demande d'arrêt (avant une reprise)."""
    write_u32(base + CTIINTACK, 1)


def setup(cores, enable: bool = True) -> CtiSync:
    """CTI de chaque session de cœur (`[(nom, session)]`, cibles arrêtées) : trouvés par les
    tables ROM, programmés ; un resume hook acquitte la demande d'arrêt avant chaque reprise."""
    from ..trace.coresight import walk
    out = CtiSync()
    seen = set()
    for name, session in cores:
        if id(session) in seen:              # cœurs d'une même session : mêmes tables ROM
            continue
        seen.add(id(session))
        try:
            comps = walk(session.read_memory)
        except Exception as exc:  # noqa: BLE001 - table illisible : dit
            out.notes.append(f"{name} : tables ROM illisibles ({exc})")
            continue
        addrs = [c.addr for c in comps if c.kind == "CTI"]
        if not comps:
            out.notes.append(f"{name} : pas de table ROM (QEMU n'a ni table ROM ni CTI)")
        elif not addrs:
            out.notes.append(f"{name} : pas de CTI dans les tables ROM")
        for a in addrs:
            program(session.write_u32, a, enable)
        out.ctis[name] = addrs
        hooks = getattr(session, "_cti_hooks", None)
        if hooks is None:
            hooks = session._cti_hooks = []
            session.add_resume_hook(lambda s=session: [acknowledge(s.write_u32, b) for b in s._cti_hooks])
        hooks[:] = addrs if enable else []
    return out
