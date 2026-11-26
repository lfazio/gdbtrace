# Firmware de test gdbtrace pour Nucleo-F072RB (STM32F072RB, Cortex-M0) : la démo FreeRTOS de
# ../freertos (main.c, noyau, port ARM_CM0), avec le démarrage et la configuration FreeRTOS de
# ../nucleo-g070 (vecteurs système seulement, identiques) et l'édition de liens de cette carte.
# Le M0 n'a ni ITM ni ETM : la sortie ITM de main.c est inerte.
# Horloge : HSI 8 MHz du reset, sans PLL. 16 Ko de RAM : tas de 9 Ko.
CC      = arm-none-eabi-gcc
K       = ../freertos/FreeRTOS-Kernel
P       = $(K)/portable/GCC/ARM_CM0
CFLAGS  = -mcpu=cortex-m0 -mthumb -g3 -O0 -ffreestanding -Wall \
          -I../nucleo-g070 -I$(P) -I$(K)/include \
          -DconfigCPU_CLOCK_HZ=8000000UL -DconfigTOTAL_HEAP_SIZE=9216
# même choix de bibliothèque C que ../freertos (newlib-nano, sinon picolibc)
LIBC    = $(if $(filter /%,$(shell $(CC) -print-file-name=nano.specs)), \
            --specs=nano.specs --specs=nosys.specs, --specs=picolibc.specs)
LDFLAGS = -nostartfiles $(LIBC) -T link.ld -Wl,--gc-sections

SRCS = ../nucleo-g070/startup.c ../freertos/main.c ../common/gdbtrace_rtt.c \
       $(K)/tasks.c $(K)/queue.c $(K)/list.c $(K)/timers.c \
       $(P)/port.c $(P)/portasm.c $(K)/portable/MemMang/heap_4.c

nucleo-f072.elf: $(SRCS) ../nucleo-g070/FreeRTOSConfig.h link.ld
	$(CC) $(CFLAGS) $(LDFLAGS) $(SRCS) -o $@ -lc -lgcc

clean:
	rm -f nucleo-f072.elf
