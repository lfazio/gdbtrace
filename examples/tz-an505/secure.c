/* Démo TrustZone, monde sécurisé (QEMU mps2-an505, Cortex-M33).
 *
 * Au reset (sécurisé) : les MPC des SSRAM rendent non sécurisées la moitié haute de la SSRAM1
 * (code NS, 0x00200000) et de la SSRAM2 (RAM NS, 0x28100000) ; le SAU déclare ces deux zones
 * NS et la zone des veneers (0x10100000) NSC, que NSCCFG ouvre côté IDAU ; VTOR_NS et MSP_NS sont pris dans la table des
 * vecteurs NS, puis le monde non sécurisé démarre (BLXNS).
 *
 * Le monde NS appelle `secure_add` par son veneer (fonction d'entrée NSC). Il peut aussi
 * provoquer une SecureFault : `ns_fault_kind` (image NS) à 1 appelle une adresse sécurisée qui
 * n'est pas une entrée (INVEP, SFAR).
 */
#include <arm_cmse.h>
#include <stdint.h>

extern uint32_t _estack, _etext, _sdata, _edata, _sbss, _ebss;

#define MPC_SSRAM1   0x58007000u
#define MPC_SSRAM2   0x58008000u
#define MPC_BLK_IDX  0x18u
#define MPC_BLK_LUT  0x1Cu
#define SAU_CTRL   (*(volatile uint32_t *)0xE000EDD0u)
#define SAU_RNR    (*(volatile uint32_t *)0xE000EDD8u)
#define SAU_RBAR   (*(volatile uint32_t *)0xE000EDDCu)
#define SAU_RLAR   (*(volatile uint32_t *)0xE000EDE0u)
#define SCB_SHCSR  (*(volatile uint32_t *)0xE000ED24u)
#define SCB_NS_VTOR (*(volatile uint32_t *)0xE002ED08u)
/* contrôleur de sécurité de l'SSE-200 : NSCCFG.CODENSC rend la zone 0x1xxxxxxx capable de NSC
   pour l'IDAU ; sans lui, le SAU seul n'y fait pas une zone NSC (SecureFault INVEP) */
#define SPCTRL_NSCCFG (*(volatile uint32_t *)0x50080014u)

#define NS_CODE   0x00200000u
#define NS_RAM    0x28100000u
#define NSC_BASE  0x10100000u

volatile uint32_t secure_calls;          /* appels reçus du monde NS */
volatile uint32_t secure_total;

typedef void (*ns_entry_t)(void) __attribute__((cmse_nonsecure_call));

/* Fonction d'entrée : le monde NS l'appelle par son veneer (SG) dans la zone NSC. */
int __attribute__((cmse_nonsecure_entry)) secure_add(int a, int b)
{
    secure_calls++;
    secure_total += (uint32_t)(a + b);
    return a + b;
}

static void mpc_ns(uint32_t mpc, uint32_t first_word, uint32_t words)
{
    for (uint32_t w = first_word; w < first_word + words; w++) {
        *(volatile uint32_t *)(mpc + MPC_BLK_IDX) = w;
        *(volatile uint32_t *)(mpc + MPC_BLK_LUT) = 0xFFFFFFFFu;   /* 32 blocs de 1 Ko : NS */
    }
}

static void sau_region(uint32_t n, uint32_t base, uint32_t limit, int nsc)
{
    SAU_RNR = n;
    SAU_RBAR = base & ~0x1Fu;
    SAU_RLAR = (limit & ~0x1Fu) | (nsc ? 2u : 0u) | 1u;
}

static void start_nonsecure(void)
{
    const uint32_t *vectors = (const uint32_t *)NS_CODE;
    mpc_ns(MPC_SSRAM1, 64, 64);              /* 0x200000-0x3FFFFF de la SSRAM1 */
    mpc_ns(MPC_SSRAM2, 32, 32);              /* 0x100000-0x1FFFFF de la SSRAM2 */
    sau_region(0, NS_CODE, NS_CODE + 0x1FFFFFu, 0);
    sau_region(1, NS_RAM, NS_RAM + 0xFFFFFu, 0);
    sau_region(2, NSC_BASE, NSC_BASE + 0x3FFu, 1);
    SPCTRL_NSCCFG = 1u;                      /* CODENSC */
    SAU_CTRL = 1u;                           /* ENABLE ; le reste : sécurisé */
    SCB_SHCSR |= 1u << 19;                   /* SECUREFAULTENA */
    __asm volatile ("dsb\n\tisb" ::: "memory");
    SCB_NS_VTOR = NS_CODE;
    __asm volatile ("msr msp_ns, %0" :: "r" (vectors[0]));
    ns_entry_t reset_ns = (ns_entry_t)cmse_nsfptr_create(vectors[1]);
    reset_ns();
}

int main(void)
{
    start_nonsecure();
    for (;;) {}
}

void Default_Handler(void) { for (;;) {} }
void HardFault_Handler(void) { for (;;) {} }
void MemManage_Handler(void) { for (;;) {} }
void BusFault_Handler(void) { for (;;) {} }
void UsageFault_Handler(void) { for (;;) {} }
void SecureFault_Handler(void) { for (;;) {} }

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
    MemManage_Handler, BusFault_Handler, UsageFault_Handler, SecureFault_Handler, 0, 0, 0,
    Default_Handler, Default_Handler, 0, Default_Handler, Default_Handler,
};
