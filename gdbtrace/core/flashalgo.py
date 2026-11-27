"""Chargeur de flash générique : un algorithme de programmation exécuté sur la cible par gdb,
quel que soit le serveur (QEMU, OpenOCD, ST-LINK_gdbserver, un gdbserver sans pilote de flash).

Deux formats d'algorithme, tous deux des ELF :

- **FLM** (CMSIS-Pack, Keil) : code indépendant de sa position (sections `PrgCode`, `PrgData`
  et leur zone à zéro), la description `FlashDevice` (section `DevDscr` : nom, adresse, taille,
  page, valeur effacée, délais, secteurs) et les fonctions Init(adr, clk, fnc), UnInit(fnc),
  EraseChip(), EraseSector(adr), ProgramPage(adr, sz, buf), et en option Verify(adr, sz, buf)
  et BlankCheck(adr, sz, pat). Code et données sont copiés en RAM ; r9 (base statique) pointe
  sur les données. Retour 0 : réussite.
- **chargeur externe de STM32CubeProgrammer** (`.stldr`) : lié à son adresse en RAM,
  description `StorageInfo` (nom, type, début, taille, page, valeur effacée, secteurs) et
  fonctions Init(), Write(adr, sz, buf), SectorErase(début, fin), et en option MassErase(),
  Read(adr, sz, buf), Verify(…). Retour 1 : réussite.

Chaque appel (Session.target_calls) : arguments dans r0-r3, sp sur une pile en RAM, lr sur
une instruction BKPT de retour (où gdb pose aussi un breakpoint matériel), pc sur la fonction ;
la cible tourne jusqu'au retour, le résultat est r0. Sans événement ni resume hook : la
programmation ne passe ni par le chrono, ni par les compteurs, ni par la trace simulée.

Disposition en RAM (FLM) : retour (4 octets), code, données, zéros, pile, puis un tampon d'une
page. Un `.stldr` reste à ses adresses ; retour, pile et tampon suivent son image, dans la RAM
donnée (`--flash-ram`) ou juste après elle.
"""
from __future__ import annotations

import os
import struct
import time
from dataclasses import dataclass, field

BKPT_RETURN = struct.pack("<HH", 0xBE00, 0xBE00)       # deux BKPT #0 : retour des fonctions
STACK_SIZE = 0x400
FLM_FUNCS = ("Init", "UnInit", "EraseChip", "EraseSector", "ProgramPage", "Verify", "BlankCheck")
STLDR_FUNCS = ("Init", "Write", "SectorErase", "MassErase", "Read", "Verify")
#: types de StorageInfo.DeviceType (Dev_Inf.h de ST)
STLDR_TYPES = {1: "flash de la puce", 2: "NAND", 3: "NOR", 4: "SRAM", 5: "PSRAM", 6: "PC card",
               7: "flash SPI", 8: "flash I2C", 9: "SDRAM", 10: "EEPROM I2C"}


class FlashAlgoError(RuntimeError):
    """Algorithme illisible, mal placé, ou qui a échoué sur la cible."""


@dataclass
class FlashDevice:
    """Mémoire que l'algorithme programme : plage, page, secteurs."""
    name: str
    start: int
    size: int
    page: int
    erased: int = 0xFF
    sectors: list[tuple[int, int]] = field(default_factory=list)   # (adresse, taille) de chaque bloc de secteurs égaux
    prog_timeout: float = 1.0        # secondes
    erase_timeout: float = 5.0
    kind: str = ""

    @property
    def end(self) -> int:
        return self.start + self.size

    def covers(self, addr: int, n: int = 1) -> bool:
        return self.start <= addr and addr + n <= self.end

    def sector_list(self) -> list[tuple[int, int]]:
        """Tous les secteurs (adresse, taille), dans l'ordre."""
        out = []
        for i, (base, size) in enumerate(self.sectors):
            stop = self.sectors[i + 1][0] if i + 1 < len(self.sectors) else self.end
            a = base
            while a < stop and size:
                out.append((a, size))
                a += size
        return out or [(self.start, self.size)]

    def sectors_for(self, lo: int, hi: int) -> list[tuple[int, int]]:
        """Secteurs qui recouvrent [lo, hi)."""
        return [(a, n) for a, n in self.sector_list() if a < hi and lo < a + n]


def _cstr(raw: bytes) -> str:
    return raw.split(b"\0", 1)[0].decode("latin-1", "replace").strip()


def _symbols(elf) -> dict[str, int]:
    tab = elf.get_section_by_name(".symtab")
    if tab is None:
        raise FlashAlgoError("algorithme sans table de symboles")
    out = {}
    for sym in tab.iter_symbols():
        if sym.name and sym["st_shndx"] != "SHN_UNDEF":
            out.setdefault(sym.name, sym["st_value"])
    return out


def _read_symbol(elf, value: int, size: int) -> bytes:
    """Octets d'un objet de l'ELF à l'adresse `value`, cherchés dans ses sections."""
    for sec in elf.iter_sections():
        addr, n = sec["sh_addr"], sec["sh_size"]
        if sec["sh_type"] == "SHT_PROGBITS" and addr <= value and value + size <= addr + n:
            data = sec.data()
            return data[value - addr: value - addr + size]
    raise FlashAlgoError(f"description introuvable à {value:#x}")


@dataclass
class Placement:
    """Où l'algorithme est en RAM et comment l'appeler."""
    image: list[tuple[int, bytes]]   # (adresse, octets) à écrire
    funcs: dict[str, int]            # nom → adresse (bit Thumb compris)
    return_addr: int
    sp: int
    static_base: int | None
    buffer: int
    buffer_size: int


class FlmAlgo:
    """Algorithme FLM (CMSIS-Pack)."""
    kind = "FLM"
    ok = 0                            # code de réussite

    def __init__(self, path: str):
        from elftools.elf.elffile import ELFFile
        self.path = path
        with open(path, "rb") as fh:
            elf = ELFFile(fh)
            syms = _symbols(elf)
            ro = rw = zi = None
            for sec in elf.iter_sections():
                if sec.name == "PrgCode" and sec["sh_type"] == "SHT_PROGBITS":
                    ro = (sec["sh_addr"], sec.data())
                elif sec.name == "PrgData" and sec["sh_type"] == "SHT_PROGBITS":
                    rw = (sec["sh_addr"], sec.data())
                elif sec.name == "PrgData" and sec["sh_type"] == "SHT_NOBITS":
                    zi = (sec["sh_addr"], sec["sh_size"])
            if ro is None:
                raise FlashAlgoError(f"{os.path.basename(path)} : pas de section PrgCode (pas un FLM)")
            if "FlashDevice" not in syms:
                raise FlashAlgoError(f"{os.path.basename(path)} : pas de FlashDevice")
            raw = _read_symbol(elf, syms["FlashDevice"], 160)
            sectors_raw = b""
            try:
                sectors_raw = _read_symbol(elf, syms["FlashDevice"] + 160, 8 * 512)
            except FlashAlgoError:           # table plus courte : lue jusqu'au marqueur de fin
                for count in (256, 64, 16, 4, 1):
                    try:
                        sectors_raw = _read_symbol(elf, syms["FlashDevice"] + 160, 8 * count)
                        break
                    except FlashAlgoError:
                        continue
        if ro[0] != 0:
            raise FlashAlgoError("FLM : PrgCode doit commencer à 0 (code indépendant de sa position)")
        code = bytearray(ro[1])
        data_at = len(code)
        if rw is not None:
            if rw[0] < len(code):
                raise FlashAlgoError("FLM : PrgData recouvre PrgCode")
            code += b"\0" * (rw[0] - len(code)) + rw[1]
            data_at = rw[0]
        if zi is not None:
            if zi[0] < len(code):
                raise FlashAlgoError("FLM : zone à zéro recouvrant le code")
            code += b"\0" * (zi[0] - len(code) + zi[1])
        self.blob = bytes(code)
        self.data_offset = data_at       # base statique = début des données
        self.funcs = {n: syms[n] for n in FLM_FUNCS if n in syms}
        for need in ("Init", "UnInit", "EraseSector", "ProgramPage"):
            if need not in self.funcs:
                raise FlashAlgoError(f"FLM : fonction {need} absente")
        vers, name = struct.unpack_from("<H128s", raw, 0)
        devtype, adr, size, page, _res, empty = struct.unpack_from("<HIIIIB", raw, 130)
        to_prog, to_erase = struct.unpack_from("<II", raw, 152)
        sectors = []
        for off in range(0, len(sectors_raw) - 7, 8):
            sz, at = struct.unpack_from("<II", sectors_raw, off)
            if sz == 0xFFFFFFFF and at == 0xFFFFFFFF:
                break
            sectors.append((adr + at, sz))
        self.version = vers
        self.device = FlashDevice(_cstr(name), adr, size, page, empty, sectors,
                                  max(to_prog, 100) / 1000, max(to_erase, 500) / 1000,
                                  kind={1: "flash de la puce", 2: "externe 8 bits", 3: "externe 16 bits",
                                        4: "externe 32 bits", 5: "SPI"}.get(devtype, f"type {devtype}"))

    def place(self, ram: tuple[int, int]) -> Placement:
        start, size = ram
        load = (start + 3) & ~3
        code_at = load + len(BKPT_RETURN)
        top = (code_at + len(self.blob) + 7) & ~7
        sp = top + STACK_SIZE
        buffer = (sp + 3) & ~3
        page = self.device.page
        if buffer + page > start + size:
            need = buffer + page - start
            raise FlashAlgoError(f"RAM trop petite pour {os.path.basename(self.path)} : {need} octets "
                                 f"nécessaires, {size} donnés (--flash-ram)")
        funcs = {n: (code_at + off) | 1 for n, off in self.funcs.items()}
        return Placement([(load, BKPT_RETURN + self.blob)], funcs, load, sp, code_at + self.data_offset,
                         buffer, page)

    def describe(self) -> str:
        d = self.device
        return (f"{os.path.basename(self.path)} (FLM) : {d.name}, {d.start:#010x}+{d.size:#x}, "
                f"page {d.page} o, {len(d.sector_list())} secteurs")


class StldrAlgo:
    """Chargeur externe de STM32CubeProgrammer (`.stldr`)."""
    kind = "stldr"
    ok = 1

    def __init__(self, path: str):
        from elftools.elf.elffile import ELFFile
        self.path = path
        with open(path, "rb") as fh:
            elf = ELFFile(fh)
            syms = _symbols(elf)
            if "StorageInfo" not in syms:
                raise FlashAlgoError(f"{os.path.basename(path)} : pas de StorageInfo (pas un .stldr)")
            raw = _read_symbol(elf, syms["StorageInfo"], 120)
            sectors_raw = b""
            for count in (10, 4, 1):
                try:
                    sectors_raw = _read_symbol(elf, syms["StorageInfo"] + 120, 8 * count)
                    break
                except FlashAlgoError:
                    continue
            self.segments = []
            self.zero = []
            for seg in elf.iter_segments():
                if seg["p_type"] != "PT_LOAD" or not seg["p_memsz"]:
                    continue
                if seg["p_flags"] & 1 == 0 and seg["p_filesz"] == 0 and seg["p_memsz"] == 0:
                    continue
                data = seg.data()
                if data:
                    self.segments.append((seg["p_paddr"], data))
                if seg["p_memsz"] > seg["p_filesz"]:
                    self.zero.append((seg["p_vaddr"] + seg["p_filesz"], seg["p_memsz"] - seg["p_filesz"]))
            dev_info = elf.get_section_by_name(".Dev_info")
            if dev_info is not None:                # description : jamais chargée en RAM
                lo, hi = dev_info["sh_addr"], dev_info["sh_addr"] + dev_info["sh_size"]
                self.segments = [(a, d) for a, d in self.segments if not (lo <= a < hi)]
        self.funcs = {n: syms[n] | 1 for n in STLDR_FUNCS if n in syms}
        for need in ("Init", "Write", "SectorErase"):
            if need not in self.funcs:
                raise FlashAlgoError(f".stldr : fonction {need} absente")
        name = raw[:100]
        devtype, = struct.unpack_from("<H", raw, 100)
        start, size, page = struct.unpack_from("<III", raw, 104)
        empty = raw[116]
        sectors, at = [], start
        for off in range(0, len(sectors_raw) - 7, 8):
            num, sz = struct.unpack_from("<II", sectors_raw, off)
            if num == 0 or sz == 0:
                break
            sectors.append((at, sz))
            at += num * sz
        self.device = FlashDevice(_cstr(name), start, size, page or 256, empty, sectors, 2.0, 60.0,
                                  kind=STLDR_TYPES.get(devtype, f"type {devtype}"))

    @property
    def image_end(self) -> int:
        ends = [a + len(d) for a, d in self.segments] + [a + n for a, n in self.zero]
        return max(ends) if ends else 0

    def place(self, ram: tuple[int, int] | None) -> Placement:
        lo = (self.image_end + 7) & ~7
        if ram is not None:
            start, size = ram
            lo = max(lo, start)
            limit = start + size
        else:
            limit = None
        sp = lo + len(BKPT_RETURN) + STACK_SIZE
        sp = (sp + 7) & ~7
        buffer = sp
        page = min(self.device.page, 0x1000)
        if limit is not None and buffer + page > limit:
            raise FlashAlgoError(f"RAM trop petite après l'image du chargeur ({os.path.basename(self.path)}) "
                                 f": --flash-ram plus grande")
        image = list(self.segments) + [(a, b"\0" * n) for a, n in self.zero] + [(lo, BKPT_RETURN)]
        return Placement(image, dict(self.funcs), lo, sp, None, buffer, page)

    def describe(self) -> str:
        d = self.device
        return (f"{os.path.basename(self.path)} (chargeur externe) : {d.name} ({d.kind}), "
                f"{d.start:#010x}+{d.size:#x}, page {d.page} o")


def load_algo(path: str):
    """FLM ou .stldr d'après le contenu (FlashDevice ou StorageInfo)."""
    if not os.path.isfile(path):
        raise FlashAlgoError(f"algorithme introuvable : {path}")
    try:
        return FlmAlgo(path)
    except FlashAlgoError as first:
        try:
            return StldrAlgo(path)
        except FlashAlgoError:
            raise first from None


def find_external_loader(name: str) -> str | None:
    """Chargeur externe donné par son seul nom : cherché dans `ExternalLoader` de
    STM32CubeProgrammer (ou `GDBTRACE_LOADER_DIR`)."""
    dirs = []
    if os.environ.get("GDBTRACE_LOADER_DIR"):
        dirs.append(os.environ["GDBTRACE_LOADER_DIR"])
    from .target import find_cubeprogrammer
    cp = find_cubeprogrammer()
    if cp:
        dirs.append(os.path.join(cp, "ExternalLoader"))
    for d in dirs:
        p = os.path.join(d, name)
        if os.path.isfile(p):
            return p
    return None


def parse_ram(spec: str) -> tuple[int, int]:
    """« 0x20000000:0x4000 » ou « 0x20000000+16K » → (adresse, taille)."""
    text = spec.replace("+", ":")
    addr, _, size = text.partition(":")
    if not size:
        raise ValueError(f"RAM de l'algorithme : ADRESSE:TAILLE attendu, pas « {spec} »")
    mult = 1
    s = size.strip().upper()
    if s.endswith("K"):
        mult, s = 1024, s[:-1]
    elif s.endswith("M"):
        mult, s = 1 << 20, s[:-1]
    return int(addr, 0), int(s, 0) * mult


# ====================================================================== exécution
def _merge(segments: list[tuple[int, bytes]]) -> list[tuple[int, bytes]]:
    from .memtools import merge_segments
    return merge_segments(sorted(segments))


class FlashLoader:
    """Un algorithme prêt à servir une session : placement en RAM, effacement, programmation,
    vérification. `ram` : (adresse, taille) de la RAM de l'algorithme (FLM : obligatoire) ;
    `clock` : fréquence passée à Init (FLM)."""

    def __init__(self, session, algo, ram: tuple[int, int] | None = None, clock: int = 0):
        self.session, self.algo, self.ram, self.clock = session, algo, ram, clock
        if algo.kind == "FLM" and ram is None:
            raise FlashAlgoError(f"{os.path.basename(algo.path)} : RAM de l'algorithme à donner "
                                 "(--flash-ram ADRESSE:TAILLE, ou un CMSIS-Pack)")
        self.placement = algo.place(ram)     # vérifie la taille dès maintenant
        self.calls = 0

    @property
    def device(self) -> FlashDevice:
        return self.algo.device

    def describe(self) -> str:
        return self.algo.describe()

    # ------------------------------------------------------------------ appels
    def _install(self) -> None:
        s = self.session
        for addr, data in self.placement.image:
            for off in range(0, len(data), 0x4000):
                s.write_memory(addr + off, data[off:off + 0x4000])

    def _call(self, call, name: str, *args, timeout: float = 5.0, check: bool = True) -> int:
        fn = self.placement.funcs.get(name)
        if fn is None:
            raise FlashAlgoError(f"{name} absente de l'algorithme")
        r0 = call(fn, args, timeout=timeout)
        self.calls += 1
        if check and r0 != self.algo.ok:
            raise FlashAlgoError(f"{name}({', '.join(hex(a) for a in args)}) a échoué : r0 = {r0:#x}")
        return r0

    def _init(self, call, fnc: int) -> None:
        if self.algo.kind == "FLM":
            self._call(call, "Init", self.device.start, self.clock, fnc)
        else:
            self._call(call, "Init")

    def _uninit(self, call, fnc: int) -> None:
        if self.algo.kind == "FLM":
            self._call(call, "UnInit", fnc)

    def _session_calls(self):
        p = self.placement
        return self.session.target_calls(p.return_addr, p.sp, p.static_base)

    # ------------------------------------------------------------------ opérations
    def erase(self, lo: int | None = None, hi: int | None = None) -> int:
        """Efface les secteurs de [lo, hi) (toute la mémoire sans argument) ; rend leur nombre."""
        d = self.device
        lo = d.start if lo is None else lo
        hi = d.end if hi is None else hi
        sectors = d.sectors_for(lo, hi)
        self._install()
        with self._session_calls() as call:
            self._init(call, 1)
            if lo <= d.start and hi >= d.end and ("EraseChip" in self.placement.funcs
                                                   or "MassErase" in self.placement.funcs):
                name = "EraseChip" if self.algo.kind == "FLM" else "MassErase"
                self._call(call, name, timeout=max(30.0, d.erase_timeout * len(sectors)))
            elif self.algo.kind == "FLM":
                for a, _n in sectors:
                    self._call(call, "EraseSector", a, timeout=d.erase_timeout + 1)
            else:
                first, last = sectors[0][0], sectors[-1][0]
                self._call(call, "SectorErase", first, last,
                           timeout=d.erase_timeout * len(sectors) + 5)
            self._uninit(call, 1)
        return len(sectors)

    def program(self, segments: list[tuple[int, bytes]], progress=None) -> dict:
        """Efface les secteurs touchés puis écrit les segments page par page (complétés par la
        valeur effacée) ; rend le bilan (octets, secteurs, pages, secondes)."""
        d = self.device
        segs = [(a, bytes(b)) for a, b in _merge(segments) if b]
        for a, b in segs:
            if not d.covers(a, len(b)):
                raise FlashAlgoError(f"{a:#x}+{len(b):#x} hors de {d.name} ({d.start:#x}-{d.end:#x})")
        t0 = time.monotonic()
        sectors = sorted({s for a, b in segs for s in d.sectors_for(a, a + len(b))})
        pages = 0
        self._install()
        p = self.placement
        with self._session_calls() as call:
            self._init(call, 1)
            if self.algo.kind == "FLM":
                for a, _n in sectors:
                    self._call(call, "EraseSector", a, timeout=d.erase_timeout + 1)
            else:
                for a, _n in sectors:
                    self._call(call, "SectorErase", a, a, timeout=d.erase_timeout + 1)
            self._uninit(call, 1)
            self._init(call, 2)
            page = min(d.page, p.buffer_size)
            flm = self.algo.kind == "FLM"
            for a, b in segs:
                pos, end = a, a + len(b)
                while pos < end:
                    if flm:                      # page entière, complétée par la valeur effacée
                        base = pos - (pos - d.start) % page
                        stop = min(base + page, end)
                        chunk = bytes([d.erased]) * (pos - base) + b[pos - a:stop - a]
                        if base + page <= d.end:
                            chunk += bytes([d.erased]) * (page - len(chunk))
                    else:                        # Write accepte toute taille
                        base, stop = pos, min(pos + page, end)
                        chunk = b[pos - a:stop - a]
                    self.session.write_memory(p.buffer, chunk)
                    if flm:
                        self._call(call, "ProgramPage", base, len(chunk), p.buffer,
                                   timeout=d.prog_timeout + 1)
                    else:
                        self._call(call, "Write", base, len(chunk), p.buffer, timeout=d.prog_timeout + 1)
                    pages += 1
                    pos = stop
                    if progress is not None:
                        progress(pos - a, len(b))
            self._uninit(call, 2)
        return {"octets": sum(len(b) for _a, b in segs), "secteurs": len(sectors), "pages": pages,
                "secondes": time.monotonic() - t0}

    def verify(self, segments: list[tuple[int, bytes]]) -> list[str]:
        """Différences entre les segments et la mémoire : fonction Verify d'un FLM, Read d'un
        .stldr, sinon relecture directe (mémoire visible à son adresse)."""
        bad = []
        segs = [(a, bytes(b)) for a, b in _merge(segments) if b]
        p = self.placement
        if self.algo.kind == "FLM" and "Verify" in p.funcs:
            self._install()
            with self._session_calls() as call:
                self._init(call, 3)
                for a, b in segs:
                    for off in range(0, len(b), p.buffer_size):
                        chunk = b[off:off + p.buffer_size]
                        self.session.write_memory(p.buffer, chunk)
                        r0 = self._call(call, "Verify", a + off, len(chunk), p.buffer, check=False)
                        if r0 != a + off + len(chunk):
                            bad.append(f"{r0:#x} (Verify)")
                            break
                self._uninit(call, 3)
            return bad
        if self.algo.kind == "stldr" and "Read" in p.funcs:
            self._install()
            with self._session_calls() as call:
                self._init(call, 3)
                for a, b in segs:
                    for off in range(0, len(b), p.buffer_size):
                        chunk = b[off:off + p.buffer_size]
                        self._call(call, "Read", a + off, len(chunk), p.buffer)
                        got = self.session.read_memory(p.buffer, len(chunk))
                        if got != chunk:
                            i = next(k for k in range(len(chunk)) if got[k] != chunk[k])
                            bad.append(f"{a + off + i:#x}")
                            break
            return bad
        for a, b in segs:
            got = self.session.read_memory(a, len(b))
            if got != b:
                i = next(k for k in range(len(b)) if got[k] != b[k])
                bad.append(f"{a + i:#x} : image {b[i]:#04x}, mémoire {got[i]:#04x}")
        return bad


def split_segments(segments, loaders) -> tuple[dict, list]:
    """Segments couverts par un chargeur ({chargeur: segments}) et le reste (pour `load`)."""
    covered: dict = {}
    rest = []
    for a, b in segments:
        pos, end = a, a + len(b)
        while pos < end:
            owner = next((ld for ld in loaders if ld.device.covers(pos)), None)
            if owner is not None:
                stop = min(end, owner.device.end)
                covered.setdefault(owner, []).append((pos, b[pos - a:stop - a]))
            else:
                nxt = min([ld.device.start for ld in loaders if ld.device.start > pos] + [end])
                stop = min(end, nxt)
                rest.append((pos, b[pos - a:stop - a]))
            pos = stop
    return covered, rest
