/* Démo des fautes Cortex-M, pour la vue Faute de GdbTrace (QEMU mps2-an385 : Cortex-M3 ;
 * QEMU microbit : Cortex-M0).
 *
 * Le débogueur écrit `fault_kind` ; la boucle principale déclenche la faute au tour suivant :
 *   1 lecture à une adresse sans mémoire     M3 : BusFault (PRECISERR, BFAR)  M0 : HardFault
 *   2 division par zéro                      M3 : UsageFault (DIVBYZERO)      M0 : rien (*)
 *   3 accès 32 bits non aligné               M3 : UsageFault (UNALIGNED)      M0 : HardFault
 *   4 instruction indéfinie (UDF)            M3 : UsageFault (UNDEFINSTR)     M0 : HardFault
 *   5 instruction indéfinie, UsageFault      M3 : HardFault (FORCED)
 *     désactivée : escalade
 *   6 saut vers une adresse paire (bit       M3 : UsageFault (INVSTATE)       M0 : HardFault
 *     Thumb à 0)
 * (*) l'ARMv6-M n'a pas d'instruction de division : la bibliothèque rend 0.
 *
 * Sur un Cortex-M3, les fautes configurables ont leur propre gestionnaire (SHCSR), et la
 * division par zéro et l'accès non aligné sont piégés (CCR.DIV_0_TRP, CCR.UNALIGN_TRP).
 */
#include <stdint.h>

volatile uint32_t fault_kind;           /* écrit par le débogueur */
volatile uint32_t loops;
volatile uint32_t sink;

#define SCB_CCR    (*(volatile uint32_t *)0xE000ED14u)
#define SCB_SHCSR  (*(volatile uint32_t *)0xE000ED24u)
#define BAD_ADDRESS 0x30000000u         /* aucune mémoire à cette adresse (mps2, nRF51) */

static uint32_t __attribute__((noinline)) read_at(uint32_t addr)
{
    return *(volatile uint32_t *)addr;
}

static int32_t __attribute__((noinline)) divide(volatile int32_t a, volatile int32_t b)
{
    return a / b;
}

static void __attribute__((noinline)) undefined_instruction(void)
{
    __asm volatile (".short 0xde00");   /* UDF #0 */
}

static void __attribute__((noinline)) jump_even(void)
{
    void (*f)(void) = (void (*)(void))0x00000100u;   /* bit 0 à 0 : état ARM, interdit */
    f();
}

static void __attribute__((noinline)) trigger(uint32_t kind)
{
    static uint8_t buf[8] __attribute__((aligned(4)));
    switch (kind) {
    case 1: sink = read_at(BAD_ADDRESS); break;
    case 2: sink = (uint32_t)divide(1, 0); break;
    case 3: sink = *(volatile uint32_t *)(buf + 1); break;
    case 4: undefined_instruction(); break;
    case 5:
#if !defined(__ARM_ARCH_6M__)
        SCB_SHCSR &= ~(1u << 18);       /* USGFAULTENA : la faute monte en HardFault */
#endif
        undefined_instruction();
        break;
    case 6: jump_even(); break;
    default: break;
    }
}

int main(void)
{
#if !defined(__ARM_ARCH_6M__)
    SCB_SHCSR |= (1u << 16) | (1u << 17) | (1u << 18);   /* MemManage, BusFault, UsageFault */
    SCB_CCR |= (1u << 4) | (1u << 3);                    /* DIV_0_TRP, UNALIGN_TRP */
#endif
    for (;;) {
        loops++;
        uint32_t kind = fault_kind;
        if (kind) {
            fault_kind = 0;
            trigger(kind);
        }
    }
}
