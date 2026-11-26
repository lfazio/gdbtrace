/* Firmware de démo FreeRTOS pour gdbtrace (QEMU mps2-an385).
 *
 * Tâches aux états variés, pour la vue RTOS :
 *   producer (prio 2) : envoie dans cmd_queue puis attend 10 ms        -> Blocked (délai)
 *   consumer (prio 3) : attend cmd_queue                               -> Blocked (queue)
 *   worker   (prio 2) : prend data_mutex, calcule, attend 5 ms         -> Running / Blocked
 *   sleeper  (prio 1) : se suspend                                     -> Suspended
 *   hog      (prio 1) : utilise beaucoup de pile, attend done_sem      -> Blocked (sémaphore)
 *   + IDLE (prio 0) et Tmr Svc (prio 4), et un timer logiciel périodique.
 *
 * Chaque tâche range sa high-water mark (en mots) dans stack_hwm[] : référence pour les tests.
 */
#include <stdint.h>
#include <string.h>

#include "FreeRTOS.h"
#include "task.h"
#include "queue.h"
#include "semphr.h"
#include "timers.h"

#include "../common/gdbtrace_rtt.h"

enum { ID_PRODUCER, ID_CONSUMER, ID_WORKER, ID_SLEEPER, ID_HOG, ID_IDLE, N_IDS };

struct command {
    uint32_t seq;
    uint32_t arg;
};

QueueHandle_t cmd_queue;
SemaphoreHandle_t data_mutex;
SemaphoreHandle_t done_sem;
TimerHandle_t blink_timer;
TaskHandle_t task_handles[N_IDS];

volatile UBaseType_t stack_hwm[N_IDS];   /* uxTaskGetStackHighWaterMark(), en mots */
volatile uint32_t produced, consumed, worked, blinks;
volatile uint32_t shared_data;

static void record_hwm(int id)
{
    stack_hwm[id] = uxTaskGetStackHighWaterMark(NULL);
}

/* ---- Sortie ITM (printf sans UART, broche SWO), à la manière de CMSIS ITM_SendChar.
 * Inerte tant que le débogueur n'a pas activé l'ITM et le port (ITM_TCR.ITMENA, ITM_TER) :
 * c'est le cas sous QEMU, qui n'émule pas l'ITM (registres lus à 0). */
#define ITM_PORT8(n)   (*(volatile uint8_t *)(0xE0000000u + 4u * (n)))
#define ITM_PORT32(n)  (*(volatile uint32_t *)(0xE0000000u + 4u * (n)))
#define ITM_TER        (*(volatile uint32_t *)0xE0000E00u)
#define ITM_TCR        (*(volatile uint32_t *)0xE0000E80u)

static int itm_enabled(unsigned port)
{
#if defined(__ARM_ARCH_6M__)
    /* Cortex-M0/M0+ (ARMv6-M) : pas d'ITM, et sa zone du PPB est réservée (un accès peut
     * provoquer une HardFault) : la sortie ITM reste muette sans y toucher. */
    (void)port;
    return 0;
#else
    return (ITM_TCR & 1u) && (ITM_TER & (1u << port));
#endif
}

static void itm_putc(unsigned port, char c)
{
    while (ITM_PORT32(port) == 0u) {}      /* FIFO du port prêt */
    ITM_PORT8(port) = (uint8_t)c;
}

void itm_puts(unsigned port, const char *s)
{
    if (!itm_enabled(port)) {
        return;
    }
    while (*s) {
        itm_putc(port, *s++);
    }
}

void itm_u32(unsigned port, uint32_t value)
{
    if (!itm_enabled(port)) {
        return;
    }
    while (ITM_PORT32(port) == 0u) {}
    ITM_PORT32(port) = value;
}

/* ---- Temps par tâche (GdbTrace, vue Temps des tâches) : à chaque changement de tâche, le
 * noyau appelle traceTASK_SWITCHED_IN() (FreeRTOSConfig.h) avec le compte de ses ticks.
 * - l'adresse du TCB qui prend le processeur part sur le port ITM GDBTRACE_TASK_PORT ; avec
 *   l'horodatage ITM, GdbTrace en tire le temps de chaque tâche ;
 * - sans ITM (Cortex-M0/M0+) ou sans capture SWO : l'horodatage (cycles, d'après SysTick et
 *   le compte des ticks) et le TCB sont aussi écrits dans un anneau en RAM,
 *   `gdbtrace_task_log`, que GdbTrace lit en marche par le canal mémoire.
 * Dans les deux cas, GdbTrace relit le nom de la tâche dans son TCB. */
#define GDBTRACE_TASK_LOG_SIZE   256u           /* entrées (puissance de 2) : 2 Ko */
#define GDBTRACE_TASK_LOG_MAGIC  0x47544C31u    /* « GTL1 » */

struct gdbtrace_task_entry {
    uint32_t ts;                    /* cycles du cœur (32 bits, GdbTrace déroule les tours) */
    uint32_t tcb;
};

struct gdbtrace_task_log {
    uint32_t magic;                 /* écrit au premier changement de tâche */
    uint32_t size;                  /* entrées de l'anneau */
    volatile uint32_t head;         /* entrées écrites depuis le démarrage */
    struct gdbtrace_task_entry e[GDBTRACE_TASK_LOG_SIZE];
};

struct gdbtrace_task_log gdbtrace_task_log;

#define GT_SYST_RVR   (*(volatile uint32_t *)0xE000E014u)
#define GT_SYST_CVR   (*(volatile uint32_t *)0xE000E018u)
#define GT_SCB_ICSR   (*(volatile uint32_t *)0xE000ED04u)
#define GT_PENDSTSET  (1u << 26)

/* Cycles depuis le démarrage de l'ordonnanceur : ticks × période de SysTick + ce que le
 * compteur (décroissant) a déjà fait du tick en cours. Appelée interruptions masquées
 * (changement de contexte) : un tick en attente est compté. */
static uint32_t gdbtrace_cycles(uint32_t ticks)
{
    uint32_t period = (GT_SYST_RVR & 0xFFFFFFu) + 1u;
    uint32_t pending = GT_SCB_ICSR & GT_PENDSTSET;
    uint32_t cvr = GT_SYST_CVR & 0xFFFFFFu;
    if (!pending && (GT_SCB_ICSR & GT_PENDSTSET)) {      /* repassé par zéro entre-temps */
        pending = 1u;
        cvr = GT_SYST_CVR & 0xFFFFFFu;
    }
    if (pending) {
        ticks++;
    }
    return ticks * period + (period - 1u - cvr);
}

void gdbtrace_task_switched_in(void *tcb, uint32_t ticks)
{
    struct gdbtrace_task_log *log = &gdbtrace_task_log;
    uint32_t head = log->head;

    itm_u32(GDBTRACE_TASK_PORT, (uint32_t)tcb);
    if (log->magic != GDBTRACE_TASK_LOG_MAGIC) {
        log->size = GDBTRACE_TASK_LOG_SIZE;
        head = 0u;
        log->magic = GDBTRACE_TASK_LOG_MAGIC;
    }
    log->e[head & (GDBTRACE_TASK_LOG_SIZE - 1u)].ts = gdbtrace_cycles(ticks);
    log->e[head & (GDBTRACE_TASK_LOG_SIZE - 1u)].tcb = (uint32_t)tcb;
    __asm volatile ("dmb" ::: "memory");                 /* l'entrée avant l'indice */
    log->head = head + 1u;
}

static char *u32_to_dec(uint32_t v, char *end)
{
    *--end = '\0';
    do {
        *--end = (char)('0' + v % 10u);
        v /= 10u;
    } while (v);
    return end;
}

uint32_t worker_step(uint32_t n)
{
    uint32_t a = 0, b = 1;
    for (uint32_t i = 0; i < n % 24; i++) {
        uint32_t t = a + b;
        a = b;
        b = t;
    }
    shared_data = a;
    worked++;
    return a;
}

static void producer_task(void *arg)
{
    (void)arg;
    struct command cmd = { 0, 0 };
    for (;;) {
        cmd.seq++;
        cmd.arg = cmd.seq * 3;
        xQueueSend(cmd_queue, &cmd, portMAX_DELAY);
        produced++;
        record_hwm(ID_PRODUCER);
        vTaskDelay(pdMS_TO_TICKS(10));
    }
}

static void consumer_task(void *arg)
{
    (void)arg;
    struct command cmd;
    for (;;) {
        if (xQueueReceive(cmd_queue, &cmd, portMAX_DELAY) == pdPASS) {
            consumed += cmd.arg;
            if ((cmd.seq % 8) == 0) {
                char num[12];
                const char *n = u32_to_dec(cmd.seq, num + sizeof num);
                itm_puts(0, "consumer: commande ");
                itm_puts(0, n);
                itm_puts(0, "\n");
                rtt_puts("consumer: commande ");    /* aussi par le RTT (vue Terminal, --rtt) */
                rtt_puts(n);
                rtt_puts("\n");
                xSemaphoreGive(done_sem);
            }
        }
        record_hwm(ID_CONSUMER);
    }
}

static void worker_task(void *arg)
{
    (void)arg;
    uint32_t n = 0;
    for (;;) {
        xSemaphoreTake(data_mutex, portMAX_DELAY);
        itm_u32(1, worker_step(n++));      /* valeur brute sur le port ITM 1 */
        xSemaphoreGive(data_mutex);
        record_hwm(ID_WORKER);
        vTaskDelay(pdMS_TO_TICKS(5));
    }
}

static void sleeper_task(void *arg)
{
    (void)arg;
    record_hwm(ID_SLEEPER);
    for (;;) {
        vTaskSuspend(NULL);
    }
}

/* Utilise volontairement une grande partie de la pile de « hog ». */
static uint32_t __attribute__((noinline)) deep_work(uint32_t depth)
{
    volatile uint8_t scratch[96];
    memset((void *)scratch, (int)depth, sizeof scratch);
    if (depth == 0) {
        return scratch[0];
    }
    return scratch[depth % sizeof scratch] + deep_work(depth - 1);
}

static void hog_task(void *arg)
{
    (void)arg;
    for (;;) {
        deep_work(6);
        record_hwm(ID_HOG);
        xSemaphoreTake(done_sem, portMAX_DELAY);
    }
}

static void blink_cb(TimerHandle_t t)
{
    (void)t;
    blinks++;
}

void vApplicationIdleHook(void)
{
    record_hwm(ID_IDLE);
}

void vApplicationStackOverflowHook(TaskHandle_t task, char *name)
{
    (void)task;
    (void)name;
    taskDISABLE_INTERRUPTS();
    for (;;) {}
}

void vApplicationMallocFailedHook(void)
{
    taskDISABLE_INTERRUPTS();
    for (;;) {}
}

int main(void)
{
    cmd_queue = xQueueCreate(4, sizeof(struct command));
    data_mutex = xSemaphoreCreateMutex();
    done_sem = xSemaphoreCreateBinary();
    vQueueAddToRegistry(cmd_queue, "cmd_queue");
    vQueueAddToRegistry(data_mutex, "data_mutex");
    vQueueAddToRegistry(done_sem, "done_sem");

    xTaskCreate(producer_task, "producer", 256, NULL, 2, &task_handles[ID_PRODUCER]);
    xTaskCreate(consumer_task, "consumer", 256, NULL, 3, &task_handles[ID_CONSUMER]);
    xTaskCreate(worker_task, "worker", 256, NULL, 2, &task_handles[ID_WORKER]);
    xTaskCreate(sleeper_task, "sleeper", 160, NULL, 1, &task_handles[ID_SLEEPER]);
    xTaskCreate(hog_task, "hog", 256, NULL, 1, &task_handles[ID_HOG]);

    blink_timer = xTimerCreate("blink", pdMS_TO_TICKS(100), pdTRUE, NULL, blink_cb);
    xTimerStart(blink_timer, 0);

    vTaskStartScheduler();
    for (;;) {}
}
