"""Exemple de script FreeRTOS (TASK.List, objets du noyau, contexte d'une tâche).

    make -C examples/freertos
    python -m gdbtrace examples/freertos/freertos.elf --batch --script examples/scripts/rtos.py
"""
import time

# laisser tourner l'ordonnanceur, puis s'arrêter dans une tâche
gdbtrace.go()
time.sleep(0.5)
gdbtrace.halt()
gdbtrace.break_("worker_step", temporary=True)
gdbtrace.go()
gdbtrace.wait()

print("== Tâches (cible arrêtée, via gdb)")
gdbtrace.tasks()
print("\n== Objets du noyau")
gdbtrace.queues()
print("\n== Contexte de la tâche « consumer »")
gdbtrace.task("consumer")

# alerte si une pile dépasse 80 %
for t in gdbtrace.rtos().tasks:
    if t.stack_percent and t.stack_percent > 80:
        print(f"\n!! pile de {t.name} utilisée à {t.stack_percent:.0f} %")

if gdbtrace.live:
    print("\n== Tâches, cible en marche (canal live)")
    gdbtrace.go()
    time.sleep(0.3)
    gdbtrace.tasks()
    gdbtrace.halt()
