/* Description d'un chargeur externe de STM32CubeProgrammer (`StorageInfo`, section
 * .Dev_info) : disposition des chargeurs `.stldr` de ST (alignement naturel, 120 octets avant
 * la table des secteurs). */
#ifndef DEV_INF_H
#define DEV_INF_H

#define MCU_FLASH 1
#define NOR_FLASH 3
#define SECTOR_NUM 10

struct DeviceSectors {
    unsigned long SectorNum;
    unsigned long SectorSize;
};

struct StorageInfo {
    char DeviceName[100];
    unsigned short DeviceType;
    unsigned long DeviceStartAddress;
    unsigned long DeviceSize;
    unsigned long PageSize;
    unsigned char EraseValue;
    struct DeviceSectors sectors[SECTOR_NUM];
};

#endif
