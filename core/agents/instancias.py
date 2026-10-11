"""Instancias de agentes por conexión (ADR-0112 §1, HU-AOS-13).

Cada conexión conversacional tiene exactamente un Coordinador (instancia `core`, índice único
parcial `uq_agent_instances_one_core`) y N especialistas, uno por defecto. Las integraciones
(calendario, correo saliente) no tienen agentes: `agent_instance_checks()` (0069) lo rechaza.

- `asegurar_instancias_base(session, tid, connection_id)`: el Coordinador y el General del
  negocio (keys reservadas `coordinador` y `general`) en una conexión, idempotente. La usa el
  backfill B2 y la usarán el alta posterior de una conexión, la promoción de ADR-0127 §3 y el
  build de releases (HU-AOS-14). Nunca pisa otro Coordinador ni otro agente por defecto.
- `listar_instancias(tid, connection_id)`: lectura para la API (`GET .../connections/{id}/agents`).

Todo corre dentro del `tenant_scope` del negocio (RLS) y además filtra por `tenant_id`.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Literal

from sqlalchemy import func, select, text

from core.agents.tolerante import KEY_COORDINADOR, KEY_GENERAL
from core.db.models import (
    AgentInstanceRecord,
    AgentRecord,
    AgentVersionRecord,
    ChannelConnection,
    ConnectionTypeRecord,
)
from core.db.session import get_session
from core.shared.tenant import get_current_tenant_id

Resultado = Literal["creada", "ya_estaba", "otro_coordinador", "sin_agente"]


class ConexionNoEncontrada(LookupError):
    pass


class ConexionNoConversacional(ValueError):
    """La conexión es una integración (o no tiene tipo de catálogo): no lleva agentes."""


def _exigir_scope(tenant_id: uuid.UUID) -> None:
    if get_current_tenant_id() != tenant_id:
        raise PermissionError("instancias: fuera del tenant_scope del negocio")


async def _conexion_conversacional(session: Any, tenant_id: uuid.UUID, connection_id: uuid.UUID) -> None:
    fila = (
        await session.execute(
            select(ConnectionTypeRecord.produces_conversations)
            .select_from(ChannelConnection)
            .outerjoin(ConnectionTypeRecord, ConnectionTypeRecord.id == ChannelConnection.type_id)
            .where(
                ChannelConnection.tenant_id == tenant_id,
                ChannelConnection.id == connection_id,
                ChannelConnection.deleted_at.is_(None),
            )
        )
    ).one_or_none()
    if fila is None:
        raise ConexionNoEncontrada(str(connection_id))
    if not fila[0]:
        raise ConexionNoConversacional(f"la conexión {connection_id} no es conversacional")


async def agentes_base(session: Any, tenant_id: uuid.UUID) -> dict[str, uuid.UUID]:
    """Ids del Coordinador y del General vivos y activos del negocio (los que existan)."""
    filas = await session.execute(
        select(AgentRecord.key, AgentRecord.id).where(
            AgentRecord.tenant_id == tenant_id,
            AgentRecord.key.in_((KEY_COORDINADOR, KEY_GENERAL)),
            AgentRecord.status == "active",
            AgentRecord.deleted_at.is_(None),
        )
    )
    return {key: aid for key, aid in filas}


@dataclass(frozen=True)
class ResultadoInstancias:
    coordinador: Resultado
    general: Resultado
    # El General entró sin ser el agente por defecto (la conexión ya tenía otro).
    general_sin_defecto: bool = False


async def asegurar_instancias_base(
    session: Any,
    tenant_id: uuid.UUID,
    connection_id: uuid.UUID,
    *,
    agentes: dict[str, uuid.UUID] | None = None,
    aplicar: bool = True,
) -> ResultadoInstancias:
    """Coordinador + General (por defecto) en la conexión, si faltan. Idempotente.

    - `agentes`: `{key: agent_id}` ya resueltos (el lote del backfill los pasa); por defecto
      se leen con `agentes_base`. Un agente que falta da `sin_agente` (en dry-run, todavía no
      existe: cuenta como `creada`).
    - `aplicar = False`: no escribe, dice lo que haría (dry-run).
    - Otro Coordinador vivo en la conexión → `otro_coordinador`: no se pisa. Otro agente por
      defecto → el General entra sin serlo.
    - Lanza `ConexionNoEncontrada` / `ConexionNoConversacional`; un error del trigger
      `agent_instance_checks()` se propaga (es un error, no un conflicto).
    - `session` tiene que haberse abierto dentro del `tenant_scope` de `tenant_id` (la RLS la
      fija al abrirla); aquí se comprueba el scope activo y además se filtra por `tenant_id`."""
    _exigir_scope(tenant_id)
    await _conexion_conversacional(session, tenant_id, connection_id)
    if agentes is None:
        agentes = await agentes_base(session, tenant_id)
    actuales = list(
        (
            await session.execute(
                select(AgentInstanceRecord.agent_id, AgentInstanceRecord.role, AgentInstanceRecord.is_default).where(
                    AgentInstanceRecord.tenant_id == tenant_id,
                    AgentInstanceRecord.connection_id == connection_id,
                    AgentInstanceRecord.deleted_at.is_(None),
                )
            )
        ).all()
    )
    presentes = {aid for aid, _, _ in actuales}

    async def _una(key: str, rol: str, por_defecto: bool) -> Resultado:
        aid = agentes.get(key)
        if aid is not None and aid in presentes:
            return "ya_estaba"
        if rol == "core" and any(r == "core" for _, r, _ in actuales):
            return "otro_coordinador"
        if aid is None:
            return "sin_agente" if aplicar else "creada"
        if not aplicar:
            return "creada"
        creada = (
            await session.execute(
                text(
                    "insert into agent_instances (tenant_id, connection_id, agent_id, role, is_default) "
                    "values (:t, :c, :a, :r, :d) on conflict do nothing returning id"
                ),
                {"t": tenant_id, "c": connection_id, "a": aid, "r": rol, "d": por_defecto},
            )
        ).scalar_one_or_none()
        if creada is not None:
            return "creada"
        # Conflicto con una escritura concurrente: se relee para clasificarlo.
        otro = (
            await session.execute(
                select(AgentInstanceRecord.agent_id).where(
                    AgentInstanceRecord.tenant_id == tenant_id,
                    AgentInstanceRecord.connection_id == connection_id,
                    AgentInstanceRecord.role == "core",
                    AgentInstanceRecord.deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()
        if rol == "core" and otro is not None and otro != aid:
            return "otro_coordinador"
        return "ya_estaba"

    coordinador = await _una(KEY_COORDINADOR, "core", False)
    otro_defecto = any(d for aid, _, d in actuales if aid != agentes.get(KEY_GENERAL))
    general = await _una(KEY_GENERAL, "specialist", not otro_defecto)
    return ResultadoInstancias(coordinador, general, general_sin_defecto=otro_defecto and general == "creada")


@dataclass(frozen=True)
class InstanciaVista:
    id: uuid.UUID
    agent_id: uuid.UUID
    key: str
    name: str
    role: str
    is_default: bool
    priority: int
    enabled: bool
    latest_version: int | None


async def listar_instancias(tenant_id: uuid.UUID, connection_id: uuid.UUID) -> list[InstanciaVista]:
    """Instancias vivas de la conexión del negocio, el Coordinador primero.
    `ConexionNoEncontrada` si la conexión no es de este negocio (o está borrada)."""
    _exigir_scope(tenant_id)
    ultima = (
        select(AgentVersionRecord.agent_id, func.max(AgentVersionRecord.version).label("v"))
        .where(AgentVersionRecord.tenant_id == tenant_id)
        .group_by(AgentVersionRecord.agent_id)
        .subquery()
    )
    async with get_session() as session:
        existe = (
            await session.execute(
                select(ChannelConnection.id).where(
                    ChannelConnection.tenant_id == tenant_id,
                    ChannelConnection.id == connection_id,
                    ChannelConnection.deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()
        if existe is None:
            raise ConexionNoEncontrada(str(connection_id))
        filas = (
            await session.execute(
                select(AgentInstanceRecord, AgentRecord.key, AgentRecord.name, ultima.c.v)
                .join(
                    AgentRecord,
                    (AgentRecord.tenant_id == AgentInstanceRecord.tenant_id)
                    & (AgentRecord.id == AgentInstanceRecord.agent_id),
                )
                .outerjoin(ultima, ultima.c.agent_id == AgentInstanceRecord.agent_id)
                .where(
                    AgentInstanceRecord.tenant_id == tenant_id,
                    AgentInstanceRecord.connection_id == connection_id,
                    AgentInstanceRecord.deleted_at.is_(None),
                )
                .order_by(
                    (AgentInstanceRecord.role != "core"),
                    AgentInstanceRecord.priority,
                    AgentInstanceRecord.created_at,
                )
                .limit(200)
            )
        ).all()
    return [
        InstanciaVista(
            id=i.id,
            agent_id=i.agent_id,
            key=key,
            name=name,
            role=i.role,
            is_default=i.is_default,
            priority=i.priority,
            enabled=i.enabled,
            latest_version=v,
        )
        for i, key, name, v in filas
    ]


__all__ = [
    "ConexionNoConversacional",
    "ConexionNoEncontrada",
    "InstanciaVista",
    "ResultadoInstancias",
    "agentes_base",
    "asegurar_instancias_base",
    "listar_instancias",
]
