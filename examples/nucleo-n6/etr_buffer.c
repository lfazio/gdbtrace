/* Tampon de l'ETR (Embedded Trace Router) : la trace ETM du Cortex-M55 n'a pas d'autre sortie
 * que le port parallèle (le SWO de la N6 ne porte que l'ITM). GdbTrace trouve ce tampon par son
 * symbole (`--etr`), y fait écrire le TMC en tampon circulaire et le lit à chaque arrêt. */
__attribute__((section(".etr"), aligned(4096), used))
unsigned char __etr_buffer__[256 * 1024];
