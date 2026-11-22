"""Exemple de session de débogage scriptée : programmer, poser des breakpoints et un
watchpoint, remplir la Watch, faire des pas, lire et modifier des variables.

    make -C examples/firmware
    python -m gdbtrace examples/firmware/demo.elf --script examples/scripts/session.py
    python -m gdbtrace examples/firmware/demo.elf --batch --script examples/scripts/session.py

Sur une carte (ici la démo est faite pour QEMU mps2-an385) : --openocd CFG… et votre ELF,
en adaptant les noms de fonctions et de variables. Dans le GUI, le script tourne hors du
thread de l'interface : les vues (Watch, Source, Pile, Breakpoints) suivent à chaque arrêt.
Objets disponibles : `gdbtrace` (API GdbTrace) et `session` (Session gdb, plus bas niveau).
"""
import time


def ici() -> str:
    """Où la cible est arrêtée : fonction, fichier:ligne."""
    f = session.frame()
    return f"{f.func or '??'} ({f.file}:{f.line})" if f.file else f"{f.pc:#010x}"


def arret() -> dict:
    """Attend l'arrêt de la cible et le raconte."""
    why = gdbtrace.wait(10)
    raison = why.get("reason", "?")
    if raison == "watchpoint-trigger":
        v = why.get("value", {})
        raison += f" : {why['wpt']['exp']} {v.get('old')} → {v.get('new')}"
    elif raison == "breakpoint-hit":
        raison += f" (breakpoint {why.get('bkptno')})"
    print(f"  arrêt : {raison} — {ici()}")
    return why


# ---------------------------------------------------------------- 1. programmer la cible
print("== Programmation (FLASH.ReProgram : écriture, vérification, reset)")
gdbtrace.program()                  # l'ELF courant ; gdbtrace.program("autre.elf") pour un autre
print(f"  cible arrêtée au vecteur de reset : pc = {gdbtrace.reg('pc'):#010x}")

# ---------------------------------------------------------------- 2. breakpoints
print("\n== Breakpoints")
b_main = gdbtrace.break_("main", temporary=True)           # temporaire : supprimé au 1er arrêt
b_upd = gdbtrace.break_("update_sensor")                   # sur une fonction
b_anim = gdbtrace.break_("animate", condition="tick == 6") # conditionnel
b_fib = gdbtrace.break_("fib", hw=True)                    # matériel (FPB) : marche en flash
gdbtrace.enable(b_fib.number, False)                       # posé mais désactivé pour l'instant
for b in gdbtrace.breaks():
    print(f"  {b.number:>3} {b.type:<14} {'oui' if b.enabled else 'non':<4} {b.func or b.addr}")

# ---------------------------------------------------------------- 3. exécution
print("\n== Go jusqu'à main")
gdbtrace.go()
arret()

# ---------------------------------------------------------------- 4. la Watch (Var.Watch)
# Dans main, le code de démarrage a initialisé .data et .bss. Dans le GUI, chaque expression
# s'ajoute à la vue Watch, avec son format d'affichage ; en --batch, sa valeur est affichée.
# Les globales sont suivies en direct, cible en marche.
print("\n== Watch")
gdbtrace.var_watch("counter")
gdbtrace.var_watch("sensors", "hex")           # format hérité par tous les membres
gdbtrace.var_watch("last_char", "ascii")
gdbtrace.var_watch("offset", "bin")
gdbtrace.var_watch("temperature")
gdbtrace.var_watch("ticks", "hex")

print("\n== Go jusqu'à update_sensor")
gdbtrace.go()
arret()
print(f"  arguments : s = {gdbtrace.eval('s')}, tick = {gdbtrace.eval('tick')} "
      f"({gdbtrace.eval('tick', 'hex')})")
print(f"  *s = {gdbtrace.eval('*s')}")
gdbtrace.var_watch("*s", "hex")                 # une expression locale : réévaluée à chaque arrêt
gdbtrace.bt()

# ---------------------------------------------------------------- 5. pas à pas
print("\n== Pas à pas")
gdbtrace.step()                                 # Step : entre dans fib()
print(f"  step → {ici()}")
gdbtrace.out()                                  # Go.Return : ressort de fib()
print(f"  out  → {ici()}")
gdbtrace.over()                                 # Step.Over : ligne suivante
print(f"  over → {ici()}, s->value = {gdbtrace.eval('s->value')}")

# ---------------------------------------------------------------- 6. breakpoint conditionnel
print("\n== Breakpoint conditionnel : animate quand tick == 6")
gdbtrace.delete(b_upd.number)                   # update_sensor ne nous intéresse plus
gdbtrace.go()
arret()
print(f"  tick = {gdbtrace.eval('tick')}, last_char = {gdbtrace.eval('last_char', 'ascii')}")
gdbtrace.delete(b_anim.number)

# ---------------------------------------------------------------- 7. watchpoint
print("\n== Watchpoint : arrêt quand `mode` est écrit")
w = gdbtrace.watch("mode")                      # access="read" / "rw" : lecture, accès
gdbtrace.go()
arret()
gdbtrace.delete(w)

# ---------------------------------------------------------------- 8. lire et modifier
print("\n== Données")
gdbtrace.set("sensors[0].value", 1234)          # Var.Assign
print(f"  sensors[0] = {gdbtrace.eval('sensors[0]')}")
print(f"  sensors[0].value en hexadécimal : {gdbtrace.eval('sensors[0].value', 'hex')}")
gdbtrace.d("sensors", 32)                       # Data.dump
gdbtrace.r()                                    # Register.view

# ---------------------------------------------------------------- 9. breakpoint matériel
print("\n== Breakpoint matériel sur fib (réactivé)")
gdbtrace.enable(b_fib.number)
gdbtrace.go()
arret()
print(f"  fib(n = {gdbtrace.eval('n')})")
gdbtrace.delete(b_fib.number)

# ---------------------------------------------------------------- 10. cible en marche
if gdbtrace.live:                               # canal mémoire latéral (QEMU QMP, OpenOCD TCL)
    print("\n== Cible en marche : lectures sans l'arrêter")
    ticks = gdbtrace.addr("ticks")
    gdbtrace.go()
    for _ in range(3):
        time.sleep(0.2)
        print(f"  ticks = {int.from_bytes(gdbtrace.mem(ticks, 4), 'little')}")
    gdbtrace.halt()
    print(f"  Break : {ici()}")

print(f"\n== Fin : {len(gdbtrace.breaks())} breakpoint(s) restant(s)")
