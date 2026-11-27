Cible arrêtée, toutes les données passent par gdb. Cible en marche, GdbTrace les lit par un canal mémoire à part, sans arrêter le cœur, quand le serveur en offre un. Un double-clic sur une variable Watch, un registre ou un octet mémoire permet d'en modifier la valeur.

## Watch et Pile

- **Watch** : saisir une expression C puis **Ajouter**. Les structures et tableaux se déplient. Une expression ajoutée cible en marche est évaluée à l'arrêt suivant ; une locale hors de portée est réévaluée à chaque arrêt.
- **Pile** : la pile d'appels et les variables locales de la frame choisie.
- **Format d'affichage** : clic droit sur une variable (Watch ou locale) › Naturel, Décimal, Hexadécimal, Octal, Binaire ou ASCII.
  - Le format d'une structure ou d'un tableau vaut pour tous ses membres ; un membre peut avoir le sien.
  - Le format reste appliqué cible en marche et il est gardé dans le layout ; celui d'une locale est gardé par fonction.
  - Un flottant en hexadécimal, décimal, octal ou binaire montre ses bits bruts (`21.5` → `0x41ac0000`).
  - Le binaire couvre toute la largeur du type, par groupes de 4 (`0b1111_1111_1111_1101`).
  - L'ASCII lit les octets dans l'ordre de la mémoire (`'AB\0\0'`).

## Registres et Mémoire

- **Registres** : les registres du cœur en hexadécimal et en décimal, avec le décodage de xPSR ; tous les autres sur demande. Les registres modifiés depuis l'arrêt précédent sont en rouge.
- **Mémoire** : un dump hexadécimal et ASCII à partir d'une adresse ou d'un symbole. Elle part de 0x20000000 (la SRAM d'un Cortex-M3/M4/M7). Pour un programme natif, ou un firmware dont la RAM est ailleurs (STM32N6), elle part de sa première variable globale.

## Outils mémoire

Le menu **Outils** de la vue Mémoire travaille sur une plage « adresse taille » (une variable seule : sa taille) :

| Outil | Effet | Cible |
| --- | --- | --- |
| Enregistrer la plage… (Data.SAVE) | écrit la plage en binaire (`.bin`), Intel HEX (`.hex`) ou S-record (`.srec`) | arrêtée, ou en marche par le canal mémoire |
| Charger une image en RAM… (Data.LOAD) | écrit une image HEX, S-record, ELF, ou un binaire à l'adresse demandée | arrêtée |
| Remplir la plage… (Data.Set) | motif répété : octets (`de ad be ef`), nombre (`0x1234`, petit-boutiste), texte (`"abc"`) | arrêtée |
| Chercher… (Data.Find) | les adresses du motif ; la vue saute à la première | arrêtée, ou en marche |
| Comparer à une image… (Data.ComPare) | octets différents entre une image et la mémoire ; la vue saute au premier | arrêtée, ou en marche |

La flash ne s'écrit pas ainsi : elle se programme ([Lancer GdbTrace](Lancer-GdbTrace)). Dans un script : `mem_save`, `mem_load`, `mem_fill`, `mem_find`, `mem_compare`.

## Périphériques

La vue **Périphériques** décode les registres et leurs champs, avec leurs énumérations, d'après un fichier CMSIS-SVD (`--svd`, ou Fichier › Charger un SVD…). Le fichier est lu en tâche de fond : celui d'un STM32N6 fait 22 Mo et demande environ une seconde, pendant laquelle l'interface reste utilisable.

- Seuls les périphériques dépliés sont lus, par blocs de registres contigus ; les trous réservés ne sont jamais lus.
- Les registres en écriture seule ou à lecture destructive (ex. le registre de données d'un UART) ne sont pas lus d'office ; clic droit › **Lire quand même**.
- Double-clic : écriture d'un registre, ou d'un champ par lecture-modification-écriture. Le registre est relu ensuite, et un écart est signalé.

## Système : CoreSight, MPU, puce

La vue **Système** dit ce que la puce a vraiment, en trois onglets.

- **CoreSight** : les tables ROM CoreSight, lues à la connexion (**Relire les tables ROM** :
  à la demande). Arbre des tables et de leurs composants : type (SCS, DWT, FPB, ITM, TPIU, ETM
  et sa version, CTI, MTB, PMU, ETF, ETR…), pièce, fabricant, révision, adresse.
  - L'ETM de la table donne sa version au décodeur (ETMv3 ou ETMv4).
  - Une puce dont la table n'a pas d'ETM refuse toute capture ETM en le disant (Trace,
    Couverture, Trace › ETM par SWO…).
  - QEMU n'a pas de table ROM : « pas de table ROM ».
- **MPU** : les régions de la MPU, relues à chaque arrêt : active, début, fin, taille,
  sous-régions désactivées, accès privilégié et non privilégié, XN, type mémoire. ARMv7-M et
  ARMv6-M : TEX/S/C/B (fortement ordonné, Device, Normal WT/WB…) ; ARMv8-M : attribut MAIR
  (Device-nGnRE, Normal WB RWA…). Le résumé dit si la MPU est activée (PRIVDEFENA, HFNMIENA).
  Une MemManage cite, dans la vue Faute, la région qui couvre l'adresse fautive.
- **Puce** : la puce du CMSIS-Pack (`--device`), et ce que la cible répond (cœur par CPUID,
  conforme ou non au pack ; ETM de la table ROM).

Démo : `examples/faults` pose ses régions MPU au démarrage. Sa faute 7 écrit dans une garde de
32 octets et donne une MemManage. `faults-m55.elf` (QEMU `--machine mps3-an547`) fait de même
avec la MPU ARMv8-M d'un Cortex-M55.

## Cible en marche

| Cible | Canal en marche |
| --- | --- |
| OpenOCD (`--openocd`, ou `--remote` + `--tcl`) | port TCL d'OpenOCD, lecture par l'AHB-AP sans arrêter le cœur |
| ST-LINK_gdbserver (`--stlink`) | le relais de GdbTrace glisse ses lectures entre les paquets de gdb (environ 1 ms par lecture) |
| QEMU | moniteur QMP |
| Programme de l'hôte | `/proc/<pid>/mem` |
| Autre serveur (`--remote` seul) | aucun : vues figées en marche, rafraîchies à l'arrêt |

Le voyant **LIVE** est vert quand le canal est disponible. Chaque vue a une case « Live », et un seul fil regroupe les lectures de toutes les vues (toutes les 200 ms).

- **Mémoire** suit la cible ; les octets modifiés sont en rouge.
- **Watch** décode en marche les globales et les statiques : entiers, `char`, `bool`, flottants, pointeurs, énumérations. Les locales, les champs de bits et les objets de plus de 4 Kio restent grisés, avec leur valeur du dernier arrêt.
- **Périphériques** et **RTOS** se rafraîchissent aussi en marche.

## Fautes (vue Faute)

Arrêtée dans un gestionnaire de faute (HardFault, MemManage, BusFault, UsageFault, SecureFault), la cible est analysée : la barre d'état et la console disent la cause, et la vue **Faute** passe au premier plan.

- **Causes en clair**, d'après le CFSR, le HFSR et, en ARMv8-M, le SFSR. Pour une faute montée en HardFault (`FORCED`), la cause d'origine est donnée : « HardFault (escaladée) : instruction indéfinie ».
- **Adresse fautive** (BFAR, MMFAR, SFAR), quand le cœur l'a enregistrée.
- **Instruction fautive**, sa fonction et sa ligne (le PC empilé) ; **Aller à la ligne fautive** ouvre la source. Pour un saut vers une adresse paire, la vue dit d'où venait l'appel.
- **Cadre empilé** (r0-r3, r12, lr, pc, xPSR) et EXC_RETURN : mode, pile (MSP ou PSP), contexte flottant. La vue Pile déroule aussi à travers l'exception (« signal handler called »).

Un Cortex-M0 ou M0+ n'a pas de registre de cause : tout monte en HardFault. La cause est alors **déduite** de l'instruction fautive et du cadre : bit Thumb à 0, instruction indéfinie, accès non aligné ou à une adresse sans mémoire (adresse calculée avec les registres de l'instant).

**Arrêt sur faute** (Run › Arrêt sur faute, case de la vue, `--break-on-fault`) : la cible s'arrête à l'entrée de toute faute, avant que le gestionnaire ne touche à rien. GdbTrace se sert du vector catch du cœur (DEMCR, ou `cortex_m vector_catch` d'OpenOCD) ; QEMU ne l'émule pas, et GdbTrace pose alors des breakpoints sur les gestionnaires de la table des vecteurs.

Démo : `examples/faults` (`make -C examples/faults`), un Cortex-M3 sous `mps2-an385` et un Cortex-M0 sous `microbit` ; écrire 1 à 6 dans `fault_kind` déclenche une faute différente. Dans un script : `gdbtrace.fault()`.

## Pour aller plus loin

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

Limite connue de QEMU 8.2 : son gdbstub **ignore les écritures du débogueur dans les
périphériques** (seules la RAM et la ROM sont écrites ; QEMU 11.1 les applique). gdbtrace le détecte à la relecture et l'affiche. Sur une
vraie sonde, les écritures fonctionnent.

### Fautes, mémoire, symboles, terminal (jalon 19)

- **Vue Faute** : arrêtée dans HardFault, BusFault…, la cible est analysée (cause en clair,
  adresse fautive, instruction et ligne fautives, cadre empilé, EXC_RETURN). Un Cortex-M0/M0+
  n'a pas de CFSR : la cause est déduite de l'instruction fautive. **Run › Arrêt sur faute**
  (`--break-on-fault`) arrête la cible à l'entrée de toute faute (vector catch ; sous QEMU,
  breakpoints sur les gestionnaires). Démo : `examples/faults`.
- **Outils mémoire** (menu Outils de la vue Mémoire, `mem_save`, `mem_load`, `mem_fill`,
  `mem_find`, `mem_compare`) : Data.SAVE en binaire, Intel HEX ou S-record, Data.LOAD,
  Data.Set, Data.Find, Data.ComPare.
- **Images** : `--program fw.hex` (ou `.srec`, `fw.bin@0x08000000`) programme une image, les
  symboles restant ceux de l'ELF ; `--add-symbols boot.elf[@DÉCALAGE]` (Fichier › Ajouter des
  symboles…) ajoute ceux d'un autre ELF, que voient aussi profil, trace et couverture.
- **Vue Symboles** (sYmbol.Browse, `sym_list`) : fonctions et variables filtrées par nom.
- **Vue Terminal** : semihosting servi par QEMU ou OpenOCD (`--semihosting`), RTT lu en marche
  par le canal mémoire (`--rtt`, bloc d'exemple `examples/common/gdbtrace_rtt.c`).
- **Trace** : Chercher (adresse, fonction, exception, texte), Tout trouver, Exporter… en CSV
  (`trace_find`, `trace_export`).

### Toutes les puces Cortex-M (jalon 22)

- **Vue Système**, onglet CoreSight (`coresight()`) : les tables ROM CoreSight de la puce (SCS,
  DWT, FPB, ITM, TPIU, ETM et sa version, CTI, MTB, PMU, ETF/ETR) ; l'ETM listé règle le
  décodeur, une puce sans ETM refuse la trace d'instructions en le disant.
- **Onglet MPU** (`mpu()`) : régions décodées, ARMv7-M (TEX/C/B, sous-régions) et ARMv8-M
  (MAIR) ; une MemManage cite sa région dans la vue Faute. Démo : `examples/faults` (faute 7,
  `faults-m55.elf` sous QEMU `mps3-an547`).
- **Chargeur de flash générique** (`--flash-loader`, `--flash-ram`, `flash_loader()`) : FLM des
  CMSIS-Packs et chargeurs externes `.stldr` de STM32CubeProgrammer exécutés sur la cible par
  gdb, quel que soit le serveur ; flash externe QSPI/XSPI comprise. Algorithmes de test :
  `examples/flashalgo`.
- **CMSIS-Pack** (`--device`, `--pack`, onglet Puce, `chip_info()`) : SVD, mémoires,
  algorithmes de flash (`--flash-loader pack`) et cœur attendu de la puce ; packs installés par
  cmsis-toolbox ou pyOCD.
