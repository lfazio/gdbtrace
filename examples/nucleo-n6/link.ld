/* STM32N657 : image entière en AXISRAM2, à l'adresse d'un FSBL (0x34180400 : le premier Ko
 * est l'en-tête de la ROM de démarrage), chargée par le débogueur. Le tampon de l'ETR (trace
 * ETM écrite en RAM par le TMC) est réservé en AXISRAM1, que ce firmware n'utilise pas. */
MEMORY
{
  RAM    (rwx) : ORIGIN = 0x34180400, LENGTH = 511K
  ETRBUF (rw)  : ORIGIN = 0x34010000, LENGTH = 256K
}
ENTRY(Reset_Handler)
_estack = ORIGIN(RAM) + LENGTH(RAM);
SECTIONS
{
  .isr_vector : { KEEP(*(.isr_vector)) } > RAM
  .text : { *(.text*) *(.rodata*) . = ALIGN(4); } > RAM
  .ARM.exidx : { *(.ARM.exidx* .gnu.linkonce.armexidx.*) } > RAM
  .data : { *(.data*) . = ALIGN(4); } > RAM
  .bss  : { _sbss = .; *(.bss*) *(COMMON) . = ALIGN(4); _ebss = .; } > RAM
  .etr (NOLOAD) : { KEEP(*(.etr)) } > ETRBUF
}
