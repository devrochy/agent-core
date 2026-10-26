"""«En curso»: qué falta y qué ya se hizo en la conversación, para quien la toma (HU-AOS-28, ADR-0169 §5).

Por cada tarea, la tarea en foco primero:
- las abiertas;
- y, en solo lectura, las cerradas como `escalated` desde que se creó la escalación activa (el error
  de acción con relevo humano y el cambio de versión irreversible cierran la tarea justo antes de
  escalar).

Lo que falta sale de `motor.faltas_de_salida` sobre la definición EXACTA de la tarea: la clave
`(procedure_id, procedure_version)` en los congelados de la release de la conversación y, si no está,
en el binario (`registro.definicion`); nunca por major. Se devuelve estructurado (clase, dato y su
rótulo humano de la habilidad): los textos del playbook del kernel son instrucciones para el modelo,
no para la persona. Los motivos van como códigos. Solo lectura: no abre episodios ni expira tareas.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import select, text

from core.db.models import ConversationTask, Escalation, TaskEvent
from core.shared.redaccion import OCULTO
from core.shared.tenant import get_current_tenant_id
from core.shared.test_mode import TEST_CHANNEL

SIN_PROCEDIMIENTOS = "sin_procedimientos"
DEFINICION_NO_DISPONIBLE = "definicion_no_disponible"
_ACCIONES = ("action_succeeded", "action_failed")
MAX_TAREAS = 20
_ACCION = re.compile(r"^[a-zA-Z0-9_]{1,80}$")  # = state._ACCION
_MOTIVO = re.compile(r"^[a-z_]{1,40}$")  # = state._MOTIVO


@dataclass(frozen=True)
class Dato:
    nombre: str
    rotulo: str
    valor: Any
    confirmado: bool
    clase: str | None


@dataclass(frozen=True)
class Faltante:
    clase: str  # dato_cliente | dato_boton | dato_contenido | dato_accion | confirmar_dato | confirmar_etapa | accion
    nombre: str
    rotulo: str


@dataclass(frozen=True)
class UltimaAccion:
    accion: str
    ok: bool
    motivo: str | None
    at: datetime


@dataclass(frozen=True)
class TareaEnCurso:
    task_id: uuid.UUID
    procedure_id: str
    titulo: str | None
    estado: str
    motivo_estado: str | None
    etapa: str
    en_foco: bool
    datos: list[Dato]
    falta: list[Faltante]
    confirmacion_pendiente: bool
    ultima_accion: UltimaAccion | None
    motivo: str | None = None  # código: por qué `falta` va vacío (p. ej. definicion_no_disponible)


@dataclass(frozen=True)
class Escalacion:
    id: uuid.UUID
    motivo: str | None
    resumen: str | None
    estado: str
    asignada_a: uuid.UUID | None
    at: datetime


@dataclass(frozen=True)
class EnCurso:
    tareas: list[TareaEnCurso] = field(default_factory=list)
    escalacion: Escalacion | None = None
    motivo: str | None = None  # SIN_PROCEDIMIENTOS si no hay ninguna tarea


def _exigir_scope(tenant_id: uuid.UUID) -> None:
    if get_current_tenant_id() != tenant_id:
        raise PermissionError("en curso: fuera del tenant_scope del negocio")


def _rotulo(rotulos: dict[str, str], nombre: str) -> str:
    return rotulos.get(nombre) or nombre.replace("_", " ")


def describir(tarea: ConversationTask, proc: Any, rotulos: dict[str, str]) -> tuple[list[Dato], list[Faltante], str | None]:
    """Datos dados y lo que falta de una tarea (pura, salvo que recibe la definición ya resuelta)."""
    from core.kernel.procedures.motor import etapa, faltas_de_salida
    from core.kernel.state import proyeccion_de

    datos = [
        Dato(
            nombre=nombre,
            rotulo=_rotulo(rotulos, nombre),
            valor=OCULTO if meta.get("data_class") == "payment" else meta.get("value"),
            confirmado=bool(meta.get("confirmed")),
            clase=meta.get("data_class"),
        )
        for nombre, meta in (tarea.slots or {}).items()
        if isinstance(meta, dict) and meta.get("value") is not None
    ]
    if proc is None:
        return datos, [], DEFINICION_NO_DISPONIBLE
    stage = etapa(proc, tarea.stage)
    if stage is None:
        return datos, [], DEFINICION_NO_DISPONIBLE
    falta = [
        Faltante(f.clase, f.nombre, f.nombre.replace("_", " ") if f.clase in ("accion", "confirmar_etapa") else _rotulo(rotulos, f.nombre))
        for f in faltas_de_salida(proc, stage, proyeccion_de(tarea))
    ]
    return datos, falta, None


async def leer(session: Any, tenant_id: uuid.UUID, conversation_id: uuid.UUID) -> EnCurso | None:
    """«En curso» de la conversación; `None` si no es de este negocio o es del playground."""
    from core.kernel.procedures import registro
    from core.mcp.host import mcp_host
    from core.orchestrator import motivos_escalacion as motivos
    from core.releases.cargar import cargar_release

    _exigir_scope(tenant_id)
    conv = (
        await session.execute(
            text(
                "SELECT release_id FROM conversations "
                "WHERE tenant_id = :t AND id = :c AND channel <> :prueba AND deleted_at IS NULL"
            ),
            {"t": tenant_id, "c": conversation_id, "prueba": TEST_CHANNEL},
        )
    ).first()
    if conv is None:
        return None
    esc = (
        await session.execute(
            select(Escalation)
            .where(
                Escalation.tenant_id == tenant_id,
                Escalation.conversation_id == conversation_id,
                Escalation.status != "resuelta",
                Escalation.deleted_at.is_(None),
            )
            .order_by(Escalation.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    condicion = ConversationTask.status.in_(("active", "suspended"))
    if esc is not None:
        condicion = condicion | (
            (ConversationTask.status == "escalated") & (ConversationTask.completed_at >= esc.created_at)
        )
    tareas = list(
        (
            await session.execute(
                select(ConversationTask)
                .where(
                    ConversationTask.tenant_id == tenant_id,
                    ConversationTask.conversation_id == conversation_id,
                    ConversationTask.parent_task_id.is_(None),
                    condicion,
                )
                .order_by(ConversationTask.stack_position, ConversationTask.created_at.desc())
                .limit(MAX_TAREAS)
            )
        ).scalars()
    )
    escalacion = (
        Escalacion(esc.id, motivos.codigo(esc.reason, esc.summary), esc.summary, esc.status, esc.assigned_admin_id, esc.created_at)
        if esc is not None
        else None
    )
    if not tareas:
        return EnCurso([], escalacion, SIN_PROCEDIMIENTOS)

    congelados: dict[tuple[str, str], Any] = {}
    if conv[0] is not None:
        release = await cargar_release(session, tenant_id, conv[0])
        if release is not None:
            congelados = release.congelados
    ultimas: dict[uuid.UUID, UltimaAccion] = {}
    for e in (
        await session.execute(
            select(TaskEvent)
            .where(
                TaskEvent.tenant_id == tenant_id,
                TaskEvent.task_id.in_([t.id for t in tareas]),
                TaskEvent.type.in_(_ACCIONES),
            )
            .order_by(TaskEvent.task_id, TaskEvent.seq)
        )
    ).scalars():
        data = e.data or {}
        accion, motivo = data.get("action"), data.get("reason")
        # Solo el nombre de la acción y un código validados: nunca `candidates`, valores ni texto
        # libre (`task_events` es append-only y no se puede redactar después).
        if not (isinstance(accion, str) and _ACCION.fullmatch(accion)):
            continue
        ultimas[e.task_id] = UltimaAccion(
            accion, e.type == "action_succeeded",
            motivo if isinstance(motivo, str) and _MOTIVO.fullmatch(motivo) else None, e.created_at,
        )

    salida: list[TareaEnCurso] = []
    for t in tareas:
        proc = registro.definicion(t.procedure_id, t.procedure_version, congelados)
        rotulos = mcp_host.get_rotulos(proc.owner) if proc is not None else {}
        datos, falta, motivo = describir(t, proc, rotulos)
        salida.append(
            TareaEnCurso(
                task_id=t.id,
                procedure_id=t.procedure_id,
                titulo=proc.title if proc is not None else None,
                estado=t.status,
                motivo_estado=t.status_reason,
                etapa=t.stage,
                en_foco=t.status == "active" and t.stack_position == 0,
                datos=datos,
                falta=falta,
                confirmacion_pendiente=t.pending_confirmation is not None,
                ultima_accion=ultimas.get(t.id),
                motivo=motivo,
            )
        )
    salida.sort(key=lambda x: (not x.en_foco, x.estado != "active", x.estado == "escalated"))
    return EnCurso(salida, escalacion, None)


__all__ = ["DEFINICION_NO_DISPONIBLE", "SIN_PROCEDIMIENTOS", "EnCurso", "describir", "leer"]
