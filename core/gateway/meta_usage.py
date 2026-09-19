"""Parseo y persistencia del uso de Graph API (`X-App-Usage`, `X-Business-Use-Case-Usage`).

Al 80% de cualquier porcentaje reportado por Meta, alerta (único canal real
de "esto es grave" en este repo: `log.warning`/`log.error` vía structlog --
no hay Sentry/Slack integrados). Al 90%, además prende un flag en Redis que
`core/gateway/sender.py` consulta antes de enviar un mensaje, para frenar
antes de que Meta corte el acceso.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime

import httpx
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from core.db.models import GraphApiUsage
from core.db.session import get_session
from core.memory.working import get_redis
from core.shared.logger import get_logger

log = get_logger(__name__)

WARN_THRESHOLD_PCT = 80
PAUSE_THRESHOLD_PCT = 90
PAUSE_TTL_S = 15 * 60  # se re-arma solo si el próximo record_usage sigue >=90

# Allowlist de headers logueables -- nunca Authorization/Cookie (criterio de
# aceptación explícito del ticket).
_LOGGABLE_HEADERS = {"x-business-use-case-usage", "x-app-usage", "x-fb-trace-id"}


def safe_log_headers(headers: httpx.Headers) -> dict[str, str]:
    return {k: v for k, v in headers.items() if k.lower() in _LOGGABLE_HEADERS}


@dataclass
class UsageSnapshot:
    app_usage: dict[str, int] = field(default_factory=dict)
    business_usage: dict[str, list[dict]] = field(default_factory=dict)
    worst_pct: int = 0


def parse_usage(headers: httpx.Headers) -> UsageSnapshot:
    app_usage: dict[str, int] = {}
    business_usage: dict[str, list[dict]] = {}
    worst_pct = 0

    raw_app = headers.get("x-app-usage")
    if raw_app:
        try:
            app_usage = json.loads(raw_app)
            worst_pct = max([worst_pct, *[v for v in app_usage.values() if isinstance(v, int | float)]])
        except (json.JSONDecodeError, TypeError):
            log.warning("graph_usage_header_invalido", header="x-app-usage")

    raw_biz = headers.get("x-business-use-case-usage")
    if raw_biz:
        try:
            business_usage = json.loads(raw_biz)
            for entries in business_usage.values():
                for entry in entries:
                    for key in ("call_count", "total_cputime", "total_time"):
                        value = entry.get(key)
                        if isinstance(value, int | float):
                            worst_pct = max(worst_pct, value)
        except (json.JSONDecodeError, AttributeError, TypeError):
            log.warning("graph_usage_header_invalido", header="x-business-use-case-usage")

    return UsageSnapshot(app_usage=app_usage, business_usage=business_usage, worst_pct=int(worst_pct))


def _pause_key(tenant_id: uuid.UUID, connection_id: uuid.UUID) -> str:
    # tenant_id en la clave (además del connection_id, ya único por tenant vía
    # uq_meta_connections_tenant_app): higiene defensiva -- un bug futuro en
    # la resolución de connection_id no debe poder pausar el envío de otro
    # tenant (hallazgo auditor-multitenant, 2026-09-19, baja severidad: hoy
    # no era explotable, pero sin este namespace dependía enteramente de eso).
    return f"meta:paused:{tenant_id}:{connection_id}"


async def set_pause_flag(tenant_id: uuid.UUID, connection_id: uuid.UUID) -> None:
    r = get_redis()
    await r.set(_pause_key(tenant_id, connection_id), "1", ex=PAUSE_TTL_S)


async def clear_pause_flag(tenant_id: uuid.UUID, connection_id: uuid.UUID) -> None:
    r = get_redis()
    await r.delete(_pause_key(tenant_id, connection_id))


async def is_paused(tenant_id: uuid.UUID | None, connection_id: uuid.UUID | None) -> bool:
    if connection_id is None or tenant_id is None:
        return False
    r = get_redis()
    return bool(await r.get(_pause_key(tenant_id, connection_id)))


async def record_usage(
    tenant_id: uuid.UUID | None,
    connection_id: uuid.UUID | None,
    headers: httpx.Headers,
    *,
    is_error: bool,
    path: str = "",
) -> UsageSnapshot:
    """Parsea los headers de uso y persiste el agregado diario. Se llama en
    TODA respuesta de Graph API, éxito o error (el uso se consume igual)."""
    snapshot = parse_usage(headers)

    if connection_id is not None and tenant_id is not None:
        today = datetime.combine(date.today(), datetime.min.time(), tzinfo=UTC)
        async with get_session() as session:
            stmt = pg_insert(GraphApiUsage).values(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                connection_id=connection_id,
                usage_date=today,
                call_count=1,
                error_count=1 if is_error else 0,
            )
            stmt = stmt.on_conflict_do_update(
                constraint="uq_graph_api_usage_connection_date",
                set_={
                    "call_count": GraphApiUsage.__table__.c.call_count + 1,
                    "error_count": GraphApiUsage.__table__.c.error_count + (1 if is_error else 0),
                    "updated_at": datetime.now(UTC),
                },
            )
            await session.execute(stmt)

    if snapshot.worst_pct >= PAUSE_THRESHOLD_PCT:
        log.error(
            "graph_api_usage_critico",
            path=path,
            worst_pct=snapshot.worst_pct,
            headers=safe_log_headers(headers),
        )
        if connection_id is not None and tenant_id is not None:
            await set_pause_flag(tenant_id, connection_id)
    elif snapshot.worst_pct >= WARN_THRESHOLD_PCT:
        log.warning(
            "graph_api_usage_alto",
            path=path,
            worst_pct=snapshot.worst_pct,
            headers=safe_log_headers(headers),
        )

    return snapshot


async def connection_id_for_phone_number(
    phone_number_id: str, tenant_id: uuid.UUID | None = None
) -> uuid.UUID | None:
    """`PhoneNumber.phone_number_id` -> `PhoneNumber.connection_id` (MetaConnection).

    Filtra por `tenant_id` explícito además de RLS (defensa en profundidad,
    igual criterio que `get_owned()` en `core/db/session.py` -- hallazgo
    auditor-multitenant, 2026-09-19: antes dependía 100% de que `set_config`
    hubiera corrido). `tenant_id=None` cae al contexto ambiente.

    Devuelve `None` si no hay fila -- hoy siempre pasa así, porque nadie
    sincroniza `PhoneNumber` desde Graph API todavía (tabla vacía). Es
    fail-CLOSED, no fail-open: el freno de uso simplemente no aplica todavía
    para ningún tenant, no se omite un chequeo de seguridad."""
    from core.db.models import PhoneNumber
    from core.shared.tenant import get_current_tenant_id

    if not phone_number_id:
        return None
    tid = tenant_id if tenant_id is not None else get_current_tenant_id()
    if tid is None:
        return None
    async with get_session() as session:
        row = (
            await session.execute(
                select(PhoneNumber.connection_id).where(
                    PhoneNumber.phone_number_id == phone_number_id, PhoneNumber.tenant_id == tid
                )
            )
        ).scalar_one_or_none()
    return row
