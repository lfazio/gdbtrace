#include <stdint.h>

extern uint32_t _estack, _etext, _sdata, _edata, _sbss, _ebss;
int main(void);

/* Handlers du port FreeRTOS ARM_CM3 */
void vPortSVCHandler(void);
void xPortPendSVHandler(void);
void xPortSysTickHandler(void);

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
    Default_Handler, Default_Handler, Default_Handler, 0, 0, 0, 0,
    vPortSVCHandler, Default_Handler, 0, xPortPendSVHandler, xPortSysTickHandler,
};
