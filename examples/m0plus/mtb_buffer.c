/* Tampon de la MTB (Micro Trace Buffer) : GdbTrace le trouve par ce symbole (`--mtb`),
   le programme et le lit à chaque arrêt. Taille : puissance de 2 d'au moins 16 octets,
   alignement sur la taille (exigence de la MTB). 1 Ko = les 128 derniers branchements. */
__attribute__((section(".mtb"), aligned(1024), used))
unsigned char __mtb_buffer__[1024];
