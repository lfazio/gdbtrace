/* QEMU mps3-an547 (Cortex-M55, ARMv8.1-M) en état sécurisé : ITCM 512 Ko @0x10000000 (alias
   sécurisé), DTCM 512 Ko @0x30000000 (alias sécurisé). */
MEMORY
{
  FLASH (rx)  : ORIGIN = 0x10000000, LENGTH = 512K
  RAM   (rwx) : ORIGIN = 0x30000000, LENGTH = 512K
}
ENTRY(Reset_Handler)
_estack = ORIGIN(RAM) + LENGTH(RAM);
SECTIONS
{
  .isr_vector : { KEEP(*(.isr_vector)) } > FLASH
  .text : { *(.text*) *(.rodata*) . = ALIGN(4); } > FLASH
  _etext = ALIGN(4);
  .data : AT(_etext) { _sdata = .; *(.data*) . = ALIGN(4); _edata = .; } > RAM
  .bss  : { _sbss = .; *(.bss*) *(COMMON) . = ALIGN(4); _ebss = .; } > RAM
}
