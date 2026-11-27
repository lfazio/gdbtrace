Les modules du paquet `gdbtrace`, de l'interface gdb (GDB/MI) aux vues. Les règles de conception (threads, deux chemins vers la mémoire, trace) sont dans `CLAUDE.md` à la racine du dépôt.

```
gdbtrace/
  mcp.py                serveur MCP (stdio) : outils pour un agent, sur une BatchSession
  core/gdb_backend.py   pilote GDB/MI asynchrone (thread lecteur, jetons, événements)
  core/session.py       API haut niveau (run-control, mémoire, registres, varobjs ; breakpoints
                        à action et à compteur, Go.Till, Step.Till / Step.Change)
  core/target.py        serveurs gdb : QEMU, OpenOCD, ST-LINK_gdbserver, distant ; programme natif
  core/rsp_relay.py     relais RSP gdb ↔ serveur : mémoire en direct, reset avec reconnexion
  core/symbols.py       ELF/DWARF direct (pyelftools) : fonctions, table des lignes
  core/live.py          canal mémoire latéral (OpenOCD TCL, QEMU QMP) + LivePoller
  core/live_vars.py     disposition et décodage des variables sans gdb (Watch en marche)
  core/formats.py       formats d'affichage (décimal, hexa, octal, binaire, ASCII), comme gdb
  trace/datatrace.py    trace de données du DWT (Var.TRACE) : comparateurs, échantillons typés
  trace/tasktrace.py    temps par tâche par l'ITM : tranches, brut, net, chronologie
  core/svd.py           parseur CMSIS-SVD (derivedFrom, dim, clusters, champs, énumérations)
  core/rtos/freertos.py awareness FreeRTOS : disposition via DWARF, parcours des listes,
                        piles, objets du noyau, contexte et pile d'appels des tâches
  trace/itm.py          décodeur ITM/DWT (instrumentation, PC, exceptions, data trace...)
  trace/tpiu.py         déformateur de trames TPIU
  trace/swo.py          sources SWO (TCP, fichier), session de trace, stats d'exceptions
  trace/config.py       programmation ITM/DWT/TPIU (ou SWO CoreSight), commandes TPIU d'OpenOCD
  trace/chips.py        ce qui dépend de la puce : SWO, horloges de trace, funnel/ETF/ETR (STM32N6)
  trace/tmc.py          ETM écrit en RAM par l'ETR, lu en continu ou à l'arrêt
  trace/profile.py      profil statistique et échantillonneur de PC
  trace/manager.py      capture SWO/ETM d'une session (GUI et batch)
  trace/etm/etm3.py     décodeur de paquets ETMv3 (Cortex-M)
  trace/etm/flow.py     reconstruction du flot d'exécution (paquets + code de l'ELF)
  trace/etm/image.py    image du code et classification Thumb-2 (capstone)
  trace/etm/encoder.py  encodeur ETMv3 (ETM simulé, tests)
  trace/etm/etm4.py     décodeur de paquets ETMv4 (Cortex-M7)
  trace/etm/flow4.py    flot ETMv4 : points de passage, exceptions, spéculation
  trace/etm/encoder4.py encodeur ETMv4 (ETM simulé, tests)
  trace/etm/qemu_log.py chemin réel depuis le journal QEMU
  trace/etm/trace.py    historique, flot d'appels ; alimente la base de couverture
  trace/coverage.py     base de couverture (E/N par adresse), lignes, fonctions, fusion
  trace/coverage_report.py  rapports HTML et LCOV
  trace/mtb.py          MTB des Cortex-M0+ : paquets, registres, décodeur, encodeur
  trace/coresight.py    tables ROM CoreSight : composants de la puce (ETM, CTI, PMU, TPIU…)
  core/mpu.py           régions de la MPU (ARMv7-M, ARMv8-M ; non sécurisée par l'alias)
  core/security.py      TrustZone : monde du cœur, SAU, pile à travers les deux mondes
  core/cores.py         plusieurs cœurs : processus du même serveur, autre serveur gdb
  core/cti.py           arrêt synchrone des cœurs par les CTI
  core/flashalgo.py     chargeur de flash : FLM, .stldr exécutés sur la cible
  core/pack.py          CMSIS-Pack : puces, mémoires, SVD, algorithmes
  trace/mtb_capture.py  capture MTB (sonde, à chaque arrêt) et MTB simulée sous QEMU
  trace/etm/config.py   programmation ETM + TPIU (port parallèle ou SWO)
  trace/etm/cycles.py   modèle de cycles Cortex-M3/M4 (durées estimées)
  trace/timing.py       analyse temporelle : pile d'appels, durées, chronogramme
  core/scripting.py     API `gdbtrace` + console Python
  core/config.py        configurations de session (YAML) traduites en ligne de commande
  gui/                  fenêtre tkinter, vues détachables, pompe d'événements thread-safe ;
                        gui/config_dialog.py : boîte de dialogue d'une configuration
  views/                Fichiers, Source, Désassemblage, Registres, Mémoire, Watch, Périphériques, RTOS,
                        Threads, Pile, Breakpoints, Console, ITM, Profil, Trace, Timing, Couverture
examples/
  firmware/             firmware de démo Cortex-M3 (SysTick 1 kHz, timer CMSDK)
  freertos/             firmware de démo FreeRTOS V11.3.3 (noyau MIT inclus) : 7 tâches,
                        queue, mutex, sémaphore, timer logiciel
  m0plus/               la démo compilée pour Cortex-M0+ (QEMU microbit), tampon MTB réservé ;
                        freertos.elf : la démo FreeRTOS de nucleo-g070 sur la même machine
  nucleo-g070/          firmware de test sur carte : la démo FreeRTOS, port ARM_CM0 (V11.3), 16 MHz
  nucleo-f072/          la même démo pour Nucleo-F072RB (Cortex-M0, 8 MHz, 16 Ko de RAM)
  nucleo-f746/          firmware de test sur carte : la démo FreeRTOS, port ARM_CM7 r0p1
  nucleo-n6/            firmware de test sur carte : la démo FreeRTOS, port ARM_CM55_NTZ, en RAM,
                        boucles ARMv8.1-M (lob.c), tampon de l'ETR réservé
  svd/mps2-an385.svd    SVD de démo (SysTick, SCB, TIMER0/1, UART0)
configs/                configurations OpenOCD (.cfg) et de session (.yaml) : démos, cartes de test
  scripts/demo.py       exemple de script
  scripts/rtos.py       exemple de script FreeRTOS
```

tkinter n'est pas thread-safe. Le lecteur gdb, la console et les actions longues tournent donc dans
des threads qui passent par `EventPump.post()`, et la boucle tkinter exécute ces appels via `after()`.
