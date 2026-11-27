La Nucleo-F446RE porte un STM32F446RE : un Cortex-M4F à 16 MHz au reset (HSI), 512 Ko de flash et 128 Ko de RAM, avec un ST-Link V2-1 embarqué. Son boîtier LQFP64 n'a pas de port de trace parallèle : l'ETM (ETMv3.5) passe par la broche SWO (PB3), avec l'ITM. GdbTrace y passe par OpenOCD.

## Firmware d'exemple

`examples/nucleo-f446` construit la démo FreeRTOS de `examples/freertos` pour cette carte : même `main.c`, horloge HSI de 16 MHz, port FreeRTOS de la F746 (ARM_CM7 r0p1, valable sur tout ARMv7E-M à FPU simple précision).

```bash
make -C examples/nucleo-f446
```

## Lancer GdbTrace sur la carte

Avec la configuration fournie (`configs/nucleo-f446re.yaml`, ou Configuration › Ouvrir… puis Connecter) :

```bash
.venv/bin/python -m gdbtrace --config configs/nucleo-f446re.yaml
```

Elle équivaut à :

```bash
.venv/bin/python -m gdbtrace examples/nucleo-f446/nucleo-f446.elf --program \
    --openocd configs/nucleo-f446re.cfg --svd STM32F446.svd --cpu-hz 16000000 \
    --etm-config --etm-port swo --etm-stall --etm-start worker_step --etm-stop 0x080002ac \
    --var-trace produced:write
```

- `0x080002ac` est l'adresse du `bx lr` de `worker_step` dans ce firmware (Désassemblage, ou `arm-none-eabi-objdump -d`) ; le test sur carte vérifie qu'elle n'a pas changé.
- Plusieurs sondes branchées : `STLINK_SERIAL=<numéro de série>` choisit la bonne.
- La connexion se fait sous reset ; `CONNECT_UNDER_RESET=0` et `--no-load` pour s'attacher sans reset.

## L'ETMv3 d'un Cortex-M4 par le SWO

Ce qu'on a constaté sur cette carte, et ce que GdbTrace en fait :

| Constat | Conséquence |
| --- | --- |
| L'ETMv3 sort environ 1 bit par instruction ; le SWO d'un ST-Link V2-1 tient 2 Mbit/s, soit un cœur à ~3 MHz | à 16 MHz, sans filtre, la trace prend toute la broche. L'ITM n'y passe plus : un firmware qui attend la FIFO de l'ITM (printf ITM bloquant, temps des tâches) reste bloqué. GdbTrace le rappelle à la programmation de l'ETM. **Filtrer la trace.** |
| `--etm-stall` n'agit qu'avec le seuil FIFOFULL de l'ETM (ETMFFLR, nul au reset) | GdbTrace le programme : seuil de 24 octets, toute la FIFO de l'ETM-M4. Plus aucun débordement. |
| L'ETM-M4 n'a pas de mode cycle-accurate (le bit d'ETMCR se relit à 0) | `--etm-cycle-accurate` est signalé « non pris en charge par cet ETM », et les durées restent estimées par le modèle de cycles. |
| Filtre par plage (`--etm-filter`) : l'événement du DWT est en retard d'une instruction ; la trace se coupe et reprend à chaque branchement, et chaque reprise coûte 6 octets | GdbTrace comble ces trous quand le code dit ce qui s'est exécuté, mais le débit part en reprises. Pour une fonction, `--etm-start` sur son entrée et `--etm-stop` sur son retour : une seule reprise par appel, et vingt fois moins de trace par instruction (les fonctions appelées sont tracées aussi). |
| Le TPIU n'émet sa synchronisation qu'au démarrage | une capture connectée après retrouve l'alignement des trames dans les données (flux ITM et ETM). |

Sur cette carte, le décodage de GdbTrace est identique à celui d'OpenCSD, et la boucle de `worker_step(n)` tourne exactement n % 24 fois sur des centaines d'appels consécutifs.

## Test sur carte

`tests/test_board_f446.py` fait comme sur la F746 :

1. construit et flashe le firmware d'exemple ;
2. lance l'interface avec le SVD, l'ETM et l'ITM par SWO (stall), l'ETM démarré à l'entrée de `worker_step` et arrêté à son retour, et une variable tracée par le DWT ;
3. relève et vérifie toutes les vues (`tests/artifacts/board-f446.json`).

En plus, les tours de boucle de chaque appel de `worker_step` doivent suivre le firmware, et un filtre par plage, en mode batch, doit donner la même trace qu'OpenCSD. La carte est trouvée seule : sonde ST-LINK dans `/sys`, puis identité de la puce (DEV_ID 0x421) lue par OpenOCD. `GDBTRACE_F446_SERIAL` impose la sonde.
