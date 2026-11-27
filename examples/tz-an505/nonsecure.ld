/* Image non sécurisée : code dans la moitié haute de la SSRAM1 (alias non sécurisé 0x00200000),
   RAM dans la moitié haute de la SSRAM2 (0x28100000) ; ces blocs sont rendus non sécurisés par
   l'image sécurisée (MPC et SAU). */
MEMORY
{
  FLASH (rx)  : ORIGIN = 0x00200000, LENGTH = 1M
  RAM   (rwx) : ORIGIN = 0x28100000, LENGTH = 1M
}
ENTRY(NS_Reset_Handler)
_estack = ORIGIN(RAM) + LENGTH(RAM);
SECTIONS
{
  .isr_vector : { KEEP(*(.isr_vector)) } > FLASH
  .text : { *(.text*) *(.rodata*) . = ALIGN(4); } > FLASH
  _etext = ALIGN(4);
  .data : AT(_etext) { _sdata = .; *(.data*) . = ALIGN(4); _edata = .; } > RAM
  .bss  : { _sbss = .; *(.bss*) *(COMMON) . = ALIGN(4); _ebss = .; } > RAM
}
