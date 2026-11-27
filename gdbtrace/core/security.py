"""TrustZone des Cortex-M (ARMv8-M, extension de sécurité) : état courant du cœur, SAU, pile
déroulée à travers les deux mondes (vue Registres, barre d'état, vue Pile, vue Système ;
`gdbtrace.security()`, `gdbtrace.sau()`, `gdbtrace.bt()`).

État courant : DSCSR.CDS (0xE000EE08, bit 16) dit si le cœur est sécurisé. Un serveur qui le
lit à 0 sans le tenir à jour (QEMU 8.2) : `sp` est le pointeur de pile bancaire du monde
courant, comparé à `msp_s`, `psp_s`, `msp_ns`, `psp_ns` (registres que gdb expose pour un cœur
à extension de sécurité). La comparaison passe d'abord ; DSCSR départage.

SAU (0xE000EDD0) : SAU_CTRL (ENABLE, ALLNS), SAU_TYPE.SREGION, SAU_RNR, SAU_RBAR, SAU_RLAR
(NSC, ENABLE). Lisible depuis l'état sécurisé seulement (RAZ/WI depuis le non sécurisé) ;
chaque région se lit en écrivant RNR, remis à sa valeur à la fin ; une écriture ignorée (QEMU
8.2) ne laisse lisible que la région choisie par le firmware, dit par la note.

Pile : gdb s'arrête au changement de monde.
- Sécurisé appelé par le non sécurisé (SG) : gdb donne le premier cadre non sécurisé (son lr
  a le bit 0 à 0), puis « previous frame inner to this frame » (les deux piles ne sont pas
  ordonnées). La suite se déroule avec les registres de ce cadre et le sp non sécurisé.
- Non sécurisé appelé par le sécurisé (BLXNS) : gdb donne le pseudo-cadre FNC_RETURN
  (0xFEFFFFFF) et ne lit pas la pile sécurisée. Le cadre FNC_RETURN (adresse de retour, RETPSR
  partiel) est en haut de la pile sécurisée du moment de l'appel ; la suite se déroule depuis
  l'adresse de retour.
Pour dérouler l'autre côté, les registres du cadre (r0-r12, sp, lr, pc) sont écrits le temps de
demander la pile à gdb, puis remis (`_context`) : gdb n'a pas d'autre moyen de partir d'un
cadre arbitraire. Lecture de la pile sécurisée depuis l'état non sécurisé : refusée sous QEMU
(accès du débogueur dans l'état courant) ; `secure_read` (QEMU : `QemuQmp.read_physical`, le
moniteur lit l'espace physique) la remplace.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Callable

DSCSR = 0xE000EE08
SAU_CTRL = 0xE000EDD0
SAU_TYPE = 0xE000EDD4
SAU_RNR = 0xE000EDD8
SAU_RBAR = 0xE000EDDC
SAU_RLAR = 0xE000EDE0
#: alias non sécurisé du SCS (0xE002E000), lisible depuis l'état sécurisé : MPU_NS, CFSR_NS…
SCS_NS_OFFSET = 0x20000

#: registres bancaires (suffixes _s et _ns), dans l'ordre de la vue Registres
BANKED = ("msp", "psp", "msplim", "psplim", "control", "primask", "basepri", "faultmask")
#: registres écrits le temps de dérouler depuis un autre cadre
CONTEXT = tuple(f"r{i}" for i in range(13)) + ("sp", "lr", "pc")
#: changements de monde suivis au plus dans une pile
MAX_SWITCHES = 8

SAU_NOTE_NS = ("SAU illisible depuis l'état non sécurisé (RAZ) : arrêter le cœur dans le "
               "monde sécurisé")
SAU_NOTE_RNR = ("sélection de région ignorée par le serveur (le gdbstub de QEMU 8.2 n'écrit pas "
                "dans les registres du cœur) : seule la région choisie par le firmware (SAU_RNR) "
                "est décodée")


def is_fnc_return(value: int) -> bool:
    """FNC_RETURN : lr d'un appel non sécurisé fait par BLXNS."""
    return value & 0xFFFFFFFE == 0xFEFFFFFE


def has_security(session) -> bool:
    """Le cœur a l'extension de sécurité (gdb en expose les registres bancaires) ?"""
    try:
        names = {n.lower() for n in session.register_names()}
    except Exception:  # noqa: BLE001 - registres inconnus : pas de TrustZone
        return False
    return "msp_s" in names and "msp_ns" in names


# ====================================================================== état courant
@dataclass
class SecurityState:
    secure: bool | None              # None : indéterminé
    how: str                         # « DSCSR.CDS », « pile : sp = msp_s »…
    handler: bool = False            # IPSR ≠ 0
    sp_name: str = ""                # pointeur de pile en service : msp_s, psp_ns…
    banked: dict[str, int] = field(default_factory=dict)   # msp_s, msp_ns… → valeur

    @property
    def label(self) -> str:
        """« S », « NS » ou « ? » (barre d'état)."""
        return "?" if self.secure is None else ("S" if self.secure else "NS")

    @property
    def world(self) -> str:
        return {None: "indéterminé", True: "sécurisé", False: "non sécurisé"}[self.secure]

    def text(self) -> str:
        mode = "Handler" if self.handler else "Thread"
        sp = f", pile {self.sp_name.upper()}" if self.sp_name else ""
        return f"{self.world}, {mode}{sp} ({self.how})"

    def as_dict(self) -> dict:
        return {"état": self.label, "monde": self.world, "mode": "Handler" if self.handler else "Thread",
                "pile": self.sp_name, "d'après": self.how,
                "registres bancaires": {k: v for k, v in self.banked.items()}}


def stack_name(world: str, handler: bool, control: int) -> str:
    """Pointeur de pile en service dans un monde : MSP en Handler, sinon CONTROL.SPSEL."""
    return ("psp" if not handler and control & 2 else "msp") + ("_s" if world == "S" else "_ns")


def security_state(session, regs: dict[str, int] | None = None) -> SecurityState | None:
    """État de sécurité du cœur arrêté, ou None sans extension de sécurité."""
    if not has_security(session):
        return None
    if regs is None:
        regs = {n.lower(): v for n, v in session.registers()}
    sp = regs.get("sp")
    handler = bool(regs.get("xpsr", 0) & 0x1FF)
    banked = {f"{b}_{w}": regs[f"{b}_{w}"] for b in BANKED for w in ("s", "ns") if f"{b}_{w}" in regs}
    try:
        dscsr = session.read_u32(DSCSR)
    except Exception:  # noqa: BLE001 - DSCSR illisible : la pile seule
        dscsr = None
    s_hits = [n for n in ("msp_s", "psp_s") if regs.get(n) == sp]
    ns_hits = [n for n in ("msp_ns", "psp_ns") if regs.get(n) == sp]
    state = SecurityState(None, "indéterminé", handler, banked=banked)
    if s_hits and not ns_hits:
        state.secure, state.how = True, f"pile : sp = {s_hits[0]}"
    elif ns_hits and not s_hits:
        state.secure, state.how = False, f"pile : sp = {ns_hits[0]}"
    elif dscsr is not None:
        state.secure, state.how = bool(dscsr >> 16 & 1), "DSCSR.CDS"
    if state.secure is not None:
        world = "S" if state.secure else "NS"
        state.sp_name = stack_name(world, handler, regs.get(f"control_{world.lower()}", 0))
    return state


# ====================================================================== SAU
@dataclass
class SauRegion:
    number: int
    enabled: bool
    base: int
    limit: int                       # dernière adresse comprise
    nsc: bool

    @property
    def attribute(self) -> str:
        return "NSC" if self.nsc else "NS"

    def row(self) -> tuple:
        """Ligne de la vue : active, début, fin, taille, attribut."""
        from .mpu import _size_text
        return ("oui" if self.enabled else "non", f"{self.base:#010x}", f"{self.limit:#010x}",
                _size_text(self.limit - self.base + 1), self.attribute)

    def as_dict(self) -> dict:
        return {"région": self.number, "active": self.enabled, "début": self.base,
                "fin": self.limit, "attribut": self.attribute}


@dataclass
class SauState:
    ctrl: int
    sregion: int
    regions: list[SauRegion] = field(default_factory=list)
    note: str = ""
    readable: bool = True

    @property
    def enabled(self) -> bool:
        return bool(self.ctrl & 1)

    @property
    def allns(self) -> bool:
        return bool(self.ctrl & 2)

    def summary(self) -> str:
        if not self.readable:
            return f"SAU : {self.note}"
        if self.enabled:
            state = "activé (hors régions : sécurisé)"
        else:
            state = "désactivé : tout " + ("non sécurisé (ALLNS)" if self.allns else "sécurisé")
        text = f"SAU {self.sregion} régions, {state} ; {len(self.regions)} lue(s)"
        return text + (f" — {self.note}" if self.note else "")

    def attribution(self, addr: int) -> str | None:
        """« S », « NS » ou « NSC » d'après le SAU seul (l'IDAU de la puce peut rendre une
        adresse plus sécurisée) ; None si les régions lues ne suffisent pas."""
        if not self.readable:
            return None
        if not self.enabled:
            return "NS" if self.allns else "S"
        hits = [r for r in self.regions if r.enabled and r.base <= addr <= r.limit]
        if len(hits) > 1:
            return "S"                   # recouvrement : sécurisé (ARMv8-M)
        if hits:
            return hits[0].attribute
        return "S" if len(self.regions) >= self.sregion else None

    def as_dict(self) -> dict:
        return {"activé": self.enabled, "ALLNS": self.allns, "régions": self.sregion,
                "lues": [r.as_dict() for r in self.regions], "note": self.note}


def decode_sau(number: int, rbar: int, rlar: int) -> SauRegion:
    return SauRegion(number, bool(rlar & 1), rbar & 0xFFFFFFE0, rlar & 0xFFFFFFE0 | 0x1F,
                     bool(rlar >> 1 & 1))


def read_sau(read_u32: Callable[[int], int], write_u32: Callable[[int, int], None] | None,
             secure: bool | None = True) -> SauState:
    """SAU de la cible arrêtée. `secure` False : état non sécurisé, SAU illisible (RAZ)."""
    if secure is False:
        return SauState(0, 0, note=SAU_NOTE_NS, readable=False)
    stype, ctrl = read_u32(SAU_TYPE), read_u32(SAU_CTRL)
    sregion = stype & 0xFF
    state = SauState(ctrl, sregion)
    if sregion == 0:
        if not ctrl:
            state.note = "pas de région (SAU_TYPE.SREGION = 0) : attribution par l'IDAU seule"
        return state
    rnr0 = read_u32(SAU_RNR) & 0xFF
    selectable = write_u32 is not None
    if selectable:
        probe = 1 if rnr0 != 1 and sregion > 1 else 0
        try:
            write_u32(SAU_RNR, probe)
            selectable = (read_u32(SAU_RNR) & 0xFF) == probe
        except Exception:  # noqa: BLE001 - écriture refusée
            selectable = False
    try:
        if selectable:
            for n in range(sregion):
                write_u32(SAU_RNR, n)
                state.regions.append(decode_sau(n, read_u32(SAU_RBAR), read_u32(SAU_RLAR)))
        else:
            if rnr0 < sregion:
                state.regions = [decode_sau(rnr0, read_u32(SAU_RBAR), read_u32(SAU_RLAR))]
            state.note = SAU_NOTE_RNR if write_u32 is not None else "sélection de région non demandée"
    finally:
        if selectable:
            try:
                write_u32(SAU_RNR, rnr0)
            except Exception:  # noqa: BLE001
                pass
    return state


def session_sau(session, state: SecurityState | None = None) -> SauState:
    state = state or security_state(session)
    return read_sau(session.read_u32, session.write_u32, None if state is None else state.secure)


# ====================================================================== pile des deux mondes
@dataclass
class WorldFrame:
    """Cadre de pile et son monde : « S », « NS », ou None (inconnu)."""
    frame: object                    # session.Frame
    world: str | None
    note: str = ""                   # changement de monde, pile illisible…

    def as_dict(self) -> dict:
        f = self.frame
        return {"pc": f.pc, "fonction": f.func, "fichier": f.file, "ligne": f.line,
                "monde": self.world, "note": self.note}


def _thread(session) -> str | None:
    try:
        return session.gdb.command("-thread-info").get("current-thread-id")
    except Exception:  # noqa: BLE001 - serveur sans threads
        return None


def _numbers(session) -> dict[str, int]:
    return {n.lower(): i for i, n in enumerate(session.register_names()) if n}


def _frame_regs(session, thread: str | None, level: int, names) -> dict[str, int]:
    """Registres déroulés par gdb pour le cadre `level`."""
    nums = _numbers(session)
    wanted = [nums[n] for n in names if n in nums]
    opts = f"--thread {thread} --frame {level} " if thread else ""
    res = session.gdb.command(f"-data-list-register-values {opts}x {' '.join(map(str, wanted))}")
    back = {i: n for n, i in nums.items()}
    out = {}
    for v in res.get("register-values", []):
        try:
            out[back[int(v["number"])]] = int(v["value"], 16)
        except (KeyError, ValueError):
            continue
    return out


@contextmanager
def _context(session, regs: dict[str, int]):
    """Registres du cœur remplacés le temps du bloc (pile demandée à gdb depuis un autre
    cadre), puis remis à leur valeur."""
    current = {n.lower(): v for n, v in session.registers()}
    written: dict[str, int] = {}
    try:
        for name in CONTEXT:
            if name in regs and regs[name] != current.get(name):
                written[name] = current[name]
                session.write_register(name, regs[name])
        yield
    finally:
        errors = []
        for name, value in written.items():
            try:
                session.write_register(name, value)
            except Exception as exc:  # noqa: BLE001 - dit plus bas, les autres sont remis
                errors.append(f"{name} : {exc}")
        if errors:
            raise RuntimeError("registres non remis après le déroulement : " + "; ".join(errors))


def _magic_exception(f) -> bool:
    return f.pc & 0xFF000000 == 0xFF000000 or (
        (f.func or "").startswith("<signal handler") and not is_fnc_return(f.pc))


def world_backtrace(session, state: SecurityState | None = None,
                    secure_read: Callable[[int, int], bytes] | None = None,
                    max_frames: int = 64) -> list[WorldFrame]:
    """Pile complète d'un cœur TrustZone, chaque cadre avec son monde ; la pile de gdb telle
    quelle (monde inconnu) sans extension de sécurité."""
    frames = session.backtrace()
    if state is None:
        state = security_state(session)
    if state is None or state.secure is None:
        return [WorldFrame(f, None) for f in frames]
    thread = _thread(session)
    cur = {n.lower(): v for n, v in session.registers()}
    out: list[WorldFrame] = []
    world = "S" if state.secure else "NS"
    pending_secure_sp: int | None = None      # haut de la pile sécurisée à l'appel non sécurisé
    ctx: dict[str, int] | None = None         # None : registres réels
    skip_first = False
    first_note = ""
    only_first = False
    for _round in range(MAX_SWITCHES + 1):
        nxt = None
        if only_first and ctx is None:
            break
        cm = _context(session, ctx) if ctx is not None else _nullcontext()
        with cm:
            seg = frames
            if ctx is not None:
                try:
                    seg = session.backtrace()
                except Exception:  # noqa: BLE001 - pile de l'autre monde illisible : son 1er cadre
                    seg = [session.frame()]
                    if not skip_first:
                        first_note += " ; pile illisible au-delà"
            for i, f in enumerate(seg):
                if i == 0 and skip_first:
                    continue
                if i == 0 and first_note:
                    out.append(WorldFrame(f, world, first_note.lstrip(" ;")))
                    if only_first:
                        break
                    continue
                if len(out) >= max_frames:
                    break
                if is_fnc_return(f.pc):
                    out.append(WorldFrame(f, world, "retour vers le monde sécurisé (FNC_RETURN)"))
                    nxt, by_gdb = _secure_caller(session, thread, i, cur, pending_secure_sp,
                                                 secure_read, out)
                    pending_secure_sp = None
                    if nxt is not None:
                        world = "S"
                        first_note = f"appelant sécurisé (cadre FNC_RETURN à {nxt['sp'] - 8:#010x})"
                        if not by_gdb:          # gdb ne lit pas la pile sécurisée : ce cadre seul
                            only_first = True
                            first_note += (" ; pile sécurisée lue hors de gdb, non déroulée "
                                           "au-delà depuis l'état non sécurisé")
                    break
                if _magic_exception(f):
                    out.append(WorldFrame(f, world, "exception"))
                    try:
                        exc = _frame_regs(session, thread, i, ("pc",)).get("pc", f.pc)
                    except Exception:  # noqa: BLE001 - EXC_RETURN illisible : monde inchangé
                        exc = f.pc
                    if exc & 0xFF000000 == 0xFF000000:
                        world = "S" if exc >> 6 & 1 else "NS"     # EXC_RETURN.S
                    continue
                if world == "S" and i >= 1:
                    try:
                        lr = _frame_regs(session, thread, i, ("lr",)).get("lr")
                    except Exception:  # noqa: BLE001
                        lr = None
                    if lr is not None and not lr & 1 and not lr & 0xFE000000 == 0xFE000000:
                        # appel reçu du monde non sécurisé : ce cadre et la suite sont NS
                        out.append(WorldFrame(f, "NS", "appel depuis le monde non sécurisé (SG)"))
                        regs = _frame_regs(session, thread, i, CONTEXT + ("xpsr",))
                        pending_secure_sp = regs.get("sp")
                        handler = bool(regs.get("xpsr", cur.get("xpsr", 0)) & 0x1FF)
                        regs["sp"] = cur.get(stack_name("NS", handler, cur.get("control_ns", 0)),
                                             cur.get("msp_ns"))
                        nxt = regs
                        world = "NS"
                        break
                out.append(WorldFrame(f, world))
        if nxt is None or len(out) >= max_frames or (only_first and ctx is not None):
            break
        ctx, skip_first = nxt, world == "NS"
        if skip_first:
            first_note = ""
    return out


class _nullcontext:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _secure_caller(session, thread, level, cur, pending_secure_sp, secure_read, out):
    """Pseudo-cadre FNC_RETURN au niveau `level` : (registres du cadre sécurisé appelant, lus
    par gdb ?), ou (None, False) : pile sécurisée illisible, dit par une note."""
    regs = _frame_regs(session, thread, level, CONTEXT + ("xpsr",))
    if pending_secure_sp is not None:
        sp = pending_secure_sp
    else:
        above = _frame_regs(session, thread, level - 1, ("xpsr",)) if level else {}
        handler = bool(above.get("xpsr", cur.get("xpsr", 0)) & 0x1FF)
        sp = cur.get(stack_name("S", handler, cur.get("control_s", 0)), cur.get("msp_s", 0))
    by_gdb = True
    try:
        data = session.read_memory(sp, 8)
    except Exception:  # noqa: BLE001 - mémoire sécurisée refusée dans l'état non sécurisé
        data = b""
    if len(data) < 8 and secure_read is not None:
        by_gdb = False
        try:
            data = secure_read(sp, 8)
        except Exception:  # noqa: BLE001
            data = b""
    if len(data) < 8:
        out[-1].note += f" ; pile sécurisée illisible depuis l'état non sécurisé ({sp:#010x})"
        return None, False
    ret = int.from_bytes(data[:4], "little")
    regs.update(pc=ret & ~1, sp=sp + 8)
    return regs, by_gdb
