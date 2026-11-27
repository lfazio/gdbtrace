## Capture SWO et vue ITM

La capture vient de trois sources :

- **OpenOCD** : `--swo-config --cpu-hz N` programme ITM, DWT et TPIU, configure le TPIU d'OpenOCD et se connecte à son port. Ajouter `--swo-hz` pour le débit et `--swo-port` pour le port TCP.
- **ST-LINK_gdbserver** : `--stlink --swo-config --cpu-hz N` lance le serveur avec sa capture SWO, programme ITM, DWT et la sortie SWO de la puce, et se connecte au flux servi par le serveur.
- **un serveur SWO en TCP** (Orbuculum, J-Link) : `--swo hôte:port`.
- **un fichier** de capture brute : `--swo-file`, avec `--swo-formatter` si le formateur TPIU était actif.

La vue **ITM** montre le texte UTF-8 de chaque port (port 0 par défaut), les autres ports en hexadécimal, avec filtre de ports et horodatage. Quand le décodage prend du retard sur le flux, le retard et les octets perdus sont affichés. Un octet perdu sur le fil compte comme une erreur dans le bilan de la capture, sans décaler les ports.

## Terminal (semihosting, RTT)

La vue **Terminal** montre ce que le programme écrit, et une ligne lui envoie du texte.

| Source | Option | Serveurs | Comment |
| --- | --- | --- | --- |
| Semihosting (BKPT 0xAB : `SYS_WRITE0`, `SYS_WRITE`, `SYS_READC`) | `--semihosting` | QEMU, OpenOCD | le serveur traite les appels et sert les entrées-sorties sur une socket |
| RTT (bloc de contrôle « SEGGER RTT » en RAM) | `--rtt [ADRESSE]` (défaut : le symbole `_SEGGER_RTT`) | tous | lu en marche par le canal mémoire (OpenOCD, relais ST-LINK), et par gdb à chaque arrêt |

- Le RTT lit le canal montant 0 et écrit le descendant 0 (la ligne d'envoi). Il faut écrire en marche pour avancer l'index de lecture : QEMU ne le sait pas par QMP, son RTT est donc lu aux arrêts seulement.
- Un programme qui appelle le semihosting sans débogueur qui le traite s'arrête sur le BKPT : ne l'appeler que si le serveur le sert.
- `examples/common/gdbtrace_rtt.c` est un bloc RTT minimal (1 Ko montant, 16 octets descendant) ; la démo FreeRTOS écrit par lui les lignes « consumer: commande N ». `examples/terminal` (QEMU) écrit par les deux, et renvoie en écho ce qu'il reçoit par le RTT.
- Dans un script : `gdbtrace.terminal()` (texte arrivé depuis l'appel précédent), `gdbtrace.terminal_send("texte")`.

## Profil

La vue **Profil** donne le pourcentage de temps CPU par fonction, par ligne (double-clic : la source) et par tâche FreeRTOS, sans instrumenter le code. « Auto » prend les échantillons de PC du SWO si une capture tourne, sinon la lecture du PC en marche (`DWT_PCSR` par OpenOCD, ou le PC de QEMU). L'onglet **Exceptions** (trace des exceptions de l'ITM) compte les entrées dans chaque exception et, avec l'horodatage ITM, donne le temps passé dedans (sans les exceptions plus prioritaires qui l'ont interrompue), la durée min et max, la période moyenne entre deux entrées, sa gigue et les préemptions ; en unités d'horodatage ITM.

Un profil qui marche ne prouve donc pas que le SWO est capturé : les vues ITM, Trace et Couverture disent si une capture tourne et ce qu'elle reçoit.

## Variables tracées

La vue **Variables tracées** suit la valeur d'une globale cible en marche, de deux façons (choix « source » de la vue) :

| Source | Comment | Il faut |
| --- | --- | --- |
| **DWT (trace)** | un comparateur du DWT surveille son adresse et émet la valeur par l'ITM à **chaque accès**, et au choix le PC de l'instruction | une capture SWO ; un M3, M4, M7, M55 |
| **échantillonné** (SNOOPer) | la variable est **lue toutes les N ms** par le canal mémoire, sans trace | un canal mémoire en marche (OpenOCD, ST-LINK, QEMU) ; marche aussi sur un Cortex-M0/M0+ |

- Lancement : dans la vue (cible arrêtée), ou `--var-trace counter`, `--var-trace offset:rw+pc`, `--snoop ticks@10` (toutes les 10 ms). Par défaut la vue propose « échantillonné » quand le cœur n'a pas d'ITM ou qu'aucune capture SWO ne tourne.
- L'échantillonnage n'est pas une trace : une valeur qui change et revient entre deux lectures n'est pas vue, et l'horodatage est celui de l'hôte (à la milliseconde). Variables de 1, 2, 4 ou 8 octets ; une période commune à toutes.
- Un petit graphe par variable, chacun à sa propre échelle ; le survol donne les valeurs à un instant.
- Limites : variables de 1, 2 ou 4 octets ; 4 comparateurs au plus sur un M3/M4/M7/M55, partagés avec les watchpoints et le filtre ETMv3. Sur un ARMv8-M (M33, M55), la variable doit être alignée sur sa taille ; GdbTrace reconnaît le DWT et le programme selon son format.

## Pour aller plus loin

### Trace SWO/ITM et profilage

**ITM (vue ITM, l'équivalent de TERM)** : le firmware écrit sur les ports de stimulus de l'ITM
(`ITM_SendChar` de CMSIS, ou `itm_puts` dans `examples/freertos/main.c`). Le flux sort par la
broche SWO et gdbtrace le décode : texte UTF-8 par port (port 0 par défaut), valeurs brutes en
hexa pour les autres ports, filtre de ports et horodatage. La capture vient :
- d'**OpenOCD** : `--swo-config` (ou « Configurer la cible… ») programme ITM, DWT et TPIU,
  configure le TPIU d'OpenOCD (`<cible>.tpiu configure -protocol uart … -output :3443`),
  puis se connecte au port ;
- d'**Orbuculum** ou de **J-Link** : `--swo HOST:PORT` ;
- d'un **fichier** de capture brute : `--swo-file` (avec `--swo-formatter` si le formateur
  TPIU était actif, par exemple sur une sonde ORBTrace).

**Profil (vue Profil, l'équivalent de PERF)** : profilage statistique sans instrumenter le
code. On obtient le % du temps CPU par fonction, par ligne (double-clic : la source s'ouvre) et
par tâche FreeRTOS. Sources des échantillons :

| Source | Comment | Cible |
|---|---|---|
| SWO | paquets « PC sample » du DWT (`--swo-config`, échantillonnage réglé via CYCTAP/POSTPRESET) | sonde + broche SWO |
| Live | lecture de `DWT_PCSR` par l'AHB-AP, cœur non arrêté | OpenOCD, Cortex-M0+ (ex. STM32G0), M3, M4, M7 ; optionnel en ARMv6-M |
| Live | PC de la vCPU lu par le moniteur (~2 000 éch./s) | QEMU |

« Auto » prend le SWO si une capture tourne, sinon le Live. Un profil qui marche ne prouve
donc pas que le SWO est capturé : la vue ITM et les vues Trace et Couverture disent si une
capture tourne et ce qu'elle reçoit.

L'onglet **Exceptions** compte les entrées dans chaque exception ou IRQ et, si l'horodatage
ITM est actif, le temps total et maximal passé dedans. Ces données viennent du SWO.

**Trace de données (vue Variables tracées, Var.TRACE / Var.DRAW)** : la valeur d'une
variable globale ou statique à chaque accès, cible en marche, sans l'arrêter ni modifier le
code.

- Un comparateur du DWT surveille l'adresse de la variable. Selon l'accès choisi (écriture,
  lecture, les deux), il émet la valeur par l'ITM, et au choix le PC de l'instruction qui y
  accède.
- La vue liste les variables tracées, leur nombre d'échantillons et leur dernière valeur (au
  format de la variable). Elle dessine un petit graphe par variable, chacun à sa propre
  échelle, en temps réel. Le survol donne les valeurs à un instant.
- Le temps vient de l'horodatage ITM (cycles du cœur, `--cpu-hz`), sinon de l'heure de
  réception.

| Lancement | Comment |
|---|---|
| Ligne de commande | `--var-trace counter` (écriture), `--var-trace offset:rw+pc` ; répétable ; avec une capture SWO ou de trace |
| GUI | vue Variables tracées : saisir l'expression, choisir l'accès, Tracer (cible arrêtée) |
| Script | `gdbtrace.var_trace("counter", "rw", pc=True)`, `gdbtrace.var_samples("counter")`, `gdbtrace.var_trace_stop()` |

Limites : variables scalaires de 1, 2 ou 4 octets (un champ ou un élément d'une structure),
4 comparateurs au plus sur un Cortex-M3/M4/M7/M55 (le DWT de l'ARMv8-M a son propre format,
programmé par GdbTrace ; la variable doit y être alignée sur sa taille). Ces comparateurs sont partagés avec le filtre
ETMv3 et les watchpoints de gdb ; GdbTrace les prend par le haut et refuse quand il n'en reste
plus. Un accès plus court que la variable est montré en hexadécimal brut. Validé sur des flux
ITM synthétiques et sur Nucleo-F746ZG (test sur carte).

QEMU n'émule ni l'ITM ni le SWO : sous QEMU, la sortie ITM du firmware reste inerte et
seul le profil « Live » fonctionne. Le décodage SWO est testé sur des flux synthétiques et un
faux serveur, et validé sur Nucleo-F746ZG (ST-Link, OpenOCD) : texte ITM, profil par SWO,
trace de données, temps des tâches.
