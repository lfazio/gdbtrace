/* Chargeur externe de test au format de STM32CubeProgrammer (`.stldr`) : la même flash
 * émulée que ram_flash_flm.c (0x00300000, 1 Mo), en 16 secteurs de 64 Ko. Lié à son adresse
 * en RAM (0x20100000) ; Init, Write, SectorErase, MassErase, Read rendent 1 en cas de réussite.
 */
#include <stdint.h>
#include "Dev_Inf.h"

#define DEV_BASE 0x00300000u
#define DEV_SIZE 0x00100000u
#define SECTOR   0x10000u
#define READY    0x52454459u             /* « REDY » */

const struct StorageInfo StorageInfo __attribute__((used, section(".Dev_info"))) = {
    "GdbTrace RAM-flash (stldr, QEMU mps2-an385)", NOR_FLASH, DEV_BASE, DEV_SIZE, 256, 0xFF,
    { { 16, SECTOR }, { 0, 0 } }
};

static volatile uint32_t ready;          /* .bss : remise à zéro par le programmateur */

int Init(void)
{
    ready = READY;
    return 1;
}

int Write(uint32_t Address, uint32_t Size, uint8_t *buffer)
{
    if (ready != READY)
        return 0;
    volatile uint8_t *dst = (volatile uint8_t *)Address;
    for (uint32_t i = 0; i < Size; i++)
        dst[i] &= buffer[i];
    return 1;
}

int SectorErase(uint32_t EraseStartAddress, uint32_t EraseEndAddress)
{
    if (ready != READY)
        return 0;
    uint32_t a = EraseStartAddress & ~(SECTOR - 1u);
    while (a <= EraseEndAddress) {
        volatile uint32_t *p = (volatile uint32_t *)a;
        for (uint32_t i = 0; i < SECTOR / 4; i++)
            p[i] = 0xFFFFFFFFu;
        a += SECTOR;
    }
    return 1;
}

int MassErase(void)
{
    return SectorErase(DEV_BASE, DEV_BASE + DEV_SIZE - 1u);
}

int Read(uint32_t Address, uint32_t Size, uint8_t *buffer)
{
    volatile uint8_t *src = (volatile uint8_t *)Address;
    for (uint32_t i = 0; i < Size; i++)
        buffer[i] = src[i];
    return 1;
}
