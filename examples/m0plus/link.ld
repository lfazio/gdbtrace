/* QEMU microbit (nRF51, Cortex-M0) : flash 256 Ko @0x0, RAM 16 Ko @0x20000000.
   Le tampon de la MTB (`.mtb`) est placé en tête de RAM, aligné sur sa taille et non
   initialisé : la MTB y écrit ses paquets, le programme ne doit pas s'en servir. */
MEMORY
{
  FLASH (rx)  : ORIGIN = 0x00000000, LENGTH = 256K
  RAM   (rwx) : ORIGIN = 0x20000000, LENGTH = 16K
}
ENTRY(Reset_Handler)
_estack = ORIGIN(RAM) + LENGTH(RAM);
SECTIONS
{
  .isr_vector : { KEEP(*(.isr_vector)) } > FLASH
  .text : { *(.text*) *(.rodata*) . = ALIGN(4); } > FLASH
  .ARM.exidx : { *(.ARM.exidx* .gnu.linkonce.armexidx.*) } > FLASH
  _etext = ALIGN(4);
  .mtb (NOLOAD) : { KEEP(*(.mtb)) } > RAM
  .data : AT(_etext) { _sdata = .; *(.data*) . = ALIGN(4); _edata = .; } > RAM
  .bss  : { _sbss = .; *(.bss*) *(COMMON) . = ALIGN(4); _ebss = .; } > RAM
}
