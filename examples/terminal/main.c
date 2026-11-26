/* Démo de la vue Terminal de GdbTrace (QEMU mps2-an385, Cortex-M3) : du texte par le
 * semihosting et par le RTT, et l'écho de ce que le débogueur envoie par le RTT.
 *
 * Le semihosting (instruction BKPT 0xAB) arrête le cœur si personne ne le traite : le
 * débogueur écrit `semihosting_on` quand le serveur le gère (QEMU --semihosting). */
#include <stdint.h>

#include "../common/gdbtrace_rtt.h"

volatile uint32_t semihosting_on;       /* écrit par le débogueur */
volatile uint32_t loops, lines, echoed;

#define SYS_WRITE0 0x04

static void semihost(int op, const void *arg)
{
    register int r0 __asm("r0") = op;
    register const void *r1 __asm("r1") = arg;
    __asm volatile ("bkpt 0xab" : "+r"(r0) : "r"(r1) : "memory");
}

static char *u32_to_dec(uint32_t v, char *end)
{
    *--end = '\0';
    do {
        *--end = (char)('0' + v % 10u);
        v /= 10u;
    } while (v);
    return end;
}

static void say(uint32_t n)
{
    char line[32] = "ligne ";
    char num[12];
    char *d = u32_to_dec(n, num + sizeof num);
    unsigned k = 6;
    while (*d) {
        line[k++] = *d++;
    }
    line[k++] = '\n';
    line[k] = '\0';
    rtt_write(line, k);
    if (semihosting_on) {
        semihost(SYS_WRITE0, line);
    }
    lines++;
}

int main(void)
{
    for (uint32_t n = 0;; n++) {
        loops++;
        if (n % 20000u == 0u) {
            say(n / 20000u);
        }
        int c = rtt_getc();
        if (c >= 0) {
            char e[] = "écho : ?\n";
            e[sizeof e - 3] = (char)c;
            rtt_write(e, sizeof e - 1);
            echoed++;
        }
    }
}
