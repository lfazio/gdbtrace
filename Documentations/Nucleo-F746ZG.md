La Nucleo-F746ZG (Cortex-M7, 216 MHz, ST-Link V2-1 embarqué) est la carte de référence : GdbTrace y trace l'ETMv4 par la broche SWO, sans sonde de trace, et un test applicatif y vérifie toutes les vues à chaque release.

## Firmware d'exemple

`examples/nucleo-f746` construit la démo FreeRTOS de `examples/freertos` pour cette carte : même `main.c`, avec la configuration, le démarrage et le port FreeRTOS ARM_CM7 r0p1 de la carte. Le cœur y tourne à 16 MHz.

```bash
make -C examples/nucleo-f746
```

## Lancer GdbTrace sur la carte

Avec la configuration fournie (`configs/nucleo-f746zg.yaml`, ou Configuration › Ouvrir… puis Connecter) :

```bash
.venv/bin/python -m gdbtrace --config configs/nucleo-f746zg.yaml
```

Elle équivaut à :

```bash
.venv/bin/python -m gdbtrace examples/nucleo-f746/nucleo-f746.elf --program \
    --openocd configs/nucleo-f746zg.cfg --svd STM32F746.svd --cpu-hz 16000000 \
    --etm-config --etm-port swo --etm-stall --var-trace produced:write --os freertos
```

- `--etm-config --etm-port swo --etm-stall` : trace ETMv4 et ITM par la broche SWO. Le cœur ralentit au débit du SWO plutôt que de perdre de la trace. Dans l'interface, cible arrêtée : **Trace › ETM par SWO…**.
- `--cpu-hz` doit être la fréquence réelle du cœur, pour le débit SWO et la conversion en µs.
- Le fichier SVD vient de STM32CubeIDE.
- Plusieurs sondes branchées : `STLINK_SERIAL=<numéro de série>` choisit la bonne.
- La connexion se fait sous reset. Pour s'attacher à un cœur qui tourne sans le réinitialiser : `CONNECT_UNDER_RESET=0` et `--no-load`.

Le débit du SWO limite la trace sans perte : environ 3,5 MHz de cœur tracé avec un ST-Link V2-1, 20 à 35 MHz avec un STLINK-V3. Au-delà, `--etm-stall` est indispensable.

## Test sur carte

```bash
tools/release_check.sh
```

Ce script lance toute la suite, construit l'oracle OpenCSD s'il manque, et **exige** les cartes de test : la Nucleo-F746ZG, la Nucleo-N657X0-Q ([Sur une Nucleo-N657X0-Q (STM32N6)](Nucleo-N657X0-Q)) les Nucleo-G070RB et F072RB ([Sur une Nucleo-G070RB ou une Nucleo-F072RB (Cortex-M0+, Cortex-M0)](Nucleo-G070RB-et-F072RB)) et la Nucleo-F446RE ([Sur une Nucleo-F446RE (STM32F446)](Nucleo-F446RE)). Le test sur la F746 (`tests/test_board_f746.py`) :

1. construit et flashe le firmware d'exemple ;
2. lance GdbTrace par son vrai point d'entrée, interface comprise, avec la configuration `configs/nucleo-f746zg.yaml` : SVD, ETM et ITM par SWO, une variable tracée, FreeRTOS ;
3. déroule trois phases : arrêt sur `worker_step`, marche avec le profil, arrêt final ;
4. relève à chaque phase le contenu de toutes les vues dans `tests/artifacts/board-f746.json`, et un test par vue vérifie ce qui doit s'y afficher.

La carte est trouvée seule (sonde ST-LINK dans `/sys`, puis identité de la puce lue par OpenOCD). `GDBTRACE_F746_SERIAL` impose la sonde, `GDBTRACE_SVD_DIR` le dossier du SVD. Avec pytest, `--boards=auto` (défaut) ignore ces tests sans carte, `--boards=require` les fait échouer, `--boards=skip` les désactive.

Le test remplace le firmware de la carte, et la laisse sans ETM ni stall.
