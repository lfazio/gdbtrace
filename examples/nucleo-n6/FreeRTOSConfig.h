/* Configuration FreeRTOS du firmware de test gdbtrace sur Nucleo-N657X0-Q (Cortex-M55, état sécurisé).
 * Même démo que sous QEMU (../freertos/main.c) ; seuls l'horloge, le compteur de temps et les
 * priorités changent. Horloge HSI de 64 MHz, celle où la ROM de démarrage laisse le cœur :
 * déterministe, sans PLL à configurer. Port ARM_CM55_NTZ de FreeRTOS : le cœur démarre en état
 * sécurisé et le firmware y reste (pas de TrustZone côté non sécurisé). */
#ifndef FREERTOS_CONFIG_H
#define FREERTOS_CONFIG_H

#include <stdint.h>

#define configCPU_CLOCK_HZ                      64000000UL     /* HSI */
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

/* Compteur de temps d'exécution : DWT CYCCNT (64 MHz). Remis à zéro au démarrage de
 * l'ordonnanceur : le DWT n'est pas réinitialisé par un reset système, et CYCCNT peut déjà
 * tourner (session de débogage précédente) ; la première tâche hériterait de son avance. */
#define RTS_DEMCR           ( *( volatile uint32_t * ) 0xE000EDFC )
#define RTS_DWT_CTRL        ( *( volatile uint32_t * ) 0xE0001000 )
#define RTS_DWT_CYCCNT      ( *( volatile uint32_t * ) 0xE0001004 )
#define RTS_DWT_LAR         ( *( volatile uint32_t * ) 0xE0001FB0 )   /* verrou logiciel CoreSight */
#define portCONFIGURE_TIMER_FOR_RUN_TIME_STATS()  do { RTS_DEMCR |= 1UL << 24; RTS_DWT_LAR = 0xC5ACCE55UL; \
                                                       RTS_DWT_CYCCNT = 0; RTS_DWT_CTRL |= 1UL; } while( 0 )
#define portGET_RUN_TIME_COUNTER_VALUE()          ( RTS_DWT_CYCCNT )

/* Fonctionnalités */
#define configUSE_MUTEXES                       1
#define configUSE_RECURSIVE_MUTEXES             1
#define configUSE_COUNTING_SEMAPHORES           1
#define configUSE_TASK_NOTIFICATIONS            1
#define configUSE_IDLE_HOOK                     1
#define configUSE_TICK_HOOK                     1    /* boucles Helium (lob.c) */
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

/* Port ARMv8-M : pas de TrustZone (tout en sécurisé), FPU et Helium (MVE) sauvegardés, ni MPU
 * ni PAC/BTI. */
#define configENABLE_TRUSTZONE                  0
#define configRUN_FREERTOS_SECURE_ONLY          1
#define configENABLE_FPU                        1
#define configENABLE_MVE                        1
#define configENABLE_MPU                        0

/* Priorités NVIC : le STM32N6 en implémente 4 bits ; 0xA0 = priorité 10. */
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
