# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Projet

GdbTrace (paquet `gdbtrace`) : débogueur Cortex-M avec trace, construit sur gdb (GDB/MI), GUI tkinter. Tout
le projet est **en français** : docstrings, commentaires, libellés d'interface, messages
d'erreur, README, wiki et messages de commit. Les commentaires citent souvent le nom de commande
GdbTrace (Data.dump, Var.view, PER.view, Trace.List…) : garder ce vocabulaire. Le produit
s'appelle **GdbTrace** : ne citer aucun autre débogueur commercial dans le code ni la doc.

Le développement avance par jalons (page `Documentations/Jalons.md` du wiki). Messages de commit :
`Jalon N, étape M : résumé`, puis une liste à puces par module touché.

## Commandes

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'   # dev = pytest + capstone (ETM)
make -C examples/firmware        # demo.elf (Cortex-M3, QEMU mps2-an385)
make -C examples/freertos        # freertos.elf
make -C examples/m0plus          # demo.elf Cortex-M0+ (QEMU -M microbit), tampon MTB réservé ; freertos.elf (port ARM_CM0)
make -C examples/nucleo-n6       # firmware de test STM32N6 (M55, en RAM, boucles v8.1-M, tampon ETR)
make -C examples/nucleo-g070     # firmware de test STM32G070RB (M0+, flash, sans ITM)
make -C examples/nucleo-f072     # firmware de test STM32F072RB (M0, 8 MHz ; démarrage et config de nucleo-g070)
make -C examples/nucleo-f446     # firmware de test STM32F446RE (M4F, flash, ETMv3 par SWO)
make -C examples/flashalgo       # algorithmes de flash de test (FLM, .stldr) et pack GdbTrace.QEMU_DFP
make -C examples/tz-an505        # TrustZone (QEMU mps2-an505) : secure.elf + nonsecure.elf (--qemu-load)
make -C examples/multicore-an521 # deux Cortex-M33 (QEMU mps2-an521, --core cpu1=2)
.venv/bin/python -m gdbtrace examples/firmware/demo.elf               # GUI sur QEMU
.venv/bin/python -m gdbtrace examples/firmware/demo.elf --etm-sim     # + trace ETM simulée
.venv/bin/python -m gdbtrace fw.elf --batch --script s.py             # sans GUI
.venv/bin/python -m gdbtrace --config configs/qemu-freertos.yaml      # session décrite en YAML
.venv/bin/python -m gdbtrace.mcp                                       # serveur MCP (stdio)
.venv/bin/python -m gdbtrace.mcp --check                               # bilan de l'environnement

xvfb-run -a .venv/bin/python -m pytest                 # suite complète
.venv/bin/python -m pytest tests/test_etm.py -k rtos   # sous-ensemble
.venv/bin/python -m pytest tests/test_etm.py::test_roundtrip_matches_qemu
tools/build_opencsd.sh           # oracle OpenCSD dans .cache/ (active test_opencsd_*)
tools/release_check.sh           # release : toute la suite, cartes de test exigées
git submodule update --init      # Documentations/ : le wiki GitHub (sous-module)
tools/publish_wiki.sh "message"  # pages modifiées : commit et push du wiki, pointeur indexé
tools/publish_wiki.sh --pull     # pages modifiées sur GitHub : sous-module mis à jour
```

Outils externes : `qemu-system-arm`, `gdb-multiarch`, `arm-none-eabi-gcc`, `xvfb-run`. Les
tests qui en ont besoin sont **ignorés** (skip) s'ils manquent, comme les tests GUI sans
`DISPLAY` et le test OpenCSD sans `.cache/opencsd` : un « passed » peut donc masquer des
tests non exécutés, vérifier les skips. Les fixtures `elf` / `rtos_elf` de
`tests/conftest.py` recompilent les firmwares (`make`) ; `session` / `rtos_session` lancent
QEMU sur un port libre. `pythonpath` inclut `tests/`, d'où `from conftest import …`.
Aucun linter n'est configuré.

Documentation : `Documentations/` est le wiki GitHub lui-même, en **sous-module** (URL
relative `../gdbtrace.wiki.git`, branche `master`) : Home, _Sidebar, une page par thème
(Lancer-GdbTrace, Trace-ETM, Scripting, Serveur-MCP, Référence-des-options…), liens entre
pages en `[texte](Nom-de-page)`. Le README reste court et renvoie au wiki. Une page modifiée
se publie en deux commits : dans le sous-module (poussé vers le wiki **avant** le dépôt,
`tools/publish_wiki.sh "message"`, ou `git push --recurse-submodules=on-demand`), puis le
pointeur du sous-module dans le commit du dépôt. Pages éditées sur GitHub :
`tools/publish_wiki.sh --pull`. Sans le sous-module, `tests/test_docs.py` est ignoré. Le wiki
ne s'atteint pas depuis une session cloud : elle ne peut ni lire ni modifier la doc, et dit
dans son compte rendu les pages à mettre à jour depuis le poste de l'utilisateur.
`git pull --rebase origin main` avant chaque commit : les commits qui arrivent sur le distant
viennent des tests sur carte.

Tests sur carte (`tests/test_board_f746.py`, Nucleo-F746ZG, firmware `examples/nucleo-f746`
flashé ; `tests/test_board_n6.py`, Nucleo-N657X0-Q, `examples/nucleo-n6` chargé en RAM par
ST-LINK_gdbserver ; `tests/test_board_m0.py`, Nucleo-G070RB et Nucleo-F072RB, fixture
paramétrée par carte (`-k g070`, `-k f072`), `examples/nucleo-g070` / `nucleo-f072` flashés,
sans trace, run-control et chrono éprouvés dans `extra`, temps des tâches par le journal en
RAM ; `tests/test_board_f446.py`, Nucleo-F446RE,
`examples/nucleo-f446` flashé, ETMv3 par SWO en start/stop sur `worker_step`, plus un filtre
par plage en batch comparé à OpenCSD). Chaque test sur carte lance la session de sa
configuration (`--config configs/<carte>.yaml`) ; `tests/test_config.py` vérifie qu'elle
redonne exactement les arguments de la session de test. Options : `--boards=auto|require|skip` (`tests/boards.py` trouve la sonde par
`/sys` puis l'IDCODE, lu par OpenOCD ou ST-LINK_gdbserver). Le GUI tourne par le vrai
`main()`, `App.mainloop` remplacé par le scénario commun `boards.run_gui` ; le contenu de
chaque vue est relevé (`boards.dump_views`, dans `tests/artifacts/board-<carte>.json`) puis
vérifié vue par vue, avec la latence de la boucle Tk. Les tests laissent la carte sans ETM,
mais avec son firmware de test.

## Architecture

### Couches

`GdbMI` (`core/gdb_backend.py`) → `Session` (`core/session.py`) → consommateurs :
`App` (GUI), `ScriptAPI` (objet `gdbtrace` de la console et des scripts), mode `--batch`
(`__main__.py`), serveur MCP (`mcp.py`).

- `BatchSession` (`__main__.py`) : session sans GUI (connexion, programmation, trace,
  `ScriptAPI`), commune à `--batch` et au serveur MCP ; `check_args` contrôle les options
  pour les deux. Serveur MCP : JSON-RPC sur stdio, une ligne par message, une session à la
  fois ouverte par l'outil `connect` (options de la ligne de commande). Les descripteurs 0 et
  1 sont dupliqués pour le protocole puis remplacés (/dev/null, stderr) : un fils (gdb, QEMU)
  ne doit ni lire les requêtes ni écrire dans les réponses. Chaque outil capture stdout/stderr
  dans son résultat ; les événements du thread lecteur (actions, sortie du programme) sont
  gardés pour le résultat suivant. Un outil sur une méthode de `ScriptAPI` est déclaré dans
  `TOOLS` (`_tool` : titre, annotations `read_only` / `destructive`) et implémenté par
  `tool_<nom>`, mêmes paramètres que le schéma (vérifié par `tests/test_mcp.py`). Le
  `.mcp.json` du dépôt le lance par `sh -c` (pas de `${…}` : Claude Code ne les développe pas
  toujours) : projet = `$CLAUDE_PROJECT_DIR` sinon `$PWD`, interpréteur = `$GDBTRACE_PYTHON`,
  sinon le venv du dépôt, sinon `python3` ; `--root` : le répertoire du projet devient le
  répertoire courant (chemins des outils relatifs à lui) ;
  `--check` / outil `check` : bilan de l'environnement (`environment_report`).

- `GdbMI` : un thread lecteur ; chaque commande porte un jeton et attend sa réponse dans une
  queue. Les abonnés sont appelés **dans le thread lecteur** et ne doivent jamais bloquer.
  `console()` capture la sortie CLI sans la diffuser (commandes internes `ptype`,
  `info line`…) et sérialise les appels.
- `Session` : utilisable depuis n'importe quel thread ; état `disconnected | stopped |
  running` ; événements via `on_event(kind, data)` ; `add_resume_hook()` est appelé avant
  **toute** reprise (go, step, next, finish, until), quel que soit l'appelant.
  Arrêts internes (MTB en AUTOHALT) : `set_stop_interceptor(fn)` peut déclarer interne un arrêt
  `signal-received` **non demandé pendant un Go** (jamais après `halt()`, ni pendant un pas)
  — aucun événement, état « running » ; le propriétaire vide la MTB puis appelle
  `resume_internal()` (relance silencieuse) ou `deliver_stop()`. L'intercepteur tourne dans le
  thread lecteur : il ne fait que lancer un fil de travail.
  Breakpoints à action (`BreakAction`, `break_options`) : un arrêt sur un breakpoint ou
  watchpoint à action pendant un Go est interne aussi. L'action tourne dans un fil
  `gdbtrace-action`, puis `resume_internal()` ou `deliver_stop()`.
  - Tant que des actions existent, la sortie console de gdb est retenue pendant la marche :
    la bannière d'un arrêt d'action est jetée, le reste est livré à l'arrêt visible.
  - Watchpoint de Go.Till : transitoire (`_transient`), masqué de `breakpoints()` et
    supprimé à la reprise suivante.
  - `step_till` / `step_change` (`_step_loop`) font une seule reprise : arrêts intermédiaires
    silencieux (`_looping`, `state` reste « running »), un `running` et un `stopped`.
- `TargetServer` (`core/target.py`) : `QemuTarget` (défaut, charge l'ELF lui-même,
  `needs_load=False`), `OpenOcdTarget`, `StLinkTarget`, `RemoteTarget` (+ `--tcl` optionnel), et
  `NativeTarget` (`target.native`) : programme de l'hôte exécuté par gdb lui-même
  (`-exec-run --start`, arrêt à `main`), dans son propre pseudo-terminal (sinon il partage
  les tubes MI de gdb), état `exited` à sa fin, `restart()`. `live_memory()` fournit le canal
  mémoire latéral (natif : `/proc/<pid>/mem`), ou `None`. `attach_pid` (`--pid`) :
  `-target-attach`, pas de relance, détaché à la déconnexion. `NativeRemoteTarget` (programme
  natif derrière gdbserver) : ne jamais sonder le port (gdbserver garde la sonde comme client) ;
  préchargé (`gdbserver :port prog`) → jusqu'à `main`, sinon `--multi` → `set remote exec-file`.
  Un OpenOCD qui ne démarre pas est expliqué d'après son journal (`explain_openocd_failure` :
  mauvaise sonde — IDCODE inattendu —, sonde déjà prise, numéro de série inconnu), avec
  les sondes ST-LINK branchées (`stlink_probes`, lues dans /sys).
- Non-stop (`non_stop`, natif) : `-gdb-set non-stop on` avant tout lancement ; un *running /
  *stopped par thread (`Session._on_mi_nonstop`, `thread_states`). L'état de la session est
  celui du thread **suivi** (`focus`) ; gdb ne change pas de thread courant quand un autre
  s'arrête, et `--thread` le change : `select_focus()` avant tout rafraîchissement des vues
  (`ViewManager.refresh_all`) et dans `wait_stopped`. Go / Break : `--all`.
- `StLinkTarget` : ST-LINK_gdbserver (lancé `-e -g`, prêt quand il écrit « Waiting for
  debugger connection » : ne jamais sonder son port, une connexion arrête le cœur) derrière
  `RspRelay` (`core/rsp_relay.py`), auquel gdb se connecte. Le relais glisse ses propres
  paquets `m`/`M` (`RelayMemory`, le canal en direct) quand gdb n'attend rien ou que la cible
  tourne, retient gdb pendant ce temps, retire les paquets « O » hors d'une marche, et fait le
  reset système (AIRCR, détachement, reconnexion, poignée de main de gdb rejouée : après
  SYSRESETREQ le serveur refuse toute reprise). `ram_boot` (STM32N6) : reset, puis `load` et
  VTOR/sp/pc pris dans la table des vecteurs (`after_load`, aussi après le `load` de la
  connexion). `verify_by_read` : le CRC de `compare-sections` est faux sur un N6.
- Configurations (`core/config.py`, `SessionConfig`) : le YAML (sections `cible`, `serveur`,
  `os`, `script`, `couverture`, `trace` ; clés = noms des options sans tirets) **se traduit en
  arguments** de `build_parser()` (`to_argv`), puis passe par `validate(args)` (règles entre
  options, communes à la ligne de commande et aux configurations), `build_target`,
  `setup_trace`. `parse_command_line` fusionne `--config` et la ligne de commande (valeur
  remplacée, liste complétée, serveur d'un autre type remplacé avec `SERVER_OPTION_DESTS`).
  Chemins relatifs au YAML, sauf `.cfg` OpenOCD et SVD par son nom (`svd.locate` /
  `find_svd`, STM32CubeIDE) s'ils n'y sont pas. Le GUI : `start_gui_session(app, args)` est le
  seul chemin de lancement (`main()` et Configuration › Connecter) ; ouvrir une configuration
  (`App.apply_config`) ne connecte pas ; `App.apply_os_views` (FreeRTOS : rtos, tasktime ;
  Pthread : threads) et `os_hidden` retiré de `known_kinds` du layout ; un Connecter en
  attente est annulé par Fermer (`_start_token`). Pendant `App.connect`, les arrêts de la
  connexion (arrivée, programmation) ne rafraîchissent pas les vues (`_connecting`) : elles
  n'ont pas encore le programme (vue Mémoire en 0x20000000, illisible sur une N6) ; `done`
  rafraîchit tout, `_end_connect` aussi en cas d'échec. Boîte de dialogue : `gui/config_dialog.py`
  (`ConfigEditor`, non bloquante, `on_ok`).
- Fautes (`core/fault.py`) : cadre empilé et EXC_RETURN pris au pseudo-cadre « <signal handler
  called> » de gdb (`$sp` = base, `$pc` = EXC_RETURN, MI `--thread --frame`) ; ARMv6-M et
  ARMv8-M Baseline (CPUID) : jamais lire CFSR/HFSR (zone réservée), cause déduite de
  l'instruction. `FaultCatch` : vector catch (DEMCR, `cortex_m vector_catch` d'OpenOCD), sinon
  breakpoints sur les gestionnaires de la table des vecteurs (QEMU : DEMCR lu à 0). Vue Faute,
  `App.report_fault` à chaque arrêt (une lecture d'ICSR hors faute).
- TrustZone (`core/security.py`, jalon 23) : monde du cœur par `sp` comparé à `msp_s`/`psp_s`/
  `msp_ns`/`psp_ns`, DSCSR.CDS pour départager (QEMU : 0) ; `App.security` à chaque arrêt
  (barre d'état, vues Registres, Pile, Système). gdb s'arrête au changement de monde
  (« previous frame inner » après un SG, pseudo-cadre FNC_RETURN) : `world_backtrace`
  écrit les registres du cadre (`_context`, remis ensuite) et redemande la pile ; appelant
  sécurisé pris dans le cadre FNC_RETURN ; depuis le non sécurisé sous QEMU, mémoire
  sécurisée par `QemuQmp.read_physical` (xp). SAU (RNR comme la MPU), MPU non sécurisée par
  l'alias 0xE002ED90, `fault` : cadre sur la pile du monde interrompu (EXC_RETURN.S).
- Plusieurs cœurs (`core/cores.py`) : `--core NOM[=N|=HÔTE:PORT][,ELF]` ; processus N du
  même serveur = inférieur du même gdb (`Session.add_process`, `schedule-multiple`,
  `scheduler-locking step`) ; autre serveur = sa `Session` (pas de non-stop chez QEMU ni
  OpenOCD). `App.select_core` remplace `app.session` (et `api.s`) par celle du cœur ;
  `App._session_event` ne livre aux vues que les événements de la session affichée. Trace :
  cœur de la session ; `qemu_log.parse(cpu=0)` ne garde que les lignes « Trace 0: ».
- Puces (jalon 22) :
  - Tables ROM CoreSight (`trace/coresight.py`, `TraceManager.discover` à la connexion, chemins
    GUI et batch) : composants nommés par DEVARCH, pièce ARM (JEP106 0x23B) ou DEVTYPE ;
    `detect_etm` prend la version de l'ETM listé ; table sans ETM → `no_etm_reason`,
    `_require_etm` (pas de table, QEMU : rien ne change). `trace/mtb.py` réexporte le parcours.
  - MPU (`core/mpu.py`) : RNR écrit puis restauré ; écriture ignorée (QEMU 8.2) → régions
    lisibles sans elle (alias A1..A3 en ARMv8-M), dit ; `fault._mpu_cause` (MemManage).
  - Chargeur de flash (`core/flashalgo.py`) : FLM (PrgCode/PrgData, r9 = données) et `.stldr`
    (segments à leur adresse ; sans Read, relu après Init, qui remet la mémoire en mode mappé) appelés par `Session.target_calls` (r0-r3, r9, sp, lr = BKPT de
    retour + breakpoint masqué, matériel si le FPB l'atteint : `_hw_breakpoint_reaches`, FPB rév. 1
    des M3/M4 et BPU des M0/M0+ sous 0x20000000 seulement, sinon logiciel ; PRIMASK à 1, `_looping` : ni événement ni resume
    hook) ; `Session.flash_loaders` : `program`/`program_image` y envoient leur mémoire
    (`split_segments`), le reste par `load` ; posés par `build_flash_loaders` (`__main__.py`,
    avant la programmation, batch et `App.flash_loader_factory`).
  - CMSIS-Pack (`core/pack.py`) : héritage famille → variante, packs installés, fichiers
    extraits dans `~/.cache/gdbtrace/packs` ; `resolve_session` pose `args.chip` et le SVD du
    pack ; `chip_core_warning` à la connexion. `gdbtrace.chip` (pas `device` : c'est le SVD).
  - Vue Système (`views/system.py`) : onglets CoreSight, MPU (relue à chaque arrêt), Puce.
- Images et symboles ajoutés : `core/memtools.py` (HEX, S-record, binaire `f.bin@adr`, ELF par
  ses segments, outils Data.*) ; `Session.program` d'une image = `load` gdb d'un Intel HEX
  temporaire, vérifié par relecture, symboles inchangés ; `Session.add_symbols`
  (`extra_symbols`, rejoués après `-file-exec-and-symbols`) ; `SymbolTable(elf, extra)` et
  `CodeImage(elf, extra)` réunissent les ELF ajoutés (profil, trace, couverture).
- Terminal (`core/terminal.py`, `ScriptAPI.term`) : semihosting servi par le serveur sur une
  socket (`TcpSource` : QEMU `-semihosting-config target=native,chardev`, OpenOCD
  `arm semihosting_redirect tcp`) ; RTT (`RttChannel`) lu par le canal mémoire en marche s'il
  écrit (`LiveMemory.can_write` : OpenOCD TCL, relais ST-LINK ; pas QMP), par gdb à l'arrêt.
  `setup_terminal` (`__main__.py`), commun aux deux chemins comme `setup_trace`.
- Programme natif PIE : les adresses de `SymbolTable` (ELF) ne sont pas celles de l'exécution
  (relocation) ; demander l'adresse à gdb (`address_of_expr`). La trace ETM et le Cortex-M
  (SVD, RTOS, reset par vecteurs) ne s'appliquent pas (`App.native`).

### Threads et tkinter

tkinter n'est pas thread-safe. Tout ce qui vient d'un autre thread passe par
`EventPump.post(fn, …)` ; les actions longues par `pump.background(fn, then=…)` ou
`App.run_action()`. `App.safe()` / `report_error()` envoient les exceptions vers la console
et la barre d'état. Les `except Exception` larges (`# noqa: BLE001`) sont voulus : un abonné
ou un callback défaillant ne doit pas tuer un thread de travail. Un flux continu (trace en
marche) ne doit jamais poster un appel par paquet : regrouper (lignes ITM :
`TraceManager._line`, notifications ETM : `_etm_changed`) ; la pompe ne draine que 100 ms
par tour. Un fil de calcul Python prend le GIL au thread GUI : chaque appel Tk attend
ensuite l'intervalle de bascule (réduit à 0,5 ms par `App`). Donc peu d'appels Tk par rendu
(une image pour la chronologie des tâches, un seul `insert` par vue texte), pas de rendu
périodique d'une vue cachée (`View.shown`, `redraw_when_shown`), et pas de rendu complet de
la trace tant qu'elle change (`View.trace_settling`). Le décodage ETM cède la place quand
la pompe exécute des rappels (`EventPump.busy` → `TraceManager.gui_busy`). `App.busy`
(programmation de la flash) refuse les commandes de marche.

### Deux chemins vers la mémoire

En all-stop, gdb ne lit rien cible en marche. D'où un motif récurrent : **cible arrêtée**, on
demande une fois à gdb la disposition (adresses, offsets DWARF, tailles, types) ; **en
marche**, on lit des octets bruts par le canal latéral (`core/live.py` : OpenOCD TCL ou
QEMU QMP) et on décode sans gdb. Appliqué par `core/live_vars.py` (Watch en direct),
`core/rtos/freertos.py` (`FreeRtosLayout.from_session` puis `snapshot(read)`) et la vue
Périphériques (SVD). `LivePoller` regroupe les lectures de toutes les vues dans un seul
thread. Pour les résolutions adresse → symbole/ligne en masse (profil, trace), utiliser
`core/symbols.py` (pyelftools) et non gdb.

### Vues

Une vue hérite de `View` (`gui/view_base.py`) : `title`, `kind` (clé du layout), `refresh()`
(thread GUI, cible arrêtée), `on_running()`, `on_disconnected()`. Détacher ou déplacer une
vue la **détruit puis la recrée** (un widget tkinter ne change pas de parent) : tout état
utile doit passer par `get_state()` / `set_state()`. Une nouvelle vue s'enregistre dans
`view_classes()` et `DEFAULT_LAYOUT` (`gui/app.py`). Hooks optionnels appelés par `App`
s'ils existent, ex. `on_svd_loaded()`.

### Trace

`TraceManager` (`trace/manager.py`) possède les captures SWO et ETM, sans dépendre de la
GUI (aussi utilisé en batch ; `post` rappelle dans le bon thread).

- SWO : source TCP/fichier → `TraceSession` → déformateur TPIU (`tpiu.py`) → `ItmDecoder`
  (`itm.py`) → abonnés (texte ITM, profil, stats d'exceptions). Sur un port parallèle
  formaté, l'ID 1 va à l'ITM et l'ID 2 à l'ETM. Le TPIU d'un M4 n'émet sa synchro complète
  qu'au démarrage et à l'arrêt : sans elle (capture connectée après, octets perdus), le
  déformateur cherche l'alignement dans les données (ID de flux, puis erreurs ITM de chaque
  décalage, `TraceSession._itm_errors`) ; rien ne se distingue : il attend.
- Débit : un SWO saturé (2 Mbit/s, ETM-M7) arrive plus vite que le Python ne décode l'ETM.
  `TcpSwoSource` lit la socket dans un fil et décode dans un autre (sinon OpenOCD, monothread,
  se bloque en écrivant : lectures en direct en échec, SWO perdu, ITM corrompu) ; l'ETM a son
  propre fil de décodage (`TraceManager._etm_loop`) pour que l'ITM reste en direct. Les
  retards sont affichés (`status()`, `trace_status()`), `wait_etm()` attend la fin. Le flot
  ETMv4 mémorise les blocs de code et compte la couverture par bloc (`take_counts`) :
  rien ne doit parcourir les instructions une à une en Python sur ce chemin.
- Trace de données (`trace/datatrace.py`) : comparateurs du DWT (FUNCTION 0xD/0xC/0x2, +PC
  0xF/0xE/0x3) programmés par `TraceManager.trace_variable` (cible arrêtée, pris par le haut,
  partagés avec le filtre ETMv3 et celui du M55 : `EtmConfig.dwt_reserved` / `dwt_used`) ;
  `DataTraceSink`
  (abonné ITM) range les paquets en échantillons typés par variable ; vue `vartrace`.
- Temps par tâche (`trace/tasktrace.py`) : le firmware écrit le TCB entrant sur le port ITM 31
  (`traceTASK_SWITCHED_IN`, démo FreeRTOS) ; `TaskTraceSink` en tire tranches, brut/net (trace
  des exceptions du DWT) ; noms relus dans les TCB à l'affichage (`namer`), jamais depuis le
  fil de capture ; vue `tasktime`. Sans capture SWO (et sur un M0/M0+), le même crochet remplit
  l'anneau `gdbtrace_task_log` (horodatage SysTick + ticks, `main.c`) : `TaskLogReader` lit les
  seules entrées nouvelles (LivePoller en marche, gdb à l'arrêt), compte les pertes, déroule
  les horodatages 32 bits ; `TraceManager.task_log_active` : jamais avec l'ITM (mesure remise à
  zéro quand l'ITM prend le relais).
- SNOOPer (`core/snooper.py`, `TraceManager.snoop`) : variables lues à période fixe par le canal
  mémoire (`TraceManager.live`, posé par l'App et le batch), son propre fil (le LivePoller est à
  200 ms), versées dans le `DataTraceSink` comme `TracedVar` de numéro **négatif** (jamais un
  comparateur du DWT : `untrace_variable` les ignore), horloge de l'hôte ; vue `vartrace`
  (source « échantillonné », par défaut sans ITM ou sans capture SWO).
- Compteurs (`core/counters.py`, `ScriptAPI._counters`, vue `counters`) : armés à la demande
  (les compteurs du DWT émettent des paquets de débordement sur le SWO), lus au départ (resume
  hook, après le chrono) et à l'arrêt, jamais remis à zéro (DWT_CYCCNT sert au chrono) ;
  compteurs de 8 bits rattrapés par `EventCounterTally` (abonné ITM du `TraceManager`) pendant
  une capture, sinon « modulo 256 » ; PMU de l'ARMv8.1-M (M55 : 8 × 16 bits chaînés par paires,
  PMU_CCNTR = DWT_CYCCNT, PMU et compteurs de profil du DWT exclusifs) préférée au DWT.
- Chrono (`core/chrono.py`, posé par `ScriptAPI`) : resume hook au départ, mesure à l'arrêt
  visible (une par `Session.stop_count`) ; SysTick + compteur de ticks (`xTickCount`,
  `uwTick`, `ICSR.PENDSTSET`) sur un ARMv6-M ou sous QEMU, `DWT_CYCCNT` au-delà ; les arrêts
  internes ne passent pas par les hooks ; reset / programmation / connexion l'invalident.
- ETM (`trace/etm/`) : `etm3.py` (paquets ETMv3.5) → `flow.py` (reconstruction avec
  `image.py`, classification Thumb-2 par capstone ; chaque fonction est balayée d'un seul
  tenant pour connaître la condition des instructions d'un bloc IT — `bxeq lr` décodé seul
  paraît inconditionnel ; `Instr.op` = mnémonique sans condition) → `trace.py` (historique, couverture,
  appels). Les événements `Step` / `ExcEntry` / `ExcReturn` portent des cycles mesurés
  (cycle-accurate) ou estimés par `cycles.py` ; `trace/timing.py` en tire le temps brut/net
  par fonction et par exception (vue Timing), et par chemin d'appel (`_Frame.path` fixé à
  l'empilement, une exception est une racine ; `TimingResult.tree`, plafonds `MAX_DEPTH` et
  `max_nodes` → nœud « … » ; invariant : somme des nets de l'arbre = somme des nets par
  fonction). Exceptions : `trace/irqstats.py` (`IrqStats` : durée sans les imbriquées, période,
  gigue, préemptions, enchaînements), remplies par `TimingAnalyzer` (`TimingResult.irq`) et par
  `ExceptionStats` (ITM, `trace/swo.py`). Distributions (`trace/distribution.py`,
  `Reservoir` : compte/min/max exacts, échantillon uniforme à graine fixe, plus grandes valeurs
  avec leur position) : `TimingResult.durations` (brut de chaque appel complet, position =
  index du listing, marques de cycles comptées), tranches de `TaskTraceSink`, valeurs d'une
  variable tracée ; onglet Distribution, `gdbtrace.distribution()`, outil MCP ; la vue Trace
  va à une position (`TraceView.goto_index`, fenêtre `offset`). `listing.py` : les lignes de Trace.List
  (`TraceRow`), communes à la vue, à `trace_list`/`trace_find`/`trace_export` et au MCP ;
  `find_rows` (adresse, entrées de fonction, exceptions, texte), export CSV.
- ETMv4 (Cortex-M7, Cortex-M55) : `etm4.py` (paquets) → `flow4.py` (file de spéculation,
  parcours du code entre points de passage) ; `encoder4.py` pour les tests et l'ETM simulé.
  MAXSPEC (TRCIDR8 : M7 1, M55 0) et COMMOPT (TRCIDR0) passent au décodeur
  (`EtmTrace.set_max_spec`). Cycle-accurate ETMv4 : les paquets Cycle Count prennent place
  dans la file comme les adresses et deviennent des marques `CycleMark` (cycles mesurés depuis
  la précédente), l'horodatage des `TimeMark` ; `timing.measured_cycles` ramène les cycles
  estimés de chaque segment à la mesure. Tout consommateur d'événements doit laisser passer
  ces marques (`qemu_log.MARKS`) : ce ne sont pas des instructions. Filtre d'un ETMv4 sans
  comparateur d'adresse (M55 : TRCIDR4.NUMACPAIRS 0, NUMPC 8) : `EtmConfig._etm4_dwt_filter`,
  comparateurs du DWT en adresse d'instruction / limite, action « déclencheur », lus comme
  entrées de l'ETM (ressource TRCRSCTLR2 des deux entrées d'une paire, TRCVIPCSSCTLR pour
  start / stop) ; un filtre retiré coupe ses comparateurs. Image
  ARMv8-M (`CodeImage.arch`, Tag_CPU_arch de l'ELF) : capstone en mode V8, et `decode_v8m`
  avant capstone pour les boucles à faible coût (LE = branchement conditionnel arrière, WLS
  avant ; DLS/LCTP/BF* ordinaires), BXNS/BLXNS, CLRM ; une instruction illisible (Helium) y
  est ordinaire, de la taille de son encodage. Le balayage des blocs IT continue après une
  instruction que capstone ne lit pas.
- Cœur (`TraceManager.detect_core`, CPUID, à la connexion, chemins GUI et batch) : un
  ARMv6-M (M0/M0+) n'a ni ITM, ni SWO, ni ETM ; `_require_itm` refuse toute capture en le
  disant (`setup_error`), `no_trace_reason()` s'affiche dans les vues ITM, Trace, Couverture,
  Variables tracées, Temps des tâches ; `DWT_PCSR` (profil Live) existe sur le M0+ du G0.
  FreeRTOS ARM_CM0 depuis V11.3 : EXC_RETURN puis r4-r11 sur la pile (`port == "CM0"`,
  `vRestoreContextOfFirstTask` + CPUID). La frame factice de fin de tâche se retire par
  l'adresse de `prvTaskExitError` (gdb la nomme d'après la fonction qui la précède).
  Modèle de cycles par cœur (`etm/cycles.py`, `CycleModel`, `model_for`) : M3/M4 pour les
  décodeurs ETM (inchangé), M0 / M0+ (tables des TRM ARM) pour la MTB réelle (M0+) ou simulée
  (QEMU microbit : M0), choisi par `TraceManager._apply_cycle_model` d'après `detect_core`.
- Puce (`trace/chips.py`, reconnue par DBGMCU_IDCODE) : SWO hors du TPIU du cœur (N6 :
  composant SWO CoreSight, horloge cœur/8, `SwoConfig.chip`), horloges de trace, chaîne
  funnel/ETF/ETR. `trace/tmc.py` (`EtrCapture`, `TraceManager.start_etm_tmc`) : ETM écrit en
  RAM par l'ETR ; lu en continu par le canal live (`poll`, fil `etr-stream` arrêté avant la
  lecture du reste à l'arrêt ; écriture qui rattrape la lecture = zone jetée, `_ETM_GAP`),
  sinon à l'arrêt (cycle-accurate, `--etr-at-stop` : ETM coupé, ETF/ETR vidés) ; relancé à
  chaque reprise
  (attributs RIF du maître AXI et horloges réécrits : un reset système les efface) ; décodé
  par le fil ETM, un tampon plein = un `_GAP` dans la file. Trace de données sur un DWT
  ARMv8-M (`dwt_is_v8m`) : MATCH/ACTION/DATAVSIZE, pas de MASK. ITM : une page de ports
  au-delà de `ItmDecoder.max_page` (0 : 32 ports) est un octet perdu, ignorée.
  `EtmTrace(version=…)` choisit v3 ou v4 ; `TraceManager.detect_etm()` lit TRCIDR1 (sous
  QEMU : CPUID, M7 → v4) après la connexion, et `configure_etm()` suit `EtmConfig.version`.
- Filtre (`etm/filter.py`, `EtmConfig.filter`, `TraceManager.set_etm_filter`) : plages et
  start/stop ; ETMv4 par ses comparateurs d'adresse, ETMv3 par ceux du DWT (blocs alignés,
  `effective_filter()`). Une reprise de la trace est un événement `TraceGap` (I-sync raison
  1/2, Trace On) que tout consommateur d'événements doit traiter ; celle qui suit une simple
  reprise après arrêt est retirée par `EtmTrace.resumed()`. L'ETM simulé applique le filtre
  effectif au chemin QEMU (`EtmFilter.apply`) ; la couverture se limite aux plages (`scope`).
  ETMv3 matériel : l'événement du DWT a une instruction de retard, la trace se coupe et
  reprend à chaque branchement ; `FlowDecoder.heal_ranges` (blocs du DWT, posés par
  `configure_etm`, jamais en simulation) comble ces trous quand le code est sans ambiguïté
  (instructions du bloc, sans branchement indirect ni condition). I-sync raison 2 =
  débordement (`overflows`), jamais comblé. Bit Cancel d'une adresse de branchement
  d'exception : la dernière instruction tracée n'a pas été exécutée ; `FlowDecoder` retient
  la dernière instruction d'un atome (`_held`) jusqu'au paquet suivant, et `flush()` l'émet en
  fin de flux (`EtmTrace.flush`, à l'arrêt, ETMv3 comme ETMv4 ; `decode()` le fait).
- ETMv3 programmé (`EtmConfig.apply`) : ETMCR relu, une option absente de l'ETM (cycle-accurate
  sur un ETM-M4) est retirée (`refused`, dite par `summary()`) et le décodeur refait
  (`TraceManager._rebuild_etm`) ; `--etm-stall` écrit aussi ETMFFLR (seuil FIFOFULL, nul au
  reset : sans lui, stall ne fait rien).
- ETM simulé (QEMU n'a pas d'ETM) : QEMU lancé avec `-accel tcg,one-insn-per-tb=on` ; le
  resume hook active le journal (`-d exec,nochain,int`), l'arrêt le convertit en tâche de
  fond (`qemu_log.py` → `encoder.py` → même décodeur). `TraceManager.wait_etm()` attend la
  fin de la conversion. Piège documenté dans `qemu_log.py` : une ligne « Trace » suivie de
  « Stopped execution of TB chain » désigne une instruction non exécutée.
- MTB (Cortex-M0+, `trace/mtb.py`, `trace/mtb_capture.py`) : paquets source|A /
  destination|S lus en SRAM à chaque arrêt (sonde : `MtbCapture`, tampon remis à zéro) ou
  simulés depuis le même journal QEMU (`QemuMtbSim`) ; décodés en `Step` / `ExcEntry` /
  `ExcReturn` et versés dans la même `EtmTrace` (`feed_events`). `TraceManager` gère ETM
  simulé, MTB simulée et MTB réelle sur le même cycle reprise / arrêt ; `source_label()`
  pour les vues. Validation : chemin QEMU → `mtb.encode` → `MtbDecoder` == chemin réel
  (`tests/test_mtb.py`, bare, rtos, m0). AUTOHALT : `TraceManager._intercept` /
  `_internal_stop` ; en simulation, QMP `stop` périodique et tronçons de journal convertis à
  l'arrêt visible (convertir pendant la course figerait la GUI : GIL).
- Validation sans matériel : chemin réel QEMU → encodeur → notre décodeur **et** OpenCSD
  (`tests/opencsd.py`) doivent retrouver exactement le même chemin. Toute modification de
  l'encodeur ou du décodeur doit garder ces tests verts (`bare` et `rtos`).

### Ajouter une fonctionnalité

Une fonctionnalité complète touche en général : le module métier (`core/` ou `trace/`), une
vue (`views/`), une méthode de `ScriptAPI` (`core/scripting.py`), une option CLI dans
`__main__.py`, la page du wiki qui en parle (`Documentations/` : cibles,
menus, options, dépannage, limites) et les tests. `__main__.py` câble la trace **deux fois**
(chemin `--batch` et chemin GUI via `App._setup_live` / `App.attach`) : garder les deux
synchronisés. Les options de capture passent par `setup_trace(args, trace, session, target,
log)` (fonction du module, commune aux deux chemins ; `log` = console dans le GUI), et l'ETM
par SWO par `TraceManager.start_etm_swo` (aussi Trace › ETM par SWO… et
`gdbtrace.etm_swo()`). Une capture qui ne démarre pas doit le dire : `setup_error` et
`trace_status()` (vues Couverture et Trace), jamais un silence.
Une option ajoutée à la ligne de commande doit marcher dans une configuration : une option de
trace s'ajoute à `TRACE_KEYS` (`core/config.py`) et à `TRACE_FIELDS` (`gui/config_dialog.py`) ;
une autre option, à sa section (`CIBLE_KEYS`, `SERVER_KEYS`…), à `from_args` et à la boîte de
dialogue ; une règle entre options va dans `validate()`, jamais directement dans `main()`.
Une méthode ajoutée à `ScriptAPI` se documente aussi dans `Documentations/Scripting.md` et,
si un agent en a l'usage, devient un outil du serveur MCP (`mcp.py`, `Documentations/Serveur-MCP.md`) ; ses
blocs `<!-- test -->` sont exécutés en `--batch` par `tests/test_docs.py` et doivent rester verts.

## Limites connues (ne pas « corriger » côté gdbtrace)

- QEMU n'émule pas le vector catch (DEMCR lu à 0 : arrêt sur faute par breakpoints) ni le
  DWT (compteurs absents : écriture de DWT_CTRL ignorée), et QMP n'écrit pas en mémoire (RTT
  lu aux arrêts). Pas de table ROM CoreSight (0xE00FF000 lu à 0). MPU et SAU : sous QEMU 8.2,
  l'écriture de MPU_RNR / SAU_RNR par le débogueur est ignorée (seule la région choisie par le
  firmware, ou son groupe par les alias en ARMv8-M) ; QEMU 11.1 l'applique (toutes les régions) :
  `tests/test_mpu.py` et `tests/test_trustzone.py` acceptent les deux (`_rnr_ignored`). `mps3-an547` : Cortex-M55 en sécurisé, ITCM 0x10000000, DTCM 0x30000000. Démos : `Reset_Handler` remet la `.bss` à zéro,
  une variable de commande (`fault_kind`, `semihosting_on`) s'écrit après l'arrivée dans `main`.
- QEMU et TrustZone (`mps2-an505`) : DSCSR lu à 0 ; accès du débogueur dans le monde courant
  (depuis le non sécurisé : mémoire sécurisée refusée, SAU et SHCSR lus à 0) ; il faut
  NSCCFG.CODENSC (0x50080014) en plus du SAU pour une zone NSC. Plusieurs CPU : un processus
  par cluster (`attach 2`), pas de mode non-stop (OpenOCD non plus).
- Le gdbstub de QEMU 8.2 ignore les écritures du débogueur dans les périphériques (RAM/ROM
  seulement ; QEMU 11.1 les applique) ; QEMU n'émule ni ITM ni SWO.
- QEMU (8.2) sous charge : environ un pas à pas sur quelques centaines revient sans avoir
  exécuté l'instruction (`vCont;s` → mêmes registres). En enjambant un breakpoint, gdb le
  revoit alors au même endroit : un passage compté deux fois (hits, `count=`, messages
  d'action). Masquer les interruptions n'y change rien (`qqemu.sstep` vaut déjà 0x7). Les
  tests qui comptent des passages exacts sous QEMU le tolèrent (`collapse`,
  `tests/test_breakactions.py`).
- Validé sur Nucleo-F746ZG (`tests/test_board_f746.py`) : capture SWO et ITM, ETMv4 par SWO,
  trace de données du DWT, profil par SWO, vue RTOS, temps des tâches. Validé sur
  Nucleo-N657X0-Q (`tests/test_board_n6.py`) : ST-LINK_gdbserver et son relais (mémoire en
  direct, reset), ITM par le SWO de la sonde, ETMv4.5 par l'ETR (boucles LE comprises), DWT
  de l'ARMv8-M. Validé sur Nucleo-F072RB (`tests/test_board_m0.py -k f072`, DEV_ID 0x448,
  `STM32F0x2.svd`) : run-control, `DWT_PCSR` du Cortex-M0, RTOS en marche. Prêt, pas encore
  passé : Nucleo-G070RB (`-k g070`, DEV_ID 0x460, `STM32G070.svd` à confirmer). Jalon 23 :
  TrustZone et plusieurs cœurs validés sous QEMU seulement (an505, an521) ; CTI pas passé. Validé sur Nucleo-F446RE (`tests/test_board_f446.py`) :
  ETMv3 par SWO (décodage = OpenCSD sur une vraie capture), filtres plage et start/stop,
  ETMFFLR. Un ETMv3 par SWO sans filtre prend toute la broche : l'ITM n'y passe plus et un
  firmware qui attend sa FIFO se fige (dit par `EtmConfig.summary()`). Validés sur carte
  (jalons 18-22) : chrono (`boards.chrono_passes`, comparé au compte des ticks : SysTick sur la
  F072, `DWT_CYCCNT` sur F746, F446 et N6), journal des tâches en RAM lu en marche (F072), RTT
  en marche (F072, F746, F446, N6), compteurs du DWT (F746, F446) et de la PMU (N6), tables ROM
  (F746, F446, N6, F072) et MPU (F746, F446, N6 ; la STM32F746 n'intègre pas le CTI du Cortex-M7 :
  entrée de la table non présente), état sécurisé du M55 de la N6 (jalon 23, vue Registres), chargeur
  externe de la flash XSPI de la N6 (`MX25UM51245G_STM32N6570-NUCLEO.stldr`), FLM du pack
  Keil.STM32F4xx_DFP sur la F446 (`boards.system_passes`, `flash_by_algorithm`). Matrice
  jalon × carte : `Documentations/Jalons.md`. Un
  firmware recompilé déplace `worker_step` : `etm-stop` de `nucleo-f446re.yaml` est son
  `bx lr` (vérifié par le test). Non validé sur sonde
  réelle : `--rtos`, port de trace parallèle, MTB (dont le sens de
  l'adresse source d'une entrée d'exception : prise comme adresse de retour). ETMv4 : M7 (ETMv4.0, par SWO) et M55 (ETMv4.5, par l'ETR d'un
  STM32N6), cycle-accurate et horodatage compris ; la branche d'un BF* (v8.1-M) n'est pas
  suivie.
- ST-LINK_gdbserver : SWV servi en octets ITM bruts (`-z`, débit = `-a` / `-b`) ; l'ST-LINK
  intégré d'une N6 perd parfois un octet de SWO à 2 Mbit/s.
- ETMv4 : un atome = un point de passage (`Instr.waypoint` : branchement ou ISB), pas une
  instruction ; en profondeur de spéculation 1, le dernier élément n'est validé qu'au
  suivant ou par `EtmTrace.flush()` (fait à l'arrêt de la cible). OpenCSD 1.4.1 déborde sur
  une vraie trace d'ETM-M7 (Trace Info SPEC=1) : pour le comparer, ramener SPEC à 0.
- OpenOCD 0.12 et un Cortex-M0 (constaté sur STM32F072) : sous reset (`connect_assert_srst`),
  le cœur ne s'arrête pas sur un simple halt et gdb est refusé → `gdb-attach { reset halt }`
  dans les `.cfg` des M0/M0+ ; un arrêt de watchpoint arrive en SIGTRAP sans adresse →
  `Session._watch_trap` (DFSR.DWTTRAP, valeur, condition) ; l'interruption d'un `halt()`
  doublée par un breakpoint est appliquée après la reprise suivante → absorbée
  (`_stale_interrupt`). Lectures en marche à ~2 ms : aucun parcours des listes FreeRTOS n'est
  cohérent → état de chaque tâche lu dans son TCB (`freertos._collect_tcbs`).
- OpenOCD 0.12 recopie dans son journal (stderr, et console gdb cible en marche) le
  résultat de chaque commande TCL : passer par `OpenOcdTcl.query()`, jamais `command()`,
  pour tout ce qui renvoie des données. Le stderr des serveurs doit rester drainé
  (`TargetServer._drain`) : un tube plein bloque OpenOCD entier.
- Tests GUI : fermer chaque `App` avec `close_app()` (`gc.collect()` dans le thread
  principal), sinon des `Variable.__del__` bloquent des threads de travail
  (« main thread is not in main loop »).
