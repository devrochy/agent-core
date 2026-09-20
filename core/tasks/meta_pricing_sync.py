"""Cron de sync de `pricing_analytics` (diario): recorre todas las
`WabaAccount` de conexiones `connected` y sincroniza el costo autoritativo
de los últimos 3 días.

Capacidad de CORE, mismo criterio que `message_template_sync.py`/
`meta_subscriptions_health.py`: entrada directa en el `beat_schedule` de
`core/tasks/celery_app.py`, sin pasar por `register_cron`.

Ventana de 3 días (no solo "ayer"): el delay real de publicación de
`pricing_analytics` NO está documentado -- se eligió un margen
conservador. El upsert idempotente (`sync_pricing_daily`) hace que
repetir días ya sincronizados sea gratis.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from core.db.models import MetaConnection, WabaAccount
from core.db.session import get_session
from core.gateway import meta_token_store
from core.gateway.meta_errors import GraphError
from core.gateway.meta_onboarding import TOKEN_KIND
from core.gateway.meta_pricing_analytics import sync_pricing_daily
from core.shared.logger import get_logger
from core.shared.tenant import tenant_scope

log = get_logger(__name__)

_SYNC_WINDOW_DAYS = 3


async def _connected_wabas() -> list[tuple[WabaAccount, MetaConnection]]:
    async with get_session() as session:
        rows = (
            await session.execute(
                select(WabaAccount, MetaConnection)
                .join(MetaConnection, MetaConnection.id == WabaAccount.connection_id)
                .where(MetaConnection.status == "connected")
            )
        ).all()
    return [(waba, connection) for waba, connection in rows]


async def _sync_one(waba: WabaAccount, connection: MetaConnection) -> None:
    now = datetime.now(UTC)
    start_unix = int((now - timedelta(days=_SYNC_WINDOW_DAYS)).replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
    end_unix = int(now.timestamp())

    async with tenant_scope(connection.tenant_id):
        token = await meta_token_store.get_token(
            connection.id, TOKEN_KIND, connection.business_id or str(connection.id), tenant_id=connection.tenant_id
        )
        app_secret = await meta_token_store.get_token(
            connection.id, "app_access", connection.meta_app_id, tenant_id=connection.tenant_id
        )
        if not token or not app_secret:
            return
        try:
            await sync_pricing_daily(
                waba.waba_id, waba.id, connection.tenant_id, start_unix, end_unix,
                token, app_secret, connection.graph_api_version, connection.id,
            )
        except GraphError as exc:
            log.error("meta_pricing_sync_fallo", waba_id=waba.waba_id, detail=exc.message)


async def sync_all_waba_pricing() -> None:
    """Nunca levanta excepción: una WABA caída no debe interrumpir el sync
    de las demás (mismo criterio que `sync_all_waba_templates`)."""
    for waba, connection in await _connected_wabas():
        try:
            await _sync_one(waba, connection)
        except Exception:
            log.exception("meta_pricing_sync_error_inesperado", waba_id=waba.waba_id)
