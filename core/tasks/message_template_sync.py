"""Cron de sync de plantillas de WhatsApp (cada hora): recorre todas las
`WabaAccount` de conexiones `connected` y `degraded` y sincroniza sus plantillas.

`degraded` también (ADR-0102): si una conexión degradada dejaba de sincronizar, a las 25 h
el check de plantillas fallaba y no salía nunca de `degraded`. `error` no: o no tiene
credenciales o su token no sirve; cuando el cron de salud la pasa a `degraded`, vuelve acá.

Capacidad de CORE, mismo criterio que `meta_subscriptions_health.py`/
`instagram_token_refresh.py`: entrada directa en el `beat_schedule` de
`core/tasks/celery_app.py`, sin pasar por `register_cron`.
"""

from __future__ import annotations

from sqlalchemy import select

from core.db.models import MetaConnection, WabaAccount
from core.db.session import get_session
from core.gateway.meta_credentials import CredentialsUnavailable, credentials_for
from core.gateway.meta_errors import GraphError
from core.gateway.meta_template_sync import acquire_sync_lock, sync_templates
from core.shared.logger import get_logger
from core.shared.tenant import tenant_scope

log = get_logger(__name__)


_ESTADOS_A_SINCRONIZAR = ("connected", "degraded")


async def _wabas_a_sincronizar() -> list[tuple[WabaAccount, MetaConnection]]:
    """WABAs de conexiones `connected`/`degraded` de TODOS los tenants, primero las de
    conexiones `connected`.

    Ids cross-tenant vía `admin_meta_connection_ids` (SECURITY DEFINER); la conexión y sus
    WABAs se leen bajo el `tenant_scope` de cada tenant. Primero las sanas: el candado
    horario es por `(tenant, waba_id)`, y dos conexiones pueden compartir la WABA; una
    degradada no debe ganarle el turno a la sana."""
    from core.db.admin_enum import admin_meta_connection_ids

    out: list[tuple[WabaAccount, MetaConnection]] = []
    for connection_id, tenant_id in await admin_meta_connection_ids(list(_ESTADOS_A_SINCRONIZAR)):
        async with tenant_scope(tenant_id):
            async with get_session() as session:
                connection = await session.get(MetaConnection, connection_id)
                # Los dos tenants deben coincidir: el del enumerador y el de la conexión
                # (defensa en profundidad, igual que `meta_health_check`); el estado se
                # vuelve a mirar porque pudo cambiar desde que se enumeró.
                if (
                    connection is None
                    or connection.tenant_id != tenant_id
                    or connection.status not in _ESTADOS_A_SINCRONIZAR
                    or connection.deleted_at is not None
                ):
                    continue
                wabas = (
                    await session.execute(
                        select(WabaAccount).where(
                            WabaAccount.connection_id == connection.id, WabaAccount.tenant_id == tenant_id
                        )
                    )
                ).scalars().all()
                out.extend((waba, connection) for waba in wabas)
    return sorted(out, key=lambda pair: _ESTADOS_A_SINCRONIZAR.index(pair[1].status))


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
    for waba, connection in await _wabas_a_sincronizar():
        try:
            await _sync_one(waba, connection)
        except Exception:
            log.exception("message_template_sync_error_inesperado", waba_id=waba.waba_id)
