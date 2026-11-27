/* Image sécurisée (QEMU mps2-an505, Cortex-M33) : code à l'alias sécurisé de la SSRAM1
   (0x10000000), veneers NSC à part (0x10100000, région NSC du SAU), RAM à l'alias sécurisé de
   la SSRAM2 (0x38000000). */
MEMORY
{
  FLASH (rx)  : ORIGIN = 0x10000000, LENGTH = 1M
  NSC   (rx)  : ORIGIN = 0x10100000, LENGTH = 1K
  RAM   (rwx) : ORIGIN = 0x38000000, LENGTH = 1M
}
ENTRY(Reset_Handler)
_estack = ORIGIN(RAM) + LENGTH(RAM);
SECTIONS
{
  .isr_vector : { KEEP(*(.isr_vector)) } > FLASH
  .text : { *(.text*) *(.rodata*) . = ALIGN(4); } > FLASH
  _etext = ALIGN(4);
  .gnu.sgstubs : { *(.gnu.sgstubs*) } > NSC
  .data : AT(_etext) { _sdata = .; *(.data*) . = ALIGN(4); _edata = .; } > RAM
  .bss  : { _sbss = .; *(.bss*) *(COMMON) . = ALIGN(4); _ebss = .; } > RAM
}
