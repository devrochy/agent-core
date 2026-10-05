"""Acciones de costos de WhatsApp/Meta (fuente única SSR + API).

- `sync_meta_pricing`: dispara el mismo trabajo que el cron diario para las WABAs
  conectadas del tenant (no escribe tarifas locales).
- `verify_credits`: consulta de SOLO LECTURA las líneas de crédito extendidas de
  una conexión (`GET /{business_id}/extendedcredits`).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select

from core.config import get_settings
from core.db.models import MessageCost, MetaConnection, PricingDaily, WabaAccount
from core.db.session import get_owned, get_session

# Ventana del sync manual (misma que el cron).
SYNC_WINDOW_DAYS = 7


async def sync_meta_pricing(tenant_id: uuid.UUID) -> list[str]:
    """Sincroniza `pricing_daily` de las WABAs conectadas. Devuelve los errores."""
    from core.gateway.meta_credentials import CredentialsUnavailable, credentials_for
    from core.gateway.meta_errors import GraphError
    from core.gateway.meta_pricing_analytics import sync_pricing_daily
    from core.tasks.meta_pricing_sync import _SYNC_WINDOW_DAYS

    now = datetime.now(UTC)
    start_unix = int(
        (now - timedelta(days=_SYNC_WINDOW_DAYS))
        .replace(hour=0, minute=0, second=0, microsecond=0)
        .timestamp()
    )
    end_unix = int(now.timestamp())

    async with get_session() as session:
        rows = (
            await session.execute(
                select(WabaAccount, MetaConnection)
                .join(MetaConnection, MetaConnection.id == WabaAccount.connection_id)
                .where(WabaAccount.tenant_id == tenant_id, MetaConnection.status == "connected")
            )
        ).all()

    errors: list[str] = []
    for waba, connection in rows:
        try:
            creds = await credentials_for(connection, waba_id=waba.waba_id)
        except CredentialsUnavailable as exc:
            # Un error legible por conexión (antes se omitía en silencio) sin cortar las demás.
            errors.append(f"{waba.waba_id}: {exc.message}"[:100])
            continue
        try:
            await sync_pricing_daily(
                waba.waba_id,
                waba.id,
                tenant_id,
                start_unix,
                end_unix,
                creds.access_token,
                creds.app_secret,
                creds.graph_api_version,
                connection.id,
            )
        except GraphError as exc:
            errors.append(exc.message[:100])
    return errors


async def list_credit_connections(tenant_id: uuid.UUID) -> list[MetaConnection]:
    """Conexiones de Meta con `business_id` (candidatas a consultar créditos)."""
    async with get_session() as session:
        return list(
            (
                await session.execute(
                    select(MetaConnection).where(
                        MetaConnection.tenant_id == tenant_id,
                        MetaConnection.business_id.is_not(None),
                    )
                )
            )
            .scalars()
            .all()
        )


async def verify_credits(
    tenant_id: uuid.UUID, connection_id: str
) -> tuple[list[dict[str, Any]], str | None]:
    """(créditos, error) de una conexión. No implica "método de pago configurado"."""
    from core.gateway.meta_business_credits import get_extended_credits
    from core.gateway.meta_credentials import CredentialsUnavailable, credentials_for
    from core.gateway.meta_errors import GraphError

    try:
        cid = uuid.UUID(connection_id)
    except ValueError:
        return [], "Conexión inválida"

    async with get_session() as session:
        connection = await get_owned(session, MetaConnection, cid, tenant_id)

    if connection is None or not connection.business_id:
        return [], "Esta conexión no tiene business_id configurado"

    try:
        creds = await credentials_for(connection)
    except CredentialsUnavailable:
        return [], "No hay token disponible para esta conexión"

    try:
        credits = await get_extended_credits(
            connection.business_id, creds.access_token, creds.app_secret, creds.graph_api_version, connection.id
        )
    except GraphError as exc:
        return [], exc.message
    return list(credits or []), None


async def meta_costs_context(tenant_id: uuid.UUID, since: datetime) -> dict[str, Any]:
    """Costos de WhatsApp (Meta) -- `cost` viene SIEMPRE de `pricing_daily`
    (autoritativo, sincronizado desde `pricing_analytics`); nunca se
    calcula localmente con una tabla de tarifas propia."""
    async with get_session() as session:
        wabas = (
            await session.execute(select(WabaAccount).where(WabaAccount.tenant_id == tenant_id))
        ).scalars().all()
        waba_ids = [w.id for w in wabas]

        by_category_country_rows = []
        by_phone_rows = []
        webhook_volume_rows = []
        pricing_volume_rows = []
        if waba_ids:
            by_category_country_rows = (
                await session.execute(
                    select(
                        PricingDaily.pricing_category, PricingDaily.country,
                        func.sum(PricingDaily.volume), func.sum(PricingDaily.cost),
                    )
                    .where(
                        PricingDaily.waba_account_id.in_(waba_ids), PricingDaily.tenant_id == tenant_id,
                        PricingDaily.date >= since.date(),
                    )
                    .group_by(PricingDaily.pricing_category, PricingDaily.country)
                    .order_by(func.sum(PricingDaily.cost).desc())
                )
            ).all()
            by_phone_rows = (
                await session.execute(
                    select(PricingDaily.phone_number, func.sum(PricingDaily.volume), func.sum(PricingDaily.cost))
                    .where(
                        PricingDaily.waba_account_id.in_(waba_ids), PricingDaily.tenant_id == tenant_id,
                        PricingDaily.date >= since.date(),
                    )
                    .group_by(PricingDaily.phone_number)
                    .order_by(func.sum(PricingDaily.cost).desc())
                )
            ).all()
            # Diferencia de VOLUMEN (nunca de dólares -- el webhook no trae
            # dólares): cuántos eventos facturables vio el webhook por
            # categoría vs. el `volume` que reportó pricing_analytics para
            # esa misma categoría/rango. Sirve para detectar desajustes
            # pronto, no para reemplazar el costo real.
            webhook_volume_rows = (
                await session.execute(
                    select(MessageCost.pricing_category, func.count(MessageCost.id))
                    .where(
                        MessageCost.tenant_id == tenant_id, MessageCost.billable.is_(True),
                        MessageCost.event_ts >= since,
                    )
                    .group_by(MessageCost.pricing_category)
                )
            ).all()
            pricing_volume_rows = (
                await session.execute(
                    select(PricingDaily.pricing_category, func.sum(PricingDaily.volume))
                    .where(
                        PricingDaily.waba_account_id.in_(waba_ids), PricingDaily.tenant_id == tenant_id,
                        PricingDaily.date >= since.date(),
                    )
                    .group_by(PricingDaily.pricing_category)
                )
            ).all()

    webhook_by_cat = {cat: count for cat, count in webhook_volume_rows}
    pricing_by_cat = {cat: int(vol or 0) for cat, vol in pricing_volume_rows}
    all_cats = set(webhook_by_cat) | set(pricing_by_cat)
    discrepancy = [
        {
            "category": cat or "(sin categoría)",
            "webhook_count": webhook_by_cat.get(cat, 0),
            "pricing_volume": pricing_by_cat.get(cat, 0),
            "diff": webhook_by_cat.get(cat, 0) - pricing_by_cat.get(cat, 0),
        }
        for cat in sorted(all_cats, key=lambda c: c or "")
    ]

    return {
        "meta_wabas": wabas,
        "meta_by_category_country": [
            {"category": cat or "(sin categoría)", "country": country or "(sin país)", "volume": int(vol or 0), "cost": float(cost or 0)}
            for cat, country, vol, cost in by_category_country_rows
        ],
        "meta_by_phone": [
            {"phone_number": phone or "(sin número)", "volume": int(vol or 0), "cost": float(cost or 0)}
            for phone, vol, cost in by_phone_rows
        ],
        "meta_discrepancy": discrepancy,
        "meta_service_allowance_hypothesis_enabled": get_settings().meta_service_message_allowance_hypothesis_enabled,
    }
