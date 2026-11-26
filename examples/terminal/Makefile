# Démo de la vue Terminal (semihosting, RTT) pour QEMU mps2-an385 (Cortex-M3).
CC      = arm-none-eabi-gcc
CFLAGS  = -mcpu=cortex-m3 -mthumb -g3 -O0 -ffreestanding -Wall
LDFLAGS = -nostdlib -T ../firmware/link.ld -Wl,--gc-sections
SRC     = ../firmware/startup.c main.c ../common/gdbtrace_rtt.c

terminal.elf: $(SRC) ../common/gdbtrace_rtt.h ../firmware/link.ld
	$(CC) $(CFLAGS) $(LDFLAGS) $(SRC) -o $@ -lgcc

clean:
	rm -f terminal.elf
