"""Gestion de la capture SWO d'une session (indépendant de l'interface graphique).

Possède la source SWO, la session de trace et les consommateurs permanents (texte ITM,
statistiques d'exceptions). Un profil peut s'y brancher pour recevoir les échantillons de
PC du DWT. `post(fn, *args)` sert à rappeler les abonnés dans le bon thread (GUI).
"""
from __future__ import annotations

import threading
import time
from typing import Callable

from .config import SwoConfig, openocd_tpiu_commands, openocd_tpiu_name
from .itm import ItmTextSink
from .profile import Profile
from .swo import ExceptionStats, FileSwoSource, SwoSource, TcpSwoSource, TraceSession, profile_sink

ETM_NOTIFY_PERIOD = 0.5               # s : rafraîchissement des vues pendant une capture
CPUID = 0xE000ED00
CORTEX_M7 = 0xC27                      # CPUID.PARTNO

LineListener = Callable[[int, str, int], None]


class TraceManager:
    def __init__(self, post: Callable[..., None] | None = None,
                 on_error: Callable[[str], None] | None = None):
        self.post = post or (lambda fn, *a: fn(*a))
        self.on_error = on_error or (lambda msg: None)
        self.session: TraceSession | None = None
        self.source: SwoSource | None = None
        self.text = ItmTextSink(text_ports={0}, on_line=self._line)
        self.exceptions = ExceptionStats()
        from .datatrace import DataTraceSink
        from .tasktrace import TaskTraceSink
        self.datatrace = DataTraceSink()     # trace de données du DWT (Var.TRACE)
        self.tasktrace = TaskTraceSink()     # temps par tâche (changements de tâche par l'ITM)
        self.line_listeners: list[LineListener] = []
        self._profile: Profile | None = None
        self._profile_sink = None
        # trace d'instructions ETM (flux TPIU id 2, ou simulée sous QEMU)
        self.etm = None                     # EtmTrace
        self.etm_sim = None                 # QemuEtmSim
        # trace MTB des Cortex-M0+ (sonde réelle, ou simulée sous QEMU), même EtmTrace
        self.mtb = None                     # MtbCapture
        self.mtb_sim = None                 # QemuMtbSim
        self.etm_listeners: list[Callable[[], None]] = []
        self._notify_lock = threading.Lock()
        self._notify_pending = False
        self._notify_last = 0.0
        self.etm_busy = False
        #: ETM par SWO configuré (le formateur TPIU doit rester actif) ; journal et cause du
        #: dernier échec de sa mise en route, montrés par les vues (`trace_status`)
        self.etm_swo = False
        self.setup_log: list[str] = []
        self.setup_error: str | None = None
        #: filtre de la trace ETM : effectif (tel que le matériel l'applique), spécifications
        #: d'origine (re-résolues sur un autre ELF), configuration matérielle programmée
        self.etm_filter = None
        self.etm_filter_specs: tuple[list[str], list[str], list[str]] | None = None
        self.etm_cfg = None

    # ------------------------------------------------------------------ ETM
    def setup_etm(self, elf: str, symbols, cycle_accurate: bool = False,
                  version: int = 3) -> str | None:
        """Prépare le décodage ETM pour cet ELF. `cycle_accurate` doit refléter la
        configuration de l'ETM (ETMCR bit 12) ; `version` : 3 (M3/M4) ou 4 (M7), voir
        `detect_etm`. Retourne un message si impossible."""
        try:
            from .etm.image import CodeImage
            from .etm.trace import EtmTrace
            self.etm = EtmTrace(CodeImage(elf), symbols, cycle_accurate=cycle_accurate,
                                version=version)
        except Exception as exc:  # noqa: BLE001 - capstone absent, ELF illisible
            self.etm = None
            return f"trace ETM indisponible : {exc}"
        return None

    def retarget(self, elf: str, symbols, cycle_accurate: bool = False) -> str | None:
        """Le programme de la cible a changé : décodage ETM (et simulation QEMU, si elle était
        active) sur le nouveau code. La trace de l'ancien programme est abandonnée."""
        sim = self.etm_sim
        if sim is not None:
            try:
                sim.close()
            except Exception:  # noqa: BLE001
                pass
            self.etm_sim = None
        mtb_sim, mtb = self.mtb_sim, self.mtb
        if mtb_sim is not None:
            try:
                mtb_sim.close()
            except Exception:  # noqa: BLE001
                pass
            self.mtb_sim = None
        version = self.etm.version if self.etm is not None else 3
        msg = self.setup_etm(elf, symbols, cycle_accurate, version)
        if sim is not None and self.etm is not None:
            self.enable_qemu_sim(sim.qmp)
        if self.etm_filter_specs and self.etm is not None:   # mêmes noms, nouvelles adresses
            try:
                self.set_etm_filter(*self.etm_filter_specs, session=getattr(self, "_attached", None))
            except Exception as exc:  # noqa: BLE001 - fonction disparue, cible en marche...
                self.etm_filter = self.etm_filter_specs = None
                self.setup_error = f"filtre ETM abandonné : {exc}"
        if mtb_sim is not None and self.etm is not None:
            self.enable_mtb_sim(mtb_sim.qmp, mtb_sim.size, getattr(self, "_attached", None),
                                autohalt=mtb_sim.autohalt)
        if mtb is not None and self.etm is not None:
            mtb.retarget(self.etm.image)
            self.etm.timing_label = "estimé (modèle de cycles, MTB)"
        for fn in list(self.etm_listeners):
            fn()
        return msg

    def detect_etm(self, session) -> int:
        """Architecture de l'ETM de la cible arrêtée : TRCIDR1/ETMIDR si un ETM répond, sinon
        d'après le cœur (CPUID : Cortex-M7 → ETMv4, pour l'ETM simulé de QEMU). Le décodeur
        suit ; retourne 3 ou 4."""
        from .etm.config import etm_version
        v = etm_version(session.read_u32)
        if v not in (2, 3, 4):                   # pas d'ETM lisible (QEMU)
            try:
                part = (session.read_u32(CPUID) >> 4) & 0xFFF
            except Exception:  # noqa: BLE001
                part = 0
            v = 4 if part == CORTEX_M7 else 3
        v = 4 if v == 4 else 3
        self.set_etm_version(v)
        return v

    def set_etm_version(self, version: int) -> None:
        """Décodeur ETMv3 ou ETMv4 ; la trace déjà décodée est abandonnée en cas de changement."""
        if self.etm is None or self.etm.version == version:
            return
        from .etm.trace import EtmTrace
        old = self.etm
        self.etm = EtmTrace(old.image, old.symbols, cycle_accurate=old.cycle_accurate,
                            version=version)
        self.etm.simulated, self.etm.timing_label = old.simulated, old.timing_label
        if self.etm_sim is not None:
            self.etm_sim.etm = self.etm
        self._etm_changed()

    def enable_qemu_sim(self, qmp) -> None:
        from .etm.qemu_sim import QemuEtmSim
        from .etm.trace import EtmTrace
        if self.etm is not None:
            if self.etm.version == 3 and not self.etm.cycle_accurate:
                # la simulation ETMv3 encode en cycle-accurate
                self.etm = EtmTrace(self.etm.image, self.etm.symbols, cycle_accurate=True)
            self.etm.simulated = True            # cycles du modèle : durées estimées
            self.etm_sim = QemuEtmSim(qmp, self.etm.image, self.etm)
            self.etm_sim.filter = self.etm_filter

    # ------------------------------------------------------------------ MTB (Cortex-M0+)
    def enable_mtb_sim(self, qmp, size: int = 1024, session=None, autohalt: bool = False) -> None:
        """MTB simulée sous QEMU : tampon de `size` octets (les `size / 8` derniers branchements
        de chaque exécution ; 0 : illimité) ; `autohalt` : arrêts internes simulés, trace
        complète."""
        from .mtb_capture import QemuMtbSim
        if self.etm is not None:
            self.mtb_sim = QemuMtbSim(qmp, self.etm.image, self.etm, size, autohalt=autohalt)
            self.etm.timing_label = "estimé (modèle de cycles, MTB simulée)"
            if session is not None:
                self.mtb_sim.last_pc = session.register("pc")
                if autohalt:
                    self.mtb_sim.start_halter(session)
                    session.set_stop_interceptor(self._intercept)

    def setup_mtb(self, session, base: int, buffer: int, size: int, autohalt: bool = False):
        """MTB d'une vraie cible (arrêtée) : programmation, puis lecture à chaque arrêt ; en
        AUTOHALT, la MTB arrête le cœur à chaque tampon plein : arrêts internes, invisibles."""
        from .mtb_capture import MtbCapture
        if self.etm is None:
            raise RuntimeError("trace d'instructions indisponible (capstone requis)")
        self.mtb = MtbCapture(session, self.etm.image, base, buffer, size, autohalt=autohalt)
        self.etm.timing_label = "estimé (modèle de cycles, MTB)"
        self.attach_session(session)
        if autohalt:
            session.set_stop_interceptor(self._intercept)
        return self.mtb.state

    # ------------------------------------------------------------------ AUTOHALT : arrêts internes
    def _autohalt_source(self):
        for src in (self.mtb, self.mtb_sim):
            if src is not None and src.autohalt:
                return src
        return None

    def _intercept(self, reason: dict) -> bool:
        """Thread lecteur de gdb : un arrêt non demandé, MTB en AUTOHALT → arrêt interne,
        traité en tâche de fond (jamais d'appel gdb ici)."""
        if self._autohalt_source() is None or getattr(self, "_attached", None) is None:
            return False
        import threading
        self.etm_busy = True
        threading.Thread(target=self._internal_stop, name="mtb-autohalt-stop", daemon=True).start()
        return True

    def _internal_stop(self) -> None:
        """Vide la MTB ; relance la cible si c'est elle qui l'a arrêtée, sinon livre l'arrêt."""
        session = self._attached
        halted = False
        try:
            pc = session.register("pc")
            if self.mtb_sim is not None and self.mtb_sim.path is not None:
                halted = self.mtb_sim.take_halt()
                if halted:                          # tronçon mis de côté, converti à l'arrêt
                    self.mtb_sim.stash_run(pc)      # visible ; journal du suivant (pas de hook)
                    self.mtb_sim.start_run()
            if self.mtb is not None:
                errors = self.mtb.decoder.stats.errors
                self.etm.feed_events(self.mtb.collect(), errors=self.mtb.decoder.stats.errors - errors)
                halted = halted or self.mtb.halted
        except Exception as exc:  # noqa: BLE001
            self.post(self.on_error, f"MTB (AUTOHALT) : {exc}")
            halted = False
        finally:
            self.etm_busy = False
        # pas de rafraîchissement des vues à chaque tampon : rendre une longue trace prend du
        # temps et figerait l'interface ; elles se mettent à jour à l'arrêt visible
        if not halted:
            self._etm_changed()
        try:
            if halted:
                session.resume_internal()
            else:
                session.deliver_stop()
        except Exception as exc:  # noqa: BLE001
            self.post(self.on_error, f"MTB (AUTOHALT) : reprise impossible : {exc}")
            session.deliver_stop(force=True)

    def source_label(self) -> str:
        """D'où vient la trace d'instructions (libellé des vues), et son filtre."""
        label = self._source_label()
        return f"{label} — {self.etm_filter.describe()}" if self.etm_filter is not None else label

    def _source_label(self) -> str:
        if self.mtb_sim is not None:
            if self.mtb_sim.autohalt:
                return f"MTB simulée (QEMU, AUTOHALT, {self.mtb_sim.autohalts} arrêts)"
            return "MTB simulée (QEMU" + (f", {self.mtb_sim.size} octets)" if self.mtb_sim.size
                                          else ", tampon illimité)")
        if self.mtb is not None:
            return f"MTB (AUTOHALT, {self.mtb.autohalts} arrêts)" if self.mtb.autohalt else "MTB"
        if self.etm_sim is not None:
            return "ETM simulé (QEMU)"
        return "capture ETM" if self.active else "trace ETM"

    # ------------------------------------------------------------------ cycle des exécutions
    def _etm_feed(self, data: bytes) -> None:
        self.etm.feed(data)
        self._etm_changed()

    def _etm_changed(self) -> None:
        """Prévient les vues que la trace a changé. Pendant une capture continue, la trace
        change à chaque paquet reçu : une seule notification en attente à la fois, et au plus
        une par ETM_NOTIFY_PERIOD ; la dernière part toujours (fin de flux). Sinon le thread
        GUI ne fait plus que redessiner les vues, et la fenêtre gèle."""
        with self._notify_lock:
            if self._notify_pending:
                return
            self._notify_pending = True
            delay = self._notify_last + ETM_NOTIFY_PERIOD - time.monotonic()
        if delay <= 0:
            self.post(self._fire_etm_changed)
        else:
            t = threading.Timer(delay, self.post, args=(self._fire_etm_changed,))
            t.daemon = True
            t.start()

    def _fire_etm_changed(self) -> None:
        with self._notify_lock:
            self._notify_pending = False
            self._notify_last = time.monotonic()
        for fn in list(self.etm_listeners):
            fn()

    def _sims(self) -> list:
        return [s for s in (self.etm_sim, self.mtb_sim) if s is not None]

    def attach_session(self, session) -> None:
        """Branche la trace sur la session : simulation activée avant chaque reprise,
        conversion (ou lecture de la MTB) à chaque arrêt. Une seule fois par session."""
        if getattr(self, "_attached", None) is session:
            return
        self._attached = session
        session.add_resume_hook(self.on_target_running)
        session.on_event(lambda kind, data: self.on_target_stopped() if kind == "stopped" else None)

    def wait_etm(self, timeout: float = 30.0) -> bool:
        """Attend la fin de la conversion de la trace de la dernière exécution."""
        import time
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if not self.etm_busy and all(s.path is None for s in self._sims()):
                return True
            time.sleep(0.02)
        return False

    def on_target_running(self) -> None:
        """Avant chaque reprise : journal QEMU de la nouvelle exécution. Si la trace de la
        précédente est encore en conversion (ou la MTB en lecture), on l'attend : sinon cette
        exécution serait perdue (ou la MTB remise à zéro pendant qu'elle trace)."""
        if self.etm_busy:
            self.wait_etm()
        if self.etm is not None:
            self.etm.resumed()                  # la resynchronisation qui suit n'est pas un trou
        for sim in self._sims():
            if sim.path is None:
                try:
                    sim.start_run()
                except Exception as exc:  # noqa: BLE001
                    self.on_error(f"trace simulée : {exc}")

    def on_target_stopped(self) -> None:
        """Fin d'une exécution, en tâche de fond : sous QEMU, conversion du journal en trace ;
        sur une cible avec MTB, lecture et remise à zéro du tampon. ETMv4 capturé : le dernier
        élément, encore spéculatif, est validé."""
        if self.mtb_sim is not None and self.mtb_sim.autohalt:
            self.mtb_sim.clear_halt()
        jobs = [s for s in self._sims() if s.path is not None]
        mtb = self.mtb
        if not jobs and mtb is None:
            if self.etm is not None and self.etm.version == 4 and self.etm.flush():
                self._etm_changed()
            return
        import threading
        session = getattr(self, "_attached", None)

        def work():
            try:
                pc = session.register("pc") if session is not None and self.mtb_sim is not None else None
                for sim in jobs:
                    sim.end_run(pc)
                if mtb is not None:
                    errors = mtb.decoder.stats.errors
                    self.etm.feed_events(mtb.collect(), errors=mtb.decoder.stats.errors - errors)
            except Exception as exc:  # noqa: BLE001
                self.post(self.on_error, f"trace d'instructions : {exc}")
            finally:
                self.etm_busy = False
            self._etm_changed()
        self.etm_busy = True          # avant le thread : wait_etm() ne peut pas le manquer
        threading.Thread(target=work, name="trace-run", daemon=True).start()

    def configure_etm(self, cfg, session) -> list[str]:
        """Programme l'ETM et le TPIU (cible arrêtée) ; le décodeur suit l'architecture de
        l'ETM détectée (v3 ou v4)."""
        if self.etm is not None:                # ETM illisible (QEMU) : version déjà détectée
            cfg.version = self.etm.version
        cfg.dwt_reserved = tuple(self.datatrace.vars)   # comparateurs de la trace de données
        if cfg.filter is None and self.etm_filter_specs and self.etm is not None:
            from .etm.filter import build_filter   # filtre demandé avant la programmation
            cfg.filter = build_filter(*self.etm_filter_specs, symbols=self.etm.symbols)
        log = [f"{name} = {val:#010x}" for name, val in cfg.apply(session)]
        self.etm_cfg = cfg
        self.etm_filter = cfg.effective_filter()
        log.append(cfg.summary())
        self.set_etm_version(cfg.version)
        self.attach_session(session)            # ETMv4 : validation à chaque arrêt
        return log

    def set_etm_filter(self, ranges=(), start=(), stop=(), session=None) -> list[str]:
        """Filtre de la trace ETM : plages (`fib`, `main.c`, `0x100-0x200`), adresses de
        démarrage et d'arrêt (`fib`, `main.c:59`, `0x1f4`) ; tout vide : tout le code.
        ETM réel déjà programmé : reprogrammé (cible arrêtée) ; ETM simulé : appliqué au chemin
        de QEMU, avec les limites du matériel (blocs alignés du DWT en ETMv3). La couverture se
        limite aux plages demandées. Retourne le journal."""
        from .etm.config import EtmConfig
        from .etm.filter import build_filter
        if self.etm is None:
            raise RuntimeError("trace ETM indisponible (capstone requis, ou pas d'ELF)")
        flt = build_filter(ranges, start, stop, self.etm.symbols)
        log: list[str] = []
        if self.etm_cfg is not None:            # ETM matériel : reprogrammé avec ce filtre
            if session is None or session.state != "stopped":
                raise RuntimeError("arrêter la cible pour reprogrammer l'ETM")
            self.etm_cfg.filter = flt if flt.active else None
            log = self.configure_etm(self.etm_cfg, session)
            eff = self.etm_cfg.effective_filter()
        else:                                   # ETM simulé (ou ETM pas encore programmé)
            cfg = EtmConfig(version=self.etm.version, filter=flt if flt.active else None,
                            dwt_reserved=tuple(self.datatrace.vars))
            cfg._etm4_filter() if cfg.version == 4 else cfg._etm3_filter()   # limites du matériel
            eff = cfg.effective_filter()
        self.etm_filter = eff
        self.etm_filter_specs = (list(ranges), list(start), list(stop)) if flt.active else None
        if self.etm_sim is not None:
            self.etm_sim.filter = eff
        self.etm.cov.set_scope(flt.ranges or None)
        self._etm_changed()
        log.append(eff.describe() if eff is not None else "filtre ETM retiré : tout le code est tracé")
        return log

    # ------------------------------------------------------------------ trace de données (DWT)
    def trace_variable(self, session, expr: str, access: str = "write", pc: bool = False,
                       cpu_hz: int | None = None) -> list[str]:
        """Var.TRACE (cible arrêtée) : un comparateur du DWT émet la valeur de `expr` à chaque
        accès (`access` : write, read, rw ; `pc` : avec le PC de l'instruction). Les échantillons
        arrivent par la capture SWO ou de trace (ITM). Retourne le journal."""
        from .datatrace import DEMCR, DWT_CTRL, TracedVar, comparator_writes, variable_layout
        if access not in ("write", "read", "rw"):
            raise ValueError("accès : write, read ou rw")
        if session is None or session.state != "stopped":
            raise RuntimeError("arrêter la cible pour programmer le DWT")
        if cpu_hz:
            self.datatrace.cpu_hz = cpu_hz
        self.untrace_variable(session, expr, quiet=True)          # reprogrammée si déjà tracée
        addr, size, layout = variable_layout(session, expr)
        session.write_u32(DEMCR, session.read_u32(DEMCR) | 1 << 24)
        numcomp = min((session.read_u32(DWT_CTRL) >> 28) & 0xF, 4) or 4
        taken = set(self.datatrace.vars)
        if self.etm_cfg is not None and getattr(self.etm_cfg, "version", 3) != 4:
            taken |= set(self.etm_cfg.dwt_used)
        free = [n for n in range(numcomp - 1, -1, -1) if n not in taken]
        if not free:
            raise RuntimeError(f"plus de comparateur du DWT libre ({numcomp} : variables tracées et "
                               "filtre de l'ETM) ; arrêter la trace d'une variable")
        var = TracedVar(expr, addr, size, free[0], access, pc, layout)
        log = []
        for name, reg, value in comparator_writes(var):
            session.write_u32(reg, value)
            log.append(f"{name} = {value:#010x}")
        self.datatrace.add(var)
        log.append(f"trace de {expr} ({size} octets à {addr:#010x}, {access}"
                   + (", avec le PC" if pc else "") + f") : comparateur {var.comparator}")
        if self.source is None:
            log.append("pas de capture SWO ou de trace en cours pour l'instant : les valeurs "
                       "arriveront avec elle (--swo-config, Trace › ETM par SWO…, vue ITM)")
        return log

    def untrace_variable(self, session, expr: str | None = None, quiet: bool = False) -> list[str]:
        """Arrête la trace de `expr` (toutes les variables si None) ; comparateur libéré."""
        from .datatrace import DWT_COMP0
        log = []
        for comp, var in list(self.datatrace.vars.items()):
            if expr is not None and var.expr != expr:
                continue
            if session is not None and session.state == "stopped":
                session.write_u32(DWT_COMP0 + 16 * comp + 8, 0)
            self.datatrace.remove(comp)
            log.append(f"trace de {var.expr} arrêtée (comparateur {comp} libéré)")
        if not log and not quiet and expr is not None:
            raise KeyError(f"« {expr} » n'est pas tracée")
        return log

    def start_etm_swo(self, session, target=None, cpu_hz: int = 0, swo_hz: int = 2_000_000,
                      stall: bool = False, stm32: bool = False, port: int = 3443,
                      swo: str | None = None, swo_file: str | None = None,
                      cycle_accurate: bool = False) -> list[str]:
        """ETM par la broche SWO (cible arrêtée) : TPIU avec formateur (ETM ID 2, ITM ID 1),
        capture du SWO, puis programmation de l'ETM. La capture est servie par OpenOCD
        (`--openocd`, ou `--remote` avec `--tcl`) sur `port`, ou ailleurs : `swo` (HOST:PORT,
        Orbuculum, J-Link) ou `swo_file` (capture rejouée). Retourne le journal ; en cas
        d'échec, la cause reste dans `setup_error` (vues Couverture et Trace)."""
        from ..core.target import OpenOcdTarget, RemoteTarget
        from .etm.config import EtmConfig
        self.setup_error = None
        try:
            served = isinstance(target, OpenOcdTarget) or (isinstance(target, RemoteTarget)
                                                           and bool(target.tcl_port))
            if not (served or swo or swo_file):
                raise RuntimeError("la sonde ne sert pas le SWO : --openocd, ou --remote … avec "
                                   "--tcl HOST:PORT (OpenOCD lancé à part), ou --swo HOST:PORT")
            if self.etm is None:
                raise RuntimeError("trace ETM indisponible (capstone requis, ou pas d'ELF)")
            if cpu_hz <= 0:
                raise ValueError("préciser la fréquence du cœur (--cpu-hz)")
            host = (target.tcl_host or target.host) if isinstance(target, RemoteTarget) else "127.0.0.1"
            cfg = SwoConfig(cpu_hz=cpu_hz, swo_hz=swo_hz, ports=0xFFFFFFFF, formatter=True)
            log = self.configure_target(cfg, session, target, port, host)
            if swo:
                h, _, p = swo.rpartition(":")
                self.start_tcp(h or "127.0.0.1", int(p), formatter=True)
                log.append(f"capture SWO TCP {h or '127.0.0.1'}:{p} démarrée")
            elif swo_file:
                self.start_file(swo_file, formatter=True)
                log.append(f"capture SWO rejouée : {swo_file}")
            etm = EtmConfig(port="swo", cpu_hz=cpu_hz, swo_hz=swo_hz, stall=stall, stm32=stm32,
                            cycle_accurate=cycle_accurate)
            log += self.configure_etm(etm, session)
        except Exception as exc:
            self.setup_error = f"ETM par SWO : {exc}"
            raise
        self.etm_swo = True
        self.setup_log = log
        return log

    def trace_status(self) -> str:
        """Où en est la trace d'instructions, en une ligne (vues Couverture et Trace) : d'où
        elle vient, ou pourquoi rien n'arrive et quoi faire."""
        if self.etm is None:
            return "trace ETM indisponible (capstone requis, ou pas d'ELF)"
        if self.etm_sim is not None or self.mtb_sim is not None or self.mtb is not None:
            return self.source_label()
        src, sess = self.source, self.session
        if src is None:
            hint = ("aucune capture de trace d'instructions : Trace › ETM par SWO… (ou --etm-config "
                    "--etm-port swo), --etm-config --trace HOST:PORT (sonde parallèle), --mtb (M0+)")
            return f"{self.setup_error} — {hint}" if self.setup_error else hint
        if src.error and not src.connected:
            return f"{src.description} : {src.error}"
        if sess is None or sess.deformatter is None:
            return (f"{src.description} sans formateur TPIU : l'ETM n'y passe pas "
                    "(Trace › ETM par SWO…, ou Formateur TPIU dans la vue ITM)")
        if sess.etm_bytes == 0:
            if not sess.bytes:
                return f"{src.description} : rien reçu pour l'instant (cible arrêtée ?)"
            return (f"{src.description} : {sess.bytes} octets reçus, aucun octet ETM (ID 2) : "
                    "ETM programmé (--etm-config) ? débit SWO et --cpu-hz justes ?")
        st = self.etm.stats
        return (f"{src.description} : {sess.etm_bytes} octets ETM, {st['instructions']} instructions, "
                f"{st['débordements']} débordements, {st['erreurs']} erreurs")

    def close(self) -> None:
        self.stop()
        for name in ("etm_sim", "mtb_sim"):
            sim = getattr(self, name)
            if sim is not None:
                try:
                    sim.close()
                except Exception:  # noqa: BLE001
                    pass
                setattr(self, name, None)
        self.mtb = None

    # ------------------------------------------------------------------ sources
    @property
    def active(self) -> bool:
        return self.source is not None and self.source.running

    def start_tcp(self, host: str, port: int, formatter: bool = False) -> None:
        self._start(TcpSwoSource(host, port), formatter)

    def start_file(self, path: str, formatter: bool = False) -> FileSwoSource:
        src = FileSwoSource(path)
        self._start(src, formatter)
        return src

    def _start(self, source: SwoSource, formatter: bool) -> None:
        self.stop()
        self.session = TraceSession(formatter=formatter,
                                    etm_sink=self._etm_feed if self.etm is not None else None)
        self.session.add_sink(lambda pkts: [self.text.add(p) for p in pkts])
        self.session.add_sink(self.exceptions)
        self.session.add_sink(self.datatrace)
        self.session.add_sink(self.tasktrace)
        if self._profile is not None:
            self._attach_profile()
        self.source = source
        source.start(self.session)

    def stop(self) -> None:
        if self.source is not None:
            self.source.stop()
        self.source = None

    def status(self) -> str:
        if self.source is None:
            return "pas de capture SWO"
        s = self.session.stats if self.session else {}
        state = "connecté" if self.source.connected else ("erreur : " + self.source.error
                                                          if self.source.error else "en attente")
        return (f"{self.source.description} — {state} — {s.get('octets', 0)} octets, "
                f"{s.get('paquets', 0)} paquets, {s.get('erreurs', 0)} erreurs, "
                f"{s.get('overflows', 0)} overflows")

    # ------------------------------------------------------------------ texte ITM
    def _line(self, port: int, text: str, ts: int) -> None:
        for fn in list(self.line_listeners):
            self.post(fn, port, text, ts)

    def set_text_ports(self, ports: set[int]) -> None:
        self.text.text_ports = set(ports)

    # ------------------------------------------------------------------ profil par SWO
    def set_profile(self, profile: Profile | None) -> None:
        """Branche (ou débranche) un profil sur les échantillons de PC du DWT."""
        if self.session is not None and self._profile_sink is not None:
            self.session.remove_sink(self._profile_sink)
        self._profile, self._profile_sink = profile, None
        if profile is not None and self.session is not None:
            self._attach_profile()

    def _attach_profile(self) -> None:
        self._profile_sink = profile_sink(self._profile)
        self.session.add_sink(self._profile_sink)

    # ------------------------------------------------------------------ cible
    def configure_target(self, cfg: SwoConfig, session, target=None, port: int = 3443,
                         host: str = "127.0.0.1") -> list[str]:
        """Programme ITM/DWT/TPIU (cible arrêtée). Avec OpenOCD, configure aussi son TPIU
        pour servir le SWO en TCP sur `port`, et démarre la capture. Retourne un journal."""
        log = [f"{name} = {val:#010x} (relu {back:#010x})" for name, val, back in cfg.apply(session)]
        from ..core.target import OpenOcdTarget, RemoteTarget
        if isinstance(target, OpenOcdTarget) or (isinstance(target, RemoteTarget) and target.tcl_port):
            tpiu = openocd_tpiu_name(session)
            for cmd in openocd_tpiu_commands(cfg, tpiu, port):
                out = session.monitor(cmd).strip()
                log.append(f"monitor {cmd}" + (f" → {out}" if out else ""))
            self.start_tcp(host if isinstance(target, RemoteTarget) else "127.0.0.1", port,
                           formatter=cfg.formatter)
            log.append(f"capture SWO TCP {port} démarrée")
        log.append(cfg.summary())
        return log
