"""Plusieurs cœurs dans une session (`--core`, clé `cible.coeurs`, `gdbtrace.cores()`,
sélecteur « Cœur » du GUI).

Deux sources de cœurs :
- **un autre processus du même serveur** (`--core NOM=N`) : QEMU présente chaque cluster
  comme un processus de son gdbstub (mps2-an521 : deux Cortex-M33, le second au numéro 2) ;
  le cœur devient un inférieur du même gdb (`-add-inferior`, `attach N`), avec ses symboles.
  Un seul gdb en all-stop : un cœur qui s'arrête arrête les autres, Go les relance tous
  (`schedule-multiple`), un pas avance le cœur choisi (les autres tournent pendant ce temps).
  Choisir un cœur choisit son thread : registres, pile, variables, pas à pas.
- **un autre serveur gdb** (`--core NOM=HÔTE:PORT`) : deuxième port d'OpenOCD pour un
  microcontrôleur à deux cœurs différents (3334 pour le second), un autre ST-LINK_gdbserver… ;
  ni QEMU ni OpenOCD n'ont le mode non-stop qu'exigerait un seul gdb pour deux connexions
  (« does not support multi-target resumption ») : le cœur a sa propre `Session` (son gdb),
  et Go / Break visent le cœur choisi ou tous (`go_all`, `halt_all`).

Le premier cœur est celui de la session (`--core NOM` sans source le nomme ; défaut
« cœur 0 »). La trace (ITM, ETM, MTB), le chrono et les compteurs restent ceux du cœur
principal. `ELF` après une virgule : les symboles du cœur (défaut : l'ELF de la session).
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

_SPEC = re.compile(r"^(?P<name>[^=,]+?)(?:=(?P<source>[^,]+))?(?:,(?P<elf>.+))?$")


class CoreError(ValueError):
    pass


@dataclass
class CoreSpec:
    """Un `--core` : NOM[=SOURCE][,ELF]. SOURCE : numéro de processus du même serveur, ou
    HÔTE:PORT d'un autre serveur gdb ; vide : le cœur de la session."""
    name: str
    process: int | None = None
    remote: str | None = None
    elf: str | None = None

    @property
    def main(self) -> bool:
        return self.process is None and self.remote is None

    def text(self) -> str:
        src = f"={self.process}" if self.process is not None else f"={self.remote}" if self.remote else ""
        return f"{self.name}{src}" + (f",{self.elf}" if self.elf else "")


def parse_core(text: str) -> CoreSpec:
    m = _SPEC.match(text.strip())
    if not m or not m.group("name").strip():
        raise CoreError(f"--core {text} : NOM[=SOURCE][,ELF] attendu (SOURCE : numéro de "
                        "processus du même serveur, ou HÔTE:PORT d'un autre serveur gdb)")
    spec = CoreSpec(m.group("name").strip(), elf=m.group("elf"))
    src = (m.group("source") or "").strip()
    if src.isdigit():
        spec.process = int(src)
    elif src:
        host, _, port = src.rpartition(":")
        if not port.isdigit():
            raise CoreError(f"--core {text} : source « {src} » : numéro de processus ou HÔTE:PORT")
        spec.remote = f"{host or '127.0.0.1'}:{port}"
    return spec


def check_cores(texts: list[str]) -> list[str]:
    """Erreurs des options --core (règles entre options : `validate()`)."""
    errors, names, sources, mains = [], set(), set(), 0
    for t in texts:
        try:
            spec = parse_core(t)
        except CoreError as exc:
            errors.append(str(exc))
            continue
        if spec.name in names:
            errors.append(f"--core : nom « {spec.name} » donné deux fois")
        names.add(spec.name)
        if spec.main:
            mains += 1
            if spec.elf:
                errors.append(f"--core {t} : le cœur de la session a l'ELF de la session")
        else:
            src = spec.process if spec.process is not None else spec.remote
            if src in sources or spec.process == 1:
                errors.append(f"--core {t} : source déjà prise"
                              + (" (le processus 1 est le cœur de la session)" if spec.process == 1 else ""))
            sources.add(src)
        if spec.elf and not os.path.exists(spec.elf):
            errors.append(f"--core {t} : ELF introuvable : {spec.elf}")
    if mains > 1:
        errors.append("--core : un seul cœur sans source (celui de la session)")
    return errors


@dataclass
class Core:
    name: str
    session: object                      # Session
    thread: str | None = None            # thread du cœur dans une session partagée
    source: str = "session"
    elf: str = ""
    own_session: bool = False            # session à lui (autre serveur gdb)

    def state(self) -> str:
        s = self.session
        return {"stopped": "arrêté", "running": "en marche", "exited": "terminé",
                "disconnected": "déconnecté"}.get(s.state, s.state)

    def as_dict(self) -> dict:
        return {"nom": self.name, "source": self.source, "elf": self.elf, "état": self.state(),
                "thread": self.thread, "session propre": self.own_session}


@dataclass
class CoreSet:
    """Les cœurs d'une session, le cœur choisi (`current`)."""
    cores: list[Core] = field(default_factory=list)
    current: Core | None = None

    @classmethod
    def build(cls, session, specs: list[CoreSpec], gdb: str = "gdb-multiarch",
              log=print) -> "CoreSet":
        """Ajoute à `session` (déjà connectée) les cœurs décrits ; le premier est le sien."""
        main = next((sp for sp in specs if sp.main), None)
        out = cls()
        first = Core(main.name if main else "cœur 0", session, source="session", elf=session.elf or "")
        out.cores.append(first)
        try:
            for sp in specs:
                if sp.main:
                    continue
                out.cores.append(out._add(session, sp, gdb))
                log(f"cœur {sp.name} : {out.cores[-1].source}")
        except BaseException:
            out.close()
            raise
        if any(c.thread is not None for c in out.cores):
            first.thread = session.inferior_thread("i1")
        out.current = first
        return out

    def _add(self, session, sp: CoreSpec, gdb: str) -> Core:
        elf = os.path.abspath(sp.elf) if sp.elf else session.elf
        if sp.process is not None:
            thread = session.add_process(sp.process, elf)
            return Core(sp.name, session, thread, f"processus {sp.process} du même serveur", elf)
        from .session import Session
        from .target import RemoteTarget
        host, _, port = sp.remote.rpartition(":")
        other = Session(gdb)
        try:
            other.connect(elf, RemoteTarget(host=host, gdb_port=int(port), needs_load=False), load=False)
        except BaseException:
            other.disconnect()
            raise
        return Core(sp.name, other, None, f"serveur gdb {sp.remote}", elf, own_session=True)

    # ------------------------------------------------------------------ choix
    def find(self, name: str) -> Core:
        for c in self.cores:
            if c.name == name:
                return c
        if str(name).isdigit() and int(name) < len(self.cores):
            return self.cores[int(name)]
        raise CoreError(f"cœur inconnu : {name} (cœurs : {', '.join(c.name for c in self.cores)})")

    def select(self, name: str) -> Core:
        """Le cœur devient le cœur choisi ; dans une session partagée, son thread devient le
        thread courant de gdb (cible arrêtée)."""
        core = self.find(name)
        if core.thread is not None and core.session.state == "stopped":
            core.session.select_thread(core.thread)
        self.current = core
        return core

    def follow_stop(self, session) -> Core | None:
        """Après un arrêt d'une session partagée : le cœur du thread courant (celui qui s'est
        arrêté, que gdb a choisi) devient le cœur choisi."""
        shared = [c for c in self.cores if c.session is session and c.thread is not None]
        if not shared:
            return None
        reason = getattr(session, "stop_reason", None) or {}
        if reason.get("reason") == "signal-received" and reason.get("signal-name") == "SIGINT" \
                and "bkptno" not in reason and self.current in shared:
            # Break : gdb choisit un thread quelconque ; le cœur choisi reste celui de l'utilisateur
            if self.current.thread != session.current_thread():
                session.select_thread(self.current.thread)
            return self.current
        try:
            tid = session.current_thread()
        except Exception:  # noqa: BLE001 - thread illisible : choix inchangé
            return None
        for c in shared:
            if c.thread == tid:
                self.current = c
                return c
        return None

    @property
    def sessions(self) -> list:
        out = []
        for c in self.cores:
            if c.session not in out:
                out.append(c.session)
        return out

    def go_all(self) -> None:
        for s in self.sessions:
            if s.state == "stopped":
                s.go()

    def halt_all(self) -> None:
        for s in self.sessions:
            if s.state == "running":
                s.halt()

    def close(self) -> None:
        """Ferme les sessions propres aux cœurs (la session principale reste à son
        propriétaire)."""
        for c in self.cores:
            if c.own_session:
                try:
                    c.session.disconnect()
                except Exception:  # noqa: BLE001 - serveur déjà parti
                    pass

    def rows(self) -> list[tuple]:
        """(nom, source, état, choisi) pour la console et le MCP."""
        return [(c.name, c.source, c.state(), c is self.current) for c in self.cores]
