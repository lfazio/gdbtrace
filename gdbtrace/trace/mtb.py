"""MTB (CoreSight Micro Trace Buffer) des Cortex-M0+ : trace des branchements en RAM.

Le Cortex-M0+ n'a ni ETM ni ITM/SWO. Sa trace d'instructions optionnelle, la MTB, écrit
dans une zone de la SRAM de la puce un paquet de 8 octets à chaque changement non séquentiel
du PC (branchement pris, exception, retour d'exception, PC modifié par le débogueur) :

    mot 0 : adresse source       [31:1], bit 0 = A (exception ou débogueur)
    mot 1 : adresse destination  [31:1], bit 0 = S (premier paquet après un démarrage)

Entre deux paquets, l'exécution est séquentielle, de la destination du précédent jusqu'à la
source du suivant : avec le code de l'ELF, on retrouve chaque instruction exécutée, et l'issue
de chaque branchement conditionnel (pris : il y a un paquet ; non pris : on l'a traversé).

- branchement : source = l'instruction de branchement (exécutée), destination = la cible ;
- entrée d'exception (A = 1) : source = adresse de retour (instruction pas encore exécutée),
  destination = le handler ; même forme quand le débogueur change le PC ;
- retour d'exception : deux paquets, `instruction de retour → EXC_RETURN` puis
  `EXC_RETURN (A = 1) → adresse de reprise` (un handler si l'exception suivante est
  enchaînée).

Le tampon est circulaire (2^(MASK+4) octets, aligné sur sa taille) : c'est un enregistreur de
vol, les derniers branchements avant l'arrêt. GdbTrace le lit à chaque arrêt, le décode, puis
remet le pointeur à zéro : chaque exécution est ajoutée une seule fois à l'historique et à la
couverture.

Registres (MTB-M0+, ARM DDI 0486) : POSITION (+0x0 : POINTER[31:3], décalage depuis
MTB_BASE ; WRAP bit 2), MASTER (+0x4 : EN bit 31, HALTREQ 9, RAMPRIV 8, SFRWPRIV 7,
TSTOPEN 6, TSTARTEN 5, MASK[4:0]), FLOW (+0x8 : WATERMARK[31:3], AUTOHALT 1, AUTOSTOP 0),
BASE (+0xC : adresse de la SRAM tracée, lecture seule).
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import Callable

from .etm.image import DIRECT, INDIRECT
from .etm.qemu_log import Event, ExcEntry, ExcReturn, Step

POSITION, MASTER, FLOW, BASE = 0x0, 0x4, 0x8, 0xC
MASTER_EN = 1 << 31
POSITION_WRAP = 1 << 2
PART_MTB_M0P = 0x932                    # numéro de pièce CoreSight du MTB-M0+
EXC_RETURN = 0xFFFFFFF9                 # simulation : retour en mode thread, pile MSP
#: adresses connues des registres MTB (à défaut de les trouver dans la table ROM)
KNOWN_BASES = {
    0x41006000: "Microchip SAM D21 / L21 / R21",
    0xF0000000: "NXP Kinetis KL / KE / KV (Cortex-M0+)",
}
SYMBOLS = ("__mtb_buffer__", "mtb_buffer", "__MTB_BUFFER", "_mtb_buffer")


def is_exc_return(addr: int) -> bool:
    return (addr & 0xFFFFFF00) == 0xFFFFFF00


@dataclass(frozen=True)
class Packet:
    src: int
    dst: int
    a: bool = False              # exception (entrée/retour) ou PC changé par le débogueur
    s: bool = False              # premier paquet d'une session de trace

    def pack(self) -> bytes:
        return struct.pack("<II", (self.src & ~1) | self.a, (self.dst & ~1) | self.s)


def parse_packets(data: bytes) -> list[Packet]:
    out = []
    for i in range(0, len(data) - 7, 8):
        w0, w1 = struct.unpack_from("<II", data, i)
        out.append(Packet(w0 & ~1, w1 & ~1, bool(w0 & 1), bool(w1 & 1)))
    return out


# ====================================================================== matériel
@dataclass
class MtbState:
    sram: int                    # MTB_BASE
    position: int
    master: int

    @property
    def size(self) -> int:
        return 1 << ((self.master & 0x1F) + 4)

    @property
    def start(self) -> int:
        """Adresse du tampon (aligné sur sa taille)."""
        return self.sram + ((self.position & ~7) & ~(self.size - 1))

    @property
    def index(self) -> int:
        return (self.position & ~7) & (self.size - 1)

    @property
    def wrapped(self) -> bool:
        return bool(self.position & POSITION_WRAP)

    @property
    def enabled(self) -> bool:
        return bool(self.master & MASTER_EN)


def read_state(read_u32: Callable[[int], int], base: int) -> MtbState:
    return MtbState(read_u32(base + BASE), read_u32(base + POSITION), read_u32(base + MASTER))


def read_packets(read_u32: Callable[[int], int], read_mem: Callable[[int, int], bytes],
                 base: int) -> tuple[list[Packet], MtbState]:
    """Paquets du tampon, du plus ancien au plus récent."""
    st = read_state(read_u32, base)
    if not st.wrapped and st.index == 0:
        return [], st
    mem = read_mem(st.start, st.size)
    data = mem[st.index:] + mem[:st.index] if st.wrapped else mem[:st.index]
    return parse_packets(data), st


def configure(write_u32: Callable[[int, int], None], read_u32: Callable[[int], int], base: int,
              buffer: int, size: int, flow: int = 0) -> MtbState:
    """Programme la MTB : tampon de `size` octets à `buffer` (dans la SRAM, aligné sur sa
    taille, réservé par le firmware : la MTB l'écrase), trace en continu (circulaire)."""
    if size < 16 or size & (size - 1):
        raise ValueError(f"taille du tampon MTB : puissance de 2 d'au moins 16 octets ({size})")
    if buffer % size:
        raise ValueError(f"tampon MTB à {buffer:#x} : doit être aligné sur sa taille ({size} octets)")
    sram = read_u32(base + BASE)
    if buffer < sram:
        raise ValueError(f"tampon MTB à {buffer:#x} hors de la SRAM tracée (MTB_BASE = {sram:#x})")
    write_u32(base + MASTER, 0)                                  # arrêt pendant la programmation
    write_u32(base + POSITION, buffer - sram)
    write_u32(base + FLOW, flow)
    write_u32(base + MASTER, MASTER_EN | (size.bit_length() - 1 - 4))
    return read_state(read_u32, base)


def restart(write_u32: Callable[[int, int], None], base: int, st: MtbState) -> None:
    """Pointeur au début du tampon, WRAP effacé : la prochaine lecture ne rendra que les
    paquets de l'exécution suivante."""
    write_u32(base + POSITION, st.start - st.sram)


# ====================================================================== découverte CoreSight
#: tables ROM explorées : celle du cœur Cortex-M0+, puis celles de fabricants connus
ROM_ROOTS = (0xE00FF000, 0xF0002000, 0x41003000)
#: noms indicatifs de pièces ARM courantes (numéro de pièce CoreSight) ; les autres sont
#: affichées par leur numéro
PART_NAMES = {0x932: "MTB-M0+", 0x008: "SCS (M0/M0+)", 0x00A: "DWT (M0/M0+)", 0x00B: "BPU (M0/M0+)",
              0x000: "SCS (M3)", 0x001: "ITM", 0x002: "DWT", 0x003: "FPB", 0x00C: "SCS (M4)",
              0x923: "TPIU (M3)", 0x9A1: "TPIU (M4)", 0x924: "ETM-M3", 0x925: "ETM-M4",
              0x975: "ETM-M7 (ETMv4, non pris en charge)"}


@dataclass
class Component:
    addr: int
    part: int
    cls: int                     # classe CoreSight : 1 = table ROM, 9 = composant de débogage
    designer: int                # JEP106 (0x43B : ARM)
    depth: int = 0

    @property
    def name(self) -> str:
        if self.cls == 1 and self.part not in PART_NAMES:
            return "table ROM"
        if self.designer == 0x43B:
            return PART_NAMES.get(self.part, f"pièce ARM {self.part:#05x}")
        return f"pièce {self.part:#05x} (fabricant {self.designer:#05x})"


def identify(read_u32: Callable[[int], int], addr: int) -> Component | None:
    """Composant CoreSight à `addr` (CIDR/PIDR), ou None (rien de lisible ni d'identifiable)."""
    try:
        cidr = [read_u32(addr + 0xFF0 + 4 * i) & 0xFF for i in range(4)]
        if cidr[0] != 0x0D or cidr[2] != 0x05 or cidr[3] != 0xB1:
            return None
        pidr = [read_u32(addr + 0xFE0 + 4 * i) & 0xFF for i in range(3)]
        pidr4 = read_u32(addr + 0xFD0) & 0xFF
    except Exception:  # noqa: BLE001 - adresse non mappée
        return None
    part = pidr[0] | (pidr[1] & 0x0F) << 8
    designer = (pidr[1] >> 4) | (pidr[2] & 0x07) << 4 | (pidr4 & 0x0F) << 7
    return Component(addr, part, cidr[1] >> 4, designer)


def rom_components(read_u32: Callable[[int], int], roots=ROM_ROOTS) -> list[Component]:
    """Parcours des tables ROM CoreSight (récursif) : tous les composants trouvés."""
    out: list[Component] = []
    seen: set[int] = set()

    def walk(table: int, depth: int) -> None:
        if table in seen or depth > 3:
            return
        seen.add(table)
        for i in range(0, 0x3C0, 4):
            try:
                entry = read_u32(table + i)
            except Exception:  # noqa: BLE001
                return
            if entry == 0:
                break
            if not entry & 1:
                continue
            comp = identify(read_u32, (table + (entry & 0xFFFFF000)) & 0xFFFFFFFF)
            if comp is None:
                continue
            comp.depth = depth
            out.append(comp)
            if comp.cls == 1:
                walk(comp.addr, depth + 1)

    for root in roots:
        top = identify(read_u32, root)
        if top is not None and top.cls == 1 and root not in seen:
            out.append(top)
            walk(root, 1)
    return out


def find_mtb(read_u32: Callable[[int], int], candidates: list[int] | None = None) -> int | None:
    """Registres de la MTB : d'abord par les tables ROM, puis aux adresses connues.
    Identification par le numéro de pièce (0x932, MTB-M0+)."""
    for comp in rom_components(read_u32):
        if comp.part == PART_MTB_M0P:
            return comp.addr
    for addr in candidates or list(KNOWN_BASES):
        comp = identify(read_u32, addr)
        if comp is not None and comp.part == PART_MTB_M0P:
            return addr
    return None


# ====================================================================== décodage
@dataclass
class MtbStats:
    packets: int = 0
    instructions: int = 0
    exceptions: int = 0
    sessions: int = 0            # paquets S (démarrages de la trace)
    debugger: int = 0            # PC changé par le débogueur
    errors: int = 0              # paquet incohérent avec le code (resynchronisation)


def vector_handlers(image, vtor: int = 0, count: int = 48) -> dict[int, int]:
    """{adresse de handler : numéro d'exception} d'après la table des vecteurs de l'image
    (le plus petit numéro quand un handler est partagé, comme Default_Handler)."""
    out: dict[int, int] = {}
    data = image.read(vtor, 4 * count) or image.read(vtor, 4 * 16) or b""
    for n in range(2, len(data) // 4):          # 1 = Reset : un saut du débogueur, pas une exception
        h = struct.unpack_from("<I", data, 4 * n)[0] & ~1
        if h and image.instr(h) is not None:
            out.setdefault(h, n)
    return out


class MtbDecoder:
    MAX_RUN = 1_000_000          # instructions séquentielles au plus entre deux paquets

    def __init__(self, image, vtor: int = 0):
        self.image = image
        self.handlers = vector_handlers(image, vtor)
        self.stats = MtbStats()

    def decode(self, packets: list[Packet], end_pc: int | None = None,
               start_pc: int | None = None) -> list[Event]:
        """Événements (Step, ExcEntry, ExcReturn) de la portion tracée. `end_pc` : PC à l'arrêt
        (prochaine instruction), pour compléter la fin, après le dernier paquet ; `start_pc` :
        PC à la reprise, si le tampon n'a pas débordé depuis (le début est alors complet)."""
        out: list[Event] = []
        st = self.stats
        cur: int | None = start_pc & ~1 if start_pc is not None else None   # prochaine instruction
        returning = False                    # entre `… → EXC_RETURN` et `EXC_RETURN → …`
        stack: list[int] = []                # adresses de retour des exceptions en cours
        for k, p in enumerate(packets):
            st.packets += 1
            if p.s:                          # trace (re)démarrée : ce qui précède est inconnu,
                st.sessions += 1             # sauf le PC de reprise s'il est fourni
                if k or start_pc is None:
                    cur, returning = None, False
            if p.a and is_exc_return(p.src):    # 2e paquet du retour d'exception
                if p.dst in self.handlers and (not stack or p.dst != stack[-1]):
                    out.append(ExcEntry(self.handlers[p.dst], p.dst, tailchain=True))
                    st.exceptions += 1           # enchaînement : pas de retour au code
                else:
                    out.append(ExcReturn())
                    if stack:
                        stack.pop()
                cur, returning = p.dst, False
                continue
            if p.a:                              # entrée d'exception, ou PC changé par le débogueur
                self._walk(cur, p.src, out, include_end=False)
                if p.dst in self.handlers:
                    out.append(ExcEntry(self.handlers[p.dst], p.dst))
                    stack.append(p.src)
                    st.exceptions += 1
                else:
                    st.debugger += 1
                cur = p.dst
                continue
            self._walk(cur, p.src, out, include_end=True)
            if is_exc_return(p.dst):
                cur, returning = None, True
            else:
                cur = p.dst
        if end_pc is not None and cur is not None:
            self._walk(cur, end_pc & ~1, out, include_end=False)
        st.instructions += sum(1 for e in out if isinstance(e, Step))
        return out

    def _walk(self, start: int | None, end: int, out: list[Event], include_end: bool) -> None:
        """Instructions séquentielles de `start` à `end` (inclus : le branchement pris). Un
        branchement conditionnel traversé n'a pas été pris (atome N)."""
        if start is None:                    # début de trace : seule la source est connue
            if include_end:
                out.append(Step(end, executed=True))
            return
        addr, it_left, n, mark = start, self._it_left_at(start), 0, len(out)
        while addr < end and n < self.MAX_RUN:
            ins = self.image.instr(addr)
            if ins is None:
                break
            in_it = it_left > 0
            it_left = _it_length(ins) or max(0, it_left - 1)
            branch = ins.kind in (DIRECT, INDIRECT)
            if branch and ins.kind == DIRECT and ins.target == addr + ins.size:
                branch = False               # saut vers l'instruction suivante : pas de paquet
            elif branch and not (ins.cond or in_it):
                break                        # branchement inconditionnel sans paquet : incohérent
            out.append(Step(addr, executed=not branch))
            addr += ins.size
            n += 1
        if addr != end:
            del out[mark:]                   # chemin impossible : on se resynchronise
            self.stats.errors += 1
            if include_end:
                out.append(Step(end, executed=True))
            return
        if include_end:
            out.append(Step(end, executed=True))

    def _it_left_at(self, addr: int) -> int:
        """Instructions d'un bloc IT restant à partir de `addr` (elle comprise), 0 hors bloc.
        Utile quand l'exécution reprend au milieu d'un bloc (exception prise entre `it` et
        l'instruction conditionnelle : l'état IT est sauvegardé dans xPSR)."""
        for back in range(2, 18, 2):            # au plus 4 instructions de 2 ou 4 octets
            c = addr - back
            ins = self.image.instr(c)
            n = _it_length(ins) if ins is not None else 0
            if not n:
                continue
            a, k = c + ins.size, 0
            while k < n and a < addr:
                nxt = self.image.instr(a)
                if nxt is None:
                    break
                a, k = a + nxt.size, k + 1
            if a == addr and k < n:
                return n - k
        return 0


def _it_length(ins) -> int:
    """Nombre d'instructions couvertes si `ins` est une instruction IT, sinon 0."""
    mnem = ins.text.split()[0].split(".")[0] if ins.text else ""
    if mnem.startswith("it") and len(mnem) <= 4 and set(mnem[2:]) <= {"t", "e"}:
        return len(mnem) - 1
    return 0


# ====================================================================== encodage (simulation)
def encode(events: list[Event], image, start: bool = True) -> list[Packet]:
    """Paquets qu'une MTB écrirait pour ce chemin d'exécution (QEMU, tests). Premier paquet
    marqué S si `start` (trace qui démarre) ; pas de limite de taille (voir `last`)."""
    from .etm.cycles import successors
    nxt = successors(events)
    out: list[Packet] = []
    prev: tuple[int, int] | None = None      # (indice, adresse) de la dernière instruction
    entered: int | None = None               # handler pas encore commencé (exception imbriquée)
    pending_return = False

    def emit(src: int, dst: int, a: bool = False) -> None:
        out.append(Packet(src, dst, a, s=start and not out))

    def seq(addr: int) -> int:
        ins = image.instr(addr)
        return addr + (ins.size if ins else 2)

    for i, ev in enumerate(events):
        if isinstance(ev, Step):
            if pending_return:
                emit(EXC_RETURN, ev.addr, a=True)
                pending_return = False
            elif prev is not None and ev.addr != seq(prev[1]):
                emit(prev[1], ev.addr)
            prev, entered = (i, ev.addr), None
        elif isinstance(ev, ExcEntry):
            if pending_return:               # retour immédiatement suivi d'une exception en
                emit(EXC_RETURN, ev.handler, a=True)    # attente : enchaînement sur le matériel
            elif ev.tailchain and prev is not None:
                emit(prev[1], EXC_RETURN)
                emit(EXC_RETURN, ev.handler, a=True)
            elif prev is not None:
                pi, pa = prev
                # adresse de retour : prochaine instruction du contexte, si elle suit bien `pa`
                # (sous RTOS, le retour peut reprendre une autre tâche)
                ret, ins = nxt.get(pi), image.instr(pa)
                if ins is not None and ins.kind == DIRECT and not ins.cond:
                    ret = ins.target             # B / BL : toujours pris
                elif ret is None or ins is None or not (
                        ret == seq(pa) or (ins.kind == DIRECT and ret == ins.target)
                        or ins.kind == INDIRECT):
                    ret = seq(pa)
                if ret != seq(pa):
                    emit(pa, ret)            # branchement pris juste avant l'exception
                emit(ret, ev.handler, a=True)
            elif entered is not None:        # exception prise avant la 1re instruction du handler
                emit(entered, ev.handler, a=True)
            pending_return = False
            prev, entered = None, ev.handler
        elif isinstance(ev, ExcReturn):
            if prev is not None:
                emit(prev[1], EXC_RETURN)
            pending_return = True
            prev, entered = None, None
    return out


def last(packets: list[Packet], size: int) -> list[Packet]:
    """Ce que contient un tampon circulaire de `size` octets à la fin : les derniers paquets."""
    n = size // 8
    return packets[-n:] if len(packets) > n else list(packets)
