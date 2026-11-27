## MTB (Cortex-M0+)

Le Cortex-M0+ n'a ni ETM ni SWO. Sa MTB écrit en SRAM un paquet à chaque branchement : c'est un enregistreur de vol des derniers branchements avant l'arrêt. Trace, Timing et Couverture fonctionnent comme avec l'ETM.

- `--mtb` active la MTB de la cible ; `--mtb-probe` liste les composants CoreSight pour savoir si la puce en a une.
- Le firmware réserve le tampon : `__mtb_buffer__`, aligné sur sa taille, ou `--mtb-buffer adresse:taille`.
- `--mtb-autohalt` donne une trace complète : la MTB arrête le cœur à chaque tampon plein, GdbTrace le vide et relance, sans arrêt visible. La cible ralentit beaucoup ; préférer un tampon de 4 à 8 Ko.
- `--mtb-sim` simule la MTB sous QEMU (`--machine microbit`, `examples/m0plus`).
- La MTB est optionnelle : les STM32G0 et STM32F0, par exemple, n'en ont pas ([Sur une Nucleo-G070RB ou une Nucleo-F072RB (Cortex-M0+, Cortex-M0)](Nucleo-G070RB-et-F072RB)).

## Pour aller plus loin

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
