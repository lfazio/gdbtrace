Tout ce que fait l'interface se pilote aussi en Python, par l'objet `gdbtrace`. La notice complète est à part : [Scripting Python](Scripting). Un agent (Claude Code) pilote GdbTrace par le serveur MCP.

## Console

La vue **Console** exécute du Python, avec l'historique et les blocs sur plusieurs lignes. Deux préfixes envoient une commande brute :

| Saisie | Effet |
| --- | --- |
| `gdbtrace.break_("fib"); gdbtrace.go(); gdbtrace.wait()` | Python, avec l'API `gdbtrace` |
| `!info breakpoints` | commande CLI de gdb |
| `mon reset halt` | commande `monitor` vers le serveur (OpenOCD) |

La console affiche aussi la sortie de gdb, les messages des breakpoints à action et la sortie d'un programme de l'hôte.

## Scripts

```bash
.venv/bin/python -m gdbtrace examples/firmware/demo.elf --script examples/scripts/session.py           # interface + script
.venv/bin/python -m gdbtrace examples/firmware/demo.elf --batch --script examples/scripts/session.py   # sans interface
```

Dans l'interface, **Fichier › Exécuter un script…** fait la même chose. Exemples fournis dans `examples/scripts/` : `session.py` (session complète), `demo.py`, `rtos.py` (FreeRTOS), `coverage.py` (campagne de couverture).

Un script peut ce que l'interface ne propose pas : une **action Python** sur un breakpoint, qui décide de l'arrêt (`gdbtrace.break_("animate", action=fonction)`), une campagne de tests en intégration continue, une injection de fautes, un journal CSV.

## Pour aller plus loin

### Console

| Saisie | Effet |
|---|---|
| `gdbtrace.break_("fib"); gdbtrace.go(); gdbtrace.wait()` | Python, avec l'API `gdbtrace` |
| `gdbtrace.break_("animate", condition="tick == 6")`, `break_("fib", hw=True, temporary=True)` | breakpoint conditionnel, matériel, temporaire |
| `gdbtrace.enable(n, False)`, `gdbtrace.delete(n)`, `gdbtrace.breaks()`, `gdbtrace.watch("mode")` | Break.DISable / ENable, Break.Delete, Break.List, watchpoint (`access="read"` / `"rw"`) |
| `gdbtrace.step()`, `over()`, `out()`, `stepi()`, `go(until="fib")` | Step, Step.Over, Go.Return, pas assembleur, Go jusqu'à |
| `gdbtrace.break_("animate", count=5)`, `break_("update_sensor", log="n={counter} t={tick:hex}")`, `break_("fib", action=f)` | Break.Set /COUNT (arrêt au 5ᵉ passage), message à chaque passage, action Python (vrai : arrêt) |
| `gdbtrace.break_options(n, condition=…, count=…, log=…)`, `gdbtrace.break_log()` | options d'un breakpoint ou d'un watchpoint existant, derniers messages |
| `gdbtrace.go_till("counter >= 20")` | Go.Till : Go jusqu'à ce que la condition devienne vraie (watchpoint matériel) |
| `gdbtrace.step_till("i == 3")`, `gdbtrace.step_change("a", asm=True)` | Step.Till, Step.Change : pas à pas jusqu'à la condition, jusqu'au changement |
| `gdbtrace.var_watch("sensors", "hex")` | Var.Watch : ajoute l'expression à la vue Watch avec son format ; sans GUI, affiche sa valeur |
| `gdbtrace.d("sensors", 32)`, `gdbtrace.r()`, `gdbtrace.bt()` | Data.dump, Register.view, pile |
| `gdbtrace.eval("offset", "hex")` | valeur d'une expression ; format : naturel, `dec`, `hex`, `oct`, `bin`, `ascii` |
| `gdbtrace.mem(addr, n)`, `gdbtrace.live_read(addr, n)` | lecture mémoire, y compris cible en marche |
| `gdbtrace.periph("TIMER0")` | PER.view : registres et champs décodés, retourne `{registre: valeur}` |
| `gdbtrace.tasks()`, `gdbtrace.queues()` | TASK.List / objets du noyau FreeRTOS (aussi cible en marche) |
| `gdbtrace.task_times()`, `gdbtrace.task_timeline(30)` | temps par tâche mesuré par l'ITM (changements de tâche tracés par le firmware) |
| `gdbtrace.task("consumer")` | contexte et pile d'appels d'une tâche |
| `gdbtrace.profile(2.0, per_task=True)` | PERF : profil statistique pendant 2 s (relance puis arrête la cible si besoin) |
| `gdbtrace.itm(0)` | texte reçu sur le port ITM 0 |
| `gdbtrace.var_trace("ticks", "rw", pc=True)`, `gdbtrace.var_samples("ticks")` | trace de données du DWT : valeur à chaque accès, cible en marche |
| `gdbtrace.swo_config(cpu_hz=180_000_000, swo_hz=2_000_000)` | programme ITM/DWT/TPIU (+ OpenOCD) |
| `gdbtrace.trace_list(30)`, `gdbtrace.calls(40)` | Trace.List, flot d'appels (trace ETM) |
| `gdbtrace.cov.summary()`, `gdbtrace.cov.list(20)`, `gdbtrace.cov.files()` | couverture : résumé, COVerage.ListFunc, ListModule |
| `gdbtrace.cov.export("cov.html")`, `gdbtrace.cov.save(f)`, `gdbtrace.cov.load(f)`, `gdbtrace.cov.reset()` | rapports (.html, .info LCOV, .json), cumul de campagnes, RàZ |
| `gdbtrace.etm_config(port_width=4, stm32=True)` | programme l'ETM et le TPIU (parallèle, ou `port="swo", cpu_hz=…`) |
| `gdbtrace.etm_swo(cpu_hz=216_000_000, stall=True)` | ETM par SWO : TPIU formaté, capture servie par OpenOCD, ETM (comme `--etm-config --etm-port swo`) |
| `gdbtrace.mtb(20)`, `gdbtrace.mtb_probe()` | paquets MTB bruts du dernier arrêt, symbolisés ; composants CoreSight de la cible |
| `gdbtrace.mtb_stats()` | compteurs MTB : paquets, instructions, arrêts AUTOHALT, débordements, erreurs |
| `gdbtrace.program("fw.elf")`, `gdbtrace.program()`, `program("fw.hex")` | FLASH.ReProgram : écrit, vérifie et reset (ELF courant par défaut ; une image HEX, S-record, binaire garde les symboles) |
| `gdbtrace.fault()`, `gdbtrace.break_on_fault()` | analyse de la faute en cours ; arrêt à l'entrée de toute faute |
| `gdbtrace.mem_save("sensors", None, "s.hex")`, `mem_find(0x20000000, 0x1000, "abc")` | Data.SAVE, Data.Find (aussi `mem_load`, `mem_fill`, `mem_compare`) |
| `gdbtrace.sym_list("sensor")`, `gdbtrace.add_symbols("boot.elf")` | sYmbol.List ; symboles d'un autre ELF |
| `gdbtrace.terminal()`, `gdbtrace.terminal_send("go\n")` | texte du semihosting et du RTT ; envoi au programme |
| `gdbtrace.trace_find("exception SysTick")`, `gdbtrace.trace_export("t.csv")` | Trace.FindAll ; listing en CSV |
| `gdbtrace.call_tree(3)`, `gdbtrace.irq_stats()` | arbre d'appels, statistiques d'exceptions |
| `gdbtrace.snoop("ticks", 10)`, `gdbtrace.counters()` | variable échantillonnée sans trace ; compteurs de performance |
| `gdbtrace.timing(20, unit="us")`, `gdbtrace.timeline(30)` | durées par fonction / derniers appels (trace ETM) |
| `!info breakpoints` | Commande CLI gdb brute |
| `mon reset halt` | Commande `monitor` vers OpenOCD |

**Notice complète du scripting Python (débogage programmatique) : [Scripting Python](Scripting)** — lancement, run-control, breakpoints, données, cible en marche, trace, recettes (CI, journal CSV, injection de fautes), référence de l'API. Exemples dans `examples/scripts/` :
`session.py` (session de débogage complète : programmation, breakpoints simple, temporaire,
conditionnel et matériel, watchpoint, Watch avec formats, pas à pas, lecture et modification
de variables, lectures cible en marche), `demo.py`, `rtos.py` (FreeRTOS), `coverage.py`.

```bash
.venv/bin/python -m gdbtrace examples/firmware/demo.elf --script examples/scripts/session.py
.venv/bin/python -m gdbtrace examples/firmware/demo.elf --batch --script examples/scripts/session.py
```
