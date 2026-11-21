from conftest import FIB_LINE

from gdbtrace.core.symbols import SymbolTable


def test_symbols(elf):
    t = SymbolTable(elf)
    fib = t.function("fib")
    assert fib and fib.start == 0xAC
    assert t.function_at(fib.start + 4).name == "fib"
    le = t.line_at(fib.start + 8)
    assert le.file.endswith("main.c") and le.line == FIB_LINE
    assert t.objects["sensors"][0] == 0x20000000
    assert any(f.endswith("startup.c") for f in t.source_files())


def test_function_at_prefers_sized_alias(m0_elf):
    """libgcc ARMv6-M : __aeabi_uidiv (taille 0) et __udivsi3 partagent leur adresse."""
    from gdbtrace.core.symbols import SymbolTable
    sym = SymbolTable(m0_elf)
    start = sym.function("__udivsi3").start
    assert sym.function("__aeabi_uidiv").start == start
    assert sym.function_at(start + 2).name == "__udivsi3"
