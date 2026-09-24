"""Medición de cuotas por tenant (ADR-0045).

Las métricas se **derivan** de la tabla que ya es su fuente de verdad (no hay
un contador paralelo): `customers`, `conversations`, `messages` y `usage_ledger`.
Cada métrica se declara una sola vez acá; el panel y el enforcement la consumen.

`count_metric()` corre dentro de `tenant_scope(tenant_id)` (RLS), así que sirve
tanto desde el webhook/consumer (que ya están en contexto) como desde el panel.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from sqlalchemy import func, select

from core.db.models import Conversation, Customer, Message
from core.db.session import get_session
from core.shared.tenant import tenant_scope

MetricScope = Literal["period", "lifetime"]


@dataclass(frozen=True)
class Metric:
    key: str
    label: str
    unit: str
    scope: MetricScope
    description: str = ""


METRICS: tuple[Metric, ...] = (
    Metric(
        key="clientes",
        label="Clientes",
        unit="clientes",
        scope="lifetime",
        description="Clientes activos del tenant (total histórico).",
    ),
    Metric(
        key="conversaciones",
        label="Conversaciones",
        unit="conversaciones",
        scope="period",
        description="Conversaciones iniciadas en el período.",
    ),
    Metric(
        key="conversaciones_audio",
        label="Conversaciones por audio",
        unit="audios",
        scope="period",
        description="Audios entrantes procesados en el período.",
    ),
    Metric(
        key="conversaciones_ocr",
        label="Conversaciones por imagen (OCR)",
        unit="imágenes",
        scope="period",
        description="Imágenes entrantes procesadas (OCR/visión) en el período.",
    ),
    Metric(
        key="plugins",
        label="Plugins",
        unit="packs",
        scope="lifetime",
        description="Packs habilitados para el tenant.",
    ),
    Metric(
        key="mensajes",
        label="Mensajes",
        unit="mensajes",
        scope="period",
        description="Mensajes salientes enviados en el período.",
    ),
    Metric(
        key="costo_llm_usd",
        label="Costo LLM",
        unit="USD",
        scope="period",
        description="Gasto LLM del período (usage_ledger, ADR-0008).",
    ),
)

METRICS_BY_KEY: dict[str, Metric] = {m.key: m for m in METRICS}
METRIC_KEYS: frozenset[str] = frozenset(METRICS_BY_KEY)


def get_metric(key: str) -> Metric | None:
    return METRICS_BY_KEY.get(key)


def metric_label(key: str) -> str:
    metric = METRICS_BY_KEY.get(key)
    return metric.label if metric else key


async def count_metric(
    tenant_id: uuid.UUID,
    key: str,
    *,
    period_start: datetime | None = None,
    session=None,
) -> int:
    """Consumo actual de una métrica para un tenant.

    Para métricas de período, `period_start` acota la ventana (el inicio del
    período de la suscripción). Devuelve 0 si la métrica no existe (nunca
    rompe el turno por una métrica mal escrita).

    `session`: si el caller YA tiene una sesión abierta dentro de su propio
    `tenant_scope`, la pasa para evitar abrir una sesión anidada (el
    `set_config(..., true)` de RLS es transaction-scoped y una sesión anidada
    sobre la misma conexión resetearía el contexto de tenant). Sin `session`,
    abre su propia sesión scoped.
    """
    metric = METRICS_BY_KEY.get(key)
    if metric is None:
        return 0

    if session is not None:
        return await _count_metric_in_session(session, tenant_id, key, period_start)

    if key == "plugins":
        return await _count_plugins(tenant_id)

    async with tenant_scope(tenant_id):
        async with get_session() as session:
            if key == "costo_llm_usd":
                return await _count_cost(session, tenant_id, period_start)
            return await _count_metric_in_session(session, tenant_id, key, period_start)


async def _count_metric_in_session(session, tenant_id: uuid.UUID, key: str, period_start) -> int:
    if key == "plugins":
        # `plugins` no se cuenta desde una sesión ajena: depende del AgentSpec del
        # tenant (otra sesión). Se resuelve fuera, con `count_metric` sin `session`.
        raise ValueError("plugins no se puede medir con una sesión compartida")
    if key == "clientes":
        return int(
            (
                await session.execute(
                    select(func.count(Customer.id)).where(
                        Customer.tenant_id == tenant_id,
                        Customer.deleted_at.is_(None),
                    )
                )
            ).scalar()
            or 0
        )
    if key == "conversaciones":
        query = select(func.count(Conversation.id)).where(
            Conversation.tenant_id == tenant_id,
            Conversation.deleted_at.is_(None),
        )
        if period_start is not None:
            query = query.where(Conversation.started_at >= period_start)
        return int((await session.execute(query)).scalar() or 0)
    if key == "conversaciones_audio":
        return await _count_media(session, tenant_id, "audio/%", period_start)
    if key == "conversaciones_ocr":
        return await _count_media(session, tenant_id, "image/%", period_start)
    if key == "mensajes":
        query = select(func.count(Message.id)).where(
            Message.tenant_id == tenant_id,
            Message.direction == "outbound",
            Message.deleted_at.is_(None),
        )
        if period_start is not None:
            query = query.where(Message.created_at >= period_start)
        return int((await session.execute(query)).scalar() or 0)
    if key == "costo_llm_usd":
        return await _count_cost(session, tenant_id, period_start)
    return 0


async def _count_cost(session, tenant_id: uuid.UUID, period_start) -> float:
    from decimal import Decimal

    from core.db.models import UsageLedger

    query = select(func.coalesce(func.sum(UsageLedger.cost_usd), 0)).where(
        UsageLedger.tenant_id == tenant_id
    )
    if period_start is not None:
        query = query.where(UsageLedger.created_at >= period_start)
    total = (await session.execute(query)).scalar() or Decimal(0)
    return float(total)


async def _count_media(session, tenant_id: uuid.UUID, prefix: str, period_start: datetime | None) -> int:
    query = select(func.count(Message.id)).where(
        Message.tenant_id == tenant_id,
        Message.direction == "inbound",
        Message.media_type.like(prefix),
        Message.deleted_at.is_(None),
    )
    if period_start is not None:
        query = query.where(Message.created_at >= period_start)
    return int((await session.execute(query)).scalar() or 0)


async def _count_plugins(tenant_id: uuid.UUID) -> int:
    """Packs de NEGOCIO habilitados del tenant (sin el core, ADR-0045).

    El core es plomería del framework y no consume cuota. Fuente única del
    conteo: `count_tenant_packs` (mismo valor que el KPI del panel).
    """
    from core.agents.spec import count_tenant_packs, load_active_spec

    async with tenant_scope(tenant_id):
        spec = await load_active_spec(tenant_id)
    return count_tenant_packs(spec)


async def usage_snapshot(
    tenant_id: uuid.UUID,
    *,
    period_start: datetime | None,
    limits: dict[str, int | None],
) -> list[dict]:
    """Consumo + límite + porcentaje por cada métrica con límite definido.

    Ordenado como `METRICS`. `limit=None` = ilimitado (`percent=None`). Es lo
    que alimenta las barras de progreso del panel.
    """
    rows: list[dict] = []
    for metric in METRICS:
        if metric.key not in limits:
            continue
        limit = limits.get(metric.key)
        used = await count_metric(tenant_id, metric.key, period_start=period_start)
        if limit is None:
            percent = None
        else:
            percent = 100.0 if limit == 0 else min(100.0, round(used * 100.0 / limit, 1))
        rows.append(
            {
                "key": metric.key,
                "label": metric.label,
                "unit": metric.unit,
                "scope": metric.scope,
                "used": used,
                "limit": limit,
                "percent": percent,
                # "excedido" es SUPERAR el límite, no estar justo en él: el plan
                # permite exactamente `limit` unidades, así que `used == limit`
                # (1/1, 5/5) NO es excedido. El bloqueo de la SIGUIENTE unidad
                # sí ocurre al llegar al tope -- eso lo decide
                # `enforcement.quota_decision` con `used >= limit`, no este flag.
                "exceeded": limit is not None and used > limit,
            }
        )
    return rows
