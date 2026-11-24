"""Session de débogage : API haut niveau au-dessus de GdbMI.

C'est l'objet que manipulent l'interface graphique et les scripts (commandes GdbTrace :
Go, Break, Step, Register, Data.dump, Var.view...).
Toutes les méthodes sont utilisables depuis n'importe quel thread.
"""
from __future__ import annotations

import os
import re
import threading
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

    def summary(self) -> str:
        if self.mismatched:
            v = "ÉCHEC de la vérification : " + ", ".join(self.mismatched)
        elif self.verified:
            v = "vérifié"
        else:
            v = "non vérifié" + (f" ({self.verify_error.splitlines()[-1]})" if self.verify_error else "")
        return f"{os.path.basename(self.elf)} programmé : {self.size} octets en {self.seconds:.1f} s, {v}"


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
        self.state = "disconnected"  # disconnected | stopped | running
        self.stop_reason: dict = {}
        self._stopped = threading.Event()
        self._listeners: list[EventFn] = []
        self._resume_hooks: list[Callable[[], None]] = []
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

    # ------------------------------------------------------------------ événements
    def on_event(self, fn: EventFn) -> None:
        """fn(kind, data) avec kind ∈ stopped, running, output, breakpoints, disconnected."""
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
        for fn in list(self._listeners):
            try:
                fn(kind, data)
            except Exception:
                pass

    def _on_mi(self, rec: dict) -> None:
        t, msg, payload = rec.get("type"), rec.get("message"), rec.get("payload")
        if t == "notify" and msg == "stopped":
            payload = payload or {}
            with self._lock:
                if self._looping:               # pas intermédiaire d'une boucle : silencieux
                    self._loop_reason = payload
                    self._loop_inflight = False
                    self._loop_stop.set()
                    return
                requested, self._halt_requested = self._halt_requested, False
                free = not requested and self.state == "running" and self._continuing
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
        self.gdb.command(f'-file-exec-and-symbols "{elf}"')
        self.gdb.command(f"-target-select extended-remote {target.remote}", timeout=20)
        self.state = "stopped"
        self._stopped.set()
        if load if load is not None else target.needs_load:
            self.load()
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
            self.gdb.stop()
        finally:
            if self.target:
                self.target.stop()
            self.state = "disconnected"
            self._emit("disconnected", {})

    def load(self) -> None:
        self.gdb.command("-target-download", timeout=120)

    def program(self, elf: str, verify: bool = True) -> "ProgramResult":
        """Programme `elf` dans la cible : arrêt, reset (cible arrêtée au vecteur de reset),
        écriture par le serveur gdb (`load` : flash avec OpenOCD/J-Link/pyOCD, RAM sous QEMU),
        vérification optionnelle (`compare-sections`), puis reset. gdb prend aussi les
        symboles de cet ELF."""
        import time
        t0 = time.monotonic()
        if self.state == "running":
            self.halt()
        self.gdb.command(f'-file-exec-and-symbols "{elf}"')
        self.elf = elf
        if self.target is not None:
            if hasattr(self.target, "elf"):      # QEMU : relancé plus tard avec ce firmware
                self.target.elf = elf
            if self.target.needs_load:          # sonde : flash écrite cible arrêtée au reset
                self.target.reset(self)
        res = self.gdb.command("-target-download", timeout=300)
        result = ProgramResult(elf, int(res.get("load-size", 0) or 0))
        if verify:
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

    def monitor(self, cmd: str) -> str:
        """Commande `monitor` brute vers le serveur (OpenOCD : reset halt, flash...)."""
        return self.gdb.console(f"monitor {cmd}")

    def cli(self, cmd: str) -> str:
        """Commande CLI gdb arbitraire (porte de sortie)."""
        return self.gdb.console(cmd)

    # ------------------------------------------------------------------ run-control
    def go(self) -> None:
        self._continuing = True
        self._stopped.clear()
        self._before_resume()
        self.gdb.command("-exec-continue")

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
                self.gdb.command("-exec-interrupt")
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
            if self.state != "disconnected":
                self.state = "stopped"
            self.stop_reason = final
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
        return _int(self.evaluate(f"(unsigned long)${name}")) & 0xFFFFFFFF

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
