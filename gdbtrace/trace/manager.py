"""Gestion de la capture SWO d'une session (indépendant de l'interface graphique).

Possède la source SWO, la session de trace et les consommateurs permanents (texte ITM,
statistiques d'exceptions). Un profil peut s'y brancher pour recevoir les échantillons de
PC du DWT. `post(fn, *args)` sert à rappeler les abonnés dans le bon thread (GUI).
"""
from __future__ import annotations

from typing import Callable

from .config import SwoConfig, openocd_tpiu_commands, openocd_tpiu_name
from .itm import ItmTextSink
from .profile import Profile
from .swo import ExceptionStats, FileSwoSource, SwoSource, TcpSwoSource, TraceSession, profile_sink

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
        self.etm_busy = False

    # ------------------------------------------------------------------ ETM
    def setup_etm(self, elf: str, symbols, cycle_accurate: bool = False) -> str | None:
        """Prépare le décodage ETM pour cet ELF. `cycle_accurate` doit refléter la
        configuration de l'ETM (ETMCR bit 12). Retourne un message si impossible."""
        try:
            from .etm.image import CodeImage
            from .etm.trace import EtmTrace
            self.etm = EtmTrace(CodeImage(elf), symbols, cycle_accurate=cycle_accurate)
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
        msg = self.setup_etm(elf, symbols, cycle_accurate)
        if sim is not None and self.etm is not None:
            self.enable_qemu_sim(sim.qmp)
        if mtb_sim is not None and self.etm is not None:
            self.enable_mtb_sim(mtb_sim.qmp, mtb_sim.size)
        if mtb is not None and self.etm is not None:
            mtb.retarget(self.etm.image)
            self.etm.timing_label = "estimé (modèle de cycles, MTB)"
        for fn in list(self.etm_listeners):
            fn()
        return msg

    def enable_qemu_sim(self, qmp) -> None:
        from .etm.qemu_sim import QemuEtmSim
        from .etm.trace import EtmTrace
        if self.etm is not None:
            if not self.etm.cycle_accurate:      # la simulation encode en cycle-accurate
                self.etm = EtmTrace(self.etm.image, self.etm.symbols, cycle_accurate=True)
            self.etm.simulated = True            # cycles du modèle : durées estimées
            self.etm_sim = QemuEtmSim(qmp, self.etm.image, self.etm)

    # ------------------------------------------------------------------ MTB (Cortex-M0+)
    def enable_mtb_sim(self, qmp, size: int = 1024, session=None) -> None:
        """MTB simulée sous QEMU : tampon de `size` octets (les `size / 8` derniers branchements
        de chaque exécution ; 0 : illimité)."""
        from .mtb_capture import QemuMtbSim
        if self.etm is not None:
            self.mtb_sim = QemuMtbSim(qmp, self.etm.image, self.etm, size)
            self.etm.timing_label = "estimé (modèle de cycles, MTB simulée)"
            if session is not None:
                self.mtb_sim.last_pc = session.register("pc")

    def setup_mtb(self, session, base: int, buffer: int, size: int):
        """MTB d'une vraie cible (arrêtée) : programmation, puis lecture à chaque arrêt."""
        from .mtb_capture import MtbCapture
        if self.etm is None:
            raise RuntimeError("trace d'instructions indisponible (capstone requis)")
        self.mtb = MtbCapture(session, self.etm.image, base, buffer, size)
        self.etm.timing_label = "estimé (modèle de cycles, MTB)"
        self.attach_session(session)
        return self.mtb.state

    def source_label(self) -> str:
        """D'où vient la trace d'instructions (libellé des vues)."""
        if self.mtb_sim is not None:
            return "MTB simulée (QEMU" + (f", {self.mtb_sim.size} octets)" if self.mtb_sim.size
                                          else ", tampon illimité)")
        if self.mtb is not None:
            return "MTB"
        if self.etm_sim is not None:
            return "ETM simulé (QEMU)"
        return "capture ETM" if self.active else "trace ETM"

    # ------------------------------------------------------------------ cycle des exécutions
    def _etm_feed(self, data: bytes) -> None:
        self.etm.feed(data)
        self._etm_changed()

    def _etm_changed(self) -> None:
        for fn in list(self.etm_listeners):
            self.post(fn)

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
        for sim in self._sims():
            if sim.path is None:
                try:
                    sim.start_run()
                except Exception as exc:  # noqa: BLE001
                    self.on_error(f"trace simulée : {exc}")

    def on_target_stopped(self) -> None:
        """Fin d'une exécution, en tâche de fond : sous QEMU, conversion du journal en trace ;
        sur une cible avec MTB, lecture et remise à zéro du tampon."""
        jobs = [s for s in self._sims() if s.path is not None]
        mtb = self.mtb
        if not jobs and mtb is None:
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
        """Programme l'ETM et le TPIU parallèle (cible arrêtée)."""
        log = [f"{name} = {val:#010x}" for name, val in cfg.apply(session)]
        log.append(cfg.summary())
        return log

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
