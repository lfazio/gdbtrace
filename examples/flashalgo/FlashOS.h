/* Interface d'un algorithme de flash CMSIS (FLM) : la description `FlashDevice` (section
 * DevDscr) et les fonctions qu'appelle le programmateur. Disposition de la structure : celle
 * des FLM des CMSIS-Packs (alignement naturel, 160 octets avant la table des secteurs). */
#ifndef FLASHOS_H
#define FLASHOS_H

#define FLASH_DRV_VERS (0x0100 + 1)
#define UNKNOWN 0
#define ONCHIP 1
#define SECTOR_NUM 512
#define SECTOR_END 0xFFFFFFFF, 0xFFFFFFFF

struct FlashSectors {
    unsigned long szSector;      /* taille des secteurs de ce bloc */
    unsigned long AddrSector;    /* début du bloc, depuis DevAdr */
};

struct FlashDevice {
    unsigned short Vers;
    char DevName[128];
    unsigned short DevType;
    unsigned long DevAdr;
    unsigned long szDev;
    unsigned long szPage;
    unsigned long Res;
    unsigned char valEmpty;
    unsigned long toProg;        /* ms */
    unsigned long toErase;       /* ms */
    struct FlashSectors sectors[SECTOR_NUM];
};

int Init(unsigned long adr, unsigned long clk, unsigned long fnc);
int UnInit(unsigned long fnc);
int EraseChip(void);
int EraseSector(unsigned long adr);
int ProgramPage(unsigned long adr, unsigned long sz, unsigned char *buf);
unsigned long Verify(unsigned long adr, unsigned long sz, unsigned char *buf);
int BlankCheck(unsigned long adr, unsigned long sz, unsigned char pat);

#endif
