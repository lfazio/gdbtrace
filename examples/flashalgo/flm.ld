/* FLM : code indépendant de sa position à partir de 0 (PrgCode), données juste après
   (PrgData, lues par r9), description à part (DevDscr, lue par le programmateur, jamais
   chargée). */
SECTIONS
{
  PrgCode 0 : { *(.text*) *(.rodata*) . = ALIGN(4); }
  PrgData : { *(.prgdata_state) *(.data*) *(.bss*) *(COMMON) . = ALIGN(4); }
  DevDscr 0x10000 : { KEEP(*(DevDscr)) }
  /DISCARD/ : { *(.ARM.exidx*) *(.ARM.attributes) *(.comment) }
}
