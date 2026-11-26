# Firmware de test gdbtrace pour Nucleo-G070RB (STM32G070RB, Cortex-M0+) : la démo FreeRTOS de
# ../freertos (main.c, noyau, port ARM_CM0), avec le démarrage, l'édition de liens et la
# configuration de cette carte. Le M0+ n'a ni ITM ni ETM : la sortie ITM de main.c est inerte.
CC      = arm-none-eabi-gcc
K       = ../freertos/FreeRTOS-Kernel
P       = $(K)/portable/GCC/ARM_CM0
CFLAGS  = -mcpu=cortex-m0plus -mthumb -g3 -O0 -ffreestanding -Wall \
          -I. -I$(P) -I$(K)/include
# même choix de bibliothèque C que ../freertos (newlib-nano, sinon picolibc)
LIBC    = $(if $(filter /%,$(shell $(CC) -print-file-name=nano.specs)), \
            --specs=nano.specs --specs=nosys.specs, --specs=picolibc.specs)
LDFLAGS = -nostartfiles $(LIBC) -T link.ld -Wl,--gc-sections

SRCS = startup.c ../freertos/main.c ../common/gdbtrace_rtt.c \
       $(K)/tasks.c $(K)/queue.c $(K)/list.c $(K)/timers.c \
       $(P)/port.c $(P)/portasm.c $(K)/portable/MemMang/heap_4.c

nucleo-g070.elf: $(SRCS) FreeRTOSConfig.h link.ld
	$(CC) $(CFLAGS) $(LDFLAGS) $(SRCS) -o $@ -lc -lgcc

clean:
	rm -f nucleo-g070.elf
