"""Serveur MCP de GdbTrace : un agent (Claude Code…) pilote une session de débogage.

    python -m gdbtrace.mcp [--root PROJET]      # stdio
    python -m gdbtrace.mcp --check              # bilan de l'environnement, puis sortie

Claude Code le trouve seul dans le `.mcp.json` du dépôt (portée projet) ; ailleurs :
`claude mcp add gdbtrace -- /chemin/.venv/bin/python -m gdbtrace.mcp --root /chemin/projet`.

Protocole MCP (Model Context Protocol) sur stdio : JSON-RPC 2.0, un message par ligne ;
`initialize`, `tools/list`, `tools/call`, `ping`. Une session à la fois, sans interface,
ouverte par l'outil `connect` avec les options de la ligne de commande de GdbTrace (ELF,
cible, trace : comme `python -m gdbtrace … --batch`). Les autres outils reprennent l'objet
`gdbtrace` des scripts (Documentations/Scripting.md) : run-control, breakpoints, variables, mémoire,
registres, pile, désassemblage, périphériques, threads, RTOS, trace, couverture, timing,
profil ; `python` exécute du code avec cet objet, `gdb` une commande CLI brute. Chaque outil
porte un titre et ses annotations MCP (lecture seule, destructif).

`--root` (sinon la variable CLAUDE_PROJECT_DIR que Claude Code pose) devient le répertoire
courant : les chemins donnés aux outils (ELF, configurations, SVD, fichiers) y sont relatifs.

Tout ce que GdbTrace écrit (print des scripts, journaux, messages des fils de trace) est
capturé et rendu dans le résultat de l'outil, ou mis de côté jusqu'au suivant : stdout est
réservé au protocole.
"""
from __future__ import annotations

import contextlib
import inspect
import io
import json
import os
import shlex
import sys
import threading
import traceback

PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
SERVER_INFO = {"name": "gdbtrace", "title": "GdbTrace", "version": "1.1"}
INSTRUCTIONS = (
    "GdbTrace : débogueur Cortex-M (QEMU, OpenOCD, ST-LINK_gdbserver) et programmes de l'hôte, "
    "avec trace (ITM/SWO, ETM, MTB). Déroulé : 1) `connect` avec les options de "
    "`python -m gdbtrace` (ex. « examples/firmware/demo.elf --etm-sim ») ; si elle échoue, "
    "`check` dit ce qui manque (gdb, QEMU, sonde, démos à construire) ; 2) `break` / `watch` "
    "puis `go` (attend l'arrêt quelques secondes), `go_till` (condition), `step`, "
    "`step_until` ; 3) lire la cible arrêtée : `status`, `backtrace`, `registers`, `evaluate`, "
    "`type`, `memory`, `disassemble`, `peripheral`, `rtos` ; 4) la trace porte sur l'exécution "
    "qui précède l'arrêt : `trace_list`, `trace_find`, `calls`, `call_tree`, `timing`, "
    "`coverage`. `python` couvre le reste (objet `gdbtrace` des scripts).")

_S = {"type": "string"}
_I = {"type": "integer"}
_N = {"type": "number"}
_B = {"type": "boolean"}
_L = {"type": "array", "items": {"type": "string"}}
_ADDR = {"type": "string", "description": "nombre (0x20000000) ou expression : « &compteur », "
         "un tableau, un pointeur, une fonction"}
_LEN = {"type": "integer", "description": "octets ; défaut : la taille de l'objet nommé"}
_PATTERN = {"type": "string", "description": "entier (0x55, 1234 : petit-boutiste, 1, 2 ou 4 "
            "octets), octets hexadécimaux séparés par des espaces (« de ad be ef »), ou texte "
            "(entre guillemets s'il ressemble à un nombre)"}


def _tool(name: str, title: str, description: str, props: dict | None = None,
          required: tuple = (), *, read_only: bool = False, destructive: bool = False) -> dict:
    """Déclaration d'un outil : schéma des arguments et annotations MCP. `read_only` : rien ne
    change ni dans la cible ni dans la session ; `destructive` : écrit dans la cible (mémoire,
    flash) ou exécute du code arbitraire."""
    schema = {"type": "object", "properties": props or {}, "additionalProperties": False}
    if required:
        schema["required"] = list(required)
    ann = {"title": title, "readOnlyHint": read_only, "openWorldHint": False}
    if not read_only:
        ann["destructiveHint"] = destructive
    return {"name": name, "title": title, "description": description, "inputSchema": schema,
            "annotations": ann}


TOOLS = [
    # --- session
    _tool("connect", "Connexion", "Ouvre une session de débogage (ferme la précédente). `args` : "
          "options de `python -m gdbtrace` sans --batch ni --script : ELF puis cible et trace, ou "
          "une configuration (« --config configs/qemu-demo.yaml »). Exemples : "
          "« examples/firmware/demo.elf » (QEMU) ; « demo.elf --etm-sim » (trace ETM simulée) ; "
          "« fw.elf --openocd configs/nucleo-f746zg.cfg --program --swo-config --cpu-hz "
          "16000000 » ; « fw.elf --stlink --stlink-ap 1 --ram-boot --program » ; "
          "« --pid 1234 » (programme natif lancé). Chemins relatifs au répertoire du projet. "
          "Rend le journal de mise en route et l'arrêt courant.",
          {"args": _S}, ("args",)),
    _tool("disconnect", "Déconnexion", "Ferme la session (gdb, serveur de la cible). "
          "`leave_running` : la cible continue de tourner.", {"leave_running": _B}),
    _tool("status", "État de la session", "État de la session : cible, état (stopped, running, "
          "exited), raison du dernier arrêt, position (fonction, fichier:ligne), état de la trace.",
          read_only=True),
    _tool("check", "Bilan de l'environnement", "Ce que GdbTrace trouve sur cette machine : gdb, "
          "QEMU, compilateur ARM, OpenOCD, ST-LINK_gdbserver, modules Python, démos construites, "
          "répertoire du projet. Sans session ; à appeler quand `connect` échoue.", read_only=True),
    # --- run-control
    _tool("go", "Go", "Lance la cible (Go). `until` : jusqu'à cet emplacement (fonction, "
          "fichier:ligne, *adresse). `wait` : secondes d'attente de l'arrêt (défaut 5 ; 0 : ne "
          "pas attendre) ; sans arrêt, la cible reste en marche (`halt`, `status`).",
          {"until": _S, "wait": _N}),
    _tool("go_till", "Go jusqu'à une condition", "Go.Till : la cible tourne en temps réel "
          "jusqu'à ce que l'expression C `condition` devienne vraie (« ticks > 100 », "
          "« sensors[2].status == 1 ») : watchpoint matériel sur ses variables, retiré à la "
          "reprise suivante. Registres ou valeurs sans adresse : `step_until`. `wait` comme "
          "`go`.", {"condition": _S, "wait": _N}, ("condition",)),
    _tool("halt", "Break", "Arrête la cible (Break)."),
    _tool("step", "Pas à pas", "Pas à pas : `kind` = line (entre dans les fonctions), over "
          "(par-dessus), out (jusqu'au retour), instruction ; `count` pas (défaut 1).",
          {"kind": {"type": "string", "enum": ["line", "over", "out", "instruction"]},
           "count": _I}),
    _tool("step_until", "Pas à pas jusqu'à une condition", "Pas à pas (ligne, ou instruction si "
          "`asm`) jusqu'à ce que `condition` soit vraie (Step.Till, registres compris : "
          "« $r0 == 3 ») ou que la valeur de `change` change (Step.Change) ; `max_steps` pas au "
          "plus (défaut 1000). Lent : un aller-retour gdb par pas.",
          {"condition": _S, "change": _S, "asm": _B, "max_steps": _I}),
    _tool("reset", "Reset", "Reset de la cible, arrêtée au vecteur de reset (programme natif : "
          "relancé au début de main).", destructive=True),
    # --- breakpoints
    _tool("break", "Poser un breakpoint", "Pose un breakpoint. `location` : fonction, "
          "fichier:ligne ou *adresse ; `condition` (expression C) ; `count` : arrêt au Nᵉ "
          "passage ; `log` : message à chaque passage ({expr}, {expr:hex}…), la cible repart ; "
          "`hardware` ; `temporary`.",
          {"location": _S, "condition": _S, "count": _I, "log": _S, "hardware": _B,
           "temporary": _B}, ("location",)),
    _tool("watch", "Poser un watchpoint", "Pose un watchpoint sur une expression (variable) : "
          "`access` = write (défaut), read ou rw (lecture et écriture).",
          {"expression": _S, "access": {"type": "string", "enum": ["write", "read", "rw"]}},
          ("expression",)),
    _tool("enable", "Activer un breakpoint", "Active (`on`, défaut) ou désactive un breakpoint "
          "ou un watchpoint par son numéro : il reste posé, avec ses options.",
          {"number": _S, "on": _B}, ("number",)),
    _tool("delete", "Supprimer un breakpoint", "Supprime un breakpoint ou un watchpoint par son "
          "numéro.", {"number": _S}, ("number",)),
    _tool("breakpoints", "Liste des breakpoints", "Liste les breakpoints et watchpoints "
          "(numéro, type, lieu, passages, condition, action) et les derniers messages de leurs "
          "actions.", read_only=True),
    # --- données
    _tool("evaluate", "Évaluer une expression", "Évalue une expression C. Cible en marche : "
          "relit par le canal mémoire une variable à adresse fixe (globale, champ, élément de "
          "tableau) déjà évaluée une fois cible arrêtée. `format` : natural, hex, dec, oct, bin, "
          "ascii.", {"expression": _S, "format": _S}, ("expression",), read_only=True),
    _tool("set", "Affecter une variable", "Affecte une valeur à une variable ou un registre "
          "(cible arrêtée).", {"expression": _S, "value": _S}, ("expression", "value")),
    _tool("type", "Type d'une expression", "Type d'une expression, typedefs résolus (champs "
          "d'une structure, énumérés), sa taille et son adresse.", {"expression": _S},
          ("expression",), read_only=True),
    _tool("registers", "Registres", "Registres du cœur (thread courant).", read_only=True),
    _tool("backtrace", "Pile d'appels", "Pile d'appels, avec les variables locales de la frame "
          "courante.", read_only=True),
    _tool("disassemble", "Désassemblage", "Désassemble `location` : fonction ou "
          "fichier:ligne (toute la fonction), adresse (`count` instructions depuis elle) ou "
          "plage « début..fin » ; défaut : la fonction courante. « => » marque le PC ; `count` "
          "lignes au plus (défaut 100).", {"location": _S, "count": _I}, read_only=True),
    _tool("memory", "Dump mémoire", "Dump hexadécimal de `length` octets (défaut 64, 4096 au "
          "plus) à `address` : nombre, ou expression dont la valeur est l'adresse (« &compteur », "
          "un tableau, un pointeur) ; cible en marche : adresse numérique, lue par le canal "
          "mémoire en direct.", {"address": _S, "length": _I}, ("address",), read_only=True),
    _tool("memory_write", "Écrire en mémoire", "Data.Set : écrit à `address` soit les octets "
          "`data` (hexadécimal : « de ad be ef »), soit l'entier `value` sur `size` octets "
          "(1, 2, 4, 8 ; petit-boutiste). Cible arrêtée ; RAM et registres de périphériques "
          "(la flash se programme par `program`). Rend la relecture.",
          {"address": _ADDR, "data": _S, "value": _S, "size": _I}, ("address",),
          destructive=True),
    _tool("memory_fill", "Remplir la mémoire", "Data.Set : `length` octets à `address` remplis "
          "du motif répété (défaut 0). Cible arrêtée.",
          {"address": _ADDR, "length": _LEN, "pattern": _PATTERN}, ("address",),
          destructive=True),
    _tool("memory_find", "Chercher en mémoire", "Data.Find : adresses du motif dans "
          "[address, address + length) ; `align` (défaut 1), `limit` résultats (défaut 100). "
          "Marche cible en marche par le canal mémoire.",
          {"address": _ADDR, "length": _LEN, "pattern": _PATTERN, "align": _I, "limit": _I},
          ("address", "pattern"), read_only=True),
    _tool("memory_file", "Mémoire et fichiers", "`action` : save (Data.SAVE : `length` octets à "
          "`address` dans `path`, format d'après l'extension : .bin, .hex Intel HEX, .srec), "
          "load (Data.LOAD : image HEX, S-record, ELF ou binaire à `address`, en RAM, cible "
          "arrêtée), compare (Data.ComPare : image et mémoire, différences).",
          {"action": {"type": "string", "enum": ["save", "load", "compare"]}, "path": _S,
           "address": _ADDR, "length": _LEN}, ("action", "path"), destructive=True),
    _tool("symbols", "Symboles", "Fonctions et variables globales de l'ELF dont le nom contient "
          "`pattern` (ou motif avec * et ?) : adresse, taille, fichier:ligne. `kind` : tout, "
          "fonctions, variables ; `top` lignes (défaut 50).",
          {"pattern": _S, "kind": _S, "top": _I}, read_only=True),
    _tool("add_symbols", "Ajouter des symboles", "Ajoute les symboles d'un autre ELF "
          "(bootloader, image chargée à part) à ceux de la session ; `offset` : décalage de "
          "chargement (0x100000). gdb, profil, trace et couverture les connaissent ensuite.",
          {"path": _S, "offset": _S}, ("path",)),
    _tool("coresight", "Composants CoreSight", "Composants de débogage et de trace de la puce, "
          "par ses tables ROM (cible arrêtée) : SCS, DWT, FPB, ITM, TPIU, ETM et sa version, CTI, "
          "MTB, PMU, ETF/ETR… ; dit s'il n'y a pas d'ETM. Vide sous QEMU (pas de table ROM).",
          read_only=True),
    _tool("mpu", "Régions de la MPU", "Régions de la MPU décodées (cible arrêtée) : début, fin, "
          "sous-régions désactivées, accès privilégié et non privilégié, XN, type mémoire "
          "(TEX/C/B en ARMv7-M, MAIR en ARMv8-M), et si la MPU est activée. TrustZone : celle "
          "de l'état courant, ou la non sécurisée (`nonsecure`).", {"nonsecure": _B},
          read_only=True),
    _tool("core", "Cœurs", "Plusieurs cœurs (option --core de `connect`) : sans `name`, la "
          "liste (« * » : le cœur choisi, état, source : la session, un processus du même "
          "serveur, un autre serveur gdb) ; avec `name` (nom ou rang), ce cœur devient celui de "
          "toutes les autres commandes (registres, pile, pas, breakpoints, Go, halt) et sa pile "
          "est donnée. Cœurs d'un même serveur : un arrêt les arrête tous, Go les relance tous.",
          {"name": _S}),
    _tool("security", "TrustZone", "Cœur ARMv8-M à extension de sécurité (cible arrêtée) : "
          "état courant (sécurisé ou non, mode, pile en service), registres bancaires (msp_s, "
          "psp_ns, control_s…), SAU (régions NS / NSC, lisible depuis l'état sécurisé), et la "
          "pile d'appels à travers les deux mondes (appels SG et BLXNS, FNC_RETURN).",
          read_only=True),
    _tool("peripheral", "Périphérique (SVD)", "PER.view : registres décodés d'un périphérique "
          "(champs en clair) d'après le SVD de la session (--svd) ou `svd` (fichier, chargé "
          "d'abord). Sans `name` : la liste des périphériques. Les registres à lecture "
          "destructive ne sont lus qu'avec `force`.",
          {"name": _S, "svd": _S, "force": _B}),
    _tool("threads", "Threads", "Threads (programme natif, ou tâches RTOS exposées par le "
          "serveur) ; `select` : nom ou numéro du thread qui devient courant.", {"select": _S}),
    _tool("rtos", "Tâches RTOS", "Tâches FreeRTOS : état, priorité, pile utilisée, CPU, objet "
          "attendu.", read_only=True),
    _tool("task", "Contexte d'une tâche", "Registres et pile d'appels d'une tâche FreeRTOS "
          "suspendue, par son nom (cible arrêtée).", {"name": _S}, ("name",), read_only=True),
    _tool("queues", "Objets du noyau", "Queues, sémaphores et mutex FreeRTOS du registre "
          "(vQueueAddToRegistry) : remplissage, détenteur, tâches en attente.", read_only=True),
    _tool("task_times", "Temps par tâche", "Temps par tâche FreeRTOS : activations, brut, net "
          "(sans les exceptions), part du temps, tranches ; par l'ITM (port 31) ou le journal en "
          "RAM `gdbtrace_task_log`. `top` lignes (défaut 20) ; `unit` : us (défaut) ou cycles.",
          {"top": _I, "unit": _S}, read_only=True),
    # --- trace
    _tool("trace_list", "Trace.List", "Les `n` dernières instructions exécutées (défaut 40), "
          "avec exceptions et trous.", {"n": _I}, read_only=True),
    _tool("trace_find", "Chercher dans la trace", "Cherche dans la trace d'instructions (les `n` "
          "derniers événements, défaut 50000) : adresse 0x…, nom de fonction (chaque entrée), "
          "« exception » ou « exception SysTick », ou texte ; `top` lignes (défaut 20).",
          {"query": _S, "n": _I, "top": _I}, ("query",), read_only=True),
    _tool("trace_export", "Exporter la trace", "Écrit les `n` derniers événements de la trace "
          "(défaut 100000) en CSV dans `path` : index, type, adresse, fonction, instruction, "
          "fichier, ligne.", {"path": _S, "n": _I}, ("path",)),
    _tool("etm_filter", "Filtre de la trace ETM", "Limite la trace ETM : `ranges` (fonctions, "
          "fichiers « main.c », plages « 0x100-0x200 »), `start` / `stop` (lieux qui démarrent "
          "et arrêtent la trace) ; tout vide : tout le code. Cible arrêtée ; la couverture se "
          "limite aux plages.", {"ranges": _L, "start": _L, "stop": _L}),
    _tool("calls", "Flot d'appels", "Flot d'appels de la trace (appels, retours, exceptions) : "
          "`n` lignes.", {"n": _I}, read_only=True),
    _tool("coverage", "Couverture", "Couverture de code par la trace : résumé et fonctions les "
          "moins couvertes (`top`, défaut 20) ; `export` : rapport dans ce fichier (.html, "
          ".info LCOV, .json) ; `reset` : remet la couverture à zéro.",
          {"top": _I, "export": _S, "reset": _B}),
    _tool("distribution", "Distribution des durées", "Trace.STATistic.DISTribution : "
          "histogramme des durées de `name` (fonction ou exception — « SysTick » — de la trace "
          "d'instructions, tâche du temps des tâches, ou valeurs d'une variable tracée), "
          "centiles p50/p90/p99, et les appels les plus longs avec leur index dans la trace "
          "(celui de `trace_list`). `bins` classes (défaut 20), `unit` « us » ou « cycles », "
          "`log` : classes logarithmiques (défaut : selon l'étalement).",
          {"name": _S, "bins": _I, "unit": _S, "log": _B}, read_only=True),
    _tool("call_tree", "Arbre d'appels", "Arbre d'appels d'après la trace (brut et net par "
          "chemin d'appel ; une exception est la racine de son arbre) : `depth` niveaux (défaut "
          "4), nœuds d'au moins `min_pct` % du temps (défaut 1).",
          {"depth": _I, "min_pct": _N}, read_only=True),
    _tool("timing", "Durées", "Durées par fonction et par exception d'après la trace (brut, "
          "net, min/max/moyenne) : `top` lignes (défaut 20).", {"top": _I}, read_only=True),
    _tool("irq_stats", "Statistiques des exceptions", "Statistiques des exceptions : entrées, "
          "durée min/moy/max, période et gigue, préemptions, enchaînements ; depuis la trace "
          "d'instructions (ETM, MTB), sinon la trace des exceptions de l'ITM.", read_only=True),
    _tool("var_trace", "Tracer une variable", "Var.TRACE : le DWT émet la valeur de "
          "`expression` (globale ou statique, 1, 2 ou 4 octets) à chaque accès (`access` : write, "
          "read, rw ; `pc` : avec l'instruction), par la capture SWO, cible en marche. `stop` : "
          "arrêter (toutes sans `expression`). Cible arrêtée ; valeurs : `var_samples`.",
          {"expression": _S, "access": {"type": "string", "enum": ["write", "read", "rw"]},
           "pc": _B, "stop": _B}),
    _tool("snoop", "Échantillonner une variable", "Variable échantillonnée toutes les "
          "`period_ms` ms (défaut 10) par le canal mémoire, cible en marche, sans trace "
          "(Cortex-M0, QEMU) ; `stop` : arrêter l'échantillonnage. Valeurs : `var_samples`.",
          {"expr": _S, "period_ms": _I, "stop": _B}, ("expr",)),
    _tool("var_samples", "Valeurs d'une variable", "Les `n` dernières valeurs (défaut 20) d'une "
          "variable tracée (`var_trace`) ou échantillonnée (`snoop`) : temps, accès, valeur "
          "(`format` : natural, hex…), instruction.",
          {"expression": _S, "n": _I, "format": _S}, ("expression",), read_only=True),
    _tool("chrono", "Chrono", "Temps du cœur pendant la dernière exécution (de la reprise à "
          "l'arrêt : go jusqu'à un breakpoint, pas…) et cumul, mesuré par la cible sans trace : "
          "SysTick et compteur de ticks (Cortex-M0/M0+, QEMU), DWT_CYCCNT au-delà. `reset` : "
          "remet le cumul à zéro.", {"reset": _B}),
    _tool("counters", "Compteurs de performance", "Compteurs de performance du cœur pendant la "
          "dernière exécution : cycles, compteurs du DWT (CPI, EXC, SLEEP, LSU, FOLD, "
          "instructions, IPC) ou PMU du Cortex-M55. Le premier appel les arme (mesure à "
          "l'exécution suivante) ; `reset` : remet le cumul à zéro ; `off` : les désarme.",
          {"reset": _B, "off": _B}),
    _tool("profile", "Profil statistique", "Profil statistique (PERF) pendant `seconds` "
          "(défaut 1) : la cible tourne, puis revient à son état.", {"seconds": _N}),
    _tool("itm", "Texte ITM", "Texte reçu sur un port ITM (`port`, défaut 0) par la capture "
          "SWO.", {"port": _I}, read_only=True),
    _tool("terminal", "Terminal", "Texte écrit par le programme (semihosting, RTT : "
          "--semihosting, --rtt) depuis l'appel précédent ; `send` : texte envoyé au programme "
          "d'abord (RTT descendant, entrée du semihosting).", {"send": _S}),
    _tool("fault", "Analyse de faute", "Analyse de la faute en cours (cible arrêtée dans "
          "HardFault, BusFault…) : cause en clair, adresse et instruction fautives, fonction et "
          "ligne, cadre empilé. `catch` : true pose l'arrêt à l'entrée de toute faute, false le "
          "retire.", {"catch": _B}),
    _tool("chip", "Puce (CMSIS-Pack)", "La puce décrite par son CMSIS-Pack (connect avec --device, "
          "--pack) : cœur, horloge maximale, mémoires, algorithmes de flash, SVD ; et le cœur lu sur "
          "la cible (CPUID), comparé au pack.", read_only=True),
    _tool("flash_info", "Chargeurs de flash", "Chargeurs de flash de la session (--flash-loader : "
          "FLM, .stldr, pack) : mémoire couverte, page, secteurs, RAM de l'algorithme. Sans "
          "chargeur, la flash passe par le serveur gdb.", read_only=True),
    _tool("flash_erase", "Effacer la flash", "Efface les secteurs de [`address`, `address` + "
          "`length`) par le chargeur de flash qui couvre l'adresse ; sans `address` : toute la "
          "mémoire de chaque chargeur. Cible arrêtée.", {"address": _ADDR, "length": _I},
          destructive=True),
    _tool("program", "Programmer", "Programme un ELF dans la cible (reset, écriture, "
          "vérification, reset) ; défaut : l'ELF de la session.", {"elf": _S}, destructive=True),
    # --- portes de sortie
    _tool("gdb", "Commande gdb", "Commande CLI gdb brute (ex. « info sharedlibrary », "
          "« x/4wx 0x20000000 »).", {"command": _S}, ("command",), destructive=True),
    _tool("python", "Code Python", "Exécute du code Python avec l'objet `gdbtrace` des scripts "
          "(et `session`, `hexdump`) : tout ce que les autres outils ne couvrent pas "
          "(page Scripting du wiki). Rend ce que le code affiche.", {"code": _S}, ("code",),
          destructive=True),
]


# ---------------------------------------------------------------------- bilan de l'environnement
_PROGRAMS = (
    ("gdb-multiarch", "gdb (GDB/MI) : indispensable (autre gdb : option --gdb de connect)"),
    ("qemu-system-arm", "cible QEMU (démos, tests, trace simulée)"),
    ("arm-none-eabi-gcc", "construction des démos (make -C examples/firmware)"),
    ("openocd", "sondes ST-LINK, CMSIS-DAP, J-Link (--openocd)"),
)
_MODULES = (
    ("capstone", "trace ETM et MTB (désassemblage du code tracé)"),
    ("elftools", "pyelftools : symboles, profil, trace"),
    ("yaml", "configurations --config"),
)
_DEMOS = (
    ("examples/firmware/demo.elf", "make -C examples/firmware"),
    ("examples/freertos/freertos.elf", "make -C examples/freertos"),
)


def _first_line(cmd: list[str]) -> str:
    import subprocess
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=5, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return ""
    lines = (r.stdout or r.stderr or "").strip().splitlines()
    return lines[0].strip() if lines else ""


def environment_report() -> tuple[list[str], bool]:
    """Lignes « ✓ / ✗ élément : version ou ce qu'il faut faire », et vrai si gdb est là (sans
    lui, aucune session)."""
    import importlib.util
    import platform
    import shutil
    from importlib import metadata

    from .core.target import find_stlink_gdbserver
    try:
        version = metadata.version("gdbtrace")
    except metadata.PackageNotFoundError:
        version = "non installé (pip install -e .)"
    lines = [f"GdbTrace {version}, Python {platform.python_version()} ({sys.executable})",
             f"répertoire du projet : {os.getcwd()}"]
    ok = True
    for prog, why in _PROGRAMS:
        path = shutil.which(prog)
        if path:
            lines.append(f"✓ {prog} : {_first_line([path, '--version']) or path}")
            continue
        if prog == "gdb-multiarch":
            alt = shutil.which("arm-none-eabi-gdb") or shutil.which("gdb")
            ok = alt is not None
            hint = f" ; trouvé : {alt} (--gdb {os.path.basename(alt)})" if alt else ""
            lines.append(f"✗ {prog} : absent — {why}{hint}")
        else:
            lines.append(f"✗ {prog} : absent — {why}")
    st = find_stlink_gdbserver()
    lines.append(f"✓ ST-LINK_gdbserver : {st}" if st else
                 "✗ ST-LINK_gdbserver : absent — sondes ST-LINK par --stlink (STM32CubeIDE, "
                 "STM32CubeCLT)")
    for mod, why in _MODULES:
        found = importlib.util.find_spec(mod) is not None
        lines.append(f"{'✓' if found else '✗'} module {mod}" + ("" if found else f" : absent — {why}"))
    if os.path.isdir("examples"):
        for elf, make in _DEMOS:
            lines.append(f"✓ {elf}" if os.path.isfile(elf) else f"✗ {elf} : à construire ({make})")
    return lines, ok


class ToolError(Exception):
    """Erreur attendue d'un outil : rendue telle quelle (isError), sans pile Python."""


class GdbTraceMcp:
    def __init__(self, out=None):
        self.out = out or sys.stdout
        self.batch = None
        self.args_text = ""
        self._pending: list[str] = []            # messages arrivés entre deux appels
        self._layouts: dict = {}                 # expression → disposition (lecture en direct)
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ protocole
    def send(self, msg: dict) -> None:
        with self._lock:
            self.out.write(json.dumps(msg, ensure_ascii=False) + "\n")
            self.out.flush()

    def handle(self, msg: dict) -> dict | None:
        """Réponse à un message JSON-RPC (None pour une notification)."""
        mid, method, params = msg.get("id"), msg.get("method"), msg.get("params") or {}
        if mid is None:                          # notification (initialized, cancelled…)
            return None
        try:
            if method == "initialize":
                asked = params.get("protocolVersion")
                version = asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0]
                result = {"protocolVersion": version, "serverInfo": SERVER_INFO,
                          "capabilities": {"tools": {"listChanged": False}},
                          "instructions": INSTRUCTIONS + " Chemins relatifs au projet : "
                          f"{os.getcwd()}."}
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": TOOLS}
            elif method == "tools/call":
                result = self.call(params.get("name", ""), params.get("arguments") or {})
            else:
                return {"jsonrpc": "2.0", "id": mid,
                        "error": {"code": -32601, "message": f"méthode inconnue : {method}"}}
        except Exception as exc:  # noqa: BLE001 - une erreur interne ne tue pas le serveur
            return {"jsonrpc": "2.0", "id": mid,
                    "error": {"code": -32603, "message": f"{type(exc).__name__}: {exc}"}}
        return {"jsonrpc": "2.0", "id": mid, "result": result}

    def serve(self, inp=None) -> None:
        for line in inp or sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError as exc:
                self.send({"jsonrpc": "2.0", "id": None,
                           "error": {"code": -32700, "message": f"JSON illisible : {exc}"}})
                continue
            reply = self.handle(msg)
            if reply is not None:
                self.send(reply)
        self.close()

    # ------------------------------------------------------------------ outils
    def call(self, name: str, arguments: dict) -> dict:
        fn = getattr(self, f"tool_{name}", None)
        if fn is None:
            return _result(f"outil inconnu : {name}", error=True)
        try:
            inspect.signature(fn).bind(**arguments)
        except TypeError as exc:
            return _result(f"arguments invalides pour {name} : {exc}", error=True)
        buf = io.StringIO()
        error = False
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            try:
                text = fn(**arguments)
            except ToolError as exc:
                text, error = str(exc), True
            except Exception as exc:  # noqa: BLE001 - rendu à l'agent, la session continue
                text, error = f"{type(exc).__name__}: {exc}", True
                if not str(exc):
                    text += "\n" + traceback.format_exc(limit=4)
        with self._lock:
            pending, self._pending = self._pending, []
        parts = [p for p in ("\n".join(pending), buf.getvalue().rstrip(), (text or "").rstrip()) if p]
        return _result("\n".join(parts) or "fait", error=error)

    def _on_event(self, kind: str, data: dict) -> None:
        """Thread lecteur de gdb : messages des actions et sortie du programme (natif) gardés
        pour le résultat de l'outil en cours ou du suivant ; ne bloque jamais."""
        if kind == "output" and data.get("stream") in ("action", "target"):
            text = data.get("text", "").rstrip("\n")
            if text:
                self._log(text)
        elif kind == "exited":
            code = data.get("exit-code", "0")
            self._log(f"programme terminé ({data.get('reason', '?')}, code "
                      f"{int(code, 8) if code.isdigit() else code})")

    def _log(self, text: str) -> None:
        """Messages des fils de trace hors d'un appel : rendus avec le résultat suivant."""
        with self._lock:
            self._pending.append(str(text).rstrip())

    def _need(self):
        if self.batch is None:
            raise ToolError("pas de session : appeler `connect` d'abord")
        return self.batch

    @property
    def api(self):
        return self._need().api

    def _where(self) -> str:
        b = self._need()
        s = b.s
        if s.state == "stopped":
            try:
                f = s.frame()
                loc = f" {f.file}:{f.line}" if f.file else ""
                return f"arrêtée : {f.func or '??'} ({f.pc:#x}){loc}"
            except Exception as exc:  # noqa: BLE001
                return f"arrêtée ({exc})"
        if s.state == "exited":
            return f"programme terminé (code {s.exit_code})"
        return s.state

    # --- session
    def tool_connect(self, args: str) -> str:
        from .__main__ import BatchSession, build_target, parse_command_line, resolve_session
        self.close()
        argv = shlex.split(args)
        if any(a in ("--batch", "--script", "--coverage") for a in argv):
            raise ToolError("--batch / --script / --coverage : sans objet ici (outils `python`, "
                            "`coverage`)")
        try:                                    # mêmes règles que la ligne de commande
            p, ns, config = parse_command_line(argv + ["--batch"])
            elf, program, verify, load, svd_warning = resolve_session(p, ns)
        except SystemExit:                      # argparse a écrit l'erreur (capturée)
            raise ToolError("options refusées") from None
        log: list[str] = [svd_warning] if svd_warning else []
        target = build_target(ns, elf)
        try:
            b = BatchSession(ns, elf, target, load, program, verify, log=log.append,
                             on_event=self._on_event, config=config)
        except Exception as exc:  # noqa: BLE001 - connexion impossible : le journal l'explique
            raise ToolError("\n".join(log + [f"connexion impossible : {exc}"])) from exc
        b.trace.on_error = self._log
        self.batch, self.args_text, self._layouts = b, args, {}
        try:
            b.setup()
        except Exception as exc:  # noqa: BLE001 - la session reste ouverte, sans cette capture
            raise ToolError("\n".join(log + [f"mise en route de la trace : {exc}",
                                              f"session ouverte : {self._where()}"])) from exc
        if ns.go and b.s.state == "stopped":
            b.s.go()
        return "\n".join(log + [f"session : {target.remote} — {self._where()}"])

    def tool_disconnect(self, leave_running: bool = False) -> str:
        if self.batch is None:
            return "pas de session"
        self.close(leave_running)
        return "session fermée" + (" (la cible continue)" if leave_running else "")

    def tool_status(self) -> str:
        if self.batch is None:
            return "pas de session (`connect`)"
        b = self.batch
        lines = [f"session : {self.args_text}", f"cible : {b.target.remote}",
                 f"état : {self._where()}"]
        reason = (b.s.stop_reason or {}).get("reason")
        if reason and b.s.state == "stopped":
            lines.append(f"raison du dernier arrêt : {reason}")
        if b.trace.etm is not None:
            lines.append(f"trace d'instructions : {b.trace.trace_status()}")
        if b.trace.source is not None:
            lines.append(f"capture SWO : {b.trace.status()}")
        return "\n".join(lines)

    def tool_check(self) -> str:
        lines, ok = environment_report()
        if not ok:
            lines.append("aucune session possible sans gdb (apt install gdb-multiarch)")
        return "\n".join(lines)

    # --- run-control
    def tool_go(self, until: str | None = None, wait: float = 5.0) -> str:
        self.api.go(until)
        return self._wait_stop(wait)

    def tool_go_till(self, condition: str, wait: float = 5.0) -> str:
        self.api.go_till(condition)
        return self._wait_stop(wait)

    def _wait_stop(self, wait: float) -> str:
        """Après une reprise : l'arrêt en une ligne (avec l'ancienne et la nouvelle valeur d'un
        watchpoint), ou la cible laissée en marche au bout de `wait` s."""
        from .core.scripting import stop_summary
        api = self.api
        if not wait:
            return "en marche"
        try:
            rec = api.wait(wait)
        except TimeoutError:
            return f"toujours en marche après {wait:g} s (`halt` pour l'arrêter)"
        self._settle()
        text = f"arrêt : {stop_summary(rec)}"
        value = rec.get("value") if isinstance(rec.get("value"), dict) else {}
        if "new" in value:
            text += f"\nvaleur : {value.get('old', '?')} → {value['new']}"
        return text

    def tool_halt(self) -> str:
        self.api.halt()
        self._settle()
        return self._where()

    def tool_step(self, kind: str = "line", count: int = 1) -> str:
        api = self.api
        fn = {"line": api.step, "over": api.over, "instruction": api.stepi}.get(kind)
        if kind == "out":
            api.out()
        elif fn is None:
            raise ToolError("kind : line, over, out ou instruction")
        else:
            fn(count)
        self._settle()
        return self._where()

    def tool_step_until(self, condition: str | None = None, change: str | None = None,
                        asm: bool = False, max_steps: int = 1000) -> str:
        from .core.scripting import stop_summary
        if bool(condition) == bool(change):
            raise ToolError("donner `condition` (Step.Till) ou `change` (Step.Change), l'un des deux")
        api = self.api
        rec = (api.step_till(condition, asm, max_steps) if condition
               else api.step_change(change, asm, max_steps))
        self._settle()
        return stop_summary(rec)

    def tool_reset(self) -> str:
        self.api.reset()
        return self._where()

    def _stopped(self):
        """La session, cible arrêtée (sinon une erreur d'outil claire)."""
        s = self.api.s
        if s.state != "stopped":
            raise ToolError(f"cible {'en marche' if s.state == 'running' else s.state} : "
                            "`halt` d'abord")
        return s

    def _addr(self, text) -> int:
        """Adresse : nombre, ou expression évaluée par gdb (cible arrêtée)."""
        if isinstance(text, int):
            return text
        t = str(text).strip()
        try:
            return int(t.lstrip("*"), 0)
        except ValueError:
            pass
        s = self.api.s
        if s.state != "stopped":
            raise ToolError("cible en marche : une adresse numérique (les symboles se "
                            "résolvent cible arrêtée)")
        return int(s.evaluate(f"(unsigned long)({t.lstrip('*')})").split()[0], 0)

    def _settle(self) -> None:
        """Après un arrêt : la trace de l'exécution est décodée avant qu'on la lise."""
        b = self.batch
        if b is not None and b.trace.etm is not None:
            b.trace.wait_etm(60)

    # --- breakpoints
    def tool_break(self, location: str, condition: str | None = None, count: int | None = None,
                   log: str | None = None, hardware: bool = False, temporary: bool = False) -> str:
        bp = self.api.break_(location, hw=hardware, temporary=temporary, condition=condition,
                             count=count, log=log)
        return f"breakpoint {_bp_text(bp)}"

    def tool_watch(self, expression: str, access: str = "write") -> str:
        num = self.api.watch(expression, access)
        return f"watchpoint {num} sur {expression} ({access})"

    def tool_enable(self, number: str, on: bool = True) -> str:
        api = self.api
        api.enable(str(number), on)
        bp = next((b for b in api.breaks() if b.number == str(number)), None)
        return _bp_text(bp) if bp is not None else f"{number} {'activé' if on else 'désactivé'}"

    def tool_delete(self, number: str) -> str:
        self.api.delete(number)
        return f"{number} supprimé"

    def tool_breakpoints(self) -> str:
        bps = self.api.breaks()
        lines = [_bp_text(b) for b in bps] or ["aucun breakpoint"]
        log = self.api.break_log(20)
        if log:
            lines += ["messages des actions :"] + [f"  {m}" for m in log]
        return "\n".join(lines)

    # --- données
    def tool_evaluate(self, expression: str, format: str = "natural") -> str:
        from .core import live_vars
        from .core.formats import normalize
        api = self.api
        fmt = normalize(format)
        if api.s.state != "stopped":
            lay = self._layouts.get(expression)
            if api.live is None or lay is None:
                why = ("pas de canal mémoire en direct pour cette cible" if api.live is None else
                       "expression jamais évaluée cible arrêtée, ou sans adresse fixe (locale, "
                       "registre, type composé)")
                raise ToolError(f"cible en marche : {why} ; `halt` d'abord")
            value = live_vars.decode(lay, api.live.read(lay.addr, lay.size), fmt)
            return f"{expression} = {value} (lu en direct)"
        value = api.eval(expression, fmt)
        if api.live is not None and expression not in self._layouts:
            lay = self._live_layout(expression)
            if lay is not None:
                self._layouts[expression] = lay
        return f"{expression} = {value}"

    def _live_layout(self, expr: str):
        """Disposition d'une feuille à adresse fixe (globale, champ, élément), cible arrêtée :
        `evaluate` la relira ensuite cible en marche par le canal mémoire (core/live_vars)."""
        from .core import live_vars
        s = self.api.s
        try:
            local_names = {v.get("name") for v in s.locals()}
        except Exception:  # noqa: BLE001 - pas de frame avec informations de débogage
            local_names = set()
        if live_vars.root_identifier(expr) in local_names:
            return None
        try:
            addr, size = s.address_of_expr(expr), s.sizeof(expr)
            if not 0 < size <= live_vars.MAX_LIVE_SIZE:
                return None
            kind = live_vars.classify(s.ptype(expr), size)
        except Exception:  # noqa: BLE001 - pas une lvalue, type inconnu…
            return None
        if kind is None:
            return None
        return live_vars.LeafLayout(addr=addr, size=size, kind=kind[0], enum_names=kind[1],
                                    live=True)

    def tool_set(self, expression: str, value: str) -> str:
        api = self.api
        api.set(expression, value)
        return f"{expression} = {api.eval(expression)}"

    def tool_registers(self) -> str:
        self.api.r()
        return ""

    def tool_backtrace(self) -> str:
        api = self.api
        api.bt()
        try:
            loc = api.s.locals()
        except Exception:  # noqa: BLE001 - frame sans informations de débogage
            loc = []
        if loc:
            print("variables locales :")
            for v in loc:
                print(f"  {v.get('name')} = {v.get('value', '…')}")
        return ""

    def tool_type(self, expression: str) -> str:
        s = self.api.s
        text = s.ptype(expression)
        extra = []
        with contextlib.suppress(Exception):    # pas une lvalue, type incomplet…
            extra.append(f"sizeof = {s.sizeof(expression)}")
        with contextlib.suppress(Exception):
            extra.append(f"adresse {s.address_of_expr(expression):#010x}")
        return text + ("\n" + ", ".join(extra) if extra else "")

    def tool_disassemble(self, location: str = "", count: int = 100) -> str:
        s = self._stopped()
        loc = (location or "").strip()
        pc = None
        with contextlib.suppress(Exception):
            pc = s.frame().pc
        if ".." in loc:
            a, b = loc.split("..", 1)
            insns = s.disassemble(self._addr(a), self._addr(b))
        elif not loc:
            func = None
            with contextlib.suppress(Exception):
                func = s.frame().func
            insns = (s.disassemble_function(func) if func else
                     s.disassemble(pc, pc + 4 * count)[:count] if pc is not None else [])
        elif _is_address(loc):                  # `count` instructions depuis l'adresse
            start = self._addr(loc)
            insns = s.disassemble(start, start + 4 * count)[:count]
        else:                                   # fonction, fichier:ligne : toute la fonction
            insns = s.disassemble_function(loc)
        if not insns:
            raise ToolError(f"rien à désassembler à « {loc or 'PC'} »")
        lines = []
        for i in insns[:count]:
            mark = "=>" if pc is not None and i["address"] == pc else "  "
            where = f"<{i['func-name']}+{i.get('offset', 0)}>" if i.get("func-name") else ""
            lines.append(f"{mark} {i['address']:#010x} {where:<26} {i.get('inst', '')}")
        if len(insns) > count:
            lines.append(f"… {len(insns) - count} instruction(s) de plus (`count`)")
        return "\n".join(lines)

    def tool_memory(self, address: str, length: int = 64) -> str:
        from .core.scripting import hexdump
        api = self.api
        if not 0 < length <= 4096:
            raise ToolError("length : de 1 à 4096 octets")
        addr = self._addr(address)
        return hexdump(api.mem(addr, length), addr)

    def tool_memory_write(self, address: str, data: str | None = None, value: str | None = None,
                          size: int = 4) -> str:
        from .core.scripting import hexdump
        s = self._stopped()
        if (data is None) == (value is None):
            raise ToolError("donner `data` (octets hexadécimaux) ou `value` (entier), l'un des deux")
        if data is not None:
            try:
                blob = bytes.fromhex(str(data).replace("0x", "").replace(",", " "))
            except ValueError:
                raise ToolError(f"data : octets hexadécimaux attendus (« de ad be ef »), "
                                f"pas « {data} »") from None
        else:
            if size not in (1, 2, 4, 8):
                raise ToolError("size : 1, 2, 4 ou 8 octets")
            try:
                v = value if isinstance(value, int) else int(str(value).strip(), 0)
            except ValueError:
                raise ToolError(f"value : entier attendu (décimal ou 0x…), pas « {value} »") from None
            if not -(1 << (8 * size - 1)) <= v < (1 << 8 * size):
                raise ToolError(f"value : {value} ne tient pas sur {size} octet(s)")
            blob = (v & ((1 << 8 * size) - 1)).to_bytes(size, "little")
        if not blob:
            raise ToolError("rien à écrire")
        addr = self._addr(address)
        s.write_memory(addr, blob)
        shown = s.read_memory(addr, min(len(blob), 64))
        return f"{len(blob)} octet(s) écrits à {addr:#x}, relus :\n" + hexdump(shown, addr)

    def tool_memory_fill(self, address: str, length: int | None = None, pattern: str = "0") -> str:
        api = self.api
        a, n = api._where(address, length)
        api.mem_fill(a, n, _pattern(pattern))
        return f"{n} octets à {a:#x} remplis de {_pattern(pattern)!r}"

    def tool_memory_find(self, address: str, pattern: str, length: int | None = None,
                         align: int = 1, limit: int = 100) -> str:
        found = self.api.mem_find(address, length, _pattern(pattern), limit, align)
        if len(found) > 16:
            print("toutes : " + ", ".join(f"{x:#x}" for x in found))
        return ""

    def tool_memory_file(self, action: str, path: str, address: str | None = None,
                         length: int | None = None) -> str:
        api = self.api
        if action == "save":
            if address is None:
                raise ToolError("save : `address` (et `length`, sauf objet nommé) à donner")
            api.mem_save(address, length, path)
        elif action == "load":
            api.mem_load(path, address)
            self._layouts = {}
        elif action == "compare":
            api.mem_compare(path, address)
        else:
            raise ToolError("action : save, load ou compare")
        return ""

    def tool_add_symbols(self, path: str, offset: str | int = 0) -> str:
        try:
            off = offset if isinstance(offset, int) else int(str(offset).strip() or "0", 0)
        except ValueError:
            raise ToolError(f"offset : entier attendu (0x100000), pas « {offset} »") from None
        self.api.add_symbols(os.path.abspath(path), off)
        return f"symboles de {path} ajoutés" + (f" (décalage {off:#x})" if off else "")

    def tool_coresight(self) -> str:
        self._stopped()
        api = self.api
        api.coresight()
        reason = api.trace.no_etm_reason() if api.trace is not None else None
        return f"trace d'instructions : {reason}" if reason else ""

    def tool_mpu(self, nonsecure: bool = False) -> str:
        self._stopped()
        try:
            self.api.mpu(nonsecure=nonsecure)
        except ValueError as exc:
            raise ToolError(str(exc)) from None
        return ""

    def tool_core(self, name: str = "") -> str:
        from .core.cores import CoreError
        if not name:
            self.api.cores()
            return ""
        try:
            self.api.core(name)
        except (RuntimeError, CoreError) as exc:
            raise ToolError(str(exc)) from None
        return ""

    def tool_security(self) -> str:
        self._stopped()
        if self.api.security() is None:
            return ""
        self.api.sau()
        print("pile :")
        self.api.stack()
        return ""

    def tool_peripheral(self, name: str = "", svd: str | None = None, force: bool = False) -> str:
        api = self.api
        out = []
        if svd:
            api.load_svd(os.path.abspath(svd))
            out.append(f"SVD chargé : {api.device.name}, {len(api.device.peripherals)} périphériques")
        dev = api.device
        if dev is None:
            raise ToolError("aucun SVD : `svd` (fichier .svd), ou --svd à `connect`")
        if not name:
            out += [f"{p.name:<16} {p.base:#010x}  {p.description}" for p in dev.peripherals]
            return "\n".join(out)
        if dev.peripheral(name) is None:
            near = [p.name for p in dev.peripherals if name.lower() in p.name.lower()]
            raise ToolError(f"périphérique inconnu : {name}"
                            + (f" ; proches : {', '.join(near[:20])}" if near else
                               " (`peripheral` sans `name` : la liste)"))
        if out:
            print("\n".join(out))
        api.periph(name, force)
        return ""

    def tool_threads(self, select: str | None = None) -> str:
        api = self.api
        if select:
            api.thread(select)
            return ""
        api.threads()
        return ""

    def tool_rtos(self) -> str:
        self.api.tasks()
        return ""

    def tool_task(self, name: str) -> str:
        self._stopped()
        self.api.task(name)
        return ""

    def tool_queues(self) -> str:
        if not self.api.queues():
            return ("aucun objet au registre des queues (vQueueAddToRegistry, "
                    "configQUEUE_REGISTRY_SIZE > 0)")
        return ""

    def tool_task_times(self, top: int = 20, unit: str = "us") -> str:
        self.api.task_times(top, unit)
        return ""

    # --- trace
    def tool_trace_list(self, n: int = 40) -> str:
        self._settle()
        self.api.trace_list(n)
        return ""

    def tool_calls(self, n: int = 40) -> str:
        self._settle()
        self.api.calls(n)
        return ""

    def tool_coverage(self, top: int = 20, export: str | None = None, reset: bool = False) -> str:
        api = self.api
        if reset:
            api.cov.reset()
            return "couverture remise à zéro"
        self._settle()
        if export:
            api.cov.export(export)
            return ""
        api.coverage(top)
        return ""

    def tool_trace_export(self, path: str, n: int = 100_000) -> str:
        self._settle()
        self.api.trace_export(path, n)
        return ""

    def tool_etm_filter(self, ranges: list | str | None = None, start: list | str | None = None,
                        stop: list | str | None = None) -> str:
        self._stopped()
        self.api.etm_filter(*_strings(ranges), start=_strings(start), stop=_strings(stop))
        return ""

    def tool_var_trace(self, expression: str | None = None, access: str = "write",
                       pc: bool = False, stop: bool = False) -> str:
        api = self.api
        if stop:
            api.var_trace_stop(expression or None)
            return ""
        if not expression:
            raise ToolError("`expression` : la variable à tracer")
        self._stopped()
        api.var_trace(expression, access, pc)
        return ""

    def tool_var_samples(self, expression: str, n: int = 20, format: str = "natural") -> str:
        from .core.formats import normalize
        rows = self.api.var_samples(expression, n or None, normalize(format))
        if not rows:
            return (f"aucune valeur de {expression} : la cible doit tourner (`go`) pendant la "
                    "trace ou l'échantillonnage")
        return f"{len(rows)} valeur(s)"

    def tool_timing(self, top: int = 20) -> str:
        self._settle()
        self.api.timing(top)
        return ""

    def tool_distribution(self, name: str, bins: int = 20, unit: str = "us", log: bool | None = None) -> str:
        self._settle()
        try:
            self.api.distribution(name, bins=bins, unit=unit, log=log)
        except (KeyError, ValueError, RuntimeError) as exc:
            raise ToolError(str(exc).strip("'\"")) from None
        return ""

    def tool_call_tree(self, depth: int = 4, min_pct: float = 1.0) -> str:
        self.api.call_tree(depth, min_pct)
        return ""

    def tool_irq_stats(self) -> str:
        self.api.irq_stats()
        return ""

    def tool_snoop(self, expr: str, period_ms: int = 10, stop: bool = False) -> str:
        if stop:
            self.api.snoop_stop(expr)
        else:
            self.api.snoop(expr, period_ms)
        return ""

    def tool_trace_find(self, query: str, n: int = 50_000, top: int = 20) -> str:
        self.api.trace_find(query, n, top)
        return ""

    def tool_chrono(self, reset: bool = False) -> str:
        if reset:
            self.api.chrono_reset()
            return "cumul du chrono remis à zéro"
        self.api.chrono()
        return ""

    def tool_symbols(self, pattern: str = "", kind: str = "tout", top: int = 50) -> str:
        self.api.sym_list(pattern, kind, top)
        return ""

    def tool_terminal(self, send: str | None = None) -> str:
        out = ""
        if send:
            out = f"envoyé par {self.api.terminal_send(send)}\n"
        text = self.api.terminal(quiet=True)
        return out + (text or "(rien de nouveau)")

    def tool_counters(self, reset: bool = False, off: bool = False) -> str:
        if off:
            self.api.counters_off()
            return ""
        if reset:
            self.api.counters_reset()
            return "cumul des compteurs remis à zéro"
        self.api.counters()
        return ""

    def tool_fault(self, catch: bool | None = None) -> str:
        if catch is not None:
            return self.api.break_on_fault(catch)
        self.api.fault()
        return ""

    def tool_profile(self, seconds: float = 1.0) -> str:
        self.api.profile(seconds)
        return ""

    def tool_itm(self, port: int = 0) -> str:
        return self.api.itm(port) or f"(rien reçu sur le port {port})"

    def tool_chip(self) -> str:
        self.api.chip_info()
        return ""

    def tool_flash_info(self) -> str:
        self.api.flash_info()
        return ""

    def tool_flash_erase(self, address: str | None = None, length: int | None = None) -> str:
        self._stopped()
        self.api.flash_erase(address, length)
        return ""

    def tool_program(self, elf: str | None = None) -> str:
        import os
        self.api.program(os.path.abspath(elf) if elf else None)
        self._layouts = {}
        return self._where()

    # --- portes de sortie
    def tool_gdb(self, command: str) -> str:
        return self.api.s.cli(command)

    def tool_python(self, code: str) -> str:
        from .core.scripting import hexdump
        api = self.api
        ns = {"gdbtrace": api, "session": api.s, "hexdump": hexdump}
        try:
            value = eval(compile(code, "<mcp>", "eval"), ns)   # expression : sa valeur
        except SyntaxError:
            exec(compile(code, "<mcp>", "exec"), ns)
            return ""
        return "" if value is None else repr(value)

    # ------------------------------------------------------------------ fin
    def close(self, leave_running: bool = False) -> None:
        b, self.batch = self.batch, None
        if b is not None:
            try:
                b.close(leave_running=leave_running)
            except Exception:  # noqa: BLE001 - fermeture au mieux
                pass


def _result(text: str, error: bool = False) -> dict:
    return {"content": [{"type": "text", "text": text}], "isError": error}


def _is_address(text: str) -> bool:
    """« 0x8000120 », « 1234 », « *0x100 », « *&buf[2] » : une adresse, pas un lieu source."""
    t = text.strip()
    if t.startswith("*"):
        return True
    try:
        int(t, 0)
        return True
    except ValueError:
        return False


def _pattern(text):
    """Motif de `memory_fill` / `memory_find` : entier (0x55, 1234), octets hexadécimaux
    séparés par des espaces (« de ad be ef »), texte entre guillemets, sinon texte tel quel."""
    if isinstance(text, int):
        return text
    t = str(text).strip()
    if len(t) >= 2 and t[0] == t[-1] and t[0] in "\"'":
        return t[1:-1]
    try:
        return int(t, 0)
    except ValueError:
        pass
    parts = t.split()
    if len(parts) > 1 and all(len(p) == 2 and all(c in "0123456789abcdefABCDEF" for c in p)
                              for p in parts):
        return bytes.fromhex(t)
    return t


def _strings(value) -> list[str]:
    """Liste de textes (un texte seul, ou séparés par des virgules, en est une aussi)."""
    if not value:
        return []
    if isinstance(value, str):
        return [v.strip() for v in value.split(",") if v.strip()]
    return [str(v) for v in value if str(v).strip()]


def _bp_text(b) -> str:
    """Une ligne par breakpoint : numéro, type, lieu, passages, options."""
    where = b.expr or (f"{b.func or '??'} {b.file}:{b.line}" if b.file else (b.func or b.addr or ""))
    extra = [f"{b.hits} passage(s)"]
    if not b.enabled:
        extra.append("désactivé")
    if b.condition:
        extra.append(f"si {b.condition}")
    if b.count:
        extra.append(f"au {b.count}ᵉ passage")
    if b.action:
        extra.append(b.action)
    return f"{b.number} {b.type} {where} ({', '.join(extra)})"


def build_parser():
    import argparse
    p = argparse.ArgumentParser(
        prog="python -m gdbtrace.mcp",
        description="Serveur MCP de GdbTrace sur stdio : un agent (Claude Code…) pilote une "
                    "session de débogage. Dans ce dépôt, le .mcp.json le lance seul.")
    p.add_argument("--root", metavar="DIR",
                   help="répertoire du projet, où les chemins donnés aux outils (ELF, "
                        "configurations, SVD, fichiers) sont relatifs (défaut : "
                        "$CLAUDE_PROJECT_DIR, sinon le répertoire courant)")
    p.add_argument("--check", action="store_true",
                   help="affiche le bilan de l'environnement (gdb, QEMU, compilateur, sondes, "
                        "démos) et sort : 0 si une session est possible")
    return p


def main(argv: list[str] | None = None) -> int:
    """stdin / stdout sont au protocole seul : un processus fils (gdb, QEMU, OpenOCD) qui
    hériterait des descripteurs 0 et 1 lirait les requêtes ou écrirait dans les réponses. Ils
    sont donc dupliqués pour le protocole, puis 0 ← /dev/null et 1 ← stderr (journal)."""
    ns = build_parser().parse_args(argv)
    root = ns.root or os.environ.get("CLAUDE_PROJECT_DIR")
    if root:
        if not os.path.isdir(root):
            print(f"gdbtrace-mcp : --root {root} : répertoire introuvable", file=sys.stderr)
            return 2
        os.chdir(root)
    if ns.check:
        lines, ok = environment_report()
        print("\n".join(lines))
        if not ok:
            print("aucune session possible sans gdb (apt install gdb-multiarch)")
        return 0 if ok else 1
    print(f"gdbtrace-mcp : projet {os.getcwd()}", file=sys.stderr)
    proto_in = os.fdopen(os.dup(0), "r", encoding="utf-8", errors="replace")
    proto_out = os.fdopen(os.dup(1), "w", encoding="utf-8")
    sys.stdout.flush()
    devnull = os.open(os.devnull, os.O_RDONLY)
    os.dup2(devnull, 0)
    os.close(devnull)
    os.dup2(2, 1)
    sys.stdout = sys.stderr                     # tout autre print va dans le journal
    server = GdbTraceMcp(proto_out)
    try:
        server.serve(proto_in)
    except KeyboardInterrupt:
        server.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
