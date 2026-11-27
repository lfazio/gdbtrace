GdbTrace est un débogueur pour microcontrôleurs Cortex-M, avec trace, construit sur gdb (GDB/MI), avec une interface graphique tkinter. Il débogue aussi un programme de l'hôte (x86-64, AArch64…), et se pilote en Python ou par un agent (serveur MCP).

- **Débogage** : run-control, breakpoints à condition, compteur ou action, Go.Till et Step.Till, variables, mémoire, registres, périphériques (SVD), fautes décodées, RTOS (FreeRTOS), threads.
- **Trace et mesures** : ITM/SWO, profil, trace de données du DWT, ETM (v3, v4), MTB, timing par fonction et par chemin d'appel, couverture de code, chrono et compteurs sans trace.
- **Puces** : QEMU, OpenOCD, ST-LINK_gdbserver ou tout serveur gdb ; tables ROM CoreSight, MPU, CMSIS-Packs, algorithmes de flash (FLM, `.stldr`).

## Par où commencer

1. [Installation](Installation) : paquets, venv, démos.
2. [Démarrage rapide](Démarrage-rapide) : les commandes courantes, de QEMU aux cartes.
3. [Lancer GdbTrace](Lancer-GdbTrace) : cibles, configurations, programmation.

## Pages

| Thème | Pages |
| --- | --- |
| Prise en main | [Installation](Installation), [Démarrage rapide](Démarrage-rapide), [Lancer GdbTrace](Lancer-GdbTrace), [L'interface](Interface) |
| Déboguer | [Breakpoints, Go.Till et Step.Change](Breakpoints), [Variables, mémoire, registres et périphériques](Données-et-périphériques), [RTOS et threads](RTOS-et-threads) |
| Trace et mesures | [Trace, profil et couverture](Trace-et-mesures), [Trace SWO et ITM](Trace-SWO-et-ITM), [Trace d'instructions ETM](Trace-ETM), [Trace MTB](Trace-MTB) |
| Cartes | [Nucleo-F746ZG](Nucleo-F746ZG), [Nucleo-N657X0-Q (STM32N6)](Nucleo-N657X0-Q), [Nucleo-G070RB et F072RB](Nucleo-G070RB-et-F072RB), [Nucleo-F446RE](Nucleo-F446RE) |
| Automatiser | [Console et scripts](Console-et-scripts), [Scripting Python](Scripting), [Serveur MCP](Serveur-MCP) |
| Référence | [Référence des options](Référence-des-options), [Dépannage et limites](Dépannage-et-limites) |
| Projet | [Jalons](Jalons), [Architecture](Architecture), [Tests](Tests) |

Ces pages viennent du dossier `Documentations/` du dépôt, publié dans le wiki par `tools/publish_wiki.sh`. Les exemples de [Scripting Python](Scripting) sont exécutés par la suite de tests.
