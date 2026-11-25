"""Serveur MCP (gdbtrace/mcp.py) : protocole JSON-RPC sur stdio, outils sur QEMU et en natif."""
import io
import json
import os
import shutil
import subprocess
import sys

import pytest
from conftest import ROOT, needs_qemu

from gdbtrace.mcp import TOOLS, GdbTraceMcp


def _handle(srv, method, params=None, mid=1):
    msg = {"jsonrpc": "2.0", "method": method, "id": mid}
    if params is not None:
        msg["params"] = params
    return srv.handle(msg)


def test_protocol_without_session():
    srv = GdbTraceMcp(io.StringIO())
    init = _handle(srv, "initialize", {"protocolVersion": "2025-03-26", "capabilities": {},
                                       "clientInfo": {"name": "t", "version": "0"}})["result"]
    assert init["protocolVersion"] == "2025-03-26"            # version du client acceptée
    assert init["serverInfo"]["name"] == "gdbtrace" and "tools" in init["capabilities"]
    other = _handle(srv, "initialize", {"protocolVersion": "1999-01-01"})["result"]
    assert other["protocolVersion"] == "2025-06-18"           # inconnue : la nôtre
    assert srv.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    assert _handle(srv, "ping")["result"] == {}
    assert _handle(srv, "resources/list")["error"]["code"] == -32601

    tools = _handle(srv, "tools/list")["result"]["tools"]
    names = [t["name"] for t in tools]
    assert len(names) == len(set(names)) == len(TOOLS)
    for t in tools:                                            # schéma JSON valide pour le client
        sch = t["inputSchema"]
        assert sch["type"] == "object" and t["description"]
        assert set(sch.get("required", [])) <= set(sch["properties"])
        assert hasattr(srv, f"tool_{t['name']}")

    def call(name, **args):
        return _handle(srv, "tools/call", {"name": name, "arguments": args})["result"]

    assert "pas de session" in call("status")["content"][0]["text"]
    res = call("go")
    assert res["isError"] and "connect" in res["content"][0]["text"]
    res = call("nexiste_pas")
    assert res["isError"] and "outil inconnu" in res["content"][0]["text"]
    res = call("status", inconnu=1)
    assert res["isError"] and "arguments invalides" in res["content"][0]["text"]
    # options rejetées par la ligne de commande : une erreur d'outil, pas un sys.exit
    for args, why in (("--etm-filter main x.elf", "--etm-filter"),
                      ("/nexiste/pas.elf", "ELF introuvable"),
                      ("x.elf --script s.py", "--script"),
                      ("x.elf --option-inconnue", "options")):
        res = call("connect", args=args)
        assert res["isError"] and why in res["content"][0]["text"], (args, res)
    srv.close()


def test_serve_reads_lines_and_survives_bad_json():
    out = io.StringIO()
    srv = GdbTraceMcp(out)
    srv.serve(io.StringIO('{"jsonrpc":"2.0","id":1,"method":"ping"}\n\npas du json\n'
                          '{"jsonrpc":"2.0","method":"notifications/initialized"}\n'
                          '{"jsonrpc":"2.0","id":2,"method":"tools/list"}\n'))
    replies = [json.loads(line) for line in out.getvalue().splitlines()]
    assert [r.get("id") for r in replies] == [1, None, 2]
    assert replies[1]["error"]["code"] == -32700 and "tools" in replies[2]["result"]


class Client:
    """Serveur lancé comme Claude Code le lance : `python -m gdbtrace.mcp`, stdio."""

    def __init__(self):
        self.p = subprocess.Popen([sys.executable, "-m", "gdbtrace.mcp"], cwd=ROOT, text=True,
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE)
        self.n = 0
        init = self.rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                       "clientInfo": {"name": "pytest", "version": "0"}})
        assert init["result"]["serverInfo"]["name"] == "gdbtrace"
        self.p.stdin.write('{"jsonrpc":"2.0","method":"notifications/initialized"}\n')

    def rpc(self, method, params):
        self.n += 1
        self.p.stdin.write(json.dumps({"jsonrpc": "2.0", "id": self.n, "method": method,
                                       "params": params}) + "\n")
        self.p.stdin.flush()
        line = self.p.stdout.readline()
        assert line, self.p.stderr.read()
        reply = json.loads(line)                # stdout : rien d'autre que le protocole
        assert reply["id"] == self.n
        return reply

    def __call__(self, name, ok=True, **args):
        res = self.rpc("tools/call", {"name": name, "arguments": args})["result"]
        text = res["content"][0]["text"]
        assert res["isError"] is not ok, f"{name} {args} : {text}"
        return text

    def close(self):
        try:
            self.p.stdin.close()
            self.p.wait(30)
        finally:
            if self.p.poll() is None:
                self.p.kill()
            self.p.stdout.close()
            self.p.stderr.close()


@pytest.fixture
def client():
    c = Client()
    yield c
    c.close()


@needs_qemu
def test_debug_session_on_qemu(client, elf):
    c = client
    out = c("connect", args=f"{os.path.relpath(elf, ROOT)} --etm-sim")
    assert "Reset_Handler" in out and "arrêtée" in out
    assert "update_sensor main.c:" in c("break", location="update_sensor")
    assert "breakpoint-hit — update_sensor" in c("go")
    bt = c("backtrace")
    assert "update_sensor" in bt and "main" in bt and "tick = 1" in bt
    assert c("evaluate", expression="counter") == "counter = 1"
    assert "0x" in c("evaluate", expression="counter", format="hex")
    dump = c("memory", address="&counter", length=16)
    addr = int(c("python", code="gdbtrace.addr('counter')"))
    assert dump.startswith(f"{addr:08x}")
    assert "01 00 00 00" in dump
    assert "main.c:" in c("step", kind="over", count=2)
    assert "1 passage(s)" in c("breakpoints")
    c("delete", number="1")
    assert "aucun breakpoint" in c("breakpoints")

    # watchpoint : l'arrêt dit l'ancienne et la nouvelle valeur
    assert "watchpoint" in c("watch", expression="counter")
    out = c("go")
    assert "watchpoint-trigger" in out and "1 → 2" in out
    trace = c("trace_list", n=8)
    assert trace.count("\n") == 7 and "main" in trace
    assert "fonction" in c("coverage", top=5)
    assert "delay" in c("timing", top=10)
    chrono = c("chrono")                                       # watchpoint : SysTick lancé
    assert "SysTick (QEMU : temps simulé)" in chrono and "cumul" in chrono
    assert "remis à zéro" in c("chrono", reset=True)
    c("python", code="[gdbtrace.delete(b.number) for b in gdbtrace.breaks()]")

    # cible en marche : une globale déjà évaluée se relit par le canal mémoire (QMP)
    c("evaluate", expression="ticks")
    assert c("go", wait=0) == "en marche"
    live = c("evaluate", expression="ticks")
    assert live.endswith("(lu en direct)")
    err = c("evaluate", ok=False, expression="sensors[2].status + 1")
    assert "cible en marche" in err
    assert "running" in c("status")
    assert "arrêtée" in c("halt")
    assert "Line" in c("gdb", command="info line fib")
    assert c("python", code="for i in range(3): print(i)") == "0\n1\n2"
    assert "ZeroDivisionError" in c("python", ok=False, code="1/0")
    # une erreur d'outil ne ferme pas la session
    assert "No symbol" in c("evaluate", ok=False, expression="pas_de_symbole")
    assert "arrêtée" in c("status")
    assert "fermée" in c("disconnect")
    assert "pas de session" in c("status")


@pytest.mark.skipif(not (shutil.which("cc") and shutil.which("gdb-multiarch")),
                    reason="cc / gdb-multiarch absents")
def test_native_program_threads_and_output(client):
    native = os.path.join(ROOT, "examples", "native")
    subprocess.run(["make", "-s", "-C", native], check=True)
    c = client
    assert "main" in c("connect", args="examples/native/pthreads --args 2000")
    c("break", location="consume_step")
    assert "consume_step" in c("go")
    ths = c("threads")
    assert "producer" in ths and "consumer" in ths
    assert "consume_step" in c("threads", select="consumer")
    c("python", code="[gdbtrace.delete(b.number) for b in gdbtrace.breaks()]")
    out = c("go", wait=20)                    # jusqu'à la fin : sa sortie revient avec l'outil
    assert "produced=2000 consumed=2000" in out and "programme terminé" in out
    assert "terminé" in c("status")
