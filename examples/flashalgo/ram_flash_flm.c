/* Algorithme de flash FLM de test : « programme » une flash émulée dans la SSRAM1 de QEMU
 * mps2-an385 (0x00300000, 1 Mo, secteurs de 4 Ko, pages de 1 Ko), avec les règles d'une
 * flash : l'effacement met des 0xFF, la programmation ne peut que baisser des bits (et).
 *
 * Comme un FLM compilé RWPI, ses données sont lues par r9 (base statique posée par le
 * programmateur) : Init vérifie la valeur initiale de `state` (données bien copiées, r9 juste).
 * Deux adresses servent aux tests : effacer le secteur STUCK ne rend jamais la main, programmer
 * la page FAULTING provoque une faute de bus.
 */
#include <stdint.h>
#include "FlashOS.h"

struct algo_state { uint32_t magic; uint32_t fnc; uint32_t calls; };
register struct algo_state *st asm("r9");

#define DEV_BASE  0x00300000u
#define DEV_SIZE  0x00100000u
#define SECTOR    0x1000u
#define PAGE      0x400u
#define MAGIC     0x464C4D21u            /* « FLM! » */
#define STUCK     (DEV_BASE + 0xF0000u)
#define FAULTING  (DEV_BASE + 0xE0000u)

/* premier objet de PrgData : à l'adresse de la base statique */
struct algo_state state __attribute__((used, section(".prgdata_state"))) = { MAGIC, 0, 0 };

const struct FlashDevice FlashDevice __attribute__((used, section("DevDscr"))) = {
    FLASH_DRV_VERS, "GdbTrace RAM-flash (QEMU mps2-an385)", ONCHIP, DEV_BASE, DEV_SIZE, PAGE, 0,
    0xFF, 100, 3000,
    { { SECTOR, 0x000000 }, { SECTOR_END } }
};

int Init(unsigned long adr, unsigned long clk, unsigned long fnc)
{
    (void)clk;
    if (st->magic != MAGIC || adr != DEV_BASE)
        return 1;
    st->fnc = fnc;
    st->calls++;
    return 0;
}

int UnInit(unsigned long fnc)
{
    if (st->fnc != fnc)
        return 1;
    st->fnc = 0;
    return 0;
}

static void fill(uint32_t adr, uint32_t n)
{
    volatile uint32_t *p = (volatile uint32_t *)adr;
    for (uint32_t i = 0; i < n / 4; i++)
        p[i] = 0xFFFFFFFFu;
}

int EraseChip(void)
{
    fill(DEV_BASE, DEV_SIZE);
    return 0;
}

int EraseSector(unsigned long adr)
{
    if (st->fnc != 1)
        return 2;
    if (adr == STUCK)
        for (;;) {}
    fill(adr & ~(SECTOR - 1u), SECTOR);
    return 0;
}

int ProgramPage(unsigned long adr, unsigned long sz, unsigned char *buf)
{
    if (st->fnc != 2)
        return 2;
    if (adr == FAULTING)
        *(volatile uint32_t *)0x30000000u = 0;   /* aucune mémoire : faute de bus */
    volatile uint8_t *dst = (volatile uint8_t *)adr;
    for (uint32_t i = 0; i < sz; i++)
        dst[i] &= buf[i];
    return 0;
}

unsigned long Verify(unsigned long adr, unsigned long sz, unsigned char *buf)
{
    volatile uint8_t *mem = (volatile uint8_t *)adr;
    for (uint32_t i = 0; i < sz; i++)
        if (mem[i] != buf[i])
            return adr + i;
    return adr + sz;
}

int BlankCheck(unsigned long adr, unsigned long sz, unsigned char pat)
{
    volatile uint8_t *mem = (volatile uint8_t *)adr;
    for (uint32_t i = 0; i < sz; i++)
        if (mem[i] != pat)
            return 1;
    return 0;
}
