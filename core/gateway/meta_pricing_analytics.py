"""`pricing_analytics` de Graph API -- ÚNICA fuente autoritativa de `cost`
en dólares (regla innegociable: nunca se calcula localmente con una tabla
de tarifas propia; los volume tiers se agregan a nivel de BUSINESS
PORTFOLIO, no de WABA, así que un panel multi-tenant no puede predecir la
tarifa unitaria de un tenant).

`GET /{WABA_ID}?fields=pricing_analytics.start(<UNIX>).end(<UNIX>)
.granularity(DAILY).dimensions(PRICING_CATEGORY,PRICING_TYPE,TIER,COUNTRY)`
-- es un GET sobre el NODO de la WABA (no un subrecurso), y las
`dimensions` van SIN corchetes en el query string, tal como lo muestra el
curl de ejemplo oficial de Meta.

ATENCIÓN -- tabla de `granularity`, NO intercambiable entre campos (fuente
de error muy probable, ya mordió en otros paneles):
    analytics               -> DAY | MONTH
    conversation_analytics  -> DAILY | HALF_HOUR | MONTHLY
    pricing_analytics       -> DAILY | HALF_HOUR | MONTHLY

Los enums de `pricing_type`/`pricing_category` de ESTA API no están
verificados contra doc primaria 2026 -- se parsea con
`model_config = ConfigDict(extra="allow")` (equivalente de
`.zod().passthrough()`) y se loguean valores desconocidos, nunca se
rechaza la ingesta por un valor no reconocido.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select

from core.db.models import PricingDaily, utcnow
from core.db.session import get_session
from core.gateway.meta_graph import graph_fetch
from core.shared.logger import get_logger

log = get_logger(__name__)


class PricingAnalyticsPoint(BaseModel):
    model_config = ConfigDict(extra="allow")

    start: int | None = None
    end: int | None = None
    phone_number: str | None = None
    country: str | None = None
    tier: str | None = None
    pricing_type: str | None = None
    pricing_category: str | None = None
    volume: int
    cost: float


async def fetch_pricing_analytics(
    waba_id: str, start_unix: int, end_unix: int, token: str, app_secret: str, version: str,
    connection_id: uuid.UUID,
) -> list[PricingAnalyticsPoint]:
    fields = (
        f"pricing_analytics.start({start_unix}).end({end_unix}).granularity(DAILY)"
        f".dimensions(PRICING_CATEGORY,PRICING_TYPE,TIER,COUNTRY)"
    )
    data = await graph_fetch(
        f"/{waba_id}", token=token, app_secret=app_secret, query={"fields": fields},
        version=version, connection_id=connection_id,
    )
    raw_points: list[dict[str, Any]] = ((data or {}).get("pricing_analytics") or {}).get("data") or []
    points: list[PricingAnalyticsPoint] = []
    for raw in raw_points:
        try:
            points.append(PricingAnalyticsPoint.model_validate(raw))
        except Exception:
            log.warning("pricing_analytics_data_point_invalido", raw=raw)
    return points


async def sync_pricing_daily(
    waba_id: str, waba_account_id: uuid.UUID, tenant_id: uuid.UUID, start_unix: int, end_unix: int,
    token: str, app_secret: str, version: str, connection_id: uuid.UUID,
) -> list[PricingAnalyticsPoint]:
    """Upsert por la clave compuesta completa -- SELECT+UPDATE-o-INSERT
    (mismo patrón que `sync_templates`/`sync_phone_numbers`), nunca
    `ON CONFLICT`: la clave con dimensiones nullable ya se resuelve como
    índice de EXPRESIÓN (coalesce) a nivel de esquema (migración 0028),
    pero acá conviene la query explícita para poder actualizar `cost`/
    `volume` si Meta revisa un día ya sincronizado."""
    points = await fetch_pricing_analytics(waba_id, start_unix, end_unix, token, app_secret, version, connection_id)

    for point in points:
        date = datetime.fromtimestamp(point.start, tz=UTC).date() if point.start else None
        if date is None:
            log.warning("pricing_analytics_sin_start", point=point.model_dump())
            continue

        async with get_session() as session:
            existing = (
                await session.execute(
                    select(PricingDaily).where(
                        PricingDaily.waba_account_id == waba_account_id,
                        PricingDaily.date == date,
                        PricingDaily.phone_number.is_(point.phone_number) if point.phone_number is None
                        else PricingDaily.phone_number == point.phone_number,
                        PricingDaily.country.is_(point.country) if point.country is None
                        else PricingDaily.country == point.country,
                        PricingDaily.tier.is_(point.tier) if point.tier is None else PricingDaily.tier == point.tier,
                        PricingDaily.pricing_type.is_(point.pricing_type) if point.pricing_type is None
                        else PricingDaily.pricing_type == point.pricing_type,
                        PricingDaily.pricing_category.is_(point.pricing_category) if point.pricing_category is None
                        else PricingDaily.pricing_category == point.pricing_category,
                        PricingDaily.tenant_id == tenant_id,
                    )
                )
            ).scalar_one_or_none()

            if existing is None:
                session.add(
                    PricingDaily(
                        tenant_id=tenant_id, waba_account_id=waba_account_id, date=date,
                        phone_number=point.phone_number, country=point.country, tier=point.tier,
                        pricing_type=point.pricing_type, pricing_category=point.pricing_category,
                        volume=point.volume, cost=point.cost, synced_at=utcnow(),
                        raw=point.model_dump(),
                    )
                )
            else:
                existing.volume = point.volume
                existing.cost = point.cost
                existing.synced_at = utcnow()
                existing.raw = point.model_dump()
                existing.updated_at = utcnow()

    return points
