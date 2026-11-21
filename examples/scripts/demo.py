"""Exemple de script GdbTrace.

    python -m gdbtrace examples/firmware/demo.elf --svd examples/svd/mps2-an385.svd \
        --script examples/scripts/demo.py
    python -m gdbtrace examples/firmware/demo.elf --svd examples/svd/mps2-an385.svd \
        --batch --script examples/scripts/demo.py
"""
import time

# --- run-control et données, cible arrêtée
gdbtrace.break_("update_sensor")
for _ in range(5):
    gdbtrace.go()
    gdbtrace.wait()
    print(f"tick={gdbtrace.eval('tick'):>3}  counter={gdbtrace.eval('counter')}")
gdbtrace.delete(gdbtrace.breaks()[0].number)
gdbtrace.d("sensors", 48)
gdbtrace.r()

# --- périphériques (SVD), cible arrêtée : lecture via gdb
if gdbtrace.device:
    gdbtrace.periph("SysTick")

# --- cible en marche : lectures par le canal live (OpenOCD TCL / QEMU QMP)
if gdbtrace.live:
    ticks = gdbtrace.addr("ticks")
    gdbtrace.go()
    for _ in range(3):
        time.sleep(0.1)
        print("ticks (live) =", int.from_bytes(gdbtrace.mem(ticks, 4), "little"))
    if gdbtrace.device:
        print("TIMER0.VALUE (live) =", hex(gdbtrace.periph("TIMER0")["VALUE"]))
    gdbtrace.halt()
