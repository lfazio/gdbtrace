"""Ce qui, dans la trace, dépend du microcontrôleur et pas du cœur : où sort le SWO, à quelle
horloge, comment s'allument les horloges de trace, et la chaîne CoreSight hors du cœur (funnel,
ETF, ETR, réplicateur) des puces qui en ont une.

Un Cortex-M3/M4/M7 a son TPIU à 0xE0040000, cadencé par le cœur : c'est le cas par défaut
(`chip=None` partout). Le STM32N6 (Cortex-M55) n'a pas ce TPIU :

- le SWO est un composant CoreSight à part (0xE008A000 : CODR = diviseur, SPPR = protocole),
  qui ne porte que l'ITM ; son horloge (ck_trace) est celle du cœur divisée par 8 (RM0486) ;
- DBGMCU_CR (0x44001004) doit allumer ck_sys_dbg et ck_trace (DBGCLKEN, TRACECLKEN) ;
- l'ETM passe par le funnel (entrée 0), l'ETF (FIFO de 4 Ko) puis le réplicateur, vers l'ETR
  (tampon en RAM, sortie 0) et le TPIU parallèle (sortie 1, filtrée : sinon il bloque le flux) ;
- l'ETR est un maître AXI derrière un RIMU (RIFSC_RIMC_ATTR0) qui sort du reset non sécurisé,
  CID 0 : la RAM, sécurisée par défaut, refuse alors ses écritures sans erreur visible. Il doit
  être déclaré sécurisé, privilégié, CID 1 (celui du cœur) ;
- l'horodatage de l'ETM vient du générateur de temps global (TSGEN, 0xE0086000), arrêté au
  reset : CNTCR.EN le démarre.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TraceChip:
    name: str
    #: SWO : "tpiu" (TPIU du cœur, 0xE0040000) ou "coresight-swo" (composant SWO CoreSight)
    swo_kind: str = "tpiu"
    swo_base: int = 0xE0040000
    #: horloge du SWO = horloge du cœur / traceclk_div
    traceclk_div: int = 1
    #: registre et bits qui allument les horloges de trace (None : rien à faire)
    dbgmcu_cr: int | None = None
    dbgmcu_bits: int = 0
    #: chaîne CoreSight hors du cœur (None : absente)
    funnel: int | None = None
    funnel_etm_port: int = 0
    etf: int | None = None
    etr: int | None = None
    replicator: int | None = None
    replicator_etr_port: int = 0
    #: (adresse, valeur) des attributs du maître AXI de l'ETR (pare-feu de la puce)
    etr_master: tuple[int, int] | None = None
    #: générateur de temps global CoreSight (horodatage de l'ETM et de l'ITM) ; None : aucun
    tsgen: int | None = None

    def start_timestamps(self, read_u32, write_u32) -> str | None:
        """Démarre le générateur de temps (CNTCR.EN) ; libellé pour le journal, ou None."""
        if self.tsgen is None:
            return None
        write_u32(self.tsgen, read_u32(self.tsgen) | 1)
        return f"générateur de temps ({self.tsgen:#010x}) démarré"


STM32N6 = TraceChip(
    name="STM32N6",
    swo_kind="coresight-swo", swo_base=0xE008A000, traceclk_div=8,
    dbgmcu_cr=0x44001004, dbgmcu_bits=1 << 20 | 1 << 21,     # DBGCLKEN, TRACECLKEN
    funnel=0xE0081000, funnel_etm_port=0,
    etf=0xE0082000, etr=0xE0083000,
    replicator=0xE0085000, replicator_etr_port=0,
    etr_master=(0x54024C10, 1 << 9 | 1 << 8 | 1 << 4),       # RIMC_ATTR0 : MPRIV, MSEC, CID 1
    tsgen=0xE0086000,
)

#: DBGMCU_IDCODE : (adresse, masque DEV_ID, valeur) → puce
_IDCODES = (
    (0x44001000, 0xFFF, 0x486, STM32N6),
)


def detect_chip(read_u32) -> TraceChip | None:
    """Puce reconnue par son DBGMCU_IDCODE (cible arrêtée), None sinon (TPIU standard)."""
    for addr, mask, value, chip in _IDCODES:
        try:
            if read_u32(addr) & mask == value:
                return chip
        except Exception:  # noqa: BLE001 - adresse absente sur cette puce
            continue
    return None
