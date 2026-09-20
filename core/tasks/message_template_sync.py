"""Cron de sync de plantillas de WhatsApp (cada hora): recorre todas las
`WabaAccount` de conexiones `connected` y sincroniza sus plantillas.

Capacidad de CORE, mismo criterio que `meta_subscriptions_health.py`/
`instagram_token_refresh.py`: entrada directa en el `beat_schedule` de
`core/tasks/celery_app.py`, sin pasar por `register_cron`.
"""

from __future__ import annotations

from sqlalchemy import select

from core.db.models import MetaConnection, WabaAccount
from core.db.session import get_session
from core.gateway import meta_token_store
from core.gateway.meta_errors import GraphError
from core.gateway.meta_onboarding import TOKEN_KIND
from core.gateway.meta_template_sync import acquire_sync_lock, sync_templates
from core.shared.logger import get_logger
from core.shared.tenant import tenant_scope

log = get_logger(__name__)


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
    if not await acquire_sync_lock(waba.waba_id):
        return  # ya sincronizada esta hora (cron o botón manual del panel)

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
            await sync_templates(
                waba.waba_id, token, app_secret, connection.graph_api_version,
                connection.id, connection.tenant_id,
            )
        except GraphError as exc:
            log.error("message_template_sync_fallo", waba_id=waba.waba_id, detail=exc.message)


async def sync_all_waba_templates() -> None:
    """Nunca levanta excepción: una WABA caída no debe interrumpir el sync
    de las demás (mismo criterio que `check_all_subscriptions_health`)."""
    for waba, connection in await _connected_wabas():
        try:
            await _sync_one(waba, connection)
        except Exception:
            log.exception("message_template_sync_error_inesperado", waba_id=waba.waba_id)
