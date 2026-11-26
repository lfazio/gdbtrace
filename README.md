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
| 9. MTB et ETM par SWO | Trace MTB des Cortex-M0+ (décodage validé sur chemins QEMU, démo M0+, MTB simulée) ; ETM d'un M3/M4 par la broche SWO, sans sonde de trace (validé sur Nucleo-F446RE) | ✅ (MTB à valider sur carte) |
| 10. ETMv4 (Cortex-M7) | Trace d'instructions de l'ETM-M7 (ETMv4.0) : décodage validé contre QEMU, OpenCSD et une vraie capture par SWO sur Nucleo-F746ZG ; version de l'ETM détectée ; ETM simulé v4 sous QEMU (`mps2-an500`) ; cycle-accurate (durées mesurées, validées contre DWT_CYCCNT sur F746 et N6) et horodatage | ✅ |
| 11. Trace ciblée et débogage avancé | Filtres de l'ETM (plages, start/stop) ; trace de données du DWT ; temps par tâche RTOS par ITM ; breakpoints à action et à compteur, Go.Till, Step.Change | ✅ |
| 12. Programmes natifs | Un ELF de l'hôte (x86-64, AArch64…) débogué sous gdb : arrêt au début de `main`, vue Threads (pthreads nommés, thread courant, changement de thread), mémoire lue en marche dans `/proc/<pid>/mem`, fin et relance du programme ; attachement à un processus lancé (`--pid`), programme servi par gdbserver, mode non-stop | ✅ |
| 13. ST-LINK_gdbserver et STM32N6 (Cortex-M55) | ST-LINK_gdbserver piloté derrière un relais RSP (mémoire en direct, reset système), image exécutée en RAM ; ITM par le SWO de la sonde ; ETMv4.5 écrit en RAM par l'ETR, lu en continu par le relais ; décodage ARMv8.1-M (boucles à faible coût, Helium) ; DWT de l'ARMv8-M ; validé sur Nucleo-N657X0-Q | ✅ |
| 14. Cortex-M0 et M0+ (STM32G0, STM32F0) | Nucleo-G070RB (M0+) et Nucleo-F072RB (M0) par OpenOCD : cœur ARMv6-M reconnu (CPUID), traces ITM, SWO et ETM refusées en le disant (console, vues de trace) ; profil par `DWT_PCSR` ; FreeRTOS port ARM_CM0 V11.3 (son cadre de pile), validé sous QEMU `microbit` ; test sur carte, passé sur la F072 (attachement sous reset, watchpoints qu'OpenOCD n'attribue pas, interruption doublée par un breakpoint, RTOS lu tâche par tâche en marche) | ✅ F072 ; 🚧 G070 (à valider sur carte) |
| 15. Configurations de session | Menu Configuration et `--config FICHIER.yaml` : cible (chemin de l'ELF, SVD), serveur (QEMU, OpenOCD, ST-LINK_gdbserver, gdb pour un programme de l'hôte, serveur déjà lancé), OS (vues RTOS, Temps des tâches ou Threads selon l'OS), script et couverture, trace ; une configuration = une ligne de commande ; exemples dans `configs/`, sessions des tests sur carte | ✅ |
| 16. Serveur MCP | Un agent (Claude Code…) pilote une session sans interface par le protocole MCP sur stdio : connexion avec les options de la ligne de commande, run-control, breakpoints, données (relues en marche), trace, couverture, timing, code Python avec l'objet `gdbtrace` | ✅ |
| 17. Nucleo-F446RE (Cortex-M4, ETMv3) | ETMv3 matériel par SWO validé : décodage identique à OpenCSD sur une vraie capture, tours de boucle exacts sur 440 appels ; stall effectif (ETMFFLR), options refusées par l'ETM signalées (pas de cycle-accurate sur un ETM-M4), filtre par le DWT avec ses trous de latence comblés ; alignement TPIU retrouvé sans synchro ; test sur carte | ✅ |
| 18. Mesure du temps sans trace | Chrono entre deux arrêts (SysTick et compte des ticks sur un Cortex-M0/M0+ et sous QEMU, `DWT_CYCCNT` au-delà) ; temps des tâches sans ITM par un journal en RAM (`gdbtrace_task_log`) lu en marche ; modèles de cycles Cortex-M0 et M0+ (manuels ARM) pour la MTB | 🚧 (à valider sur carte) |
| 19. Débogage de tous les jours | Vue Faute (CFSR, HFSR, SFSR décodés, cadre empilé, ligne fautive ; cause déduite sur un Cortex-M0/M0+) et arrêt sur faute ; outils mémoire (enregistrer, charger, remplir, chercher, comparer) ; images HEX, S-record, binaire à programmer ; symboles d'autres ELF ; navigateur de symboles ; vue Terminal (semihosting, RTT) ; recherche et export CSV dans la trace | 🚧 (RTT en marche et vector catch à valider sur carte) |
| 20. Analyse de trace avancée | Arbre d'appels (durées par chemin d'appel) ; statistiques d'exceptions (durée, période, gigue, préemptions, enchaînements) depuis la trace ou l'ITM ; variables échantillonnées sans trace par le canal mémoire (Cortex-M0, QEMU) ; compteurs de performance du DWT et de la PMU du Cortex-M55 | 🚧 (compteurs à valider sur carte) |
| 21. Serveur MCP clé en main | `.mcp.json` du dépôt : Claude Code trouve et lance le serveur seul (`--root`, `CLAUDE_PROJECT_DIR`) ; bilan de l'environnement (`--check`, outil `check`) ; 55 outils titrés et annotés (lecture seule, destructif), dont Go.Till, Step.Till / Step.Change, désassemblage, types, écriture, remplissage, recherche et fichiers mémoire, symboles ajoutés, périphériques SVD, valeurs des variables tracées, contexte des tâches et objets FreeRTOS, temps par tâche, filtre ETM, export de la trace et de la couverture ; guide pas à pas dans la notice | ✅ |

## Installation

```bash
sudo apt install python3-tk gdb-multiarch openocd qemu-system-arm gcc-arm-none-eabi
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'     # dev inclut capstone (trace ETM)
```

## Utilisation

**Notice d'utilisation complète : [`docs/utilisation.md`](docs/utilisation.md)** — installation,
lancement et cibles, interface, breakpoints, données, RTOS et threads, trace, Nucleo-F746ZG,
Nucleo-N657X0-Q (STM32N6), Nucleo-G070RB et Nucleo-F072RB (Cortex-M0+, M0), Nucleo-F446RE (STM32F446),
serveur MCP, options, dépannage. Le pilotage en Python a la sienne : [`docs/scripting.md`](docs/scripting.md).

```bash
make -C examples/firmware                                        # firmware de démo Cortex-M3
.venv/bin/python -m gdbtrace examples/firmware/demo.elf          # QEMU mps2-an385, sans matériel
.venv/bin/python -m gdbtrace fw.elf --openocd configs/nucleo-f429zi.cfg   # sonde ST-Link
.venv/bin/python -m gdbtrace fw.elf --stlink                      # ST-LINK_gdbserver (STM32CubeIDE / CubeCLT)
make -C examples/nucleo-n6 && .venv/bin/python -m gdbtrace examples/nucleo-n6/nucleo-n6.elf --stlink \
    --stlink-ap 1 --ram-boot --program --cpu-hz 64000000 --swo-config --etm-config --etm-port etr  # STM32N6
make -C examples/nucleo-g070 && .venv/bin/python -m gdbtrace examples/nucleo-g070/nucleo-g070.elf \
    --openocd configs/nucleo-g070rb.cfg --program --cpu-hz 16000000 --svd STM32G070.svd  # STM32G0 (M0+)
make -C examples/nucleo-f072 && .venv/bin/python -m gdbtrace examples/nucleo-f072/nucleo-f072.elf \
    --openocd configs/nucleo-f072rb.cfg --program --cpu-hz 8000000 --svd STM32F0x2.svd   # STM32F0 (M0)
make -C examples/nucleo-f446 && .venv/bin/python -m gdbtrace --config configs/nucleo-f446re.yaml  # STM32F446 (M4), ETMv3
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
make -C examples/native && .venv/bin/python -m gdbtrace examples/native/pthreads --args 100000  # programme natif
.venv/bin/python -m gdbtrace --config configs/nucleo-g070rb.yaml         # une session décrite en YAML
```

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

### Programmes natifs et pthreads (vue Threads)

Un ELF qui n'est pas ARM (ou `--native`) est un programme de l'hôte : gdb l'exécute lui-même,
sans serveur ni chargement, et il s'arrête au début de `main`. `--args '…'` lui passe ses
arguments (syntaxe shell), `--cwd` son répertoire de travail. Il a son propre terminal : sa
sortie arrive dans la console, et `gdbtrace.s.send_input("…")` écrit sur son entrée (elle ne
peut pas se mêler aux échanges avec gdb).

- **Vue Threads** : numéro gdb, nom (`pthread_setname_np`), LWP, état, cœur, et où en est
  chaque thread. Le thread courant est en gras, celui qui a provoqué l'arrêt est rappelé en
  tête. Double-clic (programme arrêté) : il devient le thread courant, et registres, pile,
  source et pas à pas portent sur lui. La liste suit la création et la fin des threads, même
  en marche (tous les threads s'arrêtent ensemble : mode all-stop de gdb).
- **En marche** : les vues Mémoire et Watch lisent `/proc/<pid>/mem`.
- **Fin du programme** : état TERMINÉ, avec son code de sortie ; Go le relance, Reset le
  relance arrêté au début de `main`.
- Console : `gdbtrace.threads()` (liste, « * » = courant), `gdbtrace.thread("producer")`
  (par nom ou numéro ; retourne sa pile).

Démo : `examples/native/pthreads.c` (producteur, consommateur et moniteur nommés, file
protégée par mutex et variables de condition ; `./pthreads N` s'arrête après N tours).

**Processus déjà lancé** (`--pid PID`, ELF par défaut `/proc/PID/exe`) : gdb s'y attache et
l'arrête là où il en est ; Reset et la relance sont refusés (GdbTrace ne l'a pas lancé) ; à la
déconnexion, il est détaché et continue. Il faut avoir le droit de le tracer (même
utilisateur, et `kernel.yama.ptrace_scope` à 0, sinon CAP_SYS_PTRACE) : GdbTrace le dit.

**Programme servi par gdbserver** (`--remote HOST:PORT` avec un ELF de l'hôte) : avec
`gdbserver --multi HOST:PORT`, GdbTrace lance le programme (`--remote-exec` : son chemin côté
serveur, l'ELF par défaut ; `--args`, `--cwd`) et le relance ; avec `gdbserver HOST:PORT prog
…`, il est déjà chargé, GdbTrace va jusqu'à `main` (pas de relance). Arrêt au début de `main`
dans les deux cas. GdbTrace ne sonde pas le port : gdbserver prendrait la sonde pour son
client. Mémoire en marche par `/proc/<pid>/mem` si le serveur est sur cette machine.

**Mode non-stop** (`--non-stop`) : un thread s'arrête et repart seul, les autres continuent
(un breakpoint n'arrête que le thread qui l'atteint). Les vues suivent un thread, en gras dans
la vue Threads : arrêté, il est inspecté et avance en pas à pas pendant que les autres
tournent. Go et Break valent pour tous les threads ; clic droit dans la vue Threads › Arrêter
ce thread / Relancer ce thread ; `gdbtrace.thread_stop("producer")`,
`gdbtrace.thread_resume(…)`. Les breakpoints à action y sont des arrêts ordinaires.

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
gérés pour le contexte des tâches : ARM_CM3 (testé), ARM_CM4F et ARM_CM0 (testé sous QEMU
`microbit` ; depuis V11.3, EXC_RETURN puis r4-r11 sur la pile, cadre reconnu sur un M0/M0+).

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

Côté firmware FreeRTOS, trois lignes dans `FreeRTOSConfig.h` et la fonction
`gdbtrace_task_switched_in` de `examples/freertos/main.c` :

```c
#define GDBTRACE_TASK_PORT 31
void gdbtrace_task_switched_in(void *tcb, uint32_t ticks);
#define traceTASK_SWITCHED_IN() gdbtrace_task_switched_in((void *)pxCurrentTCB, \
                                    (uint32_t)(xTickCount + xPendedTicks))
```

Deux chemins, l'un ou l'autre, jamais les deux :
- **ITM** : le TCB sur le port 31, avec l'horodatage ITM ; il faut une capture SWO ou de
  trace (`--swo-config`, Trace › ETM par SWO…) ; inerte tant que l'ITM n'est pas activé ;
- **journal en RAM**, sans capture SWO et sur un Cortex-M0/M0+ (pas d'ITM) : l'anneau
  `gdbtrace_task_log` (256 entrées, 2 Ko), horodaté en cycles d'après SysTick et le compte
  des ticks, lu en marche par le canal mémoire (OpenOCD, QEMU), puis à l'arrêt. Temps net =
  brut (pas de trace des exceptions) ; un anneau dépassé entre deux lectures est signalé. Script : `gdbtrace.task_times()`, `gdbtrace.task_timeline(30)`. Validé : le crochet de
la démo reçoit exactement `pxCurrentTCB` à chaque changement (vérifié sous QEMU), calculs
sur des flux ITM reconstitués, et capture réelle validée sur Nucleo-F746ZG (test sur carte).

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
| Live | lecture de `DWT_PCSR` par l'AHB-AP, cœur non arrêté | OpenOCD, Cortex-M0+ (ex. STM32G0), M3, M4, M7 ; optionnel en ARMv6-M |
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
4 comparateurs au plus sur un Cortex-M3/M4/M7/M55 (le DWT de l'ARMv8-M a son propre format,
programmé par GdbTrace ; la variable doit y être alignée sur sa taille). Ces comparateurs sont partagés avec le filtre
ETMv3 et les watchpoints de gdb ; GdbTrace les prend par le haut et refuse quand il n'en reste
plus. Un accès plus court que la variable est montré en hexadécimal brut. Validé sur des flux
ITM synthétiques et sur Nucleo-F746ZG (test sur carte).

QEMU n'émule ni l'ITM ni le SWO : sous QEMU, la sortie ITM du firmware reste inerte et
seul le profil « Live » fonctionne. Le décodage SWO est testé sur des flux synthétiques et un
faux serveur, et validé sur Nucleo-F746ZG (ST-Link, OpenOCD) : texte ITM, profil par SWO,
trace de données, temps des tâches.

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
| ETMv3.5 (M3/M4) | l'ETM n'a pas de comparateur : il prend ceux du DWT (déclenchement ETM sur égalité de PC), par le haut | 2 plages au plus (A OU B) ; une plage est un bloc aligné de 2^n octets qui englobe la plage demandée ; un arrêt demande un démarrage ; les comparateurs pris manquent aux watchpoints de gdb ; plage : coûteuse en débit (ci-dessous) |

L'ETM simulé applique le même filtre effectif au chemin de QEMU. Validation : chemin QEMU
filtré → encodeur → décodeur, identique en ETMv3 et ETMv4 (bare et FreeRTOS) ; registres
vérifiés sur des cibles factices ; puis sur carte (Nucleo-F746ZG, Nucleo-N6, Nucleo-F446RE).

**Plage ou start/stop sur un ETMv3 (constaté sur STM32F446).** Une plage passe par
l'événement TraceEnable, vrai aux seuls cycles où une instruction du bloc s'exécute, avec une
instruction de retard : après chaque branchement, la trace se coupe puis reprend, et chaque
reprise coûte un I-sync de 6 octets. GdbTrace comble ces trous quand le code dit sans
ambiguïté ce qui s'est exécuté (instructions du bloc, sans branchement indirect ni
condition) ; restent les vrais trous (sortie du bloc, interruption). Mais ces I-sync prennent
l'essentiel du débit : `worker_step` filtré par plage donne 208 Ko pour 82 000 instructions,
contre 14 Ko pour 134 000 avec `--etm-start worker_step --etm-stop <son bx lr>`, qui ne
reprend qu'une fois par appel (les fonctions appelées sont alors tracées aussi). Pour une
fonction, préférer start/stop.

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

Ce test tourne dans la suite (`tools/build_opencsd.sh` construit OpenCSD dans `.cache/`).
Puis sur matériel, par le SWO d'une Nucleo-F446RE (ETM-M4, 16 MHz, `--etm-stall`) : GdbTrace
et OpenCSD décodent identiquement une vraie capture, et sur 440 appels de `worker_step(n)`
tracés en entier, la boucle tourne exactement n % 24 fois, d'un tour de plus à chaque appel
(133 508 instructions, 0 erreur, 0 débordement).

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
à chaque synchronisation, GdbTrace non. L'ETMv4.5 d'un Cortex-M55 (STM32N6) est décodé aussi
(section suivante). Cycle-accurate et horodatage : section « Analyse temporelle ».

### Chrono (temps entre deux arrêts)

À chaque arrêt, la barre d'état donne le temps du cœur depuis la dernière reprise (Go
jusqu'à un breakpoint, Go.Till, pas) et le cumul : « ⏱ 4,98 ms (Σ 12,3 ms, SysTick) ».
Mesuré par la cible, sans trace :
- Cortex-M0/M0+ et QEMU : **SysTick**, avec le compte des ticks du logiciel (`xTickCount` de
  FreeRTOS, `uwTick` de la HAL STM32) ; sans lui, à une période de SysTick près ;
- Cortex-M3 et au-delà, sur carte : **`DWT_CYCCNT`**, activé à la première mesure.

Le cœur arrêté, ces compteurs n'avancent pas : les arrêts ne comptent pas, ni les arrêts
internes des breakpoints à message. Run › Remettre le chrono à zéro ; script :
`gdbtrace.chrono()`, `gdbtrace.chrono_reset()` ; serveur MCP : outil `chrono`.

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

Unités : cycles ou µs (fréquence du cœur `--cpu-hz`, 25 MHz par défaut). Sans
cycle-accurate, les durées viennent d'un modèle de cycles qui suit le cœur : Cortex-M3/M4
pour l'ETM, Cortex-M0 ou M0+ (tables de leurs manuels de référence ARM) pour la MTB, réelle
ou simulée ; la vue nomme le modèle.

**D'où viennent les durées** (toujours indiqué en haut de la vue) :

| Source | Durées |
|---|---|
| ETMv3 en mode *cycle-accurate* (`--etm-cycle-accurate`, ETMCR bit 12) | **mesurées** : chaque instruction porte ses cycles. Environ 8 fois plus de trace. L'ETM-M4 n'a pas ce mode (ETMCR relu sur STM32F446) : GdbTrace le dit, et les durées restent estimées. |
| ETMv4 en mode *cycle-accurate* (`--etm-cycle-accurate`, TRCCONFIGR.CCI) | **mesurées** par segment : un paquet Cycle Count compte les cycles depuis le précédent, à chaque groupe d'éléments P0 (seuil TRCCCCTLR, le minimum de l'ETM) ; le modèle ne fait plus que répartir chaque mesure entre les instructions du segment. Environ 3 fois plus de trace. |
| ETM simulé sous QEMU, ou ETM matériel sans cycle-accurate | **estimées** par un modèle de cycles Cortex-M3/M4 : ALU 1, LDR 2, STR 1, LDM/STM/PUSH/POP 1+N, branchement pris 1+P (P = 2), TBB/TBH 2+P, SDIV/UDIV 7, entrée d'exception 12, sortie 10, enchaînement 6. Ni attentes mémoire ni flash : un ordre de grandeur, pas une mesure. |

Le décodage cycle-accurate (formats de P-header, paquets de cycles, I-sync avec cycles) est
validé contre OpenCSD, cycle par cycle, sur les mêmes chemins QEMU que la trace ETM. En
ETMv4 : contre OpenCSD (mêmes instructions entre les mêmes comptages), puis sur carte, où la
somme des cycles mesurés égale l'écart de DWT_CYCCNT sur la même exécution : à 0,005 % près
sur la Nucleo-N6 (ETR), à 0,3 % sur la Nucleo-F746 (SWO, où un débordement de l'ETM fait
perdre un comptage). Sur la N6, les durées mesurées sont ~25 fois celles du modèle : le cœur
lit son code en AXISRAM, sans cache.

**Horodatage** (`--etm-timestamps`, ETMv4) : le compteur de temps global de la puce (TSGEN
sur un STM32N6, démarré par GdbTrace) est inséré aux synchronisations de la trace ; Trace.List
le montre (« horodatage N »).

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
- la MTB est optionnelle : les STM32L0 (L031, L072), à notre connaissance, n'en ont pas,
  ni les STM32G0 (RM0444 §40) (`--mtb-probe` tranche) ; on la trouve sur SAM D21/L21, Kinetis KL, LPC800... ;
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

### Analyse de trace avancée (jalon 20)

- **Arbre d'appels** (onglet de la vue Timing, `call_tree()`) : brut et net par chemin
  d'appel ; une exception est la racine de son arbre.
- **Exceptions** (onglet de la vue Timing ; onglet Exceptions du Profil pour l'ITM ;
  `irq_stats()`) : entrées, durée min/moy/max, période et gigue, préemptions, enchaînements.
- **Variables échantillonnées** (SNOOPer, `--snoop ticks@10`, `snoop()`) : lues par le canal
  mémoire toutes les N ms, sans trace ; dessinées par la vue Variables tracées, aussi sur un
  Cortex-M0/M0+ et sous QEMU.
- **Vue Compteurs** (`counters()`) : cycles, CPI, EXC, SLEEP, LSU, FOLD et instructions
  déduites du DWT ; instructions, défauts de cache et exceptions de la PMU d'un Cortex-M55.

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
| `gdbtrace.program("fw.elf")`, `gdbtrace.program()`, `program("fw.hex")` | FLASH.ReProgram : écrit, vérifie et reset (ELF courant par défaut ; une image HEX, S-record, binaire garde les symboles) |
| `gdbtrace.fault()`, `gdbtrace.break_on_fault()` | analyse de la faute en cours ; arrêt à l'entrée de toute faute |
| `gdbtrace.mem_save("sensors", None, "s.hex")`, `mem_find(0x20000000, 0x1000, "abc")` | Data.SAVE, Data.Find (aussi `mem_load`, `mem_fill`, `mem_compare`) |
| `gdbtrace.sym_list("sensor")`, `gdbtrace.add_symbols("boot.elf")` | sYmbol.List ; symboles d'un autre ELF |
| `gdbtrace.terminal()`, `gdbtrace.terminal_send("go\n")` | texte du semihosting et du RTT ; envoi au programme |
| `gdbtrace.trace_find("exception SysTick")`, `gdbtrace.trace_export("t.csv")` | Trace.FindAll ; listing en CSV |
| `gdbtrace.call_tree(3)`, `gdbtrace.irq_stats()` | arbre d'appels, statistiques d'exceptions |
| `gdbtrace.snoop("ticks", 10)`, `gdbtrace.counters()` | variable échantillonnée sans trace ; compteurs de performance |
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

### Serveur MCP : déboguer avec un agent (Claude Code)

`python -m gdbtrace.mcp` (ou `gdbtrace-mcp`) est un serveur [MCP](https://modelcontextprotocol.io)
sur stdio : un agent comme Claude Code ouvre une session GdbTrace sans interface et la pilote
par des outils. **Clé en main dans ce dépôt** : le `.mcp.json` de la racine déclare le
serveur ; ouvrir Claude Code dans le dépôt, approuver le serveur `gdbtrace`, et demander
(« connecte-toi à examples/firmware/demo.elf avec la trace simulée et dis-moi qui appelle
fib »). Avant, vérifier l'environnement :

```bash
.venv/bin/python -m gdbtrace.mcp --check        # gdb, QEMU, compilateur, sondes, démos
```

Pour un autre projet : `claude mcp add gdbtrace -s user -- /chemin/gdbtrace/.venv/bin/python
-m gdbtrace.mcp --root /chemin/projet` (chemins des outils relatifs au projet).

L'agent commence par l'outil `connect`, avec les options de la ligne de commande
(`examples/firmware/demo.elf --etm-sim`, `fw.elf --openocd configs/nucleo-f746zg.cfg --program
--swo-config --cpu-hz 16000000`, `--pid 1234`…), puis :

| Outils | Rôle |
| --- | --- |
| `connect`, `disconnect`, `status`, `check` | session (une à la fois), état et dernier arrêt ; bilan de l'environnement |
| `go`, `go_till`, `halt`, `step`, `step_until`, `reset` | run-control, Go.Till, Step.Till / Step.Change |
| `break`, `watch`, `enable`, `delete`, `breakpoints` | breakpoints et watchpoints (condition, compteur, message) |
| `evaluate`, `set`, `type`, `registers`, `backtrace`, `disassemble` | données ; `evaluate` relit en marche une globale déjà évaluée |
| `memory`, `memory_write`, `memory_fill`, `memory_find`, `memory_file` | mémoire : dump, écriture, remplissage, recherche, fichiers |
| `symbols`, `add_symbols`, `peripheral` | symboles, autres ELF, périphériques d'après le SVD |
| `threads`, `rtos`, `task`, `queues`, `task_times` | threads, tâches et objets FreeRTOS, temps par tâche |
| `trace_list`, `trace_find`, `trace_export`, `calls`, `etm_filter`, `coverage`, `timing`, `call_tree`, `irq_stats` | trace et analyses |
| `var_trace`, `snoop`, `var_samples`, `chrono`, `counters`, `profile`, `itm`, `terminal`, `fault` | mesures, variables dans le temps, sorties du programme, fautes |
| `program`, `gdb` (commande CLI brute), `python` (code avec l'objet `gdbtrace`) | le reste |

Chaque outil porte un titre et ses annotations MCP (lecture seule, destructif). Ce que
GdbTrace affiche (journal de mise en route, messages des breakpoints à action, sortie d'un
programme de l'hôte) revient dans le résultat des outils ; stdout ne porte que le protocole.
Guide pas à pas et dépannage : [`docs/utilisation.md`](docs/utilisation.md#serveur-mcp-claude-code).

## Architecture

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
  trace/mtb.py          MTB des Cortex-M0+ : paquets, registres, tables ROM, décodeur, encodeur
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

## Tests

```bash
xvfb-run -a .venv/bin/python -m pytest
```

Les tests lancent QEMU (`mps2-an385`) avec le firmware de démo. Sans `DISPLAY`, les tests GUI sont ignorés.
Le client TCL OpenOCD est testé contre un faux serveur TCL, et contre un vrai OpenOCD lancé sans
sonde (`noinit`) s'il est installé ; il a été utilisé sur une Nucleo-F746ZG. Validés sur cette
carte (test sur carte, ci-dessous) : capture SWO et ITM, ETMv4 par SWO, trace de données du DWT,
profil par SWO, vue RTOS et temps des tâches ; et sur une Nucleo-F446RE, l'ETMv3 par SWO.
Restent à valider sur sonde réelle : le support RTOS d'OpenOCD (`--rtos`), `DWT_PCSR` via
OpenOCD, le port de trace parallèle et la MTB d'un M0+.
Les tests ETM comparent notre décodeur à OpenCSD si `tools/build_opencsd.sh` a été lancé
(sinon ce test est ignoré) ; la trace ETM nécessite `pip install capstone` (extra `etm`).

### Tests sur carte (à chaque release)

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

- La carte est trouvée seule : sonde ST-LINK lue dans `/sys`, puis identité de la puce lue
  sans reset, par OpenOCD (F746, F446, G070, F072) ou par ST-LINK_gdbserver (N6, qu'OpenOCD
  0.12 ne connaît pas). `GDBTRACE_F746_SERIAL` / `GDBTRACE_F446_SERIAL` /
  `GDBTRACE_N6_SERIAL` / `GDBTRACE_G070_SERIAL` / `GDBTRACE_F072_SERIAL` imposent la sonde ;
  le SVD vient de
  STM32CubeIDE (`GDBTRACE_SVD_DIR` sinon).
- `pytest --boards=auto` (défaut) ignore ces tests sans la carte ; `--boards=require` les fait
  échouer ; `--boards=skip` les désactive.
- Le test remplace le firmware de la carte, et la laisse sans ETM ni stall.
