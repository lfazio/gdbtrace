/* Démarrage du firmware de test gdbtrace sur Nucleo-G070RB (Cortex-M0+) : table des vecteurs
 * système (aucune interruption de périphérique n'est utilisée), .data copiée, .bss mise à zéro.
 * Horloge : HSI16 du reset (16 MHz), sans configuration. Gestionnaires du port FreeRTOS
 * ARM_CM0 (V11.3) : SVC_Handler, PendSV_Handler, SysTick_Handler. */
#include <stdint.h>

extern uint32_t _estack, _etext, _sdata, _edata, _sbss, _ebss;
int main(void);

void SVC_Handler(void);
void PendSV_Handler(void);
void SysTick_Handler(void);

void Default_Handler(void) { for (;;) {} }
void HardFault_Handler(void) { for (;;) {} }

void Reset_Handler(void)
{
    uint32_t *src = &_etext, *dst = &_sdata;
    while (dst < &_edata) *dst++ = *src++;
    for (dst = &_sbss; dst < &_ebss; ) *dst++ = 0;
    main();
    for (;;) {}
}

__attribute__((section(".isr_vector"), used))
const void *vector_table[16] = {
    &_estack, Reset_Handler, Default_Handler, HardFault_Handler,
    0, 0, 0, 0, 0, 0, 0,
    SVC_Handler, 0, 0, PendSV_Handler, SysTick_Handler,
};
