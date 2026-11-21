import struct

from conftest import needs_qemu

from gdbtrace.core.live_vars import LeafLayout, classify, compute_layout, decode, root_identifier


def test_classify():
    assert classify("type = volatile unsigned long", 4) == ("uint", {})
    assert classify("type = short", 2) == ("int", {})
    assert classify("type = long long", 8) == ("int", {})
    assert classify("type = char", 1) == ("char", {})
    assert classify("type = signed char", 1) == ("schar", {})
    assert classify("type = const volatile float", 4) == ("float", {})
    assert classify("type = double", 8) == ("float", {})
    assert classify("type = _Bool", 1) == ("bool", {})
    assert classify("type = struct sensor *", 4) == ("ptr", {})
    assert classify("type = void (*)(void)", 4) == ("ptr", {})
    assert classify("type = volatile enum mode {MODE_IDLE, MODE_RUN, MODE_FAULT = 5, MODE_X}", 4) == \
        ("enum", {0: "MODE_IDLE", 1: "MODE_RUN", 5: "MODE_FAULT", 6: "MODE_X"})
    assert classify("type = struct sensor {\n int id;\n}", 12) is None
    assert classify("type = int [4]", 16) is None


def L(kind, size, **kw):
    return LeafLayout(addr=0, size=size, kind=kind, live=True, **kw)


def test_decode_like_gdb():
    assert decode(L("int", 2), (-3 & 0xFFFF).to_bytes(2, "little")) == "-3"
    assert decode(L("uint", 4), (0xFFFFFFFF).to_bytes(4, "little")) == "4294967295"
    assert decode(L("char", 1), b"A") == "65 'A'"
    assert decode(L("char", 1), b"\x00") == "0 '\\000'"
    assert decode(L("char", 1), b"\n") == "10 '\\n'"
    assert decode(L("schar", 1), b"\xff") == "-1 '\\377'"
    assert decode(L("float", 4), struct.pack("<f", 22.25)) == "22.25"
    assert decode(L("float", 4), struct.pack("<f", 0.1)) == "0.100000001"
    assert decode(L("float", 8), struct.pack("<d", 1.5)) == "1.5"
    assert decode(L("ptr", 4), (0x20000018).to_bytes(4, "little")) == "0x20000018"
    assert decode(L("bool", 1), b"\x01") == "true"
    enum = L("enum", 4, enum_names={0: "IDLE", 5: "FAULT"})
    assert decode(enum, (5).to_bytes(4, "little")) == "FAULT"
    assert decode(enum, (7).to_bytes(4, "little")) == "7"
    assert decode(L("uint", 4), b"\x01") == "<?>"


def test_root_identifier():
    assert root_identifier("((sensors)[1]).value") == "sensors"
    assert root_identifier("*p") == "p"


@needs_qemu
def test_compute_layout_matches_gdb(session):
    session.break_("update_sensor")
    session.go()
    session.wait_stopped(5)
    local_names = {v["name"] for v in session.locals()}
    lm = session.target.live_memory()
    try:
        # feuilles d'une structure globale : décodage live identique à gdb
        root = session.var_create("sensors")
        s1 = session.var_children(root["name"])[1]
        for leaf in session.var_children(s1["name"]):
            lay = compute_layout(session, leaf["name"], local_names)
            assert lay.live, lay.reason
            assert decode(lay, lm.read(lay.addr, lay.size)) == leaf["value"]
        for expr in ("mode", "last_char", "temperature", "offset", "ticks"):
            v = session.var_create(expr)
            lay = compute_layout(session, v["name"], local_names)
            assert lay.live and decode(lay, lm.read(lay.addr, lay.size)) == v["value"], expr
        # variable locale : refusée (la frame change pendant que la cible tourne)
        lay = compute_layout(session, session.var_create("tick")["name"], local_names)
        assert not lay.live and "locale" in lay.reason
    finally:
        lm.close()
