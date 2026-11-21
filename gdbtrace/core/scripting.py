"""API de script : l'équivalent Python du langage PRACTICE de Trace32.

Dans la console et les scripts, l'objet `t32` est disponible :

    t32.break_("fib")            # Break.Set fib
    t32.go(); t32.wait()         # Go + WAIT !STATE.RUN()
    print(t32.eval("counter"))   # Var.view counter
    t32.d(0x20000000, 32)        # Data.dump
    t32.r()                      # Register.view
    t32.mem(0x20000000, 16)      # lecture mémoire, même cible en marche (canal live)
    t32.periph("SysTick")        # PER.view (nécessite un SVD)

Un script `.py` est exécuté avec `t32.run_script("init.py")` ou au démarrage avec
l'option `--script`.
"""
from __future__ import annotations

import code
import contextlib
import io
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


class ScriptAPI:
    def __init__(self, session: Session, live: LiveMemory | None = None,
                 device: svd.Device | None = None):
        self.s = session
        #: canal mémoire en direct (None si le serveur gdb n'en offre pas)
        self.live = live
        #: périphériques décrits par le SVD chargé (None sinon)
        self.device = device
        self._rtos: freertos.FreeRtosLayout | None = None

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
                                     current_sp=self.s.register("sp"))
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
        if until:
            self.s.go_until(until)
        else:
            self.s.go()

    def halt(self) -> None: self.s.halt()
    def step(self, n: int = 1) -> None:
        for _ in range(n):
            self.s.step()
    def over(self, n: int = 1) -> None:
        for _ in range(n):
            self.s.step_over()
    def out(self) -> None: self.s.step_out()
    def stepi(self, n: int = 1) -> None:
        for _ in range(n):
            self.s.step_asm()
    def reset(self) -> None: self.s.reset()
    def wait(self, timeout: float = 10.0) -> dict: return self.s.wait_stopped(timeout)

    @property
    def running(self) -> bool: return self.s.state == "running"

    # breakpoints
    def break_(self, loc: str, **kw): return self.s.break_(loc, **kw)
    def watch(self, expr: str, access: str = "write"): return self.s.watch(expr, access)
    def delete(self, num: str) -> None: self.s.delete_break(str(num))
    def breaks(self): return self.s.breakpoints()

    # données
    def eval(self, expr: str) -> str: return self.s.evaluate(expr)
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
        for i, f in enumerate(self.s.backtrace()):
            loc = f"{f.file}:{f.line}" if f.file else ""
            print(f"#{i} {f.pc:08x} {f.func or '??'} {loc}")

    # mémoire en direct et périphériques
    def mem(self, addr: int, n: int) -> bytes:
        """Lecture mémoire qui marche aussi cible en marche (canal live si nécessaire)."""
        if self.s.state == "stopped":
            return self.s.read_memory(addr, n)
        return self.live_read(addr, n)

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
            raise RuntimeError("aucun SVD chargé (t32.load_svd ou --svd)")
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

    def run_script(self, path: str) -> None:
        runpy.run_path(path, init_globals={"t32": self, "session": self.s})


class Console:
    """Interpréteur Python interactif, exécuté hors du thread GUI.

    `write` reçoit tout ce qui est affiché (stdout/stderr) ; il est appelé depuis le
    thread d'exécution, à l'appelant de le renvoyer vers la GUI de façon sûre.
    """

    def __init__(self, api: ScriptAPI, write: Callable[[str], None]):
        self.api = api
        self.write = write
        self.namespace = {"t32": api, "session": api.s, "hexdump": hexdump}
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
        threading.Thread(target=worker, name="t32-console", daemon=True).start()


class _Writer(io.TextIOBase):
    def __init__(self, fn: Callable[[str], None]):
        self.fn = fn

    def write(self, s: str) -> int:
        if s:
            self.fn(s)
        return len(s)
