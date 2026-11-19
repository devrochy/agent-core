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

from core.db.models import MetaConnection, WabaAccount
from core.db.session import get_session
from core.gateway.meta_credentials import CredentialsUnavailable, credentials_for
from core.gateway.meta_errors import GraphError
from core.gateway.meta_pricing_analytics import sync_pricing_daily
from core.shared.logger import get_logger
from core.shared.tenant import tenant_scope

log = get_logger(__name__)

_SYNC_WINDOW_DAYS = 3


async def _connected_wabas() -> list[tuple[WabaAccount, MetaConnection]]:
    """WABAs de conexiones `connected` de TODOS los tenants (ids cross-tenant
    vía `admin_connected_waba_ids`, SECURITY DEFINER; hidratación bajo el
    `tenant_scope` de cada tenant)."""
    from core.db.admin_enum import admin_connected_waba_ids

    out: list[tuple[WabaAccount, MetaConnection]] = []
    for waba_id, tenant_id in await admin_connected_waba_ids():
        async with tenant_scope(tenant_id):
            async with get_session() as session:
                waba = await session.get(WabaAccount, waba_id)
                if waba is None or waba.tenant_id != tenant_id or waba.deleted_at is not None:
                    continue
                connection = await session.get(MetaConnection, waba.connection_id)
                # Los tres tenants deben coincidir: el del enumerador, el de la WABA y el
                # de la conexión (defensa en profundidad, igual que `meta_health_check`).
                if (
                    connection is not None
                    and connection.tenant_id == tenant_id
                    and connection.status == "connected"
                    and connection.deleted_at is None
                ):
                    out.append((waba, connection))
    return out


async def _sync_one(waba: WabaAccount, connection: MetaConnection) -> None:
    now = datetime.now(UTC)
    start_unix = int((now - timedelta(days=_SYNC_WINDOW_DAYS)).replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
    end_unix = int(now.timestamp())

    async with tenant_scope(connection.tenant_id):
        # Credenciales de ESTA conexión según su origen (HU-CONN-01): BYOK usa las
        # suyas y Embedded Signup/manual las de la plataforma.
        try:
            creds = await credentials_for(connection, waba_id=waba.waba_id)
        except CredentialsUnavailable as exc:
            log.warning(
                "meta_pricing_sync_sin_credenciales",
                waba_id=waba.waba_id, connection_id=str(connection.id),
                tenant_id=str(connection.tenant_id), reason=exc.reason,
            )
            return
        try:
            await sync_pricing_daily(
                waba.waba_id, waba.id, connection.tenant_id, start_unix, end_unix,
                creds.access_token, creds.app_secret, creds.graph_api_version, connection.id,
            )
        except GraphError as exc:
            log.error(
                "meta_pricing_sync_fallo",
                waba_id=waba.waba_id, connection_id=str(connection.id),
                tenant_id=str(connection.tenant_id), detail=exc.message,
            )


async def sync_all_waba_pricing() -> None:
    """Nunca levanta excepción: una WABA caída no debe interrumpir el sync
    de las demás (mismo criterio que `sync_all_waba_templates`)."""
    for waba, connection in await _connected_wabas():
        try:
            await _sync_one(waba, connection)
        except Exception:
            log.exception("meta_pricing_sync_error_inesperado", waba_id=waba.waba_id)
