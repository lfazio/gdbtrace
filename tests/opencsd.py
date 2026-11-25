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


#: registres d'identification de l'ETM-M7 d'un STM32F746 (lus sur une Nucleo-F746ZG) :
#: ETMv4.0, adresses 32 bits, profondeur de spéculation 1 (TRCIDR8)
ETM_M7_IDR = {"TRCIDR0(0x078)": 0x080006E1, "TRCIDR1(0x079)": 0x4100F400,
              "TRCIDR2(0x07A)": 0x00000004, "TRCIDR8(0x060)": 0x00000001,
              "TRCIDR9(0x061)": 0, "TRCIDR10(0x062)": 0, "TRCIDR11(0x063)": 0,
              "TRCIDR12(0x064)": 0x00000001, "TRCIDR13(0x065)": 0,
              "TRCAUTHSTATUS(0x3EE)": 0x000000C0}


def make_snapshot_etm4(directory: str, image, framed_trace: bytes, trace_id: int = 2,
                       configr: int = 0x1, idr: dict | None = None) -> None:
    """Snapshot d'un ETM-M7 (ETMv4). OpenCSD ne connaît pas le Cortex-M7 : on déclare un
    Cortex-M4, de même architecture (ARMv7-M) et de même profil (M) — seuls ceux-ci comptent
    pour le décodage ; les registres d'identification sont ceux du vrai ETM-M7."""
    make_snapshot(directory, image, framed_trace, trace_id)
    regs = {"TRCCONFIGR(0x004)": configr, "TRCTRACEIDR(0x010)": trace_id, **ETM_M7_IDR, **(idr or {})}
    with open(os.path.join(directory, "etm_0.ini"), "w") as fh:
        fh.write("[device]\nname=ETM_0\nclass=trace_source\ntype=ETM4\n\n[regs]\n"
                 + "".join(f"{k}=0x{v:08X}\n" for k, v in regs.items()))


_RANGE = re.compile(r"INSTR_RANGE\(exec range=0x([0-9a-f]+):\[0x([0-9a-f]+)\] num_i\((\d+)\)")
_CC = re.compile(r"\[CC=(\d+)\]")
_EXC = re.compile(r"OCSD_GEN_TRC_ELEM_EXCEPTION\(.*excep num \(0x([0-9a-f]+)\)")


def decode(directory: str, image, with_cycles: bool = False,
           etm4: bool = False) -> tuple[list, list[str]]:
    """Décode avec OpenCSD. Retourne (événements, lignes d'erreur). Événements :
    ("step", adresse) par instruction (plages développées avec l'image), ("exc", numéro),
    ("ret",). `with_cycles` : chaque événement reçoit en plus ses cycles ; les cycles sans
    instruction (éléments CYCLE_COUNT) s'ajoutent à l'événement suivant, comme dans gdbtrace.
    `etm4` : OpenCSD donne le champ TYPE brut des exceptions ETMv4, converti en numéro."""
    from gdbtrace.trace.etm.etm4 import exception_number
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
            num = int(m.group(1), 16)
            events.append(("exc", exception_number(num) if etm4 else num) + cyc(line))
        elif "EXCEPTION_RET" in line:
            events.append(("ret",) + cyc(line))
        elif re.search(r"ERROR|ADDR_NACC|ADDR_UNKNOWN", line):
            errors.append(line)
    return events, errors


def cycle_segments(directory: str) -> tuple[list[tuple[int, int]], list[str]]:
    """ETMv4 cycle-accurate décodé par OpenCSD : [(instructions depuis le comptage précédent,
    cycles comptés)] dans l'ordre des éléments CYCLE_COUNT, et les lignes d'erreur."""
    exe, libdir = lister()
    env = dict(os.environ)
    if libdir:
        env["LD_LIBRARY_PATH"] = libdir + ":" + env.get("LD_LIBRARY_PATH", "")
    out = subprocess.run([exe, "-ss_dir", directory, "-decode_only", "-tpiu", "-logstdout"],
                         capture_output=True, text=True, env=env, timeout=600).stdout
    segs, errors, n = [], [], 0
    for line in out.splitlines():
        m = _RANGE.search(line)
        if m:
            n += int(m.group(3))
        if "CYCLE_COUNT" in line or (m and "[CC=" in line):
            c = _CC.search(line)
            if c:
                segs.append((n, int(c.group(1))))
                n = 0
        elif re.search(r"ERROR|ADDR_NACC|ADDR_UNKNOWN", line):
            errors.append(line)
    return segs, errors
