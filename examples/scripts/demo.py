"""Exemple de script gdbtrace (équivalent d'un .cmm Trace32).

    python -m gdbtrace examples/firmware/demo.elf --script examples/scripts/demo.py
    python -m gdbtrace examples/firmware/demo.elf --batch --script examples/scripts/demo.py
"""
t32.break_("update_sensor")
for _ in range(5):
    t32.go()
    t32.wait()
    print(f"tick={t32.eval('tick'):>3}  counter={t32.eval('counter')}")
t32.delete(t32.breaks()[0].number)
t32.d("sensors", 48)
t32.r()
