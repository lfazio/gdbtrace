#include <stdint.h>

extern uint32_t _estack, _etext, _sdata, _edata, _sbss, _ebss;
int main(void);

void Default_Handler(void) { for (;;) {} }
void HardFault_Handler(void) { for (;;) {} }
void SysTick_Handler(void) __attribute__((weak, alias("Default_Handler")));

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
    Default_Handler, Default_Handler, 0, Default_Handler, SysTick_Handler,
};
