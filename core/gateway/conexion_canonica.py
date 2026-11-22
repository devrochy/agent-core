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

**Plan (ADR-0208 §8-9, HU-BILL-19).** Crear una fila canónica es dar de alta una conexión: exige el
canal del plan y el tope `conexiones` (`canales_del_plan.exigir_alta_de_conexion`) en la misma
transacción, después del candado del activo (orden de candados: activo → billing). Volver a ligar la
misma fila (`religada`, `ya_estaba`) no es un alta y no se mide. El backfill y la reversión de un alta
BYOK fallida pasan `exigir_plan=False`: reconstruyen lo que el negocio ya tenía.
"""

from __future__ import annotations

import uuid
from typing import Any, Literal

from sqlalchemy import select, text

from core.billing.canales_del_plan import exigir_alta_de_conexion, verificar_alta_de_conexion
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
    exigir_plan: bool = True,
) -> Resultado:
    """Crea o religa la conexión canónica de `phone_number_id` hacia su cuenta.

    "Revivir" (ADR-0115 §6) es religar: la fila de un número de Meta nunca se borra (no tiene
    estado propio; la API de conexiones manuales no la toca), así que la fila sigue viva y
    reconectar el número la reasigna a la cuenta nueva.

    Va en la MISMA sesión que el alta (o el lote del backfill). El llamador serializa por
    número (`claim_assets` toma `meta_asset:phone:<id>`), así dos altas del mismo número no
    crean dos filas. Con `aplicar=False` solo dice qué haría (dry-run del backfill).

    Crear la fila exige el plan (`exigir_plan`, ver el docstring del módulo) y puede levantar
    `QuotaExceeded` o `CanalNoIncluido`: el alta entera se deshace."""
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
        if exigir_plan:
            await exigir_alta_de_conexion(session, tenant_id, CANAL)
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


async def asegurar_conexion_en_vuelo(
    session: Any, tenant_id: uuid.UUID, phone_number_id: str, meta_connection_id: uuid.UUID
) -> ChannelConnection | None:
    """La conexión canónica de un número que llegó sin ella (la crea al recibir el primer mensaje).

    Es el caso de un número de la cuenta que se conectó por un camino que no creó su fila: el
    consumer lo asegura antes de abrir la conversación para que nazca con `connection_id`. Toma el
    mismo candado por número que `claim_assets` y el backfill y **relee todo bajo el candado**: una
    alta, una reconexión o una baja que corrió mientras esperaba manda (nunca se religa una fila
    que otra alta ya dejó viva, y no se crea para un número liberado o de una cuenta dada de baja).
    Con instancias, como las altas. None si no corresponde crearla (el consumer sigue como antes,
    sin conexión) o si no se pudo.

    **No exige el plan** (HU-BILL-19, decisión documentada en el changelog): corre en el camino del
    mensaje entrante y solo repara la fila de un número que el negocio YA tiene conectado (fila viva en
    `phone_numbers` y cuenta fuera de `draft`; el alta de ese número ya pasó por
    `numero_para_conexion`, que sí exige). Bloquear aquí descartaría o dejaría sin conexión los mensajes
    de un canal existente. Si el negocio está por encima del tope, se registra un aviso y nada más: como
    con cualquier negocio sobre el tope, conserva lo que tiene y solo no crece."""
    from core.agents.instancias import instancias_al_alta
    from core.db.models import MetaConnection, PhoneNumber

    await session.execute(
        text("select pg_advisory_xact_lock(hashtext(:k))"), {"k": f"meta_asset:phone:{phone_number_id}"}
    )

    async def _viva() -> ChannelConnection | None:
        return (
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

    existente = await _viva()
    if existente is not None:
        return existente  # otra alta la creó (o la religó) mientras esperaba el candado: gana
    cuenta = (
        await session.execute(
            select(PhoneNumber.connection_id)
            .join(
                MetaConnection,
                (MetaConnection.id == PhoneNumber.connection_id) & (MetaConnection.tenant_id == PhoneNumber.tenant_id),
            )
            .where(
                PhoneNumber.tenant_id == tenant_id,
                PhoneNumber.phone_number_id == phone_number_id,
                PhoneNumber.deleted_at.is_(None),
                MetaConnection.deleted_at.is_(None),
                MetaConnection.status != "draft",
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if cuenta is None:
        log.info("conexion_en_vuelo_sin_numero_vigente", tenant_id=str(tenant_id), phone_number_id=phone_number_id)
        return None
    try:
        async with session.begin_nested():
            await asegurar_conexion_del_numero(session, tenant_id, phone_number_id, cuenta, exigir_plan=False)
    except Exception:
        log.warning("conexion_en_vuelo_fallo", tenant_id=str(tenant_id), phone_number_id=phone_number_id, exc_info=True)
        return None
    conexion = await _viva()
    if conexion is not None:
        await _avisar_si_supera_el_tope(session, tenant_id)
        await instancias_al_alta(session, tenant_id, phone_number_id, cuenta)
    return conexion


async def _avisar_si_supera_el_tope(session: Any, tenant_id: uuid.UUID) -> None:
    """Aviso (sin bloquear) de que la reparación en vuelo dejó al negocio por encima de `conexiones`."""
    from core.billing.enforcement import quota_decision

    try:
        decision = await quota_decision(tenant_id, "conexiones", session=session)
    except Exception:  # un aviso nunca tumba el mensaje
        log.warning("conexion_en_vuelo_sin_medir_tope", tenant_id=str(tenant_id), exc_info=True)
        return
    if decision.limit is not None and decision.used > decision.limit:
        log.warning(
            "conexion_en_vuelo_sobre_el_tope", tenant_id=str(tenant_id), used=decision.used, limit=decision.limit
        )


async def _asegurar_conexion_de_activo(
    session: Any,
    tenant_id: uuid.UUID,
    external_id: str,
    meta_connection_id: uuid.UUID,
    *,
    tipo_id: str,
    channel: str,
    nombre: str,
    exigir_plan: bool = True,
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
    if exigir_plan:
        await exigir_alta_de_conexion(session, tenant_id, channel)
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


async def _hay_canonica_viva(session: Any, tenant_id: uuid.UUID, channel: str, external_id: str | None = None) -> bool:
    query = select(ChannelConnection.id).where(
        ChannelConnection.tenant_id == tenant_id,
        ChannelConnection.provider == PROVEEDOR,
        ChannelConnection.channel == channel,
        ChannelConnection.meta_connection_id.is_not(None),
        ChannelConnection.deleted_at.is_(None),
    )
    if external_id is not None:
        query = query.where(ChannelConnection.external_id == external_id)
    return (await session.execute(query.limit(1))).first() is not None


async def verificar_antes_del_oauth(tenant_id: uuid.UUID, channel: str) -> None:
    """Chequeo previo, sin lock, antes de abrir el popup de Meta (ADR-0208 §8: no cortar al dueño después
    de completar el OAuth). Levanta `CanalNoIncluido` o `QuotaExceeded`.

    El canal se exige siempre. El tope `conexiones` solo si el negocio NO tiene ya una conexión canónica
    viva de ese canal: con una, el OAuth puede ser una reconexión (token vencido, otra cuenta de Meta), que
    vuelve a ligar la misma fila y no es un alta; bloquearla sería un falso 409. En ese caso, y en una
    carrera, manda el `enforce_growth` del insert."""
    from core.db.session import get_session

    async with get_session() as session:
        if await _hay_canonica_viva(session, tenant_id, channel):
            from core.billing.canales_del_plan import exigir_canal

            await exigir_canal(tenant_id, channel, session=session)
            return
        await verificar_alta_de_conexion(tenant_id, channel, session=session)


async def exigir_cupo_de_activo(session: Any, tenant_id: uuid.UUID, channel: str, external_id: str) -> None:
    """Exige el plan para el activo `external_id` (Página, cuenta de Instagram) si todavía no tiene su fila
    canónica viva: lo usan las altas que escriben su fila propia ANTES de crear la canónica, para no dejar
    la cuenta a medias si el plan no alcanza. Mismo candado que el alta (`exigir_alta_de_conexion`)."""
    if await _hay_canonica_viva(session, tenant_id, channel, external_id):
        return
    await exigir_alta_de_conexion(session, tenant_id, channel)
