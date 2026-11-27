# Algorithmes de flash de test (chargeur de flash générique de GdbTrace) : une flash émulée
# dans la SSRAM1 de QEMU mps2-an385 (0x00300000, 1 Mo).
#   ram-flash.FLM    algorithme CMSIS (FLM), code indépendant de sa position, données par r9
#   ram-flash.stldr  chargeur externe au format de STM32CubeProgrammer, lié en RAM
CC     = arm-none-eabi-gcc
FLAGS  = -mcpu=cortex-m3 -mthumb -g -O1 -ffreestanding -fno-tree-loop-distribute-patterns -Wall
LDFLAGS = -nostdlib -nostartfiles

all: ram-flash.FLM ram-flash.stldr

ram-flash.FLM: ram_flash_flm.c FlashOS.h flm.ld
	$(CC) $(FLAGS) -ffixed-r9 $(LDFLAGS) -T flm.ld ram_flash_flm.c -o $@

ram-flash.stldr: ram_flash_stldr.c Dev_Inf.h stldr.ld
	$(CC) $(FLAGS) $(LDFLAGS) -T stldr.ld ram_flash_stldr.c -o $@

clean:
	rm -f ram-flash.FLM ram-flash.stldr
