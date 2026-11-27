"""Session de débogage : API haut niveau au-dessus de GdbMI.

C'est l'objet que manipulent l'interface graphique et les scripts (commandes GdbTrace :
Go, Break, Step, Register, Data.dump, Var.view...).
Toutes les méthodes sont utilisables depuis n'importe quel thread.
"""
from __future__ import annotations

import contextlib
import os
import re
import shlex
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable

from .gdb_backend import GdbError, GdbMI
from .target import TargetServer

EventFn = Callable[[str, dict], None]


@dataclass
class Frame:
    pc: int
    func: str | None
    file: str | None
    fullname: str | None
    line: int | None


@dataclass
class ProgramResult:
    elf: str
    size: int = 0                    # octets écrits
    seconds: float = 0.0
    verified: bool | None = None     # None : pas de vérification
    mismatched: list[str] = field(default_factory=list)
    verify_error: str = ""
    loaders: list[str] = field(default_factory=list)   # écrit par les chargeurs de flash

    def summary(self) -> str:
        if self.mismatched:
            v = "ÉCHEC de la vérification : " + ", ".join(self.mismatched)
        elif self.verified:
            v = "vérifié"
        else:
            v = "non vérifié" + (f" ({self.verify_error.splitlines()[-1]})" if self.verify_error else "")
        via = f" ; {' ; '.join(self.loaders)}" if self.loaders else ""
        return (f"{os.path.basename(self.elf)} programmé : {self.size} octets en {self.seconds:.1f} s, "
                f"{v}{via}")


@dataclass
class ThreadInfo:
    """Un thread vu par gdb : pthread d'un programme natif, tâche RTOS exposée par OpenOCD."""
    id: str                          # numéro gdb
    name: str                        # nom (pthread_setname_np, /proc/<pid>/task/<lwp>/comm)
    target_id: str                   # « Thread 0x7ffff7da06c0 (LWP 3542062) »
    lwp: int | None
    state: str                       # stopped | running
    core: int | None
    frame: Frame | None
    current: bool = False


@dataclass
class Breakpoint:
    number: str
    type: str
    enabled: bool
    addr: str | None
    func: str | None
    file: str | None
    line: int | None
    hits: int
    condition: str | None = None     # Break.Set /CONDition
    ignore: int = 0                  # passages encore ignorés avant l'arrêt
    count: int | None = None         # Break.Set /COUNT : arrêt au Nᵉ passage
    action: str = ""                 # description de l'action (message, script Python)
    expr: str | None = None          # expression surveillée (watchpoint)


@dataclass
class BreakAction:
    """Action d'un breakpoint ou d'un watchpoint (Break.Set /CMD), exécutée à chaque arrêt sur
    lui pendant un Go, cible arrêtée : `log` (texte, `{expr}` / `{expr:fmt}` remplacés par leur
    valeur), `fn(arret)` (vrai : arrêt ; texte : journalisé). La cible repart ensuite, sauf
    `stop` ou si `fn` le demande."""
    log: str | None = None
    fn: Callable[[dict], Any] | None = None
    stop: bool = False

    def describe(self) -> str:
        parts = [f"« {self.log} »"] if self.log else []
        if self.fn is not None:
            parts.append("script Python")
        if self.stop:
            parts.append("→ arrêt")
        elif self.fn is None:
            parts.append("→ continue")
        return " ".join(parts)


_FIELD = re.compile(r"\{\{|\}\}|\{([^{}]+)\}")


def expand_log(text: str, value: Callable[[str, str], str]) -> str:
    """Message d'action : `{expr}` → valeur de l'expression C, `{expr:fmt}` dans un format de
    core/formats.py (hex, dec, bin…, collé aux deux-points : `{a ? b : c}` reste une
    expression) ; `{{` et `}}` → accolades. `value(expr, format)` évalue (cible arrêtée) ; une
    erreur s'affiche à la place de la valeur."""
    from .formats import normalize

    def field_(m: re.Match) -> str:
        tok = m.group(0)
        if tok in ("{{", "}}"):
            return tok[0]
        body, fmt = m.group(1), "natural"
        expr, sep, suffix = body.rpartition(":")
        if sep and suffix and suffix == suffix.strip() and expr.strip() and not expr.endswith(":"):
            try:
                fmt = normalize(suffix)
            except ValueError:
                expr = body
        else:
            expr = body
        try:
            return value(expr.strip(), fmt)
        except Exception as exc:  # noqa: BLE001 - une expression fautive n'arrête pas le message
            return f"<erreur : {exc}>"
    return _FIELD.sub(field_, text)


def _stop_number(payload: dict) -> str | None:
    """Numéro du breakpoint ou du watchpoint qui a arrêté la cible."""
    if payload.get("bkptno"):
        return str(payload["bkptno"])
    for key in ("wpt", "hw-awpt", "hw-rwpt"):
        if isinstance(payload.get(key), dict):
            return payload[key].get("number")
    return None


_ACTION_REASONS = ("breakpoint-hit", "watchpoint-trigger", "read-watchpoint-trigger",
                   "access-watchpoint-trigger")
#: watchpoints de gdb (-break-list) et raison d'arrêt de chacun
_WATCH_REASON = {"hw watchpoint": ("watchpoint-trigger", "wpt"),
                 "read watchpoint": ("read-watchpoint-trigger", "hw-rwpt"),
                 "acc watchpoint": ("access-watchpoint-trigger", "hw-awpt")}
DFSR = 0xE000ED30                 # Debug Fault Status Register (Cortex-M), bits écrits à 1 pour effacer
#: délai pendant lequel un SIGINT non demandé est pris pour une interruption doublée
STALE_INTERRUPT_S = 30.0
DFSR_DWTTRAP = 1 << 2


def _truthy(value: str) -> bool:
    v = str(value).split()[0] if str(value).split() else ""
    try:
        return int(v, 0) != 0
    except ValueError:
        return v not in ("false", "")
_KEEP: Any = object()


def _int(v: Any) -> int:
    return int(str(v), 0)


def _mi_quote(text: str) -> str:
    """Contenu d'une chaîne C de GDB/MI (« \\ » et « \" » échappés)."""
    return text.replace("\\", "\\\\").replace('"', '\\"')


class Session:
    def __init__(self, gdb_path: str = "gdb-multiarch"):
        self.gdb = GdbMI(gdb_path)
        self.target: TargetServer | None = None
        self.elf: str | None = None
        #: ELF dont les symboles s'ajoutent à ceux de l'ELF de la session : [(chemin, décalage)]
        self.extra_symbols: list[tuple[str, int]] = []
        self.state = "disconnected"  # disconnected | stopped | running | exited
        self.non_stop = False                    # programme natif en mode non-stop
        self.focus: str | None = None            # non-stop : thread suivi (celui des vues)
        self.thread_states: dict[str, str] = {}  # non-stop : état de chaque thread
        self.stop_reason: dict = {}
        #: programme natif terminé : code de sortie (None : tué par un signal, ou pas fini)
        self.exit_code: int | None = None
        self._stopped = threading.Event()
        self._listeners: list[EventFn] = []
        self._resume_hooks: list[Callable[[], None]] = []
        #: arrêts visibles émis (événement « stopped ») : une mesure par arrêt (chrono)
        self.stop_count = 0
        self._reg_names: list[str] = []
        # arrêts internes (MTB en AUTOHALT) : la cible s'arrête d'elle-même, le propriétaire
        # de l'intercepteur traite l'arrêt puis relance ; vu de l'extérieur, elle tourne
        self._intercept: Callable[[dict], bool] | None = None
        self._internal = False
        self._internal_reason: dict = {}
        self._resuming = False
        self._quiet_running = False
        self._halt_requested = False
        self._halt_wanted = False
        self._continuing = False        # dernière reprise : Go (et non un pas ou un « until »)
        self._lock = threading.Lock()
        # breakpoints à action et à compteur ; watchpoints de Go.Till (supprimés à la reprise
        # suivante, masqués de breakpoints()) ; messages des actions
        self._actions: dict[str, BreakAction] = {}
        self._counts: dict[str, int] = {}
        self._transient: set[str] = set()
        #: chargeurs de flash (core/flashalgo.FlashLoader) : les segments de leur mémoire
        #: passent par leur algorithme, exécuté sur la cible, plutôt que par le serveur
        self.flash_loaders: list = []
        #: watchpoints posés au dernier Go et valeur de leur expression (voir _watch_trap)
        self._watch_vals: dict[str, dict] = {}
        #: interruption envoyée par halt() mais doublée par un autre arrêt (breakpoint) : le
        #: serveur peut l'appliquer après la reprise suivante (instant du constat, ou None)
        self._stale_interrupt: float | None = None
        self.action_log: deque[str] = deque(maxlen=1000)
        # boucle de pas (Step.Till, Step.Change) : une seule reprise vue de l'extérieur
        self._looping = False
        self._loop_first = False
        self._loop_inflight = False
        self._loop_stop = threading.Event()
        self._loop_reason: dict = {}
        # sortie console de gdb retenue pendant la marche quand des actions sont posées : la
        # bannière « Breakpoint N, … » d'un arrêt d'action est jetée, le reste livré à l'arrêt
        self._held: list[str] = []
        self._pty: tuple[int, int, str] | None = None   # programme natif : (maître, esclave, nom)

    # ------------------------------------------------------------------ événements
    # ------------------------------------------------------------------ non-stop
    # Programme natif en mode non-stop (`NativeTarget.non_stop`) : chaque thread s'arrête et
    # repart seul, les autres continuent. gdb envoie un *running / *stopped par thread.
    # L'état de la session est celui du thread **suivi** (`focus`, celui que montrent les vues) :
    # arrêté si lui l'est ; un thread qui s'arrête pendant que le suivi tourne devient le suivi
    # (« stopped ») ; les autres changements ne sont que des événements « threads ». gdb ne
    # change pas son thread courant quand un autre s'arrête : `select_focus()` le fait (vues,
    # wait_stopped). Go et Break valent pour tous les threads (--all), les pas pour le suivi.

    def _on_mi_nonstop(self, msg: str, payload: dict) -> None:
        ids = payload.get("stopped-threads") if msg == "stopped" else None
        tid = str(payload.get("thread-id", "all"))
        if msg == "running":
            if tid == "all":
                for k in list(self.thread_states):
                    self.thread_states[k] = "running"
            else:
                self.thread_states[tid] = "running"
            if tid == "all" or tid == self.focus or self.focus is None:
                if self.state != "running":
                    self.state = "running"
                    self._stopped.clear()
                    self._emit("running", payload)
                return
            self._emit("threads", {"event": "thread-running", "id": tid})
            return
        stopped = ([str(x) for x in ids] if isinstance(ids, list)
                   else list(self.thread_states) if ids == "all" else [tid])
        for k in stopped:
            self.thread_states[k] = "stopped"
        with self._lock:
            requested, self._halt_requested = self._halt_requested, False
        if self.state == "running" or tid == self.focus or self.focus is None:
            if tid != "all":
                self.focus = tid
            self.state = "stopped"
            self.stop_reason = payload
            self._emit("stopped", payload)
            self._stopped.set()
        else:
            self._emit("threads", {"event": "thread-stopped", "id": tid, "requested": requested})

    def select_focus(self) -> None:
        """Non-stop : le thread courant de gdb devient le thread suivi (après son arrêt)."""
        if self.non_stop and self.focus and self.thread_states.get(self.focus) == "stopped":
            try:
                self.gdb.command(f"-thread-select {self.focus}")
            except GdbError:
                pass

    def stop_thread(self, thread_id: str) -> None:
        """Non-stop : arrête ce thread seul (les autres continuent)."""
        self._need_non_stop()
        self.gdb.command(f"-exec-interrupt --thread {thread_id}")
        self._reselect_focus(thread_id)

    def resume_thread(self, thread_id: str) -> None:
        """Non-stop : relance ce thread seul."""
        self._need_non_stop()
        if str(thread_id) == self.focus:
            self._continuing = True
            self._stopped.clear()
        self._before_resume()
        self.gdb.command(f"-exec-continue --thread {thread_id}")
        self._reselect_focus(thread_id)

    def _reselect_focus(self, used: str) -> None:
        """`--thread` a changé le thread courant de gdb : le suivi le redevient s'il est arrêté
        (sinon les vues liraient les registres d'un autre thread, peut-être en marche)."""
        if str(used) != self.focus:
            self.select_focus()

    def _need_non_stop(self) -> None:
        if not self.non_stop:
            raise GdbError("un thread ne s'arrête ou ne repart seul qu'en mode non-stop (--non-stop)")

    def on_event(self, fn: EventFn) -> None:
        """fn(kind, data) avec kind ∈ stopped, running, output, breakpoints, disconnected,
        exited (programme natif terminé), threads (thread créé, terminé ou sélectionné)."""
        self._listeners.append(fn)

    def add_resume_hook(self, fn: Callable[[], None]) -> None:
        """fn() est appelée juste AVANT chaque reprise de la cible (go, step, next, finish,
        until), quel que soit l'appelant (GUI, console, script) : ex. démarrer une trace
        sans perdre les premières instructions."""
        self._resume_hooks.append(fn)

    def set_stop_interceptor(self, fn: Callable[[dict], bool] | None) -> None:
        """fn(raison) est appelée dans le thread lecteur (sans bloquer ni appeler gdb) pour
        chaque arrêt `signal-received` que le débogueur n'a pas demandé, pendant un Go (un pas à
        pas interrompu reste visible : le relancer en Go perdrait le pas). Si elle renvoie vrai,
        l'arrêt est **interne** : aucun événement, `state` reste « running », `wait_stopped()`
        continue d'attendre ; son propriétaire doit ensuite appeler `resume_internal()` ou
        `deliver_stop()`. Un arrêt demandé (`halt()`, point d'arrêt, pas à pas) n'est jamais
        intercepté."""
        self._intercept = fn

    @property
    def internal_stop(self) -> bool:
        return self._internal

    def resume_internal(self) -> None:
        """Fin d'un arrêt interne : relance la cible sans hooks ni événement `running`. Si
        `halt()` a été demandé entre-temps, l'arrêt est livré à la place."""
        with self._lock:
            if not self._internal:
                return
            if self._halt_wanted:
                self._halt_wanted = False
                deliver = True
            else:
                deliver = False
                self._internal = False
                self._resuming = True
                self._quiet_running = True
        if deliver:
            self.deliver_stop(force=True)
            return
        try:
            self.gdb.command("-exec-continue")
        except GdbError:
            self._quiet_running = False
            raise
        finally:
            with self._lock:
                self._resuming = False
                wanted, self._halt_wanted = self._halt_wanted, False
                if wanted:
                    self._halt_requested = True
        if wanted:                          # halt() arrivé pendant la relance : on l'exécute
            self.gdb.command("-exec-interrupt")

    def deliver_stop(self, force: bool = False) -> None:
        """L'arrêt intercepté devient visible (ce n'était pas la MTB, ou halt() le demande)."""
        with self._lock:
            if not self._internal and not force:
                return
            self._internal = self._halt_wanted = False
            self.state = "stopped"
            self.stop_reason = self._internal_reason
        self._emit("stopped", self.stop_reason)
        self._stopped.set()

    def _before_resume(self) -> None:
        self._drop_transient()
        self._run_resume_hooks()

    def _run_resume_hooks(self) -> None:
        for fn in list(self._resume_hooks):
            try:
                fn()
            except Exception:  # noqa: BLE001 - une trace en échec n'empêche pas d'exécuter
                pass

    def _emit(self, kind: str, data: dict) -> None:
        if kind == "stopped":
            self.stop_count += 1
        for fn in list(self._listeners):
            try:
                fn(kind, data)
            except Exception:
                pass

    def _on_mi(self, rec: dict) -> None:
        t, msg, payload = rec.get("type"), rec.get("message"), rec.get("payload")
        if t == "notify" and msg == "stopped" and (payload or {}).get("reason", "").startswith("exited"):
            # programme terminé (exited-normally, exited, exited-signalled) : natif, ou fin
            # signalée par le serveur d'une cible (rare : semihosting, serveur qui s'arrête)
            payload = payload or {}
            code = payload.get("exit-code")
            self.exit_code = int(code, 8) if code else (0 if payload["reason"] == "exited-normally" else None)
            if self.target is not None and getattr(self.target, "native", False):
                self.target.pid = None
            # ses dernières lignes avant sa fin ; avant de réveiller une boucle de pas, qui
            # sinon se finirait pendant cette attente sur un « stopped » inventé
            self._settle_tty()
            with self._lock:
                # plus rien à reprendre : arrêt interne, halt en attente, boucle de pas
                self._internal = self._resuming = False
                self._halt_requested = self._halt_wanted = False
                held, self._held = self._held, []
                self.state = "exited"
                self.stop_reason = payload
                if self._looping:               # Step.Till / Step.Change : la boucle s'arrête
                    self._loop_reason = payload
                    self._loop_inflight = False
                    self._loop_stop.set()
            for text in held:                   # sortie retenue pendant la marche (actions)
                self._emit("output", {"stream": "console", "text": text})
            self._emit("exited", payload)
            self._stopped.set()
        elif t == "notify" and msg in ("stopped", "running") and self.non_stop:
            self._on_mi_nonstop(msg, payload or {})
        elif t == "notify" and msg == "stopped":
            payload = payload or {}
            with self._lock:
                if self._looping:               # pas intermédiaire d'une boucle : silencieux
                    self._loop_reason = payload
                    self._loop_inflight = False
                    self._loop_stop.set()
                    return
                requested, self._halt_requested = self._halt_requested, False
                if requested and payload.get("reason") != "signal-received":
                    self._stale_interrupt = time.monotonic()
                free = not requested and self.state == "running" and self._continuing
                if (free and self._watch_vals and payload.get("reason") == "signal-received"
                        and payload.get("signal-name") == "SIGTRAP"):
                    # peut-être un watchpoint que le serveur n'a pas attribué : arrêt interne
                    self._internal, self._internal_reason = True, payload
                    self._held.clear()
                    threading.Thread(target=self._watch_trap, args=(payload,),
                                     name="gdbtrace-watch", daemon=True).start()
                    return
                act = (self._actions.get(_stop_number(payload) or "")
                       if free and payload.get("reason") in _ACTION_REASONS else None)
                if act is not None:             # breakpoint à action : arrêt interne
                    self._internal, self._internal_reason = True, payload
                    self._held.clear()
                    threading.Thread(target=self._run_action, args=(act, payload),
                                     name="gdbtrace-action", daemon=True).start()
                    return
                if (self._intercept is not None and not requested and self.state == "running"
                        and self._continuing and payload.get("reason") == "signal-received"):
                    self._internal, self._internal_reason = True, payload
                    try:
                        internal = bool(self._intercept(payload))
                    except Exception:  # noqa: BLE001 - un intercepteur en échec : arrêt normal
                        internal = False
                    if internal:
                        return
                    self._internal = False
                if (free and self._stale_interrupt is not None
                        and payload.get("reason") == "signal-received"
                        and payload.get("signal-name") == "SIGINT"):
                    # l'interruption d'un halt() doublé par un breakpoint, appliquée par le
                    # serveur après la reprise (OpenOCD) : personne ne l'a demandée, on repart
                    stale, self._stale_interrupt = self._stale_interrupt, None
                    if time.monotonic() - stale < STALE_INTERRUPT_S:
                        self._internal, self._internal_reason = True, payload
                        threading.Thread(target=self._resume_after_stale, name="gdbtrace-sigint",
                                         daemon=True).start()
                        return
                held, self._held = self._held, []
            for text in held:                   # sortie retenue pendant la marche
                self._emit("output", {"stream": "console", "text": text})
            self.state = "stopped"
            self.stop_reason = payload
            # abonnés d'abord (ex. la trace ETM se marque « en conversion ») : quand halt() ou
            # wait_stopped() rendent la main, leur réaction à l'arrêt est déjà engagée
            self._emit("stopped", self.stop_reason)
            self._stopped.set()
        elif t == "notify" and msg == "running":
            self.state = "running"
            self._stopped.clear()
            if self._looping:                   # boucle de pas : un seul « running »
                if not self._loop_first:
                    return
                self._loop_first = False
            if self._quiet_running:             # relance après un arrêt interne : silencieuse
                self._quiet_running = False
                return
            self._emit("running", payload or {})
        elif t == "notify" and msg and msg.startswith("breakpoint-"):
            self._emit("breakpoints", payload or {})
        elif t == "notify" and msg in ("thread-created", "thread-exited", "thread-selected"):
            if self.non_stop and msg == "thread-selected" and str((payload or {}).get("id")) != self.focus:
                return                          # --thread d'une commande : pas un choix de l'utilisateur
            if self.non_stop and msg != "thread-selected":
                tid = str((payload or {}).get("id"))
                if msg == "thread-created":
                    self.thread_states[tid] = "running" if self.state == "running" else "stopped"
                else:
                    self.thread_states.pop(tid, None)
                    if tid == self.focus:
                        self.focus = None
            self._emit("threads", {"event": msg, **(payload or {})})
        elif t == "notify" and msg == "gdb-exited":
            self.state = "disconnected"
            self._stopped.set()
            self._loop_stop.set()
            self._emit("disconnected", {})
        elif t in ("console", "target", "output"):
            if t == "console" and self._actions and self.state == "running" and not self._looping:
                self._held.append(payload or "")
                return
            self._emit("output", {"stream": t, "text": payload or ""})
        elif t == "log":
            self._emit("output", {"stream": "log", "text": payload or ""})

    # ------------------------------------------------------------------ connexion
    def connect(self, elf: str, target: TargetServer, load: bool | None = None) -> None:
        self.elf = elf
        self.target = target
        target.start()
        self.gdb.add_listener(self._on_mi)
        self.gdb.start()
        self.non_stop = bool(getattr(target, "non_stop", False))
        if self.non_stop:                         # avant tout lancement du programme
            self.gdb.command("-gdb-set mi-async on")
            self.gdb.command("-gdb-set non-stop on")
        self.gdb.command(f'-file-exec-and-symbols "{elf}"')
        if target.native and getattr(target, "remote_native", False):
            self._connect_remote_native(target)  # programme natif servi par un gdbserver
        elif target.native and getattr(target, "attach_pid", None):
            self._attach(target.attach_pid)      # processus déjà lancé (--pid)
        elif target.native:
            self._run(start=True)                # programme natif : arrêté au début de main
        else:
            self.gdb.command(f"-target-select extended-remote {target.remote}", timeout=20)
            self.state = "stopped"
            self._stopped.set()
            if load if load is not None else target.needs_load:
                self.load()
                target.after_load(self)          # image en RAM : sp / pc à ses vecteurs
        self._reg_names = self.gdb.command("-data-list-register-names").get("register-names", [])
        self._emit("stopped", {"reason": "connected"})

    def disconnect(self, halt: bool = True) -> None:
        """Ferme gdb et le serveur. `halt=False` : la cible continue de tourner (une sonde
        n'arrête pas le cœur quand on la quitte)."""
        try:
            if self.state == "running" and halt:
                try:
                    self.halt()
                except (GdbError, TimeoutError):   # cible ou gdb qui ne répond plus : on ferme quand même
                    pass
            if getattr(self.target, "attach_pid", None) and self.state in ("stopped", "running"):
                try:                            # processus attaché : il continue sans nous
                    if self.state == "running":
                        self.halt()
                    self.gdb.command("-target-detach", timeout=10)
                except (GdbError, TimeoutError):
                    pass
            self.gdb.stop()
        finally:
            if self.target:
                self.target.stop()
            if self._pty is not None:
                for fd in self._pty[:2]:
                    try:
                        os.close(fd)
                    except OSError:
                        pass
                self._pty = None
            self.state = "disconnected"
            self._emit("disconnected", {})

    def load(self) -> None:
        self.gdb.command("-target-download", timeout=120)

    def program(self, elf: str, verify: bool = True) -> "ProgramResult":
        """Programme `elf` dans la cible : arrêt, reset (cible arrêtée au vecteur de reset),
        écriture par le serveur gdb (`load` : flash avec OpenOCD/J-Link/pyOCD, RAM sous QEMU),
        vérification optionnelle (`compare-sections`), puis reset. gdb prend aussi les
        symboles de cet ELF. Une image HEX, S-record ou binaire (`fw.bin@0x08000000`) passe
        par `program_image` : les symboles restent ceux de l'ELF de la session."""
        from .memtools import is_image
        if is_image(elf):
            return self.program_image(elf, verify)
        import time
        t0 = time.monotonic()
        if self.state == "running":
            self.halt()
        self.gdb.command(f'-file-exec-and-symbols "{elf}"')
        self.elf = elf
        for path, offset in self.extra_symbols:  # `file` oublie les symboles ajoutés
            self._add_symbol_file(path, offset)
        if self.target is not None:
            if hasattr(self.target, "elf"):      # QEMU : relancé plus tard avec ce firmware
                self.target.elf = elf
        if self.flash_loaders:                  # une partie de l'image par un algorithme
            from .flashalgo import split_segments
            from .memtools import read_image
            segments = read_image(elf)
            if split_segments(segments, self.flash_loaders)[0]:
                return self._program_segments(elf, segments, verify, t0)
        if self.target is not None and self.target.needs_load:   # sonde : flash écrite au reset
            self.target.reset(self)
        res = self.gdb.command("-target-download", timeout=300)
        result = ProgramResult(elf, int(res.get("load-size", 0) or 0))
        if verify and getattr(self.target, "verify_by_read", False):
            try:
                result.mismatched = self.verify_by_read(elf)
                result.verified = not result.mismatched
            except GdbError as exc:
                result.verify_error = str(exc)
        elif verify:
            try:
                out = self.cli("compare-sections")
            except GdbError as exc:              # serveur sans lecture/CRC : non vérifié
                result.verify_error = str(exc)
            else:
                result.mismatched = [ln.split(",")[0].replace("Section ", "")
                                     for ln in out.splitlines() if "MIS-MATCHED" in ln]
                result.verified = "matched" in out and not result.mismatched
        if self.target is not None:
            self.target.reset(self)
        else:
            self.monitor("reset halt")
        self.cli("flushregs")
        result.seconds = time.monotonic() - t0
        self._emit("stopped", {"reason": "programmed"})
        return result

    def program_image(self, spec: str, verify: bool = True) -> "ProgramResult":
        """Programme une image HEX, S-record ou binaire (`fw.bin@0x08000000`) : comme
        `program`, par le `load` de gdb (flash par le serveur), sur un Intel HEX écrit pour
        l'occasion ; vérification par relecture. Les symboles ne changent pas."""
        import tempfile
        import time

        from .memtools import compare, format_ihex, read_image
        t0 = time.monotonic()
        segments = read_image(spec)
        if self.state == "running":
            self.halt()
        if self.flash_loaders:
            from .flashalgo import split_segments
            if split_segments(segments, self.flash_loaders)[0]:
                return self._program_segments(spec, segments, verify, t0)
        if self.target is not None and self.target.needs_load:
            self.target.reset(self)
        fd, tmp = tempfile.mkstemp(suffix=".hex", prefix="gdbtrace-")
        try:
            with os.fdopen(fd, "w", encoding="ascii") as fh:
                fh.write(format_ihex(segments))
            self.gdb.console(f'load "{tmp}"', timeout=300)
        finally:
            os.unlink(tmp)
        result = ProgramResult(spec, sum(len(d) for _a, d in segments))
        if verify:
            try:
                cmp = compare(self.read_memory, spec)
            except GdbError as exc:
                result.verify_error = str(exc)
            else:
                result.verified = cmp.same
                if not cmp.same:
                    result.mismatched = [cmp.summary()]
        if self.target is not None:
            self.target.reset(self)
        else:
            self.monitor("reset halt")
        self.cli("flushregs")
        result.seconds = time.monotonic() - t0
        self._emit("stopped", {"reason": "programmed"})
        return result

    def _program_segments(self, name: str, segments, verify: bool, t0: float) -> "ProgramResult":
        """Programmation dont une partie passe par les chargeurs de flash : reset (cible
        arrêtée, périphériques à leur état de départ), segments de leur mémoire par leur
        algorithme, le reste par le `load` de gdb (Intel HEX temporaire), vérification
        (fonction de l'algorithme, sinon relecture), reset."""
        import tempfile
        import time

        from .flashalgo import FlashAlgoError, split_segments
        from .memtools import format_ihex
        if self.target is not None:
            self.target.reset(self)
        else:
            self.monitor("reset halt")
        covered, rest = split_segments(segments, self.flash_loaders)
        result = ProgramResult(name, sum(len(d) for _a, d in segments))
        for loader, segs in covered.items():
            try:
                st = loader.program(segs)
            except (FlashAlgoError, GdbError, TimeoutError) as exc:
                raise FlashAlgoError(f"{os.path.basename(loader.algo.path)} : {exc}") from exc
            result.loaders.append(f"{st['octets']} octets par {os.path.basename(loader.algo.path)} "
                                  f"({st['secteurs']} secteur(s), {st['pages']} page(s), "
                                  f"{st['secondes']:.1f} s)")
        if rest:
            fd, tmp = tempfile.mkstemp(suffix=".hex", prefix="gdbtrace-")
            try:
                with os.fdopen(fd, "w", encoding="ascii") as fh:
                    fh.write(format_ihex(rest))
                self.gdb.console(f'load "{tmp}"', timeout=300)
            finally:
                os.unlink(tmp)
        if verify:
            try:
                for loader, segs in covered.items():
                    result.mismatched += loader.verify(segs)
                for a, d in rest:
                    if self.read_memory(a, len(d)) != d:
                        result.mismatched.append(f"{a:#x}+{len(d):#x}")
                result.verified = not result.mismatched
            except (FlashAlgoError, GdbError, TimeoutError) as exc:
                result.verify_error = str(exc)
        if self.target is not None:
            self.target.reset(self)
        else:
            self.monitor("reset halt")
        self.cli("flushregs")
        result.seconds = time.monotonic() - t0
        self._emit("stopped", {"reason": "programmed"})
        return result

    # ------------------------------------------------------------------ symboles ajoutés
    def add_symbols(self, path: str, offset: int = 0) -> None:
        """Symboles d'un autre ELF (bootloader, bibliothèque chargée à part, image non
        sécurisée…) en plus de ceux de l'ELF de la session : `add-symbol-file` de gdb, gardés
        après une programmation. `offset` : décalage de chargement."""
        path = os.path.abspath(path)
        if not os.path.isfile(path):
            raise RuntimeError(f"ELF introuvable : {path}")
        self._add_symbol_file(path, offset)
        self.extra_symbols = [(p, o) for p, o in self.extra_symbols if p != path] + [(path, offset)]

    def _add_symbol_file(self, path: str, offset: int) -> None:
        opt = f" -o {offset:#x}" if offset else ""
        self.gdb.console(f'add-symbol-file "{path}"{opt}')

    def verify_by_read(self, elf: str) -> list[str]:
        """Vérification par relecture (serveur dont le CRC de `compare-sections` n'est pas
        fiable : ST-LINK_gdbserver sur un STM32N6) : chaque section chargée est relue à son
        adresse de chargement et comparée à l'ELF ; retourne les sections différentes."""
        from elftools.elf.elffile import ELFFile
        bad = []
        with open(elf, "rb") as fh:
            e = ELFFile(fh)
            segs = [g for g in e.iter_segments() if g["p_type"] == "PT_LOAD" and g["p_filesz"]]
            for sec in e.iter_sections():
                if sec["sh_type"] != "SHT_PROGBITS" or not sec["sh_flags"] & 0x2 or not sec["sh_size"]:
                    continue                    # SHF_ALLOC, avec contenu
                seg = next((g for g in segs if g["p_vaddr"] <= sec["sh_addr"]
                            and sec["sh_addr"] + sec["sh_size"] <= g["p_vaddr"] + g["p_filesz"]), None)
                if seg is None:
                    continue
                lma = seg["p_paddr"] + sec["sh_addr"] - seg["p_vaddr"]
                if self.read_memory(lma, sec["sh_size"]) != sec.data():
                    bad.append(sec.name)
        return bad

    def monitor(self, cmd: str) -> str:
        """Commande `monitor` brute vers le serveur (OpenOCD : reset halt, flash...)."""
        return self.gdb.console(f"monitor {cmd}")

    def cli(self, cmd: str) -> str:
        """Commande CLI gdb arbitraire (porte de sortie)."""
        return self.gdb.console(cmd)

    # ------------------------------------------------------------------ programme natif
    def _run(self, start: bool = True, timeout: float = 30.0) -> None:
        """Lance (ou relance) le programme natif ; `start` : arrêt au début de `main`."""
        t = self.target
        if getattr(t, "preloaded", False):
            raise GdbError("programme lancé par le gdbserver lui-même : relancer le serveur "
                           "(ou le lancer avec --multi pour que GdbTrace relance le programme)")
        if not getattr(t, "remote_native", False):   # la sortie d'un programme distant reste
            self._inferior_tty()                     # sur le terminal du gdbserver
        self.gdb.command("-exec-arguments " + shlex.join(t.args))
        if t.cwd:
            self.cli(f"set cwd {t.cwd}")
        self.exit_code = None
        self._continuing = not start            # relance par Go ; arrêt à main : non
        self._stopped.clear()
        self._before_resume()
        self.gdb.command("-exec-run --start" if start else "-exec-run", timeout=timeout)
        if start:
            self.wait_stopped(timeout)
        t.pid = self.inferior_pid()

    def _connect_remote_native(self, t) -> None:
        """gdbserver : avec un programme déjà chargé (`gdbserver :port prog`), jusqu'au début
        de `main` ; sinon (`--multi`), lancé par gdb, arrêté au début de `main`."""
        self.gdb.command(f"-target-select extended-remote {t.remote}", timeout=20)
        self.state = "stopped"
        self._stopped.set()
        pid = self.inferior_pid()
        if pid:                                  # chargé par le serveur, arrêté à l'entrée
            t.preloaded, t.pid = True, pid
            self.gdb.command("-break-insert -t main")
            self._continuing = False
            self._stopped.clear()
            self.gdb.command("-exec-continue")
            self.wait_stopped(30)
            return
        self.cli(f"set remote exec-file {t.exec_file or self.elf}")
        self._run(start=True)

    def _attach(self, pid: int) -> None:
        """S'attache au processus `pid` (déjà lancé, même utilisateur ou CAP_SYS_PTRACE) :
        gdb l'arrête là où il en est."""
        try:
            self.gdb.command(f"-target-attach {pid}", timeout=30)
        except GdbError as exc:
            hint = ""
            try:
                with open("/proc/sys/kernel/yama/ptrace_scope") as fh:
                    if fh.read().strip() != "0":
                        hint = (" ; ptrace restreint (kernel.yama.ptrace_scope ≠ 0) : seul un "
                                "parent ou CAP_SYS_PTRACE peut s'attacher")
            except OSError:
                pass
            raise GdbError(f"attachement au processus {pid} impossible : {exc}{hint}") from None
        self.state = "stopped"
        self._stopped.set()
        self.target.pid = pid

    def _inferior_tty(self) -> None:
        """Terminal propre au programme natif. Sinon il hériterait de celui de gdb, c'est-à-dire
        des tubes MI : sa sortie se mêlerait aux enregistrements MI (une ligne sans fin les
        rendrait illisibles) et, s'il lisait son entrée, il volerait les commandes de gdb.
        Sa sortie part vers les abonnés (flux « target » : la console) ; `send_input()` lui
        écrit."""
        if self._pty is None:
            import tty
            master, slave = os.openpty()
            tty.setraw(slave)                    # ni écho ni « \r\n »
            self._pty = (master, slave, os.ttyname(slave))
            threading.Thread(target=self._read_tty, args=(master,), name="inferior-tty",
                             daemon=True).start()
        self.gdb.command(f'-inferior-tty-set "{self._pty[2]}"')

    def _read_tty(self, fd: int) -> None:
        while True:
            try:
                data = os.read(fd, 4096)
            except OSError:
                return
            if not data:
                return
            self._tty_reading = True
            try:
                self._emit("output", {"stream": "target", "text": data.decode(errors="replace")})
            finally:
                self._tty_reading = False

    def _settle_tty(self, timeout: float = 0.3) -> None:
        """Programme terminé : ce qu'il a écrit avant sa fin attend peut-être encore dans le
        pseudo-terminal ; on laisse le fil lecteur le livrer (au plus `timeout`) pour que la
        sortie précède l'événement « exited »."""
        if self._pty is None:
            return
        import select
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            try:
                pending, _w, _x = select.select([self._pty[0]], [], [], 0)
            except (OSError, ValueError):
                return
            if not pending and not getattr(self, "_tty_reading", False):
                return
            time.sleep(0.005)

    def send_input(self, text: str) -> None:
        """Programme natif : `text` arrive sur son entrée standard."""
        if self._pty is None:
            raise RuntimeError("pas de programme natif lancé")
        os.write(self._pty[0], text.encode())

    def restart(self) -> None:
        """Programme natif : relancé depuis le début, arrêté au début de `main`
        (le processus en cours, s'il y en a un, est tué)."""
        self._need_native()
        if getattr(self.target, "attach_pid", None):
            raise GdbError("processus attaché (--pid) : GdbTrace ne l'a pas lancé, il ne le "
                           "relance pas")
        if self.state == "running":
            self.halt()
        self._run(start=True)

    def _need_native(self) -> None:
        """Seul un programme natif se relance : gdb l'exécute lui-même. Une cible dont le
        serveur a signalé la fin (semihosting, serveur arrêté) ne repart pas ainsi."""
        if self.target is None or not getattr(self.target, "native", False):
            raise GdbError("programme terminé sur la cible : Go ne relance qu'un programme "
                           "natif ; reprogrammer l'ELF ou relancer la session")

    def inferior_pid(self) -> int | None:
        """PID du programme natif débogué, None s'il ne tourne pas."""
        for g in self.gdb.command("-list-thread-groups").get("groups", []):
            if g.get("pid"):
                return int(g["pid"])
        return None

    # ------------------------------------------------------------------ run-control
    def go(self) -> None:
        self._continuing = True
        if self.state == "exited" and getattr(self.target, "attach_pid", None):
            raise GdbError("le processus attaché est terminé : GdbTrace ne le relance pas")
        if self.state == "exited":              # programme natif terminé : relancé
            self._need_native()
            self._run(start=False)
            return
        self._stopped.clear()
        self._before_resume()
        self._arm_watch_check()
        self.gdb.command("-exec-continue --all" if self.non_stop else "-exec-continue")

    def halt(self, wait: float = 5.0) -> None:
        with self._lock:
            if self.state != "running":
                return
            looping, inflight = self._looping, self._loop_inflight
            internal = self._internal or self._resuming
            if looping or internal:             # boucle de pas, arrêt interne : livré à la fin
                self._halt_wanted = True
            else:
                self._halt_requested = True
        if looping:                             # pas en cours interrompu, ou plus de pas
            if inflight:
                try:
                    self.gdb.command("-exec-interrupt")
                except GdbError:                # pas terminé entre-temps
                    pass
            self.wait_stopped(wait)
            return
        if not internal:
            try:
                self.gdb.command("-exec-interrupt --all" if self.non_stop else "-exec-interrupt")
            except GdbError:
                if self.state != "running":     # arrêtée entre-temps (ex. point d'arrêt)
                    return
                raise
        self.wait_stopped(wait)

    def step(self) -> None:          # Step.single (source, entre dans les fonctions)
        self._sync("-exec-step")

    def step_over(self) -> None:     # Step.Over
        self._sync("-exec-next")

    def step_out(self) -> None:      # Go.Return
        self._sync("-exec-finish")

    def step_asm(self) -> None:      # Step.single en mode assembleur
        self._sync("-exec-step-instruction")

    def go_until(self, location: str, timeout: float | None = None) -> None:
        """`until LIEU` de gdb : ne s'arrête au lieu que dans la frame courante (sinon au
        retour de celle-ci). Pour un Go jusqu'à n'importe où : `gdbtrace.go(until=…)`."""
        self._continuing = False
        self._stopped.clear()
        self._before_resume()
        self.gdb.command(f"-exec-until {location}")
        if timeout is not None:
            self.wait_stopped(timeout)

    def go_till(self, cond: str) -> str:
        """Go.Till : Go jusqu'à ce que l'expression C `cond` **devienne** vraie, en temps réel :
        watchpoint matériel sur les variables de l'expression (gdb les trouve), conditionné par
        `cond`. Le watchpoint est retiré à la reprise suivante (masqué de breakpoints() d'ici
        là). Une expression qu'un watchpoint matériel ne peut suivre (registre…) est refusée :
        step_till() la teste pas à pas. N'attend pas l'arrêt. Retourne le numéro du watchpoint."""
        self._drop_transient()
        res = self.gdb.command(f'-break-watch "{_mi_quote(cond)}"')
        num = (res.get("wpt") or {}).get("number")
        if num is None:
            raise GdbError(f"Go.Till : watchpoint non posé ({res})")
        self._transient.add(num)
        body = self.gdb.command(f"-break-info {num}")["BreakpointTable"].get("body", [])
        if not body or not body[0].get("type", "").startswith("hw"):
            self._drop_transient()
            raise ValueError(f"Go.Till : « {cond} » ne peut pas être suivie par un watchpoint "
                             "matériel (registre, valeur sans adresse) : utiliser step_till()")
        try:
            self.gdb.command(f'-break-condition {num} "{_mi_quote(cond)}"')
        except GdbError:
            self._drop_transient()
            raise
        self._continuing = True
        self._stopped.clear()
        self._run_resume_hooks()
        self._arm_watch_check()
        self.gdb.command("-exec-continue")
        return num

    def step_till(self, cond: str, asm: bool = False, max_steps: int = 1000) -> dict:
        """Step.Till : pas à pas (ligne source, ou instruction si `asm`) jusqu'à ce que `cond`
        soit vraie, `max_steps` pas au plus. Toute expression convient (registres compris) ;
        lent : un aller-retour gdb par pas. Retourne l'arrêt final avec `steps` et `met`."""
        test = f"(int)!!({cond})"
        self.evaluate(test)                     # expression valide avant le premier pas
        return self._step_loop(lambda: self.evaluate(test) == "1", asm, max_steps,
                               {"what": "Step.Till", "expr": cond})

    def step_change(self, expr: str, asm: bool = False, max_steps: int = 1000) -> dict:
        """Step.Change : pas à pas jusqu'à ce que la valeur de `expr` change. L'arrêt final
        porte `old` et `new` (valeurs affichées par gdb), `steps` et `met`."""
        old = self.evaluate(expr)
        extra = {"what": "Step.Change", "expr": expr, "old": old}

        def changed() -> bool:
            extra["new"] = self.evaluate(expr)
            return extra["new"] != old
        return self._step_loop(changed, asm, max_steps, extra)

    @contextlib.contextmanager
    def target_calls(self, return_addr: int, sp: int, static_base: int | None = None):
        """Appels de fonctions sur la cible arrêtée (algorithmes de flash) : rend
        `call(adresse, args, timeout)` → r0. Chaque appel pose r0-r3, r9 (`static_base`), sp,
        lr (= `return_addr`, où une instruction BKPT attend et où gdb pose un breakpoint
        matériel, masqué de breakpoints()), pc, puis laisse tourner la cible jusqu'au retour.
        Silencieux : ni événement, ni resume hook (chrono, compteurs, trace simulée). Un délai
        dépassé interrompt la cible ; un arrêt ailleurs qu'au retour (faute) est une erreur."""
        with self._lock:
            if self.state != "stopped":
                raise GdbError("la cible doit être arrêtée")
            self._looping, self._loop_first, self._halt_wanted = True, False, False
        ret = return_addr & ~1
        num = None
        try:
            try:
                num = self.gdb.command(f"-break-insert -h *{ret:#x}")["bkpt"]["number"]
            except GdbError:                    # plus de comparateur matériel : logiciel
                num = self.gdb.command(f"-break-insert *{ret:#x}")["bkpt"]["number"]
            self._transient.add(num)

            def call(addr: int, args=(), timeout: float = 5.0) -> int:
                if len(args) > 4:
                    raise ValueError("quatre arguments au plus (r0-r3)")
                for i, value in enumerate(args):
                    self.write_register(f"r{i}", value & 0xFFFFFFFF)
                if static_base is not None:
                    self.write_register("r9", static_base)
                self.write_register("sp", sp)
                self.write_register("lr", ret | 1)
                self.write_register("pc", addr & ~1)
                try:
                    self.write_register("xpsr", 0x01000000)     # état Thumb, mode Thread
                except GdbError:
                    pass
                self._loop_stop.clear()
                with self._lock:
                    self._loop_inflight = True
                self.gdb.command("-exec-continue")
                if not self._loop_stop.wait(timeout):
                    try:
                        self.gdb.command("-exec-interrupt")
                    except GdbError:
                        pass
                    self._loop_stop.wait(5)
                    self.state = "stopped"
                    where = self._pc_text()
                    raise TimeoutError(f"fonction à {addr:#x} sans retour après {timeout:g} s "
                                       f"(arrêtée à {where})")
                self.state = "stopped"
                pc = int(self.evaluate("(unsigned int)$pc"), 0)
                if pc != ret:
                    raise GdbError(f"fonction à {addr:#x} arrêtée hors de son retour : pc {pc:#x} "
                                   f"({(self._loop_reason or {}).get('reason', '?')})")
                return int(self.evaluate("(unsigned int)$r0"), 0)
            yield call
        finally:
            if num is not None:
                self._transient.discard(num)
                try:
                    self.gdb.command(f"-break-delete {num}")
                except GdbError:
                    pass
            with self._lock:
                self._looping = self._loop_inflight = False
            if self.state != "disconnected":
                self.state = "stopped"
            self._stopped.set()

    def _pc_text(self) -> str:
        try:
            pc = int(self.evaluate("(unsigned int)$pc"), 0)
            return f"{pc:#x} {self.symbol_at(pc).split(' in section')[0]}"
        except Exception:  # noqa: BLE001
            return "?"

    def _step_loop(self, done: Callable[[], bool], asm: bool, max_steps: int, extra: dict) -> dict:
        """Pas répétés vus comme UNE reprise : hooks de reprise une fois, un seul `running`,
        arrêts intermédiaires silencieux (`state` reste « running »), un seul `stopped` final.
        S'arrête quand `done()` est vrai, au premier arrêt qui n'est pas une fin de pas
        (breakpoint, signal), après `max_steps` pas ou sur `halt()`."""
        cmd = "-exec-step-instruction" if asm else "-exec-step"
        with self._lock:
            if self.state != "stopped":
                raise GdbError("la cible doit être arrêtée")
            self._looping, self._loop_first, self._halt_wanted = True, True, False
        self._continuing = False
        self._stopped.clear()
        steps, met, rec, error = 0, False, dict(self.stop_reason), None
        try:
            self._before_resume()
            while steps < max_steps:
                with self._lock:
                    if self._halt_wanted:
                        break
                    self._loop_inflight = True
                self._loop_stop.clear()
                self.gdb.command(cmd)
                if not self._loop_stop.wait(60):    # pas sans fin (boucle infinie sur la ligne)
                    try:
                        self.gdb.command("-exec-interrupt")
                    except GdbError:
                        pass
                    self._loop_stop.wait(5)
                    error = "pas trop long : interrompu"
                steps += 1
                rec = self._loop_reason
                if error or self.state == "disconnected" or rec.get("reason") != "end-stepping-range":
                    break
                try:
                    if done():
                        met = True
                        break
                except GdbError as exc:         # expression hors de portée
                    error = str(exc)
                    break
        finally:
            with self._lock:
                self._looping = self._loop_inflight = False
                self._halt_wanted = False
            final = dict(rec)
            final.update(extra)
            final.update(steps=steps, met=met)
            if error:
                final["error"] = error
            self.stop_reason = final
            if self.state != "exited":          # programme terminé : « exited » déjà émis
                if self.state != "disconnected":
                    self.state = "stopped"
                self._emit("stopped", final)
            self._stopped.set()
        return final

    def reset(self) -> None:
        """Reset de la cible et arrêt au vecteur de reset (dépend du serveur gdb)."""
        if self.state == "running":
            self.halt()
        if self.target is not None:
            self.target.reset(self)
        else:
            self.monitor("reset halt")
        self.cli("flushregs")
        self._emit("stopped", {"reason": "reset"})

    def reset_from_vectors(self) -> None:
        """Reset « logiciel » : SP et PC relus dans la table des vecteurs (Cortex-M)."""
        vtor = 0
        try:
            vtor = self.read_u32(0xE000ED08)  # SCB->VTOR
        except GdbError:
            pass
        sp, pc = self.read_u32(vtor), self.read_u32(vtor + 4)
        self.write_register("sp", sp)
        self.write_register("pc", pc & ~1)

    def _sync(self, cmd: str, timeout: float = 30.0) -> None:
        self._continuing = False
        self._stopped.clear()
        self._before_resume()
        self.gdb.command(cmd)
        self.wait_stopped(timeout)

    def wait_stopped(self, timeout: float = 10.0) -> dict:
        if not self._stopped.wait(timeout):
            raise TimeoutError("la cible ne s'est pas arrêtée")
        self.select_focus()
        return self.stop_reason

    # ------------------------------------------------------------------ breakpoints
    def break_(self, location: str, hw: bool = False, temporary: bool = False,
               condition: str | None = None, count: int | None = None, log: str | None = None,
               action: Callable[[dict], Any] | None = None, stop: bool = False) -> Breakpoint:
        """Break.Set. `count` : arrêt au Nᵉ passage (gdb ignore les N-1 premiers, puis s'arrête
        à chaque passage) ; `log` / `action` / `stop` : voir BreakAction et break_options()."""
        flags = ("-h " if hw else "") + ("-t " if temporary else "")
        if condition:
            flags += f'-c "{_mi_quote(condition)}" '
        if count and count > 1:
            flags += f"-i {count - 1} "
        bkpt = self.gdb.command(f"-break-insert {flags}{location}")["bkpt"]
        num = bkpt.get("number", "?")
        if count and count > 1:
            self._counts[num] = count
        if log or action is not None:
            self._actions[num] = BreakAction(log or None, action, bool(stop))
        return self._bp(bkpt)

    def break_options(self, number: str, *, condition: Any = _KEEP, count: Any = _KEEP,
                      log: Any = _KEEP, action: Any = _KEEP, stop: Any = _KEEP) -> None:
        """Change les options d'un breakpoint ou d'un watchpoint ; un argument omis est gardé.
        `condition` : expression C ("" ou None la retire) ; `count` : arrêt au Nᵉ passage à
        partir de maintenant (None ou 1 : à chaque passage) ; `log` : message journalisé à
        chaque passage ; `action(arret)` : rappel Python ; `stop` : s'arrêter après l'action.
        Une action ne s'exécute que pendant un Go ; pendant un pas, l'arrêt reste visible."""
        number = str(number)
        if condition is not _KEEP:
            cmd = f"-break-condition {number}"
            self.gdb.command(f'{cmd} "{_mi_quote(condition)}"' if condition else cmd)
        if count is not _KEEP:
            n = int(count or 0)
            self.gdb.command(f"-break-after {number} {max(n - 1, 0)}")
            if n > 1:
                self._counts[number] = n
            else:
                self._counts.pop(number, None)
        if not (log is _KEEP and action is _KEEP and stop is _KEEP):
            cur = self._actions.get(number, BreakAction())
            new = BreakAction(cur.log if log is _KEEP else (log or None),
                              cur.fn if action is _KEEP else action,
                              cur.stop if stop is _KEEP else bool(stop))
            if new.log or new.fn is not None:
                self._actions[number] = new
            else:
                self._actions.pop(number, None)

    def break_action(self, number: str) -> BreakAction | None:
        """Action posée sur un breakpoint (None : aucune)."""
        return self._actions.get(str(number))

    def log_action(self, text: str, error: bool = False) -> None:
        """Message d'une action : journal `action_log` (sauf erreur) et événement `output`
        (stream « action ») pour la console et le mode batch."""
        if not error:
            self.action_log.append(text)
        self._emit("output", {"stream": "action", "text": text + "\n", "error": error})

    def _run_action(self, act: BreakAction, payload: dict) -> None:
        """Fil d'une action (cible arrêtée, arrêt interne) : message, rappel, puis relance
        silencieuse ou arrêt visible. Une action en échec arrête la cible et le dit."""
        stop = act.stop
        try:
            if act.log:
                self.log_action(expand_log(act.log, self.format_value))
            if act.fn is not None:
                res = act.fn(payload)
                if isinstance(res, str):
                    self.log_action(res)
                elif res:
                    stop = True
        except Exception as exc:  # noqa: BLE001 - signalé, et la cible reste arrêtée
            self.log_action(f"Erreur dans l'action du breakpoint {_stop_number(payload)} : "
                            f"{type(exc).__name__}: {exc}", error=True)
            stop = True
        try:
            if stop:
                self.deliver_stop()
            else:
                self.resume_internal()
        except Exception as exc:  # noqa: BLE001 - relance impossible : arrêt visible
            self.log_action(f"Erreur à la relance après l'action : {exc}", error=True)
            self.deliver_stop(force=True)

    def _resume_after_stale(self) -> None:
        try:
            self.resume_internal()
        except Exception:  # noqa: BLE001 - relance impossible : l'arrêt devient visible
            self.deliver_stop(force=True)

    # ------------------------------------------------------------------ watchpoints non signalés
    def _arm_watch_check(self) -> None:
        """Avant un Go (cible arrêtée) : watchpoints posés et valeur de leur expression. OpenOCD
        0.12 n'attribue pas toujours un arrêt de watchpoint (Cortex-M0 : SIGTRAP sans adresse,
        constaté sur STM32F072) ; gdb y voit alors un signal et ne teste ni la valeur ni la
        condition. `_watch_trap` fait ce travail. Rien pour un programme de l'hôte."""
        self._watch_vals = {}
        if getattr(self.target, "native", False):
            return
        try:
            body = self.gdb.command("-break-list")["BreakpointTable"].get("body", [])
        except (GdbError, KeyError, TypeError):
            return
        for b in body:
            if b.get("type") not in _WATCH_REASON or b.get("enabled") != "y":
                continue
            expr = b.get("what") or b.get("exp") or ""
            try:
                old = self.evaluate(expr)
            except GdbError:
                old = None
            self._watch_vals[str(b["number"])] = {"expr": expr, "type": b["type"],
                                                  "cond": b.get("cond"), "old": old}

    def _watch_trap(self, payload: dict) -> None:
        """Fil de travail (cible arrêtée, arrêt interne) : SIGTRAP pendant un Go avec des
        watchpoints. DFSR.DWTTRAP dit si c'est le DWT ; alors le watchpoint dont la valeur a
        changé (et dont la condition est vraie) devient la raison de l'arrêt, comme gdb l'aurait
        rapporté ; aucun : écriture d'une même valeur, ou condition fausse — relance
        silencieuse, comme gdb. Un piège d'une autre origine (BKPT) reste visible."""
        try:
            dfsr = self.read_u32(DFSR)
        except (GdbError, ValueError):
            dfsr = 0
        try:
            if not dfsr & DFSR_DWTTRAP:
                self.deliver_stop()
                return
            self.write_u32(DFSR, DFSR_DWTTRAP)          # effacé pour le prochain arrêt
            hit = None
            for num, w in self._watch_vals.items():
                try:
                    new = self.evaluate(w["expr"])
                except GdbError:
                    continue
                if new == w["old"] and w["type"] == "hw watchpoint":
                    continue                            # écriture sans changement
                old, w["old"] = w["old"], new
                if w["cond"] and not _truthy(self.evaluate(w["cond"])):
                    continue
                hit = (num, w, old, new)
                break
            if hit is None:
                self.resume_internal()
                return
            num, w, old, new = hit
            reason, key = _WATCH_REASON[w["type"]]
            rec = {k: v for k, v in payload.items() if k not in ("signal-name", "signal-meaning")}
            rec.update({"reason": reason, key: {"number": num, "exp": w["expr"]},
                        "value": {"old": old, "new": new} if old is not None else {"new": new}})
            self._internal_reason = rec
            act = self._actions.get(num)
            if act is not None:                         # watchpoint à action
                self._run_action(act, rec)
            else:
                self.deliver_stop()
        except Exception:  # noqa: BLE001 - doute : l'arrêt reste visible
            self.deliver_stop(force=True)

    def watch(self, expr: str, access: str = "write") -> str:
        flag = {"write": "", "read": "-r ", "rw": "-a "}[access]
        res = self.gdb.command(f"-break-watch {flag}{expr}")
        wp = res.get("wpt") or res.get("hw-rwpt") or res.get("hw-awpt") or {}
        return wp.get("number", "?")

    def delete_break(self, number: str) -> None:
        self.gdb.command(f"-break-delete {number}")
        for registry in (self._actions, self._counts):
            registry.pop(str(number), None)
        self._transient.discard(str(number))

    def _drop_transient(self) -> None:
        """Retire les watchpoints de Go.Till (déjà supprimés par gdb : hors de portée)."""
        while self._transient:
            num = self._transient.pop()
            self._actions.pop(num, None)
            try:
                self.gdb.command(f"-break-delete {num}")
            except GdbError:
                pass

    def enable_break(self, number: str, enabled: bool = True) -> None:
        self.gdb.command(f"-break-{'enable' if enabled else 'disable'} {number}")

    def breakpoints(self) -> list[Breakpoint]:
        body = self.gdb.command("-break-list")["BreakpointTable"].get("body", [])
        return [self._bp(b) for b in body if b.get("number") not in self._transient]

    def _bp(self, b: dict) -> Breakpoint:
        num = b.get("number", "?")
        act = self._actions.get(num)
        kind = b.get("type", "")
        return Breakpoint(
            number=num, type=kind,
            enabled=b.get("enabled") == "y", addr=b.get("addr"), func=b.get("func"),
            file=b.get("file"), line=int(b["line"]) if b.get("line") else None,
            hits=int(b.get("times", 0)), condition=b.get("cond") or None,
            ignore=int(b.get("ignore", 0) or 0), count=self._counts.get(num),
            action=act.describe() if act else "",
            expr=b.get("what") or b.get("exp") if "watchpoint" in kind else None,
        )

    # ------------------------------------------------------------------ état
    def frame(self) -> Frame:
        f = self.gdb.command("-stack-info-frame")["frame"]
        return Frame(pc=_int(f["addr"]), func=f.get("func"), file=f.get("file"),
                     fullname=f.get("fullname"), line=int(f["line"]) if f.get("line") else None)

    def backtrace(self) -> list[Frame]:
        stack = self.gdb.command("-stack-list-frames").get("stack", [])
        out = []
        for item in stack:
            f = item.get("frame", item)
            out.append(Frame(pc=_int(f["addr"]), func=f.get("func"), file=f.get("file"),
                             fullname=f.get("fullname"),
                             line=int(f["line"]) if f.get("line") else None))
        return out

    def registers(self) -> list[tuple[str, int]]:
        vals = self.gdb.command("-data-list-register-values x").get("register-values", [])
        out = []
        for v in vals:
            idx = int(v["number"])
            name = self._reg_names[idx] if idx < len(self._reg_names) else str(idx)
            if not name:
                continue
            try:
                out.append((name, _int(v["value"])))
            except ValueError:
                continue  # registres non scalaires (vecteurs FP...)
        return out

    def register(self, name: str) -> int:
        # conversion explicite : gdb affiche $sp/$pc comme pointeurs (« 0x... <sym+n> »)
        return _int(self.evaluate(f"(unsigned long)${name}")) & ((1 << 8 * self.pointer_size) - 1)

    def task_sp(self) -> int:
        """SP de la tâche courante d'un RTOS sur Cortex-M : PSP. Les tâches tournent sur la pile
        de processus ; arrêtée dans une interruption, `sp` est la pile principale (MSP), et la
        pile utilisée de la tâche paraissait négative. `sp` si le serveur n'expose pas `psp`."""
        try:
            return self.register("psp")
        except GdbError:
            return self.register("sp")

    @property
    def pointer_size(self) -> int:
        """Taille d'un pointeur de la cible : 4 (Cortex-M), 8 (programme natif 64 bits)."""
        if not hasattr(self, "_ptr_size"):
            try:
                self._ptr_size = int(self.evaluate("sizeof(void *)"))
            except (GdbError, ValueError):
                return 4
        return self._ptr_size

    def write_register(self, name: str, value: int) -> None:
        self.gdb.command(f"-data-evaluate-expression \"${name}={value}\"")

    def read_memory(self, addr: int, length: int) -> bytes:
        res = self.gdb.command(f"-data-read-memory-bytes {addr:#x} {length}")
        blob = bytearray()
        for chunk in res.get("memory", []):
            blob += bytes.fromhex(chunk["contents"])
        return bytes(blob)

    def write_memory(self, addr: int, data: bytes) -> None:
        self.gdb.command(f"-data-write-memory-bytes {addr:#x} {data.hex()}")

    def read_u32(self, addr: int) -> int:
        return int.from_bytes(self.read_memory(addr, 4), "little")

    def write_u32(self, addr: int, value: int) -> None:
        self.write_memory(addr, (value & 0xFFFFFFFF).to_bytes(4, "little"))

    def evaluate(self, expr: str) -> str:
        escaped = _mi_quote(expr)
        return self.gdb.command(f'-data-evaluate-expression "{escaped}"')["value"]

    def set_var(self, expr: str, value: str) -> None:
        self.evaluate(f"{expr}={value}")

    def address_of(self, symbol: str) -> int:
        val = self.evaluate(f"(unsigned long)&{symbol}")
        return _int(val.split()[0])

    def address_of_expr(self, expr: str) -> int:
        """Adresse d'une lvalue quelconque (`sensors[1].value`). GdbError si pas en mémoire."""
        return _int(self.evaluate(f"(unsigned long)&({expr})").split()[0])

    def sizeof(self, expr: str) -> int:
        return _int(self.evaluate(f"sizeof({expr})").split()[0])

    def ptype(self, expr: str) -> str:
        """Type de l'expression, typedefs résolus (`type = unsigned int`)."""
        return self.cli(f"ptype {expr}").strip()

    def disassemble(self, start: int, end: int) -> list[dict]:
        """Retourne [{address, func-name, offset, inst, opcodes}, ...]."""
        res = self.gdb.command(f"-data-disassemble -s {start:#x} -e {end:#x} -- 2")
        return [{**i, "address": _int(i["address"])} for i in res.get("asm_insns", [])]

    def disassemble_function(self, func: str) -> list[dict]:
        out = self.cli(f"info line {func}")
        # « Line N of "f.c" starts at address 0x... <func> and ends at ... »
        start = int(out.split("starts at address")[1].split()[0], 16) if "starts at address" in out else self.address_of(func)
        res = self.gdb.command(f"-data-disassemble -a {start:#x} -- 2")
        return [{**i, "address": _int(i["address"])} for i in res.get("asm_insns", [])]

    def locals(self) -> list[dict]:
        res = self.gdb.command("-stack-list-variables --all-values")
        return res.get("variables", [])

    # ------------------------------------------------------------------ objets variables (Var.view)
    def var_create(self, expr: str, floating: bool = True) -> dict:
        """floating=True : réévalué dans la frame courante à chaque arrêt (Var.Watch)."""
        escaped = expr.replace('"', '\\"')
        frame = "@" if floating else "*"
        return self.gdb.command(f'-var-create - {frame} "{escaped}"')

    def var_assign(self, name: str, value: str) -> str:
        return self.gdb.command(f"-var-assign {name} {value}")["value"]

    def path_expression(self, name: str) -> str:
        """Expression C complète d'un varobj (`var1.1.value` → `(sensors)[1].value`)."""
        return self.gdb.command(f"-var-info-path-expression {name}")["path_expr"]

    def var_children(self, name: str) -> list[dict]:
        res = self.gdb.command(f"-var-list-children --all-values {name}")
        return [c.get("child", c) for c in res.get("children", [])]

    def var_update(self) -> list[dict]:
        return self.gdb.command("-var-update --all-values *").get("changelist", [])

    def var_delete(self, name: str) -> None:
        self.gdb.command(f"-var-delete {name}")

    def var_set_format(self, name: str, fmt: str) -> str:
        """Format d'affichage d'un varobj (natural, decimal, hexadecimal, octal, binary,
        zero-hexadecimal), gardé par les `-var-update` suivants ; retourne la valeur.
        Les enfants d'une structure ou d'un tableau ne l'héritent pas."""
        return self.gdb.command(f"-var-set-format {name} {fmt}").get("value", "")

    def format_value(self, expr: str, fmt: str = "natural") -> str:
        """Valeur d'une expression au format voulu (core/formats.py) : `output/F` de gdb, sans
        historique `$N` ; binaire et ASCII d'un scalaire tirés de `output/z` (largeur du type),
        ceux d'une structure laissés à gdb (/t, /c)."""
        from .formats import AGGREGATE_LETTER, OUTPUT_LETTER, from_zero_hex, normalize
        fmt = normalize(fmt)
        if fmt == "natural":
            return self.evaluate(expr)
        out = self.cli(f"output/{OUTPUT_LETTER[fmt]} {expr}").strip()
        if fmt in AGGREGATE_LETTER:
            conv = from_zero_hex(out, fmt)
            out = conv if conv is not None else self.cli(f"output/{AGGREGATE_LETTER[fmt]} {expr}").strip()
        return out

    # ------------------------------------------------------------------ threads (RTOS côté serveur)
    def threads(self) -> list[dict]:
        """Threads vus par gdb. Avec OpenOCD `-rtos`, chaque tâche RTOS est un thread."""
        return self.gdb.command("-thread-info").get("threads", [])

    def thread_list(self) -> list[ThreadInfo]:
        """Threads du programme (pthreads d'un programme natif), le courant marqué. Utilisable
        cible en marche (sans frames)."""
        res = self.gdb.command("-thread-info")
        current = res.get("current-thread-id")
        out = []
        for t in res.get("threads", []):
            m = re.search(r"LWP (\d+)", t.get("target-id", ""))
            f = t.get("frame")
            frame = None
            if f and f.get("addr"):
                frame = Frame(pc=_int(f["addr"]), func=f.get("func"), file=f.get("file"),
                              fullname=f.get("fullname"),
                              line=int(f["line"]) if f.get("line") else None)
            out.append(ThreadInfo(
                id=t.get("id", "?"), name=t.get("name", ""), target_id=t.get("target-id", ""),
                lwp=int(m.group(1)) if m else None, state=t.get("state", ""),
                core=int(t["core"]) if str(t.get("core", "")).isdigit() else None,
                frame=frame, current=t.get("id") == current))
        return out

    def select_thread(self, thread_id: str) -> Frame | None:
        """Le thread devient le thread courant : registres, pile, variables locales et pas à
        pas portent désormais sur lui."""
        res = self.gdb.command(f"-thread-select {thread_id}")
        if self.non_stop:                         # le thread choisi devient le suivi
            self.focus = str(thread_id)
            st = self.thread_states.get(self.focus, "stopped")
            if st != self.state and self.state in ("stopped", "running"):
                self.state = st
                (self._stopped.set if st == "stopped" else self._stopped.clear)()
        self._emit("threads", {"event": "thread-selected", "id": str(thread_id)})
        f = res.get("frame")
        if not f or not f.get("addr"):
            return None
        return Frame(pc=_int(f["addr"]), func=f.get("func"), file=f.get("file"),
                     fullname=f.get("fullname"), line=int(f["line"]) if f.get("line") else None)

    def thread_for(self, task_name: str) -> str | None:
        """Identifiant du thread gdb correspondant à une tâche, ou None (pas de support RTOS
        côté serveur : QEMU, ou OpenOCD sans `-rtos`)."""
        threads = self.threads()
        if len(threads) <= 1:
            return None
        pattern = re.compile(rf"(^|\W){re.escape(task_name)}(\W|$)")
        for t in threads:
            text = " ".join(str(t.get(k, "")) for k in ("name", "details", "target-id"))
            if pattern.search(text):
                return t.get("id")
        return None

    def thread_context(self, thread_id: str) -> tuple[dict[str, int], list[Frame]]:
        """Registres et pile d'appels d'un thread, puis retour au thread courant."""
        current = self.gdb.command("-thread-info").get("current-thread-id")
        self.gdb.command(f"-thread-select {thread_id}")
        try:
            return dict(self.registers()), self.backtrace()
        finally:
            if current:
                self.gdb.command(f"-thread-select {current}")

    def symbol_at(self, addr: int) -> str:
        out = self.cli(f"info symbol {addr:#x}").strip()
        return "" if out.startswith("No symbol") else out
