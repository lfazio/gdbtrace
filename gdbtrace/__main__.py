"""Point d'entrée : python -m gdbtrace firmware.elf [--qemu | --openocd CFG... | --remote HOST:PORT]."""
from __future__ import annotations

import argparse
import os
import sys

from .core.target import OpenOcdTarget, QemuTarget, RemoteTarget, free_port


def build_target(args) -> object:
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
    return QemuTarget(elf=os.path.abspath(args.elf), machine=args.machine,
                      gdb_port=args.gdb_port or free_port())


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="gdbtrace", description="Débogueur Cortex-M à la Trace32, sur gdb")
    p.add_argument("elf", help="firmware ELF avec symboles DWARF")
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
    p.add_argument("--machine", default="mps2-an385", help="machine QEMU")
    p.add_argument("--gdb", default="gdb-multiarch", help="exécutable gdb")
    p.add_argument("--gdb-port", type=int, default=0)
    p.add_argument("--no-load", action="store_true", help="ne pas flasher l'ELF à la connexion")
    p.add_argument("--script", help="script Python exécuté après connexion (objet `t32`)")
    p.add_argument("--batch", action="store_true", help="sans GUI : exécute --script puis quitte")
    p.add_argument("--layout", default="gdbtrace-layout.json", help="fichier de layout des fenêtres")
    args = p.parse_args(argv)

    target = build_target(args)
    load = False if args.no_load else None

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

    if args.batch:
        from .core.scripting import ScriptAPI
        from .core.session import Session
        from .core import svd
        from .trace.manager import TraceManager
        device = svd.parse(args.svd) if args.svd else None
        s = Session(args.gdb)
        s.connect(os.path.abspath(args.elf), target, load)
        live = target.live_memory()
        trace = TraceManager(on_error=print)
        try:
            setup_trace(trace, s)
            if args.script:
                api = ScriptAPI(s, live, device)
                api.trace = trace
                api.run_script(args.script)
        finally:
            trace.stop()
            if live:
                live.close()
            s.disconnect()
        return 0

    from .gui.app import App
    app = App(layout_file=args.layout)
    app.cpu_hz = args.cpu_hz
    if args.svd:
        app.safe(app.load_svd, args.svd)

    def after_connect() -> None:
        app.safe(setup_trace, app.trace, app.session)
        if args.script:
            app.run_script(args.script)
    app.connect(os.path.abspath(args.elf), target, args.gdb, load, then=after_connect)
    app.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
