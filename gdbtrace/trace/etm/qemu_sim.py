"""« ETM simulé » pour QEMU, qui n'émule pas d'ETM.

Pendant chaque exécution (Go → arrêt), QEMU journalise les instructions exécutées
(activé/désactivé par le moniteur : `logfile`, `log`). À l'arrêt, le journal est converti
en flux ETMv3 par l'encodeur — le même flux qu'émettrait l'ETM d'un Cortex-M — puis décodé
comme une vraie capture. Toute la chaîne de décodage et les vues sont ainsi utilisables
sans matériel. QEMU doit être lancé avec `-accel tcg,one-insn-per-tb=on`
(`QemuTarget(etm_sim=True)`, option `--etm-sim`).
"""
from __future__ import annotations

import os
import tempfile

from .encoder import Etm3Encoder
from .qemu_log import LOG_ITEMS, parse

MAX_LOG_BYTES = 150 * 1024 * 1024      # au-delà, seule la fin du journal est décodée


class QemuEtmSim:
    def __init__(self, qmp, image, etm_trace):
        self.qmp = qmp
        self.image = image
        self.etm = etm_trace
        self.dir = tempfile.mkdtemp(prefix="gdbtrace-etm-")
        self.path: str | None = None
        self.runs = 0
        self.last_events = 0
        self.truncated = False

    def start_run(self) -> None:
        self.runs += 1
        self.path = os.path.join(self.dir, f"run{self.runs}.log")
        self.qmp.hmp(f"logfile {self.path}")
        self.qmp.hmp(f"log {LOG_ITEMS}")

    def end_run(self) -> int:
        """Arrête la journalisation, encode puis décode la trace de l'exécution.
        Retourne le nombre d'octets ETM produits."""
        if self.path is None:
            return 0
        self.qmp.hmp("log none")
        self.qmp.hmp(f"logfile {os.path.join(self.dir, 'idle.log')}")   # ferme et vide le fichier
        path, self.path = self.path, None
        try:
            size = os.path.getsize(path)
            with open(path, "rb") as fh:
                self.truncated = size > MAX_LOG_BYTES
                if self.truncated:
                    fh.seek(size - MAX_LOG_BYTES)
                    fh.readline()                                   # ligne coupée
                text = fh.read().decode("latin-1")
        finally:
            os.remove(path)
        events = list(parse(text.splitlines()))
        self.last_events = len(events)
        data = Etm3Encoder(self.image).encode(events)
        self.etm.feed(data)
        return len(data)

    def close(self) -> None:
        try:
            self.qmp.hmp("log none")
        except Exception:  # noqa: BLE001
            pass
        for name in os.listdir(self.dir):
            os.remove(os.path.join(self.dir, name))
        os.rmdir(self.dir)
