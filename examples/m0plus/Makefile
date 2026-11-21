# Démo Cortex-M0+ (ARMv6-M) : mêmes sources que ../firmware, pour QEMU `-M microbit`
# (Cortex-M0, flash 256 Ko @0x0, RAM 16 Ko @0x20000000), avec un tampon MTB réservé.
CC      = arm-none-eabi-gcc
CFLAGS  = -mcpu=cortex-m0plus -mthumb -g3 -O0 -ffreestanding -Wall
LDFLAGS = -nostdlib -T link.ld -Wl,--gc-sections
SRC     = ../firmware/startup.c ../firmware/main.c mtb_buffer.c

demo.elf: $(SRC) link.ld
	$(CC) $(CFLAGS) $(LDFLAGS) $(SRC) -o $@ -lgcc

clean:
	rm -f demo.elf
