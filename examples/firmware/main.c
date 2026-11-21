#include <stdint.h>

struct sensor {
    int id;
    int32_t value;
    uint8_t status;
};

enum mode { MODE_IDLE, MODE_RUN, MODE_FAULT = 5 };

volatile uint32_t counter;
struct sensor sensors[4] = { {1, 0, 0}, {2, 0, 0}, {3, 0, 0}, {4, 0, 0} };
const char banner[] = "gdbtrace demo";

/* Variables modifiées en continu, pour les vues « live » */
volatile uint32_t ticks;                 /* incrémenté par SysTick (1 kHz) */
volatile enum mode mode = MODE_IDLE;
volatile char last_char = 'A';
volatile float temperature = 21.5f;
volatile int16_t offset = -3;

/* Périphériques (Cortex-M + CMSDK de la carte MPS2 AN385) */
#define SYST_CSR    (*(volatile uint32_t *)0xE000E010)
#define SYST_RVR    (*(volatile uint32_t *)0xE000E014)
#define SYST_CVR    (*(volatile uint32_t *)0xE000E018)
#define TIMER0_CTRL (*(volatile uint32_t *)0x40000000)
#define TIMER0_RELOAD (*(volatile uint32_t *)0x40000008)

void peripherals_init(void);
void animate(uint32_t tick);

uint32_t fib(uint32_t n)
{
    uint32_t a = 0, b = 1;
    for (uint32_t i = 0; i < n; i++) {
        uint32_t t = a + b;
        a = b;
        b = t;
    }
    return a;
}

static void update_sensor(struct sensor *s, uint32_t tick)
{
    s->value = (int32_t)fib(tick % 20);
    s->status = (uint8_t)(tick & 0xFF);
}

static void delay(volatile uint32_t n)
{
    while (n--) {}
}

int main(void)
{
    peripherals_init();
    for (;;) {
        counter++;
        update_sensor(&sensors[counter % 4], counter);
        animate(counter);
        delay(1000);
    }
}

/* Définies après main pour ne pas décaler les adresses de fib/main utilisées par les tests. */
void peripherals_init(void)
{
    SYST_RVR = 25000 - 1;        /* 25 MHz / 25000 = 1 kHz */
    SYST_CVR = 0;
    SYST_CSR = 7;                /* ENABLE | TICKINT | CLKSOURCE=processeur */
    TIMER0_RELOAD = 0x00FFFFFF;
    TIMER0_CTRL = 1;             /* ENABLE */
}

void animate(uint32_t tick)
{
    mode = (tick & 0x100) ? MODE_RUN : MODE_IDLE;
    last_char = (char)('A' + tick % 26);
    temperature = 20.0f + (float)(tick % 50) / 4.0f;
    offset = (int16_t)(-(int32_t)(tick % 7));
}

void SysTick_Handler(void)
{
    ticks++;
}
