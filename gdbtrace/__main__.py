"""Point d'entrée : python -m gdbtrace firmware.elf [--qemu | --openocd CFG... | --remote HOST:PORT
| --native].

Programme natif (`--native`, ou automatiquement pour un ELF qui n'est pas ARM) : exécuté sous
gdb lui-même, arrêté au début de `main` ; `--args` lui passe ses arguments.

Programmation : `--program [ELF]` écrit l'ELF dans la cible à la connexion (reset, load,
vérification, reset) ; avec `--batch` et sans script, gdbtrace programme puis quitte
(code de retour 1 si la vérification échoue), `--go` laisse alors le programme tourner.
"""
from __future__ import annotations

import argparse
import os
import shlex
import sys

from .core.target import NativeTarget, OpenOcdTarget, QemuTarget, RemoteTarget, free_port


def is_native_elf(elf: str) -> bool:
    """ELF d'une autre architecture qu'ARM (x86-64, AArch64…) : un programme natif."""
    try:
        from elftools.elf.elffile import ELFFile
        with open(elf, "rb") as fh:
            return ELFFile(fh)["e_machine"] != "EM_ARM"
    except Exception:  # noqa: BLE001 - illisible : on garde la cible par défaut
        return False


def build_target(args, elf: str) -> object:
    explicit = args.qemu or args.openocd is not None or args.remote
    if args.native or (not explicit and is_native_elf(elf)):
        return NativeTarget(args=shlex.split(args.args or ""),
                            cwd=os.path.abspath(args.cwd) if args.cwd else None)
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
                      gdb_port=args.gdb_port or free_port(), etm_sim=args.etm_sim,
                      mtb_sim=mtb_sim_size(args, elf), mtb_autohalt=args.mtb_autohalt)


def mtb_sim_size(args, elf: str) -> int | None:
    """Taille du tampon de la MTB simulée : `--mtb-sim N` (0 : illimité), sinon celle du
    tampon réservé par le firmware (symbole `__mtb_buffer__`...), sinon 1 Ko ; None sans
    MTB simulée."""
    if args.mtb_sim is None:
        return None
    if args.mtb_sim >= 0:
        return args.mtb_sim
    from .core.symbols import SymbolTable
    from .trace.mtb_capture import DEFAULT_SIZE, buffer_from_symbols
    found = buffer_from_symbols(SymbolTable(os.path.abspath(elf)))
    return found[1] if found else DEFAULT_SIZE


def int_auto(text: str) -> int:
    return int(text, 0)


def _print_action(kind: str, data: dict) -> None:
    """--batch : messages des breakpoints à action sur stdout, leurs erreurs sur stderr."""
    if kind == "output" and data.get("stream") == "action":
        print(data.get("text", ""), end="", flush=True,
              file=sys.stderr if data.get("error") else sys.stdout)


def setup_trace(args, trace, session, target, log=print) -> None:
    """Options de trace, après la connexion (cible arrêtée) : programmation de la cible puis
    démarrage des captures. Commun au mode --batch (`log` = print) et au GUI (console)."""
    if args.etm_filter or args.etm_start or args.etm_stop:   # avant la programmation de l'ETM
        for line in trace.set_etm_filter(args.etm_filter, args.etm_start, args.etm_stop, session):
            log(line)
    for spec in args.var_trace:          # trace de données du DWT, avant le début des captures
        expr, _, mode = spec.rpartition(":") if ":" in spec else (spec, "", "")
        access, _, pc = mode.partition("+")
        for line in trace.trace_variable(session, expr, access or "write", pc == "pc",
                                         cpu_hz=args.cpu_hz):
            log(line)
    if args.etm_config and args.etm_port == "swo":      # ETM + ITM formatés sur le SWO
        for line in trace.start_etm_swo(session, target, args.cpu_hz, args.swo_hz,
                                         stall=args.etm_stall, stm32=args.stm32,
                                         port=args.swo_port, swo=args.swo, swo_file=args.swo_file,
                                         cycle_accurate=args.etm_cycle_accurate):
            log(line)
    else:
        if args.swo_config:
            from .trace.config import SwoConfig
            cfg = SwoConfig(cpu_hz=args.cpu_hz, swo_hz=args.swo_hz, ports=0xFFFFFFFF)
            for line in trace.configure_target(cfg, session, target, args.swo_port):
                log(line)
        if args.swo:
            host, _, port = args.swo.rpartition(":")
            trace.start_tcp(host or "127.0.0.1", int(port), args.swo_formatter)
            log(f"capture SWO TCP {args.swo} démarrée")
        elif args.swo_file:
            trace.start_file(args.swo_file, args.swo_formatter)
            log(f"capture SWO rejouée : {args.swo_file}")
        if args.etm_config:
            from .trace.etm.config import EtmConfig
            cfg = EtmConfig(port_width=args.etm_width, stm32=args.stm32, stall=args.etm_stall,
                            cycle_accurate=args.etm_cycle_accurate)
            for line in trace.configure_etm(cfg, session):
                log(line)
    if args.mtb_probe:
        from .trace.mtb import rom_components
        comps = rom_components(session.read_u32)
        log("Composants CoreSight :" if comps else "Aucune table ROM CoreSight lisible")
        for c in comps:
            log(f"  {'  ' * c.depth}{c.addr:#010x}  {c.name}")
    if args.mtb is not None:
        setup_mtb(args, trace, session, log)
    if args.trace:                       # port parallèle : formateur TPIU, ETM + ITM
        host, _, port = args.trace.rpartition(":")
        trace.start_tcp(host or "127.0.0.1", int(port), formatter=True)
        log(f"capture de trace TCP {args.trace} démarrée")
    elif args.trace_file:
        trace.start_file(args.trace_file, formatter=True)
        log(f"capture de trace rejouée : {args.trace_file}")



def setup_mtb(args, trace, session, log=print) -> None:
    """--mtb : registres (auto-détection ou adresse), tampon (option ou symbole), programmation."""
    from .trace.mtb import find_mtb
    from .trace.mtb_capture import buffer_from_symbols, describe
    base = find_mtb(session.read_u32) if args.mtb == "auto" else int(args.mtb, 0)
    if base is None:
        raise RuntimeError("MTB introuvable (ni dans les tables ROM CoreSight, ni aux adresses "
                           "connues) : la puce en a-t-elle une ? Sinon --mtb ADRESSE ; voir --mtb-probe")
    if args.mtb_buffer:
        addr, _, size = args.mtb_buffer.partition(":")
        buffer = (int(addr, 0), int(size, 0))
    else:
        buffer = buffer_from_symbols(trace.etm.symbols if trace.etm is not None else None)
        if buffer is None:
            raise RuntimeError("tampon MTB : réserver un tableau `__mtb_buffer__` aligné sur sa "
                               "taille dans le firmware, ou --mtb-buffer ADRESSE:TAILLE")
    log(describe(base, trace.setup_mtb(session, base, *buffer, autohalt=args.mtb_autohalt)))


def build_parser() -> argparse.ArgumentParser:
    """Options de la ligne de commande (aussi utilisées par les tests du GUI)."""
    p = argparse.ArgumentParser(prog="gdbtrace", description="GdbTrace : débogueur Cortex-M (et programmes natifs) sur gdb")
    p.add_argument("elf", nargs="?",
                   help="firmware ELF avec symboles DWARF (facultatif en GUI : Fichier › Programmer un ELF…)")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--qemu", action="store_true", help="cible QEMU (défaut)")
    g.add_argument("--openocd", nargs="*", metavar="CFG",
                   help="lancer OpenOCD avec ces fichiers -f (ex. board/st_nucleo_f4.cfg)")
    g.add_argument("--remote", metavar="HOST:PORT", help="serveur gdb déjà lancé (J-Link, pyOCD...)")
    g.add_argument("--native", action="store_true",
                   help="programme natif (hôte) exécuté sous gdb, arrêté au début de main "
                        "(par défaut pour un ELF qui n'est pas ARM)")
    p.add_argument("--args", metavar="'A B…'", help="programme natif : ses arguments (syntaxe shell)")
    p.add_argument("--cwd", metavar="DOSSIER", help="programme natif : son répertoire de travail")
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
    t.add_argument("--var-trace", action="append", default=[], metavar="EXPR[:ACCÈS]",
                   help="trace de données du DWT : valeur d'une globale à chaque accès ; ACCÈS = "
                        "write (défaut), read, rw, suivi de +pc pour le PC (ex. counter:rw+pc) ; "
                        "répétable ; avec une capture SWO ou de trace")
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
    e.add_argument("--etm-port", choices=("parallel", "swo"), default="parallel",
                   help="sortie de l'ETM : port de trace parallèle (sonde de trace) ou broche SWO "
                        "(formateur TPIU, capturée par la sonde de débogage via OpenOCD ; "
                        "faible débit, voir --cpu-hz / --swo-hz)")
    e.add_argument("--etm-stall", action="store_true",
                   help="ralentir le cœur plutôt que perdre de la trace quand la FIFO de l'ETM se "
                        "remplit (indispensable par SWO sur un cœur rapide)")
    e.add_argument("--stm32", action="store_true", help="activer les broches de trace (DBGMCU_CR des STM32)")
    e.add_argument("--etm-filter", action="append", default=[], metavar="PLAGE",
                   help="ne tracer que cette plage : fonction (fib), fichier (main.c) ou adresses "
                        "(0x100-0x200, 0x100+64) ; répétable")
    e.add_argument("--etm-start", action="append", default=[], metavar="LIEU",
                   help="démarrer la trace en exécutant ce lieu (fonction, fichier.c:ligne, adresse)")
    e.add_argument("--etm-stop", action="append", default=[], metavar="LIEU",
                   help="arrêter la trace après ce lieu")
    m = p.add_argument_group("trace MTB (Cortex-M0+)")
    m.add_argument("--mtb", nargs="?", const="auto", metavar="ADRESSE",
                   help="activer la MTB de la cible (registres trouvés par les tables ROM CoreSight, "
                        "ou à ADRESSE) ; tampon lu et remis à zéro à chaque arrêt")
    m.add_argument("--mtb-buffer", metavar="ADRESSE:TAILLE",
                   help="tampon de la MTB en SRAM (aligné sur sa taille) ; par défaut le symbole "
                        "__mtb_buffer__ / mtb_buffer / __MTB_BUFFER du firmware")
    m.add_argument("--mtb-sim", nargs="?", const=-1, type=int_auto, metavar="TAILLE",
                   help="QEMU : MTB simulée, tampon de TAILLE octets (défaut : celui du firmware, "
                        "sinon 1024 ; 0 : illimité, toute l'exécution) ; ex. avec --machine microbit "
                        "et examples/m0plus")
    m.add_argument("--mtb-autohalt", action="store_true",
                   help="avec --mtb ou --mtb-sim : la MTB arrête le cœur à chaque tampon plein, "
                        "GdbTrace le vide et relance (arrêts invisibles) : trace complète, cible "
                        "ralentie ; préférer un tampon de 4 à 8 Ko")
    m.add_argument("--mtb-probe", action="store_true",
                   help="lister les composants CoreSight de la cible (MTB, ETM, TPIU...) à la connexion")
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
    return p


def main(argv: list[str] | None = None) -> int:
    p = build_parser()
    args = p.parse_args(argv)
    if args.program:
        if args.elf and os.path.abspath(args.elf) != os.path.abspath(args.program):
            p.error("deux ELF différents : l'ELF de la ligne de commande et celui de --program")
        args.elf = args.program
    if not args.etm_config:              # options de programmation de l'ETM : sinon ignorées
        stray = [opt for opt, on in (("--etm-port swo", args.etm_port == "swo"),
                                     ("--etm-stall", args.etm_stall), ("--stm32", args.stm32),
                                     ("--etm-width", args.etm_width != 4)) if on]
        if stray:
            p.error(f"{', '.join(stray)} : avec --etm-config (programmer l'ETM)")
    if (args.etm_filter or args.etm_start or args.etm_stop) and not (
            args.etm_sim or args.etm_config or args.trace or args.trace_file
            or ((args.swo or args.swo_file) and args.swo_formatter)):
        p.error("--etm-filter / --etm-start / --etm-stop : avec une trace ETM (--etm-sim sous QEMU ; "
                "--etm-config sur matériel, ou une capture --trace / --trace-file)")
    if args.var_trace and not (args.swo or args.swo_file or args.swo_config or args.trace
                               or args.trace_file or (args.etm_config and args.etm_port == "swo")):
        p.error("--var-trace : avec une capture SWO ou de trace (--swo-config, --swo, --swo-file, "
                "--etm-config --etm-port swo, --trace)")
    etm_swo = args.etm_config and args.etm_port == "swo"
    swo_etm = (args.swo or args.swo_file) and args.swo_formatter     # flux formaté : ETM ID 2
    if args.coverage and not (args.etm_sim or args.trace or args.trace_file or args.coverage_merge
                              or etm_swo or swo_etm or args.mtb is not None or args.mtb_sim is not None):
        p.error("--coverage : pas de trace d'instructions (--etm-sim / --mtb-sim sous QEMU ; "
                "--etm-config --etm-port swo, --trace / --trace-file ou --mtb sur matériel)")
    if args.mtb_autohalt and args.mtb is None and args.mtb_sim is None:
        p.error("--mtb-autohalt : avec --mtb (cible) ou --mtb-sim (QEMU)")
    program = args.program is not None
    verify = not args.no_verify
    if args.elf is None and (args.batch or program):
        p.error("un ELF est nécessaire (argument ou --program ELF)")
    elf = os.path.abspath(args.elf) if args.elf else None
    if elf and not os.path.isfile(elf):
        p.error(f"ELF introuvable : {elf}")

    target = build_target(args, elf) if elf else None
    load = False if (args.no_load or program) else None

    if args.batch:
        from .core.scripting import ScriptAPI
        from .core.session import Session
        from .core import svd
        from .trace.manager import TraceManager
        device = svd.parse(args.svd) if args.svd else None
        s = Session(args.gdb)
        s.on_event(_print_action)                # messages des breakpoints à action
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
        msg = None
        if not target.native:                    # trace d'instructions : Cortex-M seulement
            msg = trace.setup_etm(elf, symbols, cycle_accurate=args.etm_cycle_accurate)
            if trace.etm is not None:
                trace.detect_etm(s)              # ETMv3 (M3/M4) ou ETMv4 (M7)
        if args.etm_sim or args.mtb_sim is not None:
            if msg:
                print(msg)
            elif args.mtb_sim is not None:
                trace.enable_mtb_sim(live, target.mtb_sim, s, autohalt=args.mtb_autohalt)
                trace.attach_session(s)
            else:
                trace.enable_qemu_sim(live)
                trace.attach_session(s)
        api = ScriptAPI(s, live, device)
        api.trace = trace
        api.symbols = symbols
        api.cpu_hz = args.cpu_hz
        api.on_program = lambda e: trace.retarget(e, api._symbols(), args.etm_cycle_accurate)
        try:
            setup_trace(args, trace, s, target)
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
    # valeurs initiales de Trace › ETM par SWO…
    app.swo_hz, app.swo_port, app.etm_stall, app.stm32 = args.swo_hz, args.swo_port, args.etm_stall, args.stm32
    if args.svd:
        app.safe(app.load_svd, args.svd)

    def after_connect(_res=None) -> None:
        # journal de la mise en route de la trace dans la console (pas sur le terminal)
        app.safe(setup_trace, args, app.trace, app.session, app.session.target or target,
                 lambda line: app.write_console(line + "\n", "gdb"))
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
