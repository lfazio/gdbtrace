"""Décodeur ITM/DWT et déformateur TPIU sur des flux construits octet par octet."""
import random

from gdbtrace.trace.itm import (DataTrace, EventCounter, ExceptionTrace, Extension,
                                GlobalTimestamp, Instrumentation, ItmDecoder, ItmTextSink,
                                LocalTimestamp, Overflow, PcSample, Sync, exception_name)
from gdbtrace.trace.tpiu import TpiuDeformatter, frame_bytes

SYNC = bytes([0, 0, 0, 0, 0, 0x80])


def stim(port, value, size=1):
    code = {1: 1, 2: 2, 4: 3}[size]
    return bytes([(port & 31) << 3 | code]) + value.to_bytes(size, "little")


def hw(ident, value, size):
    code = {1: 1, 2: 2, 4: 3}[size]
    return bytes([ident << 3 | 4 | code]) + value.to_bytes(size, "little")


# Flux de référence : chaque octet d'en-tête est calculé à la main dans les commentaires.
STREAM = (
    SYNC
    + b"\x01A"                        # port 0, 8 bits : 'A'
    + b"\x0b\x78\x56\x34\x12"         # port 1, 32 bits : 0x12345678
    + b"\x30"                         # LTS2 : delta 3
    + b"\xc0\xac\x02"                 # LTS1, TC=0 : delta 0x2C + (2 << 7) = 300
    + b"\x17\x34\x12\x00\x08"         # DWT id 2 (PC sample), 32 bits : 0x08001234
    + b"\x15\x00"                     # DWT id 2, 8 bits : sommeil
    + b"\x0e\x0f\x10"                 # DWT id 1 (exception) : n°15, fonction 1 = entrée
    + b"\x0e\x0f\x20"                 #   n°15, fonction 2 = sortie
    + b"\x05\x21"                     # DWT id 0 (compteurs) : CPI + CYC
    + b"\x47\x00\x10\x00\x20"         # DWT id 8 (data trace PC, comparateur 0)
    + b"\x8f\xef\xbe\xad\xde"         # DWT id 17 (data trace écriture, comparateur 0)
    + b"\x70"                         # overflow
    + b"\x94\x85\x01"                 # GTS1 : 0x05 + (1 << 7)
    + b"\x18"                         # extension : page de ports 1
    + b"\x11\x42"                     # port 2 de la page 1 → port 34
)


def test_page_change_beyond_implemented_ports_is_a_lost_byte():
    """32 ports (Cortex-M3 à M55) : un octet perdu sur le SWO (horodatage tronqué) fait lire
    ses octets suivants comme des en-têtes, parfois d'extension de page ; ignorés, sinon tous
    les ports suivants seraient décalés de 32 (vu sur une Nucleo-N6 : « [63] » pour le 31)."""
    lost = SYNC + b"\xc0\xa7" + b"\x17\xb0\x2c\x18\x34" + b"\x01A" + b"\xfb\x88\x89\x18\x34"
    d = ItmDecoder()
    ports = [p.port for p in d.feed(lost) if isinstance(p, Instrumentation)]
    assert ports == [0, 31] and d.page == 0 and d.errors


def test_sync_resets_the_port_page():
    d = ItmDecoder(max_page=1)
    d.feed(SYNC + b"\x18")
    assert d.page == 1
    pkts = d.feed(SYNC + b"\x01A")
    assert pkts[-1] == Instrumentation(port=0, value=0x41, size=1, ts=0)


def check_reference(pkts):
    assert isinstance(pkts[0], Sync)
    assert pkts[1] == Instrumentation(port=0, value=0x41, size=1, ts=0)
    assert pkts[2] == Instrumentation(port=1, value=0x12345678, size=4, ts=0)
    assert pkts[3] == LocalTimestamp(delta=3, tc=0, ts=3)
    assert pkts[4] == LocalTimestamp(delta=300, tc=0, ts=303)
    assert pkts[5] == PcSample(pc=0x08001234, ts=303)
    assert pkts[6] == PcSample(pc=None, ts=303)
    assert pkts[7] == ExceptionTrace(number=15, event="entry", ts=303)
    assert pkts[8] == ExceptionTrace(number=15, event="exit", ts=303)
    assert isinstance(pkts[9], EventCounter) and pkts[9].overflows == ["CPI", "CYC"]
    assert pkts[10] == DataTrace(comparator=0, kind="pc", value=0x20001000, size=4, ts=303)
    assert pkts[11] == DataTrace(comparator=0, kind="write", value=0xDEADBEEF, size=4, ts=303)
    assert isinstance(pkts[12], Overflow)
    assert pkts[13] == GlobalTimestamp(value=0x85, high=False, ts=303)
    assert pkts[14] == Extension(page=1, hardware=False, ts=303)
    assert pkts[15] == Instrumentation(port=34, value=0x42, size=1, ts=303)
    assert len(pkts) == 16


def test_reference_stream():
    d = ItmDecoder(max_page=1)          # un ITM à 64 ports (2 pages)
    check_reference(d.feed(STREAM))
    assert d.errors == 0 and d.synced and not d.buf


def test_byte_by_byte_feeding():
    d = ItmDecoder(max_page=1)
    pkts = []
    for b in STREAM:
        pkts += d.feed(bytes([b]))
    check_reference(pkts)


def test_random_chunks():
    rnd = random.Random(1)
    for _ in range(50):
        d, pkts, i = ItmDecoder(max_page=1), [], 0
        while i < len(STREAM):
            n = rnd.randint(1, 7)
            pkts += d.feed(STREAM[i:i + n])
            i += n
        check_reference(pkts)


def test_garbage_is_skipped_and_counted():
    d = ItmDecoder()
    pkts = d.feed(b"\x04\x80\xa0" + stim(0, ord("x")) + b"\x00\x00" + stim(0, ord("y")))
    assert [p.value for p in pkts if isinstance(p, Instrumentation)] == [ord("x"), ord("y")]
    assert d.errors == 4      # 0x04, 0x80, 0xA0 (réservés) + une suite de zéros sans synchro


def test_incomplete_packet_waits():
    d = ItmDecoder()
    assert d.feed(b"\x0b\x78\x56") == []
    assert d.feed(b"\x34\x12") == [Instrumentation(port=1, value=0x12345678, size=4)]


def test_text_sink():
    lines = []
    sink = ItmTextSink(text_ports={0}, on_line=lambda p, t, ts: lines.append((p, t)))
    d = ItmDecoder()
    data = b"".join(stim(0, c) for c in b"Bonjour\r\n") + stim(0, int.from_bytes(b"ok\n\0", "little"), 4)
    data += stim(3, 0xBEEF, 2) + b"".join(stim(0, c) for c in "été\n".encode())
    for p in d.feed(data):
        sink.add(p)
    assert lines == [(0, "Bonjour"), (0, "ok"), (3, "0xbeef"), (0, "été")]
    sink.add(Instrumentation(port=0, value=ord("z")))
    assert sink.text(0) == "Bonjour\nok\nété\nz"


def test_exception_names():
    assert exception_name(15) == "SysTick" and exception_name(11) == "SVCall"
    assert exception_name(16) == "IRQ0" and exception_name(0) == "Thread"


# ====================================================================== TPIU
def test_tpiu_hand_written_frames():
    # trame écrite à la main : ID 1 immédiat, données 01 80 55, puis flux null
    frame = bytes([0x03, 0x01, 0x80, 0x55] + [0x01, 0x00] * 5 + [0x01, 0x00])
    d = TpiuDeformatter()
    assert d.feed(b"\x12\x34" + b"\xff\xff\xff\x7f" + frame) == [(1, b"\x01\x80\x55")]
    # bit 0 d'un octet pair restitué depuis l'octet auxiliaire : 0x81 = 0x80 + aux bit 1
    frame = bytes([0x03, 0x10, 0x80, 0x20] + [0x01, 0x00] * 5 + [0x01, 0b10])
    assert TpiuDeformatter().feed(b"\xff\xff\xff\x7f" + frame) == [(1, b"\x10\x81\x20")]
    # changement d'ID retardé (bit aux 1) : l'octet suivant appartient encore au flux 1
    frame = bytes([0x03, 0x11, 0x05, 0x22, 0x01, 0x00] + [0x01, 0x00] * 4 + [0x01, 0b10])
    assert TpiuDeformatter().feed(b"\xff\xff\xff\x7f" + frame) == [(1, b"\x11\x22")]


def test_tpiu_roundtrip_streams_and_padding():
    rnd = random.Random(2)
    for n in range(1, 40):
        data = bytes(rnd.randrange(256) for _ in range(n))
        other = bytes(rnd.randrange(256) for _ in range(n // 2 + 1))
        raw = frame_bytes(1, data) + b"\xff\x7f" + frame_bytes(2, other)[4:]
        d = TpiuDeformatter()
        out = []
        for i in range(0, len(raw), 5):              # livraison par petits morceaux
            out += d.feed(raw[i:i + 5])
        merged = {}
        for sid, chunk in out:
            merged[sid] = merged.get(sid, b"") + chunk
        assert merged == {1: data, 2: other}, n


def test_itm_through_tpiu():
    d, itm = TpiuDeformatter(), ItmDecoder(max_page=1)
    pkts = []
    for sid, chunk in d.feed(frame_bytes(1, STREAM)):
        assert sid == 1
        pkts += itm.feed(chunk)
    check_reference(pkts)
