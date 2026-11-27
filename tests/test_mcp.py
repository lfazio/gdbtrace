"""Serveur MCP (gdbtrace/mcp.py) : protocole JSON-RPC sur stdio, outils sur QEMU et en natif."""
import csv
import inspect
import io
import json
import os
import re
import shutil
import subprocess
import sys

import pytest
from conftest import ROOT, SVD, needs_qemu

from gdbtrace import mcp
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
        fn = getattr(srv, f"tool_{t['name']}")
        assert set(inspect.signature(fn).parameters) == set(sch["properties"]), t["name"]
        ann = t["annotations"]                                 # titre et annotations MCP
        assert t["title"] and ann["title"] == t["title"] and ann["openWorldHint"] is False
        assert ann["readOnlyHint"] or "destructiveHint" in ann, t["name"]
    ann = {t["name"]: t["annotations"] for t in tools}
    assert all(ann[n]["readOnlyHint"] for n in ("status", "evaluate", "memory", "disassemble",
                                                  "trace_list", "memory_find", "check"))
    assert all(ann[n]["destructiveHint"] for n in ("memory_write", "memory_fill", "program",
                                                     "python", "gdb"))
    assert not ann["go"]["readOnlyHint"] and not ann["go"]["destructiveHint"]

    def call(name, **args):
        return _handle(srv, "tools/call", {"name": name, "arguments": args})["result"]

    check = call("check")                                     # bilan : sans session
    assert not check["isError"] and "répertoire du projet" in check["content"][0]["text"]
    assert "gdb-multiarch" in check["content"][0]["text"]
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


def _launch(cwd, env):
    """La commande du .mcp.json du dépôt, lancée telle que Claude Code la lance."""
    with open(os.path.join(ROOT, ".mcp.json"), encoding="utf-8") as fh:
        server = json.load(fh)["mcpServers"]["gdbtrace"]
    return subprocess.Popen([server["command"], *server["args"]], cwd=cwd, env=env, text=True,
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


@pytest.mark.parametrize("how", ["variable", "repertoire"])
def test_project_mcp_json(how, tmp_path):
    """Le .mcp.json du dépôt : sh trouve le projet (CLAUDE_PROJECT_DIR que Claude Code pose,
    sinon le répertoire de lancement) et l'interpréteur (GDBTRACE_PYTHON, le venv du dépôt,
    python3), sans que Claude Code ait à développer une variable."""
    with open(os.path.join(ROOT, ".mcp.json"), encoding="utf-8") as fh:
        text = fh.read()
    assert "${" not in text                       # rien à développer par Claude Code
    env = {k: v for k, v in os.environ.items() if k != "CLAUDE_PROJECT_DIR"}
    env["GDBTRACE_PYTHON"] = sys.executable
    if how == "variable":
        env["CLAUDE_PROJECT_DIR"] = ROOT
        cwd = str(tmp_path)
    else:
        cwd = ROOT
    p = _launch(cwd, env)
    try:
        p.stdin.write(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                  "params": {"protocolVersion": "2025-06-18"}}) + "\n")
        p.stdin.flush()
        reply = json.loads(p.stdout.readline() or "{}")
        assert reply.get("result", {}).get("serverInfo", {}).get("name") == "gdbtrace", \
            p.stderr.read()
        assert ROOT in reply["result"]["instructions"]   # --root : le projet
    finally:
        p.stdin.close()
        p.wait(20)
        p.stdout.close()
        p.stderr.close()
    assert mcp.build_parser().parse_args(["--root", "/proj"]).root == "/proj"


def test_main_check_and_root(monkeypatch, tmp_path, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    rc = mcp.main(["--root", ROOT, "--check"])                 # sans lire stdin
    out = capsys.readouterr().out
    assert os.getcwd() == ROOT and f"répertoire du projet : {ROOT}" in out
    assert rc == (0 if shutil.which("gdb-multiarch") or shutil.which("gdb") else 1)
    assert "qemu-system-arm" in out and "ST-LINK_gdbserver" in out and "module capstone" in out
    assert "examples/firmware/demo.elf" in out
    monkeypatch.chdir(tmp_path)                                # CLAUDE_PROJECT_DIR sans --root
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", ROOT)
    mcp.main(["--check"])
    assert os.getcwd() == ROOT
    assert mcp.main(["--root", str(tmp_path / "absent"), "--check"]) == 2
    assert "introuvable" in capsys.readouterr().err


def test_pattern_and_address_parsing():
    assert mcp._pattern("0x55") == 0x55 and mcp._pattern("1234") == 1234
    assert mcp._pattern("de ad be ef") == b"\xde\xad\xbe\xef"
    assert mcp._pattern('"1234"') == "1234" and mcp._pattern("gdbtrace") == "gdbtrace"
    assert mcp._is_address("0x100") and mcp._is_address("*&buf[2]") and not mcp._is_address("main")
    assert mcp._strings("fib, main") == ["fib", "main"] and mcp._strings(None) == []


class Client:
    """Serveur lancé comme Claude Code le lance : `python -m gdbtrace.mcp`, stdio."""

    def __init__(self, cwd=ROOT, args=()):
        env = {k: v for k, v in os.environ.items() if k != "CLAUDE_PROJECT_DIR"}
        self.p = subprocess.Popen([sys.executable, "-m", "gdbtrace.mcp", *args], cwd=cwd,
                                  text=True, env=env, stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE)
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

    def __call__(self, tool, /, ok=True, **args):         # `name` : argument d'outil possible
        res = self.rpc("tools/call", {"name": tool, "arguments": args})["result"]
        text = res["content"][0]["text"]
        assert res["isError"] is not ok, f"{tool} {args} : {text}"
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
    assert "occurrence(s)" in c("trace_find", query="fib")
    trace = c("trace_list", n=8)
    assert trace.count("\n") == 7 and "main" in trace
    assert "fonction" in c("coverage", top=5)
    assert "delay" in c("timing", top=10)
    chrono = c("chrono")                                       # watchpoint : SysTick lancé
    assert "SysTick (QEMU : temps simulé)" in chrono and "cumul" in chrono
    assert "remis à zéro" in c("chrono", reset=True)
    assert "pas de faute en cours" in c("fault")                # démo : aucune faute
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


@needs_qemu
def test_data_tools_from_project_root(elf, faults_elfs, tmp_path):
    """Serveur lancé ailleurs que dans le projet (`--root`) : chemins relatifs au projet ;
    outils run-control, données, mémoire, SVD, symboles ajoutés, échantillonnage."""
    c = Client(cwd="/", args=["--root", ROOT])
    try:
        out = c("connect", args="examples/firmware/demo.elf")
        assert "arrêtée" in out
        out = c("go_till", condition="counter > 3")
        assert "watchpoint" in out
        assert int(c("evaluate", expression="counter").split("=")[1]) == 4
        out = c("step_until", change="counter")
        assert "Step.Change counter" in out and "4 → 5" in out
        assert "Step.Till" in c("step_until", condition="$pc == $pc", asm=True)
        assert "l'un des deux" in c("step_until", ok=False)

        # breakpoint désactivé : non atteint ; réactivé : atteint
        num = c("break", location="update_sensor").split()[1]
        assert "désactivé" in c("enable", number=num, on=False)
        assert "toujours en marche" in c("go", wait=0.5)
        c("halt")
        assert "désactivé" not in c("enable", number=num)
        assert "update_sensor" in c("go")
        c("delete", number=num)

        dis = c("disassemble", location="fib")
        assert "<fib+0>" in dis and dis.count("\n") > 5
        here = c("disassemble")                                # fonction courante, PC marqué
        assert "=>" in here and "<update_sensor+" in here
        start = int(dis.split()[0], 16)
        assert len(c("disassemble", location=f"{start:#x}", count=3).splitlines()) == 3
        assert "<fib+" in c("disassemble", location=f"{start:#x}..{start + 8:#x}")
        t = c("type", expression="sensors[0]")
        assert "struct sensor" in t and "uint8_t status" in t and "sizeof = 12" in t

        # mémoire : écrire, relire, remplir, chercher, fichiers
        assert "01 00 00 00" not in c("memory_write", address="&sensors[3].value",
                                      value="0x12345678")
        assert "78 56 34 12" in c("memory", address="&sensors[3].value", length=4)
        out = c("memory_write", address="&sensors[3].value", data="de ad be ef")
        assert "de ad be ef" in out
        assert "l'un des deux" in c("memory_write", ok=False, address="0x20000000")
        c("memory_fill", address="sensors[2]", pattern="0xa5")
        assert "a5 a5 a5 a5" in c("memory", address="&sensors[2]", length=12)
        found = c("memory_find", address="sensors", pattern="de ad be ef")
        assert "1 occurrence(s)" in found
        c("memory_write", address="&sensors[1]", data="4f 4b 21 21")      # « OK!! »
        assert "1 occurrence(s)" in c("memory_find", address="sensors", pattern='"OK!!"')
        dump = tmp_path / "sensors.bin"
        c("memory_file", action="save", path=str(dump), address="sensors")
        assert dump.stat().st_size == 48
        c("memory_fill", address="sensors", pattern="0")
        assert "différent(s) sur 48" in c("memory_file", action="compare", path=str(dump),
                                          address="sensors")
        c("memory_file", action="load", path=str(dump), address="sensors")
        assert "identique" in c("memory_file", action="compare", path=str(dump),
                                address="sensors").lower()

        # périphériques d'après le SVD, chemin relatif au projet
        svd = os.path.relpath(SVD, ROOT)
        listing = c("peripheral", svd=svd)
        assert "TIMER0" in listing and "SysTick" in listing
        assert "RELOAD" in c("peripheral", name="TIMER0")
        assert "proches : TIMER0" in c("peripheral", ok=False, name="TIMER")

        # symboles d'un autre ELF
        extra = os.path.relpath(faults_elfs["m3"], ROOT)
        assert "ajoutés" in c("add_symbols", path=extra, offset="0x100000")
        assert "read_at" in c("symbols", pattern="read_at")

        # échantillonnage sans trace, valeurs par var_samples
        c("snoop", expr="ticks", period_ms=5)
        c("go", wait=0)
        import time
        time.sleep(0.5)
        c("halt")
        samples = c("var_samples", expression="ticks", n=5)
        assert "5 valeur(s)" in samples and "sample" in samples
        assert "pas tracée" in c("var_samples", ok=False, expression="counter")
    finally:
        c.close()


@needs_qemu
def test_trace_tools(client, elf, tmp_path):
    """Trace ETM simulée : filtre, export CSV, rapport et remise à zéro de la couverture."""
    c = client
    c("connect", args=f"{os.path.relpath(elf, ROOT)} --etm-sim")
    c("break", location="update_sensor")
    c("go")
    flt = c("etm_filter", ranges=["fib"])
    lo, hi = (int(x, 16) for x in re.search(r"\((0x[0-9a-f]+)–(0x[0-9a-f]+)\)", flt).groups())
    c("go")
    path = tmp_path / "trace.csv"
    assert "lignes de trace écrites" in c("trace_export", path=str(path))
    with open(path, encoding="utf-8") as fh:
        rows = list(csv.reader(fh, delimiter=";"))
    assert rows[0][:3] == ["index", "type", "adresse"] and len(rows) > 100
    # l'historique garde la première exécution ; la dernière n'a que la plage effective
    # (blocs alignés du DWT : fib et le début d'update_sensor)
    tail = [r for r in rows[1:] if r[1] == "instruction"][-30:]
    assert all(lo <= int(r[2], 16) < hi for r in tail) and "fib" in {r[3] for r in tail}
    c("etm_filter")                                           # tout le code de nouveau
    report = tmp_path / "cov.html"
    c("coverage", export=str(report))
    assert report.stat().st_size > 0
    assert "remise à zéro" in c("coverage", reset=True)


@needs_qemu
def test_rtos_tools(client, rtos_elf):
    """FreeRTOS : temps par tâche (journal en RAM), contexte d'une tâche, objets du noyau."""
    import time
    c = client
    c("connect", args=os.path.relpath(rtos_elf, ROOT))
    c("go", wait=0)
    time.sleep(0.8)
    c("halt")
    times = c("task_times")
    assert "journal des tâches en RAM" in times and "worker" in times
    assert "#0" in c("task", name="sleeper")
    q = c("queues")
    assert "cmd_queue" in q and "data_mutex" in q
    assert "inconnue" in c("task", ok=False, name="personne")


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
