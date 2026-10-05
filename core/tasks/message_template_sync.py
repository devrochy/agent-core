"""Cron de sync de plantillas de WhatsApp (cada hora): recorre todas las
`WabaAccount` de conexiones `connected` y sincroniza sus plantillas.

Capacidad de CORE, mismo criterio que `meta_subscriptions_health.py`/
`instagram_token_refresh.py`: entrada directa en el `beat_schedule` de
`core/tasks/celery_app.py`, sin pasar por `register_cron`.
"""

from __future__ import annotations

from core.db.models import MetaConnection, WabaAccount
from core.db.session import get_session
from core.gateway.meta_credentials import CredentialsUnavailable, credentials_for
from core.gateway.meta_errors import GraphError
from core.gateway.meta_template_sync import acquire_sync_lock, sync_templates
from core.shared.logger import get_logger
from core.shared.tenant import tenant_scope

log = get_logger(__name__)


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
                if waba is None or waba.tenant_id != tenant_id:
                    continue
                connection = await session.get(MetaConnection, waba.connection_id)
                # Los tres tenants deben coincidir: el del enumerador, el de la WABA y el
                # de la conexión (defensa en profundidad, igual que `meta_health_check`).
                if (
                    connection is not None
                    and connection.tenant_id == tenant_id
                    and connection.status == "connected"
                ):
                    out.append((waba, connection))
    return out


async def _sync_one(waba: WabaAccount, connection: MetaConnection) -> None:
    async with tenant_scope(connection.tenant_id):
        # Credenciales de ESTA conexión según su origen (HU-CONN-01). Se piden antes
        # del lock horario: sin credenciales no se gasta el turno de sincronización.
        try:
            creds = await credentials_for(connection, waba_id=waba.waba_id)
        except CredentialsUnavailable as exc:
            log.warning(
                "message_template_sync_sin_credenciales",
                waba_id=waba.waba_id, connection_id=str(connection.id),
                tenant_id=str(connection.tenant_id), reason=exc.reason,
            )
            return

        if not await acquire_sync_lock(connection.tenant_id, waba.waba_id):
            return  # ya sincronizada esta hora (cron o botón manual del panel)

        try:
            await sync_templates(
                waba.waba_id, creds.access_token, creds.app_secret, creds.graph_api_version,
                connection.id, connection.tenant_id,
            )
        except GraphError as exc:
            log.error(
                "message_template_sync_fallo",
                waba_id=waba.waba_id, connection_id=str(connection.id),
                tenant_id=str(connection.tenant_id), detail=exc.message,
            )


async def sync_all_waba_templates() -> None:
    """Nunca levanta excepción: una WABA caída no debe interrumpir el sync
    de las demás (mismo criterio que `check_all_subscriptions_health`)."""
    for waba, connection in await _connected_wabas():
        try:
            await _sync_one(waba, connection)
        except Exception:
            log.exception("message_template_sync_error_inesperado", waba_id=waba.waba_id)
