/* mps2-an521 : code en SSRAM1 sécurisée (0x10000000), données en SSRAM2 sécurisée ; une pile
   par cœur en haut de la RAM. */
MEMORY
{
    FLASH (rx)  : ORIGIN = 0x10000000, LENGTH = 1M
    RAM   (rwx) : ORIGIN = 0x38000000, LENGTH = 1M
}
_estack  = ORIGIN(RAM) + LENGTH(RAM);           /* cœur 0 */
_estack1 = ORIGIN(RAM) + LENGTH(RAM) - 0x4000;  /* cœur 1 */
ENTRY(Reset_Handler)
SECTIONS
{
    .isr_vector : { KEEP(*(.isr_vector)) } > FLASH
    .text : { *(.text*) *(.rodata*) . = ALIGN(4); _etext = .; } > FLASH
    .data : AT(_etext) { _sdata = .; *(.data*) . = ALIGN(4); _edata = .; } > RAM
    .bss : { _sbss = .; *(.bss*) *(COMMON) . = ALIGN(4); _ebss = .; } > RAM
}
