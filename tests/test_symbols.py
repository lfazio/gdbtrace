from gdbtrace.core.symbols import SymbolTable


def test_symbols(elf):
    t = SymbolTable(elf)
    fib = t.function("fib")
    assert fib and fib.start == 0xAC
    assert t.function_at(fib.start + 4).name == "fib"
    le = t.line_at(fib.start + 8)
    assert le.file.endswith("main.c") and le.line == 15
    assert t.objects["sensors"][0] == 0x20000000
    assert any(f.endswith("startup.c") for f in t.source_files())
