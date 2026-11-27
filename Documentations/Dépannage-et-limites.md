Quand une capture ne démarre pas, GdbTrace le dit : le journal de mise en route s'affiche dans la console, et les vues ITM, Trace et Couverture donnent la cause. Commencer par là.

## Symptômes fréquents

| Symptôme | Cause probable | Que faire |
| --- | --- | --- |
| Le profil marche mais Trace et Couverture disent « pas de capture SWO » | le profil a pris la lecture du PC en marche, pas le SWO | lancer la capture : `--swo-config`, ou Trace › ETM par SWO… ; vérifier `--cpu-hz` |
| « la table ROM CoreSight de cette puce n'a pas d'ETM » | la puce n'a pas d'ETM (vue Système, onglet CoreSight) | trace ITM, profil, trace de données, ou MTB s'il y en a une |
| Programmation : « RAM trop petite », « RAM de l'algorithme à donner » | un FLM hors pack ne sait pas où se placer | `--flash-ram ADRESSE:TAILLE` (RAM libre au reset, au moins le code, 1 Ko de pile et une page) |
| Programmation : « fonction … sans retour », « arrêtée hors de son retour » | algorithme bloqué (horloge, watchdog) ou en faute | vérifier l'algorithme et sa RAM ; la vue Faute dit la faute |
| « puce … introuvable » ou « ambiguë » | pas de pack installé pour elle, ou un préfixe trop court | `--pack FICHIER`, `cpackget add …`, ou le nom complet (bouton Choisir…) |
| « attention : le pack décrit … la cible répond … » | autre puce que celle du pack | corriger `--device`, ou la sonde |
| Octets reçus mais aucun de l'ETM | ETM non programmé, ou `--cpu-hz` / débit SWO faux | `--etm-config` (ou Trace › ETM par SWO…), fréquence réelle du cœur |
| Trace à trous (« trace interrompue ») | le débit du port ne suit pas le cœur | `--etm-stall`, un filtre ETM, ou une horloge plus basse |
| Vues figées cible en marche | pas de canal mémoire : `--remote` sans `--tcl` | ajouter `--tcl` (OpenOCD), sinon les vues se rafraîchissent à l'arrêt |
| Écriture d'un registre de périphérique sans effet sous QEMU | QEMU 8.2 ignore ces écritures (QEMU 11.1 les applique) | GdbTrace le signale à la relecture ; sur une vraie carte, elles marchent |
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
| Configuration refusée (« serveur.stlink-ap : option du serveur stlink, pas de openocd ») | une clé du YAML ne va pas avec le serveur, l'OS ou les autres options | le message nomme la clé ; mêmes règles que la ligne de commande ([Référence des options](Référence-des-options)) |
| « SVD STM32G070.svd introuvable » | SVD donné par son nom, absent de STM32CubeIDE | donner son chemin, ou `GDBTRACE_SVD_DIR` ; la session continue sans vue Périphériques |
| Claude Code : serveur gdbtrace « failed » | interpréteur introuvable (chemin relatif), ou venv sans GdbTrace | chemin absolu de `.venv/bin/python` dans `claude mcp add` ; `claude mcp list` ; journal du serveur sur stderr |
| Outil MCP `evaluate` : « cible en marche » | variable jamais évaluée cible arrêtée, locale, ou cible sans canal mémoire | l'évaluer une fois après `halt` ; avec `--remote`, ajouter `--tcl` |
| « mauvaise sonde : derrière la sonde …, la puce est un Cortex-M7 » | plusieurs cartes branchées : OpenOCD a pris une autre sonde que celle de la carte | `STLINK_SERIAL=<numéro>` devant la commande ; le message liste les sondes branchées |
| « la sonde … est déjà prise par un autre programme » | une autre session GdbTrace, un OpenOCD ou STM32CubeIDE tient la sonde | fermer l'autre programme, ou choisir une autre sonde |
| OpenOCD refuse gdb : « timed out while waiting for target halted », puis « Cannot identify target » | cœur Cortex-M0/M0+ tenu sous reset (connexion sous reset) : un simple halt n'aboutit pas | `configs/nucleo-f072rb.cfg` et `nucleo-g070rb.cfg` font `reset halt` à l'attachement (`gdb-attach`) ; reprendre ce bloc dans une configuration OpenOCD maison |
| ETMv3 par SWO : le firmware se fige (pile dans une attente de l'ITM) | l'ETM prend toute la broche, l'ITM n'y passe plus | `--etm-filter`, ou mieux `--etm-start` / `--etm-stop` ([Sur une Nucleo-F446RE (STM32F446)](Nucleo-F446RE)) ; ou ralentir le cœur |
| « non pris en charge par cet ETM : cycle-accurate » | l'ETM-M3/M4 n'a pas ce mode | durées estimées ; mesurer avec un ETMv4 (M7, M55) |
| « Cortex-M0+ (ARMv6-M) : ni ITM, ni SWO, ni ETM » | le cœur n'a pas de trace (STM32G0, STM32F0, STM32L0…) | profil par lecture du PC (« Live ») ; MTB si `--mtb-probe` en trouve une |

## Limites connues

- **Validé sur carte** :
  - Nucleo-F746ZG (Cortex-M7, OpenOCD) : ETMv4 et ITM par SWO, trace de données du DWT, profil par SWO, vues RTOS et temps des tâches ;
  - Nucleo-N657X0-Q (Cortex-M55, ST-LINK_gdbserver) : vues en marche par le relais, image en RAM, ITM par le SWO de la sonde, ETMv4.5 par l'ETR (boucles à faible coût comprises), trace de données du DWT de l'ARMv8-M, vues RTOS et temps des tâches ;
  - Nucleo-F446RE (Cortex-M4F, OpenOCD) : ETMv3 et ITM par SWO, filtres de l'ETMv3 (plage et start/stop), trace de données du DWT, profil par SWO, vues RTOS et temps des tâches ;
  - Nucleo-F072RB (Cortex-M0, OpenOCD) : run-control (breakpoints à compteur et à message, Go.Till par le DWT), vues en marche, RTOS (en marche : état de chaque tâche lu dans son TCB), profil par `DWT_PCSR`, chrono par SysTick, temps des tâches par le journal en RAM lu en marche.
- **Pas encore validé sur carte** : Nucleo-G070RB (Cortex-M0+, OpenOCD ; test prêt : `tests/test_board_m0.py -k g070`), port de trace parallèle, MTB d'un vrai M0+, `--rtos` d'OpenOCD ; le chrono par `DWT_CYCCNT` (F746, N6) et le RTT lu en marche (tests sur carte prêts) ; le vector catch de l'arrêt sur faute et le semihosting d'OpenOCD.
- **Fautes** : un Cortex-M0/M0+ n'enregistre pas la cause, elle est déduite de l'instruction (une faute dans le gestionnaire lui-même, ou sur un cadre corrompu, peut tromper la déduction). Le cadre empilé vient du déroulement de gdb ; sans informations de débogage du gestionnaire, il est retrouvé par lr et MSP/PSP, au prix d'une base approximative.
- **CoreSight, MPU, puce, chargeur de flash** : validés sous QEMU (tables ROM sur des mémoires d'après les manuels, MPU ARMv7-M de l'an385 et ARMv8-M de l'an547, algorithmes de test) ; tests sur carte prêts, pas encore passés : tables ROM (F746, F446, N6, F072), MPU relue en entier, FLM du pack Keil.STM32F4xx_DFP sur la F446, chargeur externe de la flash XSPI de la N6. QEMU n'a pas de table ROM, et son gdbstub (8.2) ignore l'écriture de MPU_RNR par le débogueur : seule la région choisie (ARMv7-M), ou son groupe de quatre par les alias (ARMv8-M), est lue. MPU sécurisée et non sécurisée (TrustZone) : jalon 23. CMSIS-Pack : séquences de débogage et debugvars ignorées ; puce à plusieurs processeurs : le premier ; rien n'est téléchargé. Algorithmes de flash : un secteur touché est effacé en entier.
- **Échantillonnage** (SNOOPer) : horloge de l'hôte, à la milliseconde ; une période plus courte que la lecture d'une variable par le canal (1 à 2 ms) n'est pas tenue.
- **Compteurs** : ceux du DWT ont 8 bits (exacts au-delà de 256 cycles seulement avec une capture SWO) ; PMU validée sur le manuel du Cortex-M55, pas encore sur carte.
- **Terminal** : semihosting servi par QEMU et OpenOCD seulement (pas ST-LINK_gdbserver) ; RTT sous QEMU lu aux arrêts seulement ; un tampon RTT montant plein perd la suite (le bloc d'exemple n'attend jamais le débogueur).
- **Chrono** : sans compteur de ticks connu (`xTickCount`, `uwTick`), la durée n'est connue qu'à une période de SysTick près. Lire `SYST_CSR` peut effacer son drapeau COUNTFLAG pour le logiciel (selon la sonde) : un firmware qui l'attend en boucle peut en manquer un.
- **Modèles de cycles M0/M0+** : d'après les tables des manuels de référence ARM (mémoire sans attente, multiplieur rapide) ; la sortie d'exception et l'enchaînement, non documentés, sont estimés.
- **Cortex-M0/M0+** : ni ITM, ni SWO, ni ETM. Sur les STM32G0 et STM32F0, 4 breakpoints matériels et 2 watchpoints, que Go.Till partage avec les watchpoints posés.
- **ETMv4** : Cortex-M7 (par SWO) et Cortex-M55 (par l'ETR d'un STM32N6), cycle-accurate et horodatage compris. Sur le M55, le filtre ETM prend les comparateurs du DWT (4 plages au plus), et la branche d'une instruction « branch future » (BF, BFL…) n'est pas suivie ; les compilateurs n'en émettent pas.
- **ETR** : lu en continu tant que la trace ne dépasse pas ce que lit la sonde (~550 Ko/s) ; au-delà, des trous. En cycle-accurate, lu à l'arrêt : un tampon plein ne garde que la fin de l'exécution.
- **SWO d'un STLINK-V3EC (N6)** : un octet perdu de temps en temps à 2 Mbit/s, compté comme une erreur ITM.
- **Filtre ETMv3** : deux plages au plus, chacune agrandie en bloc aligné de 2^n octets ; une plage coûte une reprise de trace (6 octets) à chaque branchement, start/stop une par passage.
- **ETM-M4** : ni cycle-accurate ni comparateurs d'adresse ; sa FIFO fait 24 octets.
- **ETM simulé** : QEMU ne dit pas si une instruction conditionnelle d'un bloc IT (hors branchement) a été exécutée ; elle est comptée exécutée. Les branchements conditionnels sont exacts.
- **Durées** : sans cycle-accurate, ce sont des estimations par un modèle de cycles, sans attentes mémoire. En ETMv4, une mesure couvre un groupe de branchements : sa répartition entre les instructions du groupe reste celle du modèle.
- **Couverture** : c'est celle du code objet (instructions et branches). La couverture MC/DC demande une analyse du source, hors de portée de la trace.
- **QEMU chargé** : un breakpoint est parfois vu deux fois pour un seul passage, car le pas à pas de QEMU revient sans avoir avancé. Le compteur de passages et les messages d'action comptent alors ce doublon. C'est propre à QEMU.
