GdbTrace est un débogueur pour microcontrôleurs Cortex-M, avec trace. Il est construit sur gdb (GDB/MI) et a une interface graphique tkinter. Le même outil débogue aussi un programme de l'hôte (x86-64, AArch64…).

Il couvre trois usages :

- **le débogage classique** : run-control, breakpoints, variables, mémoire, registres, périphériques, RTOS ;
- **la trace**, pour voir ce qui s'est passé avant l'arrêt et en mesurer le temps : ITM/SWO, profil, ETM, MTB, couverture de code ;
- **le débogage programmatique**, en Python, depuis la console ou en batch.

## Cibles prises en charge

| Cible | Serveur gdb | Option | Remarques |
| --- | --- | --- | --- |
| Cortex-M sans matériel | QEMU (`mps2-an385` par défaut, `microbit` pour un M0+, `mps2-an500` pour un M7) | aucune (défaut) | GdbTrace lance QEMU et y charge l'ELF ; ETM ou MTB simulés possibles |
| Carte avec sonde | OpenOCD, lancé par GdbTrace | `--openocd fichier.cfg` | flash, live, SWO, ETM réels |
| Carte STM32 avec sonde ST-LINK | ST-LINK_gdbserver (STM32CubeIDE, STM32CubeCLT), lancé par GdbTrace | `--stlink [numéro de série]` | flash ou RAM (`--ram-boot`), live, SWO ; puces qu'OpenOCD ne connaît pas (STM32N6) |
| Serveur déjà lancé | J-Link GDB Server, pyOCD, OpenOCD… | `--remote hôte:port` (+ `--tcl` pour OpenOCD) | GdbTrace s'y connecte |
| Programme de l'hôte | gdb local | aucune : l'ELF est reconnu | pthreads, arguments avec `--args` ; `--pid` pour un processus déjà lancé ; `--non-stop` |
| Programme de l'hôte servi par gdbserver | gdbserver (`--multi`, ou avec le programme) | `--remote hôte:port` | lancé ou repris par GdbTrace, arrêt au début de `main` |

## Installation

```bash
sudo apt install python3-tk gdb-multiarch openocd qemu-system-arm gcc-arm-none-eabi
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
```

L'extra `dev` installe pytest et capstone, nécessaire au décodage de la trace ETM. Les outils externes ne servent qu'à ce qui les demande :

| Outil | Sert à |
| --- | --- |
| `gdb-multiarch` | tout : c'est le moteur de débogage |
| `qemu-system-arm` | déboguer sans carte, ETM et MTB simulés |
| `openocd` | sondes ST-Link, CMSIS-DAP… ; canal mémoire en marche ; capture SWO |
| STM32CubeIDE ou STM32CubeCLT | `--stlink` : ST-LINK_gdbserver et le STM32CubeProgrammer dont il a besoin, cherchés dans le PATH puis sous `/opt/st` et `~/st` |
| `arm-none-eabi-gcc` | compiler les firmwares d'exemple (`examples/`) |
| `xvfb-run` | lancer les tests de l'interface sans écran |

Pour un premier essai sans matériel :

```bash
make -C examples/firmware
.venv/bin/python -m gdbtrace examples/firmware/demo.elf
```
