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
```

Outils externes : `qemu-system-arm`, `gdb-multiarch`, `arm-none-eabi-gcc`, `xvfb-run`. Les
tests qui en ont besoin sont **ignorés** (skip) s'ils manquent, comme les tests GUI sans
`DISPLAY` et le test OpenCSD sans `.cache/opencsd` : un « passed » peut donc masquer des
tests non exécutés, vérifier les skips. Les fixtures `elf` / `rtos_elf` de
`tests/conftest.py` recompilent les firmwares (`make`) ; `session` / `rtos_session` lancent
QEMU sur un port libre. `pythonpath` inclut `tests/`, d'où `from conftest import …`.
Aucun linter n'est configuré.

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
- `TargetServer` (`core/target.py`) : `QemuTarget` (défaut, charge l'ELF lui-même,
  `needs_load=False`), `OpenOcdTarget`, `RemoteTarget` (+ `--tcl` optionnel).
  `live_memory()` fournit le canal mémoire latéral, ou `None`.

### Threads et tkinter

tkinter n'est pas thread-safe. Tout ce qui vient d'un autre thread passe par
`EventPump.post(fn, …)` ; les actions longues par `pump.background(fn, then=…)` ou
`App.run_action()`. `App.safe()` / `report_error()` envoient les exceptions vers la console
et la barre d'état. Les `except Exception` larges (`# noqa: BLE001`) sont voulus : un abonné
ou un callback défaillant ne doit pas tuer un thread de travail.

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
- Non validé sur sonde réelle : `--rtos`, capture SWO, `DWT_PCSR` via OpenOCD, trace ETM
  matérielle, ETM par SWO, MTB (dont le sens de l'adresse source d'une entrée d'exception :
  prise comme adresse de retour). ETMv4 : M7 (ETMv4.0) seulement, sans cycle-accurate ni
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
