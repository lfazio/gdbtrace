"""Session de débogage : API haut niveau au-dessus de GdbMI.

C'est l'objet que manipulent l'interface graphique et les scripts (équivalent des
commandes Trace32 : Go, Break, Step, Register, Data.dump, Var.view...).
Toutes les méthodes sont utilisables depuis n'importe quel thread.
"""
from __future__ import annotations

import re
import threading
from dataclasses import dataclass
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
class Breakpoint:
    number: str
    type: str
    enabled: bool
    addr: str | None
    func: str | None
    file: str | None
    line: int | None
    hits: int


def _int(v: Any) -> int:
    return int(str(v), 0)


class Session:
    def __init__(self, gdb_path: str = "gdb-multiarch"):
        self.gdb = GdbMI(gdb_path)
        self.target: TargetServer | None = None
        self.elf: str | None = None
        self.state = "disconnected"  # disconnected | stopped | running
        self.stop_reason: dict = {}
        self._stopped = threading.Event()
        self._listeners: list[EventFn] = []
        self._reg_names: list[str] = []

    # ------------------------------------------------------------------ événements
    def on_event(self, fn: EventFn) -> None:
        """fn(kind, data) avec kind ∈ stopped, running, output, breakpoints, disconnected."""
        self._listeners.append(fn)

    def _emit(self, kind: str, data: dict) -> None:
        for fn in list(self._listeners):
            try:
                fn(kind, data)
            except Exception:
                pass

    def _on_mi(self, rec: dict) -> None:
        t, msg, payload = rec.get("type"), rec.get("message"), rec.get("payload")
        if t == "notify" and msg == "stopped":
            self.state = "stopped"
            self.stop_reason = payload or {}
            self._stopped.set()
            self._emit("stopped", self.stop_reason)
        elif t == "notify" and msg == "running":
            self.state = "running"
            self._stopped.clear()
            self._emit("running", payload or {})
        elif t == "notify" and msg and msg.startswith("breakpoint-"):
            self._emit("breakpoints", payload or {})
        elif t == "notify" and msg == "gdb-exited":
            self.state = "disconnected"
            self._stopped.set()
            self._emit("disconnected", {})
        elif t in ("console", "target", "output"):
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

    def disconnect(self) -> None:
        try:
            if self.state == "running":
                self.halt()
            self.gdb.stop()
        finally:
            if self.target:
                self.target.stop()
            self.state = "disconnected"
            self._emit("disconnected", {})

    def load(self) -> None:
        self.gdb.command("-target-download", timeout=120)

    def monitor(self, cmd: str) -> str:
        """Commande `monitor` brute vers le serveur (OpenOCD : reset halt, flash...)."""
        return self.gdb.console(f"monitor {cmd}")

    def cli(self, cmd: str) -> str:
        """Commande CLI gdb arbitraire (porte de sortie)."""
        return self.gdb.console(cmd)

    # ------------------------------------------------------------------ run-control
    def go(self) -> None:
        self._stopped.clear()
        self.gdb.command("-exec-continue")

    def halt(self, wait: float = 5.0) -> None:
        if self.state != "running":
            return
        self.gdb.command("-exec-interrupt")
        self.wait_stopped(wait)

    def step(self) -> None:          # Step.single (source, entre dans les fonctions)
        self._sync("-exec-step")

    def step_over(self) -> None:     # Step.Over
        self._sync("-exec-next")

    def step_out(self) -> None:      # Go.Return
        self._sync("-exec-finish")

    def step_asm(self) -> None:      # Step.single en mode assembleur
        self._sync("-exec-step-instruction")

    def go_until(self, location: str, timeout: float | None = None) -> None:  # Go <addr>
        self._stopped.clear()
        self.gdb.command(f"-exec-until {location}")
        if timeout is not None:
            self.wait_stopped(timeout)

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
        self._stopped.clear()
        self.gdb.command(cmd)
        self.wait_stopped(timeout)

    def wait_stopped(self, timeout: float = 10.0) -> dict:
        if not self._stopped.wait(timeout):
            raise TimeoutError("la cible ne s'est pas arrêtée")
        return self.stop_reason

    # ------------------------------------------------------------------ breakpoints
    def break_(self, location: str, hw: bool = False, temporary: bool = False,
               condition: str | None = None) -> Breakpoint:
        flags = ("-h " if hw else "") + ("-t " if temporary else "")
        if condition:
            flags += f'-c "{condition}" '
        bkpt = self.gdb.command(f"-break-insert {flags}{location}")["bkpt"]
        return self._bp(bkpt)

    def watch(self, expr: str, access: str = "write") -> str:
        flag = {"write": "", "read": "-r ", "rw": "-a "}[access]
        res = self.gdb.command(f"-break-watch {flag}{expr}")
        wp = res.get("wpt") or res.get("hw-rwpt") or res.get("hw-awpt") or {}
        return wp.get("number", "?")

    def delete_break(self, number: str) -> None:
        self.gdb.command(f"-break-delete {number}")

    def enable_break(self, number: str, enabled: bool = True) -> None:
        self.gdb.command(f"-break-{'enable' if enabled else 'disable'} {number}")

    def breakpoints(self) -> list[Breakpoint]:
        body = self.gdb.command("-break-list")["BreakpointTable"].get("body", [])
        return [self._bp(b) for b in body]

    @staticmethod
    def _bp(b: dict) -> Breakpoint:
        return Breakpoint(
            number=b.get("number", "?"), type=b.get("type", ""),
            enabled=b.get("enabled") == "y", addr=b.get("addr"), func=b.get("func"),
            file=b.get("file"), line=int(b["line"]) if b.get("line") else None,
            hits=int(b.get("times", 0)),
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
        escaped = expr.replace("\\", "\\\\").replace('"', '\\"')
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
