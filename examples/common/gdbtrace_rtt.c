/* Bloc de contrôle RTT minimal : voir gdbtrace_rtt.h.
 *
 * Disposition (celle de SEGGER RTT, que lisent les débogueurs) :
 *   char acID[16]                 "SEGGER RTT", écrit en dernier (le débogueur le cherche)
 *   int  MaxNumUpBuffers, MaxNumDownBuffers
 *   tampons montants puis descendants : { sName, pBuffer, SizeOfBuffer, WrOff, RdOff, Flags }
 * Canal montant : le programme écrit à WrOff, le débogueur lit de RdOff à WrOff et avance
 * RdOff. Canal descendant : l'inverse. */
#include <stdint.h>

#include "gdbtrace_rtt.h"

#define UP_SIZE   1024u
#define DOWN_SIZE 16u

struct rtt_buffer {
    const char *name;
    char *buffer;
    unsigned size;
    volatile unsigned wr;
    volatile unsigned rd;
    unsigned flags;
};

struct rtt_cb {
    char id[16];
    int max_up;
    int max_down;
    struct rtt_buffer up[1];
    struct rtt_buffer down[1];
};

static char up_buffer[UP_SIZE];
static char down_buffer[DOWN_SIZE];
struct rtt_cb _SEGGER_RTT;

static void rtt_init(void)
{
    static const char id[] = "SEGGER RTT";
    struct rtt_cb *cb = &_SEGGER_RTT;
    cb->max_up = 1;
    cb->max_down = 1;
    cb->up[0] = (struct rtt_buffer){ "Terminal", up_buffer, UP_SIZE, 0, 0, 0 };
    cb->down[0] = (struct rtt_buffer){ "Terminal", down_buffer, DOWN_SIZE, 0, 0, 0 };
    __asm volatile ("dmb" ::: "memory");
    for (unsigned i = 0; i < sizeof id; i++) {
        cb->id[i] = id[i];               /* identifiant en dernier : bloc complet quand il paraît */
    }
}

unsigned rtt_write(const char *data, unsigned len)
{
    struct rtt_buffer *b = &_SEGGER_RTT.up[0];
    if (_SEGGER_RTT.id[0] != 'S') {
        rtt_init();
    }
    unsigned wr = b->wr, rd = b->rd, n = 0;
    while (n < len) {
        unsigned next = (wr + 1u) % b->size;
        if (next == rd) {
            break;                       /* plein : la suite est perdue */
        }
        b->buffer[wr] = data[n++];
        wr = next;
    }
    __asm volatile ("dmb" ::: "memory");
    b->wr = wr;
    return n;
}

unsigned rtt_puts(const char *s)
{
    unsigned len = 0;
    while (s[len]) {
        len++;
    }
    return rtt_write(s, len);
}

int rtt_getc(void)
{
    struct rtt_buffer *b = &_SEGGER_RTT.down[0];
    if (_SEGGER_RTT.id[0] != 'S') {
        rtt_init();
    }
    unsigned rd = b->rd;
    if (rd == b->wr) {
        return -1;
    }
    int c = (unsigned char)b->buffer[rd];
    __asm volatile ("dmb" ::: "memory");
    b->rd = (rd + 1u) % b->size;
    return c;
}
