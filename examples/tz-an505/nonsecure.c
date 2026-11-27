/* Démo TrustZone, monde non sécurisé : appelle le monde sécurisé par son veneer à chaque tour.
 * `ns_fault_kind` (écrit par le débogueur, après l'arrivée dans ns_main) à 1 : appel d'une
 * adresse sécurisée qui n'est pas une entrée NSC → SecureFault (INVEP). */
#include <stdint.h>

extern uint32_t _estack, _etext, _sdata, _edata, _sbss, _ebss;
int secure_add(int a, int b);            /* veneer de la bibliothèque d'import */

volatile uint32_t ns_loops;
volatile int ns_result;
volatile uint32_t ns_fault_kind;

static void __attribute__((noinline)) ns_step(void)
{
    ns_loops++;
    ns_result = secure_add((int)ns_loops, 1);
}

int ns_main(void)
{
    for (;;) {
        ns_step();
        if (ns_fault_kind == 1) {
            ns_fault_kind = 0;
            ((void (*)(void))(0x10000001u))();   /* code sécurisé, pas une entrée : INVEP */
        }
    }
}

void NS_Default_Handler(void) { for (;;) {} }
void NS_HardFault_Handler(void) { for (;;) {} }

void NS_Reset_Handler(void)
{
    uint32_t *src = &_etext, *dst = &_sdata;
    while (dst < &_edata) *dst++ = *src++;
    for (dst = &_sbss; dst < &_ebss; ) *dst++ = 0;
    ns_main();
    for (;;) {}
}

__attribute__((section(".isr_vector"), used))
const void *ns_vector_table[16] = {
    &_estack, NS_Reset_Handler, NS_Default_Handler, NS_HardFault_Handler,
    NS_Default_Handler, NS_Default_Handler, NS_Default_Handler, 0, 0, 0, 0,
    NS_Default_Handler, NS_Default_Handler, 0, NS_Default_Handler, NS_Default_Handler,
};
