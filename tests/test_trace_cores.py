"""Ce que la trace permet selon le cœur : un Cortex-M0/M0+ (ARMv6-M) n'a ni ITM, ni SWO, ni
ETM. GdbTrace le reconnaît (CPUID) et refuse ces traces en le disant, au lieu d'écrire dans
des registres absents (STM32G0 : RM0444 §40)."""
import pytest

from gdbtrace.trace.config import SwoConfig
from gdbtrace.trace.manager import TraceManager

CPUID = 0xE000ED00
M0PLUS, M0, M4 = 0x410CC601, 0x410CC200, 0x410FC241


class FakeCore:
    """Session arrêtée : CPUID lisible, le reste du PPB absent (lecture en erreur) ; les
    écritures sont notées (aucune ne doit partir vers un ITM, TPIU ou ETM absent)."""

    def __init__(self, cpuid):
        self.cpuid = cpuid
        self.state = "stopped"
        self.writes = []

    def read_u32(self, addr):
        if addr == CPUID:
            return self.cpuid
        raise OSError(f"{addr:#x} : pas de composant")

    def write_u32(self, addr, value):
        self.writes.append((addr, value))


def test_cortex_m0plus_refuses_itm_swo_and_etm():
    tm = TraceManager()
    s = FakeCore(M0PLUS)
    assert tm.detect_core(s) == 0xC60 and tm.core_name == "Cortex-M0+"
    reason = tm.no_trace_reason()
    assert "Cortex-M0+ (ARMv6-M) : ni ITM, ni SWO, ni ETM" in reason and "--mtb-probe" in reason
    with pytest.raises(RuntimeError, match="ni ITM, ni SWO, ni ETM"):
        tm.configure_target(SwoConfig(cpu_hz=16_000_000), s)
    with pytest.raises(RuntimeError, match="ni ITM"):
        tm.trace_variable(s, "counter")
    with pytest.raises(RuntimeError, match="ni ITM"):
        tm.start_etm_swo(s, cpu_hz=16_000_000, swo_file="capture.bin")
    assert tm.setup_error.startswith("ETM par SWO : Cortex-M0+")
    with pytest.raises(RuntimeError, match="ni ITM"):
        tm.start_etm_tmc(s)
    assert s.writes == []                           # rien d'écrit dans des registres absents
    assert "ni ITM, ni SWO, ni ETM" in tm.status() and tm.status().startswith("pas de capture SWO")


def test_trace_status_names_the_core(elf):
    """Les vues Trace et Couverture disent pourquoi rien n'arrive : le cœur, pas « lancez une
    capture »."""
    pytest.importorskip("capstone")
    from gdbtrace.core.symbols import SymbolTable
    tm = TraceManager()
    assert tm.setup_etm(elf, SymbolTable(elf)) is None
    assert tm.detect_etm(FakeCore(M0PLUS)) == tm.etm.version       # aucun ETM lu sur un M0+
    assert "Cortex-M0+ (ARMv6-M) : ni ITM, ni SWO, ni ETM" in tm.trace_status()


def test_cortex_m0_and_m4():
    tm = TraceManager()
    tm.detect_core(FakeCore(M0))
    reason = tm.no_trace_reason()
    assert reason.startswith("Cortex-M0 (ARMv6-M) : ni ITM, ni SWO, ni ETM. ")   # pas de MTB
    assert "vue Profil" in reason and "RTOS" in reason              # où trouver les durées
    tm.detect_core(FakeCore(M4))
    assert tm.core_name == "Cortex-M4" and tm.no_trace_reason() is None
    tm.detect_core(type("Mute", (), {"read_u32": lambda self, a: (_ for _ in ()).throw(OSError())})())
    assert tm.core is None and tm.no_trace_reason() is None          # cœur inconnu : rien refusé
