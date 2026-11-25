/* Démarrage du firmware de test gdbtrace sur Nucleo-F746ZG : table des vecteurs système (aucune
 * interruption de périphérique n'est utilisée), FPU activée (port FreeRTOS ARM_CM7 r0p1,
 * flottants matériels), .data copiée, .bss mise à zéro. */
#include <stdint.h>

extern uint32_t _estack, _etext, _sdata, _edata, _sbss, _ebss;
int main(void);

void vPortSVCHandler(void);
void xPortPendSVHandler(void);
void xPortSysTickHandler(void);

void Default_Handler(void) { for (;;) {} }
void HardFault_Handler(void) { for (;;) {} }

#define SCB_CPACR (*(volatile uint32_t *)0xE000ED88u)

void Reset_Handler(void)
{
    SCB_CPACR |= 0xFu << 20;                 /* CP10, CP11 : accès complet à la FPU */
    __asm volatile ("dsb\n isb");
    uint32_t *src = &_etext, *dst = &_sdata;
    while (dst < &_edata) *dst++ = *src++;
    for (dst = &_sbss; dst < &_ebss; ) *dst++ = 0;
    main();
    for (;;) {}
}

__attribute__((section(".isr_vector"), used))
const void *vector_table[16] = {
    &_estack, Reset_Handler, Default_Handler, HardFault_Handler,
    Default_Handler, Default_Handler, Default_Handler, 0, 0, 0, 0,
    vPortSVCHandler, Default_Handler, 0, xPortPendSVHandler, xPortSysTickHandler,
};
