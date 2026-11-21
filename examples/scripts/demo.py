"""Exemple de script gdbtrace (équivalent d'un .cmm Trace32).

    python -m gdbtrace examples/firmware/demo.elf --svd examples/svd/mps2-an385.svd \
        --script examples/scripts/demo.py
    python -m gdbtrace examples/firmware/demo.elf --svd examples/svd/mps2-an385.svd \
        --batch --script examples/scripts/demo.py
"""
import time

# --- run-control et données, cible arrêtée
t32.break_("update_sensor")
for _ in range(5):
    t32.go()
    t32.wait()
    print(f"tick={t32.eval('tick'):>3}  counter={t32.eval('counter')}")
t32.delete(t32.breaks()[0].number)
t32.d("sensors", 48)
t32.r()

# --- périphériques (SVD), cible arrêtée : lecture via gdb
if t32.device:
    t32.periph("SysTick")

# --- cible en marche : lectures par le canal live (OpenOCD TCL / QEMU QMP)
if t32.live:
    ticks = t32.addr("ticks")
    t32.go()
    for _ in range(3):
        time.sleep(0.1)
        print("ticks (live) =", int.from_bytes(t32.mem(ticks, 4), "little"))
    if t32.device:
        print("TIMER0.VALUE (live) =", hex(t32.periph("TIMER0")["VALUE"]))
    t32.halt()
