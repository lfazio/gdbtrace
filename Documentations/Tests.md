```bash
xvfb-run -a .venv/bin/python -m pytest
```

Les tests lancent QEMU (`mps2-an385`) avec le firmware de démo. Sans `DISPLAY`, les tests GUI sont ignorés.
Le client TCL OpenOCD est testé contre un faux serveur TCL, et contre un vrai OpenOCD lancé sans
sonde (`noinit`) s'il est installé ; les tests sur carte (ci-dessous) s'en servent sur la F746,
la F446 et la F072. Ce que chaque carte a validé, jalon par jalon :
[Validation sur carte](Jalons#validation-sur-carte). Restent à valider sur sonde réelle : le
support RTOS d'OpenOCD (`--rtos`), le port de trace parallèle et la MTB d'un M0+.
Les tests ETM comparent notre décodeur à OpenCSD si `tools/build_opencsd.sh` a été lancé
(sinon ce test est ignoré) ; la trace ETM nécessite `pip install capstone` (extra `etm`).

## Tests sur carte (à chaque release)

```bash
tools/release_check.sh          # toute la suite ; une carte de test absente = échec
```

Chaque test sur carte lance la session de sa configuration (`configs/<carte>.yaml`, `--config`) :
les configurations sont ainsi validées sur les cartes, et `tests/test_config.py` vérifie
qu'elles redonnent exactement les arguments des sessions de test.

`tests/test_board_f746.py` teste l'application sur une **Nucleo-F746ZG** : le firmware de test
est construit depuis ce dépôt (`examples/nucleo-f746` : la démo FreeRTOS à 16 MHz, port
FreeRTOS ARM_CM7 r0p1) et flashé ; GdbTrace est lancé par son vrai point d'entrée, GUI compris,
avec le SVD, l'ETM et l'ITM par SWO (stall) et une variable tracée par le DWT. Trois phases
(arrêt sur `worker_step`, marche avec le profil, arrêt), et à chacune le contenu de toutes les
vues est relevé dans `tests/artifacts/board-f746.json` ; un test par vue vérifie ce qui doit
s'y afficher : source, registres, pile (fichier:ligne), Watch (et en direct), RTOS (piles,
tick en direct), périphériques du SVD, mémoire, vues selon l'OS, texte ITM, profil par SWO, variable
tracée, temps par tâche, liste de trace ETM, Timing, couverture.

`tests/test_board_n6.py` fait de même sur une **Nucleo-N657X0-Q** (STM32N6, Cortex-M55), par
ST-LINK_gdbserver et son relais : firmware `examples/nucleo-n6` chargé en RAM, ITM par le SWO,
ETM écrit en RAM par l'ETR, variable tracée par le DWT de l'ARMv8-M, vues en direct par le
relais ; en plus, le nombre de tours des boucles LE relevé dans la trace doit suivre le
firmware (`tests/artifacts/board-n6.json`).

`tests/test_board_f446.py` fait de même sur une **Nucleo-F446RE** (STM32F446, Cortex-M4F),
par OpenOCD : firmware `examples/nucleo-f446` flashé (la démo FreeRTOS à 16 MHz), ETMv3 et
ITM par SWO, l'ETM démarré à l'entrée de `worker_step` et arrêté à son retour (sans filtre,
l'ETMv3 prend toute la broche et le firmware reste bloqué dans l'attente de la FIFO de
l'ITM). En plus, le nombre de tours de la boucle de chaque appel doit suivre le firmware, et
un filtre par plage, en mode batch, doit donner la même trace brute qu'OpenCSD
(`tests/artifacts/board-f446.json`).

`tests/test_board_m0.py` fait de même sur les cartes ARMv6-M, chacune ses tests (`-k g070`,
`-k f072`) : **Nucleo-G070RB** (STM32G070RB, Cortex-M0+) et **Nucleo-F072RB** (STM32F072RB,
Cortex-M0), par OpenOCD : firmware `examples/nucleo-g070` ou `examples/nucleo-f072` flashé,
SVD, aucune trace (le cœur n'en a pas).
Les vues de trace (ITM, Trace, Couverture, Variables tracées, Temps des tâches) doivent dire
pourquoi ; le profil échantillonne `DWT_PCSR` ; et le run-control éprouve les comparateurs du
cœur : breakpoint à compteur, breakpoint à message (arrêts internes, un message par passage),
Go.Till sur une globale (watchpoint du DWT) (`tests/artifacts/board-g070.json`, `board-f072.json`).

Sur chaque carte, la session éprouve aussi les mesures et la puce (`extra`, `tests/boards.py`) :
- le chrono entre deux passages par `worker_step`, comparé au compte des ticks du noyau
  (`DWT_CYCCNT` ; SysTick sur la F072 et la G070), une pause de 1 s du débogueur non comptée ;
- le RTT lu en marche (port TCL d'OpenOCD ; relais de ST-LINK_gdbserver sur la N6) ;
- les tables ROM CoreSight et la MPU relue en entier (vue Système) ;
- les compteurs du DWT (F746, F446) ou de la PMU (N6) ;
- sur la N6, le monde sécurisé du M55 dans la vue Registres, si ST-LINK_gdbserver expose les
  registres bancaires ;
- si l'outil est installé : la flash de la F446 programmée par le FLM du pack
  Keil.STM32F4xx_DFP, la flash XSPI de la N6 par le chargeur externe de STM32CubeProgrammer
  (`MX66UW1G45G_STM32N6570-NUCLEO.stldr`) ; sinon, ces tests sont ignorés.

- La carte est trouvée seule : sonde ST-LINK lue dans `/sys`, puis identité de la puce lue
  sans reset, par OpenOCD (F746, F446, G070, F072) ou par ST-LINK_gdbserver (N6, qu'OpenOCD
  0.12 ne connaît pas). `GDBTRACE_F746_SERIAL` / `GDBTRACE_F446_SERIAL` /
  `GDBTRACE_N6_SERIAL` / `GDBTRACE_G070_SERIAL` / `GDBTRACE_F072_SERIAL` imposent la sonde ;
  le SVD vient de
  STM32CubeIDE (`GDBTRACE_SVD_DIR` sinon).
- `pytest --boards=auto` (défaut) ignore ces tests sans la carte ; `--boards=require` les fait
  échouer ; `--boards=skip` les désactive.
- Le test remplace le firmware de la carte, et la laisse sans ETM ni stall.
