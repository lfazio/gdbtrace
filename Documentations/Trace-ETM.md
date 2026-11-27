## Trace d'instructions ETM

| Source | Option | Pour qui |
| --- | --- | --- |
| ETM simulé | `--etm-sim` | QEMU : son journal d'exécution est converti en flux ETM à chaque arrêt |
| Broche SWO | `--etm-config --etm-port swo --cpu-hz N`, ou **Trace › ETM par SWO…** | une sonde de débogage (ST-Link) avec OpenOCD ; faible débit |
| Port de trace parallèle | `--etm-config [--etm-width 4] [--stm32]` puis `--trace hôte:port` | une sonde de trace et une carte qui sort TRACECLK et TRACED0-3 |
| Tampon en RAM (ETR) | `--etm-config --etm-port etr`, ou **Trace › ETM par l'ETR** | STM32N6 : la puce écrit la trace en RAM, GdbTrace la lit en continu ([Sur une Nucleo-N657X0-Q (STM32N6)](Nucleo-N657X0-Q)) |
| Fichier | `--trace-file` | rejouer une capture |

GdbTrace lit la version de l'ETM sur la cible : ETMv3 pour un M3/M4, ETMv4 pour un M7 ou un M55. `--etm-stall` ralentit le cœur plutôt que de perdre de la trace quand la FIFO de l'ETM déborde ; il est indispensable par SWO sur un cœur rapide. Une option que l'ETM n'a pas (cycle-accurate sur un ETM-M4) est signalée à la programmation. Un ETMv3 par SWO, sans filtre, peut affamer l'ITM : [Sur une Nucleo-F446RE (STM32F446)](Nucleo-F446RE).

**Filtre** (`--etm-filter`, `--etm-start`, `--etm-stop`, ou **Trace › Filtre de trace ETM…**) : ne tracer qu'une fonction, un fichier ou une plage d'adresses, ou démarrer et arrêter la trace sur un lieu. Le débit va alors à ce qui compte. Chaque reprise de la trace est marquée « trace interrompue ». Sur un Cortex-M55, dont l'ETM n'a pas de comparateurs d'adresse, le filtre passe par les comparateurs du DWT : une paire par plage, un par start ou stop, partagés avec les variables tracées. Sur un M3/M4 (ETMv3) aussi : une plage y coûte cher en débit, start/stop beaucoup moins ([Sur une Nucleo-F446RE (STM32F446)](Nucleo-F446RE)).

La vue **Trace** montre :

- **Listing** : les N dernières instructions avant l'arrêt, avec fonction, désassemblage, ligne source, exceptions et retours ;
- **Appels** : appels, retours et exceptions, indentés par profondeur.

**Chercher** (Trace.Find) dans le listing : une adresse (`0x8000120`), un nom de fonction (chaque entrée dans la fonction), `exception` ou `exception SysTick` (ou son numéro), sinon du texte (instruction, fonction, fichier). ◀ et ▶ passent d'une occurrence à l'autre, **Tout trouver** les surligne toutes. **Exporter…** écrit le listing en CSV (index, type, adresse, fonction, instruction, fichier, ligne). Dans un script : `trace_find`, `trace_export`.

En capture continue, Trace et Timing se redessinent à l'arrêt, et la Couverture ne met à jour que son résumé en marche : l'interface reste fluide.

## Timing

La vue **Timing** donne, par fonction et par exception, le nombre d'appels, le temps brut (interruptions déduites), le temps net (sans les appelés), le min, le max et la moyenne. Un chronogramme montre les appels dans le temps (molette : zoom ; glisser : déplacer ; clic : source).

- **Arbre d'appels** (Trace.STATistic.TREE) : les mêmes durées **par chemin d'appel** (`main › update_sensor › fib`) ; une exception est la racine de son propre arbre. Les nœuds se déplient à la demande, et restent ouverts d'une analyse à l'autre ; double-clic : la source. La somme des temps nets de l'arbre égale celle des fonctions.
- **Exceptions** (Trace.STATistic.InterruptIsr) : par exception, les entrées, la durée min/moy/max (sans les exceptions plus prioritaires qui l'ont interrompue), la période entre deux entrées et sa **gigue** (plus longue − plus courte), les préemptions (préemptée, préempte), les entrées enchaînées (tail-chaining) et les cycles d'entrée. La latence entre la demande d'interruption et l'entrée ne se voit pas dans une trace.

Les durées sont **mesurées** avec `--etm-cycle-accurate`, et **estimées** par un modèle de cycles sinon ; la vue indique toujours laquelle. Unités : cycles ou µs (`--cpu-hz`, 25 MHz par défaut).

- ETMv3 (M3/M4) : chaque instruction porte ses cycles ; environ 8 fois plus de trace.
- ETMv4 (M7, M55) : l'ETM compte les cycles par groupe de branchements ; le modèle répartit chaque mesure entre les instructions du groupe. Environ 3 fois plus de trace : par SWO, le stall s'impose ; par l'ETR, le tampon garde moins d'exécution.

Le modèle ne connaît ni les attentes mémoire ni les caches : sur une STM32N6 sans cache, les durées mesurées sont environ 25 fois les estimées.

Le modèle suit le cœur détecté : Cortex-M3/M4 pour la trace ETM ; Cortex-M0 ou Cortex-M0+ (tables de cycles de leurs manuels de référence ARM) pour la MTB, réelle ou simulée (QEMU `microbit` : un Cortex-M0). La vue le nomme : « estimé (modèle Cortex-M0, MTB simulée) ».

`--etm-timestamps` (ETMv4) insère dans la trace le compteur de temps global de la puce, à chaque synchronisation ; Trace.List le montre (« horodatage N »).

## Couverture

La vue **Couverture** déduit la couverture du code objet de la trace d'instructions, sans recompiler ni instrumenter :

- **instructions** exécutées ;
- **branches** : chaque branchement conditionnel doit avoir été vu pris et non pris.

Elle liste fichiers et fonctions (ok, partiel, jamais). **Annoter Source et Désassemblage** marque chaque ligne (✔ ½ ✘) et nomme l'issue manquante d'un branchement. La base s'accumule d'exécution en exécution jusqu'à **RàZ** ; **Enregistrer** et **Charger** cumulent des campagnes. Rapports : HTML autonome et LCOV (`.info`).

Tant qu'aucune instruction n'arrive, les vues Couverture et Trace disent pourquoi : pas de capture, sonde qui ne sert pas le SWO, flux sans formateur, aucun octet de l'ETM.

## Pour aller plus loin

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
