CC      = arm-none-eabi-gcc
CFLAGS  = -mcpu=cortex-m3 -mthumb -g3 -O0 -ffreestanding -Wall
LDFLAGS = -nostdlib -T link.ld -Wl,--gc-sections

demo.elf: startup.c main.c link.ld
	$(CC) $(CFLAGS) $(LDFLAGS) startup.c main.c -o $@ -lgcc

clean:
	rm -f demo.elf
