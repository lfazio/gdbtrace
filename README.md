# GdbTrace

Débogueur pour Cortex-M avec trace, construit sur **gdb** (GDB/MI)
et des briques open source (OpenOCD, QEMU, pyelftools ; Orbuculum pour la trace à venir).
Interface en **tkinter**.

## État

| Jalon | Contenu | État |
|---|---|---|
| 1. Run-control | Go/Break/Step/Over/Out/Asm/Reset, breakpoints (logiciels, matériels, watchpoints), vues Fichiers, Source, Désassemblage (source C mêlée), Registres, Mémoire, Watch, Pile, Breakpoints | ✅ |
| 2. Scripting | Console Python avec l'objet `gdbtrace`, scripts `--script`, mode `--batch` | ✅ |
| 3. Vues en direct | Mémoire, Watch et périphériques rafraîchis cible en marche (OpenOCD TCL / QEMU QMP), vue Périphériques CMSIS-SVD | ✅ |
| 4. RTOS | Vue FreeRTOS (tâches, piles, CPU, queues/mutex/sémaphores), contexte et pile d'appels de chaque tâche, en direct | ✅ |
| 5. Trace SWO/ITM | printf ITM, trace des exceptions, profilage statistique (fonctions, lignes, tâches) par SWO ou lecture du PC en marche | ✅ |
| 6. Trace ETM | Trace d'instructions ETMv3 (Cortex-M3/M4) : listing avant l'arrêt, flot d'appels, couverture ; ETM simulé sous QEMU ; décodage validé contre OpenCSD | ✅ |
| 7. Analyse temporelle | Durées par fonction et par exception (appels, brut, net, min/max/moy), chronogramme des appels ; ETM cycle-accurate (validé contre OpenCSD) ou modèle de cycles | ✅ |
| 8. Couverture de code | Couverture du code objet par la trace ETM, sans instrumentation : instructions, branches prises / non prises, lignes, fonctions ; cumul et fusion de campagnes ; rapports HTML et LCOV | ✅ |
| 9. MTB et ETM par SWO | Trace MTB des Cortex-M0+ (décodage validé sur chemins QEMU, démo M0+, MTB simulée) ; ETM d'un M3/M4 par la broche SWO, sans sonde de trace | ✅ (à valider sur carte) |
| 10. ETMv4 (Cortex-M7) | Trace d'instructions de l'ETM-M7 (ETMv4.0) : décodage validé contre QEMU, OpenCSD et une vraie capture par SWO sur Nucleo-F746ZG ; version de l'ETM détectée ; ETM simulé v4 sous QEMU (`mps2-an500`) | 🚧 (cycle-accurate et horodatage à venir) |
| 11. Trace ciblée et débogage avancé | Filtres de l'ETM (plages, start/stop) ; trace de données du DWT ; temps par tâche RTOS par ITM ; breakpoints à action et à compteur, Go.Till, Step.Change | ✅ |

## Installation

```bash
sudo apt install python3-tk gdb-multiarch openocd qemu-system-arm gcc-arm-none-eabi
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'     # dev inclut capstone (trace ETM)
```

## Utilisation

```bash
make -C examples/firmware                                        # firmware de démo Cortex-M3
.venv/bin/python -m gdbtrace examples/firmware/demo.elf          # QEMU mps2-an385, sans matériel
.venv/bin/python -m gdbtrace fw.elf --openocd configs/nucleo-f429zi.cfg   # sonde ST-Link
.venv/bin/python -m gdbtrace fw.elf --remote 127.0.0.1:2331      # J-Link GDB Server, pyOCD...
.venv/bin/python -m gdbtrace fw.elf --remote 127.0.0.1:3333 --tcl 127.0.0.1:6666  # OpenOCD lancé à part
.venv/bin/python -m gdbtrace examples/firmware/demo.elf --svd examples/svd/mps2-an385.svd  # périphériques
make -C examples/freertos && .venv/bin/python -m gdbtrace examples/freertos/freertos.elf  # FreeRTOS
.venv/bin/python -m gdbtrace fw.elf --openocd configs/nucleo-f429zi.cfg --rtos  # tâches = threads gdb
.venv/bin/python -m gdbtrace fw.elf --openocd configs/nucleo-f429zi.cfg --swo-config --cpu-hz 180000000  # ITM/SWO
.venv/bin/python -m gdbtrace fw.elf --swo 127.0.0.1:3443          # SWO servi par Orbuculum / J-Link
.venv/bin/python -m gdbtrace fw.elf --swo-file capture.bin        # rejouer une capture SWO
.venv/bin/python -m gdbtrace examples/firmware/demo.elf --etm-sim  # trace ETM simulée (QEMU)
.venv/bin/python -m gdbtrace fw.elf --openocd … --etm-config --stm32 --trace 127.0.0.1:PORT  # ETM réel
.venv/bin/python -m gdbtrace fw.elf --openocd … --etm-config --etm-port swo --cpu-hz 4000000  # ETM par SWO
make -C examples/m0plus && .venv/bin/python -m gdbtrace examples/m0plus/demo.elf --machine microbit --mtb-sim  # M0+ + MTB
.venv/bin/python -m gdbtrace fw.elf --openocd … --mtb --mtb-probe  # MTB d'un vrai Cortex-M0+
```

### Programmer la cible

**Fichier › Programmer un ELF…** (`Ctrl+O`) écrit un ELF dans la cible, sans relancer le
débogueur. La séquence : arrêt, reset, écriture (`load` : en flash avec OpenOCD, J-Link ou
pyOCD, en RAM sous QEMU), vérification (`compare-sections`), puis reset. La cible reste
arrêtée au vecteur de reset. Symboles, vues, Watch et trace ETM passent au nouveau programme.
**Reprogrammer l'ELF courant** (`Ctrl+R`) sert après une recompilation. **ELF récents**
garde les 8 derniers, enregistrés avec le layout. Sans ELF sur la ligne de commande, la GUI
démarre sans cible : choisir l'ELF lance la cible (`--qemu`, `--openocd`… ) puis la programme.

| Option | Effet |
|---|---|
| `--program [ELF]` | programme l'ELF à la connexion (par défaut celui de la ligne de commande) |
| `--no-verify` | sans vérification |
| `--go` | lance le programme ensuite |
| `--batch` sans `--script` | programme puis quitte : code de retour 1 si la vérification échoue |

```bash
.venv/bin/python -m gdbtrace --program build/fw.elf --openocd configs/nucleo-f429zi.cfg --batch --go  # flasher et lancer
.venv/bin/python -m gdbtrace --program build/fw.elf --openocd configs/nucleo-f429zi.cfg              # flasher puis déboguer
.venv/bin/python -m gdbtrace --openocd configs/nucleo-f429zi.cfg     # GUI : choisir l'ELF par le menu
```

Depuis un script : `gdbtrace.program("build/fw.elf")`, ou `gdbtrace.program()` pour l'ELF courant.

Sous QEMU, **Reset** est un vrai reset de la machine (`system_reset` sur un second port QMP) :
périphériques, NVIC et mode du cœur repartent à neuf. Un ELF programmé depuis le lancement est
réécrit après le reset, car QEMU recharge l'image passée au démarrage.

Raccourcis : `F5` Go, `F8` Break, `F11` Step, `F10` Over, `Shift+F11` Out, `F7` Step assembleur,
`Ctrl+O` Programmer un ELF, `Ctrl+R` Reprogrammer.
Un double-clic dans la marge de la vue Source ou Désassemblage pose ou retire un breakpoint.
- **Breakpoints à action et à compteur** (vue Breakpoints, bouton **Options…** ou clic droit).
  La colonne « Condition / action » résume les options. Elles valent aussi pour un watchpoint.
  - **Condition C** : arrêt seulement si elle est vraie.
  - **Arrêt au passage n°** (Break.Set /COUNT) : gdb compte les passages sans arrêter la
    cible, puis l'arrête à ce passage et aux suivants.
  - **Message** (Break.Set /CMD), par exemple `capteur n={counter} tick={tick:hex}` :
    - à chaque passage, les `{expression}` sont remplacées par leur valeur (avec un format
      au besoin), le texte s'écrit dans la console, puis la cible repart ;
    - c'est un arrêt interne : rien ne se rafraîchit et la bannière de gdb est masquée ;
    - avec « S'arrêter après le message », la cible reste arrêtée.
  - Un script peut aussi poser une **action Python** (`gdbtrace.break_(…, action=fonction)`),
    qui décide de l'arrêt.
- **Go.Till** (menu Run) : Go jusqu'à ce qu'une condition C devienne vraie, par exemple
  `counter >= 20`.
  - Temps réel : watchpoint matériel sur les variables de l'expression, retiré à la reprise
    suivante.
  - Un registre ne peut pas être suivi ainsi : utiliser Step.Till.
- **Step.Till / Step.Change** (menu Run) : pas à pas, par ligne source ou par instruction,
  jusqu'à ce que la condition soit vraie, ou jusqu'à ce que la valeur de l'expression change.
  - C'est une seule reprise : la GUI ne se rafraîchit qu'à la fin, Break interrompt la
    boucle, et un nombre de pas maximum la borne.
  - La barre d'état donne le bilan, par exemple
    `Step.Change last_char, 39 pas, 71 'G' → 72 'H'` (après un Go.Till `counter >= 7`).
- **Fichiers** (panneau de gauche) : les fichiers sources du programme (table des lignes DWARF),
  par dossier, les bibliothèques de la chaîne sous « Externes ». Chaque fichier se déplie sur ses
  fonctions ; double-clic : la source s'ouvre. Le filtre cherche dans les noms de fichiers et de
  fonctions. Le fichier où s'est arrêté le PC est en gras.
- **Désassemblage** : « Source mêlée » (List.Mix, actif par défaut) intercale chaque ligne C
  avant ses instructions ; un double-clic sur une ligne C pose un breakpoint source, sur une
  instruction un breakpoint à l'adresse.
- **Watch** : une expression ajoutée cible en marche est évaluée à l'arrêt suivant ; une locale
  hors de portée est réévaluée à chaque arrêt.
- **Format d'affichage** (Watch et locales de la vue Pile) : un clic droit sur une variable
  propose Naturel, Décimal, Hexadécimal, Octal, Binaire ou ASCII.
  - Le format d'une structure ou d'un tableau vaut pour tous ses membres, y compris ceux
    dépliés plus tard ; un membre peut avoir le sien.
  - Il s'applique aussi cible en marche (Watch en direct), et il est gardé dans le layout.
    Une locale garde son format par fonction.
  - Comme `print/x` de gdb, un flottant en hexadécimal, décimal, octal ou binaire montre ses
    bits bruts (`21.5` → `0x41ac0000`).
  - Le binaire couvre toute la largeur du type, par groupes de 4 (`0b1111_1111_1111_1101`).
  - L'ASCII lit les octets dans l'ordre de la mémoire, comme Data.dump (`'AB\0\0'`).
Un double-clic sur un registre, un octet mémoire ou une variable Watch permet d'en modifier la valeur.
Clic droit sur un onglet : détacher la vue dans une fenêtre flottante, ou la déplacer.
Le layout est enregistré dans `gdbtrace-layout.json` ; les vues ajoutées depuis la sauvegarde
apparaissent à leur place par défaut.

### Vues en direct (cible en marche)

En mode all-stop, gdb refuse toute lecture mémoire tant que la cible tourne. gdbtrace ouvre donc
un **canal mémoire latéral** qui lit pendant l'exécution, via le MEM-AP CoreSight :

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

### Vue RTOS (FreeRTOS)

La vue s'active quand FreeRTOS est lié dans l'ELF. Elle lit les structures du noyau
directement en mémoire : il n'y a rien à ajouter au firmware, et elle fonctionne avec QEMU,
OpenOCD ou J-Link. La disposition des structures (offsets de `TCB_t`, `List_t`, `Queue_t`) est
lue une fois dans le DWARF via gdb, ce qui respecte la configuration réelle du noyau.

- **Tâches** : état (Running, Ready, Blocked, Suspended, Deleted), priorité (et priorité de
  base en cas d'héritage par mutex), pile utilisée / taille, taux maximal d'utilisation et
  octets jamais utilisés (même calcul que `uxTaskGetStackHighWaterMark` ; ligne rouge au-delà de
  90 %), % CPU (`ulRunTimeCounter`), objet attendu (queue, sémaphore, mutex ou délai). La tâche
  courante est en gras et rappelée dans la barre d'état.
- **Objets noyau** (registre des queues, `configQUEUE_REGISTRY_SIZE` > 0) : queues, mutex
  (détenteur), sémaphores, tâches en attente d'envoi ou de réception.
- **Contexte d'une tâche** (double-clic, cible arrêtée) : registres relus sur la pile de la
  tâche et pile d'appels. gdb ne connaît qu'un seul fil d'exécution : gdbtrace y charge
  temporairement les registres de la tâche le temps de dérouler sa pile, puis **restaure
  toujours** ceux du cœur. Avec `--openocd … --rtos`, OpenOCD expose les tâches comme threads
  gdb et ce sont eux qui sont utilisés (chemin non testé sans sonde).
- **En marche** : la vue est rafraîchie par le canal live. Une lecture faite pendant un
  changement de contexte peut être incohérente ; elle est détectée (cycles, compteurs,
  `pvOwner`), relancée jusqu'à 4 fois, sinon ignorée.

Configuration conseillée du noyau : `configUSE_TRACE_FACILITY 1`,
`configRECORD_STACK_HIGH_ADDRESS 1` (taille des piles), `configCHECK_FOR_STACK_OVERFLOW 2`
(remplissage 0xA5), `configQUEUE_REGISTRY_SIZE` > 0, `configGENERATE_RUN_TIME_STATS 1`. Ports
gérés pour le contexte des tâches : ARM_CM3 (testé) et ARM_CM4F.

### Temps des tâches (vue Temps des tâches, TASK.STATistic)

Le temps de chaque tâche RTOS, mesuré exactement (pas d'échantillonnage). À chaque
changement de tâche, le firmware écrit l'adresse du TCB entrant sur le port ITM 31 ; avec
l'horodatage ITM, chaque tranche d'exécution est datée. GdbTrace relit le nom de la tâche
dans le TCB.

La vue donne, par tâche :
- le nombre d'activations ;
- le temps brut, et le temps net sans les exceptions (d'après la trace des exceptions du
  DWT) ;
- la part du temps, et les tranches maximale et moyenne.

Le chronogramme montre les dernières tranches, une rangée par tâche, avec la durée au survol.

Côté firmware FreeRTOS, trois lignes dans `FreeRTOSConfig.h` et une fonction, comme dans
`examples/freertos` (inerte tant que le débogueur n'a pas activé l'ITM) :

```c
#define GDBTRACE_TASK_PORT 31
void gdbtrace_task_switched_in(void *tcb);         /* écrit tcb sur le port ITM 31 */
#define traceTASK_SWITCHED_IN() gdbtrace_task_switched_in((void *)pxCurrentTCB)
```

Il faut une capture SWO ou de trace avec l'horodatage ITM (`--swo-config`, Trace › ETM par
SWO…). Script : `gdbtrace.task_times()`, `gdbtrace.task_timeline(30)`. Validé : le crochet de
la démo reçoit exactement `pxCurrentTCB` à chaque changement (vérifié sous QEMU), calculs
sur des flux ITM reconstitués ; la capture réelle reste à valider sur carte.

### Trace SWO/ITM et profilage

**ITM (vue ITM, l'équivalent de TERM)** : le firmware écrit sur les ports de stimulus de l'ITM
(`ITM_SendChar` de CMSIS, ou `itm_puts` dans `examples/freertos/main.c`). Le flux sort par la
broche SWO et gdbtrace le décode : texte UTF-8 par port (port 0 par défaut), valeurs brutes en
hexa pour les autres ports, filtre de ports et horodatage. La capture vient :
- d'**OpenOCD** : `--swo-config` (ou « Configurer la cible… ») programme ITM, DWT et TPIU,
  configure le TPIU d'OpenOCD (`<cible>.tpiu configure -protocol uart … -output :3443`),
  puis se connecte au port ;
- d'**Orbuculum** ou de **J-Link** : `--swo HOST:PORT` ;
- d'un **fichier** de capture brute : `--swo-file` (avec `--swo-formatter` si le formateur
  TPIU était actif, par exemple sur une sonde ORBTrace).

**Profil (vue Profil, l'équivalent de PERF)** : profilage statistique sans instrumenter le
code. On obtient le % du temps CPU par fonction, par ligne (double-clic : la source s'ouvre) et
par tâche FreeRTOS. Sources des échantillons :

| Source | Comment | Cible |
|---|---|---|
| SWO | paquets « PC sample » du DWT (`--swo-config`, échantillonnage réglé via CYCTAP/POSTPRESET) | sonde + broche SWO |
| Live | lecture de `DWT_PCSR` par l'AHB-AP, cœur non arrêté | OpenOCD, Cortex-M3/M4/M7 (pas M0) |
| Live | PC de la vCPU lu par le moniteur (~2 000 éch./s) | QEMU |

« Auto » prend le SWO si une capture tourne, sinon le Live. Un profil qui marche ne prouve
donc pas que le SWO est capturé : la vue ITM et les vues Trace et Couverture disent si une
capture tourne et ce qu'elle reçoit.

L'onglet **Exceptions** compte les entrées dans chaque exception ou IRQ et, si l'horodatage
ITM est actif, le temps total et maximal passé dedans. Ces données viennent du SWO.

**Trace de données (vue Variables tracées, Var.TRACE / Var.DRAW)** : la valeur d'une
variable globale ou statique à chaque accès, cible en marche, sans l'arrêter ni modifier le
code.

- Un comparateur du DWT surveille l'adresse de la variable. Selon l'accès choisi (écriture,
  lecture, les deux), il émet la valeur par l'ITM, et au choix le PC de l'instruction qui y
  accède.
- La vue liste les variables tracées, leur nombre d'échantillons et leur dernière valeur (au
  format de la variable). Elle dessine un petit graphe par variable, chacun à sa propre
  échelle, en temps réel. Le survol donne les valeurs à un instant.
- Le temps vient de l'horodatage ITM (cycles du cœur, `--cpu-hz`), sinon de l'heure de
  réception.

| Lancement | Comment |
|---|---|
| Ligne de commande | `--var-trace counter` (écriture), `--var-trace offset:rw+pc` ; répétable ; avec une capture SWO ou de trace |
| GUI | vue Variables tracées : saisir l'expression, choisir l'accès, Tracer (cible arrêtée) |
| Script | `gdbtrace.var_trace("counter", "rw", pc=True)`, `gdbtrace.var_samples("counter")`, `gdbtrace.var_trace_stop()` |

Limites : variables scalaires de 1, 2 ou 4 octets (un champ ou un élément d'une structure),
4 comparateurs au plus sur un Cortex-M3/M4/M7. Ces comparateurs sont partagés avec le filtre
ETMv3 et les watchpoints de gdb ; GdbTrace les prend par le haut et refuse quand il n'en reste
plus. Un accès plus court que la variable est montré en hexadécimal brut. Validé sur des flux
ITM synthétiques ; à valider sur carte.

QEMU n'émule ni l'ITM ni le SWO : sous QEMU, la sortie ITM du firmware reste inerte et
seul le profil « Live » fonctionne. Le décodage SWO est testé sur des flux synthétiques et un
faux serveur ; il reste à valider sur sonde réelle.

### Trace d'instructions ETM (vue Trace)

L'ETM d'un Cortex-M3/M4 (ETMv3.5) émet un flux compact (moins d'un bit par instruction) qui
permet de reconstruire **chaque instruction exécutée**, combiné au code de l'ELF. La vue Trace
(l'équivalent de Trace.List) propose :
- **Listing** : les N dernières instructions avant l'arrêt (« ce qui s'est passé avant le
  crash »), avec la fonction, le désassemblage, la ligne source, les exceptions et leurs retours.
  Double-clic : la source s'ouvre ;
- **Appels** : appels, retours, exceptions, indentés par profondeur.

La couverture de code a sa propre vue, décrite plus bas.

Sources de trace :

| Source | Option | Remarque |
|---|---|---|
| ETM simulé (QEMU) | `--etm-sim` | QEMU n'a pas d'ETM : son journal d'exécution est activé à chaque Go, puis converti à l'arrêt en flux ETMv3 (ETMv4 sur une machine Cortex-M7 : `--machine mps2-an500`), tel que l'émettrait un ETM-M, et décodé comme une vraie capture. QEMU tourne plus lentement (une instruction par bloc TCG). |
| Port de trace parallèle | `--etm-config [--etm-width 4] [--stm32]` puis `--trace HOST:PORT` ou `--trace-file F` | flux TPIU formaté : ETM (ID 2) et ITM (ID 1) séparés automatiquement. Il faut une sonde de trace (ex. ORBTrace Mini avec Orbuculum, qui sert le flux brut en TCP) et une carte qui expose TRACECLK/TRACED0-3. |
| Broche SWO | `--etm-config --etm-port swo --cpu-hz N [--swo-hz N] [--etm-stall] [--stm32]` (OpenOCD), dans le GUI **Trace › ETM par SWO…**, ou `--swo HOST:PORT` / `--swo-file F` | même flux formaté, sur la broche SWO, capturé par la sonde de débogage (ST-Link, J-Link...). Faible débit : voir ci-dessous. |

`--etm-config` programme l'ETM (ID de trace 2, trace de tout le code), le TPIU en port
parallèle 1/2/4 bits avec formateur, et avec `--stm32` les broches de trace (DBGMCU_CR).
L'architecture de l'ETM est lue sur la cible (TRCIDR1 ; sous QEMU, le cœur d'après CPUID) :
ETMv3 pour un Cortex-M3/M4, ETMv4 pour un Cortex-M7, et le décodeur suit. `--etm-stall`
ralentit le cœur plutôt que de perdre de la trace quand la FIFO de l'ETM se remplit.

**Filtre de trace** (`--etm-filter`, `--etm-start`, `--etm-stop`, Trace › Filtre de trace
ETM…, `gdbtrace.etm_filter()`) : ne tracer qu'une partie du code, ce qui laisse tout le
débit du port (ou du SWO) à ce qui compte.

- **Plages** : une fonction (`fib`), un fichier (`main.c` : ses fonctions, plages contiguës
  fusionnées) ou des adresses (`0x100-0x200`, `0x100+64`).
- **Start / stop** : la trace démarre en exécutant un lieu (fonction, `main.c:59`, adresse)
  et s'arrête après un autre. Avec des plages, les deux conditions se combinent.

Chaque reprise est un trou marqué dans Trace.List (« trace interrompue ») et dans le flot
d'appels ; l'analyse temporelle ferme alors les appels ouverts comme partiels. La couverture
ne porte que sur les plages tracées (le reste n'est pas compté « jamais »).

| ETM | Moyens | Limites |
|---|---|---|
| ETMv4 (M7) | comparateurs d'adresse de l'ETM (une paire par plage, un par déclencheur) | nombre de paires lu dans TRCIDR4 (2 sur un ETM-M7) |
| ETMv3.5 (M3/M4) | l'ETM n'a pas de comparateur : il prend ceux du DWT (déclenchement ETM sur égalité de PC), par le haut | 2 plages au plus (A OU B) ; une plage est un bloc aligné de 2^n octets qui englobe la plage demandée ; un arrêt demande un démarrage ; les comparateurs pris manquent aux watchpoints de gdb |

L'ETM simulé applique le même filtre effectif au chemin de QEMU. Validation : chemin QEMU
filtré → encodeur → décodeur, identique en ETMv3 et ETMv4 (bare et FreeRTOS) ; registres
vérifiés sur des cibles factices. À valider sur carte : le comportement réel des comparateurs.

**Comment le décodeur a été validé sans matériel.**
1. QEMU fournit le chemin réel instruction par instruction, exceptions comprises.
2. L'encodeur produit le flux ETMv3 correspondant (un atome par instruction, adresse
   seulement pour les branchements indirects et les exceptions, encodage alternatif des ETM-M).
3. Deux décodeurs indépendants reconstruisent le chemin : celui de gdbtrace et **OpenCSD**,
   le décodeur de référence d'ARM/Linaro (celui de perf).

Les deux retrouvent exactement le chemin de QEMU :
- firmware bare-metal : 604 706 instructions ;
- firmware FreeRTOS : 1 477 998 instructions, avec SVC, SysTick, PendSV et 678 exceptions
  enchaînées.

Ce test tourne dans la suite (`tools/build_opencsd.sh` construit OpenCSD dans `.cache/`). Il
reste à valider sur une vraie sonde de trace.

**ETMv4 (Cortex-M7).** Un atome ne désigne plus une instruction mais un point de passage
(branchement ou ISB) : le décodeur parcourt le code entre deux atomes. Une exception porte
l'adresse de retour préférée, qui borne les instructions exécutées avant l'interruption, puis
l'adresse du handler. La spéculation est gérée comme dans OpenCSD (profondeur 1 sur un
ETM-M7 ; le dernier élément est validé à l'arrêt de la cible). Même méthode de validation :
sur les chemins QEMU bare-metal et FreeRTOS, notre décodeur et OpenCSD retrouvent exactement
le chemin (0,3 à 0,6 bit par instruction). Puis sur matériel : 2 s capturées par SWO sur une
Nucleo-F746ZG à 216 MHz (`--etm-stall`), 1 961 SysTick et 1 962 retours, 0 débordement, et
GdbTrace et OpenCSD identiques sur 194 127 événements. Deux constats sur cette carte : les
premiers octets sortent avant que le formateur TPIU ne démarre (GdbTrace les ignore), et
OpenCSD 1.4.1 ne compte pas l'élément spéculatif annoncé par Trace Info (SPEC=1) : il déborde
à chaque synchronisation, GdbTrace non. Pas encore pris en charge : cycle-accurate et
horodatage en ETMv4, ETMv4 des ARMv8-M (M33, M55).

### Analyse temporelle (vue Timing)

L'équivalent de Trace.STATistic.Func et Trace.Chart.Func, calculé depuis la trace ETM :
- **Fonctions** : pour chaque fonction et chaque exception (`[SysTick]`, `[PendSV]`…), le
  nombre d'appels complets et partiels, le temps **brut** (de l'entrée à la sortie,
  interruptions déduites), le **net** (sans les appelés ni les interruptions), min / max /
  moyenne du brut, % du temps. Tableau triable, double-clic : la source ;
- **Chronogramme** : temps en x, profondeur d'appel en y, un rectangle par appel. Molette :
  zoom, glisser : déplacer, survol : durée et nom, clic : source. Les 7 entités au plus gros
  temps brut ont une couleur fixe (elle les suit d'une analyse à l'autre), les autres sont en
  gris « Autres ». Pointillés : appel coupé par le début ou la fin de la trace.

Unités : cycles ou µs (fréquence du cœur `--cpu-hz`, 25 MHz par défaut).

**D'où viennent les durées** (toujours indiqué en haut de la vue) :

| Source | Durées |
|---|---|
| ETM en mode *cycle-accurate* (`--etm-cycle-accurate`, ETMCR bit 12) | **mesurées** : chaque instruction porte ses cycles. Environ 8 fois plus de trace. |
| ETM simulé sous QEMU, ou ETM matériel sans cycle-accurate | **estimées** par un modèle de cycles Cortex-M3/M4 : ALU 1, LDR 2, STR 1, LDM/STM/PUSH/POP 1+N, branchement pris 1+P (P = 2), TBB/TBH 2+P, SDIV/UDIV 7, entrée d'exception 12, sortie 10, enchaînement 6. Ni attentes mémoire ni flash : un ordre de grandeur, pas une mesure. |

Le décodage cycle-accurate (formats de P-header, paquets de cycles, I-sync avec cycles) est
validé contre OpenCSD, cycle par cycle, sur les mêmes chemins QEMU que la trace ETM.

**Pile d'appels reconstruite.** Un appel (BL/BLX) empile son adresse de retour ; le cadre se
ferme quand l'exécution l'atteint. Branchement vers le début d'une fonction : appel terminal.
Une exception s'empile au-dessus du code interrompu, et sa durée est déduite des fonctions
interrompues. Identités vérifiées par les tests : Σ net = durée totale de la trace,
`delay()` a un brut constant une fois les interruptions déduites, brut = net + brut des
appelés.

**Sous RTOS.** Un retour d'exception qui ne reprend pas le code interrompu est un changement
de tâche : la pile de la tâche sortante est mise de côté. Elle est restaurée quand
l'exécution reprend après sa dernière instruction, et le temps passé dans les autres tâches
est déduit du brut, comme une interruption. Une fonction préemptée reste donc un seul appel,
dessiné en plusieurs morceaux. Limite : la trace d'instructions ne dit pas *quelle* tâche
reprend. Si plusieurs tâches sont arrêtées au même endroit avec des piles différentes, la
décision est reportée au premier retour. Si elle reste ambiguë, les morceaux sont comptés
comme appels partiels plutôt que d'être attribués au hasard (le net reste exact). Tracer
les changements de tâche par ITM lèverait cette limite (à faire).

### Couverture de code (vue Couverture)

**Principe.** GdbTrace (COVerage) ne recompile ni n'instrumente rien. La couverture est déduite de la **trace d'instructions** (ETM/PTM) du binaire livré, puis
rapportée aux lignes et aux fonctions par les informations de débogage. Le décodeur
reconstruit chaque instruction exécutée. Chaque instruction conditionnelle porte en plus son
issue : atome **E** (exécutée, ou branchement pris) ou **N** (condition fausse, ou
branchement non pris). On obtient ainsi une couverture **du code objet** :
- **instructions** (statement coverage) ;
- **branches** (decision coverage objet) : chaque branchement conditionnel doit avoir été
  vu **pris et non pris**. Un `if` toujours vrai dans les tests apparaît « partiel » même si
  toutes ses lignes ont été exécutées.

Pour une campagne longue, la trace est **streamée** vers le PC et ajoutée en continu à la
base (COVerage.ADD). On peut aussi remplir le tampon de trace de la puce ou de la sonde puis
l'ajouter. La base se cumule sur plusieurs tests et sessions (COVerage.SAVE / LOAD /Add) et
s'exporte en rapport. L'intérêt est double : on mesure exactement ce qui est livré, sans
modifier le timing, ce qu'exigent les normes de sûreté (DO-178C, ISO 26262) pour la
couverture structurelle. Il faut une cible et une sonde capables de trace d'instructions
(sonde de trace réelle, ou ETM simulé sous QEMU) :

| | |
|---|---|
| Base | compte, par adresse, les atomes E et N ; s'accumule d'exécution en exécution, jusqu'à **RàZ** |
| Instruction | `ok`, `pris` (branchement toujours pris), `non pris` (jamais pris), `cond. fausse` (instruction conditionnelle jamais exécutée), `jamais` |
| Ligne, fonction, fichier | **ok** (tout exécuté, chaque branchement des deux côtés), **partiel**, **jamais** ; % d'instructions, issues de branchements vues, nombre d'entrées |
| Code mesuré | instructions des fonctions de l'ELF, sans les *literal pools* (`$d`) ni les `nop` d'alignement ; les branchements d'un bloc IT sont conditionnels |
| Cumul | **Enregistrer** / **Charger** (fusion) d'une base JSON, refusée si le code a changé |
| Rapports | **HTML** autonome (résumé, fichiers et fonctions, source annotée, instructions en cause), **LCOV** `.info` (genhtml, GitLab, Jenkins, SonarQube…) |

Dans la GUI, la vue **Couverture** liste fichiers et fonctions avec leur état.
« Annoter Source et Désassemblage » ajoute ✔ / ½ / ✘ dans la marge de la Source. Dans le
Désassemblage, chaque instruction est colorée et l'issue manquante d'un branchement est
nommée.

En intégration continue, une campagne par script :

```bash
.venv/bin/python -m gdbtrace examples/firmware/demo.elf --etm-sim --batch \
    --script examples/scripts/coverage.py --coverage run1.json --coverage cov.html --coverage cov.info
.venv/bin/python -m gdbtrace examples/firmware/demo.elf --etm-sim --batch \
    --script examples/scripts/coverage.py --coverage-merge run1.json --coverage total.html  # cumul
```

Sur matériel, trois sources de trace d'instructions :
- **ETM par SWO**, avec la sonde de débogage d'une Nucleo (ST-Link) et OpenOCD :
  `--etm-config --etm-port swo --cpu-hz N` à la connexion, ou **Trace › ETM par SWO…**
  cible arrêtée. Sur une Nucleo-F746ZG à 216 MHz, ajouter `--etm-stall` : le cœur ralentit
  au débit du SWO au lieu de perdre de la trace (voir plus bas) ;
- une **sonde de trace parallèle** : `--etm-config --trace HOST:PORT` ;
- la **MTB** d'un Cortex-M0+ (`--mtb`, voir plus bas).

Le débit du port limite la durée d'une capture sans perte.

Tant qu'aucune instruction n'arrive, les vues Couverture et Trace disent pourquoi :
- pas de capture, avec la cause de l'échec de la mise en route ;
- sonde qui ne sert pas le SWO (`--remote` sans `--tcl`) ;
- flux sans formateur TPIU ;
- octets reçus mais aucun de l'ETM (ID 2) : ETM non programmé, ou `--cpu-hz` / débit faux.

Le journal de la mise en route s'affiche dans la console : registres, `tpiu enable`, port
TCP et débit tenable sans perte. `gdbtrace.cov.summary()` donne le même diagnostic.

Limites de l'ETM simulé : QEMU ne dit pas si la condition d'une instruction *non-branchement*
d'un bloc IT était vraie. Elle est comptée exécutée, alors qu'un vrai ETM donne l'atome exact.
Les branchements conditionnels, eux, sont exacts. Il s'agit de couverture du code objet. La couverture MC/DC (conditions d'une décision) demande une analyse du source,
hors de portée de la trace seule.

### ETM par SWO (sans sonde de trace)

La sonde de débogage d'une Nucleo (ST-Link) ne lit pas le port de trace parallèle, mais elle
capture le SWO. Avec `--etm-port swo`, le TPIU de la cible formate l'ETM (ID 2) et l'ITM
(ID 1) sur cette broche ; OpenOCD sert le flux en TCP et GdbTrace le décode comme une capture
parallèle. Mesuré sur les démos, l'ETMv3 produit 0,65 à 0,76 bit par instruction, soit
environ 1 bit par instruction sur le fil (codage UART 10 bits/octet, formateur 15/16). À
1,6 cycle par instruction, il faut ≈ 0,63 Mbit/s par MHz de cœur :

| Capture du SWO | Débit | Cœur tracé sans perte |
|---|---|---|
| ST-Link V2-1 (Nucleo-64/144 anciennes) | 2,25 Mbit/s | ≈ 3,5 MHz |
| STLINK-V3 (jusqu'à 24 MHz dans OpenOCD ; 12 à 16 constatés) | 12–24 Mbit/s | ≈ 20–35 MHz |
| Relais USART → USB d'une autre Nucleo (L072 : 4 Mbaud ; L476 : ~10 Mbaud mais USB FS à câbler) | 4–8 Mbit/s | ≈ 6–12 MHz |

Au-delà, la FIFO de l'ETM déborde : la trace a des trous (resynchronisés). La couverture
cumulée reste une borne basse juste ; Timing et Trace.List deviennent incomplets. Baisser
l'horloge du cœur pendant les tests, ou utiliser le port parallèle. `--etm-config` affiche la
fréquence tenable.

Une Nucleo comme sonde du **port parallèle** : celui-ci sort 4 bits sur les deux fronts de
TRACECLK (≈ 4 × HCLK bit/s). Aucun MCU ne l'échantillonne seul. Avec une bascule externe
(4 bits figés sur le front descendant), une interface parallèle sur un seul front (DCMI,
PSSI) reçoit 8 bits par front montant. Une F746 capte ainsi en rafale dans sa RAM (quelques
ms, sortie USB FS seulement) ; une N6 (USB HS à PHY intégré) pourrait streamer, mais c'est un
firmware à part entière. La solution mûre reste une sonde FPGA (ORBTrace Mini).

L'ETMv4 d'un Cortex-M7 est plus compact (un atome par branchement), mais un M7 exécute bien
plus d'instructions par seconde : sur une F746 à 216 MHz, le SWO d'un ST-LINK/V2-1 ne suit
que du code qui dort souvent. Sinon `--etm-stall` ralentit le cœur au débit de la trace.

### Trace MTB (Cortex-M0+)

Le Cortex-M0+ n'a ni ETM ni ITM/SWO. Sa trace d'instructions optionnelle, la **MTB** (Micro
Trace Buffer), écrit dans la SRAM de la puce un paquet de 8 octets à chaque changement non
séquentiel du PC : adresse source et destination, bit A (exception, ou PC modifié par le
débogueur), bit S (début de trace). Entre deux paquets l'exécution est séquentielle : avec le
code de l'ELF, GdbTrace retrouve chaque instruction et l'issue de chaque branchement
(non pris : traversé sans paquet). Trace.List, flot d'appels, couverture (instructions et
branches) et Timing (durées estimées par le modèle de cycles) fonctionnent comme avec l'ETM.

Le tampon est circulaire (2^n octets, aligné sur sa taille) : c'est un **enregistreur de
vol**, les derniers branchements avant l'arrêt. GdbTrace le lit à chaque arrêt, puis remet le
pointeur à zéro. Le firmware doit réserver le tampon, la MTB l'écrase :

```c
__attribute__((section(".mtb"), aligned(1024), used))
unsigned char __mtb_buffer__[1024];      /* 128 paquets ; section NOLOAD de l'édition de liens */
```

| Option | Effet |
|---|---|
| `--mtb [ADRESSE]` | active la MTB : registres trouvés par les tables ROM CoreSight (pièce 0x932), sinon aux adresses connues (SAM D21/L21 : 0x41006000, Kinetis KL : 0xF0000000), ou à ADRESSE |
| `--mtb-buffer ADRESSE:TAILLE` | tampon, à défaut du symbole `__mtb_buffer__` / `mtb_buffer` / `__MTB_BUFFER` |
| `--mtb-probe` | liste les composants CoreSight de la cible : pour savoir si la puce a une MTB |
| `--mtb-sim [TAILLE]` | QEMU : MTB simulée (taille du firmware, 1024 par défaut ; 0 = illimitée) |
| `--mtb-autohalt` | avec `--mtb` ou `--mtb-sim` : trace complète (voir ci-dessous) |

`examples/m0plus` compile la démo pour Cortex-M0+ (QEMU `-M microbit`, Cortex-M0) avec ce
tampon. Validation sans matériel : chemins réels de QEMU (Cortex-M3 bare-metal et FreeRTOS,
Cortex-M0+) → paquets MTB → décodage identique au chemin, issues des branchements comprises,
y compris exception prise au milieu d'un bloc IT, enchaînements et changements de tâche.

À savoir :
- **à valider sur carte** : la sémantique des paquets vient de la documentation publique
  (pas de MTB dans QEMU) ; un point n'est pas confirmé par écrit, l'adresse source d'une
  entrée d'exception (prise comme l'adresse de retour, instruction non exécutée). Le
  décodeur compte toute incohérence (`erreurs` dans la vue Trace) ;
- la MTB est optionnelle : les STM32L0 (L031, L072), à notre connaissance, n'en ont pas
  (`--mtb-probe` tranche) ; on la trouve sur SAM D21/L21, Kinetis KL, LPC800... ;
- pour la couverture, un enregistreur de vol ne garde que la fin de chaque exécution : voir
  le mode AUTOHALT.

**Mode AUTOHALT (trace complète).** Avec `--mtb-autohalt`, GdbTrace programme un seuil
(`FLOW.WATERMARK`, fin du tampon moins une marge de 4 paquets) et `FLOW.AUTOHALT`. Quand le
tampon est presque plein, la MTB arrête elle-même le cœur (`MASTER.HALTREQ`). GdbTrace vide
alors le tampon, efface HALTREQ, remet le pointeur à zéro et relance aussitôt. Ces arrêts sont
**internes** : la cible reste « en marche » pour l'interface, les vues et `wait()` des
scripts ; Break et les points d'arrêt fonctionnent normalement. On obtient ainsi toute
l'exécution : Trace.List complète, couverture des instructions et des branches.

Le prix : un arrêt par tampon plein, soit une dizaine d'échanges avec la sonde (20 à 40 ms).
La cible tourne donc beaucoup plus lentement : à réserver aux campagnes de couverture, avec
un tampon de 4 à 8 Ko (512 à 1024 branchements par arrêt) plutôt que 1 Ko. Un pas à pas qui
traverse un tampon plein s'arrête au seuil (le relancer en Go perdrait le pas).
`gdbtrace.mtb_stats()` donne le nombre d'arrêts, de débordements (marge insuffisante) et
d'erreurs.

En simulation (`--mtb-sim N --mtb-autohalt`), un fil arrête QEMU régulièrement (commande QMP
`stop`, vue par gdb comme l'arrêt non demandé qu'enverrait la MTB) : tout le mécanisme
d'arrêts internes est éprouvé avec le vrai gdb. La conversion des tronçons de journal QEMU,
coûteuse, est faite à l'arrêt visible (sur carte, vider la MTB est léger). Validé : nombre
d'instructions décodées égal à celui exécuté par QEMU, couverture identique à un tampon
illimité. À confirmer sur carte : le signal d'arrêt que rapporte la sonde (SIGINT attendu
avec OpenOCD, comme un Break).

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
| `gdbtrace.program("fw.elf")`, `gdbtrace.program()` | FLASH.ReProgram : écrit, vérifie et reset (ELF courant par défaut) |
| `gdbtrace.timing(20, unit="us")`, `gdbtrace.timeline(30)` | durées par fonction / derniers appels (trace ETM) |
| `!info breakpoints` | Commande CLI gdb brute |
| `mon reset halt` | Commande `monitor` vers OpenOCD |

**Notice complète du scripting Python (débogage programmatique) : [`docs/scripting.md`](docs/scripting.md)** — lancement, run-control, breakpoints, données, cible en marche, trace, recettes (CI, journal CSV, injection de fautes), référence de l'API. Exemples dans `examples/scripts/` :
`session.py` (session de débogage complète : programmation, breakpoints simple, temporaire,
conditionnel et matériel, watchpoint, Watch avec formats, pas à pas, lecture et modification
de variables, lectures cible en marche), `demo.py`, `rtos.py` (FreeRTOS), `coverage.py`.

```bash
.venv/bin/python -m gdbtrace examples/firmware/demo.elf --script examples/scripts/session.py
.venv/bin/python -m gdbtrace examples/firmware/demo.elf --batch --script examples/scripts/session.py
```

## Architecture

```
gdbtrace/
  core/gdb_backend.py   pilote GDB/MI asynchrone (thread lecteur, jetons, événements)
  core/session.py       API haut niveau (run-control, mémoire, registres, varobjs ; breakpoints
                        à action et à compteur, Go.Till, Step.Till / Step.Change)
  core/target.py        serveurs gdb : QEMU, OpenOCD, distant
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
  trace/config.py       programmation ITM/DWT/TPIU, commandes TPIU d'OpenOCD
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
  trace/mtb.py          MTB des Cortex-M0+ : paquets, registres, tables ROM, décodeur, encodeur
  trace/mtb_capture.py  capture MTB (sonde, à chaque arrêt) et MTB simulée sous QEMU
  trace/etm/config.py   programmation ETM + TPIU (port parallèle ou SWO)
  trace/etm/cycles.py   modèle de cycles Cortex-M3/M4 (durées estimées)
  trace/timing.py       analyse temporelle : pile d'appels, durées, chronogramme
  core/scripting.py     API `gdbtrace` + console Python
  gui/                  fenêtre tkinter, vues détachables, pompe d'événements thread-safe
  views/                Fichiers, Source, Désassemblage, Registres, Mémoire, Watch, Périphériques, RTOS,
                        Pile, Breakpoints, Console, ITM, Profil, Trace, Timing, Couverture
examples/
  firmware/             firmware de démo Cortex-M3 (SysTick 1 kHz, timer CMSDK)
  freertos/             firmware de démo FreeRTOS V11.3.3 (noyau MIT inclus) : 7 tâches,
                        queue, mutex, sémaphore, timer logiciel
  m0plus/               la démo compilée pour Cortex-M0+ (QEMU microbit), tampon MTB réservé
  svd/mps2-an385.svd    SVD de démo (SysTick, SCB, TIMER0/1, UART0)
  scripts/demo.py       exemple de script
  scripts/rtos.py       exemple de script FreeRTOS
```

tkinter n'est pas thread-safe. Le lecteur gdb, la console et les actions longues tournent donc dans
des threads qui passent par `EventPump.post()`, et la boucle tkinter exécute ces appels via `after()`.

## Tests

```bash
xvfb-run -a .venv/bin/python -m pytest
```

Les tests lancent QEMU (`mps2-an385`) avec le firmware de démo. Sans `DISPLAY`, les tests GUI sont ignorés.
Le client TCL OpenOCD est testé contre un faux serveur TCL, et contre un vrai OpenOCD lancé sans
sonde (`noinit`) s'il est installé ; il a été utilisé sur une Nucleo-F746ZG. Restent à valider sur
sonde réelle le support RTOS d'OpenOCD (`--rtos`), la capture SWO et `DWT_PCSR` via OpenOCD.
Les tests ETM comparent notre décodeur à OpenCSD si `tools/build_opencsd.sh` a été lancé
(sinon ce test est ignoré) ; la trace ETM nécessite `pip install capstone` (extra `etm`).
