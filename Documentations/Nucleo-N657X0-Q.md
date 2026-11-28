La Nucleo-N657X0-Q porte un STM32N6 : un Cortex-M55 (ARMv8.1-M, Helium) sans flash interne, avec un STLINK-V3EC embarqué. OpenOCD 0.12 ne connaît pas cette puce : GdbTrace passe par ST-LINK_gdbserver. L'ETM y est tracé par l'ETR de la puce, qui l'écrit en RAM, et l'ITM par le SWO de la sonde.

## Firmware d'exemple

`examples/nucleo-n6` construit la même démo FreeRTOS pour cette carte :

- port FreeRTOS ARM_CM55_NTZ (le cœur reste en état sécurisé) ;
- cœur à 64 MHz, l'horloge HSI où le laisse la ROM de démarrage ;
- image entière en AXISRAM2, à l'adresse d'un FSBL (0x34180400) ;
- à chaque tick, des boucles à faible coût (WLS, DLS, LE) et du Helium (`lob.c`), pour que la trace en contienne ;
- un tampon de 256 Ko pour l'ETR, réservé en AXISRAM1 (`__etr_buffer__`).

```bash
make -C examples/nucleo-n6
```

## Lancer GdbTrace sur la carte

La carte doit être en démarrage de développement (cavalier JP2 en 2-3) : la ROM ne charge alors rien, et le débogueur pose l'image en RAM.

```bash
.venv/bin/python -m gdbtrace --config configs/nucleo-n657x0-q.yaml
```

Elle équivaut à :

```bash
.venv/bin/python -m gdbtrace examples/nucleo-n6/nucleo-n6.elf --stlink --stlink-ap 1 --ram-boot \
    --program --svd STM32N657.svd --cpu-hz 64000000 --swo-config \
    --etm-config --etm-port etr --var-trace produced:write --os freertos
```

- `--stlink-ap 1` : le cœur est derrière le port d'accès 1, pas le 0.
- `--ram-boot` : chaque reset est un reset système ; la ROM tourne un instant, puis GdbTrace recharge l'image et pointe VTOR, sp et pc sur sa table des vecteurs (section `.isr_vector`, ou symbole `g_pfnVectors`, `__Vectors`, `vector_table`…). Couper l'alimentation efface l'image.
- `--swo-config` : ITM par le SWO, capturé par la sonde et servi par ST-LINK_gdbserver. Le SWO de la N6 est cadencé par le cœur divisé par 8 : à 64 MHz, 2 Mbit/s.
- `--etm-config --etm-port etr` : trace ETMv4.5 écrite par l'ETR dans le tampon du firmware (ou `--etr-buffer ADRESSE:TAILLE`). Dans l'interface, cible arrêtée : **Trace › ETM par l'ETR**.
- Plusieurs sondes branchées : `--stlink <numéro de série>`.

## Ce que donne la trace

- Cible en marche, GdbTrace lit le tampon en continu (toutes les 50 ms) et décode au fil de l'eau : la trace couvre toute l'exécution, pas seulement ce que tient le tampon, et la Couverture se met à jour en marche. À l'arrêt, il ne reste que la fin à lire ; Trace et Timing se redessinent.
- La sonde lit environ 550 Ko/s ; la démo en écrit environ 170 Ko/s. Si l'écriture rattrape la lecture, la zone est jetée : la trace a un trou (« trace interrompue »), et l'état de la capture compte ces rattrapages.
- En cycle-accurate (environ 3 fois plus de trace), ou avec `--etr-at-stop`, le tampon est lu seulement à l'arrêt. Il est circulaire : 256 Ko gardent environ la dernière seconde et demie de la démo, un tiers en cycle-accurate. Quand il a tourné, la trace commence par « trace interrompue », et les vues Trace et Couverture l'indiquent (« tampon plein : début de l'exécution perdu »).
- Les boucles à faible coût et le Helium du M55 sont décodés.
- Les durées de la vue Timing sont estimées, sauf avec `--etm-cycle-accurate` : le modèle de cycles ne connaît pas les attentes de l'AXISRAM, sans cache dans la démo (les durées mesurées sont environ 25 fois les estimées).

## Test sur carte

`tests/test_board_n6.py` fait comme sur la F746 :

1. charge le firmware d'exemple en RAM ;
2. lance l'interface avec la configuration `configs/nucleo-n657x0-q.yaml` : ITM par le SWO, ETM par l'ETR, une variable tracée ;
3. relève et vérifie toutes les vues (`tests/artifacts/board-n6.json`).

Il vérifie en plus le décodage des boucles LE : le nombre de tours relevé dans la trace doit suivre le firmware.

Il mesure aussi le chrono (`DWT_CYCCNT`) et les compteurs de la PMU, lit le RTT en marche par le relais, relève les tables ROM CoreSight (ETMv4, CTI, PMU) et la MPU ARMv8-M, et vérifie que le M55 tourne dans le monde sécurisé (vue Registres ; ignoré si ST-LINK_gdbserver n'expose pas les registres bancaires). Avec STM32CubeProgrammer, il programme 64 Ko de la flash XSPI par son chargeur externe (`MX66UW1G45G_STM32N6570-NUCLEO.stldr`), les relit et les efface ; sans lui, ce test est ignoré.

La carte est trouvée seule : sonde ST-LINK dans `/sys`, puis identité de la puce lue par ST-LINK_gdbserver. `GDBTRACE_N6_SERIAL` impose la sonde.

## Pour aller plus loin

### ST-LINK_gdbserver et STM32N6 (Cortex-M55)

`--stlink [SN]` lance **ST-LINK_gdbserver** (trouvé dans le PATH, STM32CubeIDE ou
STM32CubeCLT sous `/opt/st` ou `~/st`, avec le STM32CubeProgrammer dont il a besoin). gdb ne
s'y connecte pas directement mais à un **relais RSP** de GdbTrace (`core/rsp_relay.py`) :

- **mémoire en direct** : ce serveur n'a pas de port TCL, mais il répond aux lectures cible
  en marche ; le relais glisse les siennes entre les paquets de gdb (Watch, RTOS,
  Périphériques, Mémoire, échantillonnage du PC). Mesuré sur une Nucleo-N657X0-Q : 1 ms par
  lecture, 16 Ko en 27 ms ;
- **reset système** : après un reset par AIRCR, le serveur perd la cible et refuse toute
  reprise. Le relais se détache, se reconnecte et rejoue la poignée de main de gdb, qui ne voit
  rien ;
- le texte que le serveur envoie hors d'une marche (« Target is not responding ») est retiré :
  gdb le prendrait pour la réponse à sa requête.

Le **STM32N6** n'a pas de flash interne. En démarrage de développement (JP2 en 2-3), la ROM ne
charge rien. `--ram-boot` fait ce que fait l'IDE : reset système, la ROM tourne un instant,
chargement de l'image en RAM, puis VTOR, sp et pc pris dans sa table des vecteurs (section
`.isr_vector` ou symbole `g_pfnVectors`, `__Vectors`, `vector_table`…). Le cœur est derrière
le port d'accès 1 (`--stlink-ap 1`). `examples/nucleo-n6` est le firmware de test : la démo
FreeRTOS à 64 MHz (HSI, port ARM_CM55_NTZ), en AXISRAM2, avec des boucles à faible coût et
Helium appelées à chaque tick (`lob.c`).

**Trace** :

- **ITM par le SWO** : sur la N6, le SWO est un composant CoreSight à part (0xE008A000),
  cadencé par le cœur divisé par 8, qui ne porte que l'ITM. `--swo-config` le programme
  (`trace/chips.py` : puce reconnue par DBGMCU_IDCODE), et ST-LINK_gdbserver sert le flux
  capturé par la sonde (`-z`). La trace de données du DWT suit l'ARMv8-M (MATCH « adresse avec
  valeur », ACTION, DATAVSIZE ; plus de DWT_MASK).
- **ETM par l'ETR** (`--etm-config --etm-port etr`, Trace › ETM par l'ETR,
  `gdbtrace.etm_etr()`) : l'ETM n'a pas d'autre sortie que le port parallèle. Le chemin
  funnel → ETF (FIFO) → ETR écrit les trames du formateur dans un tampon circulaire en RAM.
  Le firmware le réserve (`__etr_buffer__`, aligné sur 4 Ko ; 256 Ko dans la démo), ou
  `--etr-buffer ADRESSE:TAILLE`. **Lu en continu** : cible en marche, le relais lit le
  pointeur d'écriture et les trames nouvelles toutes les 50 ms, décodées au fil de l'eau ;
  la trace n'est plus bornée par le tampon. Validé sur la carte : 3 s d'exécution (deux fois
  le tampon), et pas un tick perdu (la boucle LE de `lob_count`, appelée à chaque tick, fait
  un tour de plus à chaque appel : 2 993 appels, aucune suite rompue). La sonde lit ~550 Ko/s ;
  si l'ETR rattrape la lecture, la zone est jetée et la trace a un trou. En cycle-accurate
  (~3 fois plus de trace) et avec `--etr-at-stop`, le tampon est lu à l'arrêt : l'ETM coupé,
  l'ETF et l'ETR vidés, puis tout le tampon ; un tampon plein ne garde que la fin de
  l'exécution. Deux pièges : le réplicateur doit filtrer la sortie TPIU, sinon
  elle bloque le flux ; et le maître AXI de l'ETR sort du reset non sécurisé (RIFSC), si bien
  que la RAM refuse ses écritures sans rien signaler.
- **Décodage ARMv8.1-M** : capstone 5 ne connaît ni WLS / DLS / LE / LETP, ni BF*, ni CLRM,
  ni une partie de Helium. GdbTrace les décode lui-même : LE est un branchement conditionnel
  arrière, WLS avant, des points de passage de l'ETMv4. Une instruction Helium illisible est
  une instruction ordinaire de 32 bits. Validé sur la carte : sur une vraie trace, aucun atome
  perdu ni erreur, et le nombre de tours de la boucle LE de `lob_count` relevé dans la trace
  suit le firmware (40, 41… 64, 1, 2…). L'ETM du M55 ne spécule pas (MAXSPEC 0, lu dans
  TRCIDR8).

**Filtre** (`--etm-filter`, `--etm-start`, `--etm-stop`) : l'ETM du M55 n'a pas de
comparateurs d'adresse, mais ses « entrées comparateurs du cœur » sont ceux du DWT. Une plage
prend une paire du DWT (adresse d'instruction, puis limite incluse), dont les deux entrées
forment la ressource qui commande ViewInst ; start et stop prennent un comparateur chacun.
Plages exactes, 4 paires au plus sur un M55, comparateurs pris par le haut, partagés avec la
trace de données. Validé sur la carte : avec `--etm-filter worker_step`, seuls les appels de
`worker_step` sont tracés, chacun en entier (23 instructions de plus à chaque appel, comme le
veut le firmware). La seule entrée limite ne suffit pas : des appels y perdent leur début.

Limites : la branche d'un BF* n'est pas suivie (les compilateurs n'en émettent pas). Le SWO capturé par l'ST-LINK intégré perd parfois un octet à 2 Mbit/s :
le décodeur ITM le compte comme une erreur, sans décaler les ports.
