"""Modèles de cycles par cœur (trace/etm/cycles.py) : Cortex-M3/M4 inchangé, Cortex-M0 et M0+
d'après les tables 3-1 de leurs TRM (ARM DDI 0432C, DDI 0484C), et choix par le CPUID."""
import pytest

from gdbtrace.trace.etm.cycles import (CORTEX_M0, CORTEX_M0P, CORTEX_M3, annotate, cycles_of,
                                       model_for)
from gdbtrace.trace.etm.image import DIRECT, INDIRECT, NORMAL, Instr
from gdbtrace.trace.etm.qemu_log import ExcEntry, ExcReturn, Step


def ins(text, kind=NORMAL, op=None, cond=False, call=False, ret=False, target=None):
    return Instr(0x100, 2, kind, target, cond, call, ret, text, op or text.split()[0])


B = ins("b #0x200", DIRECT, target=0x200)
BEQ = ins("beq #0x200", DIRECT, op="b", cond=True, target=0x200)
BL = ins("bl #0x400", DIRECT, call=True, target=0x400)
BX = ins("bx lr", INDIRECT, ret=True)
POP_PC = ins("pop {r4, r5, pc}", INDIRECT, ret=True)
POP = ins("pop {r4, r5}")
STR = ins("str r0, [r1]")
LDR = ins("ldr r0, [r1]")
MRS = ins("mrs r0, primask")
ISB = ins("isb sy", op="isb")
ADD = ins("adds r0, r1")


@pytest.mark.parametrize("model,expected", [
    # B pris, B<c> non pris, BL, BX, POP {r4, r5, pc}, POP {r4, r5}, STR, LDR, MRS, ISB, ADDS
    (CORTEX_M3, [3, 1, 3, 3, 6, 3, 1, 2, 1, 1, 1]),
    (CORTEX_M0, [3, 1, 4, 3, 7, 3, 2, 2, 4, 4, 1]),
    (CORTEX_M0P, [2, 1, 3, 2, 6, 3, 2, 2, 3, 3, 1]),
])
def test_instruction_cycles(model, expected):
    got = [cycles_of(B, True, True, model), cycles_of(BEQ, False, False, model),
           cycles_of(BL, True, True, model), cycles_of(BX, True, True, model),
           cycles_of(POP_PC, True, True, model), cycles_of(POP, model=model),
           cycles_of(STR, model=model), cycles_of(LDR, model=model), cycles_of(MRS, model=model),
           cycles_of(ISB, model=model), cycles_of(ADD, model=model)]
    assert got == expected


def test_m3_model_is_the_default():
    """Le modèle Cortex-M3/M4 reste celui des décodeurs ETM (valeurs d'avant les modèles)."""
    for i in (B, BL, BX, POP_PC, STR, LDR, MRS, ADD):
        assert cycles_of(i, True, True) == cycles_of(i, True, True, CORTEX_M3)


def test_exceptions_by_model():
    class Image:
        def instr(self, addr):
            return ADD
    ev = [Step(0x100), ExcEntry(15, 0x80), Step(0x100), ExcReturn()]
    for model, entry, exit_ in ((CORTEX_M3, 12, 10), (CORTEX_M0, 16, 16), (CORTEX_M0P, 15, 15)):
        out = annotate(ev, Image(), model)
        assert [e.cycles for e in out] == [1, entry, 1, exit_], model.name


def test_model_for_core():
    assert model_for(0xC20) is CORTEX_M0 and model_for(0xC60) is CORTEX_M0P
    assert model_for(0xC24) is CORTEX_M3 and model_for(None) is CORTEX_M3
    assert model_for(None, CORTEX_M0P) is CORTEX_M0P            # une vraie MTB : un M0+
