/* Démarrage du firmware de test gdbtrace sur Nucleo-N657X0-Q (Cortex-M55).
 *
 * Démarrage de développement (JP2 en 2-3) : la ROM de démarrage ne charge rien ; le débogueur
 * remet le cœur à zéro (AIRCR), charge l'image en AXISRAM2 et pointe sp / pc sur la table des
 * vecteurs (GdbTrace : --program, comme scripts/n6-load.sh de l'émulateur). L'image entière est
 * en RAM : .data est chargée à sa place, rien à recopier ; seule .bss est mise à zéro.
 *
 * Ce que la ROM peut laisser derrière elle est remis en état avant toute pile : VTOR (sa table
 * à elle), MSPLIM / PSPLIM (une limite de pile au-dessus de la nôtre ferait une UsageFault au
 * premier empilement). FPU et Helium (MVE) : CP10 / CP11 en accès complet. */
#include <stdint.h>

extern uint32_t _estack, _sbss, _ebss;
extern const void *vector_table[];
int main(void);

void SVC_Handler(void);
void PendSV_Handler(void);
void SysTick_Handler(void);

void Default_Handler(void) { for (;;) {} }
void HardFault_Handler(void) { for (;;) {} }

#define SCB_VTOR  (*(volatile uint32_t *)0xE000ED08u)
#define SCB_CPACR (*(volatile uint32_t *)0xE000ED88u)

void reset_c(void)
{
    SCB_VTOR = (uint32_t)vector_table;
    SCB_CPACR |= 0xFu << 20;                 /* CP10, CP11 : FPU et MVE */
    __asm volatile ("dsb\n isb");
    for (uint32_t *dst = &_sbss; dst < &_ebss; ) *dst++ = 0;
    main();
    for (;;) {}
}

__attribute__((naked)) void Reset_Handler(void)
{
    __asm volatile (
        "movs r0, #0\n"
        "msr msplim, r0\n"
        "msr psplim, r0\n"
        "ldr r0, =_estack\n"
        "msr msp, r0\n"
        "isb\n"
        "b reset_c\n");
}

/* 16 vecteurs système + 194 interruptions (aucune n'est utilisée : Default_Handler). */
#define N_IRQ 194
__attribute__((section(".isr_vector"), used))
const void *vector_table[16 + N_IRQ] = {
    &_estack, Reset_Handler, Default_Handler, HardFault_Handler,
    Default_Handler, Default_Handler, Default_Handler, Default_Handler, 0, 0, 0,
    SVC_Handler, Default_Handler, 0, PendSV_Handler, SysTick_Handler,
    [16 ... 16 + N_IRQ - 1] = Default_Handler,
};
