"""Conexión canónica de cada número de WhatsApp de Meta (ADR-0115 §1 y §6, HU-AOS-11).

La conexión es `channel_connections`: hay una fila por número (`external_id` = su
`phone_number_id`) ligada a su cuenta de Meta (`meta_connection_id`). Es estable aunque el
número cambie de cuenta: reconectarlo o pasarlo a otra cuenta religa la misma fila.

- **Sin credenciales propias** (CHECK `ck_cc_meta_sin_credenciales`): las pone la cuenta
  según su origen (ADR-0094). Nace con `is_primary = false` y la API de conexiones manuales
  no la ve ni la edita.
- **Sin estado propio para Meta**: la pausa es la de la cuenta (ADR-0122). Por eso estas
  filas no se borran ni se desactivan cuando la cuenta se pausa o se elimina.
- **Una conexión manual del mismo número gana**: si el negocio ya tiene una fila viva sin
  cuenta (`meta_connection_id IS NULL`) para ese `external_id`, no se crea una segunda
  (`uq_connection_tenant_provider_external`): se informa como choque y la manual sigue
  resolviendo como hoy.

`asegurar_conexion_del_numero` corre dentro de la transacción del alta
(`meta_asset_claims.numero_para_conexion`, los cinco caminos de ADR-0122 §6) y en el backfill
(`core/gateway/conexion_canonica_backfill.py`).
"""

from __future__ import annotations

import uuid
from typing import Any, Literal

from sqlalchemy import select

from core.db.models import ChannelConnection, utcnow
from core.gateway.connection_types import (
    INSTAGRAM_META,
    MESSENGER_META,
    WHATSAPP_META,
    get_connection_type,
)
from core.shared.logger import get_logger

log = get_logger(__name__)

Resultado = Literal["creada", "religada", "ya_estaba", "choque_manual"]

PROVEEDOR = "meta"
CANAL = "whatsapp"


def _tipo() -> tuple[str, str]:
    tipo = get_connection_type(WHATSAPP_META)
    if tipo is None:  # el catálogo es código: no pasa salvo que alguien borre el tipo
        raise RuntimeError(f"Falta el tipo de conexión {WHATSAPP_META} en el catálogo")
    return tipo.id, tipo.category


async def asegurar_conexion_del_numero(
    session: Any,
    tenant_id: uuid.UUID,
    phone_number_id: str,
    meta_connection_id: uuid.UUID,
    *,
    aplicar: bool = True,
) -> Resultado:
    """Crea o religa la conexión canónica de `phone_number_id` hacia su cuenta.

    "Revivir" (ADR-0115 §6) es religar: la fila de un número de Meta nunca se borra (no tiene
    estado propio; la API de conexiones manuales no la toca), así que la fila sigue viva y
    reconectar el número la reasigna a la cuenta nueva.

    Va en la MISMA sesión que el alta (o el lote del backfill). El llamador serializa por
    número (`claim_assets` toma `meta_asset:phone:<id>`), así dos altas del mismo número no
    crean dos filas. Con `aplicar=False` solo dice qué haría (dry-run del backfill)."""
    type_id, category = _tipo()
    viva = (
        await session.execute(
            select(ChannelConnection)
            .where(
                ChannelConnection.tenant_id == tenant_id,
                ChannelConnection.provider == PROVEEDOR,
                ChannelConnection.external_id == phone_number_id,
                ChannelConnection.deleted_at.is_(None),
            )
            .order_by(ChannelConnection.created_at)
            .limit(1)
        )
    ).scalars().first()
    if viva is not None:
        if viva.meta_connection_id is None:
            log.warning(
                "conexion_canonica_choca_con_manual",
                tenant_id=str(tenant_id), phone_number_id=phone_number_id, connection_id=str(viva.id),
            )
            return "choque_manual"
        if viva.meta_connection_id == meta_connection_id:
            return "ya_estaba"
        if aplicar:
            viva.meta_connection_id = meta_connection_id
            viva.updated_at = utcnow()
            await session.flush()
        return "religada"

    if aplicar:
        session.add(
            ChannelConnection(
                tenant_id=tenant_id,
                channel=CANAL,
                provider=PROVEEDOR,
                type_id=type_id,
                category=category,
                label="",
                active=True,
                is_primary=False,
                external_id=phone_number_id,
                credentials_encrypted=None,
                meta_connection_id=meta_connection_id,
            )
        )
        await session.flush()
        log.info("conexion_canonica_creada", tenant_id=str(tenant_id), phone_number_id=phone_number_id)
    return "creada"


async def _asegurar_conexion_de_activo(
    session: Any,
    tenant_id: uuid.UUID,
    external_id: str,
    meta_connection_id: uuid.UUID,
    *,
    tipo_id: str,
    channel: str,
    nombre: str,
) -> tuple[uuid.UUID, Resultado]:
    tipo = get_connection_type(tipo_id)
    if tipo is None:  # el catálogo es código: no pasa salvo que alguien borre el tipo
        raise RuntimeError(f"Falta el tipo de conexión {tipo_id} en el catálogo")
    viva = (
        await session.execute(
            select(ChannelConnection)
            .where(
                ChannelConnection.tenant_id == tenant_id,
                ChannelConnection.provider == PROVEEDOR,
                ChannelConnection.channel == channel,
                ChannelConnection.external_id == external_id,
                ChannelConnection.deleted_at.is_(None),
            )
            .order_by(ChannelConnection.created_at)
            .limit(1)
        )
    ).scalars().first()
    if viva is not None:
        if viva.meta_connection_id is None:
            log.warning("conexion_canonica_choca_con_manual", tenant_id=str(tenant_id), external_id=external_id)
            return viva.id, "choque_manual"
        if viva.meta_connection_id == meta_connection_id:
            return viva.id, "ya_estaba"
        viva.meta_connection_id = meta_connection_id
        viva.updated_at = utcnow()
        await session.flush()
        return viva.id, "religada"
    conexion = ChannelConnection(
        tenant_id=tenant_id,
        channel=channel,
        provider=PROVEEDOR,
        type_id=tipo.id,
        category=tipo.category,
        label=nombre[:120],
        active=True,
        is_primary=False,
        external_id=external_id,
        credentials_encrypted=None,
        meta_connection_id=meta_connection_id,
    )
    session.add(conexion)
    await session.flush()
    log.info("conexion_canonica_de_activo_creada", tenant_id=str(tenant_id), channel=channel, external_id=external_id)
    return conexion.id, "creada"


async def asegurar_conexion_de_pagina(
    session: Any,
    tenant_id: uuid.UUID,
    page_id: str,
    meta_connection_id: uuid.UUID,
    *,
    nombre: str = "",
) -> tuple[uuid.UUID, Resultado]:
    """Crea o religa la conexión canónica de la Página `page_id` hacia su cuenta (ADR-0152 §2).

    Una conexión canónica por Página. Si la Página ya tiene una viva en este negocio (reconexión
    con otra cuenta de Meta), se religa a la nueva; si es una conexión manual sin cuenta, no se
    toca (`choque_manual`). Va en la MISMA sesión que el alta, después de `claim_messaging_assets`
    (que serializa por Página)."""
    return await _asegurar_conexion_de_activo(
        session, tenant_id, page_id, meta_connection_id, tipo_id=MESSENGER_META, channel="messenger", nombre=nombre
    )


async def asegurar_conexion_de_cuenta_ig(
    session: Any,
    tenant_id: uuid.UUID,
    ig_user_id: str,
    meta_connection_id: uuid.UUID,
    *,
    nombre: str = "",
) -> tuple[uuid.UUID, Resultado]:
    """Lo mismo que `asegurar_conexion_de_pagina` para una cuenta profesional de Instagram (HU-CHAN-04): su
    `external_id` es el `ig_user_id`, que es el `entry.id` con el que Meta entrega sus mensajes."""
    return await _asegurar_conexion_de_activo(
        session, tenant_id, ig_user_id, meta_connection_id, tipo_id=INSTAGRAM_META, channel="instagram", nombre=nombre
    )
