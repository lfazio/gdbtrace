La trace dit ce que la cible a fait sans l'arrêter. Deux familles : l'**ITM**, sorti par la broche SWO (texte, échantillons de PC, valeurs de variables, changements de tâche), et la **trace d'instructions** (ETM, MTB), qui reconstruit chaque instruction exécutée.

| Ce qu'on veut | Vue | Il faut |
| --- | --- | --- |
| Texte écrit par le firmware | ITM | capture SWO |
| Temps CPU par fonction, ligne, tâche | Profil | capture SWO, ou lecture du PC en marche (OpenOCD, QEMU ; aussi sur un Cortex-M0+ qui a `DWT_PCSR`) |
| Valeur d'une variable à chaque accès | Variables tracées | capture SWO |
| Temps par tâche RTOS | Temps des tâches | crochet du firmware ([RTOS et threads](RTOS-et-threads)) : capture SWO, ou journal en RAM lu en marche |
| Temps entre deux arrêts | barre d'état (chrono) | rien : SysTick ou `DWT_CYCCNT` de la cible |
| Les instructions avant l'arrêt, le flot d'appels | Trace | ETM ou MTB |
| Durées par fonction et par exception | Timing | ETM ou MTB |
| Couverture de code | Couverture | ETM ou MTB |

QEMU n'émule ni l'ITM ni le SWO : sous QEMU, seuls le profil par lecture du PC, l'ETM simulé et la MTB simulée fonctionnent.

Un Cortex-M0 ou M0+ (ARMv6-M) n'a ni ITM, ni SWO, ni ETM : y fonctionnent le profil par lecture du PC, le temps des tâches par le journal en RAM, le chrono, et la MTB si la puce en a une. GdbTrace reconnaît le cœur à la connexion, et les vues de trace le disent ([Sur une Nucleo-G070RB ou une Nucleo-F072RB (Cortex-M0+, Cortex-M0)](Nucleo-G070RB-et-F072RB)).

Pages : [Trace SWO et ITM](Trace-SWO-et-ITM) (texte, profil, variables tracées, terminal), [Trace ETM](Trace-ETM) (instructions, timing, couverture), [Trace MTB](Trace-MTB) (Cortex-M0+). Sans trace : le chrono et les compteurs, ci-dessous.

## Chrono (temps entre deux arrêts)

À chaque arrêt, la barre d'état donne le temps du cœur depuis la dernière reprise (Go jusqu'à un breakpoint, Go.Till, pas, Break), et le cumul : « ⏱ 4,98 ms (Σ 12,3 ms, SysTick) ». **Run › Remettre le chrono à zéro** efface le cumul. Dans un script : `gdbtrace.chrono()`.

| Cœur | Source | Précision |
| --- | --- | --- |
| Cortex-M0, M0+ (et QEMU, en temps simulé) | SysTick, avec le compte des ticks du logiciel : `xTickCount` (+ `xPendedTicks`) de FreeRTOS, `uwTick` de la HAL STM32 | au cycle ; sans compteur de ticks, à une période de SysTick près (« ≈ ») |
| Cortex-M3, M4, M7, M55 (carte) | `DWT_CYCCNT`, activé à la première mesure | au cycle ; les tours de 2³² cycles sont rattrapés par le compte des ticks |

Le cœur arrêté par le débogueur, ces compteurs n'avancent pas : les arrêts ne comptent pas. Les arrêts internes (breakpoints à message, AUTOHALT de la MTB) ne relancent pas la mesure. Après un reset, une programmation ou une connexion, la mesure suivante attend une nouvelle reprise. Conversion en µs et ms : `--cpu-hz`.

## Compteurs (vue Compteurs)

La vue **Compteurs** donne, pour la dernière exécution (Go jusqu'à un breakpoint, pas…) et en cumul, les compteurs de performance du cœur. Cocher **Mesurer** (cible arrêtée) les arme ; la mesure vient à l'exécution suivante.

| Cœur | Compteurs |
| --- | --- |
| Cortex-M3, M4, M7 (DWT) | cycles ; CPI (attente des instructions multicycles), EXC (surcoût des exceptions), SLEEP, LSU (attente des chargements et rangements), FOLD (instructions repliées) ; instructions déduites (cycles − CPI − EXC − SLEEP − LSU + FOLD) et IPC |
| Cortex-M55 (PMU de l'ARMv8.1-M) | cycles, instructions retirées, remplissages des caches de données et d'instructions, exceptions prises (compteurs de 16 bits chaînés en 32 bits) ; IPC |
| Cortex-M0/M0+, QEMU | absents : la vue le dit |

Les compteurs du DWT n'ont que 8 bits : leurs tours sont rattrapés par les paquets de débordement de l'ITM quand une capture SWO tourne ; sans capture, une exécution de plus de 256 cycles n'en donne la valeur que modulo 256 (marquée « ≈ »). Armés, ils émettent ces paquets sur le SWO : un débit de plus. Rien n'est remis à zéro sur la cible. Dans un script : `gdbtrace.counters()`.

## Pour aller plus loin

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
