Un agent peut déboguer à votre place, ou avec vous : `python -m gdbtrace.mcp` est un serveur
MCP (Model Context Protocol) sur stdio. L'agent ouvre une session GdbTrace sans interface,
comme le mode batch, et la pilote par 62 outils.

### Mise en route pas à pas

1. **Installer** GdbTrace dans le venv du dépôt, les outils externes, et construire les démos :

   ```bash
   python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
   ```

   ```bash
   sudo apt install gdb-multiarch qemu-system-arm gcc-arm-none-eabi openocd
   ```

   ```bash
   make -C examples/firmware && make -C examples/freertos
   ```

2. **Vérifier** l'environnement : chaque ligne dit ce qui est trouvé (✓) ou ce qui manque et
   à quoi il sert (✗) ; code de retour 0 si une session est possible (gdb présent).

   ```bash
   .venv/bin/python -m gdbtrace.mcp --check
   ```

3. **Ouvrir Claude Code dans le dépôt.** Le fichier `.mcp.json` de la racine déclare le
   serveur `gdbtrace` (portée projet). Il le lance par `sh`, qui trouve au lancement :
   - le projet : `CLAUDE_PROJECT_DIR` (posée par Claude Code), sinon le répertoire de
     lancement ; il devient le répertoire courant et le `--root` du serveur ;
   - l'interpréteur : `GDBTRACE_PYTHON` s'il est posé (venv hors du dépôt), sinon
     `.venv/bin/python` du projet, sinon `python3`.

   Aucune variable n'est à développer par Claude Code dans ce fichier. Il faut `sh` : sous
   Windows, déclarer le serveur par `claude mcp add` (étape 5).

   Au premier lancement, Claude Code demande d'approuver ce serveur de projet : accepter.
   `/mcp` dans Claude Code (ou `claude mcp list` dans un terminal) le montre connecté.
   Après un refus, `claude mcp reset-project-choices` repose la question.

4. **Demander.** Par exemple : « Connecte-toi à examples/firmware/demo.elf avec la trace ETM
   simulée, arrête-toi dans update_sensor et dis-moi quelles fonctions ont tourné et combien
   de temps », ou « Pose un breakpoint sur fib avec la condition n == 7, puis montre la pile et
   le désassemblage ». L'agent enchaîne `connect`, `break`, `go`, `backtrace`, `trace_list`,
   `timing`…

5. **Ailleurs que dans ce dépôt** (votre firmware dans un autre projet) : déclarer le serveur
   pour l'utilisateur, avec le projet en `--root` :

   ```bash
   claude mcp add gdbtrace -s user -- /chemin/gdbtrace/.venv/bin/python -m gdbtrace.mcp --root /chemin/projet
   ```

   Sans `--root`, le serveur prend `CLAUDE_PROJECT_DIR` (posée par Claude Code), sinon son
   répertoire courant. Claude Desktop : même commande dans `claude_desktop_config.json`
   (`"command"` : le Python du venv ; `"args"` : `["-m", "gdbtrace.mcp", "--root",
   "/chemin/projet"]`).

**Chemins.** Les chemins donnés aux outils (ELF, `--config`, SVD, fichiers de `memory_file`,
`trace_export`, `coverage`) sont relatifs au répertoire du projet ; `check` et l'invite de
l'agent le rappellent.

### Outils

**Session.** L'outil `connect` prend les options de la ligne de commande ([Référence des options](Référence-des-options)), sans
`--batch`, `--script` ni `--coverage` : `examples/firmware/demo.elf --etm-sim`, `fw.elf
--openocd configs/nucleo-f746zg.cfg --program --swo-config --cpu-hz 16000000`, `fw.elf
--stlink --stlink-ap 1 --ram-boot --program`, `--config configs/qemu-freertos.yaml`, `--pid
1234`. Il rend le journal de mise en route et l'arrêt courant. Une session à la fois : un
nouveau `connect` ferme la précédente.

| Outil | Effet |
| --- | --- |
| `connect`, `disconnect` (`leave_running`), `status` | ouvrir, fermer la session ; état, dernier arrêt, état de la trace |
| `check` | bilan de l'environnement (gdb, QEMU, compilateur, OpenOCD, ST-LINK_gdbserver, modules, démos), sans session |
| `go` | Go, `until` : jusqu'à un emplacement ; attend l'arrêt `wait` s (5 par défaut, 0 : sans attendre), sinon la cible reste en marche |
| `go_till` | Go.Till : en temps réel jusqu'à ce que `condition` devienne vraie (watchpoint matériel) |
| `halt`, `reset` | Break ; reset arrêté au vecteur de reset (programme de l'hôte : relancé) |
| `step` | `kind` : `line`, `over`, `out`, `instruction` ; `count` pas |
| `step_until` | pas à pas jusqu'à `condition` (Step.Till, registres compris) ou jusqu'au changement de `change` (Step.Change) ; `asm`, `max_steps` |
| `break` | breakpoint : `condition`, `count` (arrêt au Nᵉ passage), `log` (message à chaque passage, la cible repart), `hardware`, `temporary` |
| `watch`, `enable`, `delete`, `breakpoints` | watchpoint (`write`, `read`, `rw`), activer ou désactiver (`on`), supprimer, liste avec les passages et les derniers messages |
| `evaluate`, `set`, `type` | expression C (`format` : `hex`, `dec`, `bin`…), affectation, type (champs, taille, adresse) |
| `registers`, `backtrace`, `disassemble` | registres, pile avec les locales ; désassemblage d'une fonction, d'une adresse (`count`) ou d'une plage `début..fin`, PC marqué |
| `memory`, `memory_write` | dump (`address` : nombre ou expression, 4096 octets au plus) ; écriture d'octets (`data`) ou d'un entier (`value`, `size`) |
| `memory_fill`, `memory_find`, `memory_file` | Data.Set, Data.Find (motif entier, octets ou texte), et `save` / `load` / `compare` avec un fichier (.bin, .hex, .srec, ELF) |
| `symbols`, `add_symbols` | fonctions et variables de l'ELF ; symboles d'un autre ELF (`offset`) |
| `peripheral` | registres décodés d'un périphérique d'après le SVD (`svd` : le charger) ; sans `name` : la liste |
| `threads` (`select`), `rtos`, `task`, `queues`, `task_times` | threads d'un programme de l'hôte ; tâches FreeRTOS, contexte d'une tâche, queues et mutex, temps par tâche |
| `trace_list`, `trace_find`, `trace_export`, `calls` | trace d'instructions de l'exécution qui précède l'arrêt (décodée avant d'être rendue) : listing, recherche, export CSV, flot d'appels |
| `etm_filter`, `coverage` | filtre de la trace ETM (`ranges`, `start`, `stop`) ; couverture, `export` (.html, .info, .json), `reset` |
| `timing`, `call_tree`, `irq_stats` | durées par fonction, arbre d'appels, statistiques des exceptions |
| `var_trace`, `snoop`, `var_samples` | variable tracée par le DWT, ou échantillonnée par le canal mémoire ; ses valeurs |
| `chrono`, `counters`, `profile`, `itm`, `terminal` | temps de la dernière exécution, compteurs de performance, profil statistique, texte ITM, terminal (semihosting, RTT) |
| `fault` | analyse de la faute en cours ; `catch` : arrêt sur faute |
| `coresight`, `mpu`, `chip` | composants des tables ROM CoreSight (ETM présent ou non) ; régions de la MPU (`nonsecure` : la MPU non sécurisée d'un cœur TrustZone) ; puce du CMSIS-Pack et cœur lu sur la cible |
| `security` | TrustZone : monde du cœur arrêté, registres bancaires, SAU, pile à travers les deux mondes ([TrustZone et multi-cœur](TrustZone-et-multi-cœur)) |
| `core` | plusieurs cœurs (`--core` à `connect`) : sans `name`, la liste ; avec `name`, le cœur de toutes les commandes suivantes |
| `program`, `flash_info`, `flash_erase` | programmer un ELF (celui de la session par défaut ; par les chargeurs de flash posés) ; chargeurs de flash ; effacement de secteurs |
| `gdb`, `python` | commande CLI de gdb ; code Python avec l'objet `gdbtrace` (tout ce que les autres outils ne couvrent pas, [Scripting Python](Scripting)) |

Chaque outil porte un titre et ses annotations MCP : lecture seule (`status`, `evaluate`,
`memory`, `disassemble`, la trace…), ou écriture dans la cible (`memory_write`,
`memory_fill`, `memory_file`, `program`, `reset`, `gdb`, `python` : dits destructifs). Le
client peut s'en servir pour ses demandes de permission.

**Cible en marche.** gdb ne lit rien pendant la marche ; `evaluate` relit alors par le canal
mémoire (QEMU, OpenOCD, ST-LINK_gdbserver, programme de l'hôte) une variable à adresse fixe
(globale, champ, élément de tableau) que l'agent a déjà évaluée une fois cible arrêtée :
« `ticks = 1234 (lu en direct)` ». `memory` et `memory_find` lisent une adresse numérique ;
les écritures, le désassemblage et les filtres demandent la cible arrêtée (sinon l'outil le
dit : `halt` d'abord).

**Sorties.** Tout ce que GdbTrace écrit revient dans le résultat de l'outil : journal de
`connect`, `print` du code Python, messages des breakpoints à action et sortie d'un programme
de l'hôte (aussi ceux arrivés entre deux appels, rendus au suivant). Une erreur (expression
inconnue, cible en marche) est un résultat d'erreur ; la session continue.

### Dépannage

| Symptôme | Cause, remède |
| --- | --- |
| `/mcp` : gdbtrace « failed » | venv absent ou incomplet (étape 1) ; lancer à la main `.venv/bin/python -m gdbtrace.mcp --check` ; venv ailleurs que dans le dépôt : `GDBTRACE_PYTHON=/chemin/venv/bin/python` dans l'environnement de Claude Code |
| gdbtrace absent de `/mcp` | serveur de projet refusé : `claude mcp reset-project-choices`, puis relancer Claude Code dans le dépôt |
| `connect` : « ELF introuvable » | chemin relatif au projet (`check` donne le répertoire), ou `--root` à corriger |
| `connect` : QEMU, gdb ou OpenOCD introuvable | `check` dit ce qui manque et à quoi il sert |
| `connect` : sonde déjà prise | une autre session (interface, OpenOCD lancé à la main) tient la sonde : la fermer |
| « cible en marche : `halt` d'abord » | l'outil lit ou écrit par gdb, qui ne le fait que cible arrêtée |

## Pour aller plus loin

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
Guide pas à pas et dépannage : [Serveur MCP](Serveur-MCP).
