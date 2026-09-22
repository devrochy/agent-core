"""Cron diario: alerta de access tokens de Meta vencidos o por vencer.

Recorre todas las `MetaConnection` (ids cross-tenant vía `admin_enum`) y avisa
por las que tienen el access token vencido o por vencer en <= 7 días. Sin esto,
un token temporal (24h) vence en silencio y el agente deja de responder -- bug
real: el envío empezó a fallar con 401/190 sin ningún aviso.

Capacidad de CORE: entrada directa en el `beat_schedule`, sin `register_cron`.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from core.shared.logger import get_logger

log = get_logger(__name__)

_WARN_DAYS = 7


async def check_tokens_expiry() -> None:
    """Nunca levanta excepción: una conexión con problema no debe interrumpir
    el chequeo de las demás."""
    from core.db.admin_enum import admin_meta_connection_ids
    from core.db.models import MetaConnection
    from core.db.session import get_session
    from core.gateway.meta_byok import CLOUD_API_ACCESS_TOKEN_KIND
    from core.gateway.meta_onboarding import TOKEN_KIND
    from core.gateway.meta_token_store import get_token_expires_at
    from core.shared.alerts import send_alert
    from core.shared.tenant import tenant_scope

    now = datetime.now(UTC)
    soon = now + timedelta(days=_WARN_DAYS)

    for connection_id, tenant_id in await admin_meta_connection_ids(["connected", "degraded"]):
        try:
            async with tenant_scope(tenant_id):
                async with get_session() as session:
                    connection = await session.get(MetaConnection, connection_id)
                    if connection is None:
                        continue
                    subject_byok = connection.meta_app_id
                    subject_es = connection.business_id or str(connection.id)
                expires_at = await get_token_expires_at(
                    connection_id, CLOUD_API_ACCESS_TOKEN_KIND, subject_byok, tenant_id=tenant_id
                )
                if expires_at is None:
                    expires_at = await get_token_expires_at(
                        connection_id, TOKEN_KIND, subject_es, tenant_id=tenant_id
                    )
            if expires_at is None or expires_at > soon:
                continue  # token permanente, sin dato, o con margen
            expired = expires_at <= now
            await send_alert(
                "Access token de Meta por vencer",
                f"La conexión {connection_id} (tenant {tenant_id}) tiene el access token "
                f"{'VENCIDO' if expired else 'por vencer'} ({expires_at.isoformat()}). "
                "Reconectá la app en /admin/conexiones para no dejar de responder.",
                severity="critical" if expired else "warning",
            )
        except Exception:
            log.exception("meta_token_expiry_check_error", connection_id=str(connection_id))
