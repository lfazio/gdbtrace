import time

from conftest import SVD, needs_qemu

from gdbtrace.core import svd
from gdbtrace.core.scripting import ScriptAPI


def test_parse_int_svd_forms():
    # scaledNonNegativeInteger : 0x hexadécimal, #binaire, sinon décimal --
    # zéros en tête compris (le SVD STM32F746 de ST écrit `00000010`)
    assert svd.parse_int("0x10") == 16
    assert svd.parse_int("0X1F") == 31
    assert svd.parse_int("00000010") == 10
    assert svd.parse_int("0") == 0
    assert svd.parse_int("#1x1") == 5
    assert svd.parse_int("4k") == 4096
    assert svd.parse_int("+8") == 8
    assert svd.parse_int(None, 3) == 3
    assert svd.parse_int("  ", 7) == 7


def test_parse_demo_svd():
    dev = svd.parse(SVD)
    assert dev.name == "MPS2_AN385"
    names = [p.name for p in dev.peripherals]
    assert names == ["TIMER0", "TIMER1", "UART0", "SysTick", "SCB"]  # triés par adresse
    t1 = dev.peripheral("timer1")                                   # insensible à la casse
    assert t1.base == 0x40001000 and t1.description == "CMSDK APB timer 1"
    assert [r.name for r in t1.registers] == ["CTRL", "VALUE", "RELOAD", "INTSTATUS"]  # derivedFrom
    scb = dev.peripheral("SCB")
    assert [(r.name, r.offset) for r in scb.registers if r.name.startswith("SHPR")] == \
        [("SHPR1", 0x18), ("SHPR2", 0x1C), ("SHPR3", 0x20)]        # dim + dimIndex 1-3
    uart = dev.peripheral("UART0")
    assert not uart.register("DATA").readable and uart.register("DATA").side_effect  # readAction
    load = dev.peripheral("SysTick").register("LOAD")
    assert load.fields[0].lsb == 0 and load.fields[0].width == 24  # lsb/msb
    val = dev.peripheral("SysTick").register("VAL")
    assert val.fields[0].bits == "[23:0]"                            # bitRange


def test_fields_and_enums():
    cpuid = svd.parse(SVD).peripheral("SCB").register("CPUID")
    f = {x.name: x for x in cpuid.fields}
    assert f["PARTNO"].extract(0x410FC231) == 0xC23
    assert f["PARTNO"].format(0x410FC231) == "0xc23  CORTEX_M3"
    assert f["IMPLEMENTER"].format(0x410FC231) == "0x41  ARM"
    assert f["REVISION"].insert(0x410FC231, 5) == 0x410FC235
    assert [x.name for x in cpuid.fields][0] == "IMPLEMENTER"        # bits de poids fort d'abord


def test_read_blocks_never_span_gaps():
    dev = svd.parse(SVD)
    scb = dev.peripheral("SCB")
    blocks = [(a, n, [r.name for r in rs]) for a, n, rs in svd.read_blocks(scb)]
    assert blocks == [(0xE000ED00, 16, ["CPUID", "ICSR", "VTOR", "AIRCR"]),
                      (0xE000ED18, 12, ["SHPR1", "SHPR2", "SHPR3"])]
    uart = dev.peripheral("UART0")
    assert svd.read_blocks(uart)[0][0] == 0x40004004                 # DATA sautée
    assert svd.read_blocks(uart, force=True)[0][0] == 0x40004000     # sauf lecture forcée


INLINE = """<device><name>X</name><size>32</size>
<peripherals>
 <peripheral><name>DMA</name><baseAddress>0x40020000</baseAddress>
  <registers>
   <register><name>ISR</name><addressOffset>0</addressOffset><access>read-only</access></register>
   <cluster><dim>2</dim><dimIncrement>0x14</dimIncrement><name>CH%s</name><addressOffset>0x8</addressOffset>
     <register><name>CCR</name><addressOffset>0</addressOffset><size>16</size>
       <fields><field><name>PL</name><bitRange>[13:12]</bitRange>
         <enumeratedValues><enumeratedValue><name>LOW</name><value>#00</value></enumeratedValue>
           <enumeratedValue><name>HIGH</name><value>0b10</value></enumeratedValue></enumeratedValues></field></fields>
     </register>
     <register derivedFrom="CCR"><name>CCR2</name><addressOffset>4</addressOffset></register>
   </cluster>
   <register><dim>2</dim><dimIncrement>4</dimIncrement><dimIndex>A,B</dimIndex>
     <name>OUT[%s]</name><addressOffset>0x40</addressOffset><access>write-only</access></register>
  </registers>
 </peripheral>
</peripherals></device>"""


def test_clusters_register_derivation_and_dim_lists():
    p = svd.parse(INLINE).peripheral("DMA")
    regs = {r.name: r for r in p.registers}
    assert regs["CH0.CCR"].offset == 0x8 and regs["CH1.CCR"].offset == 0x1C
    assert regs["CH0.CCR"].size == 16 and regs["CH0.CCR"].nbytes == 2
    assert regs["CH1.CCR2"].offset == 0x20 and regs["CH1.CCR2"].fields[0].name == "PL"
    assert regs["CH0.CCR"].fields[0].enums == {0: "LOW", 2: "HIGH"}
    assert regs["OUT[A]"].offset == 0x40 and regs["OUT[B]"].offset == 0x44
    assert not regs["OUT[A]"].readable
    assert regs["ISR"].access == svd.READ_ONLY


@needs_qemu
def test_periph_read_in_qemu(session, capsys):
    t32 = ScriptAPI(session, session.target.live_memory(), svd.parse(SVD))
    try:
        session.break_("animate", temporary=True)
        session.go()
        session.wait_stopped(5)
        st = t32.periph("SysTick")
        assert st["CTRL"] & 7 == 7 and st["LOAD"] == 25000 - 1
        assert t32.periph("SCB")["CPUID"] == 0x410FC231
        out = capsys.readouterr().out
        assert "CLKSOURCE" in out and "PROCESSOR" in out and "CORTEX_M3" in out
        # en marche : même API, via le canal live
        session.go()
        time.sleep(0.1)
        v1 = t32.periph("TIMER0")["VALUE"]
        time.sleep(0.05)
        v2 = t32.periph("TIMER0")["VALUE"]
        assert v1 != v2                                              # le timer décompte
        capsys.readouterr()
        uart = t32.periph("UART0")
        assert "DATA" not in uart and "STATE" in uart                # DATA jamais lue d'office
        assert "DATA" in capsys.readouterr().out.split("(non lu : lecture destructive)")[0]
    finally:
        t32.live.close()
