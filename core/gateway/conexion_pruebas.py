"""Conexión interna del Centro de pruebas (ADR-0115, adenda 2026-10-24, HU-AOS-15).

La conversación de prueba (`channel == TEST_CHANNEL`) también lleva `connection_id`, porque el
*contract* lo vuelve obligatorio. Cada negocio tiene UNA conexión de tipo `playground`, creada al
primer uso (o por `backfill-conexiones` si ya había conversaciones de prueba):

- no es un canal ni produce conversaciones de clientes (no entra en backfills de agentes, releases,
  `migrar-a-v3` ni en el contract), no tiene credenciales ni release, y no se ve en el panel de
  conexiones ni en los filtros de conversaciones;
- sin release activa, el turno de prueba sigue por el camino de siempre (`asignar` devuelve None).
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from core.db.models import ChannelConnection
from core.gateway.connection_types import PLAYGROUND, get_connection_type
from core.shared.logger import get_logger

log = get_logger(__name__)

PROVEEDOR = "internal"
CANAL = "test"


class ConexionNoValida(ValueError):
    """La conexión elegida para probar no es del negocio, no está activa o no atiende conversaciones."""


async def validar_conexion_para_probar(session: Any, tenant_id: uuid.UUID, connection_id: uuid.UUID) -> uuid.UUID:
    """Probar un agente por una conexión real (HU-AOS-57): la conversación de prueba (canal `test`,
    fuera de métricas) se ata a ella para que el turno corra por su release activa. Solo conexiones
    del negocio, vivas y conversacionales; nunca la interna del playground (esa es el camino de siempre)."""
    fila = (
        await session.execute(
            select(ChannelConnection.type_id).where(
                ChannelConnection.tenant_id == tenant_id,
                ChannelConnection.id == connection_id,
                ChannelConnection.deleted_at.is_(None),
            )
        )
    ).first()
    if fila is None:
        raise ConexionNoValida("No encontramos esa conexión en tu negocio.")
    tipo = get_connection_type(fila[0])
    if tipo is None or fila[0] == PLAYGROUND or not tipo.produces_conversations:
        raise ConexionNoValida("Esa conexión no atiende conversaciones.")
    # Una conexión pausada o cerrada al público (como el chat de pruebas del primer uso) sí se puede
    # probar: la prueba no pasa por el canal, solo usa la release de la conexión.
    return connection_id


async def conexion_de_pruebas(session: Any, tenant_id: uuid.UUID) -> uuid.UUID | None:
    """El id de la conexión viva del Centro de pruebas del negocio, o None si no existe."""
    return (
        await session.execute(
            select(ChannelConnection.id)
            .where(
                ChannelConnection.tenant_id == tenant_id,
                ChannelConnection.type_id == PLAYGROUND,
                ChannelConnection.deleted_at.is_(None),
            )
            .order_by(ChannelConnection.created_at)
            .limit(1)
        )
    ).scalar_one_or_none()


async def asegurar_conexion_de_pruebas(session: Any, tenant_id: uuid.UUID) -> uuid.UUID:
    """La conexión del Centro de pruebas del negocio; la crea si no existe. Idempotente y segura
    ante dos llamadas a la vez (candado por negocio hasta el commit de la sesión)."""
    existente = await conexion_de_pruebas(session, tenant_id)
    if existente is not None:
        return existente
    await session.execute(
        text("select pg_advisory_xact_lock(hashtext(:k))"), {"k": f"conexion_de_pruebas:{tenant_id}"}
    )
    existente = await conexion_de_pruebas(session, tenant_id)
    if existente is not None:
        return existente
    tipo = get_connection_type(PLAYGROUND)
    if tipo is None:  # el catálogo es código: no pasa salvo que alguien borre el tipo
        raise RuntimeError(f"Falta el tipo de conexión {PLAYGROUND} en el catálogo")
    conexion = ChannelConnection(
        tenant_id=tenant_id,
        channel=CANAL,
        provider=PROVEEDOR,
        type_id=tipo.id,
        category=tipo.category,
        label=tipo.label,
        active=True,
        is_primary=False,
        external_id=f"playground-{tenant_id}",
        credentials_encrypted=None,
        meta_connection_id=None,
    )
    try:
        async with session.begin_nested():
            session.add(conexion)
            await session.flush()
    except IntegrityError:
        # Otra transacción ganó la carrera (índice único por proveedor y `external_id`).
        ganadora = await conexion_de_pruebas(session, tenant_id)
        if ganadora is None:
            raise
        return ganadora
    log.info("conexion_de_pruebas_creada", tenant_id=str(tenant_id))
    return conexion.id
