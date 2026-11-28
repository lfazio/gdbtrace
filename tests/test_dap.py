"""Adaptateur DAP (gdbtrace/dap.py) : un client DAP en Python pilote `python -m gdbtrace.dap`
comme le ferait VS Code — initialize, launch, breakpoints, configurationDone, arrêts, piles,
variables, évaluation (C, Python, gdb), pas, mémoire, désassemblage, pause, breakpoint posé
cible en marche, disconnect — sur la démo QEMU, puis les tâches FreeRTOS en threads."""
import base64
import io
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time

import pytest
from conftest import ROOT, needs_qemu

from gdbtrace.dap import GdbTraceDap, encode_message, read_message

MAIN_C = os.path.join(ROOT, "examples", "firmware", "main.c")


def line_of(text: str, path: str = MAIN_C) -> int:
    with open(path, encoding="utf-8") as fh:
        return next(i for i, line in enumerate(fh, 1) if text in line)


class Client:
    """Client DAP minimal : requêtes numérotées, réponses attendues, événements en file."""

    def __init__(self, tmp_path):
        self.log = open(tmp_path / "dap-stderr.txt", "w+")
        self.p = subprocess.Popen([sys.executable, "-m", "gdbtrace.dap"], cwd=ROOT,
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.log,
                                  env={**os.environ, "PYTHONPATH": ROOT})
        self.seq = 0
        self.events: queue.Queue = queue.Queue()
        self.replies: dict = {}
        self.cv = threading.Condition()
        self.output: list[str] = []
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        while True:
            msg = read_message(self.p.stdout)
            if msg is None:
                return
            if msg["type"] == "response":
                with self.cv:
                    self.replies[msg["request_seq"]] = msg
                    self.cv.notify_all()
            elif msg["type"] == "event":
                if msg["event"] == "output":
                    self.output.append(msg["body"]["output"])
                self.events.put(msg)

    def request(self, command: str, timeout: float = 60, **arguments) -> dict:
        self.seq += 1
        seq = self.seq
        self.p.stdin.write(encode_message({"seq": seq, "type": "request", "command": command,
                                           "arguments": arguments}))
        self.p.stdin.flush()
        with self.cv:
            assert self.cv.wait_for(lambda: seq in self.replies, timeout), (command, self.stderr())
            return self.replies.pop(seq)

    def ok(self, command: str, **arguments) -> dict:
        r = self.request(command, **arguments)
        assert r["success"], (command, r.get("message"), self.stderr())
        return r.get("body") or {}

    def event(self, name: str, timeout: float = 30) -> dict:
        while True:
            try:
                msg = self.events.get(timeout=timeout)
            except queue.Empty:
                raise AssertionError(f"pas d'événement {name} ; sortie : {self.output[-10:]}"
                                     f"\n{self.stderr()}") from None
            if msg["event"] == name:
                return msg.get("body") or {}

    def stderr(self) -> str:
        self.log.flush()
        self.log.seek(0)
        return self.log.read()[-3000:]

    def close(self):
        if self.p.poll() is None:
            try:
                self.request("disconnect", timeout=20, terminateDebuggee=True)
            except Exception:  # noqa: BLE001
                pass
        try:
            self.p.wait(timeout=20)
        except subprocess.TimeoutExpired:
            self.p.kill()
        self.log.close()


def test_message_framing_and_errors():
    """En-têtes Content-Length (corps UTF-8), lignes vides tolérées ; requête inconnue et
    options refusées rendues en erreur, sans tuer l'adaptateur."""
    msgs = [{"seq": 1, "type": "request", "command": "initialize", "arguments": {}},
            {"seq": 2, "type": "request", "command": "évaluer", "arguments": {}}]
    raw = io.BytesIO(b"\r\n".join(encode_message(m) for m in msgs))
    assert read_message(raw) == msgs[0] and read_message(raw) == msgs[1]
    assert read_message(raw) is None
    out = io.BytesIO()
    dap = GdbTraceDap(out)
    caps = dap.handle(msgs[0])["body"]
    assert caps["supportsConfigurationDoneRequest"] and caps["supportsDisassembleRequest"]
    assert [f["filter"] for f in caps["exceptionBreakpointFilters"]] == ["faute"]
    bad = dap.handle(msgs[1])
    assert not bad["success"] and "non prise en charge" in bad["message"]
    r = dap.handle({"seq": 3, "type": "request", "command": "launch",
                    "arguments": {"args": "x.elf --batch"}})
    assert not r["success"] and "--batch" in r["message"]
    r = dap.handle({"seq": 4, "type": "request", "command": "stackTrace", "arguments": {}})
    assert not r["success"] and "launch" in r["message"]


def _address(text: str) -> int:
    return int(re.search(r"0x[0-9a-f]+", text).group(0), 16)


@needs_qemu
def test_session_on_qemu(elf, tmp_path):
    c = Client(tmp_path)
    try:
        caps = c.ok("initialize", adapterID="gdbtrace", linesStartAt1=True)
        assert caps["supportsInstructionBreakpoints"] and caps["supportsLogPoints"]
        c.ok("launch", program=os.path.relpath(elf, ROOT))
        c.event("initialized")
        line = line_of("counter++;")
        bps = c.ok("setBreakpoints", source={"path": MAIN_C}, breakpoints=[{"line": line}])
        bp = bps["breakpoints"][0]
        assert bp["verified"] and bp["line"] == line
        c.ok("setFunctionBreakpoints", breakpoints=[])
        c.ok("setExceptionBreakpoints", filters=[])
        c.ok("configurationDone")
        st = c.event("stopped")
        assert st["reason"] == "breakpoint" and st["hitBreakpointIds"] == [bp["id"]]
        tid = st["threadId"]
        assert tid in [t["id"] for t in c.ok("threads")["threads"]]

        frames = c.ok("stackTrace", threadId=tid)["stackFrames"]
        f0 = frames[0]
        assert f0["name"] == "main" and f0["line"] == line
        assert f0["source"]["path"].endswith("main.c")
        scopes = {s["name"]: s["variablesReference"] for s in c.ok("scopes", frameId=f0["id"])["scopes"]}
        assert {"Locales", "Registres"} <= set(scopes)
        regs = {v["name"]: v["value"] for v in c.ok("variables", variablesReference=scopes["Registres"])["variables"]}
        assert {"pc", "sp", "r0"} <= set(regs) and _address(regs["pc"]) == int(f0["instructionPointerReference"], 16)

        counter = int(c.ok("evaluate", expression="counter", frameId=f0["id"], context="watch")["result"])
        sens = c.ok("evaluate", expression="sensors", frameId=f0["id"], context="watch")
        items = c.ok("variables", variablesReference=sens["variablesReference"])["variables"]
        assert len(items) == 4 and items[0]["variablesReference"] > 0
        fields = {v["name"] for v in c.ok("variables", variablesReference=items[0]["variablesReference"])["variables"]}
        assert fields == {"id", "value", "status"}
        assert c.ok("evaluate", expression=">gdbtrace.s.state", context="repl")["result"] == "'stopped'"
        assert "Line" in c.ok("evaluate", expression="-exec info line main", context="repl")["result"]

        c.ok("next", threadId=tid)                               # counter++ → update_sensor(...)
        assert c.event("stopped")["reason"] == "step"
        f0 = c.ok("stackTrace", threadId=tid)["stackFrames"][0]
        assert f0["line"] == line_of("update_sensor(&sensors[counter % 4], counter);")
        c.ok("stepIn", threadId=tid)
        c.event("stopped")
        assert c.ok("stackTrace", threadId=tid)["stackFrames"][0]["name"] == "update_sensor"
        c.ok("stepOut", threadId=tid)
        c.event("stopped")
        f0 = c.ok("stackTrace", threadId=tid)["stackFrames"][0]
        assert f0["name"] == "main"

        addr = _address(c.ok("evaluate", expression="&counter", frameId=f0["id"], context="watch")["result"])
        mem = c.ok("readMemory", memoryReference=f"{addr:#x}", count=4)
        assert int.from_bytes(base64.b64decode(mem["data"]), "little") == counter + 1
        assert mem["unreadableBytes"] == 0
        c.ok("writeMemory", memoryReference=f"{addr:#x}", data=base64.b64encode((1234).to_bytes(4, "little")).decode())
        assert c.ok("evaluate", expression="counter", frameId=f0["id"], context="watch")["result"] == "1234"

        pc = int(f0["instructionPointerReference"], 16)
        ins = c.ok("disassemble", memoryReference=f"{pc:#x}", instructionOffset=-2,
                   instructionCount=6)["instructions"]
        assert len(ins) == 6 and int(ins[2]["address"], 16) == pc and ins[2]["instruction"]
        assert ins[2].get("line") and ins[2]["location"]["path"].endswith("main.c")

        fib = _address(c.ok("evaluate", expression="&fib", context="watch")["result"])
        c.ok("setBreakpoints", source={"path": MAIN_C}, breakpoints=[])
        ib = c.ok("setInstructionBreakpoints", breakpoints=[{"instructionReference": f"{fib:#x}"}])
        assert ib["breakpoints"][0]["verified"]
        c.ok("continue", threadId=tid)
        st = c.event("stopped")
        assert st["reason"] == "instruction breakpoint"
        assert c.ok("stackTrace", threadId=tid)["stackFrames"][0]["name"] == "fib"

        c.ok("setInstructionBreakpoints", breakpoints=[])
        c.ok("continue", threadId=tid)
        # breakpoint posé cible en marche : arrêt et reprise silencieux, puis le vrai arrêt
        bps = c.ok("setBreakpoints", source={"path": MAIN_C},
                   breakpoints=[{"line": line, "logMessage": "counter={counter}"}])
        assert bps["breakpoints"][0]["verified"]
        deadline = time.monotonic() + 20                        # messages, sans arrêt visible
        while not any(o.startswith("counter=") for o in c.output) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert any(o.startswith("counter=") for o in c.output), c.output[-5:]
        c.ok("setBreakpoints", source={"path": MAIN_C}, breakpoints=[])
        c.ok("pause", threadId=tid)
        assert c.event("stopped")["reason"] == "pause"

        if caps.get("supportsStepBack"):
            c.ok("stepBack", threadId=tid)
            assert c.event("stopped")["reason"] == "step"
        else:
            assert not c.request("stepBack", threadId=tid)["success"]
        c.ok("disconnect", terminateDebuggee=True)
        assert c.p.wait(timeout=20) == 0
    finally:
        c.close()


@needs_qemu
def test_freertos_tasks_as_threads(rtos_elf, tmp_path):
    """Démo FreeRTOS : les tâches qui ne tournent pas sont des threads, leur pile se lit (par
    le contexte sauvé sur leur pile) ; avancer une tâche est refusé en le disant."""
    c = Client(tmp_path)
    try:
        c.ok("initialize", adapterID="gdbtrace")
        c.ok("launch", program=os.path.relpath(rtos_elf, ROOT))
        c.event("initialized")
        fb = c.ok("setFunctionBreakpoints", breakpoints=[{"name": "worker_step"}])
        assert fb["breakpoints"][0]["verified"]
        c.ok("configurationDone")
        st = c.event("stopped")
        assert st["reason"] == "function breakpoint"
        threads = {t["name"]: t["id"] for t in c.ok("threads")["threads"]}
        tasks = {n.split()[1]: i for n, i in threads.items() if n.startswith("tâche ")}
        assert {"producer", "consumer", "IDLE"} <= set(tasks), threads
        frames = c.ok("stackTrace", threadId=tasks["consumer"])["stackFrames"]
        assert frames and any(f["name"] != "??" for f in frames)
        r = c.request("next", threadId=tasks["consumer"])
        assert not r["success"] and "tâche FreeRTOS" in r["message"]
        top = c.ok("stackTrace", threadId=st["threadId"])["stackFrames"][0]
        assert top["name"] == "worker_step"
    finally:
        c.close()


@pytest.mark.skipif(not (shutil.which("cc") and shutil.which("gdb-multiarch")),
                    reason="cc ou gdb-multiarch absent")
def test_native_threads_output_and_exit(tmp_path):
    """Programme de l'hôte (pthreads) : threads nommés, sortie du programme en `output`, fin
    du programme en `exited` puis `terminated`."""
    native = os.path.join(ROOT, "examples", "native")
    subprocess.run(["make", "-s", "-C", native], check=True)
    c = Client(tmp_path)
    try:
        c.ok("initialize", adapterID="gdbtrace")
        c.ok("launch", program="examples/native/pthreads", args=["--args", "200"])
        c.event("initialized")
        c.ok("setFunctionBreakpoints", breakpoints=[{"name": "consume_step"}])
        c.ok("configurationDone")
        st = c.event("stopped")
        assert st["reason"] == "function breakpoint"
        names = {t["name"] for t in c.ok("threads")["threads"]}
        assert {"pthreads", "producer", "consumer"} <= names, names      # monitor : pas encore créé
        assert c.ok("stackTrace", threadId=st["threadId"])["stackFrames"][0]["name"] == "consume_step"
        c.ok("setFunctionBreakpoints", breakpoints=[])
        c.ok("continue", threadId=st["threadId"])
        assert isinstance(c.event("exited", timeout=60)["exitCode"], int)
        c.event("terminated")
        assert any("produced=" in o for o in c.output), c.output[-5:]
    finally:
        c.close()
