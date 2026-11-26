/* Bloc de contrôle RTT minimal (format SEGGER RTT : identifiant « SEGGER RTT », un tampon
 * montant et un descendant), lu et écrit par le débogueur pendant que le programme tourne :
 * vue Terminal de GdbTrace (--rtt), ou tout outil qui lit le RTT. */
#ifndef GDBTRACE_RTT_H
#define GDBTRACE_RTT_H

#include <stddef.h>

/* Écrit `len` octets sur le canal montant 0 ; ce qui ne tient pas est perdu (le programme
 * n'attend jamais le débogueur). Rend le nombre d'octets écrits. */
unsigned rtt_write(const char *data, unsigned len);
unsigned rtt_puts(const char *s);
/* Un octet reçu sur le canal descendant 0, ou -1. */
int rtt_getc(void);

#endif
