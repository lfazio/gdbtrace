/* .stldr : lié à son adresse en RAM (SSRAM2 de QEMU mps2-an385) ; .Dev_info à part, jamais
   chargé (lu dans le fichier par le programmateur). */
MEMORY
{
  RAM     (rwx) : ORIGIN = 0x20100000, LENGTH = 64K
  DEVINFO (r)   : ORIGIN = 0xF0000000, LENGTH = 1K
}
SECTIONS
{
  .text : { *(.text*) *(.rodata*) . = ALIGN(4); } > RAM
  .data : { *(.data*) . = ALIGN(4); } > RAM
  .bss  : { *(.bss*) *(COMMON) . = ALIGN(4); } > RAM
  .Dev_info : { KEEP(*(.Dev_info)) } > DEVINFO
  /DISCARD/ : { *(.ARM.exidx*) *(.comment) }
}
