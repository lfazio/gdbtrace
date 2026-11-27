# Algorithmes de flash de test (chargeur de flash générique de GdbTrace) : une flash émulée
# dans la SSRAM1 de QEMU mps2-an385 (0x00300000, 1 Mo).
#   ram-flash.FLM    algorithme CMSIS (FLM), code indépendant de sa position, données par r9
#   ram-flash.stldr  chargeur externe au format de STM32CubeProgrammer, lié en RAM
#   GdbTrace.QEMU_DFP.1.0.0.pack  CMSIS-Pack de démonstration (puce GDBTRACE-AN385 :
#                    SVD de la carte, ram-flash.FLM)
CC     = arm-none-eabi-gcc
FLAGS  = -mcpu=cortex-m3 -mthumb -g -O1 -ffreestanding -fno-tree-loop-distribute-patterns -Wall
LDFLAGS = -nostdlib -nostartfiles

PACK   = GdbTrace.QEMU_DFP.1.0.0.pack

all: ram-flash.FLM ram-flash.stldr $(PACK)

ram-flash.FLM: ram_flash_flm.c FlashOS.h flm.ld
	$(CC) $(FLAGS) -ffixed-r9 $(LDFLAGS) -T flm.ld ram_flash_flm.c -o $@

ram-flash.stldr: ram_flash_stldr.c Dev_Inf.h stldr.ld
	$(CC) $(FLAGS) $(LDFLAGS) -T stldr.ld ram_flash_stldr.c -o $@

$(PACK): GdbTrace.QEMU_DFP.pdsc ram-flash.FLM ../svd/mps2-an385.svd
	rm -rf pack.tmp && mkdir -p pack.tmp/Flash pack.tmp/SVD
	cp GdbTrace.QEMU_DFP.pdsc pack.tmp/ && cp ram-flash.FLM pack.tmp/Flash/ && cp ../svd/mps2-an385.svd pack.tmp/SVD/
	cd pack.tmp && python3 -m zipfile -c ../$@ GdbTrace.QEMU_DFP.pdsc Flash SVD
	rm -rf pack.tmp

clean:
	rm -f ram-flash.FLM ram-flash.stldr $(PACK)
