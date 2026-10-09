"""Salud de conexiones Meta y suscripciones de webhook (fuente única SSR + API).

- `health_overview`: conexiones del tenant con su último diagnóstico.
- `diagnose_connection`: corre los checks on-demand y persiste el resultado.
- `resubscribe_waba`: reencola SOLO la suscripción de la WABA (nunca el OAuth).
- `set_billing_status`: persiste el estado que un admin marcó a mano (billing no
  tiene API en Meta).
- `webhook_subscriptions_status`: estado de la suscripción de APP por canal.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select, text

from core.db.models import MetaConnection, utcnow
from core.db.session import get_session
from core.gateway.meta_connection_state import get_live_meta_connection
from core.shared.logger import get_logger

log = get_logger(__name__)


async def health_overview(tenant_id: uuid.UUID) -> list[dict[str, Any]]:
    from core.gateway.meta_credentials import access_token_expires_at
    from core.gateway.meta_health import checks_de_salud

    async with get_session() as session:
        rows = (
            (
                await session.execute(
                    # Una conexión eliminada no tiene salud que mostrar.
                    select(MetaConnection).where(
                        MetaConnection.tenant_id == tenant_id, MetaConnection.deleted_at.is_(None)
                    )
                )
            )
            .scalars()
            .all()
        )
    out: list[dict[str, Any]] = []
    for c in rows:
        exp = await access_token_expires_at(c)
        out.append(
            {
                "id": c.id,
                "channel": c.channel,
                "environment": c.environment,
                "status": c.status,
                "status_reason": c.status_reason,
                "last_health_check_at": c.last_health_check_at,
                "health": dict(c.health or {}),
                "checks": checks_de_salud(c.health),
                "billing_manual_status": ((c.health or {}).get("billing") or {}).get("manual_status"),
                "business_id": c.business_id,
                "meta_app_id": c.meta_app_id,
                "token_expires_at": exp,
            }
        )
    return out


_ESTADOS_SIN_DIAGNOSTICO = ("revoked", "draft")


async def diagnose_connection(tenant_id: uuid.UUID, connection_id: str) -> str | None:
    """Corre los checks y persiste estado. Devuelve un error legible o None."""
    from core.gateway.meta_health import (
        merge_health,
        misma_conexion,
        next_status,
        run_health_checks,
    )

    try:
        cid = uuid.UUID(connection_id)
    except ValueError:
        return "Conexión inválida"

    async with get_session() as session:
        connection = await get_live_meta_connection(session, cid, tenant_id)
    if connection is None:
        return "Conexión no encontrada"

    checks = await run_health_checks(connection, tenant_id)

    async with get_session() as session:
        # FOR UPDATE: si alguien la desactiva mientras tanto, o esperamos su commit y vemos
        # `revoked`, o su UPDATE espera al nuestro y gana. Nunca se pisa la desactivación.
        db_connection = (
            await session.execute(
                select(MetaConnection)
                .where(MetaConnection.id == cid, MetaConnection.tenant_id == tenant_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        # Si el wizard la reconectó mientras tanto, el diagnóstico es de otra credencial.
        if db_connection is not None and misma_conexion(connection, db_connection):
            db_connection.health = merge_health(db_connection.health, checks)
            db_connection.last_health_check_at = utcnow()
            # Diagnosticar no reactiva: una conexión desactivada (`revoked`), sin alta (`draft`)
            # o borrada conserva su estado. Con `degraded`/`error` volvería a prestar su
            # secreto (ADR-0101); solo se reactiva a propósito, desde `set_connection_status`.
            # Y solo mejora con el diagnóstico verificado (ADR-0102): un alta fallida sin
            # token no pasa a `connected` ni le quita el secreto a la conexión sana.
            if db_connection.status not in _ESTADOS_SIN_DIAGNOSTICO and db_connection.deleted_at is None:
                db_connection.status, db_connection.status_reason = next_status(
                    db_connection.status, db_connection.status_reason, db_connection.origin, checks
                )
    return None


async def resubscribe_waba(tenant_id: uuid.UUID, connection_id: str) -> str | None:
    """Reparación inline del check `webhook_subscribed` (solo la WABA)."""
    from core.admin.meta_numbers_service import resolve_waba_and_credentials
    from core.gateway.meta_errors import GraphError
    from core.gateway.meta_onboarding import _subscribe_waba_to_app

    try:
        cid = uuid.UUID(connection_id)
    except ValueError:
        return "Conexión inválida"

    connection, waba, creds = await resolve_waba_and_credentials(cid, tenant_id)
    if connection is None or waba is None or creds is None:
        return "No hay conexión/WABA lista todavía"

    try:
        await _subscribe_waba_to_app(
            creds.access_token,
            waba.waba_id,
            creds.graph_api_version,
            connection.id,
            creds.app_secret,
            tenant_id,
        )
    except GraphError as exc:
        return exc.message[:200]
    except Exception as exc:
        return str(exc)[:200]
    return None


async def set_billing_status(
    tenant_id: uuid.UUID, connection_id: str, manual_status: str
) -> str | None:
    try:
        cid = uuid.UUID(connection_id)
    except ValueError:
        return "Conexión inválida"

    async with get_session() as session:
        connection = await get_live_meta_connection(session, cid, tenant_id)
        if connection is None:
            return "Conexión no encontrada"
        from core.gateway.meta_health import check_billing

        # El check se rearma con el estado nuevo: guardar solo `manual_status` lo sacaba de
        # la lista de checks hasta el próximo diagnóstico, y el detalle quedaba viejo.
        connection.health = {**(connection.health or {}), "billing": {"manual_status": manual_status}}
        billing = check_billing(connection).model_dump(mode="json")
        connection.health = {**connection.health, "billing": {**billing, "manual_status": manual_status}}
    return None


async def webhook_subscriptions_status(tid: uuid.UUID | None) -> list[dict[str, Any]]:
    """Una fila por canal conectado, con el estado de su suscripción de APP."""
    from core.gateway.meta_subscriptions import OBJECT_TO_CHANNEL, list_app_subscriptions

    if tid is None:
        return []
    channel_to_object = {v: k for k, v in OBJECT_TO_CHANNEL.items()}

    async with get_session() as session:
        connections = (
            (
                await session.execute(
                    select(MetaConnection).where(
                        MetaConnection.tenant_id == tid,
                        MetaConnection.status == "connected",
                        MetaConnection.deleted_at.is_(None),
                    )
                )
            )
            .scalars()
            .all()
        )

    rows: list[dict[str, Any]] = []
    for connection in connections:
        object_ = channel_to_object.get(connection.channel)
        if object_ is None:
            continue
        subs = await list_app_subscriptions(connection.meta_app_id)
        sub = next((s for s in subs if s.object == object_), None)
        rows.append(
            {
                "channel": connection.channel,
                "object": object_,
                "meta_app_id": connection.meta_app_id,
                "callback_url": sub.callback_url if sub else None,
                "active": sub.active if sub else None,
                "fields": sub.fields if sub else [],
                "last_error": sub.last_error if sub else None,
            }
        )
    return rows


async def register_channel_subscription(tid: uuid.UUID, channel: str) -> str | None:
    """Registra la suscripción de APP para un canal (owner/master). Error o None."""
    from core.config import get_settings
    from core.gateway.meta_preflight import resolve_app_credentials
    from core.gateway.meta_subscriptions import (
        OBJECT_TO_CHANNEL,
        SubscriptionError,
        humanize_subscription_error,
        upsert_app_subscription,
    )
    from core.shared.exceptions import AgentCoreError
    from core.shared.tenant import tenant_scope

    channel_to_object = {v: k for k, v in OBJECT_TO_CHANNEL.items()}
    object_ = channel_to_object.get(channel)
    if object_ is None:
        return f"Canal desconocido: {channel}"

    config = await resolve_app_credentials(tid, channel)
    if config is None:
        return "No hay app de Meta configurada para este canal"

    settings = get_settings()
    callback_url = f"{settings.public_base_url.rstrip('/')}/webhooks/whatsapp"
    try:
        async with tenant_scope(tid):
            await upsert_app_subscription(
                config, object_, callback_url=callback_url, verify_token=settings.whatsapp_verify_token
            )
    except SubscriptionError as exc:
        return str(exc)[:140]
    except AgentCoreError as exc:  # GraphError
        return humanize_subscription_error(exc)[:140]
    return None


# "Activar" diagnostica en el request (como "Diagnosticar"): 2 + 2N llamadas a Graph de 15 s
# cada una. Se acota el total para no colgar el panel.
_ACTIVAR_TIMEOUT_S = 60.0


def _motivo_sin_verificar(checks: dict[str, dict[str, Any]], origin: str) -> str:
    """Por qué el diagnóstico no alcanza para reactivar (las reglas de `next_status`)."""
    from core.gateway.meta_health import OWN_APP_SUBSCRIBED

    claves = ["token_valid"]
    if origin == "byok":
        claves += ["app_subscription_active", OWN_APP_SUBSCRIBED]
    for clave in claves:
        check = checks.get(clave) or {}
        if check.get("ok") is not True:
            detalle = check.get("detail") or "no se pudo verificar"
            return f"No se puede reactivar: {clave} — {detalle}"
    return "No se puede reactivar: el diagnóstico no quedó verificado"


async def set_connection_status(
    tenant_id: uuid.UUID, connection_id: str, *, active: bool
) -> str | None:
    """Desactiva (pausa, `revoked`) o reactiva una conexión Meta viva del tenant (ADR-0122).

    - **Desactivar** pausa de verdad: lo que entra se guarda sin IA y no sale nada por ella.
    - **Activar** solo aplica a una pausada, y solo si el diagnóstico en el acto queda
      verificado (`next_status`, ADR-0102): token válido y, en BYOK, su app y la WABA
      suscritas. Queda `connected` o `degraded`; con `error` no se reactiva. `degraded` y
      `error` no se activan a mano: se recuperan solas con el diagnóstico.
    """
    import asyncio

    from core.gateway.meta_health import (
        merge_health,
        misma_conexion,
        next_status,
        run_health_checks,
    )
    from core.tasks.meta_subscriptions_health import refrescar_suscripcion_de_conexion

    try:
        cid = uuid.UUID(connection_id)
    except ValueError:
        return "Conexión inválida"

    if not active:
        async with get_session() as session:
            connection = await get_live_meta_connection(session, cid, tenant_id, for_update=True)
            if connection is None:
                return "Conexión no encontrada"
            connection.status = "revoked"
            connection.status_reason = "Desactivada a mano desde el panel"
        return None

    async with get_session() as session:
        connection = await get_live_meta_connection(session, cid, tenant_id)
    if connection is None:
        return "Conexión no encontrada"
    if connection.status == "connected":
        return None
    if connection.status in ("draft", "pending"):
        # Un alta que no terminó: sin token o con filas sin verificar (ADR-0095).
        return "La conexión no terminó el alta: complétala con el asistente de WhatsApp"
    if connection.status != "revoked":
        return (
            "La conexión no está desactivada: se recupera sola cuando el diagnóstico pasa. "
            "Si el token venció, reconéctala con el asistente de WhatsApp."
        )

    try:
        await refrescar_suscripcion_de_conexion(connection)
        checks = await asyncio.wait_for(run_health_checks(connection, tenant_id), timeout=_ACTIVAR_TIMEOUT_S)
    except TimeoutError:
        return "Meta no respondió a tiempo: intenta de nuevo en unos minutos"

    # Una pausada se trata como `error`: solo mejora con el diagnóstico verificado.
    status, reason = next_status("error", None, connection.origin, checks)
    if status == "error":
        # Los checks que dan `error` son los mismos que verifican: se dice cuál y por qué.
        return _motivo_sin_verificar(checks, connection.origin)

    async with get_session() as session:
        db_connection = await get_live_meta_connection(session, cid, tenant_id, for_update=True)
        if db_connection is None or db_connection.status != "revoked" or not misma_conexion(connection, db_connection):
            return "La conexión cambió mientras se diagnosticaba: revisa su estado e intenta de nuevo"
        db_connection.status, db_connection.status_reason = status, reason
        db_connection.health = merge_health(db_connection.health, checks)
        db_connection.last_health_check_at = utcnow()
    return None


async def soft_delete_connection(tenant_id: uuid.UUID, connection_id: str) -> str | None:
    """Elimina la conexión sin perder historia (invariante 8, ADR-0122).

    - La conexión, sus números y sus WABAs quedan con `deleted_at`; plantillas, precios y
      costos siguen colgando de la WABA. Reconectar el mismo número o la misma WABA en el
      negocio revive esas filas.
    - Todos sus tokens quedan revocados en el token store.
    - En Embedded Signup y manual se desuscribe la app de la plataforma de la WABA, salvo que
      otra conexión viva del negocio la use: Meta deja de mandar eventos. En BYOK no: la app
      es del negocio, y la firma de una conexión eliminada ya se rechaza. Mejor esfuerzo y
      **después** de confirmar el borrado, sin transacción abierta durante la llamada.
    """
    from sqlalchemy import update

    from core.db.models import PhoneNumber, WabaAccount
    from core.gateway import meta_token_store
    from core.gateway.meta_credentials import CredentialsUnavailable, credentials_for

    try:
        cid = uuid.UUID(connection_id)
    except ValueError:
        return "Conexión inválida"

    async with get_session() as session:
        connection = await get_live_meta_connection(session, cid, tenant_id)
    if connection is None:
        return "Conexión no encontrada"

    # Las credenciales se leen antes: una conexión eliminada ya no las presta.
    creds = None
    if connection.origin in ("embedded_signup", "manual"):
        try:
            creds = await credentials_for(connection)
        except CredentialsUnavailable:
            creds = None

    now = utcnow()
    async with get_session() as session:
        db_connection = await get_live_meta_connection(session, cid, tenant_id, for_update=True)
        if db_connection is None:
            return "Conexión no encontrada"
        db_connection.deleted_at = now
        db_connection.status = "revoked"
        db_connection.status_reason = "Eliminada desde el panel"
        wabas = (
            await session.execute(
                update(WabaAccount)
                .where(
                    WabaAccount.connection_id == cid,
                    WabaAccount.tenant_id == tenant_id,
                    WabaAccount.deleted_at.is_(None),
                )
                .values(deleted_at=now)
                .returning(WabaAccount.waba_id)
            )
        ).scalars().all()
        await session.execute(
            update(PhoneNumber)
            .where(
                PhoneNumber.connection_id == cid,
                PhoneNumber.tenant_id == tenant_id,
                PhoneNumber.deleted_at.is_(None),
            )
            .values(deleted_at=now)
        )

    revocados = await meta_token_store.revoke_all_for_connection(cid, tenant_id=tenant_id)
    log.info("meta_connection_eliminada", connection_id=str(cid), tokens_revocados=revocados)

    if creds is not None:
        for waba_id in sorted(set(wabas)):
            await _desuscribir_waba_de_la_plataforma(tenant_id, cid, waba_id, creds)
    return None


async def _desuscribir_waba_de_la_plataforma(
    tenant_id: uuid.UUID, connection_id: uuid.UUID, waba_id: str, creds: Any
) -> None:
    """`DELETE /{waba_id}/subscribed_apps` con el token de la conexión eliminada (desuscribe
    la app de la plataforma). Se salta si otra conexión viva del negocio que usa la app de la
    plataforma tiene la misma WABA. Mejor esfuerzo: un fallo se registra y nada más."""
    from core.db.models import MetaConnection as _Meta
    from core.db.models import WabaAccount
    from core.gateway.meta_errors import GraphError
    from core.gateway.meta_graph import graph_fetch

    async with get_session() as session:
        compartida = (
            await session.execute(
                select(WabaAccount.id)
                .join(_Meta, _Meta.id == WabaAccount.connection_id)
                .where(
                    WabaAccount.tenant_id == tenant_id,
                    WabaAccount.waba_id == waba_id,
                    WabaAccount.deleted_at.is_(None),
                    _Meta.tenant_id == tenant_id,
                    _Meta.deleted_at.is_(None),
                    _Meta.origin.in_(("embedded_signup", "manual")),
                    _Meta.id != connection_id,
                )
                .limit(1)
            )
        ).first()
        # Otro negocio pudo reclamarla (o la usa por una conexión manual): desuscribir le
        # cortaría la entrada (auditor-multitenant).
        de_otro = (
            await session.execute(
                text("select meta_asset_claimed_by_other_tenant(:w, NULL, :t)"), {"w": waba_id, "t": tenant_id}
            )
        ).scalar_one()
    if compartida is not None or de_otro:
        log.info("waba_compartida_no_se_desuscribe", waba_id=waba_id, connection_id=str(connection_id))
        return
    try:
        await graph_fetch(
            f"/{waba_id}/subscribed_apps", method="DELETE", token=creds.access_token, app_secret=creds.app_secret,
            version=creds.graph_api_version, connection_id=connection_id,
        )
        log.info("waba_desuscrita_al_eliminar", waba_id=waba_id, connection_id=str(connection_id))
    except GraphError as exc:
        log.warning("waba_no_desuscrita_al_eliminar", waba_id=waba_id, connection_id=str(connection_id), error=exc.message[:200])
    except Exception:
        log.warning("waba_no_desuscrita_al_eliminar", waba_id=waba_id, connection_id=str(connection_id), exc_info=True)
