"""Point d'entrée : python -m gdbtrace firmware.elf [--qemu | --openocd CFG... | --remote HOST:PORT].

Programmation : `--program [ELF]` écrit l'ELF dans la cible à la connexion (reset, load,
vérification, reset) ; avec `--batch` et sans script, gdbtrace programme puis quitte
(code de retour 1 si la vérification échoue), `--go` laisse alors le programme tourner.
"""
from __future__ import annotations

import argparse
import os
import sys

from .core.target import OpenOcdTarget, QemuTarget, RemoteTarget, free_port


def build_target(args, elf: str) -> object:
    if args.remote:
        host, _, port = args.remote.rpartition(":")
        t = RemoteTarget(host=host or "127.0.0.1", gdb_port=int(port))
        if args.tcl:
            thost, _, tport = args.tcl.rpartition(":")
            t.tcl_host, t.tcl_port = thost or t.host, int(tport)
        return t
    if args.openocd is not None:
        return OpenOcdTarget(configs=args.openocd or ["board/st_nucleo_f4.cfg"],
                             gdb_port=args.gdb_port or 3333, rtos=args.rtos)
    return QemuTarget(elf=os.path.abspath(elf), machine=args.machine,
                      gdb_port=args.gdb_port or free_port(), etm_sim=args.etm_sim)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="gdbtrace", description="Débogueur Cortex-M à la Trace32, sur gdb")
    p.add_argument("elf", nargs="?",
                   help="firmware ELF avec symboles DWARF (facultatif en GUI : Fichier › Programmer un ELF…)")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--qemu", action="store_true", help="cible QEMU (défaut)")
    g.add_argument("--openocd", nargs="*", metavar="CFG",
                   help="lancer OpenOCD avec ces fichiers -f (ex. board/st_nucleo_f4.cfg)")
    g.add_argument("--remote", metavar="HOST:PORT", help="serveur gdb déjà lancé (J-Link, pyOCD...)")
    p.add_argument("--tcl", metavar="HOST:PORT",
                   help="avec --remote : port TCL d'un OpenOCD lancé à part (mémoire en direct)")
    p.add_argument("--svd", metavar="FICHIER", help="description CMSIS-SVD des périphériques")
    p.add_argument("--rtos", nargs="?", const="auto", metavar="TYPE",
                   help="avec --openocd : support RTOS d'OpenOCD (tâches vues comme threads gdb)")
    t = p.add_argument_group("trace SWO/ITM")
    t.add_argument("--swo", metavar="HOST:PORT", help="capture SWO servie en TCP (OpenOCD, Orbuculum, J-Link)")
    t.add_argument("--swo-file", metavar="FICHIER", help="rejouer une capture SWO brute")
    t.add_argument("--swo-formatter", action="store_true", help="flux SWO avec formateur TPIU")
    t.add_argument("--swo-config", action="store_true",
                   help="programmer ITM/DWT/TPIU à la connexion (+ TPIU d'OpenOCD et capture)")
    t.add_argument("--cpu-hz", type=int, default=25_000_000, help="fréquence du cœur (Hz)")
    t.add_argument("--swo-hz", type=int, default=2_000_000, help="débit SWO (Hz)")
    t.add_argument("--swo-port", type=int, default=3443, help="port TCP du SWO servi par OpenOCD")
    e = p.add_argument_group("trace d'instructions ETM")
    e.add_argument("--etm-sim", action="store_true",
                   help="QEMU : ETM simulé (journal d'exécution converti en trace ETMv3 à chaque arrêt)")
    e.add_argument("--trace", metavar="HOST:PORT",
                   help="flux TPIU brut formaté (ETM + ITM) servi en TCP (ex. par Orbuculum)")
    e.add_argument("--trace-file", metavar="FICHIER", help="rejouer une capture TPIU formatée")
    e.add_argument("--etm-config", action="store_true",
                   help="programmer l'ETM et le TPIU parallèle à la connexion")
    e.add_argument("--etm-cycle-accurate", action="store_true",
                   help="trace ETM cycle-accurate (durées mesurées pour l'analyse temporelle, ~8× plus de trace)")
    e.add_argument("--etm-width", type=int, default=4, choices=(1, 2, 4), help="largeur du port de trace")
    e.add_argument("--stm32", action="store_true", help="activer les broches de trace (DBGMCU_CR des STM32)")
    p.add_argument("--machine", default="mps2-an385", help="machine QEMU")
    p.add_argument("--gdb", default="gdb-multiarch", help="exécutable gdb")
    p.add_argument("--gdb-port", type=int, default=0)
    p.add_argument("--no-load", action="store_true", help="ne pas flasher l'ELF à la connexion")
    g2 = p.add_argument_group("programmation")
    g2.add_argument("--program", nargs="?", const="", metavar="ELF",
                    help="programmer l'ELF dans la cible à la connexion : reset, écriture, vérification, "
                         "reset (ELF par défaut : celui de la ligne de commande)")
    g2.add_argument("--no-verify", action="store_true", help="ne pas vérifier la programmation")
    c = p.add_argument_group("couverture de code (trace ETM)")
    c.add_argument("--coverage", action="append", default=[], metavar="FICHIER",
                   help="--batch : écrire la couverture à la fin (.html, .info LCOV, .json) ; répétable")
    c.add_argument("--coverage-merge", action="append", default=[], metavar="FICHIER.json",
                   help="ajouter une couverture enregistrée (campagnes précédentes) ; répétable")
    g2.add_argument("--go", action="store_true",
                    help="lancer le programme après la connexion (et la programmation)")
    p.add_argument("--script", help="script Python exécuté après connexion (objet `gdbtrace`)")
    p.add_argument("--batch", action="store_true", help="sans GUI : exécute --script puis quitte")
    p.add_argument("--layout", default="gdbtrace-layout.json", help="fichier de layout des fenêtres")
    args = p.parse_args(argv)
    if args.program:
        if args.elf and os.path.abspath(args.elf) != os.path.abspath(args.program):
            p.error("deux ELF différents : l'ELF de la ligne de commande et celui de --program")
        args.elf = args.program
    if args.coverage and not (args.etm_sim or args.trace or args.trace_file or args.coverage_merge):
        p.error("--coverage : pas de trace ETM (--etm-sim sous QEMU, --trace/--trace-file sur matériel)")
    program = args.program is not None
    verify = not args.no_verify
    if args.elf is None and (args.batch or program):
        p.error("un ELF est nécessaire (argument ou --program ELF)")
    elf = os.path.abspath(args.elf) if args.elf else None
    if elf and not os.path.isfile(elf):
        p.error(f"ELF introuvable : {elf}")

    target = build_target(args, elf) if elf else None
    load = False if (args.no_load or program) else None

    def setup_trace(trace, session) -> None:
        """Options SWO : configuration de la cible puis démarrage de la capture."""
        if args.swo_config:
            from .trace.config import SwoConfig
            cfg = SwoConfig(cpu_hz=args.cpu_hz, swo_hz=args.swo_hz, ports=0xFFFFFFFF)
            for line in trace.configure_target(cfg, session, target, args.swo_port):
                print(line)
        if args.swo:
            host, _, port = args.swo.rpartition(":")
            trace.start_tcp(host or "127.0.0.1", int(port), args.swo_formatter)
        elif args.swo_file:
            trace.start_file(args.swo_file, args.swo_formatter)
        if args.etm_config:
            from .trace.etm.config import EtmConfig
            for line in trace.configure_etm(EtmConfig(port_width=args.etm_width, stm32=args.stm32,
                                                           cycle_accurate=args.etm_cycle_accurate), session):
                print(line)
        if args.trace:                       # port parallèle : formateur TPIU, ETM + ITM
            host, _, port = args.trace.rpartition(":")
            trace.start_tcp(host or "127.0.0.1", int(port), formatter=True)
        elif args.trace_file:
            trace.start_file(args.trace_file, formatter=True)

    if args.batch:
        from .core.scripting import ScriptAPI
        from .core.session import Session
        from .core import svd
        from .trace.manager import TraceManager
        device = svd.parse(args.svd) if args.svd else None
        s = Session(args.gdb)
        s.connect(elf, target, load)
        rc = 0
        if program:
            res = s.program(elf, verify)
            print(res.summary())
            rc = 1 if res.mismatched else 0
        live = target.live_memory()
        trace = TraceManager(on_error=print)
        from .core.symbols import SymbolTable
        symbols = SymbolTable(elf)
        msg = trace.setup_etm(elf, symbols, cycle_accurate=args.etm_cycle_accurate)
        if args.etm_sim:
            if msg:
                print(msg)
            else:
                trace.enable_qemu_sim(live)
                trace.attach_session(s)
        api = ScriptAPI(s, live, device)
        api.trace = trace
        api.symbols = symbols
        api.cpu_hz = args.cpu_hz
        api.on_program = lambda e: trace.retarget(e, api._symbols(), args.etm_cycle_accurate)
        try:
            setup_trace(trace, s)
            for path in args.coverage_merge:
                api.cov.load(path)
            if args.script:
                api.run_script(args.script)
            for path in args.coverage:
                api.cov.export(path)
            if args.coverage:
                api.cov.summary()
            if args.go and s.state == "stopped":
                s.go()                          # le programme continue après gdbtrace
        except (RuntimeError, ValueError, OSError) as exc:
            print(f"erreur : {exc}", file=sys.stderr)
            rc = 1
        finally:
            trace.close()
            if live:
                live.close()
            s.disconnect(halt=not args.go)
        return rc

    from .gui.app import App
    app = App(layout_file=args.layout)
    app.cpu_hz = args.cpu_hz
    app.etm_cycle_accurate = args.etm_cycle_accurate
    app.gdb_path = args.gdb
    app.verify_program = verify
    app.verify_var.set(verify)
    app.target_factory = lambda e: build_target(args, e)     # Fichier › Programmer un ELF…
    if args.svd:
        app.safe(app.load_svd, args.svd)

    def after_connect(_res=None) -> None:
        app.safe(setup_trace, app.trace, app.session)
        if args.script:
            app.run_script(args.script)
        if args.go:
            app.go()
    if elf is None:
        app.status.set("Pas d'ELF : Fichier › Programmer un ELF… (Ctrl+O) lance la cible")
    elif program:
        app.connect(elf, target, args.gdb, load,
                    then=lambda: app.program_elf(elf, verify, then=after_connect))
    else:
        app._remember_elf(elf)
        app.connect(elf, target, args.gdb, load, then=after_connect)
    app.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
