"""Refresco diario del token de 60 días de Instagram Login (ruta b de HU-CHAN-04, ADR-0152).

La ruta de Facebook Login usa el token de la Página y no pasa por aquí. Condiciones de Meta antes de refrescar:

1. El token debe ser válido: no hay introspección confirmada para `graph.instagram.com`, así que se prueba refrescando; si
   Meta lo rechaza la cuenta queda `degraded` («reconectar»), SIN reintentar en el mismo ciclo.
2. Al menos 24 h de antigüedad: se deduce de `expires_at` (todo token se guarda con `emitido + 60 días`), sin columna nueva.
3. `instagram_business_basic` concedido: por construcción, es el único permiso base que pide el diálogo.

Solo se refrescan los que entran en la ventana de vencimiento (`REFRESH_WINDOW_DAYS`), para no gastar una llamada por
cuenta cada día. Cada cuenta se procesa bajo el `tenant_scope` de SU negocio (RLS) y una caída no frena a las demás.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

import httpx
from sqlalchemy import select

from core.db.admin_enum import admin_meta_connection_ids
from core.db.models import IgAccount, MetaConnection, utcnow
from core.db.session import get_session
from core.gateway import meta_token_store
from core.gateway.meta_errors import GraphError
from core.gateway.meta_instagram import (
    ESTADO_CONECTADA,
    ESTADO_DEGRADADA,
    LOGIN_ROUTE_INSTAGRAM,
    con_estado,
    estado_de,
)
from core.gateway.meta_instagram_login import (
    IG_LONG_LIVED_TOKEN_KIND,
    LONG_LIVED_DAYS,
    REFRESH_MIN_AGE_HOURS,
    REFRESH_WINDOW_DAYS,
    InstagramLoginError,
    refrescar_token,
)
from core.shared.logger import get_logger
from core.shared.tenant import tenant_scope

log = get_logger(__name__)

# Errores de Meta que NO dicen que el token esté muerto: se reintenta al día siguiente sin tocar el estado de la cuenta.
_CATEGORIAS_TRANSITORIAS = {"RATE_LIMIT", "TRANSIENT"}

# Estados de la cuenta de Meta que todavía pueden tener cuentas de Instagram Login vivas.
_ESTADOS = ["connected", "degraded", "pending"]


async def _cuentas_de_instagram_login() -> list[tuple[uuid.UUID, uuid.UUID, str]]:
    """`(connection_id, tenant_id, ig_user_id)` de cada cuenta de Instagram Login viva, de TODOS los negocios. Las
    cuentas de Meta se enumeran con la función de sistema (un `select` plano sin scope daría 0 filas por RLS) y cada una
    se lee bajo el scope de su negocio."""
    cuentas: list[tuple[uuid.UUID, uuid.UUID, str]] = []
    for connection_id, tenant_id in await admin_meta_connection_ids(_ESTADOS):
        # Una lectura que falla en UN negocio (sesión, fila rara) no debe dejar sin refresco a los demás: a los 10 días
        # vencerían todos los tokens y el envío dejaría de funcionar.
        try:
            async with tenant_scope(tenant_id):
                async with get_session() as session:
                    conexion = await session.get(MetaConnection, connection_id)
                    if conexion is None or conexion.tenant_id != tenant_id or conexion.channel != "instagram":
                        continue
                    filas = (
                        await session.execute(
                            select(IgAccount.ig_user_id).where(
                                IgAccount.connection_id == connection_id,
                                IgAccount.tenant_id == tenant_id,
                                IgAccount.login_route == LOGIN_ROUTE_INSTAGRAM,
                                # Una cuenta que cambió de ruta deja su fila vieja sin uso: no se refresca.
                                IgAccount.raw["superseded"].astext.is_distinct_from("true"),
                            )
                        )
                    ).scalars().all()
        except Exception:
            log.exception("instagram_token_refresh_enumeracion_fallo", connection_id=str(connection_id))
            continue
        cuentas.extend((connection_id, tenant_id, ig_user_id) for ig_user_id in filas)
    return cuentas


async def refresh_instagram_tokens() -> dict[str, int]:
    """Nunca levanta: una cuenta caída no debe interrumpir el refresco de las demás. Devuelve un resumen para el log."""
    resumen = {"refrescadas": 0, "omitidas": 0, "degradadas": 0, "errores": 0}
    for connection_id, tenant_id, ig_user_id in await _cuentas_de_instagram_login():
        try:
            resultado = await _refrescar_una(connection_id, tenant_id, ig_user_id)
        except Exception:
            log.exception("instagram_token_refresh_error_inesperado", ig_user_id=ig_user_id)
            resultado = "errores"
        resumen[resultado] += 1
    log.info("instagram_token_refresh", **resumen)
    return resumen


async def _refrescar_una(connection_id: uuid.UUID, tenant_id: uuid.UUID, ig_user_id: str) -> str:
    async with tenant_scope(tenant_id):
        vence = await meta_token_store.get_token_expires_at(
            connection_id, IG_LONG_LIVED_TOKEN_KIND, ig_user_id, tenant_id=tenant_id
        )
        if vence is None:
            return "omitidas"  # sin token guardado (o revocado): nada que refrescar
        ahora = utcnow()
        antiguedad = ahora - (vence - timedelta(days=LONG_LIVED_DAYS))
        if antiguedad < timedelta(hours=REFRESH_MIN_AGE_HOURS):
            return "omitidas"  # Meta rechaza refrescar un token de menos de 24 h
        if vence > ahora + timedelta(days=REFRESH_WINDOW_DAYS):
            return "omitidas"  # todavía no entra en la ventana de refresco
        token = await meta_token_store.get_token(
            connection_id, IG_LONG_LIVED_TOKEN_KIND, ig_user_id, tenant_id=tenant_id
        )
        if not token:
            return "omitidas"
        try:
            nuevo, nuevo_vence = await refrescar_token(token)
        except (GraphError, InstagramLoginError, httpx.HTTPError, ValueError) as exc:
            log.error("instagram_token_refresh_fallo", ig_user_id=ig_user_id, detail=getattr(exc, "message", str(exc)))
            if _es_transitorio(exc):
                # Un límite de uso, un 5xx o un corte de red no dicen nada del token: no se degrada la cuenta, se
                # vuelve a intentar mañana (el token aún está vigente).
                return "errores"
            await _marcar(connection_id, tenant_id, ig_user_id, ESTADO_DEGRADADA, "El refresco del token falló: reconecta la cuenta.")
            return "degradadas"  # nunca se reintenta en el mismo ciclo
        await meta_token_store.put_token(
            connection_id, IG_LONG_LIVED_TOKEN_KIND, ig_user_id, nuevo, expires_at=nuevo_vence, tenant_id=tenant_id
        )
        if estado_de(await _raw(connection_id, tenant_id, ig_user_id))[0] == ESTADO_DEGRADADA:
            await _marcar(connection_id, tenant_id, ig_user_id, ESTADO_CONECTADA, None)
        return "refrescadas"


def _es_transitorio(exc: Exception) -> bool:
    """¿El fallo no dice que el token esté muerto? Un límite de uso, un 5xx, un corte de red o una respuesta ilegible
    se reintentan mañana; un rechazo de autenticación o permanente de Meta degrada la cuenta."""
    if isinstance(exc, GraphError):
        return exc.category in _CATEGORIAS_TRANSITORIAS
    return True


async def _raw(connection_id: uuid.UUID, tenant_id: uuid.UUID, ig_user_id: str) -> dict:
    async with get_session() as session:
        fila = (
            await session.execute(
                select(IgAccount).where(
                    IgAccount.connection_id == connection_id, IgAccount.tenant_id == tenant_id,
                    IgAccount.ig_user_id == ig_user_id,
                )
            )
        ).scalar_one_or_none()
        return dict(fila.raw or {}) if fila is not None else {}


async def _marcar(
    connection_id: uuid.UUID, tenant_id: uuid.UUID, ig_user_id: str, estado: str, motivo: str | None
) -> None:
    async with get_session() as session:
        fila = (
            await session.execute(
                select(IgAccount).where(
                    IgAccount.connection_id == connection_id, IgAccount.tenant_id == tenant_id,
                    IgAccount.ig_user_id == ig_user_id,
                )
            )
        ).scalar_one_or_none()
        if fila is not None:
            fila.raw = con_estado(fila.raw, estado, motivo)
