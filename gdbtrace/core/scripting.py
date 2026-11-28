"""API de script de GdbTrace, en Python.

Dans la console et les scripts, l'objet `gdbtrace` est disponible :

    gdbtrace.break_("fib")            # Break.Set fib
    gdbtrace.go(); gdbtrace.wait()    # Go + WAIT !STATE.RUN()
    print(gdbtrace.eval("counter"))   # Var.view counter
    gdbtrace.d(0x20000000, 32)        # Data.dump
    gdbtrace.r()                      # Register.view
    gdbtrace.mem(0x20000000, 16)      # lecture mémoire, même cible en marche (canal live)
    gdbtrace.periph("SysTick")        # PER.view (nécessite un SVD)

Un script `.py` est exécuté avec `gdbtrace.run_script("init.py")` ou au démarrage avec
l'option `--script`.
"""
from __future__ import annotations

import code
import contextlib
import io
import os
import runpy
import threading
from typing import Callable

from . import svd
from .gdb_backend import GdbError
from .live import LiveMemory
from .rtos import freertos
from .session import Session


def hexdump(data: bytes, base: int, width: int = 16) -> str:
    lines = []
    for off in range(0, len(data), width):
        chunk = data[off:off + width]
        hx = " ".join(f"{b:02x}" for b in chunk).ljust(width * 3 - 1)
        asc = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        lines.append(f"{base + off:08x}  {hx}  {asc}")
    return "\n".join(lines)


class StopRecord(dict):
    """Enregistrement d'arrêt de gdb (`reason`, `frame`, `bkptno`, `wpt`, `value`,
    `return-value`...) : un dict, affiché en une ligne dans la console. Après Step.Till /
    Step.Change : aussi `what`, `expr`, `steps`, `met` (condition atteinte), `old` / `new`."""

    def __repr__(self) -> str:
        return f"<arrêt : {stop_summary(self)}>"


def stop_summary(rec: dict) -> str:
    """Arrêt en une ligne : raison (ou bilan d'un Step.Till / Step.Change) — lieu."""
    f = rec.get("frame") or {}
    where = f.get("func") or f.get("addr") or "?"
    if f.get("file") and f.get("line"):
        where += f" ({f['file']}:{f['line']})"
    if "what" not in rec:
        return f"{rec.get('reason', '?')} — {where}"
    what = f"{rec['what']} {rec.get('expr', '')}, {rec.get('steps', 0)} pas"
    if "new" in rec and rec.get("met"):
        what += f", {rec['old']} → {rec['new']}"
    if not rec.get("met"):
        reason = rec.get("reason", "?")
        what += ", " + (rec.get("error") or ("limite atteinte" if reason == "end-stepping-range"
                                             else reason))
    return f"{what} — {where}"


class CoverageAPI:
    """COVerage : couverture de code mesurée par la trace ETM, sans instrumentation.

        gdbtrace.cov.summary()            # instructions, branches, fonctions
        gdbtrace.cov.list(20)             # COVerage.ListFunc (les moins couvertes d'abord)
        gdbtrace.cov.files()              # COVerage.ListModule
        gdbtrace.cov.export("cov.html")   # COVerage.EXPORT : .html, .info (LCOV), .json
        gdbtrace.cov.save("run1.json")    # COVerage.SAVE
        gdbtrace.cov.load("run2.json")    # COVerage.LOAD /Add (fusion)
        gdbtrace.cov.reset()              # COVerage.RESet
    """

    def __init__(self, api: "ScriptAPI"):
        self._api = api

    @property
    def db(self):
        return self._api._etm().cov

    def summary(self) -> str:
        s = self.db.summary()
        print(s)
        tm = self._api.trace
        if tm is not None and tm.etm is not None and not tm.etm.stats["instructions"]:
            print(f"  aucune instruction tracée : {tm.trace_status()}")
        return s

    def list(self, top: int | None = 20, file: str | None = None):
        """Fonctions, les moins couvertes d'abord (puis par nom)."""
        rows = [f for f in self.db.functions() if file is None or (f.file or "").endswith(file)]
        rows.sort(key=lambda f: (f.percent, f.branch_percent if f.branch_percent is not None else 101, f.name))
        print(f"  {'état':<8} {'instr.':>7} {'branches':>9} {'entrées':>8}  fonction")
        for f in rows[:top]:
            br = f"{f.branch_percent:7.1f} %" if f.branch_percent is not None else f"{'—':>9}"
            print(f"  {f.status:<8} {f.percent:6.1f} % {br} {f.entries:>8}  {f.name}")
        return rows

    def files(self):
        rows = self.db.files()
        for m in rows:
            br = f"{m.branch_percent:6.1f} %" if m.branch_percent is not None else "     —"
            print(f"  {m.status:<8} {m.percent:6.1f} % {br}  {m.path}")
        return rows

    def reset(self) -> None:
        self.db.reset()

    def save(self, path: str) -> None:
        self.db.save(path)

    def load(self, path: str, merge: bool = True) -> None:
        self.db.load(path, merge)

    def export(self, path: str) -> str:
        from ..trace.coverage_report import export
        tm = self._api.trace
        note = tm.source_label() if tm is not None else "trace ETM"
        fmt = export(self.db, path, note)
        print(f"couverture exportée ({fmt}) : {path}")
        return fmt


def _record_action(fn):
    """Une action de script reçoit l'arrêt en StopRecord."""
    if fn is None:
        return None
    return lambda payload: fn(StopRecord(payload))


class ScriptAPI:
    def __init__(self, session: Session, live: LiveMemory | None = None,
                 device: svd.Device | None = None):
        self.s = session
        #: canal mémoire en direct (None si le serveur gdb n'en offre pas)
        self.live = live
        #: périphériques décrits par le SVD chargé (None sinon)
        self.device = device
        self._rtos: freertos.FreeRtosLayout | None = None
        #: capture SWO/ITM (TraceManager), fournie par l'interface ou le mode batch
        self.trace = None
        #: table des symboles de l'ELF (chargée à la demande)
        self.symbols = None
        #: fréquence du cœur (conversion cycles → µs de l'analyse temporelle)
        self.cpu_hz = 25_000_000
        #: puce décrite par son CMSIS-Pack (--device : core/pack.Device), ou None
        self.chip = None
        #: appelé après chaque programmation avec le chemin de l'ELF (l'interface s'y recâble)
        self.on_program: Callable[[str], None] | None = None
        #: Var.Watch : ajoute une expression à la vue Watch (fourni par l'interface)
        self.on_var_watch: Callable[[str, str], None] | None = None
        #: COVerage : couverture de code depuis la trace ETM
        self.cov = CoverageAPI(self)
        self._until: str | None = None      # breakpoint temporaire de go(until=…)
        self._config: dict | None = None    # configuration de session (menu Configuration)
        #: cas en ligne de `check()` : (nom, réussi, message) ; le mode batch en tire son code
        self.checks: list[tuple[str, bool, str]] = []
        self._junit = None                  # rapport JUnit en cours (--junit : core/junit.py)
        #: plusieurs cœurs (--core : core/cores.CoreSet), ou None
        self.core_set = None
        self._main_session = session
        #: RunTime : temps du cœur entre la reprise et l'arrêt (SysTick, DWT_CYCCNT)
        from .chrono import Chrono
        self._chrono = Chrono(session, core=lambda: self.trace.core if self.trace is not None else None)
        session.add_resume_hook(self._chrono.start)
        self._fault_catch = None            # arrêt sur faute (FaultCatch)
        #: BMC : compteurs de performance (DWT, PMU), armés à la demande
        from .counters import Counters
        self._counters = Counters(
            session, core=lambda: self.trace.core if self.trace is not None else None,
            tally=lambda: self.trace.counter_tally if self.trace is not None else None,
            capturing=lambda: bool(self.trace is not None and self.trace.active))
        session.add_resume_hook(self._counters.start)
        #: TERM : semihosting et RTT (vue Terminal, `terminal()`)
        from .terminal import Terminal
        self.term = Terminal()

    def chrono(self) -> dict | None:
        """RunTime : temps du cœur pendant la dernière exécution (de la reprise à l'arrêt :
        Go jusqu'à un breakpoint, Go.Till, pas…) et le cumul depuis `chrono_reset()`. Mesuré
        par la cible (SysTick et compteur de ticks sur un Cortex-M0/M0+, DWT_CYCCNT au-delà),
        sans compter les arrêts. None sans mesure (programme de l'hôte, reset entre-temps)."""
        from .chrono import format_duration
        m = self._chrono.stop() if self.s.state == "stopped" else self._chrono.last
        if m is None:
            print("pas de mesure (reprise puis arrêt de la cible : Go, Go.Till, pas)")
            return None
        hz = self.cpu_hz
        sec, total = m.seconds(hz), self._chrono.total / hz if hz else None
        cyc = f"{m.cycles} cycles" if m.cycles is not None else "durée inconnue"
        print(f"{format_duration(sec)} ({cyc}, {m.source}) — cumul {format_duration(total)} en "
              f"{self._chrono.runs} mesure(s)" + (f" — {m.note}" if m.note else ""))
        return {"cycles": m.cycles, "secondes": sec, "source": m.source, "note": m.note,
                "cumul_cycles": self._chrono.total, "cumul_secondes": total}

    def chrono_reset(self) -> None:
        """Remet à zéro le cumul du chrono."""
        self._chrono.reset()

    # terminal
    def terminal(self, quiet: bool = False) -> str:
        """TERM : texte reçu du programme (semihosting, RTT) depuis l'appel précédent. Le RTT
        est relu d'abord (canal mémoire en marche, gdb à l'arrêt)."""
        if self.term.rtt is not None:
            self.term.poll_rtt()
        text = self.term.take_new()
        if not quiet and text:
            print(text, end="" if text.endswith("\n") else "\n")
        return text

    def terminal_send(self, text: str) -> str:
        """TERM : envoie du texte au programme (canal descendant du RTT, ou entrée du
        semihosting) ; rend où il est parti."""
        return self.term.send(text)

    # compteurs de performance
    def counters(self, enable: bool = True, quiet: bool = False) -> dict | None:
        """BMC : compteurs de performance du cœur pendant la dernière exécution (Go jusqu'à un
        breakpoint, pas…) : cycles et compteurs du DWT (CPI, EXC, SLEEP, LSU, FOLD,
        instructions déduites, IPC) ou de la PMU d'un Cortex-M55 (instructions, défauts de
        cache, exceptions). Le premier appel les arme (`enable`), la mesure vient à
        l'exécution suivante. None sans mesure."""
        c = self._counters
        if not c.enabled:
            self._need_stopped()
            msg = c.enable(enable)
            if not quiet:
                print(msg)
            return None
        m = c.stop() if self.s.state == "stopped" else c.last
        if m is None:
            if not quiet:
                print("pas encore de mesure : reprendre puis arrêter la cible (Go, pas)")
            return None
        if not quiet:
            print(f"compteurs ({m.source}) de la dernière exécution" + (f" — {m.note}" if m.note else ""))
            for v in m.values:
                val = "?" if v.delta is None else f"{v.delta:,}".replace(",", " ")
                print(f"  {v.name:<18} {val:>14}{'' if v.exact else ' ≈'}  {v.label}"
                      + (f" ({v.note})" if v.note else ""))
            if m.instructions is not None:
                print(f"  {'instructions':<18} {m.instructions:>14,}".replace(",", " ")
                      + (f"   IPC {m.ipc:.2f}" if m.ipc is not None else ""))
        return m.as_dict()

    def counters_reset(self) -> None:
        """Remet à zéro le cumul des compteurs."""
        self._counters.reset()

    def counters_off(self) -> None:
        """Désarme les compteurs (et coupe les paquets de débordement du DWT)."""
        print(self._counters.enable(False))

    # fautes
    def fault(self, quiet: bool = False) -> dict:
        """Analyse de la faute en cours (cible arrêtée) : cause en clair (CFSR, HFSR, SFSR
        décodés ; sur un Cortex-M0/M0+, déduite de l'instruction fautive), adresse fautive,
        instruction, fonction et ligne fautives, cadre empilé. Affiche le rapport et le rend en
        dictionnaire (`en_faute` faux hors d'un gestionnaire de faute)."""
        from .fault import analyze
        self._need_stopped()
        report = analyze(self.s)
        if not quiet:
            print(report.text())
        return report.as_dict()

    def break_on_fault(self, enable: bool = True) -> str:
        """Arrêt à l'entrée de toute faute (HardFault, MemManage, BusFault, UsageFault) : vector
        catch de DEMCR, sinon breakpoints sur les gestionnaires de la table des vecteurs (QEMU).
        Rend la méthode employée, en clair."""
        from .fault import FaultCatch
        self._need_stopped()
        if self._fault_catch is None:
            self._fault_catch = FaultCatch(self.s)
        if not enable:
            self._fault_catch.disable()
            return "arrêt sur faute retiré"
        return self._fault_catch.enable()

    def fault_catch_method(self) -> str | None:
        """Méthode de l'arrêt sur faute en place (« vector catch », « breakpoints »), ou None."""
        return None if self._fault_catch is None else self._fault_catch.method

    def _need_stopped(self) -> None:
        if self.s.state != "stopped":
            raise RuntimeError("cible en marche : arrêter d'abord (halt)")

    @property
    def config(self) -> dict | None:
        """Configuration de la session (menu Configuration, `--config`) : le contenu du YAML
        (sections `cible`, `serveur`, `os`, `script`, `couverture`, `trace`), une copie en
        lecture seule ; None sans configuration."""
        import copy
        return copy.deepcopy(self._config)

    def program(self, elf: str | None = None, verify: bool = True, quiet: bool = False):
        """FLASH.ReProgram : programme `elf` dans la cible (par défaut l'ELF courant, par ex.
        après une recompilation), vérifie, puis reset : la cible est arrêtée au vecteur de
        reset. Retourne le `ProgramResult`."""
        import os
        elf = os.path.abspath(elf) if elf else self.s.elf
        if not elf:
            raise RuntimeError("aucun ELF à programmer")
        res = self.s.program(elf, verify)
        from .memtools import is_image
        if not is_image(elf):                   # HEX, binaire : les symboles ne changent pas
            self.symbols = None                 # rechargés à la demande
            self._rtos = None
            if self.on_program is not None:
                self.on_program(elf)
        if not quiet:
            print(res.summary())
        return res

    # trace et profilage
    def _symbols(self):
        if self.symbols is None and self.s.elf:
            from .symbols import SymbolTable
            self.symbols = SymbolTable(self.s.elf, self.s.extra_symbols)
        return self.symbols

    def profile(self, seconds: float = 1.0, per_task: bool = False, show: int = 15):
        """PERF : profil statistique pendant `seconds`. La cible est relancée si elle était
        arrêtée (et arrêtée à nouveau à la fin). Échantillons SWO si une capture est en
        cours, sinon PC lu en marche par le canal live. Retourne le `Profile`."""
        import time
        from ..trace.profile import PcSampler, Profile
        prof = Profile(self._symbols())
        use_swo = self.trace is not None and self.trace.active
        sampler = None
        was_stopped = self.s.state == "stopped"
        if use_swo:
            self.trace.set_profile(prof)
        else:
            if self.live is None:
                raise RuntimeError("ni capture SWO ni canal live : impossible d'échantillonner le PC")
            task_of = None
            if per_task:
                if self._rtos is None and was_stopped:
                    self._rtos = freertos.FreeRtosLayout.from_session(self.s)
                task_of = freertos.CurrentTaskReader(self._rtos) if self._rtos else None
            sampler = PcSampler(self.live, prof, lambda: self.s.state == "running", task_of=task_of)
            sampler.start()
        try:
            if was_stopped:
                self.s.go()
            time.sleep(seconds)
            if was_stopped:
                self.s.halt()
        finally:
            if sampler is not None:
                sampler.stop()
            if use_swo:
                self.trace.set_profile(None)
        if show:
            print(prof.report(show))
            for r in prof.task_rows():
                print(f"  tâche {r.name:<12} {r.percent:5.1f} %")
        return prof

    # trace d'instructions ETM
    def _etm(self):
        if self.trace is None or self.trace.etm is None:
            raise RuntimeError("pas de trace ETM (--etm-sim sous QEMU ; --etm-config --etm-port swo, "
                               "--trace/--trace-file sur matériel)")
        self.trace.wait_etm()
        return self.trace.etm

    def mtb(self, n: int = 20):
        """Paquets bruts de la MTB lus au dernier arrêt (les `n` derniers), symbolisés :
        S = début de session, A = exception ou PC changé par le débogueur."""
        from ..trace.mtb import is_exc_return
        tm = self.trace
        src = (tm.mtb or tm.mtb_sim) if tm is not None else None
        if src is None:
            raise RuntimeError("pas de MTB (--mtb sur la cible, --mtb-sim sous QEMU)")
        tm.wait_etm()
        sym = self._symbols()

        def where(a: int) -> str:
            if is_exc_return(a):
                return "EXC_RETURN"
            f = sym.function_at(a) if sym else None
            le = sym.line_at(a) if sym else None
            loc = f"  {le.file.rsplit('/', 1)[-1]}:{le.line}" if le and f else ""
            return f"{f.name if f else '?'}{loc}"
        rows = src.last_packets[-n:]
        for p in rows:
            print(f"  {'S' if p.s else ' '}{'A' if p.a else ' '} {p.src:08x} {where(p.src):<34} → "
                  f"{p.dst:08x} {where(p.dst)}")
        return rows

    def mtb_stats(self) -> dict:
        """Compteurs de la MTB : paquets, instructions, arrêts AUTOHALT, débordements, erreurs."""
        tm = self.trace
        src = (tm.mtb or tm.mtb_sim) if tm is not None else None
        if src is None:
            raise RuntimeError("pas de MTB (--mtb sur la cible, --mtb-sim sous QEMU)")
        tm.wait_etm()
        st = src.decoder.stats
        out = {"paquets": st.packets, "instructions": st.instructions, "exceptions": st.exceptions,
               "arrêts AUTOHALT": src.autohalts, "débordements": src.wraps, "erreurs": st.errors}
        for k, v in out.items():
            print(f"  {k:<18} {v}")
        return out

    def mtb_probe(self):
        """Composants CoreSight de la cible, tables ROM du cœur et des fabricants connus
        (où se trouve une MTB) ; voir aussi `coresight()`."""
        from ..trace.coresight import CORE_ROOTS, ROM_ROOTS, describe, walk
        comps = walk(self.s.read_memory, CORE_ROOTS + ROM_ROOTS[1:])
        for line in describe(comps):
            print(f"  {line}")
        return comps

    # puce du CMSIS-Pack (SYStem.CPU)
    def chip_rows(self):
        """(titre, sections) de la puce pour la vue Système : le pack, puis ce que la cible
        répond (cœur par CPUID, accord avec le pack). None sans puce ni cible."""
        rows = []
        title = self.chip.title if self.chip is not None else "aucune puce décrite par un CMSIS-Pack (--device)"
        if self.chip is not None:
            rows = self.chip.rows()
        seen = []
        tm = self.trace
        if tm is not None and tm.core is not None:
            ok = ""
            if self.chip is not None and self.chip.core:
                ok = ("conforme au pack" if self.chip.core.replace("Cortex-", "").upper()
                      == (tm.core_name or "").replace("Cortex-", "").upper() else f"le pack dit {self.chip.core}")
            seen.append(("cœur (CPUID)", tm.core_name or "?", ok))
            if tm.components:
                etm = next((c for c in tm.components if c.kind == "ETM"), None)
                seen.append(("ETM (table ROM)", etm.name if etm else "absent", ""))
        if seen:
            rows.append(("Lu sur la cible", seen))
        return (title, rows) if rows else None

    def chip_info(self):
        """La puce décrite par son CMSIS-Pack (--pack, --device) : cœur, horloge, mémoires,
        algorithmes de flash, SVD ; et le cœur lu sur la cible. Rend le `Device` du pack."""
        info = self.chip_rows()
        if info is None:
            print("aucune puce décrite par un CMSIS-Pack (--device, --pack)")
            return None
        title, rows = info
        print(title)
        for section, items in rows:
            print(f"  {section}")
            for name, value, detail in items:
                print(f"    {name:<22} {value:<26} {detail}")
        return self.chip

    # chargeurs de flash (FLASH.Create, FLASH.Erase)
    def flash_loader(self, spec: str, ram=None, clock: int | None = None):
        """Ajoute un algorithme de flash à la session : FLM d'un CMSIS-Pack ou chargeur externe
        .stldr (un nom seul est cherché dans STM32CubeProgrammer). `ram` : (adresse, taille) ou
        « 0x20000000:0x4000 », obligatoire pour un FLM. `program()` écrit ensuite la mémoire
        de l'algorithme par lui. Rend le `FlashLoader`."""
        import os

        from .flashalgo import FlashLoader, find_external_loader, load_algo, parse_ram
        path = spec if os.path.isfile(spec) else find_external_loader(spec)
        if path is None:
            raise FileNotFoundError(f"chargeur de flash introuvable : {spec}")
        if isinstance(ram, str):
            ram = parse_ram(ram)
        loader = FlashLoader(self.s, load_algo(path), ram, clock or self.cpu_hz)
        self.s.flash_loaders = [ld for ld in self.s.flash_loaders
                                if ld.device.start != loader.device.start] + [loader]
        print(f"chargeur de flash : {loader.describe()}")
        return loader

    def flash_info(self) -> list:
        """Chargeurs de flash de la session : mémoire, page, secteurs, RAM de l'algorithme."""
        loaders = self.s.flash_loaders
        if not loaders:
            print("aucun chargeur de flash : la flash passe par le serveur gdb (load)")
        for ld in loaders:
            d, pl = ld.device, ld.placement
            sectors = d.sector_list()
            sizes = sorted({n for _a, n in sectors})
            print(f"{ld.describe()}\n  secteurs : {len(sectors)} ({', '.join(f'{n // 1024} Ko' if n >= 1024 else f'{n} o' for n in sizes)}), "
                  f"valeur effacée {d.erased:#04x} ; algorithme en RAM {pl.image[0][0]:#010x}, "
                  f"pile {pl.sp:#010x}, tampon {pl.buffer:#010x}+{pl.buffer_size:#x}")
        return loaders

    def flash_erase(self, addr=None, n: int | None = None) -> int:
        """FLASH.Erase : efface les secteurs de [addr, addr+n) par le chargeur qui couvre `addr`
        (sans argument : toute la mémoire de chaque chargeur). Cible arrêtée ; rend le nombre de
        secteurs effacés."""
        self._need_stopped()
        loaders = self.s.flash_loaders
        if not loaders:
            raise RuntimeError("aucun chargeur de flash (--flash-loader, gdbtrace.flash_loader)")
        total = 0
        if addr is None:
            for ld in loaders:
                total += ld.erase()
        else:
            a, n = self._where(addr, n)
            ld = next((x for x in loaders if x.device.covers(a)), None)
            if ld is None:
                raise ValueError(f"{a:#x} : hors de la mémoire des chargeurs de flash")
            total = ld.erase(a, a + (n or 1))
        print(f"{total} secteur(s) effacé(s)")
        return total

    def mpu(self, quiet: bool = False, nonsecure: bool = False):
        """MPU du cœur (cible arrêtée) : régions décodées (début, fin, sous-régions, accès
        privilégié et non privilégié, XN, type mémoire), ARMv7-M ou ARMv8-M. Rend un `MpuState`
        (`regions`, `enabled`, `summary()`, `region_at(adresse)`). TrustZone : la MPU de l'état
        courant, ou la non sécurisée (`nonsecure`, lue par l'alias depuis l'état sécurisé)."""
        from .mpu import session_mpu
        self._need_stopped()
        state = session_mpu(self.s, nonsecure=nonsecure)
        if not quiet:
            print(state.summary())
            for r in state.regions:
                on, base, end, size, subs, access, xn, mem = r.row()
                print(f"  {r.number:>2} {on:<3} {base}-{end} {size:>7}  {access:<36} {xn:<6} {mem}"
                      + (f"  sous-régions {subs}" if subs else ""))
        return state

    def cores(self, quiet: bool = False) -> list:
        """Cœurs de la session (--core) : nom, source (la session, un processus du même
        serveur, un autre serveur gdb), état ; « * » : le cœur choisi. Rend la liste des `Core`
        (`name`, `session`, `thread`, `source`, `elf`) ; un seul cœur sans --core."""
        from .cores import Core
        cs = self.core_set
        if cs is not None and cs.current is not None and cs.current.session.state == "stopped":
            cs.follow_stop(cs.current.session)      # même serveur : le cœur arrêté par gdb
            self.s = cs.current.session
        cores = cs.cores if cs is not None else [Core("cœur 0", self.s, source="session",
                                                      elf=self.s.elf or "")]
        if not quiet:
            for c in cores:
                mark = "*" if cs is None or c is cs.current else " "
                print(f"{mark} {c.name:<12} {c.state():<10} {c.source}"
                      + (f"  ({os.path.basename(c.elf)})" if c.elf else ""))
        return cores

    def core(self, name) -> list:
        """Choisit le cœur `name` (ou son rang, 0 : celui de la session) : registres, pile,
        variables, pas à pas, breakpoints, Go et Break portent désormais sur lui. Cœurs d'un
        même serveur : Go les relance tous, et un arrêt les arrête tous. La trace, le chrono et
        les compteurs restent ceux du cœur de la session. Rend la pile du cœur."""
        if self.core_set is None:
            raise RuntimeError("un seul cœur : --core (ou cible.coeurs) en déclare d'autres")
        c = self.core_set.select(str(name))
        self.s = c.session
        print(f"cœur {c.name} ({c.source}) : {c.state()}")
        if self.s.state == "stopped":
            self.bt()
            return self.s.backtrace()
        return []

    def cti_sync(self, enable: bool = True):
        """Arrêt synchrone des cœurs par leurs CTI (cibles arrêtées) : l'arrêt d'un cœur
        demande l'arrêt des autres en quelques cycles (canal 0 de la matrice), acquitté avant
        chaque reprise. CTI trouvés dans les tables ROM ; aucun sous QEMU. `enable` False :
        coupé. Rend un `CtiSync` (`ctis` : adresses par cœur, `summary()`)."""
        from .cti import setup
        cores = [(c.name, c.session) for c in self.cores(quiet=True)]
        if any(s.state != "stopped" for _n, s in cores):
            raise RuntimeError("cti_sync : arrêter tous les cœurs d'abord (halt_all())")
        res = setup(cores, enable)
        print(res.summary())
        return res

    def go_all(self) -> None:
        """Relance tous les cœurs (chaque session de cœur arrêtée)."""
        if self.core_set is None:
            self.go()
            return
        self.core_set.go_all()

    def halt_all(self) -> None:
        """Arrête tous les cœurs en marche."""
        if self.core_set is None:
            self.halt()
            return
        self.core_set.halt_all()

    def security(self, quiet: bool = False):
        """TrustZone (ARMv8-M) : état de sécurité du cœur arrêté (sécurisé ou non, mode, pile en
        service, d'après DSCSR.CDS ou la pile) et ses registres bancaires (msp_s, psp_ns,
        control_s…). Rend un `SecurityState` (`secure`, `label` « S »/« NS », `text()`,
        `banked`), ou None : le cœur n'a pas l'extension de sécurité."""
        from .security import security_state
        self._need_stopped()
        state = security_state(self.s)
        if not quiet:
            if state is None:
                print("pas d'extension de sécurité (TrustZone) sur ce cœur")
            else:
                print(f"état : {state.text()}")
                for world, suffix in (("sécurisé", "_s"), ("non sécurisé", "_ns")):
                    vals = [f"{k[:-len(suffix)]} {v:#010x}" for k, v in state.banked.items()
                            if k.endswith(suffix)]
                    print(f"  {world:<13} " + "  ".join(vals))
        return state

    def sau(self, quiet: bool = False):
        """SAU (TrustZone) : régions décodées (début, fin, NS ou NSC), activé ou non, ALLNS ;
        lisible depuis l'état sécurisé seulement. Rend un `SauState` (`regions`, `enabled`,
        `summary()`, `attribution(adresse)` : « S », « NS », « NSC »)."""
        from .security import session_sau
        self._need_stopped()
        state = session_sau(self.s)
        if not quiet:
            print(state.summary())
            for r in state.regions:
                on, base, end, size, attr = r.row()
                print(f"  {r.number:>2} {on:<3} {base}-{end} {size:>7}  {attr}")
        return state

    def stack(self, quiet: bool = False) -> list:
        """Pile d'appels complète (cible arrêtée), à travers les mondes sécurisé et non
        sécurisé d'un cœur TrustZone (gdb s'arrête au changement de monde) : liste de
        `WorldFrame` (`frame`, `world` « S »/« NS »/None, `note`). Sans TrustZone : la pile de
        gdb, monde None."""
        from .security import world_backtrace
        self._need_stopped()
        frames = world_backtrace(self.s, secure_read=self._secure_read())
        if not quiet:
            self._print_stack(frames)
        return frames

    def _secure_read(self):
        """Lecture de la mémoire sécurisée quand gdb la refuse (QEMU : espace physique)."""
        return getattr(self.live, "read_physical", None)

    @staticmethod
    def _print_stack(frames) -> None:
        for i, wf in enumerate(frames):
            f = wf.frame
            loc = f"{f.file}:{f.line}" if f.file else ""
            world = f"[{wf.world}] " if wf.world else ""
            note = f"  ({wf.note})" if wf.note else ""
            print(f"#{i} {world}{f.pc:08x} {f.func or '??'} {loc}{note}")

    def coresight(self, refresh: bool = True) -> list:
        """Composants CoreSight de la puce, par ses tables ROM (cible arrêtée) : SCS, DWT, FPB,
        ITM, TPIU, ETM (et sa version), CTI, MTB, PMU… Rend la liste (`Component` : `addr`,
        `name`, `kind`, `part`, `designer`, `depth`) ; vide sans table (QEMU)."""
        tm = self.trace
        if refresh or tm is None:
            self._need_stopped()
            from ..trace.coresight import walk
            comps = walk(self.s.read_memory)
            if tm is not None:
                tm.components = comps
        else:
            comps = tm.components
        if not comps:
            print("pas de table ROM CoreSight lisible (QEMU n'en a pas)")
            return comps
        for c in comps:
            print(f"  {'  ' * c.depth}{c.addr:#010x}  {c.name:<34} {c.kind or '?':<8} "
                  f"pièce {c.part:#05x}  {c.designer_name}  rév. {c.revision}")
        return comps

    def trace_list(self, n: int = 30):
        """Trace.List : les `n` dernières instructions exécutées (et exceptions)."""
        from ..trace.etm.qemu_log import CycleMark, ExcEntry, ExcReturn, TimeMark, TraceGap
        from ..trace.itm import exception_name
        etm = self._etm()
        events = etm.last(n)
        sym = self._symbols()
        for i, e in enumerate(events):
            idx = i - len(events)
            if isinstance(e, ExcEntry):
                print(f"{idx:>6}  ── exception {exception_name(e.number)} ({e.number}) → {e.handler:08x}")
            elif isinstance(e, ExcReturn):
                print(f"{idx:>6}  ── retour d'exception")
            elif isinstance(e, TraceGap):
                print(f"{idx:>6}  ── trace interrompue")
            elif isinstance(e, TimeMark):
                print(f"{idx:>6}  ── horodatage {e.value}")
            elif isinstance(e, CycleMark):
                continue
            else:
                ins = etm.image.instr(e.addr)
                f = sym.function_at(e.addr) if sym else None
                le = sym.line_at(e.addr) if sym else None
                loc = f"{le.file.rsplit('/', 1)[-1]}:{le.line}" if le and f else ""
                print(f"{idx:>6}  {e.addr:08x}  {f.name if f else '':<22} {ins.text if ins else '?':<28} {loc}")
        return events

    def trace_find(self, query: str, n: int = 50_000, show: int = 20) -> list[dict]:
        """Trace.FindAll : dans les `n` derniers événements de la trace, les lignes qui
        répondent à `query` : adresse (0x…), nom de fonction (chaque entrée), « exception » ou
        « exception SysTick » / numéro, ou texte (instruction, fonction, fichier). Affiche les
        `show` premières ; rend [{index, type, adresse, fonction, instruction, fichier, ligne}]."""
        from ..trace.etm.listing import find_rows, listing_rows
        etm = self._etm()
        rows = listing_rows(etm.last(n), etm.image, self._symbols())
        hits = [rows[i] for i in find_rows(rows, query)]
        for r in hits[:show]:
            where = f"  {r.file.rsplit('/', 1)[-1]}:{r.line}" if r.file else ""
            addr = f"{r.addr:08x}" if r.addr is not None else " " * 8
            print(f"{r.index:>7}  {addr}  {r.func:<22} {r.text}{where}")
        print(f"{len(hits)} occurrence(s) sur {len(rows)} lignes")
        return [r.as_dict() for r in hits]

    def trace_export(self, path: str, n: int = 100_000) -> int:
        """Exporte les `n` derniers événements de la trace en CSV (index, type, adresse,
        fonction, instruction, fichier, ligne) ; rend le nombre de lignes."""
        from ..trace.etm.listing import export_csv, listing_rows
        etm = self._etm()
        count = export_csv(listing_rows(etm.last(n), etm.image, self._symbols()), path)
        print(f"{count} lignes de trace écrites dans {path}")
        return count

    def coverage(self, top: int | None = 20):
        """Couverture de code par fonction d'après la trace ETM (voir aussi `gdbtrace.cov`)."""
        return self.cov.list(top)

    def calls(self, n: int = 40):
        """Flot des appels, retours et exceptions (dernières transitions)."""
        rows = self._etm().call_flow()[-n:]
        icons = {"call": "→", "return": "←", "exception": "⚡", "exc-return": "↩", "start": "•", "branch": "↷",
                 "gap": "┄"}
        labels = {"exc-return": "retour d'exception", "gap": "trace interrompue"}
        for r in rows:
            label = labels.get(r.kind, r.func)
            print(f"{r.index:>8}  {'  ' * r.depth}{icons.get(r.kind, '')} {label}")
        return rows

    def timing(self, top: int | None = 20, unit: str = "us", n: int | None = None, kind: str | None = None):
        """Trace.STATistic.Func : durées par fonction et par exception sur les `n` derniers
        événements de la trace ETM (tout l'historique par défaut). `unit` : "us" ou "cycles" ;
        `kind` : "function" ou "exception" pour filtrer. Retourne le `TimingResult`."""
        from ..trace.timing import TimingAnalyzer
        etm = self._etm()
        events = etm.last(n or etm.history.maxlen or 200_000)
        r = TimingAnalyzer(etm.image, self._symbols()).analyze(events)
        conv, u = self._time_unit(unit)
        print(f"{conv(r.total):,.2f} {u} analysés — durées {etm.timing}"
              + (f" — {r.context_switches} changements de tâche" if r.context_switches else ""))
        print(f"  {'fonction':<24} {'appels':>7} {'partiels':>8} {'brut':>12} {'%':>6} "
              f"{'min':>10} {'max':>10} {'moyenne':>10} {'net':>12}")
        fmt = lambda c: "" if c is None else f"{conv(c):,.2f}"  # noqa: E731
        for st in r.rows(kind)[:top]:
            pct = 100 * st.gross / r.total if r.total else 0.0
            print(f"  {st.name:<24} {st.calls:>7} {st.partial or '':>8} {fmt(st.gross):>12} {pct:>6.1f} "
                  f"{fmt(st.min):>10} {fmt(st.max if st.calls else None):>10} {fmt(st.avg):>10} "
                  f"{fmt(st.net):>12}")
        return r

    def distribution(self, name: str, bins: int = 20, unit: str = "us", log: bool | None = None,
                     n: int | None = None, show: bool = True):
        """Trace.STATistic.DISTribution : distribution des durées de `name` — fonction ou
        exception (« SysTick ») de la trace d'instructions, tâche (tranches du temps des
        tâches), ou variable tracée / échantillonnée (ses valeurs). Histogramme en `bins`
        classes (logarithmiques si `log`, ou si les durées s'étalent sur plus de deux
        décades), centiles p50, p90, p99, et les appels les plus longs avec leur position dans
        la trace (index de `trace_list`, -1 : le dernier). Rend une `Distribution`."""
        from ..trace.datatrace import value_distribution
        from ..trace.timing import TimingAnalyzer
        conv, u = self._time_unit(unit)
        scale = conv(1.0)
        d = None
        tm = self.trace
        if tm is not None and tm.etm is not None:
            etm = self._etm()                   # attend la fin du décodage
            if etm.history:
                r = TimingAnalyzer(etm.image, self._symbols()).analyze(
                    etm.last(n or etm.history.maxlen or 200_000))
                try:
                    d = r.distribution(name, bins, log, scale, u)
                except KeyError:
                    d = None
        if d is None and tm is not None:
            tt = tm.tasktrace
            self._poll_task_log()
            self._task_namer()
            try:
                d = tt.distribution(name, bins, log, scale, u)
            except KeyError:
                d = None
            if d is None:
                var = next((v for v in tm.datatrace.vars.values() if v.expr == name), None)
                if var is not None:
                    d = value_distribution(var, bins, log)
        if d is None:
            raise KeyError(f"« {name} » : ni fonction ou exception de la trace, ni tâche, ni "
                           "variable tracée")
        if show:
            print(d.summary())
            peak = max((b.count for b in d.bins), default=0) or 1
            for b in d.bins:
                bar = "█" * round(30 * b.count / peak)
                print(f"  {b.low:>12,.2f} – {b.high:<12,.2f} {b.count:>7}  {bar}".replace(",", " "))
            if d.longest and d.unit:
                print("  plus longs : " + ", ".join(
                    f"{v:,.2f} {d.unit}".replace(",", " ") + (f" (index {w})" if w is not None else "")
                    for v, w in d.longest[:5]))
        return d

    def call_tree(self, depth: int = 4, min_pct: float = 1.0, unit: str = "us",
                  n: int | None = None) -> list[dict]:
        """Trace.STATistic.TREE : durées par chemin d'appel (une exception est la racine de
        son arbre), jusqu'à `depth` niveaux, sans les nœuds de moins de `min_pct` % du temps
        analysé. Rend des `dict` imbriqués : nom, appels, partiels, brut, net, min, max,
        moyenne (cycles), enfants."""
        from ..trace.timing import TimingAnalyzer
        etm = self._etm()
        r = TimingAnalyzer(etm.image, self._symbols()).analyze(etm.last(n or etm.history.maxlen or 200_000))
        conv, u = self._time_unit(unit)
        print(f"{conv(r.total):,.2f} {u} analysés — durées {etm.timing} ; brut, net en {u}")

        def walk(path: tuple, level: int) -> list[dict]:
            out = []
            for child, st in r.children(path):
                pct = 100 * st.gross / r.total if r.total else 0.0
                if pct < min_pct:
                    continue
                print(f"  {'  ' * level}{st.name:<{max(8, 30 - 2 * level)}} {st.calls:>6} appel(s)"
                      f"{f' +{st.partial} partiel(s)' if st.partial else ''}  brut {conv(st.gross):,.2f}"
                      f" ({pct:.1f} %)  net {conv(st.net):,.2f}")
                node = {"nom": st.name, "appels": st.calls, "partiels": st.partial, "brut": st.gross,
                        "net": st.net, "min": st.min, "max": st.max if st.calls else None,
                        "moyenne": st.avg, "enfants": []}
                if level + 1 < depth:
                    node["enfants"] = walk(child, level + 1)
                out.append(node)
            return out
        return walk((), 0)

    def irq_stats(self, unit: str = "us", n: int | None = None) -> list[dict]:
        """Trace.STATistic.InterruptIsr : par exception, entrées, durée (sans les exceptions
        imbriquées) min/moy/max, période entre deux entrées et gigue, préemptions, entrées
        enchaînées. Depuis la trace d'instructions (ETM, MTB) si elle existe, sinon depuis la
        trace des exceptions de l'ITM (unités d'horodatage ITM)."""
        from ..trace.irqstats import sorted_rows
        etm = self.trace.etm if self.trace is not None else None
        if etm is not None and etm.history:
            from ..trace.timing import TimingAnalyzer
            r = TimingAnalyzer(etm.image, self._symbols()).analyze(etm.last(n or etm.history.maxlen or 200_000))
            rows = sorted_rows(r.irq)
            conv, u = self._time_unit(unit)
            src = f"trace d'instructions, durées {etm.timing}"
        elif self.trace is not None and self.trace.exceptions.rows():
            rows = self.trace.exceptions.rows()
            conv, u = (lambda c: c), "ticks ITM"
            src = "trace des exceptions de l'ITM"
        else:
            print("pas de trace : ni trace d'instructions (ETM, MTB) ni trace des exceptions de l'ITM")
            return []
        fmt = lambda c: "" if c is None else f"{conv(c):,.2f}"  # noqa: E731
        print(f"exceptions ({src}) ; durées et périodes en {u}")
        print(f"  {'exception':<14} {'entrées':>7} {'durée min':>10} {'moy':>10} {'max':>10} "
              f"{'période moy':>12} {'gigue':>10} {'préemptée':>9} {'préempte':>8} {'enchaînées':>10}")
        for st in rows:
            print(f"  {st.name.strip('[]'):<14} {st.count:>7} {fmt(st.min):>10} {fmt(st.avg):>10} "
                  f"{fmt(st.max if st.complete else None):>10} {fmt(st.period_avg):>12} {fmt(st.jitter):>10} "
                  f"{st.preempted:>9} {st.preempting:>8} {st.tail:>10}")
        return [st.as_dict() for st in rows]

    def timeline(self, n: int = 30, unit: str = "us", min_duration: float = 0.0):
        """Trace.Chart.Func en texte : les `n` derniers appels (début, durée, profondeur),
        en ignorant ceux plus courts que `min_duration` (dans l'unité choisie)."""
        from ..trace.timing import TimingAnalyzer
        etm = self._etm()
        r = TimingAnalyzer(etm.image, self._symbols()).analyze(etm.last(etm.history.maxlen or 200_000))
        conv, u = self._time_unit(unit)
        rows = [iv for iv in r.intervals if conv(iv.duration) >= min_duration]
        rows.sort(key=lambda iv: (iv.start, iv.depth))
        for iv in rows[-n:]:
            mark = " (partiel)" if iv.partial_start or iv.partial_end else ""
            print(f"  {conv(iv.start):>12,.2f}  {conv(iv.duration):>10,.2f} {u}  {'  ' * iv.depth}{iv.name}{mark}")
        return rows[-n:]

    def _time_unit(self, unit: str):
        if unit in ("us", "µs"):
            return (lambda c: c * 1e6 / self.cpu_hz), "µs"
        if unit == "cycles":
            return (lambda c: c), "cycles"
        raise ValueError(f"unité inconnue : {unit!r} (us ou cycles)")

    def etm_config(self, **kw) -> None:
        """Programme l'ETM et le TPIU parallèle (port_width=4, trace_id=2, stm32=False...)."""
        from ..trace.etm.config import EtmConfig
        cfg = EtmConfig(**kw)
        cfg.apply(self.s)
        print(cfg.summary())

    # trace de données du DWT (Var.TRACE)
    def var_trace(self, expr: str, access: str = "write", pc: bool = False) -> None:
        """Var.TRACE : la valeur de `expr` (globale ou statique, 1, 2 ou 4 octets) est émise par
        le DWT à chaque accès (`access` : write, read, rw ; `pc` : avec le PC de l'instruction),
        cible en marche, par la capture SWO ou de trace. Cible arrêtée pour programmer."""
        if self.trace is None:
            raise RuntimeError("pas de gestionnaire de trace")
        for line in self.trace.trace_variable(self.s, expr, access, pc, cpu_hz=self.cpu_hz):
            print(line)

    def snoop(self, expr: str, period_ms: int = 10) -> None:
        """SNOOPer : `expr` (globale ou statique, 1, 2, 4 ou 8 octets) lue toutes les
        `period_ms` ms par le canal mémoire pendant que la cible tourne, sans trace ni ITM
        (Cortex-M0/M0+, QEMU, carte sans capture SWO). Échantillons : `var_samples(expr)`, vue
        Variables tracées. Cible arrêtée pour l'ajouter ; période commune à toutes."""
        if self.trace is None:
            raise RuntimeError("pas de gestionnaire de trace")
        self._need_stopped()
        for line in self.trace.snoop(self.s, expr, period_ms):
            print(line)

    def snoop_stop(self, expr: str | None = None) -> None:
        """Arrête l'échantillonnage de `expr` (de toutes sans argument)."""
        if self.trace is None:
            raise RuntimeError("pas de gestionnaire de trace")
        for line in self.trace.unsnoop(expr):
            print(line)

    def var_trace_stop(self, expr: str | None = None) -> None:
        """Arrête la trace de `expr` (de toutes les variables sans argument)."""
        if self.trace is None:
            raise RuntimeError("pas de gestionnaire de trace")
        for line in self.trace.untrace_variable(self.s, expr):
            print(line)

    def var_samples(self, expr: str, n: int | None = 20, fmt: str = "natural") -> list:
        """Échantillons de la trace de données de `expr`, les `n` derniers (tous si None) :
        temps, accès, valeur au format `fmt`, et le PC de l'instruction s'il est tracé."""
        if self.trace is None:
            raise RuntimeError("pas de gestionnaire de trace")
        var = next((v for v in self.trace.datatrace.vars.values() if v.expr == expr), None)
        if var is None:
            raise KeyError(f"« {expr} » n'est pas tracée (gdbtrace.var_trace)")
        rows = list(var.samples)
        rows = rows[-n:] if n else rows
        sym = self._symbols()
        for smp in rows:
            where = ""
            if smp.pc is not None:
                f = sym.function_at(smp.pc) if sym else None
                where = f"  {smp.pc:#010x} {f.name if f else ''}"
            print(f"  {smp.t * 1e6:>14,.1f} µs  {smp.kind:<6} {var.text(smp, fmt)}{where}".replace(",", " "))
        return rows

    # temps par tâche (changements de tâche tracés par l'ITM)
    def _task_namer(self) -> None:
        """Noms des tâches relus dans les TCB (disposition FreeRTOS, calculée une fois cible
        arrêtée ; lectures par gdb à l'arrêt, par le canal live en marche)."""
        tt = self.trace.tasktrace
        if tt.namer is not None:
            return
        if self._rtos is None and self.s.state == "stopped":
            try:
                self._rtos = freertos.FreeRtosLayout.from_session(self.s)
            except GdbError:
                return                              # pas de FreeRTOS : adresses des TCB
        if self._rtos is not None:
            from ..trace.tasktrace import freertos_namer
            tt.namer = freertos_namer(self._rtos, self.mem)

    def _poll_task_log(self) -> None:
        """Journal des tâches en RAM (s'il fait foi) : nouvelles entrées lues, par gdb à
        l'arrêt, par le canal live en marche."""
        tm = self.trace
        if tm is None or not tm.task_log_active:
            return
        if self.s.state == "stopped":
            tm.poll_task_log(self.s.read_memory)
        elif self.live is not None:
            tm.poll_task_log(self.live.read)

    def task_times(self, top: int | None = 20, unit: str = "us"):
        """Temps par tâche : le firmware signale chaque changement de tâche (FreeRTOS :
        traceTASK_SWITCHED_IN, voir examples/freertos), par l'ITM (port 31, horodatage ITM)
        ou, sans ITM ni capture SWO, dans un anneau en RAM (`gdbtrace_task_log`, horodatage
        SysTick ; temps net = brut). Activations, temps brut, net (sans les exceptions), part
        du temps, tranches maximale et moyenne. Retourne les lignes (`TaskTime`)."""
        if self.trace is None:
            raise RuntimeError("pas de gestionnaire de trace")
        tt = self.trace.tasktrace
        self._poll_task_log()
        self._task_namer()
        rows = tt.rows()
        conv, u = self._time_unit(unit)
        if not rows:
            print("aucun changement de tâche reçu : traceTASK_SWITCHED_IN → port ITM 31 avec une "
                  "capture SWO ou de trace, ou journal en RAM gdbtrace_task_log (examples/freertos)")
            return rows
        if self.trace.task_log_active:
            lost = self.trace.task_log.lost
            print("journal des tâches en RAM (SysTick) : sans trace des exceptions, temps net = brut"
                  + (f" ; {lost} changements perdus (anneau dépassé)" if lost else ""))
        if not tt.timestamps:
            print("horodatage ITM absent : durées inconnues (activer l'horodatage de l'ITM)")
        span = tt.span or 1
        print(f"{conv(tt.span):,.2f} {u} mesurés, {tt.switches} changements de tâche, "
              f"{conv(tt.exception_cycles):,.2f} {u} en exceptions".replace(",", " "))
        print(f"  {'tâche':<16} {'activations':>11} {'brut':>12} {'net':>12} {'%':>6} "
              f"{'tranche max':>12} {'moyenne':>10}")
        for t in rows[:top]:
            print(f"  {t.name:<16} {t.activations:>11} {conv(t.gross):>12,.2f} {conv(t.net):>12,.2f} "
                  f"{100 * t.gross / span:>6.1f} {conv(t.max_slice):>12,.2f} {conv(t.avg_slice):>10,.2f}"
                  .replace(",", " "))
        return rows

    def task_timeline(self, n: int = 30, unit: str = "us"):
        """Les `n` dernières tranches d'exécution : début, durée, tâche."""
        if self.trace is None:
            raise RuntimeError("pas de gestionnaire de trace")
        tt = self.trace.tasktrace
        self._poll_task_log()
        self._task_namer()
        conv, u = self._time_unit(unit)
        rows = tt.timeline(n)
        t0 = rows[0].start if rows else 0
        for sl in rows:
            print(f"  {conv(sl.start - t0):>12,.2f}  {conv(sl.end - sl.start):>10,.2f} {u}  "
                  f"{tt.name(sl.tcb)}".replace(",", " "))
        return rows

    def etm_filter(self, *ranges: str, start=(), stop=()) -> None:
        """Filtre de la trace ETM : plages (`"fib"`, `"main.c"`, `"0x100-0x200"`), lieux de
        démarrage et d'arrêt (`start="fib"`, `stop=["main.c:59"]`) ; sans argument : tout le
        code. ETM réel reprogrammé (cible arrêtée), ETM simulé filtré ; la couverture se limite
        aux plages."""
        if self.trace is None:
            raise RuntimeError("pas de gestionnaire de trace")
        start = [start] if isinstance(start, str) else list(start)
        stop = [stop] if isinstance(stop, str) else list(stop)
        for line in self.trace.set_etm_filter(list(ranges), start, stop, self.s):
            print(line)

    def etm_swo(self, cpu_hz: int | None = None, swo_hz: int = 2_000_000, stall: bool = False,
                stm32: bool = False, port: int = 3443) -> None:
        """ETM par SWO (cible arrêtée) : TPIU formaté, capture servie par OpenOCD sur `port`,
        ETM programmé ; comme --etm-config --etm-port swo."""
        if self.trace is None:
            raise RuntimeError("pas de gestionnaire de trace")
        for line in self.trace.start_etm_swo(self.s, self.s.target, cpu_hz or self.cpu_hz, swo_hz,
                                             stall=stall, stm32=stm32, port=port):
            print(line)

    def etm_etr(self, buffer: tuple[int, int] | None = None, stall: bool = False,
                cycle_accurate: bool = False, timestamps: bool = False) -> None:
        """ETM écrit en RAM par l'ETR de la puce (cible arrêtée ; STM32N6), lu à chaque arrêt ;
        comme --etm-config --etm-port etr. `buffer` : (adresse, taille), sinon le tampon
        réservé par le firmware (`__etr_buffer__`)."""
        if self.trace is None:
            raise RuntimeError("pas de gestionnaire de trace")
        for line in self.trace.start_etm_tmc(self.s, buffer, stall=stall, cycle_accurate=cycle_accurate,
                                             timestamps=timestamps, live=self.live):
            print(line)

    def itm(self, port: int = 0) -> str:
        """Texte reçu sur un port ITM (capture SWO en cours ou terminée)."""
        if self.trace is None:
            raise RuntimeError("pas de capture SWO (--swo, --swo-file, --swo-config, "
                               "--etm-config --etm-port swo ou vue ITM)")
        return self.trace.text.text(port)

    def swo_config(self, cpu_hz: int, **kw) -> None:
        """Programme ITM/DWT/TPIU (cible arrêtée) ; avec OpenOCD, démarre aussi la capture."""
        from ..trace.config import SwoConfig
        cfg = SwoConfig(cpu_hz=cpu_hz, **kw)
        if self.trace is not None:
            for line in self.trace.configure_target(cfg, self.s, self.s.target):
                print(line)
        else:
            cfg.apply(self.s)
            print(cfg.summary())

    # RTOS (FreeRTOS)
    def rtos(self) -> freertos.RtosSnapshot:
        """TASK : instantané du noyau (via gdb à l'arrêt, via le canal live en marche)."""
        if self._rtos is None:
            if self.s.state != "stopped":
                raise RuntimeError("arrêter la cible une première fois (disposition via gdb)")
            try:
                self._rtos = freertos.FreeRtosLayout.from_session(self.s)
            except GdbError as exc:
                raise RuntimeError(f"pas de FreeRTOS dans l'ELF ({exc})") from None
        if self.s.state == "stopped":
            return freertos.snapshot(self._rtos, self.s.read_memory,
                                     current_sp=self.s.task_sp())
        snap = freertos.snapshot(self._rtos, self.live_read, attempts=8)
        if snap.incoherent:
            raise RuntimeError(f"instantané incohérent : {snap.incoherent}")
        return snap

    def tasks(self) -> list[freertos.Task]:
        """TASK.List : affiche et retourne les tâches."""
        snap = self.rtos()
        print(f"{'tâche':<12} {'état':<10} {'prio':>4}  {'pile':>11}  {'max':>5}  {'CPU':>6}  attend")
        for t in snap.tasks:
            stack = f"{t.stack_used}/{t.stack_size}" if t.stack_used is not None else "?"
            pct = f"{t.stack_percent:.0f}%" if t.stack_percent is not None else "?"
            cpu = f"{t.cpu:.1f}%" if t.cpu is not None else ""
            mark = "*" if t.current else " "
            print(f"{mark}{t.name:<11} {t.state:<10} {t.priority:>4}  {stack:>11}  {pct:>5}  "
                  f"{cpu:>6}  {t.waiting_on or ''}")
        return snap.tasks

    def threads(self) -> list:
        """Threads du programme (pthreads d'un programme natif) : affichés et retournés.
        Le thread courant est marqué « * »."""
        ths = self.s.thread_list()
        print(f" {'n°':>3}  {'nom':<16} {'LWP':>8}  {'état':<8} {'cœur':>4}  où")
        for t in ths:
            f = t.frame
            where = ""
            if f is not None:
                where = f"{f.func or '??'}" + (f"  {os.path.basename(f.file)}:{f.line}" if f.file else "")
            core = "" if t.core is None else str(t.core)
            print(f"{'*' if t.current else ' '}{t.id:>3}  {t.name:<16} {t.lwp or '':>8}  "
                  f"{t.state:<8} {core:>4}  {where}")
        return ths

    def _thread_id(self, which) -> str:
        ths = self.s.thread_list()
        t = next((t for t in ths if t.id == str(which) or t.name == str(which)), None)
        if t is None:
            raise ValueError(f"thread inconnu : {which} (threads : "
                             + ", ".join(f"{t.id} {t.name}" for t in ths) + ")")
        return t.id

    def thread(self, which) -> list:
        """Le thread `which` (numéro gdb ou nom) devient le thread courant ; retourne sa pile."""
        self.s.select_thread(self._thread_id(which))
        self.bt()
        return self.s.backtrace()

    def thread_stop(self, which) -> None:
        """Mode non-stop (--non-stop) : arrête ce thread seul (numéro gdb ou nom)."""
        self.s.stop_thread(self._thread_id(which))

    def thread_resume(self, which) -> None:
        """Mode non-stop (--non-stop) : relance ce thread seul (numéro gdb ou nom)."""
        self.s.resume_thread(self._thread_id(which))

    def queues(self) -> list[freertos.KernelObject]:
        """TASK.Objects : queues, sémaphores et mutex du registre."""
        objs = self.rtos().objects
        for o in objs:
            extra = f"pris par {o.holder}" if o.holder else f"{o.count}/{o.length}"
            waiting = ", ".join(o.waiting_send + o.waiting_receive)
            print(f"{o.name:<12} {o.kind:<20} {extra:<16} {waiting}")
        return objs

    def task(self, name: str) -> list:
        """Contexte d'une tâche : registres et pile d'appels (cible arrêtée)."""
        snap = self.rtos()
        t = snap.task(name)
        if t is None:
            raise KeyError(f"tâche inconnue : {name}")
        regs, frames = freertos.task_context(self.s, self._rtos, t)
        print(f"{t.name} ({t.state})  pc={regs.get('pc', 0):08x} sp={regs.get('sp', 0):08x}")
        for i, f in enumerate(frames):
            print(f"  #{i} {f.pc:08x} {f.func or '??'} {f.file or ''}:{f.line or ''}")
        return frames

    # run-control
    def go(self, until: str | None = None) -> None:
        """Go ; n'attend pas l'arrêt (`wait()`). `until` : Go jusqu'à cet emplacement, où qu'il
        soit (breakpoint temporaire) ; si un autre breakpoint arrête la cible avant, `wait()`
        ou `halt()` retire le temporaire."""
        if until:
            self._until = self.s.break_(until, temporary=True).number
        self.s.go()

    def _drop_until(self, rec: dict) -> None:
        n, self._until = self._until, None
        if n is not None and rec.get("bkptno") != n:
            try:
                self.s.delete_break(n)
            except GdbError:                    # déjà supprimé
                pass

    def halt(self) -> None:
        self.s.halt()
        self._drop_until(self.s.stop_reason)

    # pas à pas : chaque pas attend l'arrêt ; retourne l'enregistrement du dernier arrêt
    # (`reason`, `frame`, et pour out() `return-value`)
    def step(self, n: int = 1) -> StopRecord:
        for _ in range(n):
            self.s.step()
        return StopRecord(self.s.stop_reason)

    def over(self, n: int = 1) -> StopRecord:
        for _ in range(n):
            self.s.step_over()
        return StopRecord(self.s.stop_reason)

    def out(self) -> StopRecord:
        self.s.step_out()
        return StopRecord(self.s.stop_reason)

    def stepi(self, n: int = 1) -> StopRecord:
        for _ in range(n):
            self.s.step_asm()
        return StopRecord(self.s.stop_reason)

    def reset(self) -> None: self.s.reset()

    def wait(self, timeout: float = 10.0) -> StopRecord:
        """Attend l'arrêt de la cible (`timeout` s au plus, sinon TimeoutError) ; revient tout
        de suite si elle est déjà arrêtée. Retourne l'enregistrement du dernier arrêt."""
        rec = StopRecord(self.s.wait_stopped(timeout))
        self._drop_until(rec)
        return rec

    @property
    def running(self) -> bool: return self.s.state == "running"

    def go_till(self, cond: str) -> None:
        """Go.Till : Go jusqu'à ce que l'expression C `cond` devienne vraie (watchpoint matériel
        sur ses variables, retiré à la reprise suivante) ; n'attend pas l'arrêt (`wait()`).
        Registres et valeurs sans adresse : `step_till()`."""
        self.s.go_till(cond)

    def step_till(self, cond: str, asm: bool = False, max_steps: int = 1000) -> StopRecord:
        """Step.Till : pas à pas (ligne, ou instruction si `asm`) jusqu'à ce que `cond` soit
        vraie ; `max_steps` pas au plus (`met` faux si la limite est atteinte)."""
        return StopRecord(self.s.step_till(cond, asm, max_steps))

    def step_change(self, expr: str, asm: bool = False, max_steps: int = 1000) -> StopRecord:
        """Step.Change : pas à pas jusqu'à ce que la valeur de `expr` change (`old`, `new`)."""
        return StopRecord(self.s.step_change(expr, asm, max_steps))

    # breakpoints
    def break_(self, loc: str, hw: bool = False, temporary: bool = False,
               condition: str | None = None, count: int | None = None, log: str | None = None,
               action: Callable[[StopRecord], object] | None = None, stop: bool = False):
        """Break.Set. `count` : arrêt au Nᵉ passage ; `log` : message à chaque passage
        (`{expr}`, `{expr:hex}`…), la cible repart ; `action(arret)` : fonction appelée à chaque
        passage, cible arrêtée (vrai : arrêt, texte : journalisé) ; `stop` : arrêt après l'action."""
        return self.s.break_(loc, hw=hw, temporary=temporary, condition=condition, count=count,
                             log=log, action=_record_action(action), stop=stop)

    def break_options(self, num, **options) -> None:
        """Change `condition`, `count`, `log`, `action` ou `stop` d'un breakpoint ou d'un
        watchpoint existant (options omises gardées ; None ou "" retire)."""
        if "action" in options:
            options["action"] = _record_action(options["action"])
        self.s.break_options(str(num), **options)

    def break_log(self, n: int | None = 20) -> list[str]:
        """Derniers messages des actions de breakpoints (`log=`, texte renvoyé par `action`) ;
        `n=None` : tous (1000 au plus)."""
        msgs = list(self.s.action_log)
        return msgs[-n:] if n else msgs

    def watch(self, expr: str, access: str = "write"): return self.s.watch(expr, access)
    def delete(self, num: str) -> None: self.s.delete_break(str(num))
    def enable(self, num: str, on: bool = True) -> None:
        """Break.ENable / Break.DISable (`on=False`) : le breakpoint reste posé."""
        self.s.enable_break(str(num), on)
    def breaks(self): return self.s.breakpoints()

    # données
    def eval(self, expr: str, fmt: str = "natural") -> str:
        """Valeur d'une expression ; `fmt` : naturel, dec, hex, oct, bin, ascii (core/formats.py)."""
        return self.s.format_value(expr, fmt)

    def var_watch(self, expr: str, fmt: str = "natural") -> str | None:
        """Var.Watch : ajoute `expr` à la vue Watch, au format `fmt` (dans le GUI ; suivie en
        direct si c'est une globale) ; sans GUI, affiche sa valeur. Retourne la valeur (cible
        arrêtée), ou None (cible en marche, ou expression inconnue ici)."""
        from .formats import normalize
        fmt = normalize(fmt)
        if self.on_var_watch is not None:
            self.on_var_watch(expr, fmt)
        value, why = None, "<cible en marche : évaluée au prochain arrêt>"
        if self.s.state == "stopped":
            try:
                value = self.s.format_value(expr, fmt)
            except GdbError as exc:
                why = f"<{str(exc).rsplit(': ', 1)[-1]}>"
        if self.on_var_watch is None:
            print(f"{expr} = {value if value is not None else why}")
        return value
    def set(self, expr: str, value) -> None: self.s.set_var(expr, str(value))
    def read(self, addr: int, n: int) -> bytes: return self.s.read_memory(addr, n)
    def write(self, addr: int, data: bytes) -> None: self.s.write_memory(addr, data)
    def u32(self, addr: int) -> int: return self.s.read_u32(addr)
    def w32(self, addr: int, v: int) -> None: self.s.write_u32(addr, v)
    def reg(self, name: str) -> int: return self.s.register(name)
    def addr(self, sym: str) -> int: return self.s.address_of(sym)

    def d(self, where: int | str, n: int = 64) -> None:
        """Data.dump : affiche un dump hexadécimal."""
        addr = self.s.address_of(where) if isinstance(where, str) else where
        print(hexdump(self.s.read_memory(addr, n), addr))

    def r(self) -> None:
        """Register.view en texte."""
        regs = self.s.registers()
        for i in range(0, len(regs), 4):
            print("   ".join(f"{n:>5} {v:08x}" for n, v in regs[i:i + 4]))

    def bt(self) -> None:
        """Pile d'appels en texte ; TrustZone : à travers les deux mondes, chaque cadre avec
        le sien (`stack()`)."""
        from .security import has_security
        if has_security(self.s):
            self.stack()
            return
        for i, f in enumerate(self.s.backtrace()):
            loc = f"{f.file}:{f.line}" if f.file else ""
            print(f"#{i} {f.pc:08x} {f.func or '??'} {loc}")

    # mémoire en direct et périphériques
    def mem(self, addr: int, n: int) -> bytes:
        """Lecture mémoire qui marche aussi cible en marche (canal live si nécessaire)."""
        if self.s.state == "stopped":
            return self.s.read_memory(addr, n)
        return self.live_read(addr, n)

    # outils mémoire (Data.SAVE, Data.LOAD, Data.Set, Data.Find, Data.ComPare)
    def _where(self, addr, n: int | None = None) -> tuple[int, int | None]:
        """Adresse (entier, ou expression : `"sensors"`, `"&buf[4]"`, `"0x20000000"`) et taille
        (par défaut, celle de l'objet nommé)."""
        if isinstance(addr, int):
            return addr, n
        text = str(addr).strip()
        try:
            return int(text, 0), n
        except ValueError:
            pass
        a = self.s.address_of_expr(text)
        if n is None:
            n = self.s.sizeof(text)
        return a, n

    def mem_save(self, addr, n: int | None, path: str) -> str:
        """Data.SAVE : `n` octets à `addr` dans un fichier ; format d'après l'extension (.bin,
        .hex Intel HEX, .srec S-record). `addr` peut nommer un objet (`"sensors"` : sa taille
        par défaut). Marche cible en marche par le canal mémoire."""
        from .memtools import save
        a, n = self._where(addr, n)
        if not n:
            raise ValueError("taille à donner")
        fmt = save(self.mem, a, n, path)
        print(f"{n} octets de {a:#x} écrits dans {path} ({fmt})")
        return path

    def mem_load(self, path: str, addr=None) -> list[tuple[int, int]]:
        """Data.LOAD : écrit une image en mémoire (RAM ; la flash se programme par
        `program`). HEX, S-record et ELF portent leurs adresses ; un binaire prend `addr`
        (ou `fichier.bin@0x20000000`). Rend [(adresse, taille)] des segments écrits."""
        from .memtools import load
        self._need_stopped()
        at = None if addr is None else self._where(addr)[0]
        segs = load(self.s.write_memory, path, at)
        print(", ".join(f"{n} octets à {a:#x}" for a, n in segs) + f" depuis {path}")
        return segs

    def mem_fill(self, addr, n: int | None, pattern=0) -> None:
        """Data.Set : `n` octets à `addr` remplis du motif répété (entier, bytes, texte)."""
        from .memtools import fill
        self._need_stopped()
        a, n = self._where(addr, n)
        if not n:
            raise ValueError("taille à donner")
        fill(self.s.write_memory, a, n, pattern)

    def mem_find(self, addr, n: int | None, pattern, limit: int = 100, align: int = 1) -> list[int]:
        """Data.Find : adresses du motif (entier petit-boutiste, bytes, texte) dans [addr,
        addr+n), au plus `limit`. Marche cible en marche par le canal mémoire."""
        from .memtools import find
        a, n = self._where(addr, n)
        if not n:
            raise ValueError("taille à donner")
        found = find(self.mem, a, n, pattern, limit, align)
        print(f"{len(found)} occurrence(s)" + (" : " + ", ".join(f"{x:#x}" for x in found[:16])
                                              if found else "") + (" …" if len(found) > 16 else ""))
        return found

    def mem_compare(self, path: str, addr=None):
        """Data.ComPare : compare une image (HEX, S-record, ELF, binaire à `addr`) à la
        mémoire ; rend la `Comparison` (différences, premières adresses)."""
        from .memtools import compare
        at = None if addr is None else self._where(addr)[0]
        res = compare(self.mem, path, at)
        print(res.summary())
        return res

    def sym_list(self, pattern: str = "", kind: str = "tout", show: int = 50) -> list[dict]:
        """sYmbol.List : fonctions et variables globales dont le nom contient `pattern` (ou
        correspond au motif *, ?), sans casse ; `kind` : tout, fonctions, variables. Affiche
        les `show` premières et rend [{nom, genre, adresse, taille, fichier, ligne}]."""
        from .symbols import symbol_rows
        rows = symbol_rows(self._symbols(), pattern, kind)
        for r in rows[:show]:
            where = f"  {r['fichier']}:{r['ligne']}" if r["fichier"] else ""
            print(f"{r['adresse']:#010x} {r['taille'] or 0:6d}  {r['genre']:<8} {r['nom']}{where}")
        if len(rows) > show:
            print(f"… {len(rows) - show} de plus")
        return rows

    def add_symbols(self, path: str, offset: int = 0) -> None:
        """Ajoute les symboles d'un autre ELF (bootloader, image chargée à part) à ceux de
        l'ELF de la session ; `offset` : décalage de chargement. Profil, trace et couverture
        les connaissent aussi."""
        self.s.add_symbols(path, offset)
        self.symbols = None
        if self.on_program is not None and self.s.elf:
            self.on_program(self.s.elf)

    def live_read(self, addr: int, n: int) -> bytes:
        """Lecture par le canal latéral (OpenOCD TCL / QEMU QMP), cible arrêtée ou non."""
        if self.live is None:
            raise RuntimeError("pas de canal mémoire live pour cette cible")
        return self.live.read(addr, n)

    def load_svd(self, path: str) -> None:
        self.device = svd.parse(path)

    def periph(self, name: str, force: bool = False) -> dict[str, int]:
        """PER.view : affiche les registres décodés d'un périphérique SVD, retourne {reg: valeur}."""
        if self.device is None:
            raise RuntimeError("aucun SVD chargé (gdbtrace.load_svd ou --svd)")
        p = self.device.peripheral(name)
        if p is None:
            raise KeyError(f"périphérique inconnu : {name}")
        values: dict[str, int] = {}
        for addr, n, regs in svd.read_blocks(p, force=force):
            values.update(svd.decode_block(p, addr, self.mem(addr, n), regs))
        print(f"{p.name} @ {p.base:#010x}  {p.description}")
        for r in p.registers:
            if r.name not in values:
                why = "write-only" if r.access == svd.WRITE_ONLY else "non lu : lecture destructive"
                print(f"  {r.name:<12} {p.address(r):08x}  ({why})")
                continue
            v = values[r.name]
            print(f"  {r.name:<12} {p.address(r):08x}  {v:0{r.nbytes * 2}x}")
            for f in r.fields:
                if f.access != svd.WRITE_ONLY:
                    print(f"      {f.name:<14} {f.bits:<8} {f.format(v)}")
        return values

    # accès bas niveau
    def gdb(self, cli: str) -> str: return self.s.cli(cli)
    def monitor(self, cmd: str) -> str: return self.s.monitor(cmd)

    def script_globals(self) -> dict:
        """Globales d'un script et de la console : `gdbtrace`, `session`, `hexdump`."""
        return {"gdbtrace": self, "session": self.s, "hexdump": hexdump}

    def run_script(self, path: str) -> None:
        """Exécute un script avec les mêmes globales que la console : `gdbtrace`, `session`,
        `hexdump`."""
        runpy.run_path(path, init_globals=self.script_globals())

    def check(self, name: str, condition, message: str = "") -> bool:
        """Cas de test en ligne : affiche « ✓ nom » ou « ✗ nom : message » et rend la condition.
        Avec `--junit`, un cas du rapport ; en `--batch`, un cas en échec rend le code de
        retour non nul."""
        ok = bool(condition)
        print(f"✓ {name}" if ok else f"✗ {name}" + (f" : {message}" if message else ""))
        self.checks.append((name, ok, message))
        if self._junit is not None:
            self._junit.check(name, ok, message)
        return ok


class Console:
    """Interpréteur Python interactif, exécuté hors du thread GUI.

    `write` reçoit tout ce qui est affiché (stdout/stderr) ; il est appelé depuis le
    thread d'exécution, à l'appelant de le renvoyer vers la GUI de façon sûre.
    """

    def __init__(self, api: ScriptAPI, write: Callable[[str], None]):
        self.api = api
        self.write = write
        self.namespace = {"gdbtrace": api, "session": api.s, "hexdump": hexdump}
        self.interp = code.InteractiveInterpreter(self.namespace)
        self._buffer: list[str] = []
        self._lock = threading.Lock()
        self.busy = False

    def push(self, line: str, done: Callable[[bool], None] | None = None) -> None:
        """Ajoute une ligne. `done(more)` indique si un bloc multi-lignes attend la suite."""
        self._buffer.append(line)
        source = "\n".join(self._buffer)
        try:
            compiled = code.compile_command(source, "<console>", "single")
        except (SyntaxError, OverflowError, ValueError):
            self._buffer.clear()
            self._run(lambda: self.interp.showsyntaxerror("<console>"), done)
            return
        if compiled is None:  # bloc incomplet
            if done:
                done(True)
            return
        self._buffer.clear()
        self._run(lambda: self.interp.runcode(compiled), done)

    def run_file(self, path: str, done: Callable[[bool], None] | None = None) -> None:
        def job():
            try:
                runpy.run_path(path, init_globals=dict(self.namespace))
            except SystemExit:
                pass
            except BaseException:
                self.interp.showtraceback()
        self._run(job, done)

    def _run(self, job: Callable[[], None], done: Callable[[bool], None] | None) -> None:
        def worker():
            with self._lock:
                self.busy = True
                out = _Writer(self.write)
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
                    job()
                self.busy = False
            if done:
                done(False)
        threading.Thread(target=worker, name="gdbtrace-console", daemon=True).start()


class _Writer(io.TextIOBase):
    def __init__(self, fn: Callable[[str], None]):
        self.fn = fn

    def write(self, s: str) -> int:
        if s:
            self.fn(s)
        return len(s)
