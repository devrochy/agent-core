"""Alta de Instagram con Facebook Login: cuentas profesionales vinculadas a una Página (ADR-0152, HU-CHAN-04).

Mismo guion que el alta de Messenger (`meta_messenger_alta`), en tres llamadas porque el negocio ELIGE qué
cuentas conecta (cada una es una conexión canónica y entra en el plan):

1. `iniciar_intento`: valor de un solo uso, atado al negocio y al admin que abrió el popup (con su propio
   espacio: un intento de Messenger no conecta cuentas de Instagram).
2. `descubrir_cuentas`: consume el intento, canjea el `code` y lista las cuentas de Instagram vinculadas a las
   Páginas que el usuario administra (`GET /me/accounts` con `instagram_business_account`). A la API vuelven los
   datos de cada cuenta, NUNCA los tokens.
3. `conectar_cuentas`: por cada cuenta elegida, en UNA transacción, reclama la cuenta (un solo dueño), guarda
   su fila y su conexión canónica con sus instancias de agentes; luego guarda el token de su PÁGINA vinculada
   (con él se escribe), suscribe la Página y VERIFICA con un GET lo que Meta confirma de verdad.

Diferencias con Messenger: el activo es la cuenta (`ig_user_id`, el `entry.id` del webhook), no la Página; la
Página es solo el medio (su token y su suscripción). No hay `fb_pages` en este alta. La ruta por Instagram
Login (otra app de Meta y otros permisos) queda fuera de esta entrega.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field, replace
from typing import Any

from sqlalchemy import select

from core.config import get_settings
from core.db.models import IgAccount, MetaConnection, utcnow
from core.db.session import get_session
from core.gateway import meta_token_store
from core.gateway.conexion_canonica import asegurar_conexion_de_cuenta_ig
from core.gateway.meta_asset_claims import MetaAssetError, claim_messaging_assets
from core.gateway.meta_errors import GraphError
from core.gateway.meta_graph import graph_fetch
from core.gateway.meta_instagram import LOGIN_ROUTE_FACEBOOK
from core.gateway.meta_messenger import PAGE_TOKEN_KIND
from core.gateway.meta_messenger_alta import (
    INTENTO_TTL_S as INTENTO_TTL_S,  # re-exportado: la API informa cuánto dura el intento
)
from core.gateway.meta_messenger_alta import (
    MAX_PAGINAS_DE_GRAPH,
    MENSAJE_CHOQUE_MANUAL,
    USER_TOKEN_KIND,
    USER_TOKEN_SUBJECT,
    USER_TOKEN_TTL,
    MessengerAltaError,
    _campos_de_la_app,
    _consumir_intento,
    evaluar_pagina,
    plan_incluye_canal,
)
from core.gateway.meta_messenger_alta import iniciar_intento as _iniciar_intento
from core.shared.logger import get_logger

log = get_logger(__name__)

CANAL = "instagram"
InstagramAltaError = MessengerAltaError  # mismo error con código estable y estado HTTP

# El parámetro se llama `subscribed_fields`, NUNCA `fields` (Meta acepta el nombre equivocado sin suscribir nada).
IG_SUBSCRIBED_FIELDS = frozenset(
    {"messages", "messaging_postbacks", "messaging_seen", "message_reactions", "messaging_referral"}
)
# Sin `messages` Meta no entrega los mensajes directos aunque haya otros campos.
CAMPO_OBLIGATORIO = "messages"


@dataclass(frozen=True)
class CuentaCandidata:
    ig_user_id: str
    username: str | None
    page_id: str
    page_name: str
    tasks: tuple[str, ...]
    elegible: bool
    motivo: str | None = None
    # Solo en memoria durante `conectar_cuentas`: jamás sale en una respuesta ni en un log.
    page_access_token: str | None = field(default=None, repr=False)


@dataclass(frozen=True)
class Descubrimiento:
    connection_id: uuid.UUID
    cuentas: list[CuentaCandidata]
    ya_conectadas: frozenset[str]
    paginas_sin_instagram: int


@dataclass(frozen=True)
class CuentaConectada:
    ig_user_id: str
    channel_connection_id: uuid.UUID
    suscrita: bool
    campos: tuple[str, ...]


@dataclass(frozen=True)
class CuentaOmitida:
    ig_user_id: str
    code: str
    motivo: str


@dataclass(frozen=True)
class ResultadoConexion:
    connection_id: uuid.UUID
    conectadas: list[CuentaConectada]
    omitidas: list[CuentaOmitida]


# ------------------------------------------------------------------ plan y configuración
def plan_incluye_instagram(plan_id: str | None) -> bool:
    return plan_incluye_canal(plan_id, CANAL)


async def exigir_plan(tenant_id: uuid.UUID) -> None:
    from core.billing.enforcement import subscription_overview

    resumen = await subscription_overview(tenant_id)
    if not plan_incluye_instagram(resumen.plan_id):
        raise InstagramAltaError(
            f"Tu plan {resumen.plan_name} no incluye Instagram. Mejora tu plan para conectar tus cuentas.",
            code="plan_channel_not_included",
            status=409,
        )


def config_id_de_instagram(settings: Any) -> str:
    """Login Configuration de Instagram. Propia: no se mezcla el juego de permisos de Instagram con el de
    Messenger en el mismo diálogo, ni cae a la de WhatsApp (ADR-0152, §5)."""
    return settings.meta_instagram_config_id


def faltan_para_instagram(settings: Any) -> list[str]:
    """Variables de entorno que faltan para conectar Instagram (vacío si está lista)."""
    return [
        nombre
        for nombre, valor in (
            ("META_APP_ID", settings.meta_app_id),
            ("META_APP_SECRET", settings.meta_app_secret),
            ("META_INSTAGRAM_CONFIG_ID", config_id_de_instagram(settings)),
        )
        if not valor
    ]


def _exigir_app(settings: Any) -> None:
    if faltan := faltan_para_instagram(settings):
        log.error("instagram_app_de_la_plataforma_sin_configurar", faltan=faltan)
        raise InstagramAltaError(
            "La app de Meta de la plataforma no está configurada para Instagram.",
            code="platform_app_not_configured",
            status=409,
        )


# ------------------------------------------------------------------ paso 1: el intento
async def iniciar_intento(tenant_id: uuid.UUID, admin_id: uuid.UUID) -> str:
    return await _iniciar_intento(tenant_id, admin_id, CANAL)


# ------------------------------------------------------------------ pasos de Graph
async def _listar_cuentas(
    user_token: str, app_secret: str, version: str, connection_id: uuid.UUID
) -> tuple[list[CuentaCandidata], int]:
    """`GET /me/accounts` con la cuenta de Instagram vinculada a cada Página, paginado. Devuelve las cuentas y
    cuántas Páginas no tienen Instagram vinculado. Trae el token de cada Página: se queda en memoria."""
    cuentas: list[CuentaCandidata] = []
    sin_instagram = 0
    despues: str | None = None
    for _ in range(MAX_PAGINAS_DE_GRAPH):
        consulta = {
            "fields": "id,name,access_token,tasks,instagram_business_account{id,username}",
            "limit": "100",
        }
        if despues:
            consulta["after"] = despues
        datos = await graph_fetch(
            "/me/accounts", token=user_token, app_secret=app_secret,
            query=consulta, version=version, connection_id=connection_id,
        )
        for item in (datos or {}).get("data", []):
            page_id = str(item.get("id") or "")
            if not page_id:
                continue
            ig = item.get("instagram_business_account") or {}
            ig_user_id = str(ig.get("id") or "")
            if not ig_user_id:
                sin_instagram += 1
                continue
            tasks = tuple(str(t) for t in (item.get("tasks") or []))
            elegible, motivo = evaluar_pagina(tasks)
            token = item.get("access_token")
            if elegible and not token:
                elegible, motivo = False, "Meta no devolvió el token de la Página vinculada: vuelve a conectar con Facebook."
            cuentas.append(
                CuentaCandidata(
                    ig_user_id=ig_user_id,
                    username=ig.get("username"),
                    page_id=page_id,
                    page_name=str(item.get("name") or ""),
                    tasks=tasks,
                    elegible=elegible,
                    motivo=motivo,
                    page_access_token=str(token) if elegible else None,
                )
            )
        paging = (datos or {}).get("paging") or {}
        despues = (paging.get("cursors") or {}).get("after")
        if not despues or not paging.get("next"):
            break
    return cuentas, sin_instagram


async def _suscribir_y_verificar(
    page_id: str, page_token: str, app_id: str, app_secret: str, version: str, connection_id: uuid.UUID
) -> tuple[bool, tuple[str, ...]]:
    """Suscribe la Página con el token de la PÁGINA y verifica con un GET. `POST subscribed_fields` REEMPLAZA la
    lista: si la Página ya tiene campos (p. ej. los de Messenger) se piden la unión, para no quitárselos."""
    kwargs = {"token": page_token, "app_secret": app_secret, "version": version, "connection_id": connection_id}
    actuales = await graph_fetch(f"/{page_id}/subscribed_apps", **kwargs)
    pedidos = IG_SUBSCRIBED_FIELDS | _campos_de_la_app((actuales or {}).get("data", []), app_id)
    await graph_fetch(
        f"/{page_id}/subscribed_apps", method="POST", query={"subscribed_fields": ",".join(sorted(pedidos))}, **kwargs
    )
    verificacion = await graph_fetch(f"/{page_id}/subscribed_apps", **kwargs)
    entradas = (verificacion or {}).get("data", [])
    campos = _campos_de_la_app(entradas, app_id)
    return CAMPO_OBLIGATORIO in campos, tuple(sorted(campos))


# ------------------------------------------------------------------ la cuenta de Meta
async def _cuenta_del_negocio(tenant_id: uuid.UUID, settings: Any) -> MetaConnection:
    """La cuenta viva de Instagram del negocio con la app de la plataforma; la crea en `draft` si no hay. Una
    cuenta agrupa todas las cuentas de Instagram del negocio."""
    async with get_session() as session:
        existente = (
            await session.execute(
                select(MetaConnection).where(
                    MetaConnection.tenant_id == tenant_id,
                    MetaConnection.channel == CANAL,
                    MetaConnection.environment == "production",
                    MetaConnection.meta_app_id == settings.meta_app_id,
                    MetaConnection.deleted_at.is_(None),
                )
            )
        ).scalars().first()
        if existente is not None:
            return existente
        cuenta = MetaConnection(
            tenant_id=tenant_id, channel=CANAL, environment="production", origin="embedded_signup",
            meta_app_id=settings.meta_app_id, meta_config_id=config_id_de_instagram(settings) or None,
            graph_api_version=settings.graph_api_version, status="draft",
        )
        session.add(cuenta)
        await session.flush()
        return cuenta


# ------------------------------------------------------------------ paso 2: descubrir
async def descubrir_cuentas(tenant_id: uuid.UUID, admin_id: uuid.UUID, code: str, intento: str) -> Descubrimiento:
    from core.gateway.meta_onboarding import _fetch_exchange_token

    settings = get_settings()
    _exigir_app(settings)
    await exigir_plan(tenant_id)
    await _consumir_intento(tenant_id, admin_id, intento, CANAL)
    if not code or len(code) > 2048:
        raise InstagramAltaError("Falta el código de Meta.", code="missing_code")

    cuenta = await _cuenta_del_negocio(tenant_id, settings)
    params = {"client_id": settings.meta_app_id, "client_secret": settings.meta_app_secret, "code": code}
    try:
        user_token, _ = await _fetch_exchange_token(
            settings.graph_api_version, params, settings, redirect_path="/admin/conexiones/wizard/instagram"
        )
    except GraphError as exc:
        log.warning("instagram_canje_rechazado", tenant_id=str(tenant_id), code=exc.code)
        raise InstagramAltaError(
            "Meta no aceptó el código del popup (vence a los 30 segundos): vuelve a conectar con Facebook.",
            code="meta_error",
        ) from exc
    finally:
        del params

    # El token del usuario solo sirve para el paso 3: cifrado, de una hora, revocado al terminar.
    await meta_token_store.put_token(
        cuenta.id, USER_TOKEN_KIND, USER_TOKEN_SUBJECT, user_token,
        expires_at=utcnow() + USER_TOKEN_TTL, tenant_id=tenant_id,
    )
    try:
        cuentas, sin_instagram = await _listar_cuentas(
            user_token, settings.meta_app_secret, settings.graph_api_version, cuenta.id
        )
    except GraphError as exc:
        raise InstagramAltaError(f"Meta no dejó listar tus Páginas: {exc.message}", code="meta_error") from exc
    async with get_session() as session:
        ya = set(
            (
                await session.execute(
                    select(IgAccount.ig_user_id).where(
                        IgAccount.connection_id == cuenta.id, IgAccount.tenant_id == tenant_id
                    )
                )
            ).scalars().all()
        )
    return Descubrimiento(
        connection_id=cuenta.id,
        cuentas=[replace(c, page_access_token=None) for c in cuentas],
        ya_conectadas=frozenset(ya),
        paginas_sin_instagram=sin_instagram,
    )


# ------------------------------------------------------------------ paso 3: conectar
async def conectar_cuentas(
    tenant_id: uuid.UUID, admin_id: uuid.UUID, connection_id: uuid.UUID, ig_user_ids: list[str]
) -> ResultadoConexion:
    settings = get_settings()
    _exigir_app(settings)
    await exigir_plan(tenant_id)
    elegidas = list(dict.fromkeys(i.strip() for i in ig_user_ids if i and i.strip()))
    if not elegidas:
        raise InstagramAltaError("Elige al menos una cuenta de Instagram.", code="missing_accounts")

    async with get_session() as session:
        cuenta = await session.get(MetaConnection, connection_id)
    if cuenta is None or cuenta.tenant_id != tenant_id or cuenta.channel != CANAL or cuenta.deleted_at is not None:
        raise InstagramAltaError("No encontré esa conexión de Instagram.", code="not_found", status=404)
    user_token = await meta_token_store.get_token(
        connection_id, USER_TOKEN_KIND, USER_TOKEN_SUBJECT, tenant_id=tenant_id
    )
    if not user_token:
        raise InstagramAltaError(
            "La sesión de Facebook venció: vuelve a conectar con Facebook.", code="user_token_expired"
        )

    version = cuenta.graph_api_version or settings.graph_api_version
    try:
        listadas, _ = await _listar_cuentas(user_token, settings.meta_app_secret, version, connection_id)
    except GraphError as exc:
        raise InstagramAltaError(f"Meta no dejó listar tus Páginas: {exc.message}", code="meta_error") from exc
    candidatas = {c.ig_user_id: c for c in listadas}

    conectadas: list[CuentaConectada] = []
    omitidas: list[CuentaOmitida] = []
    for ig_user_id in elegidas:
        candidata = candidatas.get(ig_user_id)
        if candidata is None:
            # Una cuenta que el usuario NO administra no se conecta aunque conozca su ID.
            omitidas.append(
                CuentaOmitida(ig_user_id, "account_not_accessible", "Esta cuenta no está entre las que administras.")
            )
            continue
        if not candidata.elegible or not candidata.page_access_token:
            omitidas.append(CuentaOmitida(ig_user_id, "insufficient_role", candidata.motivo or "Te falta un rol."))
            continue
        try:
            canonica_id = await _guardar_cuenta(tenant_id, admin_id, connection_id, candidata)
        except MetaAssetError as exc:
            omitidas.append(CuentaOmitida(ig_user_id, exc.code, exc.message))
            continue
        try:
            suscrita, campos = await _suscribir_y_verificar(
                candidata.page_id, candidata.page_access_token, settings.meta_app_id,
                settings.meta_app_secret, version, connection_id,
            )
        except GraphError as exc:
            log.warning("instagram_suscripcion_fallo", ig_user_id=ig_user_id, code=exc.code)
            conectadas.append(CuentaConectada(ig_user_id, canonica_id, False, ()))
            continue
        await _registrar_campos(tenant_id, connection_id, ig_user_id, campos)
        conectadas.append(CuentaConectada(ig_user_id, canonica_id, suscrita, campos))

    await _cerrar(tenant_id, admin_id, connection_id, conectadas)
    return ResultadoConexion(connection_id, conectadas, omitidas)


async def _guardar_cuenta(
    tenant_id: uuid.UUID, admin_id: uuid.UUID, connection_id: uuid.UUID, cuenta_ig: CuentaCandidata
) -> uuid.UUID:
    """Reclama la cuenta y escribe todo lo suyo en UNA transacción; el token de la Página vinculada se guarda DESPUÉS
    de que confirme. Así un alta que falla no toca el token de una cuenta que ya funcionaba ni deja uno vivo en una
    conexión que se eliminó mientras tanto."""
    assert cuenta_ig.page_access_token is not None
    async with get_session() as session:
        await claim_messaging_assets(session, tenant_id, ig_user_id=cuenta_ig.ig_user_id)
        cuenta = await session.get(MetaConnection, connection_id)
        if cuenta is None or cuenta.deleted_at is not None:
            raise MetaAssetError(
                "La conexión se eliminó mientras se completaba el alta: empieza de nuevo.",
                code="connection_deleted",
            )
        fila = (
            await session.execute(
                select(IgAccount).where(
                    IgAccount.tenant_id == tenant_id, IgAccount.connection_id == connection_id,
                    IgAccount.ig_user_id == cuenta_ig.ig_user_id,
                )
            )
        ).scalar_one_or_none()
        if fila is None:
            session.add(
                IgAccount(
                    tenant_id=tenant_id, connection_id=connection_id, ig_user_id=cuenta_ig.ig_user_id,
                    username=cuenta_ig.username, linked_page_id=cuenta_ig.page_id,
                    login_route=LOGIN_ROUTE_FACEBOOK,
                )
            )
        else:
            fila.username = cuenta_ig.username
            fila.linked_page_id = cuenta_ig.page_id
            fila.login_route = LOGIN_ROUTE_FACEBOOK
        # Sale de `draft` en la misma transacción que escribe el primer activo.
        if cuenta.status == "draft":
            cuenta.status = "pending"
            cuenta.connected_by = admin_id
        await session.flush()
        nombre = f"@{cuenta_ig.username}" if cuenta_ig.username else cuenta_ig.page_name
        canonica_id, resultado = await asegurar_conexion_de_cuenta_ig(
            session, tenant_id, cuenta_ig.ig_user_id, connection_id, nombre=nombre
        )
        if resultado == "choque_manual":
            # Ya hay una conexión manual con este identificador y credenciales propias: respondería por ella, no
            # por la Página. Se deshace todo y se explica.
            raise MetaAssetError(MENSAJE_CHOQUE_MANUAL, code="manual_connection_conflict")
        from core.agents.instancias import instancias_de_conexion_nueva

        await instancias_de_conexion_nueva(session, tenant_id, canonica_id)
    await meta_token_store.put_token(
        connection_id, PAGE_TOKEN_KIND, cuenta_ig.page_id, cuenta_ig.page_access_token, tenant_id=tenant_id
    )
    return canonica_id


async def _registrar_campos(
    tenant_id: uuid.UUID, connection_id: uuid.UUID, ig_user_id: str, campos: tuple[str, ...]
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


async def _cerrar(
    tenant_id: uuid.UUID, admin_id: uuid.UUID, connection_id: uuid.UUID, conectadas: list[CuentaConectada]
) -> None:
    """La cuenta queda `connected` si alguna cuenta quedó suscrita; el token del usuario ya no hace falta."""
    async with get_session() as session:
        cuenta = await session.get(MetaConnection, connection_id)
        if cuenta is not None and cuenta.tenant_id == tenant_id and any(c.suscrita for c in conectadas):
            cuenta.status = "connected"
            cuenta.connected_at = utcnow()
            cuenta.connected_by = admin_id
    await meta_token_store.revoke_token(connection_id, USER_TOKEN_KIND, USER_TOKEN_SUBJECT, tenant_id=tenant_id)
