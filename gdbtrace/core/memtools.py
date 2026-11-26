"""Outils mémoire (Data.SAVE, Data.LOAD, Data.Set, Data.Find, Data.ComPare) et images de
programme.

Formats d'image, d'après l'extension :
- Intel HEX (`.hex`, `.ihex`) et Motorola S-record (`.srec`, `.s19`, `.s28`, `.s37`, `.mot`) :
  les adresses sont dans le fichier ;
- binaire brut (`.bin`) : l'adresse est donnée à part (`FICHIER.bin@0x08000000`) ;
- ELF (`.elf`, `.axf`, `.out`) : les segments chargés, à leur adresse de chargement (LMA).

Tout passe par deux fonctions de la session, `read(addr, n)` et `write(addr, data)` : cible
arrêtée par gdb, ou canal mémoire en direct pour la lecture. Les écritures vont en RAM (ou
dans des registres) ; la flash se programme par le serveur gdb (`Session.program`).
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Callable

Read = Callable[[int, int], bytes]
Write = Callable[[int, bytes], None]

HEX_EXT = (".hex", ".ihex", ".ihx")
SREC_EXT = (".srec", ".s19", ".s28", ".s37", ".mot", ".s")
BIN_EXT = (".bin", ".raw")
ELF_EXT = (".elf", ".axf", ".out")
CHUNK = 4096


class ImageError(ValueError):
    pass


def split_address(spec: str) -> tuple[str, int | None]:
    """`chemin@0x08000000` → (chemin, adresse) ; sans `@`, adresse None."""
    path, sep, addr = spec.rpartition("@")
    if sep and path and not os.path.exists(spec):
        try:
            return path, int(addr, 0)
        except ValueError:
            pass
    return spec, None


def image_format(path: str) -> str:
    ext = os.path.splitext(path)[1].lower()
    if ext in HEX_EXT:
        return "ihex"
    if ext in SREC_EXT:
        return "srec"
    if ext in BIN_EXT:
        return "bin"
    if ext in ELF_EXT:
        return "elf"
    try:                                        # sans extension connue : l'en-tête décide
        with open(path, "rb") as fh:
            head = fh.read(4)
    except OSError:
        return "bin"
    if head == b"\x7fELF":
        return "elf"
    if head[:1] == b":":
        return "ihex"
    if head[:1] == b"S" and head[1:2].isdigit():
        return "srec"
    return "bin"


def is_image(path: str) -> bool:
    """Image de programme qui n'est pas un ELF (HEX, S-record, binaire)."""
    return image_format(split_address(path)[0]) != "elf"


def merge_segments(chunks: list[tuple[int, bytes]]) -> list[tuple[int, bytes]]:
    """Segments triés, les contigus réunis."""
    out: list[tuple[int, bytearray]] = []
    for addr, data in sorted(chunks):
        if out and out[-1][0] + len(out[-1][1]) == addr:
            out[-1][1].extend(data)
        else:
            out.append((addr, bytearray(data)))
    return [(a, bytes(d)) for a, d in out]


# ---------------------------------------------------------------------- Intel HEX
def parse_ihex(text: str) -> list[tuple[int, bytes]]:
    chunks: list[tuple[int, bytes]] = []
    upper = 0
    for n, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        if not line.startswith(":"):
            raise ImageError(f"Intel HEX ligne {n} : « : » attendu")
        try:
            raw = bytes.fromhex(line[1:])
        except ValueError:
            raise ImageError(f"Intel HEX ligne {n} : chiffres hexadécimaux attendus") from None
        if len(raw) < 5 or len(raw) != raw[0] + 5:
            raise ImageError(f"Intel HEX ligne {n} : longueur incohérente")
        if sum(raw) & 0xFF:
            raise ImageError(f"Intel HEX ligne {n} : somme de contrôle fausse")
        count, offset, rtype, data = raw[0], int.from_bytes(raw[1:3], "big"), raw[3], raw[4:4 + raw[0]]
        if rtype == 0:
            chunks.append((upper + offset, data))
        elif rtype == 1:
            break
        elif rtype == 2:
            upper = int.from_bytes(data, "big") << 4
        elif rtype == 4:
            upper = int.from_bytes(data, "big") << 16
        elif rtype in (3, 5):                   # adresse de départ : sans objet ici
            continue
        else:
            raise ImageError(f"Intel HEX ligne {n} : enregistrement de type {rtype} inconnu")
    return merge_segments(chunks)


def format_ihex(segments: list[tuple[int, bytes]], width: int = 16) -> str:
    lines = []
    upper = None

    def rec(offset: int, rtype: int, data: bytes) -> str:
        raw = bytes([len(data)]) + offset.to_bytes(2, "big") + bytes([rtype]) + data
        return ":" + (raw + bytes([-sum(raw) & 0xFF])).hex().upper()
    for base, data in segments:
        pos = 0
        while pos < len(data):
            addr = base + pos
            if addr >> 16 != upper:
                upper = addr >> 16
                lines.append(rec(0, 4, upper.to_bytes(2, "big")))
            n = min(width, len(data) - pos, 0x10000 - (addr & 0xFFFF))
            lines.append(rec(addr & 0xFFFF, 0, data[pos:pos + n]))
            pos += n
    lines.append(":00000001FF")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------- S-record
def parse_srec(text: str) -> list[tuple[int, bytes]]:
    chunks: list[tuple[int, bytes]] = []
    for n, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        if len(line) < 4 or line[0] != "S":
            raise ImageError(f"S-record ligne {n} : « S » attendu")
        kind = line[1]
        try:
            raw = bytes.fromhex(line[2:])
        except ValueError:
            raise ImageError(f"S-record ligne {n} : chiffres hexadécimaux attendus") from None
        if not raw or len(raw) != raw[0] + 1:
            raise ImageError(f"S-record ligne {n} : longueur incohérente")
        if (sum(raw) & 0xFF) != 0xFF:
            raise ImageError(f"S-record ligne {n} : somme de contrôle fausse")
        width = {"1": 2, "2": 3, "3": 4}.get(kind)
        if width is not None:
            addr = int.from_bytes(raw[1:1 + width], "big")
            chunks.append((addr, raw[1 + width:-1]))
        elif kind not in "056789":
            raise ImageError(f"S-record ligne {n} : type S{kind} inconnu")
    return merge_segments(chunks)


def format_srec(segments: list[tuple[int, bytes]], width: int = 16) -> str:
    def rec(kind: str, addr: int, data: bytes) -> str:
        aw = {"0": 2, "3": 4, "7": 4}[kind]
        body = addr.to_bytes(aw, "big") + data
        raw = bytes([len(body) + 1]) + body
        return f"S{kind}" + (raw + bytes([~sum(raw) & 0xFF])).hex().upper()
    lines = [rec("0", 0, b"gdbtrace")]
    for base, data in segments:
        for pos in range(0, len(data), width):
            lines.append(rec("3", base + pos, data[pos:pos + width]))
    lines.append(rec("7", 0, b""))
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------- images
def read_image(spec: str, address: int | None = None) -> list[tuple[int, bytes]]:
    """Segments [(adresse, octets)] d'une image. `spec` peut porter l'adresse d'un binaire
    (`fw.bin@0x08000000`) ; `address` la remplace."""
    path, at = split_address(spec)
    if address is not None:
        at = address
    fmt = image_format(path)
    if fmt == "elf":
        from elftools.elf.elffile import ELFFile
        with open(path, "rb") as fh:
            elf = ELFFile(fh)
            segs = [(g["p_paddr"], g.data()) for g in elf.iter_segments()
                    if g["p_type"] == "PT_LOAD" and g["p_filesz"]]
        if at is not None and segs:                 # ELF relogé : décalé en bloc
            delta = at - min(a for a, _ in segs)
            segs = [(a + delta, d) for a, d in segs]
        return merge_segments(segs)
    if fmt == "bin":
        if at is None:
            raise ImageError(f"{os.path.basename(path)} : binaire brut, adresse à donner "
                             "(FICHIER.bin@0x08000000)")
        with open(path, "rb") as fh:
            return [(at, fh.read())]
    with open(path, encoding="ascii", errors="replace") as fh:
        text = fh.read()
    segs = parse_ihex(text) if fmt == "ihex" else parse_srec(text)
    if at is not None and segs:
        delta = at - segs[0][0]
        segs = [(a + delta, d) for a, d in segs]
    return segs


def write_image(path: str, segments: list[tuple[int, bytes]]) -> str:
    """Écrit les segments au format de l'extension ; rend le format."""
    fmt = image_format(path) if os.path.splitext(path)[1] else "bin"
    if fmt == "elf":
        raise ImageError("enregistrement en ELF non pris en charge : .bin, .hex ou .srec")
    if fmt == "bin":
        if len(segments) != 1:
            raise ImageError("plusieurs segments : un binaire brut n'a pas d'adresses (.hex ou .srec)")
        with open(path, "wb") as fh:
            fh.write(segments[0][1])
    else:
        with open(path, "w", encoding="ascii") as fh:
            fh.write(format_ihex(segments) if fmt == "ihex" else format_srec(segments))
    return fmt


def read_range(read: Read, addr: int, n: int) -> bytes:
    out = bytearray()
    while len(out) < n:
        k = min(CHUNK, n - len(out))
        out += read(addr + len(out), k)
    return bytes(out)


def save(read: Read, addr: int, n: int, path: str) -> str:
    """Data.SAVE : `n` octets à `addr` dans `path` (.bin, .hex, .srec) ; rend le format."""
    return write_image(path, [(addr, read_range(read, addr, n))])


def load(write: Write, spec: str, address: int | None = None) -> list[tuple[int, int]]:
    """Data.LOAD : écrit l'image en mémoire (RAM) ; rend [(adresse, taille)] des segments."""
    segs = read_image(spec, address)
    for base, data in segs:
        for pos in range(0, len(data), CHUNK):
            write(base + pos, data[pos:pos + CHUNK])
    return [(a, len(d)) for a, d in segs]


def to_pattern(value) -> bytes:
    """Motif d'octets : bytes, texte (UTF-8), entier (petit-boutiste, taille minimale 1, 2
    ou 4 octets), liste d'octets."""
    if isinstance(value, (bytes, bytearray)):
        return bytes(value)
    if isinstance(value, str):
        return value.encode("utf-8")
    if isinstance(value, int):
        size = 1 if 0 <= value <= 0xFF else 2 if 0 <= value <= 0xFFFF else 4
        return (value & ((1 << 8 * size) - 1)).to_bytes(size, "little")
    if isinstance(value, (list, tuple)):
        return bytes(value)
    raise ImageError(f"motif non reconnu : {value!r}")


def fill(write: Write, addr: int, n: int, pattern) -> None:
    """Data.Set : `n` octets à `addr` remplis du motif répété."""
    pat = to_pattern(pattern)
    if not pat:
        raise ImageError("motif vide")
    block = (pat * (CHUNK // len(pat) + 1))[:CHUNK]
    for pos in range(0, n, CHUNK):
        k = min(CHUNK, n - pos)
        off = pos % len(pat)
        write(addr + pos, (block[off:] + block[:off])[:k] if off else block[:k])


def find(read: Read, addr: int, n: int, pattern, limit: int = 100, align: int = 1) -> list[int]:
    """Data.Find : adresses où le motif apparaît dans [addr, addr+n), au plus `limit`.
    `align` : ne garder que les adresses multiples de cet alignement."""
    pat = to_pattern(pattern)
    if not pat:
        raise ImageError("motif vide")
    found: list[int] = []
    keep = b""
    pos = 0
    while pos < n and len(found) < limit:
        k = min(CHUNK * 16, n - pos)
        data = keep + read(addr + pos, k)
        start = addr + pos - len(keep)
        i = data.find(pat)
        while i >= 0 and len(found) < limit:
            a = start + i
            if a % align == 0 and (not found or a > found[-1]):
                found.append(a)
            i = data.find(pat, i + 1)
        keep = data[-(len(pat) - 1):] if len(pat) > 1 else b""
        pos += k
    return found


@dataclass
class Comparison:
    """Data.ComPare : différences entre une image et la mémoire."""
    total: int = 0                                         # octets comparés
    differing: int = 0                                     # octets différents
    first: list[tuple[int, int, int]] = field(default_factory=list)   # (adresse, image, mémoire)
    segments: list[tuple[int, int]] = field(default_factory=list)

    @property
    def same(self) -> bool:
        return self.differing == 0

    def summary(self) -> str:
        if self.same:
            return f"identique : {self.total} octets en {len(self.segments)} segment(s)"
        out = f"{self.differing} octet(s) différent(s) sur {self.total}"
        if self.first:
            a, want, got = self.first[0]
            out += f" ; premier à {a:#x} : image {want:#04x}, mémoire {got:#04x}"
        return out


def compare(read: Read, spec: str, address: int | None = None, keep: int = 32) -> Comparison:
    res = Comparison()
    for base, data in read_image(spec, address):
        res.segments.append((base, len(data)))
        got = read_range(read, base, len(data))
        res.total += len(data)
        if got == data:
            continue
        for i, (want, have) in enumerate(zip(data, got)):
            if want != have:
                res.differing += 1
                if len(res.first) < keep:
                    res.first.append((base + i, want, have))
    return res
