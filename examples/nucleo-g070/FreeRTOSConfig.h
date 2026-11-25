/* Configuration FreeRTOS du firmware de test gdbtrace sur Nucleo-G070RB (Cortex-M0+), port
 * ARM_CM0 (V11.3). Même démo que sous QEMU (../freertos/main.c). Sert aussi à la Nucleo-F072RB
 * (Cortex-M0, ../nucleo-f072 : HSI 8 MHz, 16 Ko de RAM) et à la version QEMU microbit
 * (Cortex-M0, ../m0plus), qui réduisent le tas et fixent l'horloge (Makefile).
 * Horloge HSI16 du reset (16 MHz) ; le nRF51 de QEMU microbit tourne aussi à 16 MHz. */
#ifndef FREERTOS_CONFIG_H
#define FREERTOS_CONFIG_H

#include <stdint.h>

#ifndef configCPU_CLOCK_HZ
#define configCPU_CLOCK_HZ                      16000000UL     /* HSI16 */
#endif
#define configTICK_RATE_HZ                      1000
#define configUSE_PREEMPTION                    1
#define configUSE_TIME_SLICING                  1
#define configMAX_PRIORITIES                    5
#define configMINIMAL_STACK_SIZE                128      /* en mots */
#define configMAX_TASK_NAME_LEN                 12
#define configTICK_TYPE_WIDTH_IN_BITS           TICK_TYPE_WIDTH_32_BITS
#define configIDLE_SHOULD_YIELD                 1

/* Port ARM_CM0 : ni MPU ni TrustZone. La vérification des gestionnaires par VTOR est coupée :
 * le Cortex-M0 (QEMU microbit) n'a pas de VTOR. */
#define configENABLE_MPU                        0
#define configCHECK_HANDLER_INSTALLATION        0

/* Mémoire : 36 Ko de RAM sur la G070 (16 Ko sur la F072 et le microbit : tas réduit) */
#define configSUPPORT_DYNAMIC_ALLOCATION        1
#define configSUPPORT_STATIC_ALLOCATION         0
#ifndef configTOTAL_HEAP_SIZE
#define configTOTAL_HEAP_SIZE                   ( 16 * 1024 )
#endif

/* Ce qui intéresse un débogueur « OS aware » */
#define configUSE_TRACE_FACILITY                1    /* uxTCBNumber, ucQueueType */
#define configRECORD_STACK_HIGH_ADDRESS         1    /* pxEndOfStack : taille de pile connue */
#define configCHECK_FOR_STACK_OVERFLOW          2    /* piles remplies de 0xA5 */
#define configQUEUE_REGISTRY_SIZE               8    /* noms des queues/sémaphores/mutex */
#define configGENERATE_RUN_TIME_STATS           1    /* ulRunTimeCounter par tâche */

/* Compteur de temps d'exécution : le DWT d'un ARMv6-M n'a pas de CYCCNT. Le compte des ticks
 * (1 ms) suffit au % CPU de la vue RTOS, sans temporisateur propre à la puce. */
#define portCONFIGURE_TIMER_FOR_RUN_TIME_STATS()  do { } while( 0 )
#define portGET_RUN_TIME_COUNTER_VALUE()          xTaskGetTickCount()

/* Fonctionnalités */
#define configUSE_MUTEXES                       1
#define configUSE_RECURSIVE_MUTEXES             1
#define configUSE_COUNTING_SEMAPHORES           1
#define configUSE_TASK_NOTIFICATIONS            1
#define configUSE_IDLE_HOOK                     1
#define configUSE_TICK_HOOK                     0
#define configUSE_MALLOC_FAILED_HOOK            1
#define configUSE_TIMERS                        1
#define configTIMER_TASK_PRIORITY               4
#define configTIMER_QUEUE_LENGTH                8
#define configTIMER_TASK_STACK_DEPTH            256

#define INCLUDE_vTaskDelay                      1
#define INCLUDE_vTaskDelayUntil                 1
#define INCLUDE_vTaskSuspend                    1
#define INCLUDE_vTaskDelete                     1
#define INCLUDE_uxTaskPriorityGet               1
#define INCLUDE_uxTaskGetStackHighWaterMark     1
#define INCLUDE_xTaskGetCurrentTaskHandle       1
#define INCLUDE_xTaskGetIdleTaskHandle          1

#define configASSERT( x )    if( ( x ) == 0 ) { taskDISABLE_INTERRUPTS(); for( ; ; ) { } }

/* GdbTrace : temps par tâche par l'ITM. Le M0+ n'a pas d'ITM : main.c rend cette sortie
 * inerte en ARMv6-M ; le crochet reste, pour garder la même démo sur tous les cœurs. */
#define GDBTRACE_TASK_PORT                      31
void gdbtrace_task_switched_in(void *tcb);
#define traceTASK_SWITCHED_IN()                 gdbtrace_task_switched_in((void *)pxCurrentTCB)

#endif /* FREERTOS_CONFIG_H */
