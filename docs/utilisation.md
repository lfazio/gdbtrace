# GdbTrace : notice d'utilisation

Comment installer, lancer et utiliser GdbTrace : interface, breakpoints, données, RTOS, trace, carte de référence, options. Le pilotage en Python (console, scripts, mode batch) a sa propre notice : [`docs/scripting.md`](scripting.md).

**Sommaire**

1. [Présentation et installation](#1-présentation-et-installation)
2. [Lancer GdbTrace](#2-lancer-gdbtrace)
3. [L'interface](#3-linterface)
4. [Breakpoints, Go.Till et Step.Change](#4-breakpoints-gotill-et-stepchange)
5. [Variables, mémoire, registres et périphériques](#5-variables-mémoire-registres-et-périphériques)
6. [RTOS et threads](#6-rtos-et-threads)
7. [Trace, profil et couverture](#7-trace-profil-et-couverture)
8. [Sur une Nucleo-F746ZG](#8-sur-une-nucleo-f746zg)
9. [Sur une Nucleo-N657X0-Q (STM32N6)](#9-sur-une-nucleo-n657x0-q-stm32n6)
10. [Sur une Nucleo-G070RB ou une Nucleo-F072RB (Cortex-M0+, Cortex-M0)](#10-sur-une-nucleo-g070rb-ou-une-nucleo-f072rb-cortex-m0-cortex-m0)
11. [Console et scripts Python](#11-console-et-scripts-python)
12. [Référence des options](#12-référence-des-options)
13. [Dépannage et limites](#13-dépannage-et-limites)

---

## 1. Présentation et installation

GdbTrace est un débogueur pour microcontrôleurs Cortex-M, avec trace. Il est construit sur gdb (GDB/MI) et a une interface graphique tkinter. Le même outil débogue aussi un programme de l'hôte (x86-64, AArch64…).

Il couvre trois usages :

- **le débogage classique** : run-control, breakpoints, variables, mémoire, registres, périphériques, RTOS ;
- **la trace**, pour voir ce qui s'est passé avant l'arrêt et en mesurer le temps : ITM/SWO, profil, ETM, MTB, couverture de code ;
- **le débogage programmatique**, en Python, depuis la console ou en batch.

### Cibles prises en charge

| Cible | Serveur gdb | Option | Remarques |
| --- | --- | --- | --- |
| Cortex-M sans matériel | QEMU (`mps2-an385` par défaut, `microbit` pour un M0+, `mps2-an500` pour un M7) | aucune (défaut) | GdbTrace lance QEMU et y charge l'ELF ; ETM ou MTB simulés possibles |
| Carte avec sonde | OpenOCD, lancé par GdbTrace | `--openocd fichier.cfg` | flash, live, SWO, ETM réels |
| Carte STM32 avec sonde ST-LINK | ST-LINK_gdbserver (STM32CubeIDE, STM32CubeCLT), lancé par GdbTrace | `--stlink [numéro de série]` | flash ou RAM (`--ram-boot`), live, SWO ; puces qu'OpenOCD ne connaît pas (STM32N6) |
| Serveur déjà lancé | J-Link GDB Server, pyOCD, OpenOCD… | `--remote hôte:port` (+ `--tcl` pour OpenOCD) | GdbTrace s'y connecte |
| Programme de l'hôte | gdb local | aucune : l'ELF est reconnu | pthreads, arguments avec `--args` ; `--pid` pour un processus déjà lancé ; `--non-stop` |
| Programme de l'hôte servi par gdbserver | gdbserver (`--multi`, ou avec le programme) | `--remote hôte:port` | lancé ou repris par GdbTrace, arrêt au début de `main` |

### Installation

```bash
sudo apt install python3-tk gdb-multiarch openocd qemu-system-arm gcc-arm-none-eabi
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
```

L'extra `dev` installe pytest et capstone, nécessaire au décodage de la trace ETM. Les outils externes ne servent qu'à ce qui les demande :

| Outil | Sert à |
| --- | --- |
| `gdb-multiarch` | tout : c'est le moteur de débogage |
| `qemu-system-arm` | déboguer sans carte, ETM et MTB simulés |
| `openocd` | sondes ST-Link, CMSIS-DAP… ; canal mémoire en marche ; capture SWO |
| STM32CubeIDE ou STM32CubeCLT | `--stlink` : ST-LINK_gdbserver et le STM32CubeProgrammer dont il a besoin, cherchés dans le PATH puis sous `/opt/st` et `~/st` |
| `arm-none-eabi-gcc` | compiler les firmwares d'exemple (`examples/`) |
| `xvfb-run` | lancer les tests de l'interface sans écran |

Pour un premier essai sans matériel :

```bash
make -C examples/firmware
.venv/bin/python -m gdbtrace examples/firmware/demo.elf
```

## 2. Lancer GdbTrace

GdbTrace se lance par `python -m gdbtrace`, suivi de l'ELF à déboguer puis des options. Sans option de cible, un ELF ARM part sous QEMU et un ELF de l'hôte est exécuté par gdb.

```bash
.venv/bin/python -m gdbtrace examples/firmware/demo.elf                    # QEMU mps2-an385
.venv/bin/python -m gdbtrace fw.elf --openocd board/st_nucleo_f7.cfg       # sonde, OpenOCD lancé par GdbTrace
.venv/bin/python -m gdbtrace fw.elf --stlink                               # sonde ST-LINK, ST-LINK_gdbserver
.venv/bin/python -m gdbtrace fw.elf --remote 127.0.0.1:2331                # serveur gdb déjà lancé
.venv/bin/python -m gdbtrace fw.elf --remote 127.0.0.1:3333 --tcl 127.0.0.1:6666   # OpenOCD lancé à part
.venv/bin/python -m gdbtrace examples/native/pthreads --args 100000       # programme de l'hôte
```

### Choisir la cible

| Cible | Option | Ce que fait GdbTrace |
| --- | --- | --- |
| QEMU | `--qemu` (défaut), `--machine` pour une autre carte | lance QEMU, y charge l'ELF, ouvre le canal QMP pour lire la mémoire en marche |
| Sonde avec OpenOCD | `--openocd f1.cfg f2.cfg…` | lance OpenOCD avec ces fichiers `-f`, flashe l'ELF (sauf `--no-load`), utilise son port TCL pour la mémoire en marche |
| Sonde ST-LINK avec ST-LINK_gdbserver | `--stlink [SN]`, `--stlink-ap N`, `--ram-boot` | lance ST-LINK_gdbserver sur la sonde SN (à donner si plusieurs sondes sont branchées), sur le port d'accès N du cœur ; gdb passe par un relais de GdbTrace qui lit la mémoire en marche et fait le reset. `--ram-boot` : image exécutée en RAM (section 9) |
| Serveur gdb déjà lancé | `--remote hôte:port` | s'y connecte ; ajouter `--tcl hôte:port` si c'est un OpenOCD, pour la mémoire en marche |
| Programme de l'hôte | `--native` (implicite pour un ELF non ARM) | gdb l'exécute et l'arrête au début de `main` ; `--args` et `--cwd` règlent ses arguments et son répertoire |
| Processus de l'hôte déjà lancé | `--pid PID` (ELF : `/proc/PID/exe` par défaut) | gdb s'y attache et l'arrête là où il en est ; à la fin, il est détaché et continue |
| Programme de l'hôte servi par gdbserver | `--remote hôte:port` avec un ELF de l'hôte | `gdbserver --multi` : GdbTrace lance le programme (`--remote-exec` : son chemin côté serveur) ; `gdbserver hôte:port prog` : déjà chargé ; arrêt au début de `main` |

Les serveurs lancés par GdbTrace (QEMU, OpenOCD, ST-LINK_gdbserver) s'arrêtent avec lui, même s'il est tué brutalement : aucun serveur orphelin ne garde la sonde.

### Configurations (menu Configuration)

Une **configuration** décrit toute une session dans un fichier YAML : la cible, le serveur gdb, l'OS, le script et la couverture, la trace. Elle se traduit en options de la ligne de commande : ce qu'elle permet, et ses erreurs, sont ceux de la ligne de commande.

| Entrée du menu Configuration | Effet |
| --- | --- |
| Nouvelle… | ouvre la boîte de dialogue sur une configuration vide |
| Ouvrir… | charge un fichier `.yaml` : titre, SVD, réglages, vues selon l'OS, **sans connecter** |
| Récentes | les 8 dernières configurations ouvertes ou enregistrées |
| Modifier… | ouvre la boîte de dialogue sur la configuration courante |
| Enregistrer (`Ctrl+S`), Enregistrer sous… | écrit le fichier YAML |
| Connecter | lance la session décrite ; une session ouverte est d'abord déconnectée |
| Fermer | déconnecte, retire la configuration et son SVD, rouvre les vues de l'OS |

La boîte de dialogue a cinq onglets :

- **Cible** : nom, chemin de l'ELF, SVD (un fichier, ou un nom seul cherché dans STM32CubeIDE), fréquence du cœur, programmer l'ELF à la connexion, vérifier, Go après la connexion.
- **Serveur** : QEMU (machine), OpenOCD (fichiers `.cfg`, tâches RTOS d'OpenOCD), ST-LINK_gdbserver de STMicroelectronics (numéro de série, port d'accès, image en RAM), gdb pour un programme de l'hôte (arguments, répertoire), ou serveur déjà lancé (hôte:port, port TCL) ; l'exécutable gdb et le port.
- **OS** : Aucun, FreeRTOS ou Pthread ; « Détecter dans l'ELF » le déduit des symboles.
- **Trace** : SWO/ITM, ETM (sortie, stall, cycle-accurate, tampon de l'ETR), variables tracées, filtre ETM, simulation QEMU, MTB.
- **Script et couverture** : le script à exécuter après la connexion, les couvertures à fusionner au départ, les fichiers de couverture écrits à la fin du script.

OK valide la configuration ; une erreur s'affiche en bas de la boîte, qui reste ouverte. Une configuration modifiée et pas enregistrée est proposée à l'enregistrement avant Ouvrir, Nouvelle, Fermer ou Quitter. Le titre de la fenêtre porte son nom, suivi de « * » si elle est modifiée.

**Les vues suivent l'OS** : FreeRTOS ouvre les vues RTOS et Temps des tâches ; Pthread ouvre la vue Threads ; aucun OS n'ouvre aucune des trois. Sans configuration, toutes les vues du layout restent.

En ligne de commande, `--config FICHIER.yaml` lance la session de la configuration. Les autres options l'emportent sur elle : une valeur remplace la sienne, une liste (`--var-trace`, `--coverage`…) s'ajoute à la sienne, un serveur d'un autre type remplace le sien. Exemples dans `configs/` : `qemu-demo.yaml`, `qemu-freertos.yaml`, `native-pthreads.yaml`, et les sessions des cartes de test (sections 8 à 10). Le format est décrit dans la section 12.

```bash
.venv/bin/python -m gdbtrace --config configs/qemu-freertos.yaml
.venv/bin/python -m gdbtrace --config configs/nucleo-n657x0-q.yaml --stlink 0042001A3133510F37363734
```

### Programmer la cible

**Fichier › Programmer un ELF…** (`Ctrl+O`) arrête la cible, la remet à zéro, écrit l'ELF (en flash avec une sonde, en RAM sous QEMU ou avec `--ram-boot`), le vérifie, puis refait un reset. La cible reste arrêtée au vecteur de reset ; symboles, vues et trace passent au nouveau programme.

La vérification compare les sections par CRC (`compare-sections`). Avec ST-LINK_gdbserver, elle relit les sections et les compare à l'ELF : le CRC de ce serveur est faux sur un STM32N6.

- **Reprogrammer l'ELF courant** (`Ctrl+R`) : après une recompilation.
- **ELF récents** : les 8 derniers.
- Sans ELF sur la ligne de commande, l'interface démarre sans cible : choisir l'ELF lance la cible puis la programme.
- Pendant la programmation, Go, Break, les pas et les réglages de trace sont refusés jusqu'à la fin.

| Option | Effet |
| --- | --- |
| `--program [ELF]` | programme l'ELF à la connexion |
| `--no-verify` | sans vérification |
| `--go` | lance le programme ensuite |

### Sans interface : le mode batch

`--batch` exécute un script (`--script s.py`) puis quitte, sans fenêtre. Le code de retour vaut 0 si tout s'est bien passé, 1 sinon ; c'est la forme pour l'intégration continue. Sans `--script`, `--batch --program` flashe la cible et quitte.

```bash
.venv/bin/python -m gdbtrace --program build/fw.elf --openocd board/st_nucleo_f7.cfg --batch --go
.venv/bin/python -m gdbtrace fw.elf --batch --script tests/sur_cible.py
```

## 3. L'interface

La fenêtre principale range 19 vues en quatre zones à onglets, sous un menu, une barre d'outils et au-dessus d'une barre d'état. Chaque vue se détache ou se déplace, et la disposition est gardée d'une session à l'autre.

### Run-control

| Commande | Raccourci | Bouton | Effet |
| --- | --- | --- | --- |
| Go | `F5` | ▶ Go | lance la cible |
| Break | `F8` | ■ Break | arrête la cible |
| Step | `F11` | ↓ Step | une ligne source, en entrant dans les fonctions |
| Step Over | `F10` | → Over | une ligne source, par-dessus les appels |
| Step Out (Go.Return) | `Shift+F11` | ↑ Out | jusqu'au retour de la fonction |
| Step instruction | `F7` | ⇣ Asm | une instruction |
| Reset | — | ⟲ Reset | reset de la cible, arrêtée au vecteur de reset (programme natif : relancé au début de `main`) |
| Go.Till, Step.Till / Step.Change | menu Run | — | voir la section 4 |

### Menus

- **Fichier** : Programmer un ELF… (`Ctrl+O`), Reprogrammer l'ELF courant (`Ctrl+R`), ELF récents, Exécuter un script…, Charger un SVD…, Enregistrer le layout, Déconnecter, Quitter.
- **Configuration** : Nouvelle…, Ouvrir…, Récentes, Modifier…, Enregistrer (`Ctrl+S`), Enregistrer sous…, Connecter, Fermer (section 2).
- **Run** : les commandes ci-dessus.
- **Trace** : ETM par SWO…, ETM par l'ETR (RAM, STM32N6), Filtre de trace ETM…, Arrêter la capture.
- **Vues** : ouvre n'importe quelle vue dans une nouvelle fenêtre.

### Vues

| Zone | Vues |
| --- | --- |
| Gauche | Fichiers |
| Code | Source, Désassemblage, Trace, Timing, Couverture |
| Données | Registres, Watch, Périphériques, RTOS, Threads, Pile, Breakpoints |
| Bas | Console, Mémoire, ITM, Profil, Variables tracées, Temps des tâches |

Chaque zone est un jeu d'onglets ; le menu Vues ouvre n'importe quelle vue dans une fenêtre à part.

- **Fichiers** : les sources du programme par dossier, dépliées sur leurs fonctions ; le fichier où s'est arrêté le PC est en gras. Double-clic : la source s'ouvre.
- **Source** et **Désassemblage** suivent le PC. Le désassemblage intercale chaque ligne C avant ses instructions (source mêlée). Un double-clic dans la marge pose ou retire un breakpoint.
- Les autres vues sont décrites dans les sections 4 à 7.

### Barre d'état

Elle montre de gauche à droite :

- l'état de la cible : ARRÊTÉ, RUNNING, TERMINÉ (programme natif fini) ou DÉCONNECTÉ ;
- le voyant LIVE, allumé quand des vues se rafraîchissent cible en marche ;
- la tâche RTOS courante ;
- le dernier message : raison de l'arrêt, bilan d'un Step.Change, erreur.

### Disposition

- Clic droit sur un onglet : **Détacher** la vue dans une fenêtre flottante, ou la **Déplacer** vers une autre zone.
- **Fichier › Enregistrer le layout** écrit `gdbtrace-layout.json` ; `--layout` en choisit un autre. Les vues apparues depuis la sauvegarde prennent leur place par défaut.

## 4. Breakpoints, Go.Till et Step.Change

Un breakpoint se pose d'un double-clic dans la marge de la vue Source ou Désassemblage, ou dans la vue Breakpoints. Il peut s'arrêter sous condition, au Nᵉ passage, ou exécuter une action sans arrêter la cible.

### Poser un breakpoint

- **Marge de la vue Source** : breakpoint sur la ligne C.
- **Marge du Désassemblage** : sur une ligne C, breakpoint source ; sur une instruction, breakpoint à l'adresse.
- **Vue Breakpoints** : un emplacement comme dans gdb (`update_sensor`, `main.c:59`, `*0x080001f4`), puis **Ajouter**. Cocher **Matériel** pour un comparateur du FPB, indispensable en flash quand le serveur ne sait pas y écrire. **Watchpoint** surveille une variable en écriture.

Breakpoints matériels et watchpoints sont en nombre limité : typiquement 6 et 4 sur un Cortex-M3/M4, 8 et 4 sur un M7, 4 et 2 sur un M0+.

### Vue Breakpoints

Chaque ligne donne le numéro, le type, l'adresse, le nombre de passages (Hits), l'emplacement et une colonne **Condition / action**, par exemple `si tick > 2 · au 5ᵉ passage (reste 3) · « n={counter} » → continue`.

- **On/Off** active ou désactive sans supprimer ; **Supprimer** retire.
- Double-clic : la source s'ouvre sur le breakpoint.
- **Options…** ou clic droit › Options… ouvre le dialogue d'options.

### Options d'un breakpoint ou d'un watchpoint

| Option | Effet |
| --- | --- |
| Condition C | arrêt seulement si l'expression est vraie (vide : toujours) |
| Arrêt au passage n° | gdb compte les passages sans arrêter la cible, s'arrête à celui-ci, puis à chaque passage ; le compte part du moment où l'option est posée |
| Message | à chaque passage, le texte s'écrit dans la console, puis la cible repart |
| S'arrêter après le message | la cible reste arrêtée après le message |

Dans le message, `{expression}` est remplacé par la valeur de l'expression C, et `{expression:hex}` par la même valeur dans un format (`hex`, `dec`, `oct`, `bin`, `ascii`). Exemple : `capteur n={counter} tick={tick:hex}` donne `capteur n=7 tick=0x7`.

Un passage avec message est un arrêt interne : les vues ne se rafraîchissent pas et la bannière « Breakpoint N » de gdb est masquée. Chaque passage coûte un aller-retour avec la cible, de l'ordre de la milliseconde : sur une fonction appelée des milliers de fois par seconde, la cible ralentit d'autant. Un script peut aussi poser une action Python qui décide de l'arrêt (section 11 et [`docs/scripting.md`](scripting.md)).

### Go.Till

**Run › Go.Till (jusqu'à une condition)…** lance la cible jusqu'à ce qu'une condition C devienne vraie, par exemple `counter >= 20`.

- La cible tourne à pleine vitesse : GdbTrace pose un watchpoint matériel sur les variables de l'expression, avec l'expression pour condition.
- Le watchpoint est retiré à la reprise suivante et n'apparaît pas dans la vue Breakpoints.
- Un registre ou une valeur sans adresse ne peut pas être suivi ainsi : GdbTrace le refuse et renvoie vers Step.Till.

### Step.Till et Step.Change

**Run › Step.Till / Step.Change…** avance pas à pas jusqu'à ce qu'une expression soit vraie (Step.Till) ou que sa valeur change (Step.Change).

- Les pas sont des lignes source, ou des instructions si **Pas assembleur** est coché.
- Toute expression convient, registres compris. Un nombre de pas maximum borne la boucle (1000 par défaut).
- La boucle compte comme une seule exécution : les vues ne se rafraîchissent qu'à la fin, et **Break** l'interrompt.
- Un breakpoint ou un signal l'arrête aussi.
- La barre d'état donne le bilan, par exemple `Step.Change last_char, 39 pas, 71 'G' → 72 'H'`.

## 5. Variables, mémoire, registres et périphériques

Cible arrêtée, toutes les données passent par gdb. Cible en marche, GdbTrace les lit par un canal mémoire à part, sans arrêter le cœur, quand le serveur en offre un. Un double-clic sur une variable Watch, un registre ou un octet mémoire permet d'en modifier la valeur.

### Watch et Pile

- **Watch** : saisir une expression C puis **Ajouter**. Les structures et tableaux se déplient. Une expression ajoutée cible en marche est évaluée à l'arrêt suivant ; une locale hors de portée est réévaluée à chaque arrêt.
- **Pile** : la pile d'appels et les variables locales de la frame choisie.
- **Format d'affichage** : clic droit sur une variable (Watch ou locale) › Naturel, Décimal, Hexadécimal, Octal, Binaire ou ASCII.
  - Le format d'une structure ou d'un tableau vaut pour tous ses membres ; un membre peut avoir le sien.
  - Le format reste appliqué cible en marche et il est gardé dans le layout ; celui d'une locale est gardé par fonction.
  - Un flottant en hexadécimal, décimal, octal ou binaire montre ses bits bruts (`21.5` → `0x41ac0000`).
  - Le binaire couvre toute la largeur du type, par groupes de 4 (`0b1111_1111_1111_1101`).
  - L'ASCII lit les octets dans l'ordre de la mémoire (`'AB\0\0'`).

### Registres et Mémoire

- **Registres** : les registres du cœur en hexadécimal et en décimal, avec le décodage de xPSR ; tous les autres sur demande. Les registres modifiés depuis l'arrêt précédent sont en rouge.
- **Mémoire** : un dump hexadécimal et ASCII à partir d'une adresse ou d'un symbole. Elle part de 0x20000000 (la SRAM d'un Cortex-M3/M4/M7). Pour un programme natif, ou un firmware dont la RAM est ailleurs (STM32N6), elle part de sa première variable globale.

### Périphériques

La vue **Périphériques** décode les registres et leurs champs, avec leurs énumérations, d'après un fichier CMSIS-SVD (`--svd`, ou Fichier › Charger un SVD…). Le fichier est lu en tâche de fond : celui d'un STM32N6 fait 22 Mo et demande environ une seconde, pendant laquelle l'interface reste utilisable.

- Seuls les périphériques dépliés sont lus, par blocs de registres contigus ; les trous réservés ne sont jamais lus.
- Les registres en écriture seule ou à lecture destructive (ex. le registre de données d'un UART) ne sont pas lus d'office ; clic droit › **Lire quand même**.
- Double-clic : écriture d'un registre, ou d'un champ par lecture-modification-écriture. Le registre est relu ensuite, et un écart est signalé.

### Cible en marche

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

## 6. RTOS et threads

La vue RTOS montre les tâches FreeRTOS sans rien ajouter au firmware. La vue Temps des tâches mesure le temps de chacune, à condition que le firmware écrive ses changements de tâche sur l'ITM. La vue Threads montre les threads d'un programme de l'hôte.

### Vue RTOS (FreeRTOS)

Elle s'active quand FreeRTOS est lié dans l'ELF. Elle lit les structures du noyau en mémoire, d'après leur disposition lue une fois dans le DWARF, et marche avec QEMU, OpenOCD ou un autre serveur.

- **Tâches** : état, priorité (et priorité de base en cas d'héritage par mutex), pile utilisée sur sa taille, octets jamais utilisés (ligne rouge au-delà de 90 %), % CPU, objet attendu. La tâche courante est en gras et rappelée dans la barre d'état.
- **Objets du noyau** : queues, mutex (avec leur détenteur), sémaphores, tâches en attente. Il faut `configQUEUE_REGISTRY_SIZE` > 0.
- **Contexte d'une tâche** (double-clic, cible arrêtée) : ses registres, relus sur sa pile, et sa pile d'appels. Les registres du cœur sont toujours restaurés ensuite.
- **En marche** : la vue se rafraîchit par le canal mémoire. Une lecture faite pendant un changement de contexte est détectée et relancée.

Configuration conseillée du noyau : `configUSE_TRACE_FACILITY 1`, `configRECORD_STACK_HIGH_ADDRESS 1`, `configCHECK_FOR_STACK_OVERFLOW 2`, `configQUEUE_REGISTRY_SIZE` > 0, `configGENERATE_RUN_TIME_STATS 1`. Ports gérés pour le contexte des tâches : ARM_CM3, ARM_CM4F et ARM_CM0. Depuis V11.3, le port ARM_CM0 range sur la pile EXC_RETURN, puis r4-r11 : GdbTrace reconnaît ce cadre (symbole `vRestoreContextOfFirstTask` sur un Cortex-M0/M0+).

### Temps des tâches

La vue **Temps des tâches** mesure exactement le temps de chaque tâche, sans échantillonnage. Elle donne par tâche les activations, le temps brut, le temps net (sans les exceptions), la part du temps, la tranche maximale et la moyenne. Un chronogramme montre les dernières tranches, une rangée par tâche, avec la durée au survol.

Côté firmware, trois lignes dans `FreeRTOSConfig.h` et une fonction qui écrit le TCB entrant sur le port ITM 31, comme dans `examples/freertos` :

```c
#define GDBTRACE_TASK_PORT 31
void gdbtrace_task_switched_in(void *tcb);         /* écrit tcb sur le port ITM 31 */
#define traceTASK_SWITCHED_IN() gdbtrace_task_switched_in((void *)pxCurrentTCB)
```

La fonction reste inerte tant que le débogueur n'a pas activé l'ITM. La mesure demande une capture SWO avec l'horodatage ITM (`--swo-config`, ou Trace › ETM par SWO…).

### Vue Threads (programme de l'hôte)

- Pour chaque thread : numéro gdb, nom (`pthread_setname_np`), LWP, état, cœur et position.
- Le thread courant est en gras ; celui qui a provoqué l'arrêt est rappelé en tête.
- **Double-clic** (programme arrêté) : le thread devient courant, et registres, pile, source et pas à pas portent sur lui.
- La liste suit la création et la fin des threads, même en marche. Tous les threads s'arrêtent ensemble (mode all-stop de gdb), sauf en mode non-stop.

**Mode non-stop** (`--non-stop`) : un thread s'arrête et repart seul, les autres continuent ; un breakpoint n'arrête que le thread qui l'atteint.

- Les vues suivent un thread, en gras : celui qui vient de s'arrêter, ou celui choisi (double-clic, ou clic droit › Suivre ce thread). Arrêté, il s'inspecte et avance en pas à pas pendant que les autres tournent.
- Clic droit › **Arrêter ce thread** / **Relancer ce thread** ; Go et Break valent pour tous les threads.
- Les breakpoints à action y sont des arrêts ordinaires.

Le programme a son propre terminal : sa sortie arrive dans la console. À la fin du programme, l'état passe à TERMINÉ avec son code de sortie ; **Go** le relance, **Reset** le relance arrêté au début de `main`. Un processus attaché (`--pid`) ne se relance pas : GdbTrace ne l'a pas lancé. Démo : `examples/native/pthreads.c` (producteur, consommateur et moniteur nommés).

## 7. Trace, profil et couverture

La trace dit ce que la cible a fait sans l'arrêter. Deux familles : l'**ITM**, sorti par la broche SWO (texte, échantillons de PC, valeurs de variables, changements de tâche), et la **trace d'instructions** (ETM, MTB), qui reconstruit chaque instruction exécutée.

| Ce qu'on veut | Vue | Il faut |
| --- | --- | --- |
| Texte écrit par le firmware | ITM | capture SWO |
| Temps CPU par fonction, ligne, tâche | Profil | capture SWO, ou lecture du PC en marche (OpenOCD, QEMU ; aussi sur un Cortex-M0+ qui a `DWT_PCSR`) |
| Valeur d'une variable à chaque accès | Variables tracées | capture SWO |
| Temps par tâche RTOS | Temps des tâches | capture SWO + crochet du firmware (section 6) |
| Les instructions avant l'arrêt, le flot d'appels | Trace | ETM ou MTB |
| Durées par fonction et par exception | Timing | ETM ou MTB |
| Couverture de code | Couverture | ETM ou MTB |

QEMU n'émule ni l'ITM ni le SWO : sous QEMU, seuls le profil par lecture du PC, l'ETM simulé et la MTB simulée fonctionnent.

Un Cortex-M0 ou M0+ (ARMv6-M) n'a ni ITM, ni SWO, ni ETM : seuls le profil par lecture du PC et la MTB, si la puce en a une, y fonctionnent. GdbTrace reconnaît le cœur à la connexion, et les vues de trace le disent (section 10).

### Capture SWO et vue ITM

La capture vient de trois sources :

- **OpenOCD** : `--swo-config --cpu-hz N` programme ITM, DWT et TPIU, configure le TPIU d'OpenOCD et se connecte à son port. Ajouter `--swo-hz` pour le débit et `--swo-port` pour le port TCP.
- **ST-LINK_gdbserver** : `--stlink --swo-config --cpu-hz N` lance le serveur avec sa capture SWO, programme ITM, DWT et la sortie SWO de la puce, et se connecte au flux servi par le serveur.
- **un serveur SWO en TCP** (Orbuculum, J-Link) : `--swo hôte:port`.
- **un fichier** de capture brute : `--swo-file`, avec `--swo-formatter` si le formateur TPIU était actif.

La vue **ITM** montre le texte UTF-8 de chaque port (port 0 par défaut), les autres ports en hexadécimal, avec filtre de ports et horodatage. Quand le décodage prend du retard sur le flux, le retard et les octets perdus sont affichés. Un octet perdu sur le fil compte comme une erreur dans le bilan de la capture, sans décaler les ports.

### Profil

La vue **Profil** donne le pourcentage de temps CPU par fonction, par ligne (double-clic : la source) et par tâche FreeRTOS, sans instrumenter le code. « Auto » prend les échantillons de PC du SWO si une capture tourne, sinon la lecture du PC en marche (`DWT_PCSR` par OpenOCD, ou le PC de QEMU). L'onglet **Exceptions** compte les entrées dans chaque exception et, avec l'horodatage ITM, le temps passé dedans.

Un profil qui marche ne prouve donc pas que le SWO est capturé : les vues ITM, Trace et Couverture disent si une capture tourne et ce qu'elle reçoit.

### Variables tracées

La vue **Variables tracées** suit la valeur d'une globale à chaque accès, cible en marche. Un comparateur du DWT surveille son adresse et émet la valeur par l'ITM, et au choix le PC de l'instruction.

- Lancement : dans la vue (cible arrêtée), ou `--var-trace counter`, `--var-trace offset:rw+pc`.
- Un petit graphe par variable, chacun à sa propre échelle ; le survol donne les valeurs à un instant.
- Limites : variables de 1, 2 ou 4 octets ; 4 comparateurs au plus sur un M3/M4/M7/M55, partagés avec les watchpoints et le filtre ETMv3. Sur un ARMv8-M (M33, M55), la variable doit être alignée sur sa taille ; GdbTrace reconnaît le DWT et le programme selon son format.

### Trace d'instructions ETM

| Source | Option | Pour qui |
| --- | --- | --- |
| ETM simulé | `--etm-sim` | QEMU : son journal d'exécution est converti en flux ETM à chaque arrêt |
| Broche SWO | `--etm-config --etm-port swo --cpu-hz N`, ou **Trace › ETM par SWO…** | une sonde de débogage (ST-Link) avec OpenOCD ; faible débit |
| Port de trace parallèle | `--etm-config [--etm-width 4] [--stm32]` puis `--trace hôte:port` | une sonde de trace et une carte qui sort TRACECLK et TRACED0-3 |
| Tampon en RAM (ETR) | `--etm-config --etm-port etr`, ou **Trace › ETM par l'ETR** | STM32N6 : la puce écrit la trace en RAM, GdbTrace la lit en continu (section 9) |
| Fichier | `--trace-file` | rejouer une capture |

GdbTrace lit la version de l'ETM sur la cible : ETMv3 pour un M3/M4, ETMv4 pour un M7 ou un M55. `--etm-stall` ralentit le cœur plutôt que de perdre de la trace quand la FIFO de l'ETM déborde ; il est indispensable par SWO sur un cœur rapide.

**Filtre** (`--etm-filter`, `--etm-start`, `--etm-stop`, ou **Trace › Filtre de trace ETM…**) : ne tracer qu'une fonction, un fichier ou une plage d'adresses, ou démarrer et arrêter la trace sur un lieu. Le débit va alors à ce qui compte. Chaque reprise de la trace est marquée « trace interrompue ». Sur un Cortex-M55, dont l'ETM n'a pas de comparateurs d'adresse, le filtre passe par les comparateurs du DWT : une paire par plage, un par start ou stop, partagés avec les variables tracées.

La vue **Trace** montre :

- **Listing** : les N dernières instructions avant l'arrêt, avec fonction, désassemblage, ligne source, exceptions et retours ;
- **Appels** : appels, retours et exceptions, indentés par profondeur.

En capture continue, Trace et Timing se redessinent à l'arrêt, et la Couverture ne met à jour que son résumé en marche : l'interface reste fluide.

### Timing

La vue **Timing** donne, par fonction et par exception, le nombre d'appels, le temps brut (interruptions déduites), le temps net (sans les appelés), le min, le max et la moyenne. Un chronogramme montre les appels dans le temps (molette : zoom ; glisser : déplacer ; clic : source).

Les durées sont **mesurées** avec `--etm-cycle-accurate`, et **estimées** par un modèle de cycles sinon ; la vue indique toujours laquelle. Unités : cycles ou µs (`--cpu-hz`, 25 MHz par défaut).

- ETMv3 (M3/M4) : chaque instruction porte ses cycles ; environ 8 fois plus de trace.
- ETMv4 (M7, M55) : l'ETM compte les cycles par groupe de branchements ; le modèle répartit chaque mesure entre les instructions du groupe. Environ 3 fois plus de trace : par SWO, le stall s'impose ; par l'ETR, le tampon garde moins d'exécution.

Le modèle ne connaît ni les attentes mémoire ni les caches : sur une STM32N6 sans cache, les durées mesurées sont environ 25 fois les estimées.

`--etm-timestamps` (ETMv4) insère dans la trace le compteur de temps global de la puce, à chaque synchronisation ; Trace.List le montre (« horodatage N »).

### Couverture

La vue **Couverture** déduit la couverture du code objet de la trace d'instructions, sans recompiler ni instrumenter :

- **instructions** exécutées ;
- **branches** : chaque branchement conditionnel doit avoir été vu pris et non pris.

Elle liste fichiers et fonctions (ok, partiel, jamais). **Annoter Source et Désassemblage** marque chaque ligne (✔ ½ ✘) et nomme l'issue manquante d'un branchement. La base s'accumule d'exécution en exécution jusqu'à **RàZ** ; **Enregistrer** et **Charger** cumulent des campagnes. Rapports : HTML autonome et LCOV (`.info`).

Tant qu'aucune instruction n'arrive, les vues Couverture et Trace disent pourquoi : pas de capture, sonde qui ne sert pas le SWO, flux sans formateur, aucun octet de l'ETM.

### MTB (Cortex-M0+)

Le Cortex-M0+ n'a ni ETM ni SWO. Sa MTB écrit en SRAM un paquet à chaque branchement : c'est un enregistreur de vol des derniers branchements avant l'arrêt. Trace, Timing et Couverture fonctionnent comme avec l'ETM.

- `--mtb` active la MTB de la cible ; `--mtb-probe` liste les composants CoreSight pour savoir si la puce en a une.
- Le firmware réserve le tampon : `__mtb_buffer__`, aligné sur sa taille, ou `--mtb-buffer adresse:taille`.
- `--mtb-autohalt` donne une trace complète : la MTB arrête le cœur à chaque tampon plein, GdbTrace le vide et relance, sans arrêt visible. La cible ralentit beaucoup ; préférer un tampon de 4 à 8 Ko.
- `--mtb-sim` simule la MTB sous QEMU (`--machine microbit`, `examples/m0plus`).
- La MTB est optionnelle : les STM32G0 et STM32F0, par exemple, n'en ont pas (section 10).

## 8. Sur une Nucleo-F746ZG

La Nucleo-F746ZG (Cortex-M7, 216 MHz, ST-Link V2-1 embarqué) est la carte de référence : GdbTrace y trace l'ETMv4 par la broche SWO, sans sonde de trace, et un test applicatif y vérifie toutes les vues à chaque release.

### Firmware d'exemple

`examples/nucleo-f746` construit la démo FreeRTOS de `examples/freertos` pour cette carte : même `main.c`, avec la configuration, le démarrage et le port FreeRTOS ARM_CM7 r0p1 de la carte. Le cœur y tourne à 16 MHz.

```bash
make -C examples/nucleo-f746
```

### Lancer GdbTrace sur la carte

Avec la configuration fournie (`configs/nucleo-f746zg.yaml`, ou Configuration › Ouvrir… puis Connecter) :

```bash
.venv/bin/python -m gdbtrace --config configs/nucleo-f746zg.yaml
```

Elle équivaut à :

```bash
.venv/bin/python -m gdbtrace examples/nucleo-f746/nucleo-f746.elf --program \
    --openocd configs/nucleo-f746zg.cfg --svd STM32F746.svd --cpu-hz 16000000 \
    --etm-config --etm-port swo --etm-stall --var-trace produced:write --os freertos
```

- `--etm-config --etm-port swo --etm-stall` : trace ETMv4 et ITM par la broche SWO. Le cœur ralentit au débit du SWO plutôt que de perdre de la trace. Dans l'interface, cible arrêtée : **Trace › ETM par SWO…**.
- `--cpu-hz` doit être la fréquence réelle du cœur, pour le débit SWO et la conversion en µs.
- Le fichier SVD vient de STM32CubeIDE.
- Plusieurs sondes branchées : `STLINK_SERIAL=<numéro de série>` choisit la bonne.
- La connexion se fait sous reset. Pour s'attacher à un cœur qui tourne sans le réinitialiser : `CONNECT_UNDER_RESET=0` et `--no-load`.

Le débit du SWO limite la trace sans perte : environ 3,5 MHz de cœur tracé avec un ST-Link V2-1, 20 à 35 MHz avec un STLINK-V3. Au-delà, `--etm-stall` est indispensable.

### Test sur carte

```bash
tools/release_check.sh
```

Ce script lance toute la suite, construit l'oracle OpenCSD s'il manque, et **exige** les cartes de test : la Nucleo-F746ZG, la Nucleo-N657X0-Q (section 9) et les Nucleo-G070RB et F072RB (section 10). Le test sur la F746 (`tests/test_board_f746.py`) :

1. construit et flashe le firmware d'exemple ;
2. lance GdbTrace par son vrai point d'entrée, interface comprise, avec la configuration `configs/nucleo-f746zg.yaml` : SVD, ETM et ITM par SWO, une variable tracée, FreeRTOS ;
3. déroule trois phases : arrêt sur `worker_step`, marche avec le profil, arrêt final ;
4. relève à chaque phase le contenu de toutes les vues dans `tests/artifacts/board-f746.json`, et un test par vue vérifie ce qui doit s'y afficher.

La carte est trouvée seule (sonde ST-LINK dans `/sys`, puis identité de la puce lue par OpenOCD). `GDBTRACE_F746_SERIAL` impose la sonde, `GDBTRACE_SVD_DIR` le dossier du SVD. Avec pytest, `--boards=auto` (défaut) ignore ces tests sans carte, `--boards=require` les fait échouer, `--boards=skip` les désactive.

Le test remplace le firmware de la carte, et la laisse sans ETM ni stall.

## 9. Sur une Nucleo-N657X0-Q (STM32N6)

La Nucleo-N657X0-Q porte un STM32N6 : un Cortex-M55 (ARMv8.1-M, Helium) sans flash interne, avec un STLINK-V3EC embarqué. OpenOCD 0.12 ne connaît pas cette puce : GdbTrace passe par ST-LINK_gdbserver. L'ETM y est tracé par l'ETR de la puce, qui l'écrit en RAM, et l'ITM par le SWO de la sonde.

### Firmware d'exemple

`examples/nucleo-n6` construit la même démo FreeRTOS pour cette carte :

- port FreeRTOS ARM_CM55_NTZ (le cœur reste en état sécurisé) ;
- cœur à 64 MHz, l'horloge HSI où le laisse la ROM de démarrage ;
- image entière en AXISRAM2, à l'adresse d'un FSBL (0x34180400) ;
- à chaque tick, des boucles à faible coût (WLS, DLS, LE) et du Helium (`lob.c`), pour que la trace en contienne ;
- un tampon de 256 Ko pour l'ETR, réservé en AXISRAM1 (`__etr_buffer__`).

```bash
make -C examples/nucleo-n6
```

### Lancer GdbTrace sur la carte

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

### Ce que donne la trace

- Cible en marche, GdbTrace lit le tampon en continu (toutes les 50 ms) et décode au fil de l'eau : la trace couvre toute l'exécution, pas seulement ce que tient le tampon, et la Couverture se met à jour en marche. À l'arrêt, il ne reste que la fin à lire ; Trace et Timing se redessinent.
- La sonde lit environ 550 Ko/s ; la démo en écrit environ 170 Ko/s. Si l'écriture rattrape la lecture, la zone est jetée : la trace a un trou (« trace interrompue »), et l'état de la capture compte ces rattrapages.
- En cycle-accurate (environ 3 fois plus de trace), ou avec `--etr-at-stop`, le tampon est lu seulement à l'arrêt. Il est circulaire : 256 Ko gardent environ la dernière seconde et demie de la démo, un tiers en cycle-accurate. Quand il a tourné, la trace commence par « trace interrompue », et les vues Trace et Couverture l'indiquent (« tampon plein : début de l'exécution perdu »).
- Les boucles à faible coût et le Helium du M55 sont décodés.
- Les durées de la vue Timing sont estimées, sauf avec `--etm-cycle-accurate` : le modèle de cycles ne connaît pas les attentes de l'AXISRAM, sans cache dans la démo (les durées mesurées sont environ 25 fois les estimées).

### Test sur carte

`tests/test_board_n6.py` fait comme sur la F746 :

1. charge le firmware d'exemple en RAM ;
2. lance l'interface avec la configuration `configs/nucleo-n657x0-q.yaml` : ITM par le SWO, ETM par l'ETR, une variable tracée ;
3. relève et vérifie toutes les vues (`tests/artifacts/board-n6.json`).

Il vérifie en plus le décodage des boucles LE : le nombre de tours relevé dans la trace doit suivre le firmware.

La carte est trouvée seule : sonde ST-LINK dans `/sys`, puis identité de la puce lue par ST-LINK_gdbserver. `GDBTRACE_N6_SERIAL` impose la sonde.

## 10. Sur une Nucleo-G070RB ou une Nucleo-F072RB (Cortex-M0+, Cortex-M0)

Ces deux cartes portent un cœur ARMv6-M, sans trace : un Cortex-M0+ sur la G070, un Cortex-M0 sur la F072. Chacune a un ST-Link V2-1 embarqué, et GdbTrace y passe par OpenOCD.

| | Nucleo-G070RB | Nucleo-F072RB |
| --- | --- | --- |
| Puce | STM32G070RB | STM32F072RB |
| Cœur | Cortex-M0+ | Cortex-M0 |
| Horloge au reset (gardée par la démo) | HSI16, 16 MHz | HSI, 8 MHz |
| Flash, RAM | 128 Ko, 36 Ko | 128 Ko, 16 Ko |
| Configuration OpenOCD | `configs/nucleo-g070rb.cfg` | `configs/nucleo-f072rb.cfg` |
| SVD (STM32CubeIDE) | `STM32G070.svd` | `STM32F072.svd` |
| DEV_ID (DBGMCU_IDCODE) | 0x460 | 0x448 |
| Firmware d'exemple | `examples/nucleo-g070` | `examples/nucleo-f072` |

### Ce que permet un Cortex-M0 ou M0+

Ces cœurs n'ont ni ITM, ni SWO, ni ETM, ni MTB (STM32G0 : RM0444, chapitre 40) : aucune trace. Ils ont 4 breakpoints matériels et un DWT à 2 comparateurs, avec `DWT_PCSR` sur le M0+ du G0 (sur le M0 du F0, à confirmer par le test sur carte).

| Fonction | Sur ces cartes |
| --- | --- |
| Run-control, breakpoints (à compteur, à message), Go.Till, Step.Till et Step.Change | oui ; 4 breakpoints matériels en flash, 2 watchpoints (Go.Till en prend un) |
| Watch, Mémoire, Registres, Périphériques (SVD), RTOS | oui, cible arrêtée ou en marche (mémoire lue par OpenOCD) |
| Profil | oui, par lecture de `DWT_PCSR` cible en marche (« Live ») |
| ITM, Variables tracées, Temps des tâches, Trace, Timing, Couverture | non : ni ITM ni trace d'instructions |

GdbTrace reconnaît le cœur à la connexion (registre CPUID). Les vues ITM, Variables tracées, Temps des tâches, Trace et Couverture l'affichent : « Cortex-M0+ (ARMv6-M) : ni ITM, ni SWO, ni ETM » (ou « Cortex-M0 … »). Une trace demandée (`--swo-config`, `--etm-config`, `--var-trace`, **Trace › ETM par SWO…**, bouton **Tracer**) est refusée avec ce même message, sans rien écrire dans des registres absents.

### Firmware d'exemple

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

### Lancer GdbTrace sur la carte

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
    --openocd configs/nucleo-f072rb.cfg --svd STM32F072.svd --cpu-hz 8000000 --os freertos
```

- Profil : vue **Profil**, source « Auto » ou « Live », **Démarrer** cible en marche.
- Le fichier SVD vient de STM32CubeIDE.
- Plusieurs sondes branchées : `STLINK_SERIAL=<numéro de série>` choisit la bonne.
- La connexion se fait sous reset. Pour s'attacher à un cœur qui tourne sans le réinitialiser : `CONNECT_UNDER_RESET=0` et `--no-load`.

### Test sur carte

`tests/test_board_m0.py` fait comme sur la F746, sans trace, pour chacune des deux cartes (`-k g070`, `-k f072`) :

1. construit et flashe le firmware d'exemple ;
2. lance l'interface avec la configuration de la carte (`configs/nucleo-g070rb.yaml`, `configs/nucleo-f072rb.yaml`) ;
3. relève et vérifie toutes les vues (`tests/artifacts/board-g070.json`, `board-f072.json`) : vues à l'arrêt et en marche, RTOS, profil par `DWT_PCSR`, et les vues de trace, qui doivent dire pourquoi elles restent vides.

Il éprouve en plus les comparateurs du cœur :

- un breakpoint à compteur ;
- un breakpoint à message : un message par passage, sans arrêt visible ;
- un Go.Till sur une globale.

Chaque carte est trouvée seule : sonde ST-LINK dans `/sys`, puis identité de la puce (DEV_ID) lue par OpenOCD. `GDBTRACE_G070_SERIAL` et `GDBTRACE_F072_SERIAL` imposent la sonde. Une carte absente ne fait sauter que ses propres tests.

```bash
xvfb-run -a .venv/bin/python -m pytest tests/test_board_m0.py -k f072 --boards=require
```

## 11. Console et scripts Python

Tout ce que fait l'interface se pilote aussi en Python, par l'objet `gdbtrace`. La notice complète est à part : [`docs/scripting.md`](scripting.md).

### Console

La vue **Console** exécute du Python, avec l'historique et les blocs sur plusieurs lignes. Deux préfixes envoient une commande brute :

| Saisie | Effet |
| --- | --- |
| `gdbtrace.break_("fib"); gdbtrace.go(); gdbtrace.wait()` | Python, avec l'API `gdbtrace` |
| `!info breakpoints` | commande CLI de gdb |
| `mon reset halt` | commande `monitor` vers le serveur (OpenOCD) |

La console affiche aussi la sortie de gdb, les messages des breakpoints à action et la sortie d'un programme de l'hôte.

### Scripts

```bash
.venv/bin/python -m gdbtrace examples/firmware/demo.elf --script examples/scripts/session.py           # interface + script
.venv/bin/python -m gdbtrace examples/firmware/demo.elf --batch --script examples/scripts/session.py   # sans interface
```

Dans l'interface, **Fichier › Exécuter un script…** fait la même chose. Exemples fournis dans `examples/scripts/` : `session.py` (session complète), `demo.py`, `rtos.py` (FreeRTOS), `coverage.py` (campagne de couverture).

Un script peut ce que l'interface ne propose pas : une **action Python** sur un breakpoint, qui décide de l'arrêt (`gdbtrace.break_("animate", action=fonction)`), une campagne de tests en intégration continue, une injection de fautes, un journal CSV.

## 12. Référence des options

`python -m gdbtrace [ELF] [options]`. L'ELF est facultatif avec l'interface : Fichier › Programmer un ELF… le choisit ensuite. `--help` liste les mêmes options.

### Cible et session

| Option | Effet |
| --- | --- |
| `--qemu` | cible QEMU (défaut pour un ELF ARM) |
| `--machine M` | machine QEMU (`mps2-an385` par défaut ; `microbit`, `mps2-an500`…) |
| `--openocd CFG…` | lancer OpenOCD avec ces fichiers `-f` |
| `--stlink [SN]` | lancer ST-LINK_gdbserver sur la sonde de numéro de série SN (à donner si plusieurs sondes sont branchées) |
| `--stlink-ap N` | avec `--stlink` : port d'accès du cœur (STM32N6 : 1) |
| `--ram-boot` | avec `--stlink` : image exécutée en RAM (reset système, chargement, sp et pc pris dans sa table des vecteurs) |
| `--remote H:P` | serveur gdb déjà lancé |
| `--tcl H:P` | avec `--remote` : port TCL d'un OpenOCD lancé à part (mémoire en marche) |
| `--native` | programme de l'hôte (défaut pour un ELF non ARM) |
| `--pid PID` | s'attacher à un programme de l'hôte déjà lancé (ELF par défaut : `/proc/PID/exe`) |
| `--non-stop` | programme de l'hôte : mode non-stop de gdb (un thread s'arrête et repart seul) |
| `--remote-exec CHEMIN` | programme de l'hôte servi par `gdbserver --multi` : son chemin côté serveur |
| `--args 'A B…'` | programme de l'hôte : ses arguments (syntaxe shell) |
| `--cwd DOSSIER` | programme de l'hôte : son répertoire de travail |
| `--svd F` | description CMSIS-SVD des périphériques |
| `--rtos [TYPE]` | avec `--openocd` : tâches RTOS vues comme threads gdb |
| `--gdb GDB`, `--gdb-port P` | exécutable gdb, port du serveur gdb (avec `--stlink` : port du relais) |
| `--no-load` | ne pas flasher l'ELF à la connexion |
| `--layout F` | fichier de layout des fenêtres |
| `--config F.yaml` | configuration de session (section 2) ; les autres options l'emportent sur elle |
| `--os aucun\|freertos\|pthread` | OS de la cible : vues RTOS et Temps des tâches (FreeRTOS), Threads (Pthread), aucune des trois (aucun) ; sans `--os`, toutes les vues |

### Programmation, scripts, batch

| Option | Effet |
| --- | --- |
| `--program [ELF]` | programmer l'ELF à la connexion (reset, écriture, vérification, reset) |
| `--no-verify` | sans vérification |
| `--go` | lancer le programme après la connexion |
| `--script S` | script Python exécuté après la connexion |
| `--batch` | sans interface : exécute `--script` puis quitte |

### Trace SWO et ITM

| Option | Effet |
| --- | --- |
| `--swo H:P` | capture SWO servie en TCP |
| `--swo-file F` | rejouer une capture SWO brute |
| `--swo-formatter` | flux SWO avec formateur TPIU |
| `--swo-config` | programmer ITM, DWT et TPIU (ou le SWO de la puce) à la connexion, et démarrer la capture (OpenOCD, ST-LINK_gdbserver) |
| `--cpu-hz N`, `--swo-hz N`, `--swo-port P` | fréquence du cœur, débit SWO, port TCP du SWO d'OpenOCD |
| `--var-trace EXPR[:ACCÈS]` | trace de données du DWT ; accès `write`, `read`, `rw`, suivi de `+pc` ; répétable |

### Trace d'instructions ETM

| Option | Effet |
| --- | --- |
| `--etm-sim` | QEMU : ETM simulé |
| `--trace H:P`, `--trace-file F` | flux TPIU formaté en TCP, ou rejoué depuis un fichier |
| `--etm-config` | programmer l'ETM et sa sortie (TPIU, ou ETR) à la connexion |
| `--etm-port parallel\|swo\|etr` | sortie de l'ETM : port parallèle, broche SWO, ou tampon en RAM écrit par l'ETR (STM32N6) |
| `--etr-buffer ADRESSE:TAILLE` | avec `--etm-port etr` : tampon de l'ETR (défaut : le symbole `__etr_buffer__` du firmware) |
| `--etr-at-stop` | avec `--etm-port etr` : lire le tampon seulement à l'arrêt (par défaut : en continu, sauf en cycle-accurate) |
| `--etm-width 1\|2\|4` | largeur du port parallèle |
| `--etm-stall` | ralentir le cœur plutôt que perdre de la trace |
| `--etm-cycle-accurate` | durées mesurées (environ 8 fois plus de trace en ETMv3, 3 fois en ETMv4) |
| `--etm-timestamps` | horodatage de la trace ETMv4 (compteur de temps global de la puce) |
| `--stm32` | activer les broches de trace (DBGMCU_CR des STM32) |
| `--etm-filter PLAGE` | ne tracer que cette fonction, ce fichier ou ces adresses ; répétable |
| `--etm-start LIEU`, `--etm-stop LIEU` | démarrer, arrêter la trace sur un lieu |

### MTB et couverture

| Option | Effet |
| --- | --- |
| `--mtb [ADRESSE]` | activer la MTB de la cible |
| `--mtb-buffer ADRESSE:TAILLE` | tampon de la MTB en SRAM |
| `--mtb-sim [TAILLE]` | QEMU : MTB simulée |
| `--mtb-autohalt` | trace complète, au prix d'arrêts internes |
| `--mtb-probe` | lister les composants CoreSight de la cible |
| `--coverage F` | écrire la couverture à la fin du `--batch`, ou du script dans l'interface (`.html`, `.info`, `.json`) ; répétable |
| `--coverage-merge F.json` | ajouter une couverture enregistrée ; répétable |

### Fichier de configuration (YAML)

Une configuration (section 2) est un fichier YAML. Les sections ont des noms français ; dans `serveur` et `trace`, les clés sont les noms des options de la ligne de commande, sans leurs tirets de tête.

```yaml
gdbtrace: 1                        # version du format
nom: Nucleo-N657X0-Q
cible:
  elf: ../examples/nucleo-n6/nucleo-n6.elf
  svd: STM32N657.svd
  cpu-hz: 64000000
  programmer: true
  verifier: true                   # false : --no-verify
  go: false
serveur:
  type: stlink
  stlink-ap: 1
  ram-boot: true
os: freertos
script: ../examples/scripts/rtos.py
couverture:
  fusionner: [campagne1.json]
  exporter: [couverture.html, couverture.json]
trace:
  swo-config: true
  etm-config: true
  etm-port: etr
  var-trace: [produced:write]
```

| Section | Clés | Équivalent en ligne de commande |
| --- | --- | --- |
| `cible` | `elf`, `svd`, `cpu-hz`, `programmer`, `verifier`, `go` | l'ELF, `--svd`, `--cpu-hz`, `--program`, `--no-verify` (si `false`), `--go` |
| `serveur` | `type` : `qemu`, `openocd`, `stlink`, `gdb`, `remote` | `--qemu`, `--openocd`, `--stlink`, `--native`, `--remote` |
| `serveur`, QEMU | `machine` | `--machine` |
| `serveur`, OpenOCD | `openocd` (liste de `.cfg`), `rtos` | `--openocd CFG…`, `--rtos` |
| `serveur`, ST-LINK | `stlink` (numéro de série), `stlink-ap`, `ram-boot` | `--stlink SN`, `--stlink-ap`, `--ram-boot` |
| `serveur`, programme de l'hôte | `args`, `cwd` | `--native --args`, `--cwd` |
| `serveur`, déjà lancé | `remote`, `tcl` | `--remote`, `--tcl` |
| `serveur`, commun | `gdb`, `gdb-port`, `no-load` | `--gdb`, `--gdb-port`, `--no-load` |
| `os` | `aucun`, `freertos`, `pthread` | `--os` |
| `script` | chemin | `--script` |
| `couverture` | `fusionner`, `exporter` (listes) | `--coverage-merge`, `--coverage` |
| `trace` | toute option de trace : `swo-config`, `swo-hz`, `etm-config`, `etm-port`, `var-trace`, `etm-filter`, `etm-sim`, `mtb`… | la même option |

- Une option sans valeur s'écrit `true` ; une option répétable s'écrit en liste.
- Un chemin relatif l'est au fichier YAML. Exceptions : un fichier `.cfg` qui n'est pas à côté du YAML est cherché par OpenOCD dans son dossier de scripts (`board/st_nucleo_f4.cfg`), et un SVD donné par son seul nom est cherché dans STM32CubeIDE (`GDBTRACE_SVD_DIR` pour un autre dossier).
- À l'enregistrement, un chemin proche du fichier (au plus deux `..`) est écrit relatif, les autres absolus.
- Une clé inconnue, ou qui ne va pas avec le serveur choisi, est refusée en la nommant.

## 13. Dépannage et limites

Quand une capture ne démarre pas, GdbTrace le dit : le journal de mise en route s'affiche dans la console, et les vues ITM, Trace et Couverture donnent la cause. Commencer par là.

### Symptômes fréquents

| Symptôme | Cause probable | Que faire |
| --- | --- | --- |
| Le profil marche mais Trace et Couverture disent « pas de capture SWO » | le profil a pris la lecture du PC en marche, pas le SWO | lancer la capture : `--swo-config`, ou Trace › ETM par SWO… ; vérifier `--cpu-hz` |
| Octets reçus mais aucun de l'ETM | ETM non programmé, ou `--cpu-hz` / débit SWO faux | `--etm-config` (ou Trace › ETM par SWO…), fréquence réelle du cœur |
| Trace à trous (« trace interrompue ») | le débit du port ne suit pas le cœur | `--etm-stall`, un filtre ETM, ou une horloge plus basse |
| Vues figées cible en marche | pas de canal mémoire : `--remote` sans `--tcl` | ajouter `--tcl` (OpenOCD), sinon les vues se rafraîchissent à l'arrêt |
| Écriture d'un registre de périphérique sans effet sous QEMU | QEMU ignore ces écritures | GdbTrace le signale à la relecture ; sur une vraie carte, elles marchent |
| Rien dans la vue ITM sous QEMU | QEMU n'émule ni l'ITM ni le SWO | tester l'ITM sur carte |
| OpenOCD ne démarre pas : port déjà pris | un autre OpenOCD tient la sonde ou le port | GdbTrace vérifie les ports avant de lancer et le dit ; arrêter l'autre serveur |
| La connexion échoue sur un firmware qui ne dort jamais | connexion à chaud difficile | connexion sous reset (défaut de `configs/nucleo-f746zg.cfg`) |
| Plusieurs sondes, la mauvaise est prise | OpenOCD prend la première | `STLINK_SERIAL=<numéro>` |
| Breakpoint refusé en flash | le serveur ne sait pas écrire en flash | cocher **Matériel** |
| « ST-LINK_gdbserver introuvable » | ni STM32CubeIDE ni STM32CubeCLT sous `/opt/st` ou `~/st` | installer l'un des deux, ou mettre `ST-LINK_gdbserver` dans le PATH |
| STM32N6 : le programme ne démarre pas, ou la connexion échoue | cavalier de démarrage, ou mauvais port d'accès | JP2 en 2-3 (démarrage de développement), `--stlink-ap 1 --ram-boot` |
| « tampon de l'ETR » refusé | le firmware ne réserve pas de tampon | un tableau `__etr_buffer__` aligné sur 4 Ko, ou `--etr-buffer ADRESSE:TAILLE` |
| « ST-LINK_gdbserver lancé sans SWO » | `--swo-config` absent au lancement : le serveur n'a pas démarré sa capture | relancer GdbTrace avec `--swo-config` (et `--cpu-hz`) |
| Go.Till refusé | la condition porte sur un registre ou une valeur sans adresse | Step.Till |
| Configuration refusée (« serveur.stlink-ap : option du serveur stlink, pas de openocd ») | une clé du YAML ne va pas avec le serveur, l'OS ou les autres options | le message nomme la clé ; mêmes règles que la ligne de commande (section 12) |
| « SVD STM32G070.svd introuvable » | SVD donné par son nom, absent de STM32CubeIDE | donner son chemin, ou `GDBTRACE_SVD_DIR` ; la session continue sans vue Périphériques |
| « Cortex-M0+ (ARMv6-M) : ni ITM, ni SWO, ni ETM » | le cœur n'a pas de trace (STM32G0, STM32F0, STM32L0…) | profil par lecture du PC (« Live ») ; MTB si `--mtb-probe` en trouve une |

### Limites connues

- **Validé sur carte** :
  - Nucleo-F746ZG (Cortex-M7, OpenOCD) : ETMv4 et ITM par SWO, trace de données du DWT, profil par SWO, vues RTOS et temps des tâches ;
  - Nucleo-N657X0-Q (Cortex-M55, ST-LINK_gdbserver) : vues en marche par le relais, image en RAM, ITM par le SWO de la sonde, ETMv4.5 par l'ETR (boucles à faible coût comprises), trace de données du DWT de l'ARMv8-M, vues RTOS et temps des tâches.
- **Pas encore validé sur carte** : Nucleo-G070RB (Cortex-M0+) et Nucleo-F072RB (Cortex-M0), par OpenOCD (test prêt : `tests/test_board_m0.py`), lecture de `DWT_PCSR` par OpenOCD (et sa présence sur le Cortex-M0), port de trace parallèle, ETMv3 matériel (M3/M4), MTB d'un vrai M0+, `--rtos` d'OpenOCD.
- **Cortex-M0/M0+** : ni ITM, ni SWO, ni ETM. Sur les STM32G0 et STM32F0, 4 breakpoints matériels et 2 watchpoints, que Go.Till partage avec les watchpoints posés.
- **ETMv4** : Cortex-M7 (par SWO) et Cortex-M55 (par l'ETR d'un STM32N6), cycle-accurate et horodatage compris. Sur le M55, le filtre ETM prend les comparateurs du DWT (4 plages au plus), et la branche d'une instruction « branch future » (BF, BFL…) n'est pas suivie ; les compilateurs n'en émettent pas.
- **ETR** : lu en continu tant que la trace ne dépasse pas ce que lit la sonde (~550 Ko/s) ; au-delà, des trous. En cycle-accurate, lu à l'arrêt : un tampon plein ne garde que la fin de l'exécution.
- **SWO d'un STLINK-V3EC (N6)** : un octet perdu de temps en temps à 2 Mbit/s, compté comme une erreur ITM.
- **Filtre ETMv3** : deux plages au plus, chacune agrandie en bloc aligné de 2^n octets.
- **ETM simulé** : QEMU ne dit pas si une instruction conditionnelle d'un bloc IT (hors branchement) a été exécutée ; elle est comptée exécutée. Les branchements conditionnels sont exacts.
- **Durées** : sans cycle-accurate, ce sont des estimations par un modèle de cycles, sans attentes mémoire. En ETMv4, une mesure couvre un groupe de branchements : sa répartition entre les instructions du groupe reste celle du modèle.
- **Couverture** : c'est celle du code objet (instructions et branches). La couverture MC/DC demande une analyse du source, hors de portée de la trace.
- **QEMU chargé** : un breakpoint est parfois vu deux fois pour un seul passage, car le pas à pas de QEMU revient sans avoir avancé. Le compteur de passages et les messages d'action comptent alors ce doublon. C'est propre à QEMU.
