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
    from core.db.admin_enum import admin_meta_connection_ids, tiene_numeros
    from core.db.models import MetaConnection
    from core.db.session import get_session
    from core.gateway.meta_credentials import access_token_expires_at
    from core.shared.alerts import send_alert
    from core.shared.tenant import tenant_scope

    now = datetime.now(UTC)
    soon = now + timedelta(days=_WARN_DAYS)

    # `error` también: un token a menos de 7 días de vencer ya pone la conexión en `error`
    # (`meta_health.decide_status`), y desde ADR-0101 sigue recibiendo mensajes, así que el
    # aviso de vencimiento es lo que lo hace visible. Solo si todavía tiene números: un alta
    # fallida o reemplazada (el reintento le pasa el número a la nueva) avisaría cada día.
    for connection_id, tenant_id in await admin_meta_connection_ids(["connected", "degraded", "error"]):
        try:
            async with tenant_scope(tenant_id):
                async with get_session() as session:
                    connection = await session.get(MetaConnection, connection_id)
                    if connection is None or connection.tenant_id != tenant_id:
                        continue
                    if connection.status == "error" and not await tiene_numeros(session, connection.id, tenant_id):
                        continue
                # Solo el token que la conexión usa según su `origin` (ADR-0094): uno
                # viejo de otro origen no debe disparar alertas críticas falsas.
                expires_at = await access_token_expires_at(connection)
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
