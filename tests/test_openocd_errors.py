"""Échecs de démarrage d'OpenOCD expliqués (mauvaise sonde, sonde prise, numéro inconnu)."""
from gdbtrace.core import target
from gdbtrace.core.target import explain_openocd_failure

HEAD = ["Open On-Chip Debugger 0.12.0", "Info : clock speed 1000 kHz"]


def test_wrong_probe(monkeypatch):
    """Plusieurs cartes : OpenOCD prend la première sonde, la puce n'est pas la bonne."""
    monkeypatch.setattr(target, "stlink_probes", lambda: [("AAA", "ST-LINK/V2-1"),
                                                          ("BBB", "STLINK-V3")])
    log = HEAD + ["Info : STLINK V2J48M35 (API v2) VID:PID 0483:374B",
                  "Warn : UNEXPECTED idcode: 0x5ba02477", "Error: expected 1 of 1: 0x0bb11477"]
    why = explain_openocd_failure(log, ["/x/configs/nucleo-f072rb.cfg"])
    assert why.startswith("mauvaise sonde : derrière la première sonde trouvée")
    assert "Cortex-M7 (IDCODE 0x5ba02477)" in why and "Cortex-M0 (IDCODE 0x0bb11477)" in why
    assert "nucleo-f072rb.cfg" in why and "AAA (ST-LINK/V2-1), BBB (STLINK-V3)" in why
    assert "STLINK_SERIAL" in why
    assert "derrière la sonde AAA" in explain_openocd_failure(log, ["f.cfg"], "AAA")


def test_busy_probe_and_unknown_serial(monkeypatch):
    monkeypatch.setattr(target, "stlink_probes", lambda: [("AAA", "ST-LINK/V2-1")])
    # sonde tenue par un autre OpenOCD : bloqué avant d'ouvrir la sonde, sans message
    assert "déjà prise" in explain_openocd_failure(HEAD, ["f.cfg"], "AAA")
    assert "déjà prise" in explain_openocd_failure(HEAD + ["Error: libusb_claim_interface() "
                                                           "failed with LIBUSB_ERROR_BUSY"], ["f.cfg"])
    assert explain_openocd_failure(HEAD + ["Info : STLINK V2J28M17 (API v2)",
                                           "Error: init mode failed"], ["f.cfg"]) is None
