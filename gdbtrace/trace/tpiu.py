"""Déformateur TPIU (CoreSight) : sépare les flux de trace multiplexés.

Quand le formateur du TPIU est actif (trace parallèle, ou SWO avec plusieurs sources),
les données sont emballées en trames de 16 octets :
- octets pairs : bit 0 = 1 → changement d'ID de flux (ID = octet >> 1),
  bit 0 = 0 → donnée dont le bit 0 réel est dans l'octet auxiliaire (octet 15) ;
- octets impairs : données ;
- octet 15 : bits auxiliaires ; pour un changement d'ID, le bit indique si le nouvel ID
  s'applique **après** l'octet de données suivant.
Les trames sont alignées par la synchro complète FF FF FF 7F ; la demi-synchro FF 7F
peut s'intercaler entre deux trames (bourrage). Un tampon de TMC (ETB, ETF, ETR) ne contient
que des trames, alignées sur 16 octets depuis son début : `aligned=True`.

Sur le SWO, la synchro complète est rare : le TPIU d'un Cortex-M4 l'émet au démarrage du
formateur et à l'arrêt (observé sur STM32F446), pas périodiquement. Une capture qui se
connecte après, ou qui a perdu des octets, n'en reverrait jamais. Avec `ids` (flux attendus)
et `scorer`, l'alignement est alors retrouvé dans les données : chaque décalage possible est
essayé sur SEARCH_FRAMES trames ; un ID de flux inattendu l'élimine (ce qui écarte les
décalages impairs), et `scorer(morceaux)` (erreurs du décodage ITM) départage les décalages
pairs, qui ne se distinguent que par l'octet pris pour l'octet auxiliaire.
"""
from __future__ import annotations

FULL_SYNC = b"\xff\xff\xff\x7f"
HALF_SYNC = b"\xff\x7f"
FRAME = 16
SEARCH_FRAMES = 64


class TpiuDeformatter:
    def __init__(self, aligned: bool = False, ids=None, scorer=None):
        self.buf = bytearray()
        self.aligned = aligned        # trames alignées, sans synchro (mémoire d'un TMC)
        self.synced = aligned
        self.stream = 0             # ID de flux courant (0 = aucun)
        self.frames = 0
        #: flux attendus et juge des décalages : alignement retrouvé sans synchro (en-tête)
        self.ids = set(ids) if ids else None
        self.scorer = scorer
        self.realigned = 0          # alignements retrouvés dans les données

    def _find_alignment(self) -> int | None:
        """Décalage des trames dans `buf`, sans synchro ; None : pas encore assez de données,
        ou rien ne se distingue (attendre la suite)."""
        n = SEARCH_FRAMES * FRAME
        if len(self.buf) < n + FRAME:
            return None
        scored = []
        for off in range(FRAME):
            chunks = TpiuDeformatter(aligned=True).feed(bytes(self.buf[off:off + n]))
            if any(sid not in self.ids for sid, _ in chunks):
                continue
            scored.append((self.scorer(chunks) if self.scorer else 0, off))
        scored.sort()
        if not scored or (len(scored) > 1 and scored[0][0] == scored[1][0]):
            return None
        return scored[0][1]

    def feed(self, data: bytes) -> list[tuple[int, bytes]]:
        """Retourne [(id_de_flux, octets)] dans l'ordre ; l'ID 0 (null) est ignoré."""
        self.buf += data
        out: list[tuple[int, bytearray]] = []
        if not self.synced:
            k = self.buf.find(FULL_SYNC)
            if k >= 0:
                del self.buf[:k + 4]
                self.synced = True
            elif self.ids is None:
                del self.buf[:max(0, len(self.buf) - 3)]
                return []
            else:
                off = self._find_alignment()
                if off is None:                  # on garde quatre fenêtres au plus
                    excess = len(self.buf) - 4 * SEARCH_FRAMES * FRAME
                    if excess > 0:
                        del self.buf[:excess]
                    return []
                del self.buf[:off]
                self.synced = True
                self.realigned += 1
                self.stream = 0             # flux de la première trame inconnu avant son ID
        while True:
            if not self.aligned and self.buf[:4] == FULL_SYNC:
                del self.buf[:4]
                continue
            if not self.aligned and self.buf[:2] == HALF_SYNC:
                del self.buf[:2]
                continue
            if len(self.buf) < FRAME:
                break
            self._frame(bytes(self.buf[:FRAME]), out)
            del self.buf[:FRAME]
            self.frames += 1
        return [(sid, bytes(d)) for sid, d in out if sid not in (0, 0x7F)]

    def _emit(self, out: list, byte: int) -> None:
        if out and out[-1][0] == self.stream:
            out[-1][1].append(byte)
        else:
            out.append((self.stream, bytearray([byte])))

    def _frame(self, f: bytes, out: list) -> None:
        aux = f[15]
        for i in range(8):
            even = f[2 * i]
            has_odd = i < 7
            bit = (aux >> i) & 1
            if even & 1:                      # changement d'ID
                new_id = even >> 1
                if bit and has_odd:           # l'octet suivant appartient encore à l'ancien flux
                    self._emit(out, f[2 * i + 1])
                    self.stream = new_id
                else:
                    self.stream = new_id
                    if has_odd:
                        self._emit(out, f[2 * i + 1])
            else:
                self._emit(out, (even & 0xFE) | bit)
                if has_odd:
                    self._emit(out, f[2 * i + 1])


def frame_bytes(stream: int, data: bytes) -> bytes:
    """Emballe `data` dans des trames TPIU sur un seul flux (tests, rejeu de captures).

    Chaque trame commence par un changement d'ID immédiat vers `stream`. En fin de données,
    on bascule vers le flux null (ID 0) ; s'il reste un seul octet, le changement d'ID est
    marqué « retardé » (bit auxiliaire à 1) pour que cet octet, placé en position impaire,
    appartienne encore à `stream`.
    """
    out = bytearray(FULL_SYNC)
    rest = list(data)
    while rest:
        frame = bytearray(FRAME)
        aux = 0
        frame[0] = (stream << 1) | 1           # changement d'ID immédiat (bit aux 0)
        frame[1] = rest.pop(0)
        for slot in range(1, 8):
            odd = slot < 7
            if not rest:                       # plus rien : flux null jusqu'à la fin
                frame[2 * slot] = 0x01
                continue
            if len(rest) == 1 and odd:         # dernier octet en position impaire
                frame[2 * slot] = 0x01         # → flux null, mais après cet octet
                aux |= 1 << slot
                frame[2 * slot + 1] = rest.pop(0)
                continue
            b = rest.pop(0)
            frame[2 * slot] = b & 0xFE
            aux |= (b & 1) << slot
            if odd:
                frame[2 * slot + 1] = rest.pop(0)
        frame[15] = aux
        out += frame
    return bytes(out)
