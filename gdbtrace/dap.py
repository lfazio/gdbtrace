"""Adaptateur de débogage de GdbTrace (Debug Adapter Protocol) sur stdio.

VS Code (extension `vscode/gdbtrace`), nvim-dap ou Emacs (dap-mode) pilotent une session sans
interface : la même `BatchSession` que le mode `--batch` et le serveur MCP. Messages DAP :
en-têtes `Content-Length`, ligne vide, corps JSON (UTF-8).

Déroulement : `initialize` (capacités), `launch` (options de la ligne de commande : `args`,
ou `program` / `config`), puis l'événement `initialized` ; le client pose ses breakpoints
(`setBreakpoints`, `setFunctionBreakpoints`, `setInstructionBreakpoints`,
`setExceptionBreakpoints`) et envoie `configurationDone` : la cible part, ou reste arrêtée
(`stopOnEntry`). Les arrêts de la session deviennent des événements `stopped` (raison
traduite), une reprise non demandée `continued`, la fin du programme `exited` puis
`terminated` ; la sortie du programme, les messages des breakpoints à message et le journal
de GdbTrace arrivent en `output`.

- Threads : ceux de gdb (pthreads d'un programme natif, cœurs d'un même serveur), plus les
  autres cœurs (`--core`, autres serveurs) et les tâches FreeRTOS, dont la pile se lit (pas de
  pas à pas sur une tâche qui ne tourne pas).
- Variables : locales (objets variables de gdb pour les structures et tableaux), registres,
  périphériques du SVD (registres décodés, champs).
- `evaluate` : expression C dans la frame ; à la console (`repl`), `>code` exécute du Python
  avec l'objet `gdbtrace` (comme la console de GdbTrace), `-exec cmd` une commande de gdb.
- Les pas (`next`, `stepIn`, `stepOut`) tournent dans un fil à part : `pause` reste traité.
- Breakpoint posé cible en marche (gdb en all-stop le refuse) : arrêt silencieux, pose,
  reprise ; un vrai arrêt survenu entre-temps est rapporté.
- `stepBack` / `reverseContinue` : par le retour arrière de la trace (`gdbtrace.step_back`,
  `gdbtrace.reverse_continue`), annoncés (`supportsStepBack`) seulement s'il existe.

Comme pour le serveur MCP, les descripteurs 0 et 1 sont dupliqués pour le protocole puis
remplacés (/dev/null, stderr) : un fils (gdb, QEMU, OpenOCD) n'y lit ni n'y écrit.
"""
from __future__ import annotations

import base64
import contextlib
import io
import json
import os
import shlex
import sys
import threading
import traceback

#: tâches FreeRTOS et autres cœurs : identifiants de threads hors de ceux de gdb
TASK_BASE = 100_000
CORE_BASE = 200_000
#: lecture mémoire par tranches (une erreur arrête la lecture : octets illisibles)
MEMORY_CHUNK = 4096


class DapError(Exception):
    """Erreur attendue d'une requête : rendue au client (success false, message)."""


# ====================================================================== cadre des messages
def read_message(stream) -> dict | None:
    """Un message (en-têtes, ligne vide, corps JSON) ; None à la fin du flux. `stream` : binaire."""
    length = None
    while True:
        line = stream.readline()
        if not line:
            return None
        line = line.strip()
        if not line:
            if length is None:
                continue                        # lignes vides entre deux messages
            break
        name, _, value = line.decode("ascii", "replace").partition(":")
        if name.strip().lower() == "content-length":
            length = int(value.strip())
    body = b""
    while len(body) < length:
        chunk = stream.read(length - len(body))
        if not chunk:
            return None
        body += chunk
    return json.loads(body.decode("utf-8"))


def encode_message(msg: dict) -> bytes:
    body = json.dumps(msg, ensure_ascii=False).encode("utf-8")
    return b"Content-Length: %d\r\n\r\n" % len(body) + body


def _mi(text: str) -> str:
    from .core.session import _mi_quote
    return _mi_quote(text)


def capabilities() -> dict:
    from .core.scripting import ScriptAPI
    back = callable(getattr(ScriptAPI, "step_back", None))
    return {
        "supportsConfigurationDoneRequest": True,
        "supportsFunctionBreakpoints": True,
        "supportsConditionalBreakpoints": True,
        "supportsHitConditionalBreakpoints": True,
        "supportsLogPoints": True,
        "supportsInstructionBreakpoints": True,
        "supportsEvaluateForHovers": True,
        "supportsReadMemoryRequest": True,
        "supportsWriteMemoryRequest": True,
        "supportsDisassembleRequest": True,
        "supportsSteppingGranularity": True,
        "supportsStepBack": back,
        "supportTerminateDebuggee": True,
        "exceptionBreakpointFilters": [
            {"filter": "faute", "label": "Fautes Cortex-M", "default": False,
             "description": "arrêt à l'entrée de toute faute (HardFault, MemManage, BusFault, "
                            "UsageFault) : vector catch, sinon breakpoints sur les gestionnaires"}],
    }


# ====================================================================== adaptateur
class GdbTraceDap:
    def __init__(self, out=None):
        self.out = out or sys.stdout.buffer
        self._seq = 0
        self._lock = threading.Lock()
        self.batch = None
        self.line_base = 1                      # linesStartAt1 du client
        self.stop_on_entry = False
        self._launching = False                 # connexion : ses arrêts ne sont pas rapportés
        self._silent = False                    # arrêt / reprise internes (breakpoint en marche)
        self._requested = False                 # reprise demandée : pas d'événement continued
        self._closing = False
        self._terminated = False
        self._source_bps: dict[str, list[str]] = {}
        self._function_bps: list[str] = []
        self._instruction_bps: list[str] = []
        self._frames: dict[int, tuple] = {}     # id → (fil, niveau, Frame)
        self._handles: dict[int, object] = {}   # variablesReference → fonction des variables
        self._varobjs: list[str] = []
        self._stale: list[str] = []         # objets de gdb à supprimer (arrêt passé)
        self._after = None                      # événement à envoyer après la réponse
        self._no_rtos = False
        self._py: dict | None = None
        self._worker: threading.Thread | None = None

    # ------------------------------------------------------------------ protocole
    def send(self, msg: dict) -> None:
        with self._lock:
            self._seq += 1
            msg["seq"] = self._seq
            self.out.write(encode_message(msg))
            self.out.flush()

    def event(self, name: str, body: dict | None = None) -> None:
        msg = {"type": "event", "event": name}
        if body is not None:
            msg["body"] = body
        self.send(msg)

    def _console(self, text) -> None:
        text = str(text)
        if text:
            self.event("output", {"category": "console", "output": text.rstrip("\n") + "\n"})

    def handle(self, req: dict) -> dict | None:
        """Réponse à une requête (None pour un message qui n'en est pas une)."""
        if req.get("type") != "request":
            return None
        cmd, args = req.get("command", ""), req.get("arguments") or {}
        reply = {"type": "response", "request_seq": req.get("seq", 0), "command": cmd,
                 "success": True}
        fn = getattr(self, "req_" + cmd, None)
        try:
            if fn is None:
                raise DapError(f"requête non prise en charge : {cmd}")
            body = fn(args)
            if body is not None:
                reply["body"] = body
        except DapError as exc:
            reply.update(success=False, message=str(exc))
        except Exception as exc:  # noqa: BLE001 - une erreur interne ne tue pas l'adaptateur
            reply.update(success=False, message=f"{type(exc).__name__}: {exc}")
            self._console(traceback.format_exc(limit=6))
        return reply

    def serve(self, inp=None) -> None:
        inp = inp or sys.stdin.buffer
        while True:
            msg = read_message(inp)
            if msg is None:
                break
            reply = self.handle(msg)
            if reply is not None:
                self.send(reply)
                after = self._after
                if after is not None:           # événement qui suit la réponse (initialized)
                    self._after = None
                    after()
            if self._closing:
                break
        self.close()

    # ------------------------------------------------------------------ session
    @property
    def s(self):
        if self.batch is None:
            raise DapError("pas de session : requête launch d'abord")
        return self.batch.api.s                 # cœur choisi (--core) compris

    @property
    def api(self):
        if self.batch is None:
            raise DapError("pas de session : requête launch d'abord")
        return self.batch.api

    def _stopped(self) -> None:
        if self.s.state == "running":
            raise DapError("cible en marche : la mettre en pause d'abord")
        if self.s.state == "exited":
            raise DapError("programme terminé")

    def close(self, leave_running: bool = False) -> None:
        b, self.batch = self.batch, None
        if b is not None:
            self._closing = True
            try:
                b.close(leave_running=leave_running)
            except Exception as exc:  # noqa: BLE001
                print(f"gdbtrace-dap : fermeture : {exc}", file=sys.stderr)

    def _on_event(self, kind: str, data: dict) -> None:
        """Thread lecteur de gdb : traduit les événements de la session, sans jamais bloquer
        (ni appel à gdb, ni attente)."""
        if kind == "output":
            stream, text = data.get("stream"), data.get("text", "")
            if stream == "target" and text:
                self.event("output", {"category": "stdout", "output": text})
            elif stream == "action" and text:
                self.event("output", {"category": "stderr" if data.get("error") else "console",
                                      "output": text})
            return
        if self._launching or self._closing:
            return
        if kind == "stopped":
            self._invalidate()
            if self._silent:
                return
            self.event("stopped", self._stop_body(data))
        elif kind == "running":
            self._invalidate()
            if self._silent:
                return
            if self._requested:
                self._requested = False
            else:
                self.event("continued", {"threadId": self._thread_of(data),
                                         "allThreadsContinued": True})
        elif kind == "exited":
            code = data.get("exit-code", "0")
            self.event("exited", {"exitCode": int(code, 8) if str(code).isdigit() else 0})
            self._terminate()
        elif kind == "disconnected":
            self._terminate()

    def _terminate(self) -> None:
        if not self._terminated:
            self._terminated = True
            self.event("terminated", {})

    @staticmethod
    def _thread_of(data: dict) -> int:
        tid = data.get("thread-id")
        return int(tid) if str(tid).isdigit() else 1

    def _stop_body(self, data: dict) -> dict:
        r = data.get("reason", "")
        body = {"threadId": self._thread_of(data), "allThreadsStopped": True}
        if r == "breakpoint-hit":
            num = str(data.get("bkptno", ""))
            reason = ("function breakpoint" if num in self._function_bps else
                      "instruction breakpoint" if num in self._instruction_bps else "breakpoint")
            if num.isdigit():
                body["hitBreakpointIds"] = [int(num)]
        elif r in ("end-stepping-range", "function-finished", "location-reached"):
            reason = "step"
        elif r.startswith("watchpoint") or r.startswith("access-watchpoint") or r.startswith("read-watchpoint"):
            reason = "data breakpoint"
        elif r == "signal-received" and data.get("signal-name") not in ("SIGINT", "SIGTRAP", "0", None):
            reason = "exception"
            body["description"] = data.get("signal-meaning") or data.get("signal-name")
        elif r in ("connected", "programmed", "reset"):
            reason = "entry"
        else:
            reason = "pause"
        body["reason"] = reason
        return body

    def _invalidate(self) -> None:
        """Arrêt ou reprise : frames, références de variables et objets de gdb périmés."""
        self._frames.clear()
        self._handles.clear()
        self._stale = list(self._varobjs)
        self._varobjs.clear()

    def _drop_varobjs(self) -> None:
        stale, self._stale = self._stale, []
        for name in stale:
            with contextlib.suppress(Exception):
                self.s.var_delete(name)

    def _handle(self, fn) -> int:
        ref = len(self._handles) + 1
        self._handles[ref] = fn
        return ref

    def _resume(self, fn, thread: int | None = None) -> None:
        """Reprise demandée par le client (pas d'événement continued) ; un pas tourne dans un
        fil à part (synchrone dans la session : `pause` doit rester traité)."""
        self._stopped()
        self._select_thread(thread)
        self._requested = True

        def run():
            try:
                fn()
            except TimeoutError:
                pass                            # encore en marche : l'arrêt viendra en événement
            except Exception as exc:  # noqa: BLE001
                self._requested = False
                self._console(f"reprise : {type(exc).__name__}: {exc}")
        self._worker = threading.Thread(target=run, name="gdbtrace-dap-run", daemon=True)
        self._worker.start()

    def _select_thread(self, thread: int | None) -> None:
        if thread is None:
            return
        if thread >= CORE_BASE:                 # autre cœur : choisi pour la suite
            cores = self.api.cores(quiet=True)
            k = thread - CORE_BASE
            if 0 <= k < len(cores):
                with contextlib.redirect_stdout(io.StringIO()):
                    self.api.core(k)
            return
        if thread >= TASK_BASE:
            raise DapError("tâche FreeRTOS : sa pile se lit, mais seul le cœur avance (choisir "
                           "le thread du cœur)")
        with contextlib.suppress(Exception):
            if str(thread) != self.s.current_thread():
                self.s.select_thread(str(thread))

    # ------------------------------------------------------------------ requêtes : mise en route
    def req_initialize(self, args: dict) -> dict:
        self.line_base = 1 if args.get("linesStartAt1", True) else 0
        return capabilities()

    def req_launch(self, args: dict) -> None:
        from .__main__ import BatchSession, build_target, parse_command_line, resolve_session
        if self.batch is not None:
            raise DapError("session déjà ouverte")
        if args.get("cwd"):
            os.chdir(os.path.expanduser(args["cwd"]))
        extra = args.get("args") or []
        argv = shlex.split(extra) if isinstance(extra, str) else [str(a) for a in extra]
        if args.get("config"):
            argv = ["--config", str(args["config"])] + argv
        if args.get("program"):
            argv = [str(args["program"])] + argv
        if any(a in ("--batch", "--script") for a in argv):
            raise DapError("--batch / --script : sans objet ici")
        self.stop_on_entry = bool(args.get("stopOnEntry", False))
        err = io.StringIO()
        try:
            with contextlib.redirect_stderr(err):    # argparse écrit son erreur
                p, ns, config = parse_command_line(argv + ["--batch"])
                elf, program, verify, load, svd_warning = resolve_session(p, ns)
        except SystemExit:
            raise DapError("options refusées : " + err.getvalue().strip().splitlines()[-1]
                           if err.getvalue().strip() else "options refusées") from None
        if svd_warning:
            self._console(svd_warning)
        target = build_target(ns, elf)
        self._launching = True
        try:
            b = BatchSession(ns, elf, target, load, program, verify, log=self._console,
                             on_event=self._on_event, config=config)
        except Exception as exc:  # noqa: BLE001 - le journal (console) explique
            self._launching = False
            raise DapError(f"connexion impossible : {exc}") from exc
        b.trace.on_error = self._console
        self.batch = b
        try:
            b.setup()
        except Exception as exc:  # noqa: BLE001 - la session reste ouverte
            self._console(f"mise en route de la trace : {exc}")
        finally:
            self._launching = False
        self._console(f"session : {target.remote}")
        self._after = lambda: self.event("initialized")

    def req_attach(self, args: dict) -> None:
        return self.req_launch(args)

    def req_configurationDone(self, args: dict) -> None:
        s = self.s
        if self.stop_on_entry or s.state != "stopped":
            if s.state == "stopped":
                self._after = lambda: self.event("stopped", {"reason": "entry", "threadId": 1,
                                                             "allThreadsStopped": True})
            return
        self._resume(s.go)

    def req_disconnect(self, args: dict) -> None:
        leave = args.get("terminateDebuggee") is False
        self._closing = True
        self.close(leave_running=leave)

    def req_terminate(self, args: dict) -> None:
        self.req_disconnect({})
        self._terminate()

    # ------------------------------------------------------------------ breakpoints
    def _while_stopped(self, fn):
        """`fn()` cible arrêtée : en marche, arrêt silencieux, `fn`, puis reprise ; un arrêt
        survenu entre-temps (breakpoint, faute) est rapporté au lieu de la reprise."""
        s = self.s
        if s.state != "running":
            return fn()
        self._silent = True
        try:
            s.halt()
            reason = dict(s.stop_reason or {})
            # l'arrêt demandé, ou celui d'un breakpoint à message (son action n'arrête pas : la
            # demande d'arrêt l'a rendu visible) ; un autre arrêt est rapporté
            act = (s.break_action(str(reason.get("bkptno", "")))
                   if reason.get("reason") == "breakpoint-hit" else None)
            ours = ((reason.get("reason") == "signal-received"
                     and reason.get("signal-name") in ("SIGINT", "SIGTRAP", "0", None))
                    or (act is not None and not act.stop and act.fn is None))
            res = fn()
        finally:
            self._silent = False
        if ours:
            self._silent = True
            try:
                s.go()
            finally:
                self._silent = False
        else:
            self.event("stopped", self._stop_body(reason))
        return res

    def _bp_body(self, bp, line: int | None = None, path: str | None = None) -> dict:
        body = {"id": int(bp.number) if str(bp.number).isdigit() else 0, "verified": True}
        if bp.line or line:
            body["line"] = (bp.line or line) - 1 + self.line_base
        src = path or bp.file
        if src:
            body["source"] = {"name": os.path.basename(src), "path": path or src}
        if bp.addr and str(bp.addr).startswith("0x"):
            body["instructionReference"] = bp.addr
        return body

    def _insert(self, location: str, spec: dict, **extra):
        cond = spec.get("condition") or None
        count = None
        hit = str(spec.get("hitCondition") or "").strip().lstrip(">=").strip()
        if hit:
            if not hit.isdigit():
                raise DapError(f"condition de passage : un nombre (Nᵉ passage), pas « {hit} »")
            count = int(hit)
        log = spec.get("logMessage") or None
        return self.s.break_(location, condition=cond, count=count, log=log, **extra)

    def _replace(self, numbers: list[str]) -> None:
        for num in numbers:
            with contextlib.suppress(Exception):
                self.s.delete_break(num)

    def req_setBreakpoints(self, args: dict) -> dict:
        path = (args.get("source") or {}).get("path")
        if not path:
            raise DapError("source sans chemin")
        specs = args.get("breakpoints")
        if specs is None:
            specs = [{"line": n} for n in args.get("lines", [])]

        def apply():
            self._replace(self._source_bps.pop(path, []))
            out, nums = [], []
            for spec in specs:
                line = int(spec["line"]) - self.line_base + 1
                try:
                    bp = self._insert(f'"{_mi(path)}:{line}"', spec)
                except Exception as exc:  # noqa: BLE001 - rendu par breakpoint
                    out.append({"verified": False, "line": spec["line"], "message": str(exc)})
                    continue
                nums.append(bp.number)
                out.append(self._bp_body(bp, line, path))
            self._source_bps[path] = nums
            return out
        return {"breakpoints": self._while_stopped(apply)}

    def req_setFunctionBreakpoints(self, args: dict) -> dict:
        def apply():
            self._replace(self._function_bps)
            self._function_bps = []
            out = []
            for spec in args.get("breakpoints", []):
                try:
                    bp = self._insert(spec["name"], spec)
                except Exception as exc:  # noqa: BLE001
                    out.append({"verified": False, "message": str(exc)})
                    continue
                self._function_bps.append(bp.number)
                out.append(self._bp_body(bp))
            return out
        return {"breakpoints": self._while_stopped(apply)}

    def req_setInstructionBreakpoints(self, args: dict) -> dict:
        def apply():
            self._replace(self._instruction_bps)
            self._instruction_bps = []
            out = []
            for spec in args.get("breakpoints", []):
                try:
                    addr = int(str(spec["instructionReference"]), 0) + int(spec.get("offset", 0))
                    bp = self._insert(f"*{addr:#x}", spec)
                except Exception as exc:  # noqa: BLE001
                    out.append({"verified": False, "message": str(exc)})
                    continue
                self._instruction_bps.append(bp.number)
                out.append(self._bp_body(bp))
            return out
        return {"breakpoints": self._while_stopped(apply)}

    def req_setExceptionBreakpoints(self, args: dict) -> dict:
        filters = args.get("filters") or []
        if self.batch is None:
            return {"breakpoints": []}
        if "faute" in filters:
            if self.batch.target.native:
                return {"breakpoints": [{"verified": False, "message": "programme de l'hôte : sans objet"}]}
            text = self._while_stopped(lambda: self.api.break_on_fault())
            self._console(text)
            return {"breakpoints": [{"verified": True}]}
        return {"breakpoints": []}

    # ------------------------------------------------------------------ run-control
    def req_continue(self, args: dict) -> dict:
        self._resume(self.s.go, args.get("threadId"))
        return {"allThreadsContinued": True}

    def _step(self, args: dict, source, instruction) -> None:
        asm = args.get("granularity") == "instruction"
        self._resume(instruction if asm else source, args.get("threadId"))

    def req_next(self, args: dict) -> None:
        s = self.s
        self._step(args, s.step_over, lambda: s._sync("-exec-next-instruction"))

    def req_stepIn(self, args: dict) -> None:
        s = self.s
        self._step(args, s.step, s.step_asm)

    def req_stepOut(self, args: dict) -> None:
        s = self.s
        self._step(args, s.step_out, s.step_out)

    def req_pause(self, args: dict) -> None:
        s = self.s
        if s.state == "running":
            s.halt()

    def _reverse(self, name: str, args: dict) -> None:
        fn = getattr(self.api, name, None)
        if not callable(fn):
            raise DapError("retour arrière indisponible (pas de trace d'instructions)")
        self._stopped()
        self._invalidate()
        with contextlib.redirect_stdout(io.StringIO()):
            fn()
        self.event("stopped", {"reason": "step", "threadId": args.get("threadId", 1),
                               "allThreadsStopped": True})

    def req_stepBack(self, args: dict) -> None:
        self._reverse("step_back", args)

    def req_reverseContinue(self, args: dict) -> None:
        self._reverse("reverse_continue", args)

    # ------------------------------------------------------------------ threads et piles
    def req_threads(self, args: dict) -> dict:
        if self.batch is None:
            return {"threads": []}
        s = self.s
        out = []
        try:
            for t in s.thread_list():
                name = t.name or t.target_id or f"thread {t.id}"
                if not s.target.native and len(out) == 0 and not t.name:
                    name = self.batch.trace.core_name or "cœur"
                if str(t.id).isdigit():
                    out.append({"id": int(t.id), "name": name})
        except Exception:  # noqa: BLE001
            pass
        if not out:
            out = [{"id": 1, "name": self.batch.trace.core_name or "cœur"}]
        cs = self.api.core_set
        if cs is not None:
            for k, c in enumerate(cs.cores):
                if c is not cs.current and c.session is not s:
                    out.append({"id": CORE_BASE + k, "name": f"cœur {c.name} ({c.state()})"})
        for k, t in enumerate(self._tasks()):
            if not t.current:
                out.append({"id": TASK_BASE + k, "name": f"tâche {t.name} ({t.state})"})
        return {"threads": out}

    def _tasks(self) -> list:
        if self._no_rtos or self.s.state != "stopped" or self.batch.target.native:
            return []
        try:
            return self.api.rtos().tasks
        except Exception:  # noqa: BLE001 - pas de FreeRTOS dans l'ELF
            self._no_rtos = True
            return []

    def _frame_id(self, thread, level: int, frame) -> int:
        fid = len(self._frames) + 1
        self._frames[fid] = (thread, level, frame)
        return fid

    def _dap_frame(self, fid: int, f) -> dict:
        d = {"id": fid, "name": f.func or "??", "line": 0, "column": 0,
             "instructionPointerReference": f"{f.pc:#x}"}
        if f.fullname or f.file:
            path = f.fullname or f.file
            d["source"] = {"name": os.path.basename(path), "path": path}
            d["line"] = (f.line or 1) - 1 + self.line_base
            d["column"] = self.line_base
        if f.func and f.func.startswith("<"):
            d["presentationHint"] = "label"
        return d

    def req_stackTrace(self, args: dict) -> dict:
        self._stopped()
        self._drop_varobjs()
        thread = int(args.get("threadId", 1))
        frames, key = self._backtrace(thread)
        start = int(args.get("startFrame", 0) or 0)
        levels = int(args.get("levels", 0) or 0)
        chosen = frames[start:start + levels] if levels else frames[start:]
        out = [self._dap_frame(self._frame_id(key, start + i, f), f) for i, f in enumerate(chosen)]
        return {"stackFrames": out, "totalFrames": len(frames)}

    def _backtrace(self, thread: int):
        """(frames, clé du fil) ; clé : identifiant de gdb, ("tâche", n) ou ("cœur", k)."""
        from .core.session import Frame
        if thread >= CORE_BASE:
            c = self.api.core_set.cores[thread - CORE_BASE]
            if c.session.state != "stopped":
                return [], ("cœur", thread)
            return c.session.backtrace(), ("cœur", thread)
        if thread >= TASK_BASE:
            from .core.rtos import freertos
            t = self._tasks()[thread - TASK_BASE]
            _regs, frames = freertos.task_context(self.s, self.api._rtos, t)
            return frames, ("tâche", thread)
        res = self.s.gdb.command(f"-stack-list-frames --thread {thread}")
        frames = []
        for item in res.get("stack", []):
            f = item.get("frame", item)
            frames.append(Frame(pc=int(f["addr"], 0), func=f.get("func"), file=f.get("file"),
                                fullname=f.get("fullname"),
                                line=int(f["line"]) if f.get("line") else None))
        return frames, str(thread)

    # ------------------------------------------------------------------ variables
    def req_scopes(self, args: dict) -> dict:
        self._stopped()
        key, level, _f = self._frame(args.get("frameId"))
        scopes = []
        if isinstance(key, str):                # thread de gdb : locales et registres de la frame
            scopes.append({"name": "Locales", "presentationHint": "locals", "expensive": False,
                           "variablesReference": self._handle(lambda: self._locals(key, level))})
            scopes.append({"name": "Registres", "presentationHint": "registers", "expensive": False,
                           "variablesReference": self._handle(lambda: self._registers(key, level))})
        if self.api.device is not None:
            scopes.append({"name": f"Périphériques ({self.api.device.name})", "expensive": True,
                           "variablesReference": self._handle(self._peripherals)})
        return {"scopes": scopes}

    def _frame(self, fid) -> tuple:
        if fid is None:
            return ("1", 0, None)
        try:
            return self._frames[int(fid)]
        except (KeyError, ValueError):
            raise DapError("frame périmée (la cible a bougé)") from None

    def req_variables(self, args: dict) -> dict:
        self._stopped()
        fn = self._handles.get(int(args.get("variablesReference", 0)))
        if fn is None:
            return {"variables": []}
        out = fn()
        start, count = int(args.get("start", 0) or 0), int(args.get("count", 0) or 0)
        return {"variables": out[start:start + count] if count else out[start:]}

    def _varobj(self, expr: str, thread: str | None, level: int | None) -> dict:
        opts = f"--thread {thread} --frame {level} " if thread is not None else ""
        vo = self.s.gdb.command(f'-var-create {opts}- * "{_mi(expr)}"')
        self._varobjs.append(vo["name"])
        return vo

    def _children(self, varobj: str) -> list:
        out = []
        for c in self.s.var_children(varobj):
            num = int(c.get("numchild", 0) or 0)
            ref = self._handle(lambda n=c["name"]: self._children(n)) if num else 0
            out.append({"name": c.get("exp", "?"), "value": c.get("value", ""),
                        "type": c.get("type", ""), "variablesReference": ref})
        return out

    def _locals(self, thread: str, level: int) -> list:
        res = self.s.gdb.command(f"-stack-list-variables --thread {thread} --frame {level} "
                                 "--simple-values")
        out = []
        for v in res.get("variables", []):
            name, value, ref = v["name"], v.get("value"), 0
            if value is None:                   # structure, tableau : objet variable de gdb
                try:
                    vo = self._varobj(name, thread, level)
                    value = vo.get("value", "{…}")
                    if int(vo.get("numchild", 0) or 0):
                        ref = self._handle(lambda n=vo["name"]: self._children(n))
                except Exception:  # noqa: BLE001
                    value = "{…}"
            out.append({"name": name, "value": value, "type": v.get("type", ""),
                        "variablesReference": ref, "evaluateName": name})
        return out

    def _registers(self, thread: str, level: int) -> list:
        names = self.s.register_names()
        res = self.s.gdb.command(f"-data-list-register-values --thread {thread} --frame {level} x")
        out = []
        for r in res.get("register-values", []):
            n = int(r.get("number", -1))
            name = names[n] if 0 <= n < len(names) else ""
            if name:
                out.append({"name": name, "value": r.get("value", ""), "variablesReference": 0,
                            "evaluateName": f"${name}"})
        return out

    def _peripherals(self) -> list:
        return [{"name": p.name, "value": f"{p.base:#010x}", "type": p.description or "",
                 "variablesReference": self._handle(lambda p=p: self._periph_regs(p))}
                for p in self.api.device.peripherals]

    def _periph_regs(self, p) -> list:
        from .core import svd
        values: dict[str, int] = {}
        try:
            for addr, n, regs in svd.read_blocks(p):
                values.update(svd.decode_block(p, addr, self.api.mem(addr, n), regs))
        except Exception as exc:  # noqa: BLE001 - périphérique illisible (horloge coupée…)
            return [{"name": "erreur", "value": str(exc), "variablesReference": 0}]
        out = []
        for r in p.registers:
            if r.name not in values:
                why = "écriture seule" if r.access == svd.WRITE_ONLY else "non lu (lecture destructive)"
                out.append({"name": r.name, "value": why, "variablesReference": 0})
                continue
            v = values[r.name]
            fields = [f for f in r.fields if f.access != svd.WRITE_ONLY]
            ref = self._handle(lambda fs=fields, v=v: [
                {"name": f.name, "value": f.format(v), "type": f.bits, "variablesReference": 0}
                for f in fs]) if fields else 0
            out.append({"name": r.name, "value": f"{v:#0{r.nbytes * 2 + 2}x}",
                        "type": f"{p.address(r):#010x}", "variablesReference": ref})
        return out

    def req_evaluate(self, args: dict) -> dict:
        expr = args.get("expression", "")
        ctx = args.get("context", "")
        if ctx == "repl" and expr.startswith(">"):
            return {"result": self._python(expr[1:]), "variablesReference": 0}
        if ctx == "repl" and expr.startswith("-exec "):
            self._stopped()
            return {"result": self.s.cli(expr[6:]).rstrip(), "variablesReference": 0}
        self._stopped()
        key, level, _f = self._frame(args.get("frameId")) if args.get("frameId") else ("1", 0, None)
        thread = key if isinstance(key, str) else None
        try:
            vo = self._varobj(expr, thread, level if thread is not None else None)
        except Exception as exc:  # noqa: BLE001
            raise DapError(str(exc)) from None
        ref = self._handle(lambda n=vo["name"]: self._children(n)) if int(vo.get("numchild", 0) or 0) else 0
        return {"result": vo.get("value", ""), "type": vo.get("type", ""), "variablesReference": ref}

    def _python(self, code: str) -> str:
        """Code Python de la console (`>…`) : mêmes globales que la console de GdbTrace."""
        from .core.scripting import hexdump
        if self._py is None:
            self._py = {"gdbtrace": self.api, "session": self.api.s, "hexdump": hexdump}
        buf = io.StringIO()
        result = ""
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            try:
                try:
                    value = eval(compile(code, "<dap>", "eval"), self._py)   # noqa: S307
                    result = "" if value is None else repr(value)
                except SyntaxError:
                    exec(compile(code, "<dap>", "exec"), self._py)           # noqa: S102
            except Exception:  # noqa: BLE001 - rendu à la console
                traceback.print_exc(limit=3)
        return (buf.getvalue() + result).rstrip()

    # ------------------------------------------------------------------ mémoire et code
    def req_readMemory(self, args: dict) -> dict:
        self._stopped()
        addr = int(str(args["memoryReference"]), 0) + int(args.get("offset", 0) or 0)
        count = int(args.get("count", 0))
        data = b""
        while len(data) < count:
            n = min(MEMORY_CHUNK, count - len(data))
            try:
                data += self.s.read_memory(addr + len(data), n)
            except Exception:  # noqa: BLE001 - zone illisible : la suite aussi
                break
        return {"address": f"{addr:#x}", "data": base64.b64encode(data).decode("ascii"),
                "unreadableBytes": count - len(data)}

    def req_writeMemory(self, args: dict) -> dict:
        self._stopped()
        addr = int(str(args["memoryReference"]), 0) + int(args.get("offset", 0) or 0)
        data = base64.b64decode(args.get("data", ""))
        self.s.write_memory(addr, data)
        self.event("memory", {"memoryReference": args["memoryReference"],
                              "offset": int(args.get("offset", 0) or 0), "count": len(data)})
        return {"bytesWritten": len(data)}

    def req_disassemble(self, args: dict) -> dict:
        self._stopped()
        base = int(str(args["memoryReference"]), 0) + int(args.get("offset", 0) or 0)
        ioff = int(args.get("instructionOffset", 0) or 0)
        count = int(args.get("instructionCount", 0))
        before, after = max(0, -ioff), count + max(0, ioff)
        lo = max(0, base - 4 * before - 16)
        hi = base + 4 * after + 16
        insns = self._disasm(lo, hi)
        idx = next((i for i, x in enumerate(insns) if x["a"] >= base), len(insns))
        out = []
        for k in range(idx + ioff, idx + ioff + count):
            if 0 <= k < len(insns):
                out.append(insns[k]["d"])
            else:                               # hors de la fenêtre lue : instruction invalide
                a = base + 2 * (k - idx)
                out.append({"address": f"{max(a, 0):#x}", "instruction": "??",
                            "presentationHint": "invalid"})
        return {"instructions": out}

    def _disasm(self, lo: int, hi: int) -> list:
        try:
            res = self.s.gdb.command(f"-data-disassemble -s {lo:#x} -e {hi:#x} -- 5")
        except Exception:  # noqa: BLE001 - zone illisible
            return []
        out = []

        def add(i, src=None):
            a = int(i["address"], 0)
            d = {"address": f"{a:#x}", "instruction": i.get("inst", ""),
                 "instructionBytes": i.get("opcodes", "")}
            if i.get("func-name"):
                d["symbol"] = f"{i['func-name']}+{i.get('offset', 0)}"
            if src and src.get("line"):
                path = src.get("fullname") or src.get("file")
                if path:
                    d["location"] = {"name": os.path.basename(path), "path": path}
                d["line"] = int(src["line"]) - 1 + self.line_base
            out.append({"a": a, "d": d})
        for item in res.get("asm_insns", []):
            # mode 5 : groupes « src_and_asm_line » (le parseur MI peut ôter la clé)
            src = item.get("src_and_asm_line", item if "line_asm_insn" in item else None)
            if src is not None:
                for i in src.get("line_asm_insn", []):
                    add(i.get("insn", i) if isinstance(i, dict) else i, src)
            else:
                add(item)
        out.sort(key=lambda x: x["a"])
        return out


# ====================================================================== lancement
def build_parser():
    import argparse
    return argparse.ArgumentParser(
        prog="python -m gdbtrace.dap",
        description="Adaptateur de débogage (Debug Adapter Protocol) de GdbTrace sur stdio : "
                    "VS Code (extension vscode/gdbtrace), nvim-dap, Emacs (dap-mode).")


def main(argv: list[str] | None = None) -> int:
    """stdin / stdout au protocole seul (voir l'en-tête)."""
    build_parser().parse_args(argv)
    proto_in = os.fdopen(os.dup(0), "rb")
    proto_out = os.fdopen(os.dup(1), "wb")
    sys.stdout.flush()
    devnull = os.open(os.devnull, os.O_RDONLY)
    os.dup2(devnull, 0)
    os.close(devnull)
    os.dup2(2, 1)
    sys.stdout = sys.stderr
    adapter = GdbTraceDap(proto_out)
    try:
        adapter.serve(proto_in)
    except KeyboardInterrupt:
        adapter.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
