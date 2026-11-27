/*
 * Programme natif de démo pour GdbTrace : threads POSIX nommés.
 *
 *   producer : remplit une file bornée (mutex + variable de condition), produce_step()
 *   consumer : la vide, consume_step() ; total = somme des valeurs consommées
 *   monitor  : relit `produced` et `consumed` toutes les 10 ms (lecture en direct)
 *   main     : attend la fin des trois
 *
 * Usage : ./pthreads [TOURS]   (0 ou rien : sans fin)
 */
#define _GNU_SOURCE
#include <pthread.h>
#include <stdio.h>
#include <stdlib.h>
#include <unistd.h>

#define QUEUE_LEN 8

static int queue[QUEUE_LEN];
static int head, tail, count;
static pthread_mutex_t lock = PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t not_empty = PTHREAD_COND_INITIALIZER;
static pthread_cond_t not_full = PTHREAD_COND_INITIALIZER;

volatile long produced;          /* lus en direct pendant que le programme tourne */
volatile long consumed;
volatile long total;
static long rounds;              /* 0 : sans fin */

int produce_step(int value)
{
    pthread_mutex_lock(&lock);
    while (count == QUEUE_LEN)
        pthread_cond_wait(&not_full, &lock);
    queue[tail] = value;
    tail = (tail + 1) % QUEUE_LEN;
    count++;
    produced++;
    pthread_cond_signal(&not_empty);
    pthread_mutex_unlock(&lock);
    return value;
}

int consume_step(void)
{
    pthread_mutex_lock(&lock);
    while (count == 0)
        pthread_cond_wait(&not_empty, &lock);
    int value = queue[head];
    head = (head + 1) % QUEUE_LEN;
    count--;
    consumed++;
    total += value;
    pthread_cond_signal(&not_full);
    pthread_mutex_unlock(&lock);
    return value;
}

/* chaque thread se nomme lui-même avant tout travail : nommé par main après sa création, il
   pouvait atteindre un breakpoint encore anonyme (machine chargée) */
static void name_self(const char *name)
{
    pthread_setname_np(pthread_self(), name);
}

static void *producer(void *arg)
{
    (void)arg;
    name_self("producer");
    for (long i = 1; rounds == 0 || i <= rounds; i++) {
        produce_step((int)(i % 1000));
        usleep(200);
    }
    return NULL;
}

static void *consumer(void *arg)
{
    (void)arg;
    name_self("consumer");
    for (long i = 1; rounds == 0 || i <= rounds; i++)
        consume_step();
    return NULL;
}

static void *monitor(void *arg)
{
    (void)arg;
    name_self("monitor");
    long last = -1;
    while (rounds == 0 || consumed < rounds) {
        if (consumed != last)
            last = consumed;
        usleep(10000);
    }
    return NULL;
}

int main(int argc, char **argv)
{
    pthread_t threads[3];
    void *(*bodies[3])(void *) = {producer, consumer, monitor};
    const char *names[3] = {"producer", "consumer", "monitor"};

    rounds = argc > 1 ? atol(argv[1]) : 0;
    for (int i = 0; i < 3; i++) {
        pthread_create(&threads[i], NULL, bodies[i], NULL);
        pthread_setname_np(threads[i], names[i]);
    }
    for (int i = 0; i < 3; i++)
        pthread_join(threads[i], NULL);
    printf("produced=%ld consumed=%ld total=%ld\n", produced, consumed, total);
    return total == 0 && rounds > 0;
}
