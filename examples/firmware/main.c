#include <stdint.h>

struct sensor {
    int id;
    int32_t value;
    uint8_t status;
};

volatile uint32_t counter;
struct sensor sensors[4] = { {1, 0, 0}, {2, 0, 0}, {3, 0, 0}, {4, 0, 0} };
const char banner[] = "gdbtrace demo";

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
    for (;;) {
        counter++;
        update_sensor(&sensors[counter % 4], counter);
        delay(1000);
    }
}
