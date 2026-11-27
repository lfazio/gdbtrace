# GdbTrace

Débogueur pour Cortex-M avec trace, construit sur **gdb** (GDB/MI) et des briques open source
(OpenOCD, QEMU, pyelftools, capstone). Interface en **tkinter** ; pilotage en Python et par un
agent (serveur MCP).

- **Débogage** : run-control, breakpoints à condition, compteur ou action, Go.Till et
  Step.Till, variables, mémoire, registres, périphériques (SVD), fautes décodées, FreeRTOS,
  programmes de l'hôte et leurs threads.
- **Trace et mesures** : ITM/SWO, profil, trace de données du DWT, ETM (v3 et v4), MTB, timing
  par fonction et par chemin d'appel, couverture de code, chrono et compteurs sans trace.
- **Cibles** : QEMU, OpenOCD, ST-LINK_gdbserver ou tout serveur gdb ; tables ROM CoreSight,
  MPU, CMSIS-Packs, algorithmes de flash (FLM, `.stldr`), TrustZone, plusieurs cœurs ; validé
  sur Nucleo-F746ZG, Nucleo-N657X0-Q (STM32N6), Nucleo-F446RE et Nucleo-F072RB.

## Documentation

**[Wiki du projet](https://github.com/lfazio/gdbtrace/wiki)** : installation, démarrage rapide,
interface, breakpoints, données, RTOS, trace (SWO, ETM, MTB), cartes, scripting Python,
serveur MCP, référence des options, dépannage, jalons, architecture, tests.

Ses pages sont dans [`Documentations/`](Documentations/Home.md) et publiées dans le wiki par
`tools/publish_wiki.sh`.

## Installation

```bash
sudo apt install python3-tk gdb-multiarch openocd qemu-system-arm gcc-arm-none-eabi
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'     # dev inclut capstone (trace ETM)
```

## Démarrage rapide

```bash
make -C examples/firmware                                        # firmware de démo Cortex-M3
.venv/bin/python -m gdbtrace examples/firmware/demo.elf          # QEMU mps2-an385, sans matériel
.venv/bin/python -m gdbtrace examples/firmware/demo.elf --etm-sim   # + trace ETM simulée
.venv/bin/python -m gdbtrace fw.elf --openocd configs/nucleo-f746zg.cfg   # sonde, OpenOCD
.venv/bin/python -m gdbtrace --config configs/nucleo-f446re.yaml # session décrite en YAML
.venv/bin/python -m gdbtrace fw.elf --batch --script s.py        # sans interface
.venv/bin/python -m gdbtrace.mcp --check                         # serveur MCP : bilan de l'environnement
```

Les autres commandes (STM32N6, Cortex-M0, ETM par SWO, MTB, programmes de l'hôte…) :
[Démarrage rapide](https://github.com/lfazio/gdbtrace/wiki/Démarrage-rapide).

## État

23 jalons, du run-control à la trace ETMv4, aux CMSIS-Packs, à TrustZone et aux puces à
plusieurs cœurs ; le détail et ce qui reste à valider sur carte :
[Jalons](https://github.com/lfazio/gdbtrace/wiki/Jalons).

## Tests

```bash
xvfb-run -a .venv/bin/python -m pytest
```

Les tests lancent QEMU ; ceux qui demandent un outil absent, un écran ou une carte sont
ignorés. Tests sur carte et oracle OpenCSD : [Tests](https://github.com/lfazio/gdbtrace/wiki/Tests).
