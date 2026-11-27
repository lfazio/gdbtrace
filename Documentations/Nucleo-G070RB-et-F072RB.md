Ces deux cartes portent un cœur ARMv6-M, sans trace : un Cortex-M0+ sur la G070, un Cortex-M0 sur la F072. Chacune a un ST-Link V2-1 embarqué, et GdbTrace y passe par OpenOCD.

| | Nucleo-G070RB | Nucleo-F072RB |
| --- | --- | --- |
| Puce | STM32G070RB | STM32F072RB |
| Cœur | Cortex-M0+ | Cortex-M0 |
| Horloge au reset (gardée par la démo) | HSI16, 16 MHz | HSI, 8 MHz |
| Flash, RAM | 128 Ko, 36 Ko | 128 Ko, 16 Ko |
| Configuration OpenOCD | `configs/nucleo-g070rb.cfg` | `configs/nucleo-f072rb.cfg` |
| SVD (STM32CubeIDE) | `STM32G070.svd` | `STM32F0x2.svd` |
| DEV_ID (DBGMCU_IDCODE) | 0x460 | 0x448 |
| Firmware d'exemple | `examples/nucleo-g070` | `examples/nucleo-f072` |

## Ce que permet un Cortex-M0 ou M0+

Ces cœurs n'ont ni ITM, ni SWO, ni ETM, ni MTB (STM32G0 : RM0444, chapitre 40) : aucune trace. Ils ont 4 breakpoints matériels et un DWT à 2 comparateurs, avec `DWT_PCSR` sur le M0+ du G0 et sur le M0 du F072 (vérifié sur carte).

| Fonction | Sur ces cartes |
| --- | --- |
| Run-control, breakpoints (à compteur, à message), Go.Till, Step.Till et Step.Change | oui ; 4 breakpoints matériels en flash, 2 watchpoints (Go.Till en prend un) |
| Watch, Mémoire, Registres, Périphériques (SVD), RTOS | oui, cible arrêtée ou en marche (mémoire lue par OpenOCD) |
| Profil | oui, par lecture de `DWT_PCSR` cible en marche (« Live ») |
| Temps des tâches | oui, par le journal en RAM `gdbtrace_task_log` ([RTOS et threads](RTOS-et-threads)), lu en marche par OpenOCD |
| Chrono entre deux arrêts | oui, par SysTick et le compte des ticks de FreeRTOS ([Trace, profil et couverture](Trace-et-mesures)) |
| ITM, Variables tracées, Trace, Timing, Couverture | non : ni ITM ni trace d'instructions |

**Où voir les durées sans trace.** Le temps passé, on le lit dans des registres et en mémoire :

- par fonction et par ligne : vue **Profil**, source « Auto » ou « Live », **Démarrer** cible en marche. GdbTrace lit `DWT_PCSR` (le PC du cœur) des centaines de fois par seconde, sans l'arrêter, et compte où il se trouve ;
- par tâche : vue **Temps des tâches**, mesurée au cycle par le journal en RAM (activations, temps brut, tranches, chronogramme) ; et colonne **CPU** de la vue **RTOS** (compteurs de FreeRTOS, au tick) ;
- d'un arrêt à l'autre : le **chrono** de la barre d'état (par exemple d'un breakpoint au suivant).

Les durées d'appel par fonction (vue Timing) demandent une trace d'instructions, que ces cœurs n'ont pas.

GdbTrace reconnaît le cœur à la connexion (registre CPUID). Les vues ITM, Variables tracées, Trace et Couverture l'affichent : « Cortex-M0+ (ARMv6-M) : ni ITM, ni SWO, ni ETM » (ou « Cortex-M0 … »), et renvoient au profil, au temps des tâches par le journal en RAM et au chrono ; la vue Temps des tâches aussi, quand le firmware n'a pas ce journal. Une trace demandée (`--swo-config`, `--etm-config`, `--var-trace`, **Trace › ETM par SWO…**, bouton **Tracer**) est refusée avec ce même message, sans rien écrire dans des registres absents.

## Firmware d'exemple

`examples/nucleo-g070` et `examples/nucleo-f072` construisent la démo FreeRTOS de `examples/freertos` pour ces cartes :

- même `main.c` ; la sortie ITM y est inerte dès la compilation, car sur un ARMv6-M la zone de l'ITM est réservée ;
- port FreeRTOS ARM_CM0 de V11.3.3 ;
- horloge du reset, sans configuration ; tas de 16 Ko sur la G070, de 9 Ko sur la F072 ;
- même démarrage et même configuration FreeRTOS (ceux de `examples/nucleo-g070`) ; seules l'édition de liens, l'horloge et la taille du tas changent.

```bash
make -C examples/nucleo-g070
make -C examples/nucleo-f072
```

Sans carte, `examples/m0plus` construit la même démo pour QEMU `-M microbit` (`freertos.elf`, Cortex-M0) : la vue RTOS et le contexte des tâches d'un M0 s'y vérifient.

## Lancer GdbTrace sur la carte

Avec les configurations fournies (`configs/nucleo-g070rb.yaml`, `configs/nucleo-f072rb.yaml`) :

```bash
.venv/bin/python -m gdbtrace --config configs/nucleo-g070rb.yaml
.venv/bin/python -m gdbtrace --config configs/nucleo-f072rb.yaml
```

Elles équivalent à :

```bash
.venv/bin/python -m gdbtrace examples/nucleo-g070/nucleo-g070.elf --program \
    --openocd configs/nucleo-g070rb.cfg --svd STM32G070.svd --cpu-hz 16000000 --os freertos
.venv/bin/python -m gdbtrace examples/nucleo-f072/nucleo-f072.elf --program \
    --openocd configs/nucleo-f072rb.cfg --svd STM32F0x2.svd --cpu-hz 8000000 --os freertos
```

- Profil : vue **Profil**, source « Auto » ou « Live », **Démarrer** cible en marche.
- Le fichier SVD vient de STM32CubeIDE.
- Plusieurs sondes branchées : `STLINK_SERIAL=<numéro de série>` choisit la bonne.
- La connexion se fait sous reset. Pour s'attacher à un cœur qui tourne sans le réinitialiser : `CONNECT_UNDER_RESET=0` et `--no-load`.

## Test sur carte

`tests/test_board_m0.py` fait comme sur la F746, sans trace, pour chacune des deux cartes (`-k g070`, `-k f072`) :

1. construit et flashe le firmware d'exemple ;
2. lance l'interface avec la configuration de la carte (`configs/nucleo-g070rb.yaml`, `configs/nucleo-f072rb.yaml`) ;
3. relève et vérifie toutes les vues (`tests/artifacts/board-g070.json`, `board-f072.json`) : vues à l'arrêt et en marche, RTOS, profil par `DWT_PCSR`, temps des tâches par le journal en RAM, et les autres vues de trace, qui doivent dire pourquoi elles restent vides.

Il éprouve en plus les comparateurs du cœur et le chrono :

- un breakpoint à compteur ;
- un breakpoint à message : un message par passage, sans arrêt visible ;
- un Go.Till sur une globale ;
- le chrono entre deux passages par `worker_step` (5 ms d'attente, plus l'ordonnancement : environ 8 ms sur le Cortex-M0 à 8 MHz), qui doit suivre le compte des ticks du noyau à un tick près, dont un après une pause de 1 s du débogueur, qui ne doit pas compter.

Chaque carte est trouvée seule : sonde ST-LINK dans `/sys`, puis identité de la puce (DEV_ID) lue par OpenOCD. `GDBTRACE_G070_SERIAL` et `GDBTRACE_F072_SERIAL` imposent la sonde. Une carte absente ne fait sauter que ses propres tests.

```bash
xvfb-run -a .venv/bin/python -m pytest tests/test_board_m0.py -k f072 --boards=require
```
