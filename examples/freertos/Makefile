CC      = arm-none-eabi-gcc
K       = FreeRTOS-Kernel
CFLAGS  = -mcpu=cortex-m3 -mthumb -g3 -O0 -ffreestanding -Wall \
          -I. -I$(K)/include -I$(K)/portable/GCC/ARM_CM3
# Bibliothèque C (memset, memcpy) : newlib-nano si la chaîne l'a, sinon picolibc (Debian
# forky n'a plus libnewlib-arm-none-eabi ; picolibc-arm-none-eabi la remplace).
# -print-file-name ne renvoie un chemin absolu que si le fichier existe.
LIBC    = $(if $(filter /%,$(shell $(CC) -print-file-name=nano.specs)), \
            --specs=nano.specs --specs=nosys.specs, --specs=picolibc.specs)
LDFLAGS = -nostartfiles $(LIBC) -T ../firmware/link.ld -Wl,--gc-sections

SRCS = startup.c main.c \
       $(K)/tasks.c $(K)/queue.c $(K)/list.c $(K)/timers.c \
       $(K)/portable/GCC/ARM_CM3/port.c $(K)/portable/MemMang/heap_4.c

freertos.elf: $(SRCS) FreeRTOSConfig.h ../firmware/link.ld
	$(CC) $(CFLAGS) $(LDFLAGS) $(SRCS) -o $@ -lc -lgcc

clean:
	rm -f freertos.elf
