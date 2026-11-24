# Scripting Python : notice d'utilisation

GdbTrace se pilote entièrement en Python. Un script peut programmer la cible, poser des
breakpoints, faire avancer l'exécution, lire et modifier des variables, la mémoire et les
registres, puis décider de la suite selon ce qu'il trouve. Il peut aussi lancer des mesures :
profil, trace, couverture. C'est le **débogage programmatique** : reproduire un scénario de
débogage à l'identique, tester un firmware sur sa cible en intégration continue, injecter des
fautes, journaliser l'évolution d'une variable.

Les exemples de cette notice tournent sur la démo fournie (`examples/firmware/demo.elf`,
Cortex-M3 sous QEMU). Chaque bloc de code est exécuté par la suite de tests
(`tests/test_docs.py`) : ils fonctionnent tels quels.

**Sommaire**

1. [Principe](#1-principe)
2. [Démarrage rapide](#2-démarrage-rapide)
3. [Lancer un script](#3-lancer-un-script)
4. [Contrôler l'exécution](#4-contrôler-lexécution)
5. [Breakpoints et watchpoints](#5-breakpoints-et-watchpoints)
6. [Lire et modifier les données](#6-lire-et-modifier-les-données)
7. [Cible en marche](#7-cible-en-marche)
8. [Programmer la cible](#8-programmer-la-cible)
9. [Périphériques et RTOS](#9-périphériques-et-rtos)
10. [Trace, profil et couverture](#10-trace-profil-et-couverture)
11. [Accès bas niveau](#11-accès-bas-niveau)
12. [Recettes](#12-recettes)
13. [Pièges et bonnes pratiques](#13-pièges-et-bonnes-pratiques)
14. [Référence](#14-référence)

---

## 1. Principe

Un script GdbTrace est un fichier Python ordinaire. GdbTrace l'exécute après la connexion à
la cible, avec trois objets déjà définis :

| Objet | Rôle |
|---|---|
| `gdbtrace` | l'API de GdbTrace : run-control, breakpoints, données, trace… (section 14) |
| `session` | la session gdb sous-jacente, plus bas niveau : pile, locales, varobjs… (section 11) |
| `hexdump(octets, adresse)` | met en forme un dump hexadécimal |

Tout le reste est du Python : boucles, conditions, fichiers, `csv`, `json`, `time`,
`assert`. Le même script tourne de quatre façons :

| Où | Comment | Remarque |
|---|---|---|
| Sans interface | `python -m gdbtrace fw.elf --batch --script s.py` | intégration continue ; code de retour exploitable |
| Au démarrage du GUI | `python -m gdbtrace fw.elf --script s.py` | les vues (Source, Watch, Pile, Breakpoints…) suivent chaque arrêt |
| Depuis le GUI | Fichier › Exécuter un script… | même chose, sur la cible déjà connectée |
| Ligne par ligne | console du GUI (onglet Console) | `gdbtrace.…` ; `!cmd` pour une commande gdb, `mon cmd` pour une commande `monitor` |

Dans le GUI, le script tourne dans son propre fil d'exécution : l'interface reste réactive,
et `print` écrit dans la console. Un script peut en lancer un autre avec
`gdbtrace.run_script("autre.py")`.

## 2. Démarrage rapide

Dans l'environnement du projet (README : `pip install -e '.[dev]'`) :

```bash
make -C examples/firmware
python -m gdbtrace examples/firmware/demo.elf --batch --script mon_script.py
```

`mon_script.py` :

<!-- test -->
```python
gdbtrace.break_("update_sensor")          # Break.Set
gdbtrace.go()                             # Go : rend la main tout de suite
arret = gdbtrace.wait(5)                  # attend l'arrêt, 5 s au plus
print(arret)                              # <arrêt : breakpoint-hit — update_sensor (main.c:45)>
print("tick =", gdbtrace.eval("tick"))    # argument de la fonction
print("s =", gdbtrace.eval("*s"))         # structure pointée
gdbtrace.over()                           # Step.Over (attend l'arrêt)
print("s->value =", gdbtrace.eval("s->value", "hex"))
gdbtrace.bt()                             # pile d'appels
gdbtrace.d("sensors", 32)                 # Data.dump
```

Un exemple complet, commenté, se trouve dans `examples/scripts/session.py` :
programmation, breakpoints de toutes sortes, watchpoint, Watch, pas à pas, modification de
variables et lectures cible en marche.

## 3. Lancer un script

### Choisir la cible

| Option | Cible |
|---|---|
| *(aucune)* ou `--qemu` | QEMU, sans matériel ; `--machine` pour une autre carte (`mps2-an500` : Cortex-M7, `microbit` : Cortex-M0) |
| `--openocd CFG…` | sonde réelle via OpenOCD, par ex. `--openocd configs/nucleo-f746zg.cfg` |
| `--remote HOST:PORT [--tcl HOST:PORT]` | serveur gdb déjà lancé (J-Link, pyOCD, OpenOCD) |

### Options utiles

| Option | Effet |
|---|---|
| `--program [ELF]` | programme l'ELF à la connexion (reset, écriture, vérification, reset) |
| `--no-load` | se connecter sans charger l'ELF (cible déjà programmée) |
| `--go` | laisse la cible tourner à la fin du script |
| `--svd F.svd` | description des périphériques (`gdbtrace.periph`) |
| `--cpu-hz N` | fréquence du cœur (durées en µs, configuration du SWO) |
| `--etm-sim`, `--mtb-sim N` | trace d'instructions simulée sous QEMU (section 10) |
| `--coverage F` | écrit la couverture à la fin (`.html`, `.info`, `.json`) |

### Paramètres d'un script

`sys.argv` contient la ligne de commande de GdbTrace, pas des arguments du script. Pour
paramétrer un script, passer par des variables d'environnement :

```bash
SEUIL=500 python -m gdbtrace fw.elf --batch --script attente.py
```

```python
import os
seuil = int(os.environ.get("SEUIL", "100"))
```

### Codes de retour (`--batch`)

| Fin du script | Code | Sortie |
|---|---|---|
| normale | 0 | |
| `sys.exit(n)` / `raise SystemExit(n)` | n | |
| erreur de GdbTrace ou de gdb (`GdbError`, `RuntimeError`, `ValueError`, `OSError`, `TimeoutError`) | 1 | `erreur : …` sur stderr |
| autre exception, `assert` compris | 1 | trace Python |

Dans tous les cas, la session est fermée proprement : gdb et le serveur sont arrêtés, et la
cible est arrêtée sauf avec `--go`. Dans le GUI, `sys.exit` termine seulement le script.

## 4. Contrôler l'exécution

| Appel | Effet | Attend l'arrêt ? |
|---|---|---|
| `go()` | Go | **non** : enchaîner avec `wait()` |
| `go(until="animate")` | Go jusqu'à un emplacement, où qu'il soit (breakpoint temporaire) | **non** : `wait()` |
| `wait(timeout=10)` | attend l'arrêt ; `TimeoutError` au-delà | — |
| `halt()` | Break | oui (5 s) |
| `step(n=1)` | Step : ligne source, entre dans les appels | oui |
| `over(n=1)` | Step.Over : ligne source, par-dessus les appels | oui |
| `out()` | Go.Return : jusqu'au retour de la fonction | oui |
| `stepi(n=1)` | pas d'une instruction | oui |
| `reset()` | reset de la cible, arrêtée au vecteur de reset | oui |
| `running` | `True` si la cible tourne | — |

`wait()` et les pas renvoient l'**enregistrement d'arrêt** de gdb : un `dict` qui s'affiche
en une ligne. `wait()` revient tout de suite si la cible est déjà arrêtée, et renvoie alors
le dernier arrêt. Si un autre breakpoint arrête la cible avant l'emplacement d'un
`go(until=…)`, `wait()` retire le breakpoint temporaire.

| `reason` | Arrêt | Champs utiles |
|---|---|---|
| `breakpoint-hit` | breakpoint, ou fin d'un `go(until=…)` | `bkptno`, `frame` |
| `watchpoint-trigger` | watchpoint en écriture | `wpt["exp"]`, `value["old"]`, `value["new"]` |
| `read-watchpoint-trigger`, `access-watchpoint-trigger` | watchpoint en lecture, en accès | `value` |
| `end-stepping-range` | fin d'un `step`, `over`, `stepi` | `frame` |
| `function-finished` | fin d'un `out()` | `return-value` |
| `signal-received` | `halt()` (Break) | `signal-name` (`SIGINT`) |

`frame` contient `func`, `file`, `line`, `addr` et `args`. Les valeurs viennent de gdb, sous
forme de texte.

<!-- test -->
```python
gdbtrace.go(until="main")
print(gdbtrace.wait(5)["reason"])               # breakpoint-hit (temporaire)
gdbtrace.break_("fib", temporary=True)          # supprimé au premier arrêt
gdbtrace.go()
arret = gdbtrace.wait(5)
print(arret["reason"], arret["frame"]["args"])  # breakpoint-hit [{'name': 'n', 'value': '1'}]
gdbtrace.step(2)                                # deux lignes dans fib()
retour = gdbtrace.out()
print("fib a rendu", retour["return-value"])
gdbtrace.go()                                   # plus aucun breakpoint : la cible tourne
assert gdbtrace.running
gdbtrace.halt()                                 # Break
print(gdbtrace.wait()["signal-name"])           # SIGINT
```

### Go.Till, Step.Till, Step.Change

| Appel | Effet | Attend l'arrêt ? |
|---|---|---|
| `go_till(cond)` | Go.Till : Go jusqu'à ce que l'expression C `cond` devienne vraie, en temps réel | **non** : `wait()` |
| `step_till(cond, asm=False, max_steps=1000)` | Step.Till : pas à pas jusqu'à ce que `cond` soit vraie | oui |
| `step_change(expr, asm=False, max_steps=1000)` | Step.Change : pas à pas jusqu'à ce que la valeur de `expr` change | oui |

`go_till` pose un watchpoint matériel sur les variables de l'expression, avec l'expression
pour condition. gdb trouve lui-même ces variables : `counter >= 20` surveille `counter`. La
cible tourne à pleine vitesse et s'arrête quand l'expression **devient** vraie.
- Le watchpoint est retiré à la reprise suivante et n'apparaît pas dans `breaks()`.
- Il occupe un ou plusieurs comparateurs du DWT.
- Une expression sur une locale arrête aussi la cible à la sortie de sa fonction (`reason`
  vaut `watchpoint-scope`).
- Une expression qu'aucun watchpoint matériel ne peut suivre (registre `$r0`, valeur sans
  adresse) lève `ValueError` : utiliser `step_till`.

`step_till` et `step_change` avancent d'une ligne source à chaque pas (comme `step()`), ou
d'une instruction avec `asm=True`.
- Toute expression convient, registres compris.
- C'est lent : un aller-retour avec la cible à chaque pas. `max_steps` borne la boucle.
- Vue de l'extérieur, la boucle est **une seule** reprise : le GUI ne se rafraîchit qu'à la
  fin, et Break (`halt()` depuis un autre fil) l'interrompt.
- Un breakpoint ou un signal l'arrête aussi.

L'enregistrement d'arrêt final porte en plus :

| Champ | Contenu |
|---|---|
| `what`, `expr` | `Step.Till` ou `Step.Change`, et l'expression |
| `steps` | nombre de pas faits |
| `met` | `True` si la condition est atteinte ; `False` : limite `max_steps`, breakpoint, Break |
| `old`, `new` | Step.Change : valeurs avant et après, en texte |
| `error` | expression devenue illisible (locale hors de portée) |

<!-- test -->
```python
gdbtrace.go_till("counter >= 20")
print(gdbtrace.wait(5))                         # <arrêt : watchpoint-trigger — main (main.c:59)>
assert gdbtrace.eval("counter") == "20"

gdbtrace.break_("fib", condition="n == 5", temporary=True)
gdbtrace.go()
gdbtrace.wait(5)
gdbtrace.over()                                 # a = 0, b = 1
print(gdbtrace.step_change("a"))                # <arrêt : Step.Change a, 3 pas, 0 → 1 — fib (main.c:38)>
print(gdbtrace.step_till("i == 3"))             # <arrêt : Step.Till i == 3, 10 pas — fib (main.c:36)>
assert gdbtrace.eval("i") == "3"
r = gdbtrace.step_till("$r0 == 0x1234", asm=True, max_steps=20)
print(r["met"], r["steps"])                     # False 20 : limite atteinte
```

## 5. Breakpoints et watchpoints

`gdbtrace.break_(emplacement, hw=False, temporary=False, condition=None, count=None,
log=None, action=None, stop=False)` pose un breakpoint et renvoie un objet `Breakpoint`.
L'emplacement s'écrit comme dans gdb :

| Emplacement | Exemple |
|---|---|
| fonction | `"update_sensor"` |
| fichier:ligne | `"main.c:59"` |
| adresse | `"*0x080001f4"`, `f"*{gdbtrace.addr('fib'):#x}"` |

| Option | Effet |
|---|---|
| `temporary=True` | supprimé au premier arrêt |
| `condition="tick == 6"` | ne s'arrête que si l'expression C est vraie |
| `hw=True` | breakpoint matériel (comparateur du FPB) : indispensable en flash quand gdb ne peut pas y écrire |
| `count=5` | arrêt au 5ᵉ passage, puis à chaque passage (voir plus bas) |
| `log="n={counter}"` | message à chaque passage, puis la cible repart (voir plus bas) |
| `action=fonction` | fonction Python appelée à chaque passage, cible arrêtée (voir plus bas) |

`gdbtrace.watch(expression, access="write")` pose un watchpoint sur une variable et renvoie
son numéro. `access` vaut `"write"`, `"read"` ou `"rw"`. Les watchpoints sont matériels
(comparateurs du DWT).

Breakpoints matériels et watchpoints sont en nombre limité. Typiquement : 6 breakpoints et
4 watchpoints sur un Cortex-M3/M4, 8 et 4 sur un M7, 4 et 2 sur un M0+.

| Appel | Effet |
|---|---|
| `breaks()` | liste des breakpoints et watchpoints |
| `enable(numéro, False)` / `enable(numéro)` | désactiver / réactiver sans supprimer |
| `delete(numéro)` | supprimer |

Un `Breakpoint` a les champs suivants :
- `number`, `type` (`breakpoint`, `hw breakpoint`, `hw watchpoint`…) et `enabled` ;
- `addr`, `func`, `file`, `line`, et `expr` (expression d'un watchpoint) ;
- `hits` : nombre de passages ;
- `condition`, `count` et `ignore` (passages encore ignorés) ;
- `action` : description de l'action, vide s'il n'y en a pas.

<!-- test -->
```python
b_fonction = gdbtrace.break_("update_sensor")
b_ligne = gdbtrace.break_("main.c:59")
b_adresse = gdbtrace.break_(f"*{gdbtrace.addr('fib'):#x}")
b_cond = gdbtrace.break_("animate", condition="tick == 6", temporary=True)
b_hw = gdbtrace.break_("animate", hw=True)
gdbtrace.enable(b_hw.number, False)
w = gdbtrace.watch("mode")

for b in gdbtrace.breaks():
    print(f"{b.number:>3} {b.type:<14} {'actif' if b.enabled else 'inactif':<8} "
          f"{b.func or ''} {b.file or ''}:{b.line or ''}  ({b.hits} arrêts)")

for b in (b_fonction, b_ligne, b_adresse):     # ne garder que le conditionnel et le watchpoint
    gdbtrace.delete(b.number)
gdbtrace.go()
print(gdbtrace.wait(5))                         # animate quand tick == 6
gdbtrace.go()
arret = gdbtrace.wait(10)                       # mode écrit
print(arret["wpt"]["exp"], arret["value"]["old"], "→", arret["value"]["new"])
gdbtrace.delete(w)
```

### Breakpoints à action et à compteur

**Compteur.** `count=N` arrête la cible au **Nᵉ passage**. Les N-1 premiers passages sont
comptés par gdb, sans arrêt visible. Ensuite, le breakpoint arrête la cible à chaque passage,
jusqu'à un nouveau `count`.

**Message.** `log` est un texte, écrit à chaque passage, après quoi la cible repart. Le texte
peut contenir :
- `{expression}` : remplacé par la valeur de l'expression C ;
- `{expression:format}` : la même chose dans un format de `eval()` (`hex`, `dec`, `bin`…).
  Le format doit être collé aux deux-points : `{a ? b : c}` reste une expression ;
- `{{` et `}}` : des accolades littérales.

Les messages s'affichent dans la console du GUI, ou sur la sortie standard en `--batch`.
`break_log(n=20)` rend les derniers.

**Action.** `action` est une fonction appelée à chaque passage, cible arrêtée. Elle reçoit
l'enregistrement d'arrêt et peut tout lire avec `gdbtrace`. Ce qu'elle renvoie décide de la
suite :

| Retour | Suite |
|---|---|
| `True` | la cible s'arrête : `wait()` rend cet arrêt |
| un texte | journalisé comme un message ; la cible repart |
| `None`, `False` | la cible repart |

Avec `stop=True`, la cible s'arrête après chaque message ou action. Une action qui lève une
exception arrête la cible, et l'erreur s'affiche.

Messages et actions s'exécutent pendant un Go, sur un arrêt **interne** : aucun arrêt visible,
le GUI ne se rafraîchit pas, et la bannière « Breakpoint N » de gdb est masquée. Ils valent
aussi pour un watchpoint. Pendant un pas, l'action s'exécute, mais l'arrêt reste visible.

`break_options(numéro, condition=…, count=…, log=…, action=…, stop=…)` change les options
d'un breakpoint ou d'un watchpoint existant. Une option omise est gardée ; `None` ou `""`
la retire. Dans le GUI : vue Breakpoints, bouton **Options…** ou clic droit.

<!-- test -->
```python
b = gdbtrace.break_("animate", count=5)
gdbtrace.go()
gdbtrace.wait(5)
print(gdbtrace.eval("tick"), gdbtrace.breaks()[0].hits)    # 5 5

m = gdbtrace.break_("update_sensor", log="capteur n={counter} tick={tick:hex}")
gdbtrace.break_options(b.number, condition="tick == 8")
gdbtrace.go()
gdbtrace.wait(5)                                # animate, tick == 8
print(gdbtrace.break_log(3))                    # ['capteur n=6 tick=0x6', …, 'capteur n=8 tick=0x8']
assert gdbtrace.break_log(1) == ["capteur n=8 tick=0x8"]
gdbtrace.delete(b.number)
gdbtrace.delete(m.number)

vus = []
def mesure(arret):                              # appelée à chaque passage, cible arrêtée
    vus.append(int(gdbtrace.eval("tick", "dec")))
    if len(vus) == 30:
        return True                             # vrai : la cible s'arrête
    if len(vus) % 10 == 0:
        return f"{len(vus)} passages"           # texte : journalisé, la cible repart

gdbtrace.break_("animate", action=mesure)
gdbtrace.go()
gdbtrace.wait(10)
print(vus[0], vus[-1], gdbtrace.break_log(2))   # 9 38 ['10 passages', '20 passages']
assert len(vus) == 30
```

## 6. Lire et modifier les données

Les expressions sont du C, évaluées par gdb dans la **frame courante** : globales, locales
et arguments de la fonction arrêtée, champs, indices, pointeurs, casts. Ces appels demandent
une cible **arrêtée** ; cible en marche, voir la section 7.

| Appel | Effet |
|---|---|
| `eval(expr, fmt="natural")` | valeur sous forme de texte (Var.view) |
| `set(expr, valeur)` | affectation (Var.Assign) |
| `var_watch(expr, fmt)` | ajoute l'expression à la vue Watch du GUI ; sans GUI, affiche sa valeur |
| `reg("pc")` | valeur d'un registre (entier) |
| `addr("sensors")` | adresse d'un symbole |
| `read(adresse, n)` / `write(adresse, octets)` | lecture / écriture de mémoire brute (`bytes`) |
| `u32(adresse)` / `w32(adresse, v)` | mot de 32 bits |
| `d(symbole ou adresse, n)` | Data.dump à l'écran |
| `r()` | Register.view à l'écran |
| `bt()` | pile d'appels à l'écran |

**Formats** (`fmt`) : `natural` (format de gdb), `dec`, `hex`, `oct`, `bin`, `ascii`, comme
le menu du clic droit dans la Watch.
- Le binaire couvre toute la largeur du type : `0b1111_1111_1111_1101`.
- L'ASCII lit les octets dans l'ordre de la mémoire : `'AB\0\0'`.
- Un flottant en `hex`, `dec`, `oct` ou `bin` montre ses bits bruts.
- Pour comparer un entier, demander `dec` puis convertir : `int(gdbtrace.eval("x", "dec"))`.
  Le format naturel d'un `char` est `65 'A'`, celui d'une énumération son nom.

`session` donne aussi :
- `session.frame()` : frame courante, avec `func`, `file`, `line` et `pc` ;
- `session.backtrace()` : liste de frames ;
- `session.locals()` : locales et arguments, des `dict` avec `name` et `value` ;
- `session.registers()` : liste de couples (nom, valeur).

<!-- test -->
```python
gdbtrace.go(until="animate")
gdbtrace.wait(5)
f = session.frame()
print(f"arrêté dans {f.func} ({f.file}:{f.line}), pc = {gdbtrace.reg('pc'):#010x}")
for v in session.locals():
    print(f"  {v['name']} = {v['value']}")

print(gdbtrace.eval("sensors[1]"))                       # {id = 2, value = 1, status = 1 '\001'}
print(gdbtrace.eval("offset"), gdbtrace.eval("offset", "hex"), gdbtrace.eval("offset", "bin"))
print(gdbtrace.eval("last_char", "ascii"), gdbtrace.eval("temperature", "hex"))
n = int(gdbtrace.eval("sensors[1].status", "dec"))      # entier Python

gdbtrace.set("sensors[0].value", 1234)
adr = gdbtrace.addr("sensors")
gdbtrace.w32(adr + 4, 0xCAFE)                           # sensors[0].value, par la mémoire
assert gdbtrace.u32(adr + 4) == 0xCAFE
print(hexdump(gdbtrace.read(adr, 24), adr))
gdbtrace.var_watch("sensors", "hex")                    # GUI : vue Watch ; --batch : valeur affichée
```

## 7. Cible en marche

En mode all-stop, gdb ne lit rien pendant que la cible tourne. GdbTrace ouvre donc un
**canal mémoire latéral** qui lit sans arrêter le cœur : le port TCL avec OpenOCD, QMP sous
QEMU. `gdbtrace.live` vaut `None` si la cible n'en offre pas (J-Link ou pyOCD par
`--remote`, sans `--tcl`).

| Appel | Cible en marche |
|---|---|
| `mem(adresse, n)` | oui : par gdb à l'arrêt, par le canal live en marche |
| `live_read(adresse, n)` | oui, toujours par le canal live |
| `addr(symbole)` | à l'arrêt (faire le calcul une fois, avant `go()`) |
| `tasks()`, `queues()`, `rtos()` | oui (après un premier arrêt) |
| `profile(secondes)` | oui |
| `periph(nom)` | oui (registres lus par le canal live) |
| `eval`, `set`, `reg`, `read`, `step`… | **non** : arrêter d'abord |

<!-- test -->
```python
import time
adr = gdbtrace.addr("ticks")                    # adresse calculée cible arrêtée
gdbtrace.go()
for _ in range(3):
    time.sleep(0.1)
    ticks = int.from_bytes(gdbtrace.mem(adr, 4), "little")
    print(f"ticks = {ticks}")
gdbtrace.halt()
```

## 8. Programmer la cible

`gdbtrace.program(elf=None, verify=True)` enchaîne reset, écriture (flash avec une sonde,
RAM sous QEMU), vérification et reset. La cible reste arrêtée au vecteur de reset. Sans
argument, l'ELF courant est reprogrammé, par exemple après une recompilation. Les symboles,
les vues et la trace passent au nouveau programme.

L'appel renvoie un `ProgramResult` : `size` (octets écrits), `seconds`, `verified`
(`True`, `False` ou `None` si non vérifié), `mismatched` (sections différentes) et
`summary()`.

<!-- test -->
```python
res = gdbtrace.program()                        # affiche « demo.elf programmé : … vérifié »
assert res.verified and not res.mismatched
print(f"{res.size} octets en {res.seconds:.1f} s, pc = {gdbtrace.reg('pc'):#x}")
```

## 9. Périphériques et RTOS

**Périphériques** (`--svd F.svd`, ou `gdbtrace.load_svd("F.svd")`) : `periph("NOM")`
affiche les registres décodés, champs et énumérations compris, et renvoie
`{registre: valeur}`. Les registres à lecture destructive ne sont lus qu'avec
`force=True`. Le SVD chargé est dans `gdbtrace.device`.

<!-- test: --svd examples/svd/mps2-an385.svd -->
```python
gdbtrace.go(until="main")
gdbtrace.wait(5)
gdbtrace.over()                                 # peripherals_init() exécutée
regs = gdbtrace.periph("SysTick")                # affiche CTRL, LOAD, VAL, CALIB et leurs champs
print(f"période SysTick : {regs['LOAD'] + 1} cycles")
```

**Temps par tâche** (sur carte, capture SWO avec horodatage ITM ; firmware qui écrit le TCB
entrant sur le port ITM 31 à chaque changement de tâche, voir `examples/freertos`) :
`task_times()` donne activations, temps brut, net (sans les exceptions), part du temps et
tranches ; `task_timeline(30)` les dernières tranches.

**FreeRTOS** : `tasks()` (TASK.List), `queues()` (objets du noyau), `task("nom")`
(registres et pile d'appels d'une tâche) et `rtos()` (instantané complet). Un premier arrêt
est nécessaire pour lire la disposition des structures du noyau ; ensuite ces appels
marchent aussi cible en marche.

<!-- test: examples/freertos/freertos.elf -->
```python
import time
gdbtrace.go()
time.sleep(0.5)
gdbtrace.halt()
for t in gdbtrace.tasks():                      # affiche aussi le tableau
    if t.stack_percent and t.stack_percent > 80:
        print(f"!! pile de {t.name} à {t.stack_percent:.0f} %")
gdbtrace.queues()
```

## 10. Trace, profil et couverture

| Appel | Effet | Il faut |
|---|---|---|
| `profile(secondes, per_task=False)` | profil statistique (PERF) ; relance puis arrête la cible si elle était arrêtée | canal live ou capture SWO |
| `swo_config(cpu_hz, …)` puis `itm(port)` | programme ITM/DWT/TPIU ; texte reçu sur un port ITM | broche SWO, sonde |
| `var_trace(expr, access="write", pc=False)`, `var_samples(expr, n=20)`, `var_trace_stop(expr=None)` | trace de données du DWT : valeur d'une globale à chaque accès, cible en marche | capture SWO ou de trace |
| `etm_swo(cpu_hz, stall=…)` | ETM par la broche SWO (comme `--etm-config --etm-port swo`) | OpenOCD, cible arrêtée |
| `etm_filter("fib", start=…, stop=…)` | ne tracer qu'une plage, ou entre deux lieux ; sans argument : tout le code | trace ETM (cible arrêtée si ETM réel) |
| `trace_list(n)` | Trace.List : les dernières instructions exécutées | trace ETM ou MTB |
| `calls(n)` | flot des appels, retours et exceptions | idem |
| `timing(top, unit="us")`, `timeline(n)` | durées par fonction, chronogramme en texte | idem |
| `cov.summary()`, `cov.list(n)`, `cov.files()` | couverture de code | idem |
| `cov.save(f)`, `cov.load(f)`, `cov.export(f)`, `cov.reset()` | cumul et rapports (`.html`, `.info`, `.json`) | idem |
| `mtb(n)`, `mtb_stats()`, `mtb_probe()` | MTB d'un Cortex-M0+ | `--mtb` ou `--mtb-sim` |

**Trace de données** (sur carte, avec une capture SWO ; QEMU n'émule ni DWT ni ITM) :

```python
import time
gdbtrace.swo_config(cpu_hz=216_000_000)         # ITM, DWT, TPIU ; capture servie par OpenOCD
gdbtrace.var_trace("ticks")                     # chaque écriture de ticks
gdbtrace.var_trace("mode", "rw", pc=True)       # lectures et écritures, avec le PC
gdbtrace.go()
time.sleep(1)
gdbtrace.halt()
gdbtrace.var_samples("mode", n=None)            # temps, accès, valeur, PC → fonction
gdbtrace.var_trace_stop()
```

Sans matériel, `--etm-sim` simule la trace sous QEMU. Elle est convertie à chaque arrêt, ce
qui rend les mesures reproductibles.

<!-- test: --etm-sim -->
```python
gdbtrace.break_("animate", temporary=True)
gdbtrace.go()
gdbtrace.wait(10)
gdbtrace.trace_list(8)                          # avant l'arrêt
gdbtrace.calls(6)
gdbtrace.timing(5)                              # durées estimées par le modèle de cycles
```

<!-- test -->
```python
gdbtrace.profile(0.5, show=5)                   # sous QEMU : PC lu en marche par QMP
```

Filtre : ne tracer que `fib`. Les reprises de la trace sont marquées « trace interrompue »,
la couverture ne porte que sur la plage.

<!-- test: --etm-sim -->
```python
gdbtrace.etm_filter("fib")                      # ETM simulé : bloc aligné du DWT (ETMv3)
gdbtrace.break_("animate", temporary=True)
gdbtrace.go()
gdbtrace.wait(10)
gdbtrace.trace_list(12)
gdbtrace.cov.list()                             # fib seulement
gdbtrace.etm_filter()                           # retour à tout le code
```

## 11. Accès bas niveau

| Appel | Effet |
|---|---|
| `gdbtrace.gdb("info line main")` | commande CLI gdb ; renvoie sa sortie |
| `gdbtrace.monitor("reset halt")` | commande `monitor` du serveur gdb (OpenOCD…) ; renvoie sa sortie |
| `session.write_register("r0", 42)` | écrit un registre |
| `session.disassemble_function("fib")` | instructions (`dict` : `address`, `inst`…) |
| `session.ptype("sensors")`, `session.sizeof("sensors")` | type, taille |
| `session.backtrace()`, `session.locals()`, `session.frame()` | voir la section 6 |
| `session.state` | `"stopped"`, `"running"` ou `"disconnected"` |

<!-- test -->
```python
print(gdbtrace.gdb("info line fib").strip())
print(session.ptype("struct sensor"))
for ins in session.disassemble_function("fib")[:4]:
    print(f"{ins['address']:#010x}  {ins['inst']}")
```

## 12. Recettes

### Attendre une condition en comptant les passages

<!-- test -->
```python
b = gdbtrace.break_("update_sensor")
passages = 0
while True:
    gdbtrace.go()
    gdbtrace.wait(5)
    passages += 1
    if int(gdbtrace.eval("tick", "dec")) >= 10:
        break
gdbtrace.delete(b.number)
print(f"{passages} passages, tick = {gdbtrace.eval('tick')}")
```

Sans aller-retour avec le script :
- une condition qui ne dépend que de la fonction : breakpoint conditionnel
  (`condition="tick >= 10"`) ;
- le Nᵉ passage : `count=N` ;
- une condition sur des globales, où qu'elle devienne vraie : `go_till("ticks >= 10")`, en
  temps réel (section 4).

### Test automatique pour l'intégration continue

Le script vérifie le comportement du firmware sur sa cible et rend un code de retour : 0 si
tout est bon, 1 sinon. Un job CI lance
`python -m gdbtrace fw.elf --program --batch --script test_capteurs.py`.

<!-- test -->
```python
import sys

echecs = []


def verifie(expr, attendu):
    obtenu = int(gdbtrace.eval(expr, "dec"))
    print(f"{'ok' if obtenu == attendu else 'ÉCHEC':<5} {expr} = {obtenu} (attendu {attendu})")
    if obtenu != attendu:
        echecs.append(expr)


# update_sensor(&sensors[counter % 4], counter) avec counter == 5 : sensors[1]
gdbtrace.break_("update_sensor", condition="tick == 5", temporary=True)
gdbtrace.go()
gdbtrace.wait(5)
gdbtrace.out()                                  # la fonction est exécutée jusqu'au retour
verifie("sensors[1].value", 5)                  # fib(5)
verifie("sensors[1].status", 5)                 # tick & 0xFF
sys.exit(1 if echecs else 0)
```

### Journaliser une variable à chaque passage (CSV)

<!-- test -->
```python
import csv

gdbtrace.break_("animate")
with open("animate.csv", "w", newline="") as fh:
    journal = csv.writer(fh)
    journal.writerow(["counter", "mode", "last_char", "temperature", "offset"])
    for _ in range(20):
        gdbtrace.go()
        gdbtrace.wait(5)
        gdbtrace.out()                          # animate() exécutée : nouvelles valeurs
        journal.writerow([gdbtrace.eval("counter"), gdbtrace.eval("mode"),
                          gdbtrace.eval("last_char", "ascii"), gdbtrace.eval("temperature"),
                          gdbtrace.eval("offset")])
print(open("animate.csv").read().splitlines()[:3])
```

### Surveiller une variable sans arrêter la cible

Le script attend qu'une variable globale franchisse un seuil, en la lisant par le canal live,
puis arrête la cible au plus près.

<!-- test -->
```python
import time

if gdbtrace.live is None:
    raise SystemExit("pas de canal mémoire live pour cette cible")
adr = gdbtrace.addr("ticks")
seuil = 300
gdbtrace.go()
debut = time.monotonic()
while int.from_bytes(gdbtrace.mem(adr, 4), "little") < seuil:
    if time.monotonic() - debut > 10:
        raise TimeoutError(f"ticks n'a pas atteint {seuil}")
    time.sleep(0.01)
gdbtrace.halt()
print(f"seuil atteint en {time.monotonic() - debut:.2f} s : ticks = {gdbtrace.eval('ticks')}")
```

### Injecter une faute

On force une valeur (argument, variable, registre) puis on observe la réaction du firmware.

<!-- test -->
```python
# 1. argument : animate(tick) avec le bit 8 forcé → le mode doit passer à MODE_RUN
gdbtrace.break_("animate", temporary=True)
gdbtrace.go()
gdbtrace.wait(5)
gdbtrace.set("tick", 0x100)
gdbtrace.out()
assert gdbtrace.eval("mode") == "MODE_RUN", gdbtrace.eval("mode")

# 2. registre : la valeur rendue par fib() (r0) remplacée au retour
gdbtrace.break_("fib", temporary=True)
gdbtrace.go()
gdbtrace.wait(5)
gdbtrace.out()                                  # de retour dans update_sensor, résultat dans r0
session.write_register("r0", 1234)
gdbtrace.over()                                 # fin de la ligne : s->value = fib(...)
assert gdbtrace.eval("s->value") == "1234", gdbtrace.eval("s->value")
print("fautes injectées et observées")
```

### Campagne de couverture

<!-- test: --etm-sim -->
```python
import time
gdbtrace.break_("animate", temporary=True)      # un premier passage complet dans la boucle
gdbtrace.go()
gdbtrace.wait(10)
gdbtrace.go()                                   # puis la cible tourne un moment
time.sleep(0.5)
gdbtrace.halt()
gdbtrace.cov.summary()
gdbtrace.cov.list(5)                            # les fonctions les moins couvertes d'abord
gdbtrace.cov.save("campagne1.json")             # à fusionner plus tard : cov.load(...)
gdbtrace.cov.export("couverture.html")
```

## 13. Pièges et bonnes pratiques

- **`go()` n'attend pas.** Enchaîner avec `wait(timeout)`, sinon la ligne suivante
  s'exécute pendant que la cible tourne, et gdb refuse les lectures.
- **Toujours un délai.** `wait(5)` plutôt qu'une attente sans fin : un breakpoint jamais
  atteint donne alors une `TimeoutError` (code 1 en `--batch`) au lieu d'un script bloqué.
- **Un breakpoint peut arrêter la cible avant `halt()`.** Tester `gdbtrace.running` avant
  d'appeler `halt()` ; `halt()` sur une cible arrêtée ne fait rien.
- **Locales et frame.** Une locale n'existe que dans sa fonction. Après `out()`, on est dans
  l'appelant, et ses locales à lui sont visibles.
- **Après un reset ou `program()`**, la cible est au vecteur de reset : les globales ne sont
  pas encore initialisées par le code de démarrage. Faire `go(until="main")` d'abord.
- **Comparer des nombres** : `int(gdbtrace.eval(expr, "dec"))`, pas le texte du format
  naturel (`65 'A'`, noms d'énumération, `0x… <symbole>`).
- **Cible en marche** : seules les lectures par le canal live marchent (section 7). Calculer
  les adresses avant `go()`.
- **Breakpoints en flash** : sur une vraie cible, préférer `hw=True` si le serveur gdb ne
  sait pas poser de breakpoint logiciel en flash ; leur nombre est limité (section 5).
- **Sous QEMU**, les écritures du débogueur dans les registres de périphériques sont
  ignorées (RAM et registres du cœur seulement), et il n'y a ni ITM ni SWO.
- **Laisser la cible propre** : supprimer les breakpoints posés par le script si la session
  continue dans le GUI.
- **Actions de breakpoint.**
  - L'action tourne dans un fil à part, cible arrêtée : elle lit ce qu'elle veut, mais ne
    relance pas la cible (`go()`, `step()`). Elle renvoie `True` pour arrêter la cible.
  - Son `print()` n'atteint la console du GUI que pendant qu'un script de la console
    s'exécute. Renvoyer un texte, ou utiliser `log=` : ils vont toujours dans la console et
    dans `break_log()`.
  - Chaque passage coûte un aller-retour avec la cible, de l'ordre de la milliseconde : sur
    une fonction appelée des milliers de fois par seconde, la cible ralentit d'autant.
- **Go.Till ou Step.Till.** `go_till` est en temps réel mais exige un watchpoint matériel :
  une variable en mémoire, pas un registre. `step_till` accepte tout mais avance pas à pas.

## 14. Référence

### Run-control

| Appel | Rôle |
|---|---|
| `go(until=None)` | Go, ou Go jusqu'à un emplacement ; n'attend pas |
| `wait(timeout=10.0)` | attend l'arrêt ; renvoie l'enregistrement d'arrêt |
| `halt()` | Break |
| `step(n=1)`, `over(n=1)`, `out()`, `stepi(n=1)` | pas à pas ; renvoient l'enregistrement d'arrêt |
| `go_till(cond)` | Go.Till : Go jusqu'à ce que `cond` devienne vraie (watchpoint matériel) ; n'attend pas |
| `step_till(cond, asm=False, max_steps=1000)` | Step.Till : pas à pas jusqu'à `cond` vraie |
| `step_change(expr, asm=False, max_steps=1000)` | Step.Change : pas à pas jusqu'au changement de `expr` |
| `reset()` | reset, arrêt au vecteur de reset |
| `running` | la cible tourne-t-elle ? |

### Breakpoints

| Appel | Rôle |
|---|---|
| `break_(loc, hw=False, temporary=False, condition=None, count=None, log=None, action=None, stop=False)` | pose un breakpoint → `Breakpoint` |
| `break_options(n, condition=…, count=…, log=…, action=…, stop=…)` | change les options d'un breakpoint ou d'un watchpoint |
| `break_log(n=20)` | derniers messages des actions |
| `watch(expr, access="write")` | watchpoint (`"write"`, `"read"`, `"rw"`) → numéro |
| `breaks()` | liste → `[Breakpoint]` |
| `enable(n, on=True)` | active / désactive |
| `delete(n)` | supprime |

### Données (cible arrêtée)

| Appel | Rôle |
|---|---|
| `eval(expr, fmt="natural")` | valeur (texte) ; `fmt` : `natural`, `dec`, `hex`, `oct`, `bin`, `ascii` |
| `set(expr, valeur)` | affectation |
| `var_watch(expr, fmt="natural")` | vue Watch (GUI) ; sans GUI, affiche la valeur |
| `reg(nom)`, `addr(symbole)` | registre, adresse (entiers) |
| `read(adr, n)`, `write(adr, octets)`, `u32(adr)`, `w32(adr, v)` | mémoire |
| `d(adr ou symbole, n=64)`, `r()`, `bt()` | dump, registres, pile (à l'écran) |

### Cible en marche, périphériques, RTOS

| Appel | Rôle |
|---|---|
| `mem(adr, n)`, `live_read(adr, n)` | mémoire, aussi cible en marche |
| `live` | canal live, ou `None` |
| `load_svd(f)`, `periph(nom, force=False)`, `device` | périphériques (SVD) |
| `rtos()`, `tasks()`, `queues()`, `task(nom)` | FreeRTOS |
| `task_times(top=20, unit="us")`, `task_timeline(n=30)` | temps par tâche mesuré par l'ITM |

### Programmation

| Appel | Rôle |
|---|---|
| `program(elf=None, verify=True)` | programme, vérifie, reset → `ProgramResult` |

### Trace et mesures

| Appel | Rôle |
|---|---|
| `profile(secondes=1.0, per_task=False, show=15)` | profil statistique → `Profile` |
| `swo_config(cpu_hz, …)`, `itm(port=0)` | SWO, texte ITM |
| `var_trace(expr, access="write", pc=False)`, `var_samples(expr, n=20, fmt="natural")`, `var_trace_stop(expr=None)` | trace de données du DWT (Var.TRACE) |
| `etm_swo(cpu_hz, swo_hz=2_000_000, stall=False, stm32=False, port=3443)` | ETM par SWO |
| `etm_config(**options)` | ETM et TPIU parallèle |
| `etm_filter(*plages, start=(), stop=())` | filtre de la trace ETM ; sans argument : tout le code |
| `trace_list(n=30)`, `calls(n=40)` | Trace.List, flot d'appels |
| `timing(top=20, unit="us", n=None, kind=None)`, `timeline(n=30, unit="us")` | durées |
| `cov.summary()`, `cov.list(top, file=None)`, `cov.files()` | couverture |
| `cov.save(f)`, `cov.load(f, merge=True)`, `cov.export(f)`, `cov.reset()` | campagnes, rapports |
| `mtb(n=20)`, `mtb_stats()`, `mtb_probe()` | MTB (Cortex-M0+) |
| `cpu_hz` | fréquence du cœur (conversion cycles → µs) |

### Divers

| Appel | Rôle |
|---|---|
| `gdb(cli)`, `monitor(cmd)` | commandes gdb et `monitor` brutes |
| `run_script(chemin)` | exécute un autre script |
| `symbols` | table des symboles de l'ELF (pyelftools), chargée à la demande |
| `session` | la session gdb (section 11) |
