# Firmware de test gdbtrace pour Nucleo-F446RE : la démo FreeRTOS de ../freertos (main.c,
# noyau), avec la configuration et le démarrage de cette carte. Port FreeRTOS : celui de la
# F746 (ARM_CM7 r0p1), valable sur tout ARMv7E-M à FPU simple précision, dont le M4F.
CC      = arm-none-eabi-gcc
K       = ../freertos/FreeRTOS-Kernel
CFLAGS  = -mcpu=cortex-m4 -mthumb -mfpu=fpv4-sp-d16 -mfloat-abi=hard -g3 -O0 -ffreestanding -Wall \
          -I. -I../nucleo-f746/FreeRTOS-port -I$(K)/include
# même choix de bibliothèque C que ../freertos (newlib-nano, sinon picolibc)
LIBC    = $(if $(filter /%,$(shell $(CC) -print-file-name=nano.specs)), \
            --specs=nano.specs --specs=nosys.specs, --specs=picolibc.specs)
LDFLAGS = -nostartfiles $(LIBC) -T link.ld -Wl,--gc-sections

SRCS = startup.c ../freertos/main.c ../common/gdbtrace_rtt.c \
       $(K)/tasks.c $(K)/queue.c $(K)/list.c $(K)/timers.c \
       ../nucleo-f746/FreeRTOS-port/port.c $(K)/portable/MemMang/heap_4.c

nucleo-f446.elf: $(SRCS) FreeRTOSConfig.h link.ld
	$(CC) $(CFLAGS) $(LDFLAGS) $(SRCS) -o $@ -lc -lgcc

clean:
	rm -f nucleo-f446.elf
