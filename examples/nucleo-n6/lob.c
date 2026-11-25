/* Code ARMv8.1-M pour la trace : boucles à faible coût (WLS / DLS / LE) et Helium (MVE),
 * compilées en -O2 quel que soit le reste du firmware (-O0). Appelées à chaque tick de
 * FreeRTOS (vApplicationTickHook, dans l'interruption SysTick) : la trace ETM du M55 en
 * contient donc toujours, et le décodeur doit savoir que LE est un branchement. */
#include <stdint.h>

#define N_SAMPLES 64

volatile uint32_t lob_sum, lob_ticks;
int32_t lob_samples[N_SAMPLES];

/* Boucle à compteur : le compilateur en fait une boucle WLS / LE (ou DLS / LE). */
__attribute__((optimize("O2"), noinline)) uint32_t lob_count(const int32_t *v, uint32_t n)
{
    uint32_t acc = 0;
    for (uint32_t i = 0; i < n; i++) {
        acc += (uint32_t)v[i] ^ i;
    }
    return acc;
}

/* Boucle vectorisée : Helium avec prédication de queue (DLSTP / LETP). */
__attribute__((optimize("O3"), noinline)) void lob_scale(int32_t *v, uint32_t n, int32_t k)
{
    for (uint32_t i = 0; i < n; i++) {
        v[i] = v[i] * k + (int32_t)i;
    }
}

void vApplicationTickHook(void)
{
    uint32_t t = ++lob_ticks;
    lob_scale(lob_samples, 8u + (t & 31u), 3);
    lob_sum = lob_count(lob_samples, 1u + (t % N_SAMPLES));
}
