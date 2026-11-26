"""Serveur MCP de GdbTrace : un agent (Claude Code…) pilote une session de débogage.

    python -m gdbtrace.mcp                      # stdio ; dans Claude Code :
    claude mcp add gdbtrace -- /chemin/.venv/bin/python -m gdbtrace.mcp

Protocole MCP (Model Context Protocol) sur stdio : JSON-RPC 2.0, un message par ligne ;
`initialize`, `tools/list`, `tools/call`, `ping`. Une session à la fois, sans interface,
ouverte par l'outil `connect` avec les options de la ligne de commande de GdbTrace (ELF,
cible, trace : comme `python -m gdbtrace … --batch`). Les autres outils reprennent l'objet
`gdbtrace` des scripts (docs/scripting.md) : run-control, breakpoints, variables, mémoire,
registres, pile, threads, RTOS, trace, couverture, timing, profil ; `python` exécute du code
avec cet objet, `gdb` une commande CLI brute.

Tout ce que GdbTrace écrit (print des scripts, journaux, messages des fils de trace) est
capturé et rendu dans le résultat de l'outil, ou mis de côté jusqu'au suivant : stdout est
réservé au protocole.
"""
from __future__ import annotations

import contextlib
import inspect
import io
import json
import shlex
import sys
import threading
import traceback

PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
SERVER_INFO = {"name": "gdbtrace", "version": "1.0"}
INSTRUCTIONS = (
    "GdbTrace : débogueur Cortex-M (QEMU, OpenOCD, ST-LINK_gdbserver) et programmes natifs, "
    "avec trace (ITM/SWO, ETM, MTB). Commencer par `connect` (options de "
    "`python -m gdbtrace`, ex. « examples/firmware/demo.elf --etm-sim »). Une cible arrêtée "
    "se lit par `status`, `backtrace`, `registers`, `evaluate`… ; `go` attend l'arrêt "
    "(breakpoint) quelques secondes. La trace d'instructions (`trace_list`, `coverage`, "
    "`timing`) porte sur l'exécution précédant l'arrêt.")

_S = {"type": "string"}
_I = {"type": "integer"}
_N = {"type": "number"}
_B = {"type": "boolean"}


def _tool(name: str, description: str, props: dict | None = None,
          required: tuple = ()) -> dict:
    schema = {"type": "object", "properties": props or {}, "additionalProperties": False}
    if required:
        schema["required"] = list(required)
    return {"name": name, "description": description, "inputSchema": schema}


TOOLS = [
    _tool("connect", "Ouvre une session de débogage (ferme la précédente). `args` : options de "
          "`python -m gdbtrace` sans --batch ni --script : ELF puis cible et trace, ou une "
          "configuration (« --config configs/qemu-demo.yaml »). Exemples : "
          "« examples/firmware/demo.elf » (QEMU) ; « demo.elf --etm-sim » (trace ETM simulée) ; "
          "« fw.elf --openocd configs/nucleo-f746zg.cfg --program --swo-config --cpu-hz "
          "16000000 » ; « fw.elf --stlink --stlink-ap 1 --ram-boot --program » ; "
          "« --pid 1234 » (programme natif lancé). Chemins relatifs au répertoire du serveur. "
          "Rend le journal de mise en route et l'arrêt courant.",
          {"args": _S}, ("args",)),
    _tool("disconnect", "Ferme la session (gdb, serveur de la cible). `leave_running` : la "
          "cible continue de tourner.", {"leave_running": _B}),
    _tool("status", "État de la session : cible, état (stopped, running, exited), raison du "
          "dernier arrêt, position (fonction, fichier:ligne), état de la trace."),
    _tool("go", "Lance la cible (Go). `until` : jusqu'à cet emplacement (fonction, "
          "fichier:ligne, *adresse). `wait` : secondes d'attente de l'arrêt (défaut 5 ; 0 : ne "
          "pas attendre) ; sans arrêt, la cible reste en marche (`halt`, `status`).",
          {"until": _S, "wait": _N}),
    _tool("halt", "Arrête la cible (Break)."),
    _tool("step", "Pas à pas : `kind` = line (entre dans les fonctions), over (par-dessus), "
          "out (jusqu'au retour), instruction ; `count` pas (défaut 1).",
          {"kind": {"type": "string", "enum": ["line", "over", "out", "instruction"]},
           "count": _I}),
    _tool("reset", "Reset de la cible, arrêtée au vecteur de reset (programme natif : relancé "
          "au début de main)."),
    _tool("break", "Pose un breakpoint. `location` : fonction, fichier:ligne ou *adresse ; "
          "`condition` (expression C) ; `count` : arrêt au Nᵉ passage ; `log` : message à "
          "chaque passage ({expr}, {expr:hex}…), la cible repart ; `hardware` ; `temporary`.",
          {"location": _S, "condition": _S, "count": _I, "log": _S, "hardware": _B,
           "temporary": _B}, ("location",)),
    _tool("watch", "Pose un watchpoint sur une expression (variable) : `access` = write "
          "(défaut), read ou rw (lecture et écriture).",
          {"expression": _S, "access": {"type": "string", "enum": ["write", "read", "rw"]}},
          ("expression",)),
    _tool("delete", "Supprime un breakpoint ou un watchpoint par son numéro.",
          {"number": _S}, ("number",)),
    _tool("breakpoints", "Liste les breakpoints et watchpoints (numéro, type, lieu, passages, "
          "condition, action) et les derniers messages de leurs actions."),
    _tool("evaluate", "Évalue une expression C. Cible en marche : relit par le canal mémoire "
          "une variable à adresse fixe (globale, champ, élément de tableau) déjà évaluée une "
          "fois cible arrêtée. `format` : natural, hex, dec, oct, bin, ascii.",
          {"expression": _S, "format": _S}, ("expression",)),
    _tool("set", "Affecte une valeur à une variable ou un registre (cible arrêtée).",
          {"expression": _S, "value": _S}, ("expression", "value")),
    _tool("registers", "Registres du cœur (thread courant)."),
    _tool("backtrace", "Pile d'appels, avec les variables locales de la frame courante."),
    _tool("memory", "Dump hexadécimal de `length` octets (défaut 64, 4096 au plus) à "
          "`address` : nombre, ou expression dont la valeur est l'adresse (« &compteur », un "
          "tableau, un pointeur) ; cible en marche : adresse numérique, lue par le canal mémoire "
          "en direct.", {"address": _S, "length": _I}, ("address",)),
    _tool("threads", "Threads (programme natif, ou tâches RTOS exposées par le serveur) ; "
          "`select` : nom ou numéro du thread qui devient courant.", {"select": _S}),
    _tool("rtos", "Tâches FreeRTOS : état, priorité, pile utilisée, CPU, objet attendu."),
    _tool("trace_list", "Trace.List : les `n` dernières instructions exécutées (défaut 40), "
          "avec exceptions et trous.", {"n": _I}),
    _tool("calls", "Flot d'appels de la trace (appels, retours, exceptions) : `n` lignes.",
          {"n": _I}),
    _tool("coverage", "Couverture de code par la trace : résumé et fonctions les moins "
          "couvertes (`top`, défaut 20).", {"top": _I}),
    _tool("call_tree", "Arbre d'appels d'après la trace (brut et net par chemin d'appel ; une "
          "exception est la racine de son arbre) : `depth` niveaux (défaut 4), nœuds d'au moins "
          "`min_pct` % du temps (défaut 1).", {"depth": _I, "min_pct": _N}),
    _tool("trace_find", "Cherche dans la trace d'instructions (les `n` derniers événements, défaut "
          "50000) : adresse 0x…, nom de fonction (chaque entrée), « exception » ou « exception "
          "SysTick », ou texte ; `top` lignes (défaut 20).", {"query": _S, "n": _I, "top": _I}, ("query",)),
    _tool("timing", "Durées par fonction et par exception d'après la trace (brut, net, "
          "min/max/moyenne) : `top` lignes (défaut 20).", {"top": _I}),
    _tool("chrono", "Temps du cœur pendant la dernière exécution (de la reprise à l'arrêt : go "
          "jusqu'à un breakpoint, pas…) et cumul, mesuré par la cible sans trace : SysTick et "
          "compteur de ticks (Cortex-M0/M0+, QEMU), DWT_CYCCNT au-delà. `reset` : remet le "
          "cumul à zéro.", {"reset": _B}),
    _tool("symbols", "Fonctions et variables globales de l'ELF dont le nom contient `pattern` (ou "
          "motif avec * et ?) : adresse, taille, fichier:ligne. `kind` : tout, fonctions, "
          "variables ; `top` lignes (défaut 50).", {"pattern": _S, "kind": _S, "top": _I}),
    _tool("terminal", "Texte écrit par le programme (semihosting, RTT : --semihosting, --rtt) depuis "
          "l'appel précédent ; `send` : texte envoyé au programme d'abord (RTT descendant, entrée "
          "du semihosting).", {"send": _S}),
    _tool("fault", "Analyse de la faute en cours (cible arrêtée dans HardFault, BusFault…) : "
          "cause en clair, adresse et instruction fautives, fonction et ligne, cadre empilé. "
          "`catch` : true pose l'arrêt à l'entrée de toute faute, false le retire.", {"catch": _B}),
    _tool("profile", "Profil statistique (PERF) pendant `seconds` (défaut 1) : la cible tourne, "
          "puis revient à son état.", {"seconds": _N}),
    _tool("itm", "Texte reçu sur un port ITM (`port`, défaut 0) par la capture SWO.",
          {"port": _I}),
    _tool("program", "Programme un ELF dans la cible (reset, écriture, vérification, reset) ; "
          "défaut : l'ELF de la session.", {"elf": _S}),
    _tool("gdb", "Commande CLI gdb brute (ex. « info sharedlibrary », « x/4wx 0x20000000 »).",
          {"command": _S}, ("command",)),
    _tool("python", "Exécute du code Python avec l'objet `gdbtrace` des scripts (et `session`, "
          "`hexdump`) : tout ce que les autres outils ne couvrent pas (docs/scripting.md). "
          "Rend ce que le code affiche.", {"code": _S}, ("code",)),
]


class ToolError(Exception):
    """Erreur attendue d'un outil : rendue telle quelle (isError), sans pile Python."""


class GdbTraceMcp:
    def __init__(self, out=None):
        self.out = out or sys.stdout
        self.batch = None
        self.args_text = ""
        self._pending: list[str] = []            # messages arrivés entre deux appels
        self._layouts: dict = {}                 # expression → disposition (lecture en direct)
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ protocole
    def send(self, msg: dict) -> None:
        with self._lock:
            self.out.write(json.dumps(msg, ensure_ascii=False) + "\n")
            self.out.flush()

    def handle(self, msg: dict) -> dict | None:
        """Réponse à un message JSON-RPC (None pour une notification)."""
        mid, method, params = msg.get("id"), msg.get("method"), msg.get("params") or {}
        if mid is None:                          # notification (initialized, cancelled…)
            return None
        try:
            if method == "initialize":
                asked = params.get("protocolVersion")
                version = asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0]
                result = {"protocolVersion": version, "serverInfo": SERVER_INFO,
                          "capabilities": {"tools": {"listChanged": False}},
                          "instructions": INSTRUCTIONS}
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": TOOLS}
            elif method == "tools/call":
                result = self.call(params.get("name", ""), params.get("arguments") or {})
            else:
                return {"jsonrpc": "2.0", "id": mid,
                        "error": {"code": -32601, "message": f"méthode inconnue : {method}"}}
        except Exception as exc:  # noqa: BLE001 - une erreur interne ne tue pas le serveur
            return {"jsonrpc": "2.0", "id": mid,
                    "error": {"code": -32603, "message": f"{type(exc).__name__}: {exc}"}}
        return {"jsonrpc": "2.0", "id": mid, "result": result}

    def serve(self, inp=None) -> None:
        for line in inp or sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError as exc:
                self.send({"jsonrpc": "2.0", "id": None,
                           "error": {"code": -32700, "message": f"JSON illisible : {exc}"}})
                continue
            reply = self.handle(msg)
            if reply is not None:
                self.send(reply)
        self.close()

    # ------------------------------------------------------------------ outils
    def call(self, name: str, arguments: dict) -> dict:
        fn = getattr(self, f"tool_{name}", None)
        if fn is None:
            return _result(f"outil inconnu : {name}", error=True)
        try:
            inspect.signature(fn).bind(**arguments)
        except TypeError as exc:
            return _result(f"arguments invalides pour {name} : {exc}", error=True)
        buf = io.StringIO()
        error = False
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            try:
                text = fn(**arguments)
            except ToolError as exc:
                text, error = str(exc), True
            except Exception as exc:  # noqa: BLE001 - rendu à l'agent, la session continue
                text, error = f"{type(exc).__name__}: {exc}", True
                if not str(exc):
                    text += "\n" + traceback.format_exc(limit=4)
        with self._lock:
            pending, self._pending = self._pending, []
        parts = [p for p in ("\n".join(pending), buf.getvalue().rstrip(), (text or "").rstrip()) if p]
        return _result("\n".join(parts) or "fait", error=error)

    def _on_event(self, kind: str, data: dict) -> None:
        """Thread lecteur de gdb : messages des actions et sortie du programme (natif) gardés
        pour le résultat de l'outil en cours ou du suivant ; ne bloque jamais."""
        if kind == "output" and data.get("stream") in ("action", "target"):
            text = data.get("text", "").rstrip("\n")
            if text:
                self._log(text)
        elif kind == "exited":
            code = data.get("exit-code", "0")
            self._log(f"programme terminé ({data.get('reason', '?')}, code "
                      f"{int(code, 8) if code.isdigit() else code})")

    def _log(self, text: str) -> None:
        """Messages des fils de trace hors d'un appel : rendus avec le résultat suivant."""
        with self._lock:
            self._pending.append(str(text).rstrip())

    def _need(self):
        if self.batch is None:
            raise ToolError("pas de session : appeler `connect` d'abord")
        return self.batch

    @property
    def api(self):
        return self._need().api

    def _where(self) -> str:
        b = self._need()
        s = b.s
        if s.state == "stopped":
            try:
                f = s.frame()
                loc = f" {f.file}:{f.line}" if f.file else ""
                return f"arrêtée : {f.func or '??'} ({f.pc:#x}){loc}"
            except Exception as exc:  # noqa: BLE001
                return f"arrêtée ({exc})"
        if s.state == "exited":
            return f"programme terminé (code {s.exit_code})"
        return s.state

    # --- session
    def tool_connect(self, args: str) -> str:
        from .__main__ import BatchSession, build_target, parse_command_line, resolve_session
        self.close()
        argv = shlex.split(args)
        if any(a in ("--batch", "--script", "--coverage") for a in argv):
            raise ToolError("--batch / --script / --coverage : sans objet ici (outils `python`, "
                            "`coverage`)")
        try:                                    # mêmes règles que la ligne de commande
            p, ns, config = parse_command_line(argv + ["--batch"])
            elf, program, verify, load, svd_warning = resolve_session(p, ns)
        except SystemExit:                      # argparse a écrit l'erreur (capturée)
            raise ToolError("options refusées") from None
        log: list[str] = [svd_warning] if svd_warning else []
        target = build_target(ns, elf)
        try:
            b = BatchSession(ns, elf, target, load, program, verify, log=log.append,
                             on_event=self._on_event, config=config)
        except Exception as exc:  # noqa: BLE001 - connexion impossible : le journal l'explique
            raise ToolError("\n".join(log + [f"connexion impossible : {exc}"])) from exc
        b.trace.on_error = self._log
        self.batch, self.args_text, self._layouts = b, args, {}
        try:
            b.setup()
        except Exception as exc:  # noqa: BLE001 - la session reste ouverte, sans cette capture
            raise ToolError("\n".join(log + [f"mise en route de la trace : {exc}",
                                              f"session ouverte : {self._where()}"])) from exc
        if ns.go and b.s.state == "stopped":
            b.s.go()
        return "\n".join(log + [f"session : {target.remote} — {self._where()}"])

    def tool_disconnect(self, leave_running: bool = False) -> str:
        if self.batch is None:
            return "pas de session"
        self.close(leave_running)
        return "session fermée" + (" (la cible continue)" if leave_running else "")

    def tool_status(self) -> str:
        if self.batch is None:
            return "pas de session (`connect`)"
        b = self.batch
        lines = [f"session : {self.args_text}", f"cible : {b.target.remote}",
                 f"état : {self._where()}"]
        reason = (b.s.stop_reason or {}).get("reason")
        if reason and b.s.state == "stopped":
            lines.append(f"raison du dernier arrêt : {reason}")
        if b.trace.etm is not None:
            lines.append(f"trace d'instructions : {b.trace.trace_status()}")
        if b.trace.source is not None:
            lines.append(f"capture SWO : {b.trace.status()}")
        return "\n".join(lines)

    # --- run-control
    def tool_go(self, until: str | None = None, wait: float = 5.0) -> str:
        from .core.scripting import stop_summary
        api = self.api
        api.go(until)
        if not wait:
            return "en marche"
        try:
            rec = api.wait(wait)
        except TimeoutError:
            return f"toujours en marche après {wait:g} s (`halt` pour l'arrêter)"
        self._settle()
        text = f"arrêt : {stop_summary(rec)}"
        value = rec.get("value") if isinstance(rec.get("value"), dict) else {}
        if "new" in value:
            text += f"\nvaleur : {value.get('old', '?')} → {value['new']}"
        return text

    def tool_halt(self) -> str:
        self.api.halt()
        self._settle()
        return self._where()

    def tool_step(self, kind: str = "line", count: int = 1) -> str:
        api = self.api
        fn = {"line": api.step, "over": api.over, "instruction": api.stepi}.get(kind)
        if kind == "out":
            api.out()
        elif fn is None:
            raise ToolError("kind : line, over, out ou instruction")
        else:
            fn(count)
        self._settle()
        return self._where()

    def tool_reset(self) -> str:
        self.api.reset()
        return self._where()

    def _settle(self) -> None:
        """Après un arrêt : la trace de l'exécution est décodée avant qu'on la lise."""
        b = self.batch
        if b is not None and b.trace.etm is not None:
            b.trace.wait_etm(60)

    # --- breakpoints
    def tool_break(self, location: str, condition: str | None = None, count: int | None = None,
                   log: str | None = None, hardware: bool = False, temporary: bool = False) -> str:
        bp = self.api.break_(location, hw=hardware, temporary=temporary, condition=condition,
                             count=count, log=log)
        return f"breakpoint {_bp_text(bp)}"

    def tool_watch(self, expression: str, access: str = "write") -> str:
        num = self.api.watch(expression, access)
        return f"watchpoint {num} sur {expression} ({access})"

    def tool_delete(self, number: str) -> str:
        self.api.delete(number)
        return f"{number} supprimé"

    def tool_breakpoints(self) -> str:
        bps = self.api.breaks()
        lines = [_bp_text(b) for b in bps] or ["aucun breakpoint"]
        log = self.api.break_log(20)
        if log:
            lines += ["messages des actions :"] + [f"  {m}" for m in log]
        return "\n".join(lines)

    # --- données
    def tool_evaluate(self, expression: str, format: str = "natural") -> str:
        from .core import live_vars
        from .core.formats import normalize
        api = self.api
        fmt = normalize(format)
        if api.s.state != "stopped":
            lay = self._layouts.get(expression)
            if api.live is None or lay is None:
                why = ("pas de canal mémoire en direct pour cette cible" if api.live is None else
                       "expression jamais évaluée cible arrêtée, ou sans adresse fixe (locale, "
                       "registre, type composé)")
                raise ToolError(f"cible en marche : {why} ; `halt` d'abord")
            value = live_vars.decode(lay, api.live.read(lay.addr, lay.size), fmt)
            return f"{expression} = {value} (lu en direct)"
        value = api.eval(expression, fmt)
        if api.live is not None and expression not in self._layouts:
            lay = self._live_layout(expression)
            if lay is not None:
                self._layouts[expression] = lay
        return f"{expression} = {value}"

    def _live_layout(self, expr: str):
        """Disposition d'une feuille à adresse fixe (globale, champ, élément), cible arrêtée :
        `evaluate` la relira ensuite cible en marche par le canal mémoire (core/live_vars)."""
        from .core import live_vars
        s = self.api.s
        try:
            local_names = {v.get("name") for v in s.locals()}
        except Exception:  # noqa: BLE001 - pas de frame avec informations de débogage
            local_names = set()
        if live_vars.root_identifier(expr) in local_names:
            return None
        try:
            addr, size = s.address_of_expr(expr), s.sizeof(expr)
            if not 0 < size <= live_vars.MAX_LIVE_SIZE:
                return None
            kind = live_vars.classify(s.ptype(expr), size)
        except Exception:  # noqa: BLE001 - pas une lvalue, type inconnu…
            return None
        if kind is None:
            return None
        return live_vars.LeafLayout(addr=addr, size=size, kind=kind[0], enum_names=kind[1],
                                    live=True)

    def tool_set(self, expression: str, value: str) -> str:
        api = self.api
        api.set(expression, value)
        return f"{expression} = {api.eval(expression)}"

    def tool_registers(self) -> str:
        self.api.r()
        return ""

    def tool_backtrace(self) -> str:
        api = self.api
        api.bt()
        try:
            loc = api.s.locals()
        except Exception:  # noqa: BLE001 - frame sans informations de débogage
            loc = []
        if loc:
            print("variables locales :")
            for v in loc:
                print(f"  {v.get('name')} = {v.get('value', '…')}")
        return ""

    def tool_memory(self, address: str, length: int = 64) -> str:
        from .core.scripting import hexdump
        api = self.api
        if not 0 < length <= 4096:
            raise ToolError("length : de 1 à 4096 octets")
        try:
            addr = int(str(address), 0)
        except ValueError:
            if api.s.state != "stopped":
                raise ToolError("cible en marche : une adresse numérique (les symboles se "
                                "résolvent cible arrêtée)") from None
            addr = int(api.s.evaluate(f"(unsigned long)({address})").split()[0], 0)
        return hexdump(api.mem(addr, length), addr)

    def tool_threads(self, select: str | None = None) -> str:
        api = self.api
        if select:
            api.thread(select)
            return ""
        api.threads()
        return ""

    def tool_rtos(self) -> str:
        self.api.tasks()
        return ""

    # --- trace
    def tool_trace_list(self, n: int = 40) -> str:
        self._settle()
        self.api.trace_list(n)
        return ""

    def tool_calls(self, n: int = 40) -> str:
        self._settle()
        self.api.calls(n)
        return ""

    def tool_coverage(self, top: int = 20) -> str:
        self._settle()
        self.api.coverage(top)
        return ""

    def tool_timing(self, top: int = 20) -> str:
        self._settle()
        self.api.timing(top)
        return ""

    def tool_call_tree(self, depth: int = 4, min_pct: float = 1.0) -> str:
        self.api.call_tree(depth, min_pct)
        return ""

    def tool_trace_find(self, query: str, n: int = 50_000, top: int = 20) -> str:
        self.api.trace_find(query, n, top)
        return ""

    def tool_chrono(self, reset: bool = False) -> str:
        if reset:
            self.api.chrono_reset()
            return "cumul du chrono remis à zéro"
        self.api.chrono()
        return ""

    def tool_symbols(self, pattern: str = "", kind: str = "tout", top: int = 50) -> str:
        self.api.sym_list(pattern, kind, top)
        return ""

    def tool_terminal(self, send: str | None = None) -> str:
        out = ""
        if send:
            out = f"envoyé par {self.api.terminal_send(send)}\n"
        text = self.api.terminal(quiet=True)
        return out + (text or "(rien de nouveau)")

    def tool_fault(self, catch: bool | None = None) -> str:
        if catch is not None:
            return self.api.break_on_fault(catch)
        self.api.fault()
        return ""

    def tool_profile(self, seconds: float = 1.0) -> str:
        self.api.profile(seconds)
        return ""

    def tool_itm(self, port: int = 0) -> str:
        return self.api.itm(port) or f"(rien reçu sur le port {port})"

    def tool_program(self, elf: str | None = None) -> str:
        import os
        self.api.program(os.path.abspath(elf) if elf else None)
        self._layouts = {}
        return self._where()

    # --- portes de sortie
    def tool_gdb(self, command: str) -> str:
        return self.api.s.cli(command)

    def tool_python(self, code: str) -> str:
        from .core.scripting import hexdump
        api = self.api
        ns = {"gdbtrace": api, "session": api.s, "hexdump": hexdump}
        try:
            value = eval(compile(code, "<mcp>", "eval"), ns)   # expression : sa valeur
        except SyntaxError:
            exec(compile(code, "<mcp>", "exec"), ns)
            return ""
        return "" if value is None else repr(value)

    # ------------------------------------------------------------------ fin
    def close(self, leave_running: bool = False) -> None:
        b, self.batch = self.batch, None
        if b is not None:
            try:
                b.close(leave_running=leave_running)
            except Exception:  # noqa: BLE001 - fermeture au mieux
                pass


def _result(text: str, error: bool = False) -> dict:
    return {"content": [{"type": "text", "text": text}], "isError": error}


def _bp_text(b) -> str:
    """Une ligne par breakpoint : numéro, type, lieu, passages, options."""
    where = b.expr or (f"{b.func or '??'} {b.file}:{b.line}" if b.file else (b.func or b.addr or ""))
    extra = [f"{b.hits} passage(s)"]
    if not b.enabled:
        extra.append("désactivé")
    if b.condition:
        extra.append(f"si {b.condition}")
    if b.count:
        extra.append(f"au {b.count}ᵉ passage")
    if b.action:
        extra.append(b.action)
    return f"{b.number} {b.type} {where} ({', '.join(extra)})"


def main() -> int:
    """stdin / stdout sont au protocole seul : un processus fils (gdb, QEMU, OpenOCD) qui
    hériterait des descripteurs 0 et 1 lirait les requêtes ou écrirait dans les réponses. Ils
    sont donc dupliqués pour le protocole, puis 0 ← /dev/null et 1 ← stderr (journal)."""
    import os
    proto_in = os.fdopen(os.dup(0), "r", encoding="utf-8", errors="replace")
    proto_out = os.fdopen(os.dup(1), "w", encoding="utf-8")
    sys.stdout.flush()
    devnull = os.open(os.devnull, os.O_RDONLY)
    os.dup2(devnull, 0)
    os.close(devnull)
    os.dup2(2, 1)
    sys.stdout = sys.stderr                     # tout autre print va dans le journal
    server = GdbTraceMcp(proto_out)
    try:
        server.serve(proto_in)
    except KeyboardInterrupt:
        server.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
