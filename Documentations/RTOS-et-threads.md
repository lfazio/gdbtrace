La vue RTOS montre les tâches FreeRTOS sans rien ajouter au firmware. La vue Temps des tâches mesure le temps de chacune, à condition que le firmware écrive ses changements de tâche sur l'ITM. La vue Threads montre les threads d'un programme de l'hôte.

## Vue RTOS (FreeRTOS)

Elle s'active quand FreeRTOS est lié dans l'ELF. Elle lit les structures du noyau en mémoire, d'après leur disposition lue une fois dans le DWARF, et marche avec QEMU, OpenOCD ou un autre serveur.

- **Tâches** : état, priorité (et priorité de base en cas d'héritage par mutex), pile utilisée sur sa taille, octets jamais utilisés (ligne rouge au-delà de 90 %), % CPU, objet attendu. La tâche courante est en gras et rappelée dans la barre d'état.
- **Objets du noyau** : queues, mutex (avec leur détenteur), sémaphores, tâches en attente. Il faut `configQUEUE_REGISTRY_SIZE` > 0.
- **Contexte d'une tâche** (double-clic, cible arrêtée) : ses registres, relus sur sa pile, et sa pile d'appels. Les registres du cœur sont toujours restaurés ensuite.
- **En marche** : la vue se rafraîchit par le canal mémoire. Une lecture faite pendant un changement de contexte est détectée et relancée.

Configuration conseillée du noyau : `configUSE_TRACE_FACILITY 1`, `configRECORD_STACK_HIGH_ADDRESS 1`, `configCHECK_FOR_STACK_OVERFLOW 2`, `configQUEUE_REGISTRY_SIZE` > 0, `configGENERATE_RUN_TIME_STATS 1`. Ports gérés pour le contexte des tâches : ARM_CM3, ARM_CM4F et ARM_CM0. Depuis V11.3, le port ARM_CM0 range sur la pile EXC_RETURN, puis r4-r11 : GdbTrace reconnaît ce cadre (symbole `vRestoreContextOfFirstTask` sur un Cortex-M0/M0+).

## Temps des tâches

La vue **Temps des tâches** mesure exactement le temps de chaque tâche, sans échantillonnage. Elle donne par tâche les activations, le temps brut, le temps net (sans les exceptions), la part du temps, la tranche maximale et la moyenne. Un chronogramme montre les dernières tranches, une rangée par tâche, avec la durée au survol.

Côté firmware, trois lignes dans `FreeRTOSConfig.h` et la fonction `gdbtrace_task_switched_in` de `examples/freertos/main.c`, appelée à chaque changement de tâche avec le TCB entrant et le compte des ticks :

```c
#define GDBTRACE_TASK_PORT 31
void gdbtrace_task_switched_in(void *tcb, uint32_t ticks);
#define traceTASK_SWITCHED_IN() gdbtrace_task_switched_in((void *)pxCurrentTCB, \
                                    (uint32_t)(xTickCount + xPendedTicks))
```

La fonction donne chaque changement de deux façons ; GdbTrace prend l'une ou l'autre, jamais les deux :

| Chemin | Quand | Ce qu'il faut |
| --- | --- | --- |
| **ITM** : le TCB sur le port 31, horodaté par l'ITM | une capture SWO tourne | `--swo-config`, ou Trace › ETM par SWO… ; inerte tant que le débogueur n'a pas activé l'ITM |
| **Journal en RAM** : l'anneau `gdbtrace_task_log` (256 entrées, 2 Ko), horodaté en cycles d'après SysTick et le compte des ticks | pas de capture SWO, et sur un Cortex-M0/M0+ (pas d'ITM) | un canal mémoire en marche (OpenOCD, QEMU) : la vue lit les nouvelles entrées pendant la marche, puis la fin à l'arrêt |

Par le journal en RAM, le temps net est le temps brut : pas de trace des exceptions. Si l'anneau se remplit entre deux lectures (sonde lente, changements très fréquents), les plus vieilles entrées sont perdues, et la vue le dit.

## Vue Threads (programme de l'hôte)

- Pour chaque thread : numéro gdb, nom (`pthread_setname_np`), LWP, état, cœur et position.
- Le thread courant est en gras ; celui qui a provoqué l'arrêt est rappelé en tête.
- **Double-clic** (programme arrêté) : le thread devient courant, et registres, pile, source et pas à pas portent sur lui.
- La liste suit la création et la fin des threads, même en marche. Tous les threads s'arrêtent ensemble (mode all-stop de gdb), sauf en mode non-stop.

**Mode non-stop** (`--non-stop`) : un thread s'arrête et repart seul, les autres continuent ; un breakpoint n'arrête que le thread qui l'atteint.

- Les vues suivent un thread, en gras : celui qui vient de s'arrêter, ou celui choisi (double-clic, ou clic droit › Suivre ce thread). Arrêté, il s'inspecte et avance en pas à pas pendant que les autres tournent.
- Clic droit › **Arrêter ce thread** / **Relancer ce thread** ; Go et Break valent pour tous les threads.
- Les breakpoints à action y sont des arrêts ordinaires.

Le programme a son propre terminal : sa sortie arrive dans la console. À la fin du programme, l'état passe à TERMINÉ avec son code de sortie ; **Go** le relance, **Reset** le relance arrêté au début de `main`. Un processus attaché (`--pid`) ne se relance pas : GdbTrace ne l'a pas lancé. Démo : `examples/native/pthreads.c` (producteur, consommateur et moniteur nommés).

## Pour aller plus loin

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
