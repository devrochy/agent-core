"""Estado de la conversación: tareas *event-sourced* (ADR-0114 §1, HU-AOS-04).

Una tarea es una instancia de un procedimiento (`procedure/1`) dentro de una conversación.

- `task_events` es la fuente de verdad (append-only, `prevent_mutation()`).
- `conversation_tasks` es su proyección. Se escribe SOLO aquí, en la misma transacción que el
  evento (`iniciar` y `registrar` no hacen commit: lo hace quien llama, y si falla no queda
  ni el evento ni la proyección).
- La proyección se calcula con un único reductor puro (`aplicar`), el mismo que usa la
  reconstrucción (`reconstruir`), así que reconstruir da exactamente lo guardado.

Cada evento lleva en `data.schema_version` la versión del procedimiento vigente cuando se
escribió. El reductor la comprueba contra la proyección: entre versiones, solo un evento
`revalidated` cambia la versión (ADR-0117 §4), y la reconstrucción funciona a través de él.

Forma de `slots` en la proyección (contrato de hecho para el motor, el panel y las evals):
`{nombre: {"value", "source", "actor", "seq", "turn_id", "confirmed", "data_class"}}`;
`valores()` devuelve solo los valores.

La confirmación pendiente de un procedimiento vive en la tarea (`pending_confirmation`), nunca
en `conversations.state.pending_confirmation`, que queda para acciones fuera de procedimiento.
Las tareas son registro del negocio, como `messages` (ADR-0053): la purga por cliente no las
toca y solo `purge_tenant` las borra.

El motor que decide QUÉ evento escribir está en `core/kernel/procedures/ejecucion.py`
(HU-AOS-05); aquí se garantiza que lo que se
escribe es coherente y queda junto con su proyección.
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any

from agentcore_contracts.procedure import (
    ACTION_PATTERN,
    NAME_PATTERN,
    PROCEDURE_ID_PATTERN,
    SEMVER_PATTERN,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.db.models import ConversationTask, TaskEvent
from core.shared.logger import get_logger

log = get_logger(__name__)

TIPOS = frozenset(
    {
        "started", "slot_set", "slot_cleared", "stage_entered", "precondition_failed",
        "action_requested", "action_succeeded", "action_failed", "confirmation_requested",
        "confirmed", "suspended", "resumed", "owner_changed", "revalidated", "episode_expired",
        "completed", "cancelled", "failed", "escalated",
    }
)
ACTORES = frozenset({"llm", "kernel", "tool", "human", "backoffice"})
ABIERTOS = frozenset({"active", "suspended"})
TERMINALES = frozenset({"completed", "cancelled", "failed", "escalated"})
CLASES_DE_DATO = frozenset({"public", "pii", "payment"})
EPISODE_EXPIRED = "episode_expired"
# Tope del valor de un dato serializado: una tarea guarda datos, no documentos.
MAX_VALOR_BYTES = 4096
# Tope del `data` completo de un evento: `task_events` es append-only y no se puede redactar.
MAX_DATA_BYTES = 8192
# Tope de la pila (la columna es smallint; el `maxStack` real del KernelSpec es mucho menor).
MAX_STACK_POSITION = 100

# Quién puede emitir cada tipo de evento. Las transiciones de la tarea son del kernel (el motor
# decide sobre lo que propone el LLM); el LLM solo fija datos, pide acciones o cancela por pedido
# del cliente; una persona del equipo actúa desde la bandeja.
ACTORES_POR_TIPO: dict[str, frozenset[str]] = {
    "started": frozenset({"kernel", "human", "backoffice"}),
    "slot_set": frozenset({"llm", "tool", "kernel", "human", "backoffice"}),
    "slot_cleared": frozenset({"llm", "kernel", "human"}),
    "stage_entered": frozenset({"kernel"}),
    "precondition_failed": frozenset({"kernel"}),
    "action_requested": frozenset({"llm", "kernel"}),
    "action_succeeded": frozenset({"tool", "kernel"}),
    "action_failed": frozenset({"tool", "kernel"}),
    "confirmation_requested": frozenset({"kernel"}),
    "confirmed": frozenset({"kernel", "human"}),
    "suspended": frozenset({"kernel", "human"}),
    "resumed": frozenset({"kernel", "human"}),
    "owner_changed": frozenset({"kernel", "human"}),
    "revalidated": frozenset({"kernel"}),
    "episode_expired": frozenset({"kernel"}),
    "completed": frozenset({"kernel", "backoffice"}),
    "cancelled": frozenset({"llm", "kernel", "human"}),
    "failed": frozenset({"kernel", "tool"}),
    "escalated": frozenset({"kernel", "human"}),
}
# Actores que pueden dar un dato por confirmado al fijarlo (nunca el LLM ni una tool).
_CONFIRMAN = frozenset({"kernel", "human", "backoffice"})

_NOMBRE = re.compile(NAME_PATTERN)
_ACCION = re.compile(ACTION_PATTERN)
_PROCEDIMIENTO = re.compile(PROCEDURE_ID_PATTERN)
_SEMVER = re.compile(SEMVER_PATTERN)
_MOTIVO = re.compile(r"^[a-z_]{1,40}$")  # = ck_conversation_tasks_status_reason (0074)
# Origen de un dato: el `setBy` del contrato, o el cliente por el LLM, o una persona del equipo.
_ORIGEN = re.compile(
    r"^(action:[a-zA-Z0-9_]{1,80}"
    r"|interactive:[a-z0-9_-]{1,30}:"
    r"|content:(location|image|document|audio)"
    r"|llm|human)$"
)
_CLASES_OBJETIVO = {"stage": _NOMBRE, "slot": _NOMBRE, "action": _ACCION}


def _origen_permitido(actor: str, origen: str) -> bool:
    """El origen de un dato debe corresponder a quien lo escribe: el LLM no firma por una acción
    ni por una persona, y una tool solo firma por su acción."""
    if actor == "llm":
        return origen == "llm" or origen.startswith(("interactive:", "content:"))
    if actor == "tool":
        return origen.startswith("action:")
    if actor in {"human", "backoffice"}:
        return origen == "human"
    return True  # kernel: resuelve botones, contenido y resultados de acciones


class TransicionInvalida(ValueError):
    """El evento no se puede aplicar a la tarea en su estado actual (fail-closed)."""


class TareaNoEncontrada(LookupError):
    """La tarea no existe en el negocio actual."""


@dataclass(frozen=True)
class Evento:
    seq: int
    type: str
    data: Mapping[str, Any]
    actor: str
    created_at: datetime
    turn_id: uuid.UUID | None = None


@dataclass(frozen=True)
class Proyeccion:
    """Estado vigente de una tarea: lo mismo que guarda `conversation_tasks`."""

    procedure_id: str
    procedure_version: str
    status: str
    stage: str
    stack_position: int
    last_seq: int
    created_at: datetime
    updated_at: datetime
    slots: dict[str, dict[str, Any]] = field(default_factory=dict)
    owner_instance_id: uuid.UUID | None = None
    parent_task_id: uuid.UUID | None = None
    started_turn_id: uuid.UUID | None = None
    pending_confirmation: dict[str, Any] | None = None
    status_reason: str | None = None
    completed_at: datetime | None = None

    @property
    def abierta(self) -> bool:
        return self.status in ABIERTOS


def valores(slots: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    """`{nombre: valor}` de los datos de una tarea."""
    return {nombre: meta.get("value") for nombre, meta in slots.items()}


# ---------------------------------------------------------------- reductor (puro)


def _error(evento: Evento, motivo: str) -> TransicionInvalida:
    return TransicionInvalida(f"{evento.type} (seq {evento.seq}): {motivo}")


def _uuid(evento: Evento, valor: Any, campo: str) -> uuid.UUID | None:
    if valor is None:
        return None
    try:
        return uuid.UUID(str(valor))
    except ValueError:
        raise _error(evento, f"{campo} no es un uuid") from None


def _texto(evento: Evento, data: Mapping[str, Any], campo: str, patron: re.Pattern[str]) -> str:
    valor = data.get(campo)
    if not isinstance(valor, str) or not patron.fullmatch(valor):
        raise _error(evento, f"falta {campo} válido")
    return valor


def _motivo(evento: Evento, data: Mapping[str, Any], *, obligatorio: bool) -> str | None:
    motivo = data.get("reason")
    if motivo is None and not obligatorio:
        return None
    if not isinstance(motivo, str) or not _MOTIVO.fullmatch(motivo):
        raise _error(evento, "reason debe ser un código [a-z_]")
    return motivo


def _objetivo(evento: Evento, data: Mapping[str, Any]) -> dict[str, str]:
    objetivo = data.get("target")
    if not isinstance(objetivo, Mapping):
        raise _error(evento, "falta target")
    clase, nombre = objetivo.get("kind"), objetivo.get("name")
    patron = _CLASES_OBJETIVO.get(clase) if isinstance(clase, str) else None
    if patron is None or not isinstance(nombre, str) or not patron.fullmatch(nombre):
        raise _error(evento, "target inválido")
    return {"kind": str(clase), "name": nombre}


def _cerrar(p: Proyeccion, evento: Evento, status: str, motivo: str | None) -> Proyeccion:
    return replace(
        p, status=status, status_reason=motivo, completed_at=evento.created_at, pending_confirmation=None
    )


def _iniciar(evento: Evento) -> Proyeccion:
    data = evento.data
    if evento.seq != 1:
        raise _error(evento, "started debe ser el evento 1")
    version = _texto(evento, data, "procedure_version", _SEMVER)
    posicion = data.get("stack_position", 0)
    if isinstance(posicion, bool) or not isinstance(posicion, int) or not 0 <= posicion <= MAX_STACK_POSITION:
        raise _error(evento, "stack_position inválido")
    return Proyeccion(
        procedure_id=_texto(evento, data, "procedure_id", _PROCEDIMIENTO),
        procedure_version=version,
        status="active" if posicion == 0 else "suspended",
        stage=_texto(evento, data, "stage", _NOMBRE),
        stack_position=posicion,
        last_seq=1,
        created_at=evento.created_at,
        updated_at=evento.created_at,
        owner_instance_id=_uuid(evento, data.get("owner_instance_id"), "owner_instance_id"),
        parent_task_id=_uuid(evento, data.get("parent_task_id"), "parent_task_id"),
        started_turn_id=evento.turn_id,
    )


def aplicar(p: Proyeccion | None, evento: Evento) -> Proyeccion:
    """Aplica un evento a la proyección. Puro: no toca la base ni el reloj."""
    if evento.type not in TIPOS:
        raise _error(evento, "tipo desconocido")
    if evento.actor not in ACTORES:
        raise _error(evento, "actor desconocido")
    if evento.actor not in ACTORES_POR_TIPO[evento.type]:
        raise _error(evento, f"el actor {evento.actor} no puede emitir este evento")
    data = evento.data
    if not isinstance(data, Mapping):
        raise _error(evento, "data debe ser un objeto")
    if len(json.dumps(data, ensure_ascii=False, default=str).encode("utf-8")) > MAX_DATA_BYTES:
        raise _error(evento, "data demasiado grande")

    if evento.type == "started":
        if p is not None:
            raise _error(evento, "la tarea ya empezó")
        nueva = _iniciar(evento)
        if data.get("schema_version") != nueva.procedure_version:
            raise _error(evento, "schema_version no coincide con la versión del procedimiento")
        return nueva

    if p is None:
        raise _error(evento, "la tarea no empezó")
    if evento.seq != p.last_seq + 1:
        raise _error(evento, f"se esperaba seq {p.last_seq + 1}")
    if p.status in TERMINALES:
        raise _error(evento, f"la tarea ya está {p.status}")
    version_del_evento = data.get("to_version") if evento.type == "revalidated" else p.procedure_version
    if data.get("schema_version") != version_del_evento:
        raise _error(evento, "schema_version no coincide con la versión del procedimiento")

    p = replace(p, last_seq=evento.seq, updated_at=evento.created_at)
    pendiente = p.pending_confirmation
    tipo = evento.type

    if tipo == "slot_set":
        nombre = _texto(evento, data, "slot", _NOMBRE)
        if "value" not in data or data["value"] is None:
            raise _error(evento, "slot_set sin valor (usa slot_cleared)")
        if len(json.dumps(data["value"], ensure_ascii=False, default=str).encode("utf-8")) > MAX_VALOR_BYTES:
            raise _error(evento, "valor demasiado grande")
        clase = data.get("data_class")
        if clase not in CLASES_DE_DATO:
            raise _error(evento, "falta data_class")
        origen = _texto(evento, data, "source", _ORIGEN)
        if not _origen_permitido(evento.actor, origen):
            raise _error(evento, f"el actor {evento.actor} no puede firmar el origen {origen}")
        confirmado = data.get("confirmed", False)
        if not isinstance(confirmado, bool) or (confirmado and evento.actor not in _CONFIRMAN):
            raise _error(evento, "solo el kernel o una persona dan un dato por confirmado")
        slots = dict(p.slots)
        slots[nombre] = {
            "value": data["value"],
            "source": origen,
            "actor": evento.actor,
            "seq": evento.seq,
            "turn_id": str(evento.turn_id) if evento.turn_id else None,
            "confirmed": confirmado,
            "data_class": clase,
        }
        # Un valor nuevo invalida la confirmación que se había pedido de ese dato.
        if pendiente and pendiente.get("kind") == "slot" and pendiente.get("name") == nombre:
            pendiente = None
        return replace(p, slots=slots, pending_confirmation=pendiente)

    if tipo == "slot_cleared":
        nombre = _texto(evento, data, "slot", _NOMBRE)
        if nombre not in p.slots:
            raise _error(evento, f"el dato {nombre} no está")
        slots = {k: v for k, v in p.slots.items() if k != nombre}
        if pendiente and pendiente.get("kind") == "slot" and pendiente.get("name") == nombre:
            pendiente = None
        return replace(p, slots=slots, pending_confirmation=pendiente)

    if tipo == "stage_entered":
        if data.get("from_stage") != p.stage:
            raise _error(evento, "from_stage no es la etapa vigente")
        return replace(p, stage=_texto(evento, data, "to_stage", _NOMBRE), pending_confirmation=None)

    if tipo == "confirmation_requested":
        objetivo = _objetivo(evento, data)
        if objetivo["kind"] == "slot" and objetivo["name"] not in p.slots:
            raise _error(evento, "no se puede confirmar un dato que no está")
        return replace(p, pending_confirmation={**objetivo, "seq": evento.seq})

    if tipo == "confirmed":
        objetivo = _objetivo(evento, data)
        if not pendiente or {"kind": pendiente.get("kind"), "name": pendiente.get("name")} != objetivo:
            raise _error(evento, "no hay una confirmación pendiente de eso")
        slots = p.slots
        if objetivo["kind"] == "slot":
            slots = dict(p.slots)
            slots[objetivo["name"]] = {**slots[objetivo["name"]], "confirmed": True}
        return replace(p, slots=slots, pending_confirmation=None)

    if tipo == "suspended":
        posicion = data.get("stack_position")
        if p.status != "active":
            raise _error(evento, "solo se pausa una tarea activa")
        if isinstance(posicion, bool) or not isinstance(posicion, int) or not 1 <= posicion <= MAX_STACK_POSITION:
            raise _error(evento, "stack_position debe ser > 0")
        return replace(p, status="suspended", stack_position=posicion)

    if tipo == "resumed":
        if p.status != "suspended":
            raise _error(evento, "solo se retoma una tarea en pausa")
        return replace(p, status="active", stack_position=0)

    if tipo == "owner_changed":
        return replace(p, owner_instance_id=_uuid(evento, data.get("to_instance_id"), "to_instance_id"))

    if tipo == "revalidated":
        if data.get("from_version") != p.procedure_version:
            raise _error(evento, "from_version no es la versión vigente")
        version = _texto(evento, data, "to_version", _SEMVER)
        descartados = data.get("dropped_slots", [])
        if not isinstance(descartados, list) or not all(isinstance(d, str) for d in descartados):
            raise _error(evento, "dropped_slots inválido")
        slots = {k: v for k, v in p.slots.items() if k not in set(descartados)}
        return replace(
            p,
            procedure_version=version,
            stage=_texto(evento, data, "to_stage", _NOMBRE),
            slots=slots,
            pending_confirmation=None,
        )

    if tipo == EPISODE_EXPIRED:
        return _cerrar(p, evento, "cancelled", EPISODE_EXPIRED)

    if tipo == "completed":
        return _cerrar(p, evento, "completed", _motivo(evento, data, obligatorio=False))

    if tipo in {"cancelled", "failed", "escalated"}:
        return _cerrar(p, evento, tipo, _motivo(evento, data, obligatorio=True))

    # action_requested / action_succeeded / action_failed / precondition_failed: historia de la
    # tarea (auditoría, evals, verificador); no cambian la proyección más allá de la secuencia.
    _texto(evento, data, "action", _ACCION)
    return p


def reducir(eventos: Iterable[Evento]) -> Proyeccion:
    """Proyección desde los eventos de una tarea, en orden de `seq`."""
    p: Proyeccion | None = None
    for evento in eventos:
        p = aplicar(p, evento)
    if p is None:
        raise TransicionInvalida("una tarea sin eventos no tiene proyección")
    return p


# ---------------------------------------------------------------- persistencia


def proyeccion_de(fila: ConversationTask) -> Proyeccion:
    return Proyeccion(
        procedure_id=fila.procedure_id,
        procedure_version=fila.procedure_version,
        status=fila.status,
        stage=fila.stage,
        stack_position=fila.stack_position,
        last_seq=fila.last_seq,
        created_at=fila.created_at,
        updated_at=fila.updated_at,
        slots=dict(fila.slots or {}),
        owner_instance_id=fila.owner_instance_id,
        parent_task_id=fila.parent_task_id,
        started_turn_id=fila.started_turn_id,
        pending_confirmation=fila.pending_confirmation,
        status_reason=fila.status_reason,
        completed_at=fila.completed_at,
    )


def _volcar(fila: ConversationTask, p: Proyeccion) -> None:
    fila.procedure_id = p.procedure_id
    fila.procedure_version = p.procedure_version
    fila.status = p.status
    fila.stage = p.stage
    fila.stack_position = p.stack_position
    fila.last_seq = p.last_seq
    fila.slots = p.slots
    fila.owner_instance_id = p.owner_instance_id
    fila.parent_task_id = p.parent_task_id
    fila.started_turn_id = p.started_turn_id
    fila.pending_confirmation = p.pending_confirmation
    fila.status_reason = p.status_reason
    fila.completed_at = p.completed_at
    fila.created_at = p.created_at
    fila.updated_at = p.updated_at


def _evento_de(fila: TaskEvent) -> Evento:
    return Evento(
        seq=fila.seq, type=fila.type, data=fila.data or {}, actor=fila.actor,
        created_at=fila.created_at, turn_id=fila.turn_id,
    )


def _ahora(now: datetime | None) -> datetime:
    return now or datetime.now(UTC)


def _mismo_negocio(tenant_id: uuid.UUID) -> None:
    """El `tenant_id` explícito debe ser el del contexto: RLS ya lo exige al escribir, pero un
    desajuste es un error del llamador y debe fallar fuerte, no como "no encontrada"."""
    from core.shared.tenant import get_current_tenant_id

    actual = get_current_tenant_id()
    if actual is not None and actual != tenant_id:
        raise TareaNoEncontrada("la tarea no es del negocio actual")


async def iniciar(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    conversation_id: uuid.UUID,
    procedure_id: str,
    procedure_version: str,
    stage: str,
    actor: str,
    stack_position: int = 0,
    owner_instance_id: uuid.UUID | None = None,
    parent_task_id: uuid.UUID | None = None,
    turn_id: uuid.UUID | None = None,
    now: datetime | None = None,
) -> ConversationTask:
    """Crea la tarea con su evento `started` (seq 1). No hace commit.

    Una sub-tarea solo cuelga de una tarea de la MISMA conversación (la FK compuesta solo
    garantiza el mismo negocio).
    """
    _mismo_negocio(tenant_id)
    if parent_task_id is not None:
        padre = (
            await session.execute(
                select(ConversationTask.id).where(
                    ConversationTask.tenant_id == tenant_id,
                    ConversationTask.conversation_id == conversation_id,
                    ConversationTask.id == parent_task_id,
                )
            )
        ).scalar_one_or_none()
        if padre is None:
            raise TareaNoEncontrada("la tarea padre no es de esta conversación")
    data: dict[str, Any] = {
        "schema_version": procedure_version,
        "procedure_id": procedure_id,
        "procedure_version": procedure_version,
        "stage": stage,
        "stack_position": stack_position,
        "owner_instance_id": str(owner_instance_id) if owner_instance_id else None,
        "parent_task_id": str(parent_task_id) if parent_task_id else None,
    }
    evento = Evento(seq=1, type="started", data=data, actor=actor, created_at=_ahora(now), turn_id=turn_id)
    p = aplicar(None, evento)
    fila = ConversationTask(id=uuid.uuid4(), tenant_id=tenant_id, conversation_id=conversation_id)
    _volcar(fila, p)
    session.add(fila)
    await session.flush()  # la FK del evento apunta a la proyección
    session.add(_fila_evento(tenant_id, fila.id, evento))
    await session.flush()
    log.info("task_started", task_id=str(fila.id), procedure_id=procedure_id, version=procedure_version)
    return fila


def _fila_evento(tenant_id: uuid.UUID, task_id: uuid.UUID, evento: Evento) -> TaskEvent:
    return TaskEvent(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        task_id=task_id,
        seq=evento.seq,
        type=evento.type,
        data=dict(evento.data),
        actor=evento.actor,
        turn_id=evento.turn_id,
        created_at=evento.created_at,
    )


async def registrar(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    conversation_id: uuid.UUID,
    task_id: uuid.UUID,
    tipo: str,
    data: Mapping[str, Any] | None = None,
    actor: str,
    turn_id: uuid.UUID | None = None,
    now: datetime | None = None,
) -> ConversationTask:
    """Escribe un evento y actualiza la proyección en la MISMA transacción. No hace commit.

    La fila de la tarea se bloquea (`FOR UPDATE`) mientras se calcula el siguiente `seq`:
    dos escrituras concurrentes sobre la misma tarea se serializan. `schema_version` lo pone
    este módulo (la versión vigente, o `to_version` en `revalidated`).

    La tarea se busca por negocio Y conversación: un `task_id` de otra conversación del mismo
    negocio (otro cliente) no se alcanza. Orden de bloqueos: conversación → tareas (ver
    `core/kernel/episodio.py`); quien bloquee la conversación lo hace antes de llamar aquí.
    """
    _mismo_negocio(tenant_id)
    fila = (
        await session.execute(
            select(ConversationTask)
            .where(
                ConversationTask.tenant_id == tenant_id,
                ConversationTask.conversation_id == conversation_id,
                ConversationTask.id == task_id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if fila is None:
        raise TareaNoEncontrada(str(task_id))
    actual = proyeccion_de(fila)
    datos = dict(data or {})
    datos["schema_version"] = datos.get("to_version") if tipo == "revalidated" else actual.procedure_version
    evento = Evento(
        seq=actual.last_seq + 1, type=tipo, data=datos, actor=actor, created_at=_ahora(now), turn_id=turn_id
    )
    nueva = aplicar(actual, evento)
    session.add(_fila_evento(tenant_id, fila.id, evento))
    _volcar(fila, nueva)
    await session.flush()
    log.info("task_event", task_id=str(fila.id), type=tipo, seq=evento.seq, actor=actor)
    return fila


async def eventos(
    session: AsyncSession, *, tenant_id: uuid.UUID, conversation_id: uuid.UUID, task_id: uuid.UUID
) -> list[TaskEvent]:
    """Eventos de una tarea de esta conversación, por `seq` ([] si no es de ella)."""
    return list(
        (
            await session.execute(
                select(TaskEvent)
                .join(
                    ConversationTask,
                    (ConversationTask.tenant_id == TaskEvent.tenant_id) & (ConversationTask.id == TaskEvent.task_id),
                )
                .where(
                    TaskEvent.tenant_id == tenant_id,
                    TaskEvent.task_id == task_id,
                    ConversationTask.conversation_id == conversation_id,
                )
                .order_by(TaskEvent.seq)
            )
        ).scalars()
    )


async def eventos_de_tarea(
    session: AsyncSession, *, tenant_id: uuid.UUID, conversation_id: uuid.UUID, task_id: uuid.UUID
) -> list[Evento]:
    """Los eventos de una tarea de esta conversación como `Evento` (los lee el motor)."""
    filas = await eventos(session, tenant_id=tenant_id, conversation_id=conversation_id, task_id=task_id)
    return [_evento_de(e) for e in filas]


async def reconstruir(
    session: AsyncSession, *, tenant_id: uuid.UUID, conversation_id: uuid.UUID, task_id: uuid.UUID
) -> Proyeccion:
    """La proyección calculada desde `task_events` (debe ser igual a la guardada)."""
    filas = await eventos(session, tenant_id=tenant_id, conversation_id=conversation_id, task_id=task_id)
    return reducir(_evento_de(e) for e in filas)


async def tareas_abiertas(
    session: AsyncSession, *, tenant_id: uuid.UUID, conversation_id: uuid.UUID
) -> list[ConversationTask]:
    """Tareas en foco y en pausa de la conversación (sin sub-tareas), por posición en la pila."""
    return list(
        (
            await session.execute(
                select(ConversationTask)
                .where(
                    ConversationTask.tenant_id == tenant_id,
                    ConversationTask.conversation_id == conversation_id,
                    ConversationTask.status.in_(ABIERTOS),
                    ConversationTask.parent_task_id.is_(None),
                )
                .order_by(ConversationTask.stack_position, ConversationTask.created_at)
            )
        ).scalars()
    )


async def expirar_tareas_inactivas(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    conversation_id: uuid.UUID,
    sin_actividad_desde: datetime,
    now: datetime,
) -> list[uuid.UUID]:
    """Cierra con `episode_expired` las tareas abiertas sin actividad desde `sin_actividad_desde`.

    Es el inicio del episodio anterior (ADR-0114 §1): una tarea que no tuvo ningún evento en
    todo ese episodio queda `cancelled` con motivo `episode_expired`. El evento lleva solo los
    NOMBRES de los datos (los valores ya están en la tarea; no se duplica PII en una tabla que
    no se puede redactar). Los datos válidos pasan a `memory_facts` en la consolidación nocturna
    (`core/memory/facts.py`, HU-AOS-36), fuera de esta transacción.

    Debe correr ANTES de cualquier `revalidated` del episodio nuevo: la revalidación toca
    `updated_at` y la tarea ya no se vería inactiva.
    """
    vencidas = (
        await session.execute(
            select(ConversationTask)
            .where(
                ConversationTask.tenant_id == tenant_id,
                ConversationTask.conversation_id == conversation_id,
                ConversationTask.status.in_(ABIERTOS),
                ConversationTask.updated_at < sin_actividad_desde,
            )
            .order_by(ConversationTask.created_at)
            .with_for_update()
        )
    ).scalars().all()
    cerradas: list[uuid.UUID] = []
    for tarea in vencidas:
        nombres = sorted(tarea.slots or {})
        fila = await registrar(
            session,
            tenant_id=tenant_id,
            conversation_id=conversation_id,
            task_id=tarea.id,
            tipo=EPISODE_EXPIRED,
            data={"reason": EPISODE_EXPIRED, "slots": nombres},
            actor="kernel",
            now=now,
        )
        cerradas.append(fila.id)
    if cerradas:
        log.info("tasks_episode_expired", conversation_id=str(conversation_id), count=len(cerradas))
    return cerradas

