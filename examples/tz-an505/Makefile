# Démo TrustZone pour QEMU mps2-an505 (Cortex-M33) : deux images.
#   secure.elf     monde sécurisé (-kernel) : MPC, SAU, entrée NSC secure_add, démarrage NS
#   nonsecure.elf  monde non sécurisé (--qemu-load), lié à la bibliothèque d'import secure_nsc.o
# Lancer : python -m gdbtrace examples/tz-an505/secure.elf --machine mps2-an505 \
#            --qemu-load examples/tz-an505/nonsecure.elf --add-symbols examples/tz-an505/nonsecure.elf
CC     = arm-none-eabi-gcc
FLAGS  = -mcpu=cortex-m33 -mthumb -mfloat-abi=soft -g3 -O0 -ffreestanding -Wall
LDFLAGS = -nostdlib

all: secure.elf nonsecure.elf

secure.elf secure_nsc.o: secure.c secure.ld
	$(CC) $(FLAGS) -mcmse $(LDFLAGS) -T secure.ld secure.c -o secure.elf \
	    -Wl,--section-start=.gnu.sgstubs=0x10100000 \
	    -Wl,--cmse-implib,--out-implib=secure_nsc.o -lgcc

nonsecure.elf: nonsecure.c nonsecure.ld secure_nsc.o
	$(CC) $(FLAGS) $(LDFLAGS) -T nonsecure.ld nonsecure.c secure_nsc.o -o nonsecure.elf -lgcc

clean:
	rm -f secure.elf nonsecure.elf secure_nsc.o
