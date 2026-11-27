GdbTrace se lance par `python -m gdbtrace`, suivi de l'ELF à déboguer puis des options. Sans option de cible, un ELF ARM part sous QEMU et un ELF de l'hôte est exécuté par gdb.

```bash
.venv/bin/python -m gdbtrace examples/firmware/demo.elf                    # QEMU mps2-an385
.venv/bin/python -m gdbtrace fw.elf --openocd board/st_nucleo_f7.cfg       # sonde, OpenOCD lancé par GdbTrace
.venv/bin/python -m gdbtrace fw.elf --stlink                               # sonde ST-LINK, ST-LINK_gdbserver
.venv/bin/python -m gdbtrace fw.elf --remote 127.0.0.1:2331                # serveur gdb déjà lancé
.venv/bin/python -m gdbtrace fw.elf --remote 127.0.0.1:3333 --tcl 127.0.0.1:6666   # OpenOCD lancé à part
.venv/bin/python -m gdbtrace examples/native/pthreads --args 100000       # programme de l'hôte
```

## Choisir la cible

| Cible | Option | Ce que fait GdbTrace |
| --- | --- | --- |
| QEMU | `--qemu` (défaut), `--machine` pour une autre carte | lance QEMU, y charge l'ELF, ouvre le canal QMP pour lire la mémoire en marche |
| Sonde avec OpenOCD | `--openocd f1.cfg f2.cfg…` | lance OpenOCD avec ces fichiers `-f`, flashe l'ELF (sauf `--no-load`), utilise son port TCL pour la mémoire en marche |
| Sonde ST-LINK avec ST-LINK_gdbserver | `--stlink [SN]`, `--stlink-ap N`, `--ram-boot` | lance ST-LINK_gdbserver sur la sonde SN (à donner si plusieurs sondes sont branchées), sur le port d'accès N du cœur ; gdb passe par un relais de GdbTrace qui lit la mémoire en marche et fait le reset. `--ram-boot` : image exécutée en RAM ([Sur une Nucleo-N657X0-Q (STM32N6)](Nucleo-N657X0-Q)) |
| Serveur gdb déjà lancé | `--remote hôte:port` | s'y connecte ; ajouter `--tcl hôte:port` si c'est un OpenOCD, pour la mémoire en marche |
| Programme de l'hôte | `--native` (implicite pour un ELF non ARM) | gdb l'exécute et l'arrête au début de `main` ; `--args` et `--cwd` règlent ses arguments et son répertoire |
| Processus de l'hôte déjà lancé | `--pid PID` (ELF : `/proc/PID/exe` par défaut) | gdb s'y attache et l'arrête là où il en est ; à la fin, il est détaché et continue |
| Programme de l'hôte servi par gdbserver | `--remote hôte:port` avec un ELF de l'hôte | `gdbserver --multi` : GdbTrace lance le programme (`--remote-exec` : son chemin côté serveur) ; `gdbserver hôte:port prog` : déjà chargé ; arrêt au début de `main` |

Les serveurs lancés par GdbTrace (QEMU, OpenOCD, ST-LINK_gdbserver) s'arrêtent avec lui, même s'il est tué brutalement : aucun serveur orphelin ne garde la sonde.

## Configurations (menu Configuration)

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

En ligne de commande, `--config FICHIER.yaml` lance la session de la configuration. Les autres options l'emportent sur elle : une valeur remplace la sienne, une liste (`--var-trace`, `--coverage`…) s'ajoute à la sienne, un serveur d'un autre type remplace le sien. Exemples dans `configs/` : `qemu-demo.yaml`, `qemu-freertos.yaml`, `native-pthreads.yaml`, et les sessions des cartes de test (pages des cartes). Le format est décrit dans la page [Référence des options](Référence-des-options).

```bash
.venv/bin/python -m gdbtrace --config configs/qemu-freertos.yaml
.venv/bin/python -m gdbtrace --config configs/nucleo-n657x0-q.yaml --stlink 0042001A3133510F37363734
```

## Programmer la cible

**Fichier › Programmer un ELF…** (`Ctrl+O`) arrête la cible, la remet à zéro, écrit l'ELF (en flash avec une sonde, en RAM sous QEMU ou avec `--ram-boot`), le vérifie, puis refait un reset. La cible reste arrêtée au vecteur de reset ; symboles, vues et trace passent au nouveau programme.

La vérification compare les sections par CRC (`compare-sections`). Avec ST-LINK_gdbserver, elle relit les sections et les compare à l'ELF : le CRC de ce serveur est faux sur un STM32N6.

- **Image HEX, S-record ou binaire** : Programmer… et `--program` l'acceptent aussi ; un binaire brut demande son adresse (`fw.bin@0x08000000`). L'image est écrite par le serveur gdb comme un ELF, vérifiée par relecture, et les symboles restent ceux de l'ELF de la session.
- **Fichier › Ajouter des symboles…** (`--add-symbols ELF[@DÉCALAGE]`) : les symboles d'un autre ELF (bootloader, image chargée à part) s'ajoutent à ceux de la session ; Source, Pile, profil, trace et couverture les connaissent. Ils restent après une programmation.
- **Reprogrammer l'ELF courant** (`Ctrl+R`) : après une recompilation.
- **ELF récents** : les 8 derniers.
- Sans ELF sur la ligne de commande, l'interface démarre sans cible : choisir l'ELF lance la cible puis la programme.
- Pendant la programmation, Go, Break, les pas et les réglages de trace sont refusés jusqu'à la fin.

| Option | Effet |
| --- | --- |
| `--program [ELF]` | programme l'ELF à la connexion ; ou une image HEX, S-record, binaire (`fw.bin@0x08000000`) |
| `--add-symbols ELF[@DÉCALAGE]` | symboles d'un autre ELF en plus ; répétable |
| `--no-verify` | sans vérification |
| `--go` | lance le programme ensuite |

### Algorithme de flash exécuté sur la cible

Le serveur gdb programme la flash interne qu'il connaît (pilotes d'OpenOCD, de
ST-LINK_gdbserver). Pour une autre mémoire (flash externe QSPI/XSPI, puce qu'OpenOCD ne connaît
pas, serveur sans pilote), GdbTrace exécute lui-même un **algorithme de flash** sur la cible, par
gdb, quel que soit le serveur :

- un **FLM** de CMSIS-Pack (Keil) : code indépendant de sa position, copié en RAM ;
- un **chargeur externe `.stldr`** de STM32CubeProgrammer : lié à son adresse en RAM. Un nom seul
  (`MX66UW1G45G_STM32N6570-NUCLEO.stldr`) est cherché dans le dossier `ExternalLoader` de
  STM32CubeProgrammer (`GDBTRACE_LOADER_DIR` pour un autre dossier).

À la programmation (`--program`, Fichier › Programmer…), la cible est remise à zéro. Les segments
de la mémoire de l'algorithme passent par lui : effacement des secteurs touchés, puis écriture
page par page. Le reste de l'image passe par le serveur. La vérification prend la fonction
Verify de l'algorithme (ou Read d'un `.stldr`), sinon une relecture. Le résumé dit ce qu'a écrit
chaque algorithme. Chaque appel pose r0-r3, r9 (base statique d'un FLM), sp, lr sur une
instruction de retour gardée par un breakpoint matériel, puis laisse tourner la cible jusqu'au
retour. Les appels se font interruptions masquées, sans événement : ni chrono, ni compteurs, ni
trace simulée. Un algorithme bloqué est interrompu après son délai, une faute dans l'algorithme
est dite.

| Option | Effet |
| --- | --- |
| `--flash-loader F` | algorithme de flash : `.FLM`, `.stldr` (ou son nom), ou `pack` (ceux de `--device`) ; répétable, un par mémoire |
| `--flash-ram ADRESSE:TAILLE` | RAM où placer l'algorithme (code, pile, tampon d'une page) ; un FLM hors pack l'exige |

```bash
.venv/bin/python -m gdbtrace fw.elf --stlink --stlink-ap 1 --program --flash-loader MX66UW1G45G_STM32N6570-NUCLEO.stldr
```

```bash
make -C examples/flashalgo        # algorithmes de test : flash émulée à 0x00300000 sous QEMU
```

```bash
.venv/bin/python -m gdbtrace examples/firmware/demo.elf --program motif.bin@0x00300000 --flash-loader examples/flashalgo/ram-flash.FLM --flash-ram 0x20100000:0x10000
```

### Puce décrite par son CMSIS-Pack

`--device NOM` (`--pack FICHIER` pour un pack précis, sinon les packs installés) décrit la puce
par son CMSIS-Pack. GdbTrace en tire :

- le **SVD** des périphériques, si `--svd` n'est pas donné ;
- les **algorithmes de flash** : `--flash-loader pack` prend ceux par défaut de la puce, avec leur
  RAM et l'horloge du pack ;
- la description de la puce dans la vue Système (onglet Puce) : cœur, horloge maximale, mémoires,
  algorithmes ;
- une **vérification** du cœur : celui du pack est comparé à celui qui répond (CPUID), et un écart
  est signalé (autre puce, ou mauvais `--device`).

Un pack est un `.pack` (zip), un `.pdsc`, ou son dossier. Les packs installés sont cherchés dans
`CMSIS_PACK_ROOT`, `~/.cache/arm/packs` (cmsis-toolbox), `~/.arm/Packs` et
`~/.local/share/cmsis-pack-manager` (pyOCD), dernière version de chaque pack. GdbTrace ne
télécharge rien : `cpackget add Keil::STM32F4xx_DFP` (cmsis-toolbox) installe un pack. Le nom de
la puce se donne sans casse, ou par un préfixe unique (`STM32F446RE` pour `STM32F446RETx`). Dans
Configuration, le bouton **Choisir…** liste les puces du pack ou des packs installés.

```bash
.venv/bin/python -m gdbtrace fw.elf --openocd configs/nucleo-f446re.cfg --device STM32F446RE --flash-loader pack --program
```

```bash
.venv/bin/python -m gdbtrace examples/firmware/demo.elf --pack examples/flashalgo/GdbTrace.QEMU_DFP.1.0.0.pack --device GDBTRACE-AN385 --flash-loader pack --program motif.bin@0x00300000
```

## Sans interface : le mode batch

`--batch` exécute un script (`--script s.py`) puis quitte, sans fenêtre. Le code de retour vaut 0 si tout s'est bien passé, 1 sinon ; c'est la forme pour l'intégration continue. Sans `--script`, `--batch --program` flashe la cible et quitte.

```bash
.venv/bin/python -m gdbtrace --program build/fw.elf --openocd board/st_nucleo_f7.cfg --batch --go
.venv/bin/python -m gdbtrace fw.elf --batch --script tests/sur_cible.py
```

## Pour aller plus loin

### Configurations de session (menu Configuration)

Une **configuration** décrit une session en YAML : la cible (chemin de l'ELF, SVD, fréquence
du cœur, programmation), le serveur gdb, l'OS, le script et la couverture, et la trace. Elle
**se traduit en arguments de la ligne de commande** (`gdbtrace/core/config.py`) : GUI, `--batch`
et ligne de commande partagent les mêmes options et les mêmes erreurs.

```yaml
gdbtrace: 1
nom: Nucleo-G070RB
cible:
  elf: ../examples/nucleo-g070/nucleo-g070.elf   # relatif au fichier YAML
  svd: STM32G070.svd                             # nom seul : cherché dans STM32CubeIDE
  cpu-hz: 16000000
  programmer: true
serveur:
  type: openocd                    # qemu | openocd | stlink | gdb (programme de l'hôte) | remote
  openocd: [nucleo-g070rb.cfg]
os: freertos                       # aucun | freertos | pthread
script: ../examples/scripts/rtos.py
couverture: {fusionner: [], exporter: [couverture.html]}
trace: {swo-config: true, var-trace: [produced:write]}   # options de trace, sans les tirets
```

- **Menu Configuration** : Nouvelle…, Ouvrir…, Récentes, Modifier…, Enregistrer (Ctrl+S),
  Enregistrer sous…, Connecter, Fermer. Ouvrir charge la configuration (titre, SVD, vues) sans
  connecter ; **Connecter** lance la session (la précédente est déconnectée). Une
  configuration modifiée et pas enregistrée est proposée à l'enregistrement avant d'être
  quittée.
- **Boîte de dialogue** à onglets : Cible, Serveur (champs propres à chaque serveur), OS
  (« Détecter dans l'ELF »), Trace (SWO/ITM, ETM, variables, filtre, simulation, MTB), Script
  et couverture. OK la valide comme la ligne de commande.
- **Vues selon l'OS** : FreeRTOS → RTOS et Temps des tâches ; Pthread → Threads ; aucun →
  aucune des trois. Sans configuration, toutes les vues du layout.
- **`--config FICHIER.yaml`** : la configuration donne la base, les options de la ligne de
  commande l'emportent (une valeur remplace, une liste s'ajoute, un serveur d'un autre type
  remplace celui de la configuration). Ex. `--config configs/nucleo-n657x0-q.yaml --stlink SN`.
- **Exemples** (`configs/`) : `qemu-demo.yaml`, `qemu-freertos.yaml`, `native-pthreads.yaml`, et
  les sessions des cinq cartes de test (`nucleo-f746zg.yaml`, `nucleo-n657x0-q.yaml`,
  `nucleo-g070rb.yaml`, `nucleo-f072rb.yaml`, `nucleo-f446re.yaml`).
- Dans un script, `gdbtrace.config` donne la configuration courante (lecture seule).

### Programmer la cible

**Fichier › Programmer un ELF…** (`Ctrl+O`) écrit un ELF dans la cible, sans relancer le
débogueur. La séquence : arrêt, reset, écriture (`load` : en flash avec OpenOCD, J-Link ou
pyOCD, en RAM sous QEMU ou sur un STM32N6), vérification (`compare-sections` ; avec
ST-LINK_gdbserver, relecture des sections, son CRC étant faux sur un STM32N6), puis reset. La cible reste
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
