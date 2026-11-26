# Démo Cortex-M0+ (ARMv6-M) : mêmes sources que ../firmware, pour QEMU `-M microbit`
# (Cortex-M0, flash 256 Ko @0x0, RAM 16 Ko @0x20000000), avec un tampon MTB réservé.
# freertos.elf : la démo FreeRTOS (port ARM_CM0) de ../nucleo-g070 sur la même machine, pour
# valider sans carte la vue RTOS d'un M0 (cadre de pile du port, contexte des tâches).
CC      = arm-none-eabi-gcc
CFLAGS  = -mcpu=cortex-m0plus -mthumb -g3 -O0 -ffreestanding -Wall
LDFLAGS = -nostdlib -T link.ld -Wl,--gc-sections
SRC     = ../firmware/startup.c ../firmware/main.c mtb_buffer.c

K       = ../freertos/FreeRTOS-Kernel
P       = $(K)/portable/GCC/ARM_CM0
# 16 Ko de RAM : tas réduit (configTOTAL_HEAP_SIZE de ../nucleo-g070/FreeRTOSConfig.h)
RTOS_CFLAGS = $(CFLAGS) -I../nucleo-g070 -I$(P) -I$(K)/include -DconfigTOTAL_HEAP_SIZE=9216
LIBC    = $(if $(filter /%,$(shell $(CC) -print-file-name=nano.specs)), \
            --specs=nano.specs --specs=nosys.specs, --specs=picolibc.specs)
RTOS_SRC = ../nucleo-g070/startup.c ../freertos/main.c ../common/gdbtrace_rtt.c \
           $(K)/tasks.c $(K)/queue.c $(K)/list.c $(K)/timers.c \
           $(P)/port.c $(P)/portasm.c $(K)/portable/MemMang/heap_4.c

all: demo.elf freertos.elf

demo.elf: $(SRC) link.ld
	$(CC) $(CFLAGS) $(LDFLAGS) $(SRC) -o $@ -lgcc

freertos.elf: $(RTOS_SRC) ../nucleo-g070/FreeRTOSConfig.h link.ld
	$(CC) $(RTOS_CFLAGS) -nostartfiles $(LIBC) -T link.ld -Wl,--gc-sections $(RTOS_SRC) -o $@ -lc -lgcc

clean:
	rm -f demo.elf freertos.elf
