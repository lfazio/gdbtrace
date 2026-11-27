# Démo des fautes : faults-m3.elf (QEMU mps2-an385, Cortex-M3), faults-m0.elf (QEMU
# microbit, Cortex-M0) et faults-m55.elf (QEMU mps3-an547, Cortex-M55, MPU ARMv8-M), mêmes sources.
CC      = arm-none-eabi-gcc
FLAGS   = -mthumb -g3 -O0 -ffreestanding -Wall
LDFLAGS = -nostdlib -Wl,--gc-sections
SRC     = startup.c main.c

all: faults-m3.elf faults-m0.elf faults-m55.elf

faults-m3.elf: $(SRC) ../firmware/link.ld
	$(CC) -mcpu=cortex-m3 $(FLAGS) $(LDFLAGS) -T ../firmware/link.ld $(SRC) -o $@ -lgcc

faults-m0.elf: $(SRC) ../m0plus/link.ld
	$(CC) -mcpu=cortex-m0 $(FLAGS) $(LDFLAGS) -T ../m0plus/link.ld $(SRC) -o $@ -lgcc

faults-m55.elf: $(SRC) link-an547.ld
	$(CC) -mcpu=cortex-m55 -mfloat-abi=soft $(FLAGS) $(LDFLAGS) -T link-an547.ld $(SRC) -o $@ -lgcc

clean:
	rm -f faults-m3.elf faults-m0.elf faults-m55.elf
