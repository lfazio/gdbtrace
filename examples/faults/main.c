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
 *   7 écriture dans la garde de la MPU       M3/M55 : MemManage (DACCVIOL, MMFAR)
 * (*) l'ARMv6-M n'a pas d'instruction de division : la bibliothèque rend 0.
 *
 * Sur un Cortex-M3, les fautes configurables ont leur propre gestionnaire (SHCSR), et la
 * division par zéro et l'accès non aligné sont piégés (CCR.DIV_0_TRP, CCR.UNALIGN_TRP).
 *
 * MPU (vue Système, onglet MPU), si le cœur en a une : code en lecture seule exécutable, RAM
 * sans exécution, une garde de 32 octets sans aucun accès, périphériques en Device ; le reste
 * suit la carte par défaut (PRIVDEFENA). ARMv7-M (QEMU mps2-an385) : RBAR/RASR ; ARMv8-M
 * (faults-m55.elf, QEMU mps3-an547) : RBAR/RLAR et MAIR0.
 */
#include <stdint.h>

volatile uint32_t fault_kind;           /* écrit par le débogueur */
volatile uint32_t loops;
volatile uint32_t sink;

#define SCB_CCR    (*(volatile uint32_t *)0xE000ED14u)
#define SCB_SHCSR  (*(volatile uint32_t *)0xE000ED24u)
#define MPU_TYPE   (*(volatile uint32_t *)0xE000ED90u)
#define MPU_CTRL   (*(volatile uint32_t *)0xE000ED94u)
#define MPU_RNR    (*(volatile uint32_t *)0xE000ED98u)
#define MPU_RBAR   (*(volatile uint32_t *)0xE000ED9Cu)
#define MPU_RASR   (*(volatile uint32_t *)0xE000EDA0u)   /* ARMv7-M */
#define MPU_RLAR   (*(volatile uint32_t *)0xE000EDA0u)   /* ARMv8-M, même adresse */
#define MPU_MAIR0  (*(volatile uint32_t *)0xE000EDC0u)
#if defined(__ARM_ARCH_8M_MAIN__) || defined(__ARM_ARCH_8_1M_MAIN__)
#define BAD_ADDRESS 0x08000000u         /* aucune mémoire à cette adresse (mps3-an547) */
#define CODE_BASE   0x10000000u         /* ITCM, alias sécurisé */
#define RAM_BASE    0x30000000u         /* DTCM, alias sécurisé */
#else
#define BAD_ADDRESS 0x30000000u         /* aucune mémoire à cette adresse (mps2, nRF51) */
#define CODE_BASE   0x00000000u
#define RAM_BASE    0x20000000u
#endif

/* garde de la MPU : 32 octets, alignés sur leur taille, que rien ne doit toucher */
volatile uint8_t mpu_guard[32] __attribute__((aligned(32)));

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
    case 7: mpu_guard[4] = 1; break;
    default: break;
    }
}

static void mpu_setup(void)
{
#if !defined(__ARM_ARCH_6M__)
    if (((MPU_TYPE >> 8) & 0xFF) < 4)
        return;                          /* pas de MPU (ou trop peu de régions) */
#if defined(__ARM_ARCH_8M_MAIN__) || defined(__ARM_ARCH_8_1M_MAIN__)
    /* MAIR0 : attribut 0 = Normal write-back RA/WA, attribut 1 = Device-nGnRE.
       RBAR : BASE | SH[4:3] | AP[2:1] | XN ; RLAR : LIMIT | AttrIndx[3:1] | EN.
       AP : 0 lecture-écriture privilégiée, 1 lecture-écriture, 2 lecture seule privilégiée,
       3 lecture seule. Le code tourne privilégié : la garde est en lecture seule. */
    MPU_MAIR0 = 0xFFu | (0x04u << 8);
    MPU_RNR = 0; MPU_RBAR = CODE_BASE | (3u << 1);                       /* code : lecture seule */
    MPU_RLAR = (CODE_BASE + 0x7FFE0u) | 1u;                               /* 512 Ko, attribut 0 */
    MPU_RNR = 1; MPU_RBAR = RAM_BASE | (1u << 1) | 1u;                    /* RAM : RW, XN */
    MPU_RLAR = (RAM_BASE + 0x7FFE0u) | 1u;
    MPU_RNR = 2; MPU_RBAR = (uint32_t)mpu_guard | (2u << 1) | 1u;         /* garde : lecture seule */
    MPU_RLAR = (uint32_t)mpu_guard | 1u;                                  /* 32 octets */
    MPU_RNR = 3; MPU_RBAR = 0x40000000u | (1u << 1) | 1u;                 /* périphériques, XN */
    MPU_RLAR = 0x4FFFFFE0u | (1u << 1) | 1u;                              /* attribut 1 : Device */
#else
    /* RASR : XN[28] | AP[26:24] | TEX[21:19] S[18] C[17] B[16] | SRD[15:8] | SIZE[5:1] | EN */
    MPU_RNR = 0; MPU_RBAR = CODE_BASE;
    MPU_RASR = (6u << 24) | (1u << 17) | (21u << 1) | 1u;                   /* 4 Mo, RO, WT */
    MPU_RNR = 1; MPU_RBAR = RAM_BASE;
    MPU_RASR = (1u << 28) | (3u << 24) | (1u << 19) | (3u << 16) | (21u << 1) | 1u;   /* RW, XN, WB */
    MPU_RNR = 2; MPU_RBAR = (uint32_t)mpu_guard;
    MPU_RASR = (1u << 28) | (0u << 24) | (4u << 1) | 1u;                    /* 32 o, aucun accès */
    MPU_RNR = 3; MPU_RBAR = 0x40000000u;
    MPU_RASR = (1u << 28) | (3u << 24) | (1u << 18) | (1u << 16) | (28u << 1) | 1u;  /* Device */
#endif
    MPU_CTRL = (1u << 2) | 1u;           /* PRIVDEFENA | ENABLE */
    __asm volatile ("dsb\n\tisb" ::: "memory");
#endif
}

int main(void)
{
#if !defined(__ARM_ARCH_6M__)
    SCB_SHCSR |= (1u << 16) | (1u << 17) | (1u << 18);   /* MemManage, BusFault, UsageFault */
    SCB_CCR |= (1u << 4) | (1u << 3);                    /* DIV_0_TRP, UNALIGN_TRP */
#endif
    mpu_setup();
    for (;;) {
        loops++;
        uint32_t kind = fault_kind;
        if (kind) {
            fault_kind = 0;
            trigger(kind);
        }
    }
}
