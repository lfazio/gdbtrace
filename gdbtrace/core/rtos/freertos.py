"""« OS awareness » FreeRTOS : tâches, piles, objets du noyau, contexte des tâches.

Principe (comme les variables live) :
1. cible arrêtée, on demande une fois à gdb la disposition des structures du noyau
   (offsets des champs de TCB_t, List_t, Queue_t... d'après le DWARF) et les adresses
   des listes ; la configuration du noyau (champs optionnels) est ainsi respectée ;
2. ensuite, `snapshot()` parcourt les listes avec une simple fonction `read(addr, n)` :
   gdb quand la cible est arrêtée, canal live (OpenOCD/QEMU) quand elle tourne.

En marche, une liste peut être modifiée pendant qu'on la parcourt : le parcours est
borné, détecte les cycles et vérifie que chaque TCB se désigne bien lui-même ; en cas
de doute le snapshot est marqué `incoherent` et l'appelant garde le précédent.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from ..gdb_backend import GdbError

Reader = Callable[[int, int], bytes]

MAX_TASKS = 256
FILL_BYTE = 0xA5          # tskSTACK_FILL_BYTE
SCAN_CHUNK = 256

QUEUE_TYPES = {0: "queue", 1: "mutex", 2: "sémaphore compteur", 3: "sémaphore binaire",
               4: "mutex récursif"}
MUTEX_TYPES = {1, 4}

# Registres sauvegardés sur la pile d'une tâche par le port ARM_CM3 (adresses croissantes) ;
# ARM_CM0 jusqu'à V11.2 aussi. ARM_CM0 depuis V11.3 (portasm.c) : EXC_RETURN d'abord, puis
# r4-r7 et r8-r11 (9 mots).
CM3_SOFT = ["r4", "r5", "r6", "r7", "r8", "r9", "r10", "r11"]
CPUID = 0xE000ED00
ARMV6M = {0xC20, 0xC60}                 # Cortex-M0, M0+ (CPUID.PARTNO)
HW_FRAME = ["r0", "r1", "r2", "r3", "r12", "lr", "pc", "xpsr"]


def detect(symbols) -> bool:
    """FreeRTOS est-il lié dans l'ELF ? (les listes du noyau sont `static` : symboles
    locaux sans type, d'où le test sur `pxCurrentTCB` et une fonction du noyau)."""
    return (symbols is not None and "pxCurrentTCB" in symbols.objects
            and symbols.function("vTaskSwitchContext") is not None)


def _armv6m(session) -> bool:
    """La cible est-elle un Cortex-M0/M0+ (CPUID) ?"""
    try:
        return (session.read_u32(CPUID) >> 4) & 0xFFF in ARMV6M
    except Exception:  # noqa: BLE001 - CPUID illisible : on garde le cadre ARM_CM3
        return False


def _u(data: bytes, off: int, size: int = 4) -> int:
    return int.from_bytes(data[off:off + size], "little")


# ====================================================================== disposition
@dataclass
class FreeRtosLayout:
    ptr: int = 4
    # TCB_t
    tcb_size: int = 0
    tcb_top: int = 0
    tcb_state: int = 0
    tcb_event: int = 0
    tcb_prio: int = 0
    tcb_stack: int = 0
    tcb_name: int = 0
    name_len: int = 0
    tcb_end: int | None = None
    tcb_base_prio: int | None = None
    tcb_number: int | None = None
    tcb_runtime: int | None = None
    runtime_size: int = 4
    # ListItem_t / List_t
    li_next: int = 0
    li_owner: int = 0
    li_container: int = 0
    li_size: int = 0
    list_size: int = 0
    list_count: int = 0
    list_end: int = 0             # adresse relative de xListEnd
    list_end_next: int = 0        # xListEnd.pxNext
    # variables du noyau
    current_tcb: int = 0
    ready: int = 0
    n_prio: int = 0
    delayed1: int = 0
    delayed2: int = 0
    pending: int = 0
    suspended: int | None = None
    termination: int | None = None
    tick_count: int | None = None
    tick_size: int = 4
    num_tasks: int | None = None
    sched_running: int | None = None
    # Queue_t et registre
    q_size: int = 0
    q_wait_send: int = 0
    q_wait_recv: int = 0
    q_msgs: int = 0
    q_length: int = 0
    q_item_size: int = 0
    q_type: int | None = None
    q_holder: int | None = None
    registry: int | None = None
    registry_len: int = 0
    reg_item_size: int = 0
    reg_name: int = 0
    reg_handle: int = 0
    #: port de pile : "CM3" (sans FPU), "CM4F" (EXC_RETURN + registres FP éventuels) ou "CM0"
    #: (ARM_CM0 depuis V11.3 : EXC_RETURN en tête)
    port: str = "CM3"
    #: cache des noms d'objets (chaînes en flash, constantes)
    names: dict[int, str] = field(default_factory=dict, repr=False)

    @classmethod
    def from_session(cls, session, symbols=None) -> "FreeRtosLayout":
        """Interroge gdb (cible arrêtée) sur les types et variables du noyau."""
        def ev(expr: str) -> int:
            return int(session.evaluate(expr).split()[0], 0)

        def off(typ: str, fld: str) -> int:
            """Offset d'un champ. `fld` peut lister des variantes « a|b » : les noms changent
            selon la version de FreeRTOS et les alias de compatibilité (pxContainer est
            pvContainer dans le DWARF quand configENABLE_BACKWARD_COMPATIBILITY le renomme)."""
            last: Exception | None = None
            for name in fld.split("|"):
                try:
                    return ev(f"(unsigned long)&(({typ} *)0)->{name}")
                except GdbError as exc:
                    last = exc
            raise last

        def opt(fn, *a):
            try:
                return fn(*a)
            except (GdbError, ValueError):
                return None

        L = cls()
        L.ptr = ev("sizeof(void *)")
        L.tcb_size = ev("sizeof(TCB_t)")
        L.tcb_top = off("TCB_t", "pxTopOfStack")
        L.tcb_state = off("TCB_t", "xStateListItem|xGenericListItem")   # avant V9 : xGeneric...
        L.tcb_event = off("TCB_t", "xEventListItem")
        L.tcb_prio = off("TCB_t", "uxPriority")
        L.tcb_stack = off("TCB_t", "pxStack")
        L.tcb_name = off("TCB_t", "pcTaskName")
        L.name_len = ev("sizeof(((TCB_t *)0)->pcTaskName)")
        L.tcb_end = opt(off, "TCB_t", "pxEndOfStack")
        L.tcb_base_prio = opt(off, "TCB_t", "uxBasePriority")
        L.tcb_number = opt(off, "TCB_t", "uxTCBNumber")
        L.tcb_runtime = opt(off, "TCB_t", "ulRunTimeCounter")
        if L.tcb_runtime is not None:
            L.runtime_size = ev("sizeof(((TCB_t *)0)->ulRunTimeCounter)")

        L.li_next = off("ListItem_t", "pxNext")
        L.li_owner = off("ListItem_t", "pvOwner")
        L.li_container = off("ListItem_t", "pxContainer|pvContainer")
        L.li_size = ev("sizeof(ListItem_t)")
        L.list_size = ev("sizeof(List_t)")
        L.list_count = off("List_t", "uxNumberOfItems")
        L.list_end = off("List_t", "xListEnd")
        L.list_end_next = off("List_t", "xListEnd.pxNext")

        addr = session.address_of_expr
        L.current_tcb = addr("pxCurrentTCB")
        L.ready = addr("pxReadyTasksLists")
        L.n_prio = ev("sizeof(pxReadyTasksLists) / sizeof(List_t)")
        L.delayed1 = addr("xDelayedTaskList1")
        L.delayed2 = addr("xDelayedTaskList2")
        L.pending = addr("xPendingReadyList")
        L.suspended = opt(addr, "xSuspendedTaskList")
        L.termination = opt(addr, "xTasksWaitingTermination")
        L.tick_count = opt(addr, "xTickCount")
        L.tick_size = opt(ev, "sizeof(xTickCount)") or 4
        L.num_tasks = opt(addr, "uxCurrentNumberOfTasks")
        L.sched_running = opt(addr, "xSchedulerRunning")

        L.q_size = opt(ev, "sizeof(Queue_t)") or 0
        if L.q_size:
            L.q_wait_send = off("Queue_t", "xTasksWaitingToSend")
            L.q_wait_recv = off("Queue_t", "xTasksWaitingToReceive")
            L.q_msgs = off("Queue_t", "uxMessagesWaiting")
            L.q_length = off("Queue_t", "uxLength")
            L.q_item_size = off("Queue_t", "uxItemSize")
            L.q_type = opt(off, "Queue_t", "ucQueueType")
            L.q_holder = opt(off, "Queue_t", "u.xSemaphore.xMutexHolder|pxMutexHolder")
        L.registry = opt(addr, "xQueueRegistry")
        if L.registry is not None:
            L.reg_item_size = ev("sizeof(xQueueRegistry[0])")
            L.registry_len = ev("sizeof(xQueueRegistry)") // L.reg_item_size
            base = L.registry
            L.reg_name = addr("xQueueRegistry[0].pcQueueName") - base
            L.reg_handle = addr("xQueueRegistry[0].xHandle") - base

        if symbols is not None and symbols.function("vPortEnableVFP") is not None:
            L.port = "CM4F"
        elif symbols is not None and symbols.function("vRestoreContextOfFirstTask") is not None \
                and _armv6m(session):
            L.port = "CM0"                   # ARM_CM0 V11.3+ (les ports ARMv8-M ont ce symbole aussi)
        return L

    def kernel_lists(self) -> list[tuple[str, int, str]]:
        """[(nom, adresse, état des tâches qui s'y trouvent)]."""
        out = [(f"ready[{p}]", self.ready + p * self.list_size, "Ready") for p in range(self.n_prio)]
        out += [("delayed1", self.delayed1, "Blocked"), ("delayed2", self.delayed2, "Blocked"),
                ("pending", self.pending, "Ready")]
        if self.suspended is not None:
            out.append(("suspended", self.suspended, "Suspended"))
        if self.termination is not None:
            out.append(("termination", self.termination, "Deleted"))
        return out


# ====================================================================== instantané
@dataclass
class Task:
    tcb: int
    name: str
    state: str                       # Running, Ready, Blocked, Suspended, Deleted
    priority: int
    base_priority: int | None
    number: int | None
    stack_base: int                  # pxStack (adresse basse)
    stack_end: int | None            # pxEndOfStack (adresse haute), si connue
    stack_top: int                   # pxTopOfStack (dernier SP sauvegardé)
    stack_size: int | None           # octets
    stack_used: int | None           # octets utilisés maintenant
    stack_free_min: int | None       # octets jamais utilisés (high-water mark)
    runtime: int | None
    cpu: float | None = None         # % du temps CPU
    waiting_on: str | None = None    # objet attendu (nom de queue, « délai »...)
    list_name: str = ""
    current: bool = False

    @property
    def stack_percent(self) -> float | None:
        if self.stack_size and self.stack_free_min is not None:
            return 100.0 * (self.stack_size - self.stack_free_min) / self.stack_size
        return None


@dataclass
class KernelObject:
    handle: int
    name: str
    kind: str
    count: int                        # messages présents (ou valeur du sémaphore)
    length: int
    item_size: int
    holder: str | None = None         # détenteur d'un mutex
    waiting_send: list[str] = field(default_factory=list)
    waiting_receive: list[str] = field(default_factory=list)


@dataclass
class RtosSnapshot:
    tasks: list[Task]
    objects: list[KernelObject]
    current_tcb: int
    tick: int | None
    scheduler_running: bool | None
    incoherent: str | None = None

    def task(self, name: str) -> Task | None:
        return next((t for t in self.tasks if t.name == name), None)

    @property
    def current(self) -> Task | None:
        return next((t for t in self.tasks if t.current), None)


class Incoherent(Exception):
    """Structures du noyau incohérentes (lues pendant une modification)."""


class _Mem:
    """Lectures d'un snapshot : têtes de listes lues en un bloc, TCB lus une seule fois.

    En marche, moins il y a de lectures pendant le parcours, moins le noyau a de chances
    de modifier les listes entre deux lectures.
    """

    def __init__(self, L: FreeRtosLayout, read: Reader):
        self.L, self.read = L, read
        self.tcbs: dict[int, bytes] = {}
        self.block_addr = 0
        self.block = b""

    def preload_lists(self) -> None:
        addrs = [a for _, a, _ in self.L.kernel_lists()]
        lo, hi = min(addrs), max(addrs) + self.L.list_size
        if hi - lo <= 2048:                     # listes statiques voisines dans tasks.c
            self.block_addr, self.block = lo, self.read(lo, hi - lo)

    def list_head(self, addr: int) -> bytes:
        off = addr - self.block_addr
        if self.block and 0 <= off and off + self.L.list_size <= len(self.block):
            return self.block[off:off + self.L.list_size]
        return self.read(addr, self.L.list_size)

    def tcb(self, addr: int) -> bytes:
        if addr not in self.tcbs:
            self.tcbs[addr] = self.read(addr, self.L.tcb_size)
        return self.tcbs[addr]


def _walk(mem: _Mem, list_addr: int, item_off: int) -> list[int]:
    """Parcourt une liste de tâches. `item_off` : offset dans le TCB de l'élément chaîné
    (xStateListItem pour les listes du noyau, xEventListItem pour les attentes sur objet).
    Retourne les adresses des TCB, après vérification de chaque pvOwner."""
    L = mem.L
    head = mem.list_head(list_addr)
    count = _u(head, L.list_count)
    if count > MAX_TASKS:
        raise Incoherent(f"liste {list_addr:#x} : {count} éléments")
    end = list_addr + L.list_end
    node = _u(head, L.list_end_next, L.ptr)
    out: list[int] = []
    seen = set()
    while node != end:
        if node == 0 or node % 4 or node in seen or len(out) > count:
            raise Incoherent(f"liste {list_addr:#x} : chaînage invalide")
        seen.add(node)
        tcb_addr = node - item_off
        tcb = mem.tcb(tcb_addr)
        if _u(tcb, item_off + L.li_owner, L.ptr) != tcb_addr:
            raise Incoherent(f"liste {list_addr:#x} : élément {node:#x} sans TCB valide")
        out.append(tcb_addr)
        node = _u(tcb, item_off + L.li_next, L.ptr)
    if len(out) != count:
        raise Incoherent(f"liste {list_addr:#x} : {len(out)} éléments au lieu de {count}")
    return out


def _read_cstr(read: Reader, addr: int, maxlen: int = 32) -> str:
    try:
        raw = read(addr, maxlen)
    except Exception:  # noqa: BLE001
        return f"<{addr:#x}>"
    return raw.split(b"\0", 1)[0].decode("latin-1")


def _free_stack(read: Reader, base: int, limit: int) -> int:
    """Octets encore à 0xA5 au fond de la pile (croissance vers le bas : depuis pxStack)."""
    free = 0
    while free < limit:
        n = min(SCAN_CHUNK, limit - free)
        chunk = read(base + free, n)
        for b in chunk:
            if b != FILL_BYTE:
                return free
            free += 1
    return free


class CurrentTaskReader:
    """Nom de la tâche courante (`pxCurrentTCB`), pour le profilage par tâche.

    Une lecture de 4 octets par appel ; le nom de chaque TCB n'est lu qu'une fois.
    """

    def __init__(self, layout: FreeRtosLayout):
        self.L = layout
        self.names: dict[int, str] = {}

    def __call__(self, read: Reader) -> str | None:
        L = self.L
        tcb = _u(read(L.current_tcb, L.ptr), 0, L.ptr)
        if not tcb:
            return None
        name = self.names.get(tcb)
        if name is None:
            raw = read(tcb + L.tcb_name, L.name_len)
            name = self.names[tcb] = raw.split(b"\0", 1)[0].decode("latin-1")
        return name


def snapshot(L: FreeRtosLayout, read: Reader, prev: RtosSnapshot | None = None,
             current_sp: int | None = None, scan_stacks: bool = True,
             attempts: int = 1) -> RtosSnapshot:
    """Lit l'état du noyau. `current_sp` : SP réel de la tâche courante (cible arrêtée).
    `attempts` > 1 : nouvel essai si les listes ont changé pendant la lecture (en marche)."""
    result = None
    for _ in range(max(1, attempts)):
        try:
            return _snapshot(L, read, prev, current_sp, scan_stacks)
        except Incoherent as exc:
            result = RtosSnapshot([], [], 0, None, None, incoherent=str(exc))
    return result


def _snapshot(L, read, prev, current_sp, scan_stacks) -> RtosSnapshot:
    mem = _Mem(L, read)
    current = _u(read(L.current_tcb, L.ptr), 0, L.ptr)
    if current == 0:
        # aucune tâche créée : les listes du noyau ne sont même pas initialisées
        return RtosSnapshot([], [], 0, None, False)
    # --- phase critique : le moins de lectures possible
    mem.preload_lists()
    found: list[tuple[int, str, str]] = []      # (TCB, liste, état)
    for list_name, list_addr, state in L.kernel_lists():
        for tcb_addr in _walk(mem, list_addr, L.tcb_state):
            found.append((tcb_addr, list_name, state))
    if len({a for a, _, _ in found}) != len(found):
        raise Incoherent("TCB présent dans deux listes")
    if len(found) > MAX_TASKS:
        raise Incoherent("trop de tâches")
    objects_raw = _objects_raw(mem) if L.registry is not None and L.q_size else []

    # --- hors phase critique : compteurs, piles, noms
    tick = _u(read(L.tick_count, L.tick_size), 0, L.tick_size) if L.tick_count else None
    running = bool(_u(read(L.sched_running, 4), 0)) if L.sched_running else None
    tasks: dict[int, Task] = {}
    for tcb_addr, list_name, state in found:
        tasks[tcb_addr] = _task_from_tcb(L, read, tcb_addr, mem.tcbs[tcb_addr], state,
                                         list_name, scan_stacks)
    tcbs = mem.tcbs

    # liste d'événements dans laquelle attend chaque tâche (0 = aucune)
    event_list = {a: _u(tcbs[a], L.tcb_event + L.li_container, L.ptr) for a in tasks}

    # états fins : tâche courante, attente sans délai (liste suspended + liste d'événements)
    for addr, t in tasks.items():
        if addr == current:
            t.state, t.current = "Running", True
        elif t.list_name == "suspended" and event_list[addr]:
            t.state = "Blocked"
        if t.state == "Blocked":
            t.waiting_on = f"liste {event_list[addr]:#x}" if event_list[addr] else "délai"
        if t.current and current_sp is not None and t.stack_end is not None:
            t.stack_top = current_sp
            t.stack_used = t.stack_end + 4 - current_sp

    objects = _objects(L, read, objects_raw, tasks)
    # nom de l'objet attendu : une tâche bloquée sur un sémaphore/mutex attend en « réception »
    waits = {}
    for o in objects:
        is_queue = o.kind == "queue"
        waits[o.handle + L.q_wait_recv] = f"{o.name} (réception)" if is_queue else o.name
        waits[o.handle + L.q_wait_send] = f"{o.name} (envoi)" if is_queue else o.name
    for addr, t in tasks.items():
        if t.state == "Blocked" and event_list[addr] in waits:
            t.waiting_on = waits[event_list[addr]]

    _cpu(tasks, prev)
    ordered = sorted(tasks.values(), key=lambda t: (-t.priority, t.number or 0, t.name))
    return RtosSnapshot(ordered, objects, current, tick, running)


def _task_from_tcb(L, read, addr, tcb, state, list_name, scan_stacks) -> Task:
    name = tcb[L.tcb_name:L.tcb_name + L.name_len].split(b"\0", 1)[0].decode("latin-1")
    base = _u(tcb, L.tcb_stack, L.ptr)
    top = _u(tcb, L.tcb_top, L.ptr)
    end = _u(tcb, L.tcb_end, L.ptr) if L.tcb_end is not None else None
    size = used = free = None
    if end is not None and end >= base:
        size = end + 4 - base
        used = end + 4 - top if base <= top <= end + 4 else None
        if scan_stacks:
            free = _free_stack(read, base, size)
    elif scan_stacks:
        free = _free_stack(read, base, max(0, top - base))
    return Task(
        tcb=addr, name=name, state=state,
        priority=_u(tcb, L.tcb_prio),
        base_priority=_u(tcb, L.tcb_base_prio) if L.tcb_base_prio is not None else None,
        number=_u(tcb, L.tcb_number) if L.tcb_number is not None else None,
        stack_base=base, stack_end=end, stack_top=top, stack_size=size,
        stack_used=used, stack_free_min=free,
        runtime=_u(tcb, L.tcb_runtime, L.runtime_size) if L.tcb_runtime is not None else None,
        list_name=list_name,
    )


def _cpu(tasks: dict[int, Task], prev: RtosSnapshot | None) -> None:
    """% CPU : depuis le snapshot précédent si fourni, sinon depuis le démarrage."""
    if any(t.runtime is None for t in tasks.values()):
        return
    before = {t.tcb: t.runtime for t in prev.tasks} if prev and not prev.incoherent else {}
    deltas = {}
    for a, t in tasks.items():
        old = before.get(a)
        deltas[a] = (t.runtime - old) & 0xFFFFFFFF if old is not None else t.runtime
    total = sum(deltas.values())
    if total <= 0:
        return
    for a, t in tasks.items():
        t.cpu = 100.0 * deltas[a] / total


def _objects_raw(mem: _Mem) -> list[tuple[int, int, bytes, list[int], list[int]]]:
    """Phase critique : (handle, ptr du nom, Queue_t brut, TCB en attente d'envoi / de réception)."""
    L = mem.L
    reg = mem.read(L.registry, L.registry_len * L.reg_item_size)
    out = []
    for i in range(L.registry_len):
        off = i * L.reg_item_size
        handle = _u(reg, off + L.reg_handle, L.ptr)
        if not handle:
            continue
        q = mem.read(handle, L.q_size)
        # les têtes des listes d'attente sont dans le Queue_t déjà lu
        mem.block_addr, mem.block = handle, q
        send = _walk(mem, handle + L.q_wait_send, L.tcb_event)
        recv = _walk(mem, handle + L.q_wait_recv, L.tcb_event)
        out.append((handle, _u(reg, off + L.reg_name, L.ptr), q, send, recv))
    return out


def _objects(L, read, raw, tasks: dict[int, Task]) -> list[KernelObject]:
    out = []
    for handle, name_ptr, q, send, recv in raw:
        qtype = q[L.q_type] if L.q_type is not None else 0
        name = L.names.get(name_ptr)
        if name is None:                      # noms en flash : lus une seule fois
            name = L.names[name_ptr] = _read_cstr(read, name_ptr)
        obj = KernelObject(handle, name, QUEUE_TYPES.get(qtype, f"type {qtype}"),
                           count=_u(q, L.q_msgs), length=_u(q, L.q_length),
                           item_size=_u(q, L.q_item_size))
        if qtype in MUTEX_TYPES and L.q_holder is not None:
            holder = _u(q, L.q_holder, L.ptr)
            obj.holder = tasks[holder].name if holder in tasks else (f"{holder:#x}" if holder else None)
        obj.waiting_send = [tasks[a].name if a in tasks else f"{a:#x}" for a in send]
        obj.waiting_receive = [tasks[a].name if a in tasks else f"{a:#x}" for a in recv]
        out.append(obj)
    return out


# ====================================================================== contexte des tâches
def task_registers(L: FreeRtosLayout, task: Task, read: Reader) -> dict[str, int]:
    """Registres sauvegardés sur la pile d'une tâche **non courante** (port ARM_CM3, CM4F ou
    CM0)."""
    top = task.stack_top
    if L.port == "CM0":                  # EXC_RETURN, r4-r11
        soft = read(top, 9 * 4)
        regs = {n: _u(soft, 4 + i * 4) for i, n in enumerate(CM3_SOFT)}
        pos, fp_frame = top + 36, False
    elif L.port == "CM4F":
        soft = read(top, 9 * 4)
        regs = {n: _u(soft, i * 4) for i, n in enumerate(CM3_SOFT)}
        exc_return = _u(soft, 32)
        pos = top + 36 + (0 if exc_return & 0x10 else 16 * 4)   # s16-s31 si contexte FP
        fp_frame = not (exc_return & 0x10)
    else:
        soft = read(top, 8 * 4)
        regs = {n: _u(soft, i * 4) for i, n in enumerate(CM3_SOFT)}
        pos, fp_frame = top + 32, False
    hw = read(pos, 8 * 4)
    regs.update({n: _u(hw, i * 4) for i, n in enumerate(HW_FRAME)})
    sp = pos + 32 + (18 * 4 if fp_frame else 0)
    if regs["xpsr"] & (1 << 9):        # alignement 8 octets ajouté par le matériel
        sp += 4
    regs["sp"] = sp
    return regs


CORE_REGS = ["r0", "r1", "r2", "r3", "r4", "r5", "r6", "r7", "r8", "r9", "r10", "r11", "r12",
             "sp", "lr", "pc"]
TASK_EXIT_ARTIFACTS = {"prvTaskExitError", "pxPortInitialiseStack"}
TASK_RETURN = "prvTaskExitError"      # portTASK_RETURN_ADDRESS : LR initial de chaque tâche


def task_context(session, layout: FreeRtosLayout, task: Task) -> tuple[dict[str, int], list]:
    """(registres, frames) d'une tâche, cible arrêtée.

    - tâche courante : registres réels du cœur ;
    - avec OpenOCD `--rtos`, gdb connaît les tâches comme threads : on sélectionne le thread
      (chemin non testé faute de sonde) ;
    - sinon : contexte relu sur la pile de la tâche, pile d'appels par chargement
      temporaire de ses registres (toujours restaurés).
    """
    if task.current:
        return dict(session.registers()), session.backtrace()
    thread = session.thread_for(task.name)
    if thread is not None:
        return session.thread_context(thread)
    regs = task_registers(layout, task, session.read_memory)
    return regs, task_backtrace(session, regs)


def _task_return_address(session) -> int | None:
    """Adresse de prvTaskExitError (bit Thumb retiré), ou None si gdb ne la connaît pas."""
    try:
        return session.address_of_expr(TASK_RETURN) & ~1
    except Exception:  # noqa: BLE001 - configTASK_RETURN_ADDRESS, symbole absent…
        return None


def task_backtrace(session, regs: dict[str, int]) -> list:
    """Pile d'appels d'une tâche : charge ses registres dans gdb le temps de dérouler la pile,
    puis restaure **toujours** les registres d'origine. Cible arrêtée uniquement."""
    if session.state != "stopped":
        raise RuntimeError("la cible doit être arrêtée")
    saved = {r: session.register(r) for r in CORE_REGS}
    try:
        for r in CORE_REGS:
            if r in regs:
                session.write_register(r, regs[r])
        frames = session.backtrace()
        # La pile initiale d'une tâche porte une adresse de retour factice
        # (prvTaskExitError) : gdb en tire une frame sans signification, on la retire.
        # gdb nomme une frame appelante d'après l'octet qui précède l'adresse de retour :
        # c'est la fonction placée avant prvTaskExitError (pxPortInitialiseStack en ARM_CM3,
        # vPortSetupTimerInterrupt en ARM_CM0) ; d'où la comparaison par adresse.
        exit_pc = _task_return_address(session)
        while frames and (frames[-1].func in TASK_EXIT_ARTIFACTS
                          or (exit_pc is not None and frames[-1].pc == exit_pc)):
            frames.pop()
        return frames
    finally:
        for r in CORE_REGS:
            session.write_register(r, saved[r])
        session.cli("flushregs")
        if session.register("pc") != saved["pc"] or session.register("sp") != saved["sp"]:
            raise RuntimeError("registres non restaurés après la pile d'appels de la tâche")
