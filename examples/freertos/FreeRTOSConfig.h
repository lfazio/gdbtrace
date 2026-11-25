/* Configuration FreeRTOS du firmware de démo gdbtrace (QEMU mps2-an385, Cortex-M3). */
#ifndef FREERTOS_CONFIG_H
#define FREERTOS_CONFIG_H

#include <stdint.h>

#define configCPU_CLOCK_HZ                      25000000UL
#define configTICK_RATE_HZ                      1000
#define configUSE_PREEMPTION                    1
#define configUSE_TIME_SLICING                  1
#define configMAX_PRIORITIES                    5
#define configMINIMAL_STACK_SIZE                128      /* en mots */
#define configMAX_TASK_NAME_LEN                 12
#define configTICK_TYPE_WIDTH_IN_BITS           TICK_TYPE_WIDTH_32_BITS
#define configIDLE_SHOULD_YIELD                 1

/* Mémoire */
#define configSUPPORT_DYNAMIC_ALLOCATION        1
#define configSUPPORT_STATIC_ALLOCATION         0
#define configTOTAL_HEAP_SIZE                   ( 24 * 1024 )

/* Ce qui intéresse un débogueur « OS aware » */
#define configUSE_TRACE_FACILITY                1    /* uxTCBNumber, ucQueueType */
#define configRECORD_STACK_HIGH_ADDRESS         1    /* pxEndOfStack : taille de pile connue */
#define configCHECK_FOR_STACK_OVERFLOW          2    /* piles remplies de 0xA5 */
#define configQUEUE_REGISTRY_SIZE               8    /* noms des queues/sémaphores/mutex */
#define configGENERATE_RUN_TIME_STATS           1    /* ulRunTimeCounter par tâche */

/* Compteur de temps d'exécution : TIMER0 CMSDK (25 MHz, décompte) inversé */
#define RTS_TIMER0_CTRL     ( *( volatile uint32_t * ) 0x40000000 )
#define RTS_TIMER0_VALUE    ( *( volatile uint32_t * ) 0x40000004 )
#define RTS_TIMER0_RELOAD   ( *( volatile uint32_t * ) 0x40000008 )
#define portCONFIGURE_TIMER_FOR_RUN_TIME_STATS()  do { RTS_TIMER0_RELOAD = 0xFFFFFFFFUL; RTS_TIMER0_VALUE = 0xFFFFFFFFUL; RTS_TIMER0_CTRL = 1; } while( 0 )
#define portGET_RUN_TIME_COUNTER_VALUE()          ( 0xFFFFFFFFUL - RTS_TIMER0_VALUE )

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

/* Priorités NVIC. QEMU implémente 8 bits de priorité : le port exige alors un bit 0 nul.
 * 0xA0 convient avec 3, 4 ou 8 bits implémentés. */
#define configKERNEL_INTERRUPT_PRIORITY         255
#define configMAX_SYSCALL_INTERRUPT_PRIORITY    160

#define configASSERT( x )    if( ( x ) == 0 ) { taskDISABLE_INTERRUPTS(); for( ; ; ) { } }

/* GdbTrace : temps par tâche (vue Temps des tâches). À chaque changement de tâche, l'adresse
 * du TCB entrant est écrite sur le port ITM GDBTRACE_TASK_PORT (main.c) ; inerte tant que le
 * débogueur n'a pas activé l'ITM. Pour un autre firmware : ces trois lignes, et la fonction. */
#define GDBTRACE_TASK_PORT                      31
void gdbtrace_task_switched_in(void *tcb, uint32_t ticks);
/* développée dans tasks.c : xTickCount et xPendedTicks y sont visibles (horodatage du journal
 * en RAM, à partir de SysTick) */
#define traceTASK_SWITCHED_IN()                 gdbtrace_task_switched_in((void *)pxCurrentTCB, \
                                                    (uint32_t)(xTickCount + xPendedTicks))

#endif /* FREERTOS_CONFIG_H */
