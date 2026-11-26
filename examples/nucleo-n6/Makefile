# Firmware de test gdbtrace pour Nucleo-N657X0-Q : la démo FreeRTOS de ../freertos (main.c,
# noyau), avec la configuration, le démarrage et le port ARM_CM55_NTZ de cette carte, plus du
# code ARMv8.1-M (lob.c : boucles à faible coût, Helium) pour la trace.
CC      = arm-none-eabi-gcc
K       = ../freertos/FreeRTOS-Kernel
CFLAGS  = -mcpu=cortex-m55 -mthumb -mfloat-abi=hard -g3 -O0 -ffreestanding -Wall \
          -I. -IFreeRTOS-port -I$(K)/include
# même choix de bibliothèque C que ../freertos (newlib-nano, sinon picolibc)
LIBC    = $(if $(filter /%,$(shell $(CC) -print-file-name=nano.specs)), \
            --specs=nano.specs --specs=nosys.specs, --specs=picolibc.specs)
LDFLAGS = -nostartfiles $(LIBC) -T link.ld -Wl,--gc-sections -Wl,--no-warn-rwx-segments

SRCS = startup.c etr_buffer.c lob.c ../freertos/main.c ../common/gdbtrace_rtt.c \
       $(K)/tasks.c $(K)/queue.c $(K)/list.c $(K)/timers.c \
       FreeRTOS-port/port.c FreeRTOS-port/portasm.c $(K)/portable/MemMang/heap_4.c

nucleo-n6.elf: $(SRCS) FreeRTOSConfig.h link.ld
	$(CC) $(CFLAGS) $(LDFLAGS) $(SRCS) -o $@ -lc -lgcc

clean:
	rm -f nucleo-n6.elf
