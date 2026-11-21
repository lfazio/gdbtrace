CC      = arm-none-eabi-gcc
K       = FreeRTOS-Kernel
CFLAGS  = -mcpu=cortex-m3 -mthumb -g3 -O0 -ffreestanding -Wall \
          -I. -I$(K)/include -I$(K)/portable/GCC/ARM_CM3
LDFLAGS = -nostartfiles --specs=nano.specs --specs=nosys.specs -T ../firmware/link.ld -Wl,--gc-sections

SRCS = startup.c main.c \
       $(K)/tasks.c $(K)/queue.c $(K)/list.c $(K)/timers.c \
       $(K)/portable/GCC/ARM_CM3/port.c $(K)/portable/MemMang/heap_4.c

freertos.elf: $(SRCS) FreeRTOSConfig.h ../firmware/link.ld
	$(CC) $(CFLAGS) $(LDFLAGS) $(SRCS) -o $@ -lc -lgcc

clean:
	rm -f freertos.elf
