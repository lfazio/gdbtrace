# gdbtrace

Débogueur pour Cortex-M dans l'esprit de Lauterbach Trace32, construit sur **gdb** (GDB/MI)
et des briques open source (OpenOCD, QEMU, pyelftools ; Orbuculum pour la trace à venir).
Interface en **tkinter**.

## État

| Jalon | Contenu | État |
|---|---|---|
| 1. Run-control | Go/Break/Step/Over/Out/Asm/Reset, breakpoints (logiciels, matériels, watchpoints), vues Source, Désassemblage, Registres, Mémoire, Watch, Pile, Breakpoints | ✅ |
| 2. Scripting | Console Python avec l'objet `t32`, scripts `--script`, mode `--batch` | ✅ |
| 3. Vues en direct | Mémoire, Watch et périphériques rafraîchis cible en marche (OpenOCD TCL / QEMU QMP), vue Périphériques CMSIS-SVD | ✅ |
| 4. RTOS | Threads, vues FreeRTOS (tâches, piles) | à faire |
| 5. Trace SWO/ITM | printf ITM, profilage statistique par échantillonnage du PC (DWT) | à faire |
| 6. Trace ETM | Capture ORBTrace, historique d'exécution, couverture de code | à faire |

## Installation

```bash
sudo apt install python3-tk gdb-multiarch openocd qemu-system-arm gcc-arm-none-eabi
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
```

## Utilisation

```bash
make -C examples/firmware                                        # firmware de démo Cortex-M3
.venv/bin/python -m gdbtrace examples/firmware/demo.elf          # QEMU mps2-an385, sans matériel
.venv/bin/python -m gdbtrace fw.elf --openocd configs/nucleo-f429zi.cfg   # sonde ST-Link
.venv/bin/python -m gdbtrace fw.elf --remote 127.0.0.1:2331      # J-Link GDB Server, pyOCD...
.venv/bin/python -m gdbtrace fw.elf --remote 127.0.0.1:3333 --tcl 127.0.0.1:6666  # OpenOCD lancé à part
.venv/bin/python -m gdbtrace examples/firmware/demo.elf --svd examples/svd/mps2-an385.svd  # périphériques
```

Raccourcis : `F5` Go, `F8` Break, `F11` Step, `F10` Over, `Shift+F11` Out, `F7` Step assembleur.
Un double-clic dans la marge de la vue Source ou Désassemblage pose ou retire un breakpoint.
Un double-clic sur un registre, un octet mémoire ou une variable Watch permet d'en modifier la valeur.
Clic droit sur un onglet : détacher la vue dans une fenêtre flottante, ou la déplacer.
Le layout est enregistré dans `gdbtrace-layout.json`.

### Vues en direct (cible en marche)

En mode all-stop, gdb refuse toute lecture mémoire tant que la cible tourne. gdbtrace ouvre donc
un **canal mémoire latéral** qui lit pendant l'exécution, comme Trace32 via le MEM-AP CoreSight :

| Cible | Canal | Remarque |
|---|---|---|
| OpenOCD (`--openocd`) | port TCL (6666), `read_memory` | OpenOCD ≥ 0.12 ; sur Cortex-M, lecture via l'AHB-AP sans arrêter le cœur |
| OpenOCD lancé à part | `--remote … --tcl HOST:PORT` | idem |
| QEMU (défaut) | QMP, commande moniteur `x` | lit aussi le bloc privé Cortex-M (SysTick, SCB) |
| J-Link, pyOCD (`--remote` seul) | aucun | vues gelées en marche, rafraîchies à l'arrêt |

L'indicateur **LIVE** de la barre d'état est vert quand le canal est disponible. Chaque vue a une
case « Live » ; les lectures de toutes les vues sont regroupées par un seul thread (`LivePoller`,
200 ms).

- **Mémoire** : le dump suit la cible en marche, octets modifiés en rouge.
- **Watch** : les variables globales et statiques sont décodées sans gdb (entiers, `char`, `bool`,
  `float`/`double`, pointeurs, énumérations), d'après l'adresse, la taille et le type calculés à
  l'arrêt. Les variables locales, les champs de bits et les objets de plus de 4 Kio restent
  **grisés** en marche : leur valeur est celle du dernier arrêt.
- **Périphériques** (`--svd` ou Fichier › Charger un SVD…) : registres et champs décodés, avec
  leurs énumérations. Seuls les périphériques dépliés sont lus, et seulement par blocs de
  registres contigus : les trous réservés ne sont jamais lus. Les registres en écriture seule ou à
  lecture destructive (`readAction`, ex. UART DATA) ne sont pas lus automatiquement ; un clic
  droit propose « Lire quand même ». Double-clic : écriture d'un registre, ou d'un champ par
  lecture-modification-écriture. Le registre est relu après chaque écriture et un écart est
  signalé.

Limite connue de QEMU : son gdbstub **ignore les écritures du débogueur dans les périphériques**
(seules la RAM et la ROM sont écrites). gdbtrace le détecte à la relecture et l'affiche. Sur une
vraie sonde, les écritures fonctionnent.

### Console

| Saisie | Effet |
|---|---|
| `t32.break_("fib"); t32.go(); t32.wait()` | Python, avec l'API `t32` |
| `t32.d("sensors", 32)`, `t32.r()`, `t32.bt()` | Data.dump, Register.view, pile |
| `t32.mem(addr, n)`, `t32.live_read(addr, n)` | lecture mémoire, y compris cible en marche |
| `t32.periph("TIMER0")` | PER.view : registres et champs décodés, retourne `{registre: valeur}` |
| `!info breakpoints` | Commande CLI gdb brute |
| `mon reset halt` | Commande `monitor` vers OpenOCD |

Voir `gdbtrace/core/scripting.py` pour l'API complète et `examples/scripts/demo.py`.

## Architecture

```
gdbtrace/
  core/gdb_backend.py   pilote GDB/MI asynchrone (thread lecteur, jetons, événements)
  core/session.py       API haut niveau (run-control, mémoire, registres, varobjs)
  core/target.py        serveurs gdb : QEMU, OpenOCD, distant
  core/symbols.py       ELF/DWARF direct (pyelftools) : fonctions, table des lignes
  core/live.py          canal mémoire latéral (OpenOCD TCL, QEMU QMP) + LivePoller
  core/live_vars.py     disposition et décodage des variables sans gdb (Watch en marche)
  core/svd.py           parseur CMSIS-SVD (derivedFrom, dim, clusters, champs, énumérations)
  core/scripting.py     API `t32` + console Python
  gui/                  fenêtre tkinter, vues détachables, pompe d'événements thread-safe
  views/                Source, Désassemblage, Registres, Mémoire, Watch, Périphériques, Pile,
                        Breakpoints, Console
examples/
  firmware/             firmware de démo Cortex-M3 (SysTick 1 kHz, timer CMSDK)
  svd/mps2-an385.svd    SVD de démo (SysTick, SCB, TIMER0/1, UART0)
  scripts/demo.py       exemple de script
```

tkinter n'est pas thread-safe. Le lecteur gdb, la console et les actions longues tournent donc dans
des threads qui passent par `EventPump.post()`, et la boucle tkinter exécute ces appels via `after()`.

## Tests

```bash
xvfb-run -a .venv/bin/python -m pytest
```

Les tests lancent QEMU (`mps2-an385`) avec le firmware de démo. Sans `DISPLAY`, les tests GUI sont ignorés.
Le client TCL OpenOCD est testé contre un faux serveur TCL ; il reste à valider sur une sonde réelle.
