# gdbtrace

Débogueur pour Cortex-M dans l'esprit de Lauterbach Trace32, construit sur **gdb** (GDB/MI)
et des briques open source (OpenOCD, QEMU, pyelftools ; Orbuculum pour la trace à venir).
Interface en **tkinter**.

## État

| Jalon | Contenu | État |
|---|---|---|
| 1. Run-control | Go/Break/Step/Over/Out/Asm/Reset, breakpoints (logiciels, matériels, watchpoints), vues Source, Désassemblage, Registres, Mémoire, Watch, Pile, Breakpoints | ✅ |
| 2. Scripting | Console Python avec l'objet `t32`, scripts `--script`, mode `--batch` | ✅ |
| 3. Vues en direct | Mémoire et variables rafraîchies cible en marche (port TCL OpenOCD), vues SVD | à faire |
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
```

Raccourcis : `F5` Go, `F8` Break, `F11` Step, `F10` Over, `Shift+F11` Out, `F7` Step assembleur.
Un double-clic dans la marge de la vue Source ou Désassemblage pose ou retire un breakpoint.
Un double-clic sur un registre, un octet mémoire ou une variable Watch permet d'en modifier la valeur.
Clic droit sur un onglet : détacher la vue dans une fenêtre flottante, ou la déplacer.
Le layout est enregistré dans `gdbtrace-layout.json`.

### Console

| Saisie | Effet |
|---|---|
| `t32.break_("fib"); t32.go(); t32.wait()` | Python, avec l'API `t32` |
| `t32.d("sensors", 32)`, `t32.r()`, `t32.bt()` | Data.dump, Register.view, pile |
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
  core/scripting.py     API `t32` + console Python
  gui/                  fenêtre tkinter, vues détachables, pompe d'événements thread-safe
  views/                Source, Désassemblage, Registres, Mémoire, Watch, Pile, Breakpoints, Console
```

tkinter n'est pas thread-safe. Le lecteur gdb, la console et les actions longues tournent donc dans
des threads qui passent par `EventPump.post()`, et la boucle tkinter exécute ces appels via `after()`.

## Tests

```bash
xvfb-run -a .venv/bin/python -m pytest
```

Les tests lancent QEMU (`mps2-an385`) avec le firmware de démo. Sans `DISPLAY`, les tests GUI sont ignorés.
