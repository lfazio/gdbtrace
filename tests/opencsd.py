"""Oracle OpenCSD pour les tests ETM : snapshot + `trc_pkt_lister -decode`.

OpenCSD (ARM/Linaro) est le décodeur de référence (celui de perf). On lui donne la même
trace et le même code que notre décodeur, dans son format « snapshot » :
- cœur Cortex-M4 avec la mémoire des segments de l'ELF ;
- ETM ETMv3.5 avec l'ETMIDR réel d'un ETM-M4 (0x4114F250 : encodage de branchement
  alternatif), ID de trace 2 ;
- trace en trames TPIU synchronisées par FSYNC (option -tpiu).
Construire OpenCSD : tools/build_opencsd.sh (dans .cache/opencsd).
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT = os.path.join(ROOT, ".cache", "opencsd", "decoder")


def lister() -> tuple[str, str] | None:
    """(exécutable trc_pkt_lister, répertoire des bibliothèques) ou None."""
    base = os.environ.get("OPENCSD_DECODER", DEFAULT)
    exe = os.path.join(base, "tests", "bin", "builddir", "trc_pkt_lister")
    libdir = os.path.join(base, "lib", "builddir")
    if os.path.exists(exe):
        return exe, libdir
    found = shutil.which("trc_pkt_lister")
    return (found, "") if found else None


ETMIDR_M4 = 0x4114F250


def make_snapshot(directory: str, image, framed_trace: bytes, trace_id: int = 2,
                  cycle_accurate: bool = False) -> None:
    os.makedirs(directory, exist_ok=True)
    with open(os.path.join(directory, "cstrace.bin"), "wb") as fh:
        fh.write(framed_trace)
    dumps = []
    for i, (base, data) in enumerate(image.segments):
        name = f"mem{i}.bin"
        with open(os.path.join(directory, name), "wb") as fh:
            fh.write(data)
        dumps.append(f"[dump{i + 1}]\nfile={name}\naddress=0x{base:08X}\nlength=0x{len(data):08X}\n")
    files = {
        "snapshot.ini": "[snapshot]\nversion=1.0\n\n[device_list]\ndevice0=cpu_0.ini\n"
                        "device1=etm_0.ini\n\n[trace]\nmetadata=trace.ini\n",
        "cpu_0.ini": "[device]\nname=cpu_0\nclass=core\ntype=Cortex-M4\n\n[regs]\nR15=0x0\n\n"
                     + "\n".join(dumps),
        "etm_0.ini": "[device]\nname=ETM_0\nclass=trace_source\ntype=ETM3.5\n\n[regs]\n"
                     f"ETMCR(0x000)=0x{0x1000 if cycle_accurate else 0:08X}\n"   # bit 12 : cycle-accurate
                     f"ETMIDR(0x079)=0x{ETMIDR_M4:08X}\nETMCCER(0x07A)=0x00000000\n"
                     f"ETMTRACEIDR(0x080)=0x{trace_id:08X}\n",
        "trace.ini": "[trace_buffers]\nbuffers=buffer0\n\n[buffer0]\nname=ETB_0\nfile=cstrace.bin\n"
                     "format=coresight\n\n[source_buffers]\nETM_0=ETB_0\n\n"
                     "[core_trace_sources]\ncpu_0=ETM_0\n",
    }
    for name, text in files.items():
        with open(os.path.join(directory, name), "w") as fh:
            fh.write(text)


_RANGE = re.compile(r"INSTR_RANGE\(exec range=0x([0-9a-f]+):\[0x([0-9a-f]+)\] num_i\((\d+)\)")
_CC = re.compile(r"\[CC=(\d+)\]")
_EXC = re.compile(r"OCSD_GEN_TRC_ELEM_EXCEPTION\(.*excep num \(0x([0-9a-f]+)\)")


def decode(directory: str, image, with_cycles: bool = False) -> tuple[list, list[str]]:
    """Décode avec OpenCSD. Retourne (événements, lignes d'erreur). Événements :
    ("step", adresse) par instruction (plages développées avec l'image), ("exc", numéro),
    ("ret",). `with_cycles` : chaque événement reçoit en plus ses cycles ; les cycles sans
    instruction (éléments CYCLE_COUNT) s'ajoutent à l'événement suivant, comme dans gdbtrace."""
    exe, libdir = lister()
    env = dict(os.environ)
    if libdir:
        env["LD_LIBRARY_PATH"] = libdir + ":" + env.get("LD_LIBRARY_PATH", "")
    out = subprocess.run([exe, "-ss_dir", directory, "-decode_only", "-tpiu", "-logstdout"],
                         capture_output=True, text=True, env=env, timeout=600).stdout
    events, errors = [], []
    pending = 0

    def cyc(line):
        nonlocal pending
        m = _CC.search(line)
        c, pending = pending + (int(m.group(1)) if m else 0), 0
        return (c,) if with_cycles else ()

    for line in out.splitlines():
        m = _RANGE.search(line)
        if m:
            addr, end = int(m.group(1), 16), int(m.group(2), 16)
            first = True
            while addr < end:          # ETMv3 : une plage = une instruction (un atome)
                events.append(("step", addr) + (cyc(line) if first else ((0,) if with_cycles else ())))
                first = False
                addr += image.instr(addr).size
            continue
        if "OCSD_GEN_TRC_ELEM_CYCLE_COUNT" in line:
            m = _CC.search(line)
            pending += int(m.group(1)) if m else 0
            continue
        m = _EXC.search(line)
        if m:
            events.append(("exc", int(m.group(1), 16)) + cyc(line))
        elif "EXCEPTION_RET" in line:
            events.append(("ret",) + cyc(line))
        elif re.search(r"ERROR|ADDR_NACC|ADDR_UNKNOWN", line):
            errors.append(line)
    return events, errors
