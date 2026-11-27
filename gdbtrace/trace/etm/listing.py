"""Trace.List en lignes : ce que montre la vue Trace (onglet Listing), cherché (Trace.Find,
Trace.FindAll) et exporté (CSV), commun à la vue, aux scripts et au serveur MCP."""
from __future__ import annotations

import csv
from dataclasses import dataclass

from .qemu_log import CycleMark, ExcEntry, ExcReturn, TimeMark, TraceGap


@dataclass
class TraceRow:
    index: int                      # position depuis la fin (-1 : la dernière)
    kind: str                       # instruction, exception, retour, interruption, horodatage
    addr: int | None = None
    func: str = ""
    text: str = ""                  # instruction désassemblée, ou libellé
    file: str | None = None
    line: int | None = None
    number: int | None = None       # numéro d'exception

    def as_dict(self) -> dict:
        return {"index": self.index, "type": self.kind, "adresse": self.addr, "fonction": self.func,
                "instruction": self.text, "fichier": self.file, "ligne": self.line}


def listing_rows(events, image, symbols, offset: int = 0) -> list[TraceRow]:
    """Lignes du listing pour ces événements (marques de cycles omises). `offset` : nombre
    d'événements plus récents, non listés (fenêtre plus ancienne de la trace)."""
    from ..itm import exception_name
    rows: list[TraceRow] = []
    total = len(events) + offset
    for i, e in enumerate(events):
        idx = i - total
        if isinstance(e, ExcEntry):
            rows.append(TraceRow(idx, "exception", e.handler, "", f"exception {exception_name(e.number)} "
                                 f"({e.number})", number=e.number))
        elif isinstance(e, ExcReturn):
            rows.append(TraceRow(idx, "retour", None, "", "retour d'exception"))
        elif isinstance(e, TraceGap):
            rows.append(TraceRow(idx, "interruption", None, "", "trace interrompue"))
        elif isinstance(e, TimeMark):
            rows.append(TraceRow(idx, "horodatage", None, "", f"horodatage {e.value}"))
        elif isinstance(e, CycleMark):
            continue
        else:
            ins = image.instr(e.addr) if image is not None else None
            f = symbols.function_at(e.addr) if symbols else None
            le = symbols.line_at(e.addr) if symbols and f else None
            rows.append(TraceRow(idx, "instruction", e.addr, f.name if f else "",
                                 ins.text if ins else "?", le.file if le else None, le.line if le else None))
    return rows


def find_rows(rows: list[TraceRow], query: str) -> list[int]:
    """Positions (dans `rows`) qui répondent à la recherche :
    - une adresse (`0x8000120`, `8000120`) : les instructions à cette adresse ;
    - un nom de fonction exact : chaque **entrée** dans la fonction ;
    - `exception` (ou `exc`), un nom ou un numéro d'exception (`SysTick`, `exc 15`) : les
      entrées d'exception ;
    - sinon : le texte dans l'instruction, la fonction ou le fichier (sans casse)."""
    q = query.strip()
    if not q:
        return []
    low = q.lower()
    try:
        addr = int(q, 16) if not low.startswith("0x") else int(q, 0)
        hexlike = all(c in "0123456789abcdefx" for c in low)
    except ValueError:
        addr, hexlike = None, False
    if hexlike and addr is not None:
        hits = [i for i, r in enumerate(rows) if r.addr == addr and r.kind == "instruction"]
        if hits:
            return hits
    words = low.split()
    if words and words[0] in ("exception", "exc", "exceptions"):
        rest = " ".join(words[1:])
        return [i for i, r in enumerate(rows) if r.kind == "exception"
                and (not rest or rest == str(r.number) or rest in r.text.lower())]
    if any(r.func == q for r in rows):
        return [i for i, r in enumerate(rows)
                if r.func == q and (i == 0 or rows[i - 1].func != q)]
    exc = [i for i, r in enumerate(rows) if r.kind == "exception" and low in r.text.lower()]
    if exc:
        return exc
    return [i for i, r in enumerate(rows)
            if low in r.text.lower() or low in r.func.lower() or (r.file and low in r.file.lower())]


def export_csv(rows: list[TraceRow], path: str) -> int:
    """Écrit les lignes en CSV (séparateur « ; », UTF-8) ; rend leur nombre."""
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh, delimiter=";")
        w.writerow(["index", "type", "adresse", "fonction", "instruction", "fichier", "ligne"])
        for r in rows:
            w.writerow([r.index, r.kind, f"{r.addr:#010x}" if r.addr is not None else "", r.func,
                        r.text, r.file or "", r.line or ""])
    return len(rows)
