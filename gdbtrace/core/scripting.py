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


class CoverageAPI:
    """COVerage : couverture de code mesurée par la trace ETM, sans instrumentation.

        t32.cov.summary()                    # instructions, branches, fonctions
        t32.cov.list(20)                     # COVerage.ListFunc (les moins couvertes d'abord)
        t32.cov.files()                      # COVerage.ListModule
        t32.cov.export("cov.html")           # COVerage.EXPORT : .html, .info (LCOV), .json
        t32.cov.save("run1.json"); t32.cov.load("run2.json")   # SAVE / LOAD /Add
        t32.cov.reset()                      # COVerage.RESet
    """

    def __init__(self, api: "ScriptAPI"):
        self._api = api

    @property
    def db(self):
        return self._api._etm().cov

    def summary(self) -> str:
        s = self.db.summary()
        print(s)
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
        note = "ETM simulé (QEMU)" if tm is not None and tm.etm_sim is not None else "trace ETM"
        fmt = export(self.db, path, note)
        print(f"couverture exportée ({fmt}) : {path}")
        return fmt


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
        #: appelé après chaque programmation avec le chemin de l'ELF (l'interface s'y recâble)
        self.on_program: Callable[[str], None] | None = None
        #: COVerage : couverture de code depuis la trace ETM
        self.cov = CoverageAPI(self)

    def program(self, elf: str | None = None, verify: bool = True, quiet: bool = False):
        """FLASH.ReProgram : programme `elf` dans la cible (par défaut l'ELF courant, par ex.
        après une recompilation), vérifie, puis reset : la cible est arrêtée au vecteur de
        reset. Retourne le `ProgramResult`."""
        import os
        elf = os.path.abspath(elf) if elf else self.s.elf
        if not elf:
            raise RuntimeError("aucun ELF à programmer")
        res = self.s.program(elf, verify)
        self.symbols = None                     # rechargés à la demande
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
            self.symbols = SymbolTable(self.s.elf)
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
            raise RuntimeError("pas de trace ETM (--etm-sim sous QEMU, --trace/--trace-file sur matériel)")
        self.trace.wait_etm()
        return self.trace.etm

    def trace_list(self, n: int = 30):
        """Trace.List : les `n` dernières instructions exécutées (et exceptions)."""
        from ..trace.etm.qemu_log import ExcEntry, ExcReturn
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
            else:
                ins = etm.image.instr(e.addr)
                f = sym.function_at(e.addr) if sym else None
                le = sym.line_at(e.addr) if sym else None
                loc = f"{le.file.rsplit('/', 1)[-1]}:{le.line}" if le and f else ""
                print(f"{idx:>6}  {e.addr:08x}  {f.name if f else '':<22} {ins.text if ins else '?':<28} {loc}")
        return events

    def coverage(self, top: int | None = 20):
        """Couverture de code par fonction d'après la trace ETM (voir aussi `t32.cov`)."""
        return self.cov.list(top)

    def calls(self, n: int = 40):
        """Flot des appels, retours et exceptions (dernières transitions)."""
        rows = self._etm().call_flow()[-n:]
        icons = {"call": "→", "return": "←", "exception": "⚡", "exc-return": "↩", "start": "•", "branch": "↷"}
        for r in rows:
            label = "retour d'exception" if r.kind == "exc-return" else r.func
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

    def itm(self, port: int = 0) -> str:
        """Texte reçu sur un port ITM (capture SWO en cours ou terminée)."""
        if self.trace is None:
            raise RuntimeError("pas de capture SWO (--swo, --swo-file ou vue ITM)")
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
