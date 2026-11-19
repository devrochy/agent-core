"""Alta de Instagram con Instagram Login (ruta b de HU-CHAN-04, ADR-0152).

A diferencia de Facebook Login (`meta_instagram_alta`) no hay Página ni lista que elegir: la persona autoriza UNA cuenta
profesional directamente con Instagram. Dos pasos:

1. `iniciar_login`: da la dirección de autorización de Instagram. Su `state` es un intento de un solo uso atado al negocio
   y al admin (Redis, 15 min), no una firma con un secreto compartido: no hay secreto que pueda faltar ni degradarse
   (el diseño viejo firmaba con el App Secret y el auditor exigió que no cayera a uno débil).
2. `completar_login`: consume el `state`, canjea el código (token corto → token de 60 días), lee la cuenta, la reclama
   (un solo dueño, ADR-0095), guarda su fila, su conexión canónica y su token, y suscribe y VERIFICA el webhook.

Va detrás de `INSTAGRAM_LOGIN_ENABLED` (apagada por defecto): se prende tras probarla con una cuenta real en test.
La cuenta de Meta del negocio es otra que la de Facebook Login (`meta_app_id` = la app de Instagram), así que las dos
rutas conviven sin pisarse; si la misma cuenta pasa de una ruta a la otra, su conexión canónica se religa.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from core.config import get_settings
from core.db.models import IgAccount, MetaConnection, utcnow
from core.db.session import get_session
from core.gateway import meta_token_store
from core.gateway.meta_asset_claims import MetaAssetError, claim_messaging_assets
from core.gateway.meta_errors import GraphError
from core.gateway.meta_instagram import (
    ESTADO_CONECTADA,
    LOGIN_ROUTE_INSTAGRAM,
    con_estado,
)
from core.gateway.meta_instagram_alta import InstagramAltaError, exigir_plan
from core.gateway.meta_instagram_cuentas import (
    MENSAJE_CHOQUE_MANUAL,
    activar_ruta,
    hay_conexion_manual,
)
from core.gateway.meta_instagram_login import (
    IG_LONG_LIVED_TOKEN_KIND,
    InstagramLoginError,
    canjear_codigo,
    canjear_por_token_largo,
    datos_de_la_cuenta,
    faltan_para_instagram_login,
    suscribir_y_verificar,
    url_de_autorizacion,
)
from core.gateway.meta_messenger_alta import (
    INTENTO_TTL_S as INTENTO_TTL_S,  # re-exportado: la API informa cuánto dura el intento
)
from core.gateway.meta_messenger_alta import (
    _consumir_intento,
)
from core.gateway.meta_messenger_alta import iniciar_intento as _iniciar_intento
from core.shared.logger import get_logger

log = get_logger(__name__)

CANAL_DEL_INTENTO = "instagram_login"


@dataclass(frozen=True)
class LoginIniciado:
    url: str
    expires_in: int


@dataclass(frozen=True)
class CuentaConectadaPorLogin:
    ig_user_id: str
    username: str | None
    channel_connection_id: uuid.UUID
    connection_id: uuid.UUID
    suscrita: bool
    campos: tuple[str, ...]


def login_disponible(settings: Any) -> list[str]:
    """Qué impide ofrecer la ruta b: `["INSTAGRAM_LOGIN_ENABLED"]` si está apagada, las variables que falten si no. Vacío =
    disponible."""
    if not settings.instagram_login_enabled:
        return ["INSTAGRAM_LOGIN_ENABLED"]
    return faltan_para_instagram_login(settings)


def _exigir_login(settings: Any) -> None:
    if (faltan := login_disponible(settings)) :
        log.warning("instagram_login_no_disponible", faltan=faltan)
        raise InstagramAltaError(
            "Conectar con Instagram no está disponible en este entorno.", code="instagram_login_unavailable", status=409
        )


async def iniciar_login(tenant_id: uuid.UUID, admin_id: uuid.UUID) -> LoginIniciado:
    settings = get_settings()
    _exigir_login(settings)
    await exigir_plan(tenant_id)
    state = await _iniciar_intento(tenant_id, admin_id, CANAL_DEL_INTENTO)
    return LoginIniciado(url=url_de_autorizacion(state), expires_in=INTENTO_TTL_S)


async def _buscar_cuenta(tenant_id: uuid.UUID, settings: Any) -> MetaConnection | None:
    async with get_session() as session:
        return (
            await session.execute(
                select(MetaConnection).where(
                    MetaConnection.tenant_id == tenant_id,
                    MetaConnection.channel == "instagram",
                    MetaConnection.environment == "production",
                    MetaConnection.meta_app_id == settings.instagram_app_id,
                    MetaConnection.deleted_at.is_(None),
                )
            )
        ).scalars().first()


async def _cuenta_del_negocio(tenant_id: uuid.UUID, settings: Any) -> MetaConnection:
    """La cuenta viva de Instagram del negocio con la app de Instagram; la crea en `draft` si no hay. Dos altas
    simultáneas chocan con el índice único parcial: la perdedora lee la que creó la otra."""
    if (existente := await _buscar_cuenta(tenant_id, settings)) is not None:
        return existente
    try:
        async with get_session() as session:
            cuenta = MetaConnection(
                tenant_id=tenant_id, channel="instagram", environment="production", origin="embedded_signup",
                meta_app_id=settings.instagram_app_id, graph_api_version=settings.graph_api_version, status="draft",
            )
            session.add(cuenta)
            await session.flush()
            return cuenta
    except IntegrityError:
        if (existente := await _buscar_cuenta(tenant_id, settings)) is not None:
            return existente
        raise


async def completar_login(
    tenant_id: uuid.UUID, admin_id: uuid.UUID, code: str, state: str
) -> CuentaConectadaPorLogin:
    settings = get_settings()
    _exigir_login(settings)
    await exigir_plan(tenant_id)
    await _consumir_intento(tenant_id, admin_id, state, CANAL_DEL_INTENTO)
    if not code or len(code) > 2048:
        raise InstagramAltaError("Falta el código de Instagram.", code="missing_code")

    try:
        token_corto, user_id_del_canje = await canjear_codigo(code)
        token_largo, vence = await canjear_por_token_largo(token_corto)
        ig_user_id, username = await datos_de_la_cuenta(token_largo)
    except (GraphError, InstagramLoginError, httpx.HTTPError) as exc:
        log.warning("instagram_login_canje_rechazado", tenant_id=str(tenant_id), detail=getattr(exc, "code", None))
        raise InstagramAltaError(
            "Instagram no aceptó la autorización (el código vence a la hora): vuelve a conectar con Instagram.",
            code="meta_error",
        ) from exc
    finally:
        del code
    # Manda el `user_id` de `/me` con el token largo: es la cuenta profesional, el `entry.id` con el que Meta entrega sus
    # mensajes. El del canje puede ser el identificador con alcance de la app; si difieren solo se registra (sin los ids,
    # que no hacen falta) para confirmarlo con la primera cuenta real.
    if user_id_del_canje and user_id_del_canje != ig_user_id:
        log.warning("instagram_login_ids_distintos", tenant_id=str(tenant_id))
    if not (ig_user_id.isascii() and ig_user_id.isdigit()) or len(ig_user_id) > 32:
        raise InstagramAltaError("Instagram devolvió un identificador de cuenta inválido.", code="meta_error")

    cuenta = await _cuenta_del_negocio(tenant_id, settings)
    try:
        canonica_id = await _guardar_cuenta(tenant_id, admin_id, cuenta.id, ig_user_id, username, token_largo, vence)
    except MetaAssetError as exc:
        raise InstagramAltaError(exc.message, code=exc.code, status=409) from exc

    version = cuenta.graph_api_version or settings.graph_api_version
    try:
        suscrita, campos = await suscribir_y_verificar(ig_user_id, token_largo, version)
    except (GraphError, InstagramLoginError, httpx.HTTPError) as exc:
        log.warning("instagram_login_suscripcion_fallo", ig_user_id=ig_user_id, detail=getattr(exc, "code", None))
        suscrita, campos = False, ()
    await _cerrar(tenant_id, admin_id, cuenta.id, ig_user_id, suscrita, campos)
    return CuentaConectadaPorLogin(ig_user_id, username, canonica_id, cuenta.id, suscrita, campos)


async def _guardar_cuenta(
    tenant_id: uuid.UUID, admin_id: uuid.UUID, connection_id: uuid.UUID, ig_user_id: str, username: str | None,
    token: str, vence: Any,
) -> uuid.UUID:
    """Reclama la cuenta y escribe su fila en UNA transacción; guarda el token DESPUÉS de que confirme y solo entonces
    religa la conexión canónica (`activar_ruta`): un alta que falla a la mitad no toca lo que ya funcionaba."""
    async with get_session() as session:
        await claim_messaging_assets(session, tenant_id, ig_user_id=ig_user_id)
        cuenta = await session.get(MetaConnection, connection_id)
        if cuenta is None or cuenta.deleted_at is not None:
            raise MetaAssetError(
                "La conexión se eliminó mientras se completaba el alta: empieza de nuevo.", code="connection_deleted"
            )
        if await hay_conexion_manual(session, tenant_id, ig_user_id):
            raise MetaAssetError(MENSAJE_CHOQUE_MANUAL, code="manual_connection_conflict")
        fila = (
            await session.execute(
                select(IgAccount).where(
                    IgAccount.tenant_id == tenant_id, IgAccount.connection_id == connection_id,
                    IgAccount.ig_user_id == ig_user_id,
                )
            )
        ).scalar_one_or_none()
        if fila is None:
            session.add(
                IgAccount(
                    tenant_id=tenant_id, connection_id=connection_id, ig_user_id=ig_user_id, username=username,
                    linked_page_id=None, login_route=LOGIN_ROUTE_INSTAGRAM,
                    raw=con_estado({}, ESTADO_CONECTADA),
                )
            )
        else:
            fila.username, fila.login_route, fila.linked_page_id = username, LOGIN_ROUTE_INSTAGRAM, None
            fila.raw = con_estado(fila.raw, ESTADO_CONECTADA)
        if cuenta.status == "draft":
            cuenta.status = "pending"
            cuenta.connected_by = admin_id
    await meta_token_store.put_token(
        connection_id, IG_LONG_LIVED_TOKEN_KIND, ig_user_id, token, expires_at=vence, tenant_id=tenant_id
    )
    return await activar_ruta(tenant_id, ig_user_id, connection_id, nombre=f"@{username}" if username else "")


async def _cerrar(
    tenant_id: uuid.UUID, admin_id: uuid.UUID, connection_id: uuid.UUID, ig_user_id: str, suscrita: bool,
    campos: tuple[str, ...],
) -> None:
    async with get_session() as session:
        fila = (
            await session.execute(
                select(IgAccount).where(
                    IgAccount.tenant_id == tenant_id, IgAccount.connection_id == connection_id,
                    IgAccount.ig_user_id == ig_user_id,
                )
            )
        ).scalar_one_or_none()
        if fila is not None:
            fila.raw = {**(fila.raw or {}), "subscribed_fields": list(campos)}
        cuenta = await session.get(MetaConnection, connection_id)
        if cuenta is not None and cuenta.tenant_id == tenant_id and suscrita:
            cuenta.status = "connected"
            cuenta.connected_at = utcnow()
            cuenta.connected_by = admin_id
