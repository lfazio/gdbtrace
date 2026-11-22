"""« ETM simulé » pour QEMU, qui n'émule pas d'ETM.

Pendant chaque exécution (Go → arrêt), QEMU journalise les instructions exécutées
(activé/désactivé par le moniteur : `logfile`, `log`). À l'arrêt, le journal est converti
en flux ETMv3 (ou ETMv4 si la trace est en v4 : Cortex-M7, `mps2-an500`) par l'encodeur — le
même flux qu'émettrait l'ETM d'un Cortex-M — puis décodé
comme une vraie capture. Toute la chaîne de décodage et les vues sont ainsi utilisables
sans matériel. QEMU doit être lancé avec `-accel tcg,one-insn-per-tb=on`
(`QemuTarget(etm_sim=True)`, option `--etm-sim`).
"""
from __future__ import annotations

import os
import tempfile

from .cycles import annotate
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

    def end_run(self, end_pc: int | None = None) -> int:
        """Arrête la journalisation, encode puis décode la trace de l'exécution.
        Retourne le nombre d'octets de trace produits. `end_pc` : PC à l'arrêt (MTB)."""
        events = self._take_log()
        if events is None:
            return 0
        self.last_events = len(events)
        return self._convert(events, end_pc)

    def _take_log(self) -> list | None:
        """Événements réels (chemin QEMU) de l'exécution qui vient de s'arrêter."""
        path = self._close_log()
        return None if path is None else self._read_log(path)

    def _close_log(self) -> str | None:
        """Arrête la journalisation ; chemin du journal de l'exécution (None : aucune)."""
        if self.path is None:
            return None
        self.qmp.hmp("log none")
        self.qmp.hmp(f"logfile {os.path.join(self.dir, 'idle.log')}")   # ferme et vide le fichier
        path, self.path = self.path, None
        return path

    def _read_log(self, path: str) -> list:
        """Événements d'un journal (supprimé après lecture)."""
        if not os.path.exists(path):            # relancé mais jamais exécuté (arrêt aussitôt)
            return []
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
        return list(parse(text.splitlines()))

    def _convert(self, events: list, end_pc: int | None) -> int:
        events = annotate(events, self.image)        # cycles du modèle (QEMU n'en a pas)
        if getattr(self.etm, "version", 3) == 4:
            from .encoder4 import Etm4Encoder
            data = Etm4Encoder(self.image).encode(events)
            self.etm.feed(data)
            self.etm.flush()                         # fin de l'exécution : rien n'est spéculatif
            return len(data)
        # ETMv3 : encodé en mode cycle-accurate
        data = Etm3Encoder(self.image, cycle_accurate=True).encode(events)
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
