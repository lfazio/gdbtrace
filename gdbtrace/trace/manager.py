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

    def enable_qemu_sim(self, qmp) -> None:
        from .etm.qemu_sim import QemuEtmSim
        from .etm.trace import EtmTrace
        if self.etm is not None:
            if not self.etm.cycle_accurate:      # la simulation encode en cycle-accurate
                self.etm = EtmTrace(self.etm.image, self.etm.symbols, cycle_accurate=True)
            self.etm.simulated = True            # cycles du modèle : durées estimées
            self.etm_sim = QemuEtmSim(qmp, self.etm.image, self.etm)

    def _etm_feed(self, data: bytes) -> None:
        self.etm.feed(data)
        self._etm_changed()

    def _etm_changed(self) -> None:
        for fn in list(self.etm_listeners):
            self.post(fn)

    def attach_session(self, session) -> None:
        """Branche la simulation ETM sur la session : journal activé avant chaque reprise,
        converti à chaque arrêt."""
        session.add_resume_hook(self.on_target_running)
        session.on_event(lambda kind, data: self.on_target_stopped() if kind == "stopped" else None)

    def wait_etm(self, timeout: float = 30.0) -> bool:
        """Attend la fin de la conversion de la trace de la dernière exécution."""
        import time
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if not self.etm_busy and (self.etm_sim is None or self.etm_sim.path is None):
                return True
            time.sleep(0.02)
        return False

    def on_target_running(self) -> None:
        if self.etm_sim is not None and self.etm_sim.path is None:
            try:
                self.etm_sim.start_run()
            except Exception as exc:  # noqa: BLE001
                self.on_error(f"ETM simulé : {exc}")

    def on_target_stopped(self) -> None:
        """Fin d'une exécution : sous QEMU, conversion du journal en trace ETM (en tâche de fond)."""
        if self.etm_sim is None or self.etm_sim.path is None:
            return
        import threading

        def work():
            try:
                self.etm_sim.end_run()
            except Exception as exc:  # noqa: BLE001
                self.post(self.on_error, f"ETM simulé : {exc}")
            finally:
                self.etm_busy = False
            self._etm_changed()
        self.etm_busy = True          # avant le thread : wait_etm() ne peut pas le manquer
        threading.Thread(target=work, name="etm-sim", daemon=True).start()

    def configure_etm(self, cfg, session) -> list[str]:
        """Programme l'ETM et le TPIU parallèle (cible arrêtée)."""
        log = [f"{name} = {val:#010x}" for name, val in cfg.apply(session)]
        log.append(cfg.summary())
        return log

    def close(self) -> None:
        self.stop()
        if self.etm_sim is not None:
            try:
                self.etm_sim.close()
            except Exception:  # noqa: BLE001
                pass
            self.etm_sim = None

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
            self.start_tcp(host if isinstance(target, RemoteTarget) else "127.0.0.1", port)
            log.append(f"capture SWO TCP {port} démarrée")
        log.append(cfg.summary())
        return log
