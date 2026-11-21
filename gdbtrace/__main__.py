"""Point d'entrée : python -m gdbtrace firmware.elf [--qemu | --openocd CFG... | --remote HOST:PORT]."""
from __future__ import annotations

import argparse
import os
import sys

from .core.target import OpenOcdTarget, QemuTarget, RemoteTarget, free_port


def build_target(args) -> object:
    if args.remote:
        host, _, port = args.remote.rpartition(":")
        return RemoteTarget(host=host or "127.0.0.1", gdb_port=int(port))
    if args.openocd is not None:
        return OpenOcdTarget(configs=args.openocd or ["board/st_nucleo_f4.cfg"],
                             gdb_port=args.gdb_port or 3333)
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

    if args.batch:
        from .core.scripting import ScriptAPI
        from .core.session import Session
        s = Session(args.gdb)
        s.connect(os.path.abspath(args.elf), target, load)
        try:
            if args.script:
                ScriptAPI(s).run_script(args.script)
        finally:
            s.disconnect()
        return 0

    from .gui.app import App
    app = App(layout_file=args.layout)
    app.connect(os.path.abspath(args.elf), target, args.gdb, load,
                then=(lambda: app.run_script(args.script)) if args.script else None)
    app.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
