"""Exemple de script FreeRTOS (TASK.List, objets du noyau, contexte d'une tâche).

    make -C examples/freertos
    python -m gdbtrace examples/freertos/freertos.elf --batch --script examples/scripts/rtos.py
"""
import time

# laisser tourner l'ordonnanceur, puis s'arrêter dans une tâche
t32.go()
time.sleep(0.5)
t32.halt()
t32.break_("worker_step", temporary=True)
t32.go()
t32.wait()

print("== Tâches (cible arrêtée, via gdb)")
t32.tasks()
print("\n== Objets du noyau")
t32.queues()
print("\n== Contexte de la tâche « consumer »")
t32.task("consumer")

# alerte si une pile dépasse 80 %
for t in t32.rtos().tasks:
    if t.stack_percent and t.stack_percent > 80:
        print(f"\n!! pile de {t.name} utilisée à {t.stack_percent:.0f} %")

if t32.live:
    print("\n== Tâches, cible en marche (canal live)")
    t32.go()
    time.sleep(0.3)
    t32.tasks()
    t32.halt()
