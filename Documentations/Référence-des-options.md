`python -m gdbtrace [ELF] [options]`. L'ELF est facultatif avec l'interface : Fichier › Programmer un ELF… le choisit ensuite. `--help` liste les mêmes options.

## Cible et session

| Option | Effet |
| --- | --- |
| `--qemu` | cible QEMU (défaut pour un ELF ARM) |
| `--machine M` | machine QEMU (`mps2-an385` par défaut ; `microbit`, `mps2-an500`, `mps2-an505` (TrustZone), `mps2-an521` (deux cœurs), `mps3-an547`…) |
| `--qemu-load ELF` | QEMU : autre image chargée avec l'ELF (`-device loader`, rechargée à chaque reset) : image non sécurisée, bootloader… ; répétable |
| `--openocd CFG…` | lancer OpenOCD avec ces fichiers `-f` |
| `--stlink [SN]` | lancer ST-LINK_gdbserver sur la sonde de numéro de série SN (à donner si plusieurs sondes sont branchées) |
| `--stlink-ap N` | avec `--stlink` : port d'accès du cœur (STM32N6 : 1) |
| `--ram-boot` | avec `--stlink` : image exécutée en RAM (reset système, chargement, sp et pc pris dans sa table des vecteurs) |
| `--remote H:P` | serveur gdb déjà lancé |
| `--tcl H:P` | avec `--remote` : port TCL d'un OpenOCD lancé à part (mémoire en marche) |
| `--native` | programme de l'hôte (défaut pour un ELF non ARM) |
| `--pid PID` | s'attacher à un programme de l'hôte déjà lancé (ELF par défaut : `/proc/PID/exe`) |
| `--non-stop` | programme de l'hôte : mode non-stop de gdb (un thread s'arrête et repart seul) |
| `--remote-exec CHEMIN` | programme de l'hôte servi par `gdbserver --multi` : son chemin côté serveur |
| `--args 'A B…'` | programme de l'hôte : ses arguments (syntaxe shell) |
| `--cwd DOSSIER` | programme de l'hôte : son répertoire de travail |
| `--svd F` | description CMSIS-SVD des périphériques |
| `--device NOM` | puce décrite par son CMSIS-Pack : SVD, mémoires, algorithmes de flash, cœur attendu |
| `--pack F` | CMSIS-Pack de la puce (`.pack`, `.pdsc`, dossier) ; sans lui, les packs installés |
| `--rtos [TYPE]` | avec `--openocd` : tâches RTOS vues comme threads gdb |
| `--gdb GDB`, `--gdb-port P` | exécutable gdb, port du serveur gdb (avec `--stlink` : port du relais) |
| `--no-load` | ne pas flasher l'ELF à la connexion |
| `--layout F` | fichier de layout des fenêtres |
| `--config F.yaml` | configuration de session ([Lancer GdbTrace](Lancer-GdbTrace)) ; les autres options l'emportent sur elle |
| `--core NOM[=SOURCE][,ELF]` | plusieurs cœurs ([TrustZone et multi-cœur](TrustZone-et-multi-cœur)) : sans SOURCE, le nom du cœur de la session ; `=N` : processus N du même serveur (QEMU `mps2-an521` : 2) ; `=HÔTE:PORT` : autre serveur gdb ; `,ELF` : ses symboles ; répétable |
| `--os aucun\|freertos\|pthread` | OS de la cible : vues RTOS et Temps des tâches (FreeRTOS), Threads (Pthread), aucune des trois (aucun) ; sans `--os`, toutes les vues |

## Programmation, scripts, batch

| Option | Effet |
| --- | --- |
| `--program [ELF]` | programmer l'ELF à la connexion (reset, écriture, vérification, reset) |
| `--no-verify` | sans vérification |
| `--flash-loader F` | algorithme de flash exécuté sur la cible (`.FLM`, `.stldr`, `pack`) ; répétable |
| `--flash-ram ADRESSE:TAILLE` | RAM de l'algorithme de flash |
| `--go` | lancer le programme après la connexion |
| `--script S` | script Python exécuté après la connexion |
| `--batch` | sans interface : exécute `--script` puis quitte |

## Trace SWO et ITM

| Option | Effet |
| --- | --- |
| `--swo H:P` | capture SWO servie en TCP |
| `--swo-file F` | rejouer une capture SWO brute |
| `--swo-formatter` | flux SWO avec formateur TPIU |
| `--swo-config` | programmer ITM, DWT et TPIU (ou le SWO de la puce) à la connexion, et démarrer la capture (OpenOCD, ST-LINK_gdbserver) |
| `--cpu-hz N`, `--swo-hz N`, `--swo-port P` | fréquence du cœur, débit SWO, port TCP du SWO d'OpenOCD |
| `--var-trace EXPR[:ACCÈS]` | trace de données du DWT ; accès `write`, `read`, `rw`, suivi de `+pc` ; répétable |
| `--snoop EXPR[@MS]` | variable échantillonnée par le canal mémoire, sans trace (toutes les 10 ms par défaut) ; répétable |

## Trace d'instructions ETM

| Option | Effet |
| --- | --- |
| `--etm-sim` | QEMU : ETM simulé |
| `--trace H:P`, `--trace-file F` | flux TPIU formaté en TCP, ou rejoué depuis un fichier |
| `--etm-config` | programmer l'ETM et sa sortie (TPIU, ou ETR) à la connexion |
| `--etm-port parallel\|swo\|etr` | sortie de l'ETM : port parallèle, broche SWO, ou tampon en RAM écrit par l'ETR (STM32N6) |
| `--etr-buffer ADRESSE:TAILLE` | avec `--etm-port etr` : tampon de l'ETR (défaut : le symbole `__etr_buffer__` du firmware) |
| `--etr-at-stop` | avec `--etm-port etr` : lire le tampon seulement à l'arrêt (par défaut : en continu, sauf en cycle-accurate) |
| `--etm-width 1\|2\|4` | largeur du port parallèle |
| `--etm-stall` | ralentir le cœur plutôt que perdre de la trace |
| `--etm-cycle-accurate` | durées mesurées (environ 8 fois plus de trace en ETMv3, 3 fois en ETMv4) |
| `--etm-timestamps` | horodatage de la trace ETMv4 (compteur de temps global de la puce) |
| `--stm32` | activer les broches de trace (DBGMCU_CR des STM32) |
| `--etm-filter PLAGE` | ne tracer que cette fonction, ce fichier ou ces adresses ; répétable |
| `--etm-start LIEU`, `--etm-stop LIEU` | démarrer, arrêter la trace sur un lieu |

## Fautes et terminal

| Option | Effet |
| --- | --- |
| `--break-on-fault` | arrêter la cible à l'entrée de toute faute (vector catch, sinon breakpoints sur les gestionnaires) |
| `--semihosting` | semihosting servi par QEMU ou OpenOCD, dans la vue Terminal |
| `--rtt [ADRESSE]` | RTT : bloc de contrôle à cette adresse (défaut : `_SEGGER_RTT`), dans la vue Terminal |

## MTB et couverture

| Option | Effet |
| --- | --- |
| `--mtb [ADRESSE]` | activer la MTB de la cible |
| `--mtb-buffer ADRESSE:TAILLE` | tampon de la MTB en SRAM |
| `--mtb-sim [TAILLE]` | QEMU : MTB simulée |
| `--mtb-autohalt` | trace complète, au prix d'arrêts internes |
| `--mtb-probe` | lister les composants CoreSight de la cible |
| `--coverage F` | écrire la couverture à la fin du `--batch`, ou du script dans l'interface (`.html`, `.info`, `.json`) ; répétable |
| `--coverage-merge F.json` | ajouter une couverture enregistrée ; répétable |

## Fichier de configuration (YAML)

Une configuration ([Lancer GdbTrace](Lancer-GdbTrace)) est un fichier YAML. Les sections ont des noms français ; dans `serveur` et `trace`, les clés sont les noms des options de la ligne de commande, sans leurs tirets de tête.

```yaml
gdbtrace: 1                        # version du format
nom: Nucleo-N657X0-Q
cible:
  elf: ../examples/nucleo-n6/nucleo-n6.elf
  svd: STM32N657.svd
  cpu-hz: 64000000
  programmer: true
  verifier: true                   # false : --no-verify
  go: false
serveur:
  type: stlink
  stlink-ap: 1
  ram-boot: true
os: freertos
script: ../examples/scripts/rtos.py
couverture:
  fusionner: [campagne1.json]
  exporter: [couverture.html, couverture.json]
trace:
  swo-config: true
  etm-config: true
  etm-port: etr
  var-trace: [produced:write]
```

| Section | Clés | Équivalent en ligne de commande |
| --- | --- | --- |
| `cible` | `elf`, `svd`, `pack`, `device`, `cpu-hz`, `programmer` (`true`, ou le chemin d'une image HEX, S-record, binaire), `verifier`, `go`, `break-on-fault`, `add-symbols` (liste), `flash-loader` (liste), `flash-ram`, `coeurs` (liste de `NOM[=SOURCE][,ELF]`) | l'ELF, `--svd`, `--pack`, `--device`, `--cpu-hz`, `--program`, `--no-verify` (si `false`), `--go`, `--break-on-fault`, `--add-symbols`, `--flash-loader`, `--flash-ram`, `--core` |
| `serveur` | `type` : `qemu`, `openocd`, `stlink`, `gdb`, `remote` | `--qemu`, `--openocd`, `--stlink`, `--native`, `--remote` |
| `serveur`, QEMU | `machine`, `qemu-load` (liste) | `--machine`, `--qemu-load` |
| `serveur`, OpenOCD | `openocd` (liste de `.cfg`), `rtos` | `--openocd CFG…`, `--rtos` |
| `serveur`, ST-LINK | `stlink` (numéro de série), `stlink-ap`, `ram-boot` | `--stlink SN`, `--stlink-ap`, `--ram-boot` |
| `serveur`, programme de l'hôte | `args`, `cwd`, `pid`, `non-stop` | `--native --args`, `--cwd`, `--pid` (sans `--native`), `--non-stop` |
| `serveur`, déjà lancé | `remote`, `tcl` ; programme de l'hôte servi par gdbserver : `remote-exec`, `args`, `cwd`, `non-stop` | `--remote`, `--tcl`, `--remote-exec`, `--args`, `--cwd`, `--non-stop` |
| `serveur`, commun | `gdb`, `gdb-port`, `no-load` | `--gdb`, `--gdb-port`, `--no-load` |
| `os` | `aucun`, `freertos`, `pthread` | `--os` |
| `script` | chemin | `--script` |
| `couverture` | `fusionner`, `exporter` (listes) | `--coverage-merge`, `--coverage` |
| `trace` | toute option de trace et du terminal : `swo-config`, `swo-hz`, `etm-config`, `etm-port`, `var-trace`, `etm-filter`, `etm-sim`, `mtb`, `semihosting`, `rtt`… | la même option |

- Une option sans valeur s'écrit `true` ; une option répétable s'écrit en liste.
- Un chemin relatif l'est au fichier YAML. Exceptions : un fichier `.cfg` qui n'est pas à côté du YAML est cherché par OpenOCD dans son dossier de scripts (`board/st_nucleo_f4.cfg`), et un SVD donné par son seul nom est cherché dans STM32CubeIDE (`GDBTRACE_SVD_DIR` pour un autre dossier) ; de même un chargeur de flash donné par son nom (ou `pack`) n'est pas un chemin.
- À l'enregistrement, un chemin proche du fichier (au plus deux `..`) est écrit relatif, les autres absolus.
- Une clé inconnue, ou qui ne va pas avec le serveur choisi, est refusée en la nommant.
