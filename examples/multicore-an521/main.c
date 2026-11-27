/* Démo à deux cœurs pour QEMU mps2-an521 (SSE-200, deux Cortex-M33), une seule image.
 *
 * Au reset, le cœur 0 démarre seul ; le cœur 1 attend (CPUWAIT du contrôle système de
 * l'SSE-200). Les deux partent de la même table des vecteurs : Reset_Handler lit le numéro du
 * cœur (registre CPUID privé de chaque cœur, 0x4001F000), le cœur 0 initialise la RAM puis
 * libère le cœur 1 et tourne dans core0_main ; le cœur 1 prend sa propre pile et tourne dans
 * core1_main. Chacun incrémente son compteur ; le cœur 1 recopie celui du cœur 0 (mailbox).
 */
#include <stdint.h>

extern uint32_t _estack, _estack1, _etext, _sdata, _edata, _sbss, _ebss;

#define CPU_IDENTITY (*(volatile uint32_t *)0x4001F000u)   /* 0 ou 1, propre à chaque cœur */
#define SYSCTL_CPUWAIT (*(volatile uint32_t *)0x50021118u) /* bit 1 : cœur 1 retenu */

volatile uint32_t core0_count;
volatile uint32_t core1_count;
volatile uint32_t mailbox;              /* écrit par le cœur 0, lu par le cœur 1 */
volatile uint32_t core1_seen;
volatile uint32_t core1_started;

void __attribute__((noinline)) core0_step(void)
{
    core0_count++;
    mailbox = core0_count;
}

void __attribute__((noinline)) core1_step(void)
{
    core1_count++;
    core1_seen = mailbox;
}

void core0_main(void)
{
    SYSCTL_CPUWAIT = 0;                  /* libère le cœur 1 */
    for (;;)
        core0_step();
}

void core1_main(void)
{
    core1_started = 1;
    for (;;)
        core1_step();
}

void Default_Handler(void) { for (;;) {} }

void __attribute__((naked)) Reset_Handler(void)
{
    __asm volatile (
        "ldr r0, =0x4001F000\n\t"
        "ldr r0, [r0]\n\t"
        "cbz r0, 1f\n\t"
        "ldr r1, =_estack1\n\t"           /* cœur 1 : sa pile, puis core1_main */
        "mov sp, r1\n\t"
        "b core1_main\n"
        "1:\n\t"
        "b reset_core0\n");
}

void reset_core0(void)
{
    uint32_t *src = &_etext, *dst = &_sdata;
    while (dst < &_edata) *dst++ = *src++;
    for (dst = &_sbss; dst < &_ebss; ) *dst++ = 0;
    core0_main();
}

__attribute__((section(".isr_vector"), used))
const void *vector_table[16] = {
    &_estack, Reset_Handler, Default_Handler, Default_Handler,
    Default_Handler, Default_Handler, Default_Handler, Default_Handler, 0, 0, 0,
    Default_Handler, Default_Handler, 0, Default_Handler, Default_Handler,
};
