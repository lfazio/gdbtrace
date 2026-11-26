# Démo des fautes : faults-m3.elf (QEMU mps2-an385, Cortex-M3) et faults-m0.elf (QEMU
# microbit, Cortex-M0), mêmes sources.
CC      = arm-none-eabi-gcc
FLAGS   = -mthumb -g3 -O0 -ffreestanding -Wall
LDFLAGS = -nostdlib -Wl,--gc-sections
SRC     = startup.c main.c

all: faults-m3.elf faults-m0.elf

faults-m3.elf: $(SRC) ../firmware/link.ld
	$(CC) -mcpu=cortex-m3 $(FLAGS) $(LDFLAGS) -T ../firmware/link.ld $(SRC) -o $@ -lgcc

faults-m0.elf: $(SRC) ../m0plus/link.ld
	$(CC) -mcpu=cortex-m0 $(FLAGS) $(LDFLAGS) -T ../m0plus/link.ld $(SRC) -o $@ -lgcc

clean:
	rm -f faults-m3.elf faults-m0.elf
