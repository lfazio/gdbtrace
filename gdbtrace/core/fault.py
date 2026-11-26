"""Analyse de faute Cortex-M (vue Faute, `gdbtrace.fault()`, `--break-on-fault`).

Cible arrêtée dans un gestionnaire de faute (HardFault, MemManage, BusFault, UsageFault,
SecureFault), on lit :
- les registres d'état du SCB : CFSR (MMFSR, BFSR, UFSR), HFSR, MMFAR, BFAR, et en ARMv8-M
  avec la sécurité SFSR, SFAR. Un Cortex-M0/M0+ (ARMv6-M, aussi l'ARMv8-M Baseline du M23) n'a
  ni CFSR ni adresses de faute : leur zone du SCB est réservée, on n'y touche pas ;
- le cadre empilé à l'entrée de l'exception (r0-r3, r12, lr, pc, xPSR). Sa base et EXC_RETURN
  viennent du pseudo-cadre « <signal handler called> » de gdb, qui déroule l'exception ($sp
  de ce cadre = base du cadre empilé, $pc = EXC_RETURN) ; à défaut, de lr et de MSP / PSP ;
- l'instruction fautive (PC empilé), sa fonction et sa ligne : le cadre suivant de gdb.

Les causes sont dites en clair. Sans CFSR (ARMv6-M), elles sont déduites de l'instruction
fautive et du cadre : bit Thumb perdu, UDF, adresse d'un accès mémoire (non aligné ou sans
mémoire), ce que la vue signale comme déduction.

`FaultCatch` arrête la cible à l'entrée de toute faute : vector catch (DEMCR, ou la commande
`cortex_m vector_catch` d'OpenOCD qui le gère), sinon breakpoints sur les gestionnaires de la
table des vecteurs (QEMU n'émule pas le vector catch).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

CPUID = 0xE000ED00
ICSR = 0xE000ED04
VTOR = 0xE000ED08
CFSR = 0xE000ED28            # puis HFSR, DFSR, MMFAR, BFAR, AFSR (6 mots)
SFSR = 0xE000EDE4            # puis SFAR
DEMCR = 0xE000EDFC

#: numéro d'exception → nom (VECTACTIVE)
FAULTS = {3: "HardFault", 4: "MemManage", 5: "BusFault", 6: "UsageFault", 7: "SecureFault"}
EXCEPTION_NAMES = {1: "Reset", 2: "NMI", **FAULTS, 11: "SVCall", 12: "DebugMonitor",
                   14: "PendSV", 15: "SysTick"}

#: ARMv8-M Mainline : CFSR, et SFSR si la sécurité est implémentée (M33, M35P, M52, M55, M85)
V8M_MAIN = {0xD21, 0xD31, 0xD24, 0xD22, 0xD23}

#: (bit, nom, explication) du CFSR : MMFSR [7:0], BFSR [15:8], UFSR [31:16]
CFSR_BITS = [
    (0, "IACCVIOL", "violation d'accès en exécution (MPU, ou région non exécutable)"),
    (1, "DACCVIOL", "violation d'accès aux données (MPU)"),
    (3, "MUNSTKERR", "faute MPU au dépilement d'un retour d'exception"),
    (4, "MSTKERR", "faute MPU à l'empilement d'une entrée d'exception (pile hors région ?)"),
    (5, "MLSPERR", "faute MPU à la sauvegarde paresseuse de l'état flottant"),
    (7, "MMARVALID", "MMFAR contient l'adresse fautive"),
    (8, "IBUSERR", "erreur de bus en lisant une instruction"),
    (9, "PRECISERR", "erreur de bus précise sur une donnée"),
    (10, "IMPRECISERR", "erreur de bus imprécise (écriture tamponnée) : le PC empilé est après "
                        "l'instruction fautive"),
    (11, "UNSTKERR", "erreur de bus au dépilement d'un retour d'exception"),
    (12, "STKERR", "erreur de bus à l'empilement d'une entrée d'exception (pile débordée ?)"),
    (13, "LSPERR", "erreur de bus à la sauvegarde paresseuse de l'état flottant"),
    (15, "BFARVALID", "BFAR contient l'adresse fautive"),
    (16, "UNDEFINSTR", "instruction indéfinie"),
    (17, "INVSTATE", "état invalide : bit Thumb à 0 (saut vers une adresse paire, pointeur de "
                     "fonction invalide ?)"),
    (18, "INVPC", "retour d'exception invalide (EXC_RETURN incohérent)"),
    (19, "NOCP", "coprocesseur absent ou désactivé (FPU non activée dans CPACR ?)"),
    (20, "STKOF", "débordement de pile : limite MSPLIM ou PSPLIM franchie"),
    (24, "UNALIGNED", "accès non aligné (CCR.UNALIGN_TRP, ou LDM/STM/LDRD non aligné)"),
    (25, "DIVBYZERO", "division par zéro (CCR.DIV_0_TRP)"),
]
HFSR_BITS = [
    (1, "VECTTBL", "erreur de bus en lisant la table des vecteurs"),
    (30, "FORCED", "faute configurable montée en HardFault (gestionnaire désactivé, ou priorité "
                   "insuffisante) : la cause est dans le CFSR"),
    (31, "DEBUGEVT", "événement de débogage sans débogueur (BKPT)"),
]
SFSR_BITS = [
    (0, "INVEP", "entrée sécurisée invalide (pas de SG dans une région NSC)"),
    (1, "INVIS", "EXC_RETURN invalide pour l'état de sécurité"),
    (2, "INVER", "retour d'exception invalide (intégrité du cadre sécurisé)"),
    (3, "AUVIOL", "accès non sécurisé à une adresse sécurisée (SAU / IDAU)"),
    (4, "INVTRAN", "branchement de sécurisé vers non sécurisé sans BXNS/BLXNS"),
    (5, "LSPERR", "erreur à la sauvegarde paresseuse de l'état flottant"),
    (6, "SFARVALID", "SFAR contient l'adresse fautive"),
    (7, "LSERR", "erreur à l'entrée ou au retour paresseux"),
]
#: bits de vector catch de DEMCR (VC_HARDERR seul en ARMv6-M)
VC_MMERR, VC_NOCPERR, VC_CHKERR, VC_STATERR = 1 << 4, 1 << 5, 1 << 6, 1 << 7
VC_BUSERR, VC_INTERR, VC_HARDERR, VC_SFERR = 1 << 8, 1 << 9, 1 << 10, 1 << 11
VC_ALL_V7 = VC_MMERR | VC_NOCPERR | VC_CHKERR | VC_STATERR | VC_BUSERR | VC_INTERR | VC_HARDERR

STACKED = ("r0", "r1", "r2", "r3", "r12", "lr", "pc", "xpsr")


def is_exc_return(value: int) -> bool:
    """EXC_RETURN (0xFFFFFFxx) ou FNC_RETURN (0xFEFFFFFE/F) : valeurs de lr hors du code."""
    return value & 0xFF000000 in (0xFF000000, 0xFE000000)


@dataclass
class StackedFrame:
    """Cadre empilé par le matériel à l'entrée de l'exception."""
    base: int                        # adresse de r0 empilé
    exc_return: int
    regs: dict[str, int]             # r0, r1, r2, r3, r12, lr, pc, xpsr
    exact: bool = True               # False : base déduite de MSP/PSP, prologue du gestionnaire inconnu

    @property
    def stack(self) -> str:
        return "PSP" if self.exc_return & 0x4 else "MSP"

    @property
    def thread_mode(self) -> bool:
        return bool(self.exc_return & 0x8)

    @property
    def fp_context(self) -> bool:
        return not self.exc_return & 0x10

    def describe(self) -> str:
        mode = "Thread" if self.thread_mode else "Handler"
        fp = ", contexte flottant empilé" if self.fp_context else ""
        return f"EXC_RETURN {self.exc_return:#010x} : retour en mode {mode}, pile {self.stack}{fp}"


@dataclass
class FaultReport:
    """Ce que la vue Faute montre."""
    arch: str                                   # "ARMv6-M", "ARMv7-M", "ARMv8-M Mainline"…
    active: int                                 # ICSR.VECTACTIVE
    registers: dict[str, int] = field(default_factory=dict)
    causes: list[tuple[str, str]] = field(default_factory=list)   # (nom du bit, explication)
    notes: list[str] = field(default_factory=list)
    frame: StackedFrame | None = None
    fault_pc: int | None = None
    fault_func: str | None = None
    fault_file: str | None = None
    fault_fullname: str | None = None
    fault_line: int | None = None
    instruction: str | None = None
    address: int | None = None                  # adresse de la donnée fautive, si connue
    deduced: bool = False                       # causes déduites de l'instruction (ARMv6-M)

    @property
    def exception(self) -> str | None:
        return EXCEPTION_NAMES.get(self.active, f"exception {self.active}" if self.active else None)

    @property
    def in_fault(self) -> bool:
        return self.active in FAULTS

    def where(self) -> str:
        if self.fault_pc is None:
            return ""
        loc = f"{self.fault_func or '?'}"
        if self.fault_file and self.fault_line:
            loc += f" ({self.fault_file.rsplit('/', 1)[-1]}:{self.fault_line})"
        return f"{loc} à {self.fault_pc:#x}"

    def summary(self) -> str:
        """Une ligne : barre d'état, sortie des scripts."""
        if not self.in_fault:
            if any(self.registers.get(k) for k in ("CFSR", "HFSR", "SFSR")):
                return "pas de faute en cours (registres d'une faute passée : ils restent jusqu'à leur effacement)"
            return "pas de faute en cours"
        roots = [(n, t) for n, t in self.causes if not n.endswith("VALID")]
        forced = any(n == "FORCED" for n, _t in roots)
        first = next((t for n, t in roots if n not in ("FORCED", "DEBUGEVT")),
                     roots[0][1] if roots else None)
        text = f"{self.exception}" + (" (escaladée)" if forced else "")
        if first:
            text += f" : {first.split(' (')[0]}"
        if self.address is not None and f"{self.address:#x}" not in text:
            text += f", adresse {self.address:#x}"
        if self.fault_pc is not None:
            text += f" — {self.where()}"
        return text

    def as_dict(self) -> dict:
        return {
            "exception": self.exception, "en_faute": self.in_fault, "architecture": self.arch,
            "causes": [{"bit": n, "texte": t} for n, t in self.causes], "notes": list(self.notes),
            "registres": dict(self.registers), "adresse": self.address,
            "pc": self.fault_pc, "fonction": self.fault_func, "fichier": self.fault_file,
            "ligne": self.fault_line, "instruction": self.instruction, "deduit": self.deduced,
            "cadre": None if self.frame is None else {
                "base": self.frame.base, "exc_return": self.frame.exc_return,
                "pile": self.frame.stack, **self.frame.regs},
        }

    def text(self) -> str:
        """Rapport complet en texte (console, `gdbtrace.fault()`)."""
        out = [self.summary()]
        for name, t in self.causes:
            out.append(f"  {name:<12} {t}")
        if self.instruction:
            out.append(f"  instruction : {self.instruction}")
        for n in self.notes:
            out.append(f"  note : {n}")
        if self.registers:
            out.append("  " + "  ".join(f"{k} {v:#010x}" for k, v in self.registers.items()))
        if self.frame is not None:
            f = self.frame
            out.append(f"  cadre empilé à {f.base:#010x} ({f.describe()})")
            out.append("  " + "  ".join(f"{k} {f.regs[k]:#010x}" for k in STACKED))
        return "\n".join(out)


def core_arch(cpuid: int) -> str:
    """Architecture d'après CPUID : ARMv6-M, ARMv8-M Baseline, ARMv7-M, ARMv8-M Mainline."""
    part = (cpuid >> 4) & 0xFFF
    if (cpuid >> 16) & 0xF == 0xC:
        return "ARMv8-M Baseline" if part == 0xD20 else "ARMv6-M"
    return "ARMv8-M Mainline" if part in V8M_MAIN else "ARMv7-M"


def has_cfsr(arch: str) -> bool:
    return arch in ("ARMv7-M", "ARMv8-M Mainline")


def _bits(value: int, table) -> list[tuple[str, str]]:
    return [(name, text) for bit, name, text in table if value >> bit & 1]


def _current_thread(session) -> str | None:
    try:
        return session.gdb.command("-thread-info").get("current-thread-id")
    except Exception:  # noqa: BLE001 - serveur sans threads : on s'en passe
        return None


def _frame_reg(session, thread: str | None, level: int, reg: str) -> int:
    opts = f"--thread {thread} --frame {level} " if thread else ""
    val = session.gdb.command(f'-data-evaluate-expression {opts}"(unsigned long)${reg}"')["value"]
    return int(val.split()[0], 0) & 0xFFFFFFFF


def find_stacked_frame(session, arch: str):
    """(StackedFrame, cadre interrompu de gdb, niveau du cadre interrompu) ou (None, None, None).

    Le pseudo-cadre d'exception de gdb donne la base et EXC_RETURN ; sinon lr, s'il vaut
    EXC_RETURN, et MSP ou PSP (base approximative : le prologue du gestionnaire a pu déplacer sp)."""
    try:
        bt = session.backtrace()
    except Exception:  # noqa: BLE001
        bt = []
    thread = _current_thread(session)
    base = exc = None
    level = None
    exact = True
    for i, f in enumerate(bt):
        if is_exc_return(f.pc) or (f.func or "").startswith("<signal handler"):
            try:
                base = _frame_reg(session, thread, i, "sp")
                exc = _frame_reg(session, thread, i, "pc")
                level = i + 1
            except Exception:  # noqa: BLE001 - registres du cadre illisibles : repli
                base = None
            break
    if base is None:
        try:
            lr = session.register("lr")
        except Exception:  # noqa: BLE001
            return None, None, None
        if not is_exc_return(lr):
            return None, None, None
        exc, exact = lr, False
        try:
            base = session.register("psp" if lr & 0x4 else "msp")
        except Exception:  # noqa: BLE001 - serveur sans msp/psp : sp courant
            base = session.register("sp")
    regs_at = base
    if arch == "ARMv8-M Mainline" and not exc & (1 << 5) and exc & 0xFF000000 == 0xFF000000:
        regs_at += 0x28              # DCRS = 0 : signature et r4-r11 empilés avant le cadre de base
    data = session.read_memory(regs_at, 32)
    regs = {name: int.from_bytes(data[4 * k:4 * k + 4], "little") for k, name in enumerate(STACKED)}
    frame = StackedFrame(base=regs_at, exc_return=exc, regs=regs, exact=exact)
    caller = bt[level] if level is not None and level < len(bt) else None
    return frame, caller, level


_MEM_OP = re.compile(r"^(ldr|str)(b|h|sb|sh|d)?(\.w|\.n)?\s+[^,]+,\s*\[(\w+)(?:,\s*(#-?\w+|\w+))?\]")
_SIZE = {None: 4, "b": 1, "sb": 1, "h": 2, "sh": 2, "d": 8}


def _deduce(report: FaultReport, session, level: int | None) -> None:
    """ARMv6-M : pas de CFSR. Cause déduite du cadre et de l'instruction fautive."""
    f = report.frame
    report.deduced = True
    if f is not None and not f.regs["xpsr"] >> 24 & 1:
        report.causes.append(("T=0", "bit Thumb à 0 dans xPSR : saut vers une adresse paire "
                                     "(pointeur de fonction invalide ?)"))
        return
    ins = (report.instruction or "").lower()
    if ins.startswith("udf") or ins.startswith("(bad)") or ins.startswith(".inst"):
        report.causes.append(("UDF", "instruction indéfinie"))
        return
    m = _MEM_OP.match(ins)
    if m and level is not None:
        thread = _current_thread(session)
        try:
            base = _frame_reg(session, thread, level, m.group(4))
            off = m.group(5)
            if off is None:
                delta = 0
            elif off.startswith("#"):
                delta = int(off[1:], 0)
            else:
                delta = _frame_reg(session, thread, level, off)
        except Exception:  # noqa: BLE001 - registre illisible : pas de déduction d'adresse
            report.causes.append(("ACCÈS", "accès mémoire fautif (adresse non déterminée)"))
            return
        addr = (base + delta) & 0xFFFFFFFF
        size = _SIZE.get(m.group(2), 4)
        kind = "lecture" if m.group(1) == "ldr" else "écriture"
        report.address = addr
        if addr % min(size, 4):
            report.causes.append(("NON ALIGNÉ", f"{kind} de {size} octets non alignée à {addr:#x} "
                                                "(interdite en ARMv6-M)"))
        else:
            report.causes.append(("ACCÈS", f"{kind} à {addr:#x} : adresse sans mémoire, ou "
                                           "périphérique absent ou sans horloge ?"))
        return
    if m is None and re.match(r"^(ldm|stm|push|pop)", ins):
        report.causes.append(("ACCÈS", "accès mémoire multiple fautif (pile ou pointeur invalide ?)"))
        return
    report.causes.append(("?", "cause non enregistrée par ce cœur (pas de CFSR) : voir "
                               "l'instruction et le cadre empilé"))


def analyze(session) -> FaultReport:
    """Analyse de la faute en cours (cible arrêtée)."""
    cpuid = session.read_u32(CPUID)
    arch = core_arch(cpuid)
    active = session.read_u32(ICSR) & 0x1FF
    report = FaultReport(arch=arch, active=active)
    if has_cfsr(arch):
        words = session.read_memory(CFSR, 24)
        cfsr, hfsr, _dfsr, mmfar, bfar, _afsr = (int.from_bytes(words[i:i + 4], "little")
                                                 for i in range(0, 24, 4))
        report.registers = {"CFSR": cfsr, "HFSR": hfsr, "MMFAR": mmfar, "BFAR": bfar}
        sfsr = sfar = 0
        if arch == "ARMv8-M Mainline":
            try:
                sec = session.read_memory(SFSR, 8)
                sfsr, sfar = int.from_bytes(sec[:4], "little"), int.from_bytes(sec[4:], "little")
            except Exception:  # noqa: BLE001 - sécurité non implémentée : RAZ ou refus
                sfsr = sfar = 0
            if sfsr:
                report.registers.update({"SFSR": sfsr, "SFAR": sfar})
        report.causes = _bits(hfsr, HFSR_BITS) + _bits(cfsr, CFSR_BITS) + _bits(sfsr, SFSR_BITS)
        if sfsr >> 6 & 1:
            report.address = sfar
        elif cfsr >> 15 & 1:
            report.address = bfar
        elif cfsr >> 7 & 1:
            report.address = mmfar
    if not report.in_fault:
        return report
    frame, caller, level = find_stacked_frame(session, arch)
    report.frame = frame
    if frame is not None:
        report.fault_pc = frame.regs["pc"]
        if not frame.exact:
            report.notes.append("cadre retrouvé par lr et la pile (pas de déroulement de gdb) : "
                                "base approximative si le gestionnaire a déjà empilé")
        if caller is not None:
            report.fault_func, report.fault_file, report.fault_line = caller.func, caller.file, caller.line
            report.fault_fullname = caller.fullname
        else:
            try:
                report.fault_func = session.symbol_at(report.fault_pc)
            except Exception:  # noqa: BLE001
                pass
        try:
            ins = session.disassemble(report.fault_pc & ~1, (report.fault_pc & ~1) + 4)
            if ins:
                report.instruction = ins[0].get("inst", "").replace("\t", " ")
        except Exception:  # noqa: BLE001 - PC hors du code (saut invalide)
            report.instruction = None
        if (report.registers.get("CFSR", 0) >> 10) & 1:
            report.notes.append("faute imprécise : l'instruction fautive précède le PC empilé")
        if not frame.regs["xpsr"] >> 24 & 1 or (report.registers.get("CFSR", 0) >> 17) & 1:
            lr = frame.regs["lr"]       # saut invalide : le PC empilé est la destination
            try:
                origin = session.symbol_at(lr & ~1).split(" in section")[0]
            except Exception:  # noqa: BLE001
                origin = ""
            report.notes.append(f"le PC empilé est la destination du saut ; appel depuis "
                                f"{origin or '?'} (lr {lr:#x})")
    if not has_cfsr(arch):
        _deduce(report, session, level)
    elif not report.causes:
        report.causes.append(("?", "aucun bit de cause levé (faute effacée par le logiciel ?)"))
    return report


def in_fault(session) -> bool:
    """Arrêt dans un gestionnaire de faute ? Une seule lecture (ICSR.VECTACTIVE)."""
    return (session.read_u32(ICSR) & 0x1FF) in FAULTS


class FaultCatch:
    """Arrêt de la cible à l'entrée de toute faute (`--break-on-fault`)."""

    def __init__(self, session):
        self.s = session
        self.method: str | None = None       # "vector catch", "breakpoints" ou None
        self.breakpoints: list[str] = []

    def enable(self) -> str:
        """Pose l'arrêt ; rend la méthode employée, en clair."""
        self.disable()
        s = self.s
        arch = core_arch(s.read_u32(CPUID))
        bits = VC_HARDERR if not has_cfsr(arch) else VC_ALL_V7
        target = getattr(s, "target", None)
        if type(target).__name__ == "OpenOcdTarget":
            names = "hard_err" if not has_cfsr(arch) else \
                "hard_err int_err bus_err state_err chk_err nocp_err mm_err"
            try:
                s.monitor(f"cortex_m vector_catch {names}")
            except Exception:  # noqa: BLE001 - commande refusée : DEMCR directement
                pass
        if not s.read_u32(DEMCR) & bits:
            s.write_u32(DEMCR, s.read_u32(DEMCR) | bits)
        if s.read_u32(DEMCR) & bits == bits:
            self.method = "vector catch"
            return "arrêt sur faute : vector catch (DEMCR)"
        # pas de vector catch (QEMU) : breakpoints sur les gestionnaires de faute
        vtor = s.read_u32(VTOR) if has_cfsr(arch) else 0
        numbers = [3] if not has_cfsr(arch) else [3, 4, 5, 6] + ([7] if arch == "ARMv8-M Mainline" else [])
        seen: set[int] = set()
        for n in numbers:
            addr = s.read_u32(vtor + 4 * n) & ~1
            if addr and addr not in seen:
                seen.add(addr)
                self.breakpoints.append(s.break_(f"*{addr:#x}").number)
        self.method = "breakpoints"
        return (f"arrêt sur faute : {len(self.breakpoints)} breakpoint(s) sur les gestionnaires "
                "de faute (pas de vector catch sur ce serveur)")

    def disable(self) -> None:
        s = self.s
        if self.method == "vector catch":
            try:
                if type(getattr(s, "target", None)).__name__ == "OpenOcdTarget":
                    s.monitor("cortex_m vector_catch none")
                s.write_u32(DEMCR, s.read_u32(DEMCR) & ~(VC_ALL_V7 | VC_SFERR))
            except Exception:  # noqa: BLE001 - déconnecté : rien à retirer
                pass
        for number in self.breakpoints:
            try:
                s.delete_break(number)
            except Exception:  # noqa: BLE001
                pass
        self.breakpoints = []
        self.method = None
