"""Refresco diario de los tokens de larga duración de Instagram Login
(ruta b) -- la ruta (a) reusa el token de Página de Messenger, que no
expira de la misma forma y no necesita este cron.

CONDICIONES documentadas por Meta antes de refrescar (las 3 se comprueban):
1. El token debe ser VÁLIDO -- se verifica intentando el refresco mismo
   (no hay endpoint de introspección confirmado para `graph.instagram.com`
   en 2026); si Meta lo rechaza, se marca la cuenta `degraded`.
2. Al menos 24 HORAS de antigüedad -- derivado de `expires_at` (todo token
   se guarda con `expires_at = emitido + 60 días`, así que la antigüedad es
   `ahora - (expires_at - 60 días)`), sin agregar una columna nueva a la
   tabla de custodia de tokens (ya auditada por 3 tickets anteriores).
3. `instagram_business_basic` concedido -- se da por cumplido por
   construcción: es el ÚNICO scope que la ruta (b) pide en el diálogo de
   autorización, no existe combinación donde el token exista sin él.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta

import httpx
from sqlalchemy import select

from core.db.models import IgAccount, MetaConnection, utcnow
from core.db.session import get_session
from core.gateway import meta_token_store
from core.gateway.meta_errors import GraphError, classify_and_build_error
from core.gateway.meta_instagram import (
    IG_GRAPH_BASE,
    IG_LONG_LIVED_TOKEN_KIND,
    LONG_LIVED_DAYS,
    REFRESH_MIN_AGE_HOURS,
    REFRESH_WINDOW_DAYS,
)
from core.shared.logger import get_logger
from core.shared.tenant import tenant_scope

log = get_logger(__name__)

_TIMEOUT_S = 15.0


async def _refresh_one(access_token: str) -> tuple[str, datetime]:
    async with httpx.AsyncClient(timeout=_TIMEOUT_S) as client:
        response = await client.get(
            f"{IG_GRAPH_BASE}/refresh_access_token",
            params={"grant_type": "ig_refresh_token", "access_token": access_token},
        )
    if response.status_code >= 400:
        try:
            body = response.json()
        except Exception:
            body = None
        raise classify_and_build_error(http_status=response.status_code, body=body, headers=response.headers, raw_text=response.text)
    token = (response.json() or {}).get("access_token")
    if not token:
        raise GraphError("Instagram no devolvió access_token en el refresco", category="PERMANENT")
    return str(token), utcnow() + timedelta(days=LONG_LIVED_DAYS)


async def _eligible_accounts() -> list[tuple[uuid.UUID, str, uuid.UUID]]:
    """(connection_id, ig_user_id, tenant_id) de cada `IgAccount` de la
    ruta (b) -- filtradas por elegibilidad se hacen una por una en
    `refresh_instagram_tokens` (necesitan `tenant_scope` para leer el
    token de cada una)."""
    async with get_session() as session:
        rows = (
            await session.execute(select(IgAccount).where(IgAccount.login_route == "ig_instagram_login"))
        ).scalars().all()
    return [(row.connection_id, row.ig_user_id, row.tenant_id) for row in rows]


async def refresh_instagram_tokens() -> None:
    """Nunca levanta excepción: una cuenta caída no debe interrumpir el
    refresco de las demás (mismo criterio que `meta_subscriptions_health.py`)."""
    for connection_id, ig_user_id, tenant_id in await _eligible_accounts():
        try:
            await _maybe_refresh_one(connection_id, ig_user_id, tenant_id)
        except Exception:
            log.exception("instagram_token_refresh_error_inesperado", ig_user_id=ig_user_id)


async def _maybe_refresh_one(connection_id: uuid.UUID, ig_user_id: str, tenant_id: uuid.UUID) -> None:
    async with tenant_scope(tenant_id):
        expires_at = await meta_token_store.get_token_expiry(
            connection_id, IG_LONG_LIVED_TOKEN_KIND, ig_user_id, tenant_id=tenant_id
        )
        if expires_at is None:
            return  # sin token guardado -- nada que refrescar

        issued_at = expires_at - timedelta(days=LONG_LIVED_DAYS)
        age = utcnow() - issued_at
        if age < timedelta(hours=REFRESH_MIN_AGE_HOURS):
            log.info("instagram_token_refresh_omitido_muy_nuevo", ig_user_id=ig_user_id, age_hours=age.total_seconds() / 3600)
            return
        if expires_at > utcnow() + timedelta(days=REFRESH_WINDOW_DAYS):
            return  # todavía no entra en la ventana de refresco

        token = await meta_token_store.get_token(connection_id, IG_LONG_LIVED_TOKEN_KIND, ig_user_id, tenant_id=tenant_id)
        if not token:
            return

        try:
            new_token, new_expires_at = await _refresh_one(token)
        except GraphError as exc:
            log.error("instagram_token_refresh_fallo", ig_user_id=ig_user_id, detail=exc.message)
            async with get_session() as session:
                account = (
                    await session.execute(
                        select(IgAccount).where(
                            IgAccount.ig_user_id == ig_user_id,
                            IgAccount.connection_id == connection_id,
                            IgAccount.tenant_id == tenant_id,
                        )
                    )
                ).scalar_one_or_none()
                if account is not None:
                    account.status = "degraded"
                    account.status_reason = f"Refresco de token falló: {exc.message} -- reconexión requerida"
                db_connection = await session.get(MetaConnection, connection_id)
                if db_connection is not None:
                    db_connection.status_reason = "instagram_token_refresh_fallo"
            return  # nunca reintenta en el mismo tick

        await meta_token_store.put_token(
            connection_id, IG_LONG_LIVED_TOKEN_KIND, ig_user_id, new_token, expires_at=new_expires_at, tenant_id=tenant_id
        )
        async with get_session() as session:
            account = (
                await session.execute(
                    select(IgAccount).where(
                        IgAccount.ig_user_id == ig_user_id,
                        IgAccount.connection_id == connection_id,
                        IgAccount.tenant_id == tenant_id,
                    )
                )
            ).scalar_one_or_none()
            if account is not None and account.status == "degraded":
                account.status = "connected"
                account.status_reason = None
