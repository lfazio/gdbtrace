# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Projet

GdbTrace (paquet `gdbtrace`) : débogueur Cortex-M avec trace, construit sur gdb (GDB/MI), GUI tkinter. Tout
le projet est **en français** : docstrings, commentaires, libellés d'interface, messages
d'erreur, README et messages de commit. Les commentaires citent souvent le nom de commande
GdbTrace (Data.dump, Var.view, PER.view, Trace.List…) : garder ce vocabulaire. Le produit
s'appelle **GdbTrace** : ne citer aucun autre débogueur commercial dans le code ni la doc.

Le développement avance par jalons (voir le tableau du README). Messages de commit :
`Jalon N, étape M : résumé`, puis une liste à puces par module touché.

## Commandes

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'   # dev = pytest + capstone (ETM)
make -C examples/firmware        # demo.elf (Cortex-M3, QEMU mps2-an385)
make -C examples/freertos        # freertos.elf
make -C examples/m0plus          # demo.elf Cortex-M0+ (QEMU -M microbit), tampon MTB réservé
.venv/bin/python -m gdbtrace examples/firmware/demo.elf               # GUI sur QEMU
.venv/bin/python -m gdbtrace examples/firmware/demo.elf --etm-sim     # + trace ETM simulée
.venv/bin/python -m gdbtrace fw.elf --batch --script s.py             # sans GUI

xvfb-run -a .venv/bin/python -m pytest                 # suite complète
.venv/bin/python -m pytest tests/test_etm.py -k rtos   # sous-ensemble
.venv/bin/python -m pytest tests/test_etm.py::test_roundtrip_matches_qemu
tools/build_opencsd.sh           # oracle OpenCSD dans .cache/ (active test_opencsd_*)
tools/release_check.sh           # release : toute la suite, cartes de test exigées
```

Outils externes : `qemu-system-arm`, `gdb-multiarch`, `arm-none-eabi-gcc`, `xvfb-run`. Les
tests qui en ont besoin sont **ignorés** (skip) s'ils manquent, comme les tests GUI sans
`DISPLAY` et le test OpenCSD sans `.cache/opencsd` : un « passed » peut donc masquer des
tests non exécutés, vérifier les skips. Les fixtures `elf` / `rtos_elf` de
`tests/conftest.py` recompilent les firmwares (`make`) ; `session` / `rtos_session` lancent
QEMU sur un port libre. `pythonpath` inclut `tests/`, d'où `from conftest import …`.
Aucun linter n'est configuré.

Tests sur carte (`tests/test_board_f746.py`, Nucleo-F746ZG, firmware `examples/nucleo-f746`
flashé par le test) : `--boards=auto|require|skip` (`tests/boards.py` trouve la sonde par
`/sys` puis l'IDCODE). Le GUI tourne par le vrai `main()`, `App.mainloop` remplacé par un
scénario ; le contenu de chaque vue est relevé (`boards.dump_views`, dans
`tests/artifacts/board-f746.json`) puis vérifié vue par vue, avec la latence de la boucle Tk.
Le test laisse la carte sans ETM ni stall, mais avec son firmware de test.

## Architecture

### Couches

`GdbMI` (`core/gdb_backend.py`) → `Session` (`core/session.py`) → consommateurs :
`App` (GUI), `ScriptAPI` (objet `gdbtrace` de la console et des scripts), mode `--batch`
(`__main__.py`).

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
  `needs_load=False`), `OpenOcdTarget`, `RemoteTarget` (+ `--tcl` optionnel), et
  `NativeTarget` (`target.native`) : programme de l'hôte exécuté par gdb lui-même
  (`-exec-run --start`, arrêt à `main`), dans son propre pseudo-terminal (sinon il partage
  les tubes MI de gdb), état `exited` à sa fin, `restart()`. `live_memory()` fournit le canal
  mémoire latéral (natif : `/proc/<pid>/mem`), ou `None`.
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
  formaté, l'ID 1 va à l'ITM et l'ID 2 à l'ETM.
- Débit : un SWO saturé (2 Mbit/s, ETM-M7) arrive plus vite que le Python ne décode l'ETM.
  `TcpSwoSource` lit la socket dans un fil et décode dans un autre (sinon OpenOCD, monothread,
  se bloque en écrivant : lectures en direct en échec, SWO perdu, ITM corrompu) ; l'ETM a son
  propre fil de décodage (`TraceManager._etm_loop`) pour que l'ITM reste en direct. Les
  retards sont affichés (`status()`, `trace_status()`), `wait_etm()` attend la fin. Le flot
  ETMv4 mémorise les blocs de code et compte la couverture par bloc (`take_counts`) :
  rien ne doit parcourir les instructions une à une en Python sur ce chemin.
- Trace de données (`trace/datatrace.py`) : comparateurs du DWT (FUNCTION 0xD/0xC/0x2, +PC
  0xF/0xE/0x3) programmés par `TraceManager.trace_variable` (cible arrêtée, pris par le haut,
  partagés avec le filtre ETMv3 : `EtmConfig.dwt_reserved` / `dwt_used`) ; `DataTraceSink`
  (abonné ITM) range les paquets en échantillons typés par variable ; vue `vartrace`.
- Temps par tâche (`trace/tasktrace.py`) : le firmware écrit le TCB entrant sur le port ITM 31
  (`traceTASK_SWITCHED_IN`, démo FreeRTOS) ; `TaskTraceSink` en tire tranches, brut/net (trace
  des exceptions du DWT) ; noms relus dans les TCB à l'affichage (`namer`), jamais depuis le
  fil de capture ; vue `tasktime`.
- ETM (`trace/etm/`) : `etm3.py` (paquets ETMv3.5) → `flow.py` (reconstruction avec
  `image.py`, classification Thumb-2 par capstone ; chaque fonction est balayée d'un seul
  tenant pour connaître la condition des instructions d'un bloc IT — `bxeq lr` décodé seul
  paraît inconditionnel ; `Instr.op` = mnémonique sans condition) → `trace.py` (historique, couverture,
  appels). Les événements `Step` / `ExcEntry` / `ExcReturn` portent des cycles mesurés
  (cycle-accurate) ou estimés par `cycles.py` ; `trace/timing.py` en tire le temps brut/net
  par fonction et par exception (vue Timing).
- ETMv4 (Cortex-M7) : `etm4.py` (paquets) → `flow4.py` (file de spéculation, parcours du
  code entre points de passage) ; `encoder4.py` pour les tests et l'ETM simulé.
  `EtmTrace(version=…)` choisit v3 ou v4 ; `TraceManager.detect_etm()` lit TRCIDR1 (sous
  QEMU : CPUID, M7 → v4) après la connexion, et `configure_etm()` suit `EtmConfig.version`.
- Filtre (`etm/filter.py`, `EtmConfig.filter`, `TraceManager.set_etm_filter`) : plages et
  start/stop ; ETMv4 par ses comparateurs d'adresse, ETMv3 par ceux du DWT (blocs alignés,
  `effective_filter()`). Une reprise de la trace est un événement `TraceGap` (I-sync raison
  1/2, Trace On) que tout consommateur d'événements doit traiter ; celle qui suit une simple
  reprise après arrêt est retirée par `EtmTrace.resumed()`. L'ETM simulé applique le filtre
  effectif au chemin QEMU (`EtmFilter.apply`) ; la couverture se limite aux plages (`scope`).
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
`__main__.py`, la section du README et les tests. `__main__.py` câble la trace **deux fois**
(chemin `--batch` et chemin GUI via `App._setup_live` / `App.attach`) : garder les deux
synchronisés. Les options de capture passent par `setup_trace(args, trace, session, target,
log)` (fonction du module, commune aux deux chemins ; `log` = console dans le GUI), et l'ETM
par SWO par `TraceManager.start_etm_swo` (aussi Trace › ETM par SWO… et
`gdbtrace.etm_swo()`). Une capture qui ne démarre pas doit le dire : `setup_error` et
`trace_status()` (vues Couverture et Trace), jamais un silence.
Une méthode ajoutée à `ScriptAPI` se documente aussi dans `docs/scripting.md` (notice) ; ses
blocs `<!-- test -->` sont exécutés en `--batch` par `tests/test_docs.py` et doivent rester verts.

## Limites connues (ne pas « corriger » côté gdbtrace)

- Le gdbstub de QEMU ignore les écritures du débogueur dans les périphériques (RAM/ROM
  seulement) ; QEMU n'émule ni ITM ni SWO.
- QEMU (8.2) sous charge : environ un pas à pas sur quelques centaines revient sans avoir
  exécuté l'instruction (`vCont;s` → mêmes registres). En enjambant un breakpoint, gdb le
  revoit alors au même endroit : un passage compté deux fois (hits, `count=`, messages
  d'action). Masquer les interruptions n'y change rien (`qqemu.sstep` vaut déjà 0x7). Les
  tests qui comptent des passages exacts sous QEMU le tolèrent (`collapse`,
  `tests/test_breakactions.py`).
- Validé sur Nucleo-F746ZG (`tests/test_board_f746.py`) : capture SWO et ITM, ETMv4 par SWO,
  trace de données du DWT, profil par SWO, vue RTOS, temps des tâches. Non validé sur sonde
  réelle : `--rtos`, `DWT_PCSR` via OpenOCD, port de trace parallèle, ETMv3 matériel (M3/M4,
  y compris par SWO), MTB (dont le sens de l'adresse source d'une entrée d'exception : prise
  comme adresse de retour). ETMv4 : M7 (ETMv4.0) seulement, sans cycle-accurate ni
  horodatage ; pas les ARMv8-M (M33, M55).
- ETMv4 : un atome = un point de passage (`Instr.waypoint` : branchement ou ISB), pas une
  instruction ; en profondeur de spéculation 1, le dernier élément n'est validé qu'au
  suivant ou par `EtmTrace.flush()` (fait à l'arrêt de la cible). OpenCSD 1.4.1 déborde sur
  une vraie trace d'ETM-M7 (Trace Info SPEC=1) : pour le comparer, ramener SPEC à 0.
- OpenOCD 0.12 recopie dans son journal (stderr, et console gdb cible en marche) le
  résultat de chaque commande TCL : passer par `OpenOcdTcl.query()`, jamais `command()`,
  pour tout ce qui renvoie des données. Le stderr des serveurs doit rester drainé
  (`TargetServer._drain`) : un tube plein bloque OpenOCD entier.
- Tests GUI : fermer chaque `App` avec `close_app()` (`gc.collect()` dans le thread
  principal), sinon des `Variable.__del__` bloquent des threads de travail
  (« main thread is not in main loop »).
