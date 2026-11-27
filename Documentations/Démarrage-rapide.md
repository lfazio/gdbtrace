Les commandes les plus courantes, de la démo sous QEMU aux cartes réelles. Le détail est dans [Lancer GdbTrace](Lancer-GdbTrace) et les pages des cartes.

```bash
make -C examples/firmware                                        # firmware de démo Cortex-M3
.venv/bin/python -m gdbtrace examples/firmware/demo.elf          # QEMU mps2-an385, sans matériel
.venv/bin/python -m gdbtrace fw.elf --openocd configs/nucleo-f429zi.cfg   # sonde ST-Link
.venv/bin/python -m gdbtrace fw.elf --stlink                      # ST-LINK_gdbserver (STM32CubeIDE / CubeCLT)
make -C examples/nucleo-n6 && .venv/bin/python -m gdbtrace examples/nucleo-n6/nucleo-n6.elf --stlink \
    --stlink-ap 1 --ram-boot --program --cpu-hz 64000000 --swo-config --etm-config --etm-port etr  # STM32N6
make -C examples/nucleo-g070 && .venv/bin/python -m gdbtrace examples/nucleo-g070/nucleo-g070.elf \
    --openocd configs/nucleo-g070rb.cfg --program --cpu-hz 16000000 --svd STM32G070.svd  # STM32G0 (M0+)
make -C examples/nucleo-f072 && .venv/bin/python -m gdbtrace examples/nucleo-f072/nucleo-f072.elf \
    --openocd configs/nucleo-f072rb.cfg --program --cpu-hz 8000000 --svd STM32F0x2.svd   # STM32F0 (M0)
make -C examples/nucleo-f446 && .venv/bin/python -m gdbtrace --config configs/nucleo-f446re.yaml  # STM32F446 (M4), ETMv3
.venv/bin/python -m gdbtrace fw.elf --remote 127.0.0.1:2331      # J-Link GDB Server, pyOCD...
.venv/bin/python -m gdbtrace fw.elf --remote 127.0.0.1:3333 --tcl 127.0.0.1:6666  # OpenOCD lancé à part
.venv/bin/python -m gdbtrace examples/firmware/demo.elf --svd examples/svd/mps2-an385.svd  # périphériques
make -C examples/freertos && .venv/bin/python -m gdbtrace examples/freertos/freertos.elf  # FreeRTOS
.venv/bin/python -m gdbtrace fw.elf --openocd configs/nucleo-f429zi.cfg --rtos  # tâches = threads gdb
.venv/bin/python -m gdbtrace fw.elf --openocd configs/nucleo-f429zi.cfg --swo-config --cpu-hz 180000000  # ITM/SWO
.venv/bin/python -m gdbtrace fw.elf --swo 127.0.0.1:3443          # SWO servi par Orbuculum / J-Link
.venv/bin/python -m gdbtrace fw.elf --swo-file capture.bin        # rejouer une capture SWO
.venv/bin/python -m gdbtrace examples/firmware/demo.elf --etm-sim  # trace ETM simulée (QEMU)
.venv/bin/python -m gdbtrace fw.elf --openocd … --etm-config --stm32 --trace 127.0.0.1:PORT  # ETM réel
.venv/bin/python -m gdbtrace fw.elf --openocd … --etm-config --etm-port swo --cpu-hz 4000000  # ETM par SWO
make -C examples/m0plus && .venv/bin/python -m gdbtrace examples/m0plus/demo.elf --machine microbit --mtb-sim  # M0+ + MTB
.venv/bin/python -m gdbtrace fw.elf --openocd … --mtb --mtb-probe  # MTB d'un vrai Cortex-M0+
make -C examples/tz-an505 && .venv/bin/python -m gdbtrace examples/tz-an505/secure.elf --machine mps2-an505 \
    --qemu-load examples/tz-an505/nonsecure.elf --add-symbols examples/tz-an505/nonsecure.elf  # TrustZone
make -C examples/multicore-an521 && .venv/bin/python -m gdbtrace examples/multicore-an521/multicore.elf \
    --machine mps2-an521 --core cpu0 --core cpu1=2                  # deux Cortex-M33
make -C examples/native && .venv/bin/python -m gdbtrace examples/native/pthreads --args 100000  # programme natif
.venv/bin/python -m gdbtrace --config configs/nucleo-g070rb.yaml         # une session décrite en YAML
```
