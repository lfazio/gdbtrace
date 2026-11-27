# Démo à deux cœurs pour QEMU mps2-an521 (deux Cortex-M33), une image pour les deux.
# Lancer : python -m gdbtrace examples/multicore-an521/multicore.elf --machine mps2-an521 --cores
CC     = arm-none-eabi-gcc
FLAGS  = -mcpu=cortex-m33 -mthumb -mfloat-abi=soft -g3 -O0 -ffreestanding -Wall
LDFLAGS = -nostdlib

all: multicore.elf

multicore.elf: main.c link.ld
	$(CC) $(FLAGS) $(LDFLAGS) -T link.ld main.c -o $@ -lgcc

clean:
	rm -f multicore.elf
