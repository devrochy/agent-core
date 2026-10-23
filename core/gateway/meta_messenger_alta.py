"""Alta de Messenger: Facebook Login, descubrimiento de Páginas y conexión de las elegidas (ADR-0152).

Tres pasos, cada uno una llamada de la API, porque el negocio ELIGE qué Páginas conecta (cada una es una
conexión canónica y entra en el plan):

1. `iniciar_intento`: un valor aleatorio de un solo uso, atado al negocio y al admin que abrió el popup. El
   panel lo manda de vuelta con el `code`; ata el canje a esa sesión (una pestaña vieja, otro admin o un
   pedido armado en otro lado no canjean).
2. `descubrir_paginas`: consume el intento, canjea el `code` por el token del usuario y lista las Páginas que
   administra (`GET /me/accounts`). El token del usuario se guarda cifrado y vence en una hora: solo sirve para
   el paso 3 y se revoca al terminar. A la API vuelven los datos de cada Página, NUNCA sus tokens.
3. `conectar_paginas`: para cada Página elegida, dentro de UNA transacción, reclama la Página (un solo dueño,
   ADR-0095/0152), guarda su fila y su conexión canónica con sus instancias de agentes; luego guarda el token de
   la Página, suscribe el webhook y VERIFICA con un GET lo que Meta confirma de verdad.

La cuenta de Meta (`meta_connections`, canal `messenger`) nace en `draft` y sale de ahí en la misma transacción
que escribe la primera Página: una conexión en `draft` nunca tiene activos, así que no cuenta en el ruteo del
webhook (migración 0104).
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import uuid
from dataclasses import dataclass, field, replace
from datetime import timedelta
from typing import Any

from sqlalchemy import select

from core.config import get_settings
from core.db.models import FbPage, MetaConnection, utcnow
from core.db.session import get_session
from core.gateway import meta_token_store
from core.gateway.conexion_canonica import asegurar_conexion_de_pagina
from core.gateway.meta_asset_claims import MetaAssetError, claim_messaging_assets
from core.gateway.meta_errors import GraphError
from core.gateway.meta_graph import graph_fetch
from core.gateway.meta_messenger import PAGE_TOKEN_KIND
from core.shared.exceptions import AgentCoreError
from core.shared.logger import get_logger

log = get_logger(__name__)

# El intento de un popup vale lo que dura uno razonable (igual que el de WhatsApp).
INTENTO_TTL_S = 15 * 60
USER_TOKEN_KIND = "user_access"
USER_TOKEN_SUBJECT = "me"
USER_TOKEN_TTL = timedelta(hours=1)
MAX_PAGINAS_DE_GRAPH = 5  # 5 × 100: más Páginas que cualquier negocio de esta plataforma

# Campos que Messenger pide al suscribir una Página. El parámetro se llama `subscribed_fields`, NUNCA
# `fields` (Meta acepta el nombre equivocado sin suscribir nada).
PAGE_SUBSCRIBED_FIELDS = (
    "messages,messaging_postbacks,messaging_optins,message_deliveries,"
    "message_reads,messaging_referrals,message_echoes,messaging_handovers"
)

# La referencia de `subscribed_apps` pide CREATE_CONTENT, MANAGE o MODERATE; la de la Messenger Platform pide
# MESSAGING (más MODERATE para comentarios). Las dos páginas de Meta discrepan (2026-10-23): se exigen las dos
# cosas. Un administrador de la Página las tiene todas.
TAREAS_DE_SUSCRIPCION = frozenset({"MANAGE", "MODERATE", "CREATE_CONTENT"})
TAREA_DE_MENSAJERIA = "MESSAGING"


class MessengerAltaError(AgentCoreError):
    """El alta no puede seguir. `code` es estable para la API; `status` el HTTP sugerido."""

    def __init__(self, message: str, *, code: str, status: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.status = status


@dataclass(frozen=True)
class PaginaCandidata:
    page_id: str
    name: str
    category: str | None
    tasks: tuple[str, ...]
    elegible: bool
    motivo: str | None = None
    # Solo en memoria durante `conectar_paginas`: jamás sale en una respuesta ni en un log.
    access_token: str | None = field(default=None, repr=False)


@dataclass(frozen=True)
class Descubrimiento:
    connection_id: uuid.UUID
    paginas: list[PaginaCandidata]
    ya_conectadas: frozenset[str]


@dataclass(frozen=True)
class PaginaConectada:
    page_id: str
    channel_connection_id: uuid.UUID
    suscrita: bool
    campos: tuple[str, ...]


@dataclass(frozen=True)
class PaginaOmitida:
    page_id: str
    code: str
    motivo: str


@dataclass(frozen=True)
class ResultadoConexion:
    connection_id: uuid.UUID
    conectadas: list[PaginaConectada]
    omitidas: list[PaginaOmitida]


# ------------------------------------------------------------------ plan
def plan_incluye_messenger(plan_id: str | None) -> bool:
    """¿El plan incluye el canal Messenger? (`features.canales`, decisión del dueño 2026-10-23: todos los
    planes de pago)."""
    from core.billing.plans import get_plan

    plan = get_plan(plan_id) if plan_id else None
    return bool(plan and "messenger" in (plan.features.get("canales") or []))


async def exigir_plan(tenant_id: uuid.UUID) -> None:
    from core.billing.enforcement import subscription_overview

    resumen = await subscription_overview(tenant_id)
    if not plan_incluye_messenger(resumen.plan_id):
        raise MessengerAltaError(
            f"Tu plan {resumen.plan_name} no incluye Messenger. Mejora tu plan para conectar tus Páginas.",
            code="plan_channel_not_included",
            status=409,
        )


# ------------------------------------------------------------------ paso 1: el intento
def _clave_intento(tenant_id: uuid.UUID, intento: str) -> str:
    return f"messenger_attempt:{tenant_id}:{hashlib.sha256(intento.encode()).hexdigest()}"


async def iniciar_intento(tenant_id: uuid.UUID, admin_id: uuid.UUID) -> str:
    """Abre un intento de popup. Solo se guarda su hash (con el admin que lo pidió), vence a los 15 min y es de
    un solo uso."""
    from core.memory.working import get_redis

    intento = secrets.token_urlsafe(24)
    await get_redis().set(_clave_intento(tenant_id, intento), str(admin_id), ex=INTENTO_TTL_S)
    return intento


async def _consumir_intento(tenant_id: uuid.UUID, admin_id: uuid.UUID, intento: str) -> None:
    from core.memory.working import get_redis

    invalido = MessengerAltaError(
        "El intento venció o no es de esta sesión: abre el popup de Facebook otra vez.",
        code="invalid_attempt",
        status=400,
    )
    if not intento:
        raise invalido
    guardado = await get_redis().getdel(_clave_intento(tenant_id, intento))  # un solo uso, aunque falle lo que sigue
    if guardado is None:
        raise invalido
    guardado = guardado.decode() if isinstance(guardado, bytes) else str(guardado)
    if not hmac.compare_digest(guardado, str(admin_id)):
        raise invalido


# ------------------------------------------------------------------ pasos de Graph
def evaluar_pagina(tasks: list[str] | tuple[str, ...]) -> tuple[bool, str | None]:
    """¿Puede conectarse la Página con estos roles del usuario? Devuelve `(elegible, motivo)`."""
    roles = set(tasks)
    if TAREA_DE_MENSAJERIA in roles and roles & TAREAS_DE_SUSCRIPCION:
        return True, None
    return False, (
        "Te falta un rol sobre esta Página (mensajería y administración). Pídele a un administrador de la "
        "Página que te lo asigne en Meta Business Suite."
    )


async def _listar_paginas(
    user_token: str, app_secret: str, version: str, connection_id: uuid.UUID
) -> list[PaginaCandidata]:
    """`GET /me/accounts`, paginado. Trae el token de cada Página: se queda en memoria."""
    candidatas: list[PaginaCandidata] = []
    despues: str | None = None
    for _ in range(MAX_PAGINAS_DE_GRAPH):
        consulta = {"fields": "id,name,category,access_token,tasks", "limit": "100"}
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
            tasks = tuple(str(t) for t in (item.get("tasks") or []))
            elegible, motivo = evaluar_pagina(tasks)
            token = item.get("access_token")
            if elegible and not token:
                elegible, motivo = False, "Meta no devolvió el token de esta Página: vuelve a conectar con Facebook."
            candidatas.append(
                PaginaCandidata(
                    page_id=page_id,
                    name=str(item.get("name") or ""),
                    category=item.get("category"),
                    tasks=tasks,
                    elegible=elegible,
                    motivo=motivo,
                    access_token=str(token) if elegible else None,
                )
            )
        despues = (((datos or {}).get("paging") or {}).get("cursors") or {}).get("after")
        if not despues or not ((datos or {}).get("paging") or {}).get("next"):
            break
    return candidatas


async def _suscribir_y_verificar(
    page_id: str, page_token: str, app_secret: str, version: str, connection_id: uuid.UUID
) -> tuple[bool, tuple[str, ...]]:
    """`POST /{page}/subscribed_apps` con el token de la PÁGINA y, enseguida, un GET: lo que se guarda son los
    campos que Meta confirma, no los pedidos."""
    await graph_fetch(
        f"/{page_id}/subscribed_apps", method="POST", token=page_token, app_secret=app_secret,
        query={"subscribed_fields": PAGE_SUBSCRIBED_FIELDS}, version=version, connection_id=connection_id,
    )
    verificacion = await graph_fetch(
        f"/{page_id}/subscribed_apps", token=page_token, app_secret=app_secret,
        version=version, connection_id=connection_id,
    )
    entradas = (verificacion or {}).get("data", [])
    campos: set[str] = set()
    for entrada in entradas:
        campos.update(str(c) for c in entrada.get("subscribed_fields", []))
    return bool(entradas), tuple(sorted(campos))


# ------------------------------------------------------------------ la cuenta de Meta
async def _cuenta_del_negocio(tenant_id: uuid.UUID, settings: Any) -> MetaConnection:
    """La cuenta viva de Messenger del negocio con la app de la plataforma; la crea en `draft` si no hay. Una
    cuenta agrupa todas las Páginas del negocio (índice único parcial por negocio, canal, entorno y app)."""
    async with get_session() as session:
        existente = (
            await session.execute(
                select(MetaConnection).where(
                    MetaConnection.tenant_id == tenant_id,
                    MetaConnection.channel == "messenger",
                    MetaConnection.environment == "production",
                    MetaConnection.meta_app_id == settings.meta_app_id,
                    MetaConnection.deleted_at.is_(None),
                )
            )
        ).scalars().first()
        if existente is not None:
            return existente
        cuenta = MetaConnection(
            tenant_id=tenant_id, channel="messenger", environment="production", origin="embedded_signup",
            meta_app_id=settings.meta_app_id, meta_config_id=config_id_de_messenger(settings) or None,
            graph_api_version=settings.graph_api_version, status="draft",
        )
        session.add(cuenta)
        await session.flush()
        return cuenta


def config_id_de_messenger(settings: Any) -> str:
    """Login Configuration de Messenger; sin una propia, la de la plataforma."""
    return settings.meta_messenger_config_id or settings.meta_config_id


def faltan_para_messenger(settings: Any) -> list[str]:
    """Variables de entorno que faltan para conectar Messenger (vacío si está lista)."""
    return [
        nombre
        for nombre, valor in (
            ("META_APP_ID", settings.meta_app_id),
            ("META_APP_SECRET", settings.meta_app_secret),
            ("META_MESSENGER_CONFIG_ID", config_id_de_messenger(settings)),
        )
        if not valor
    ]


def _exigir_app(settings: Any) -> None:
    if faltan := faltan_para_messenger(settings):
        log.error("messenger_app_de_la_plataforma_sin_configurar", faltan=faltan)
        raise MessengerAltaError(
            "La app de Meta de la plataforma no está configurada para Messenger.",
            code="platform_app_not_configured",
            status=409,
        )


# ------------------------------------------------------------------ paso 2: descubrir
async def descubrir_paginas(tenant_id: uuid.UUID, admin_id: uuid.UUID, code: str, intento: str) -> Descubrimiento:
    from core.gateway.meta_onboarding import _fetch_exchange_token

    settings = get_settings()
    _exigir_app(settings)
    await exigir_plan(tenant_id)
    await _consumir_intento(tenant_id, admin_id, intento)
    if not code or len(code) > 2048:
        raise MessengerAltaError("Falta el código de Meta.", code="missing_code")

    cuenta = await _cuenta_del_negocio(tenant_id, settings)
    params = {"client_id": settings.meta_app_id, "client_secret": settings.meta_app_secret, "code": code}
    try:
        user_token, _ = await _fetch_exchange_token(
            settings.graph_api_version, params, settings, redirect_path="/admin/conexiones/wizard/messenger"
        )
    except GraphError as exc:
        log.warning("messenger_canje_rechazado", tenant_id=str(tenant_id), code=exc.code)
        raise MessengerAltaError(
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
        paginas = await _listar_paginas(user_token, settings.meta_app_secret, settings.graph_api_version, cuenta.id)
    except GraphError as exc:
        raise MessengerAltaError(
            f"Meta no dejó listar tus Páginas: {exc.message}", code="meta_error"
        ) from exc
    async with get_session() as session:
        ya = set(
            (
                await session.execute(
                    select(FbPage.page_id).where(FbPage.connection_id == cuenta.id, FbPage.tenant_id == tenant_id)
                )
            ).scalars().all()
        )
    return Descubrimiento(
        connection_id=cuenta.id,
        paginas=[replace(p, access_token=None) for p in paginas],
        ya_conectadas=frozenset(ya),
    )


# ------------------------------------------------------------------ paso 3: conectar
async def conectar_paginas(
    tenant_id: uuid.UUID, admin_id: uuid.UUID, connection_id: uuid.UUID, page_ids: list[str]
) -> ResultadoConexion:
    settings = get_settings()
    _exigir_app(settings)
    await exigir_plan(tenant_id)
    elegidas = list(dict.fromkeys(p.strip() for p in page_ids if p and p.strip()))
    if not elegidas:
        raise MessengerAltaError("Elige al menos una Página.", code="missing_pages")

    async with get_session() as session:
        cuenta = await session.get(MetaConnection, connection_id)
    if (
        cuenta is None
        or cuenta.tenant_id != tenant_id
        or cuenta.channel != "messenger"
        or cuenta.deleted_at is not None
    ):
        raise MessengerAltaError("No encontré esa conexión de Messenger.", code="not_found", status=404)
    user_token = await meta_token_store.get_token(
        connection_id, USER_TOKEN_KIND, USER_TOKEN_SUBJECT, tenant_id=tenant_id
    )
    if not user_token:
        raise MessengerAltaError(
            "La sesión de Facebook venció: vuelve a conectar con Facebook.", code="user_token_expired"
        )

    version = cuenta.graph_api_version or settings.graph_api_version
    try:
        candidatas = {
            p.page_id: p
            for p in await _listar_paginas(user_token, settings.meta_app_secret, version, connection_id)
        }
    except GraphError as exc:
        raise MessengerAltaError(f"Meta no dejó listar tus Páginas: {exc.message}", code="meta_error") from exc

    conectadas: list[PaginaConectada] = []
    omitidas: list[PaginaOmitida] = []
    for page_id in elegidas:
        candidata = candidatas.get(page_id)
        if candidata is None:
            # Una Página que el usuario NO administra no se conecta aunque conozca su ID.
            omitidas.append(PaginaOmitida(page_id, "page_not_accessible", "Esta Página no está entre las que administras."))
            continue
        if not candidata.elegible or not candidata.access_token:
            omitidas.append(PaginaOmitida(page_id, "insufficient_role", candidata.motivo or "Te falta un rol."))
            continue
        try:
            canonica_id = await _guardar_pagina(tenant_id, admin_id, connection_id, candidata)
        except MetaAssetError as exc:
            omitidas.append(PaginaOmitida(page_id, exc.code, exc.message))
            continue
        try:
            suscrita, campos = await _suscribir_y_verificar(
                page_id, candidata.access_token, settings.meta_app_secret, version, connection_id
            )
        except GraphError as exc:
            log.warning("messenger_suscripcion_fallo", page_id=page_id, code=exc.code)
            conectadas.append(PaginaConectada(page_id, canonica_id, False, ()))
            continue
        await _registrar_campos(tenant_id, connection_id, page_id, campos)
        conectadas.append(PaginaConectada(page_id, canonica_id, suscrita, campos))

    await _cerrar(tenant_id, admin_id, connection_id, conectadas)
    return ResultadoConexion(connection_id, conectadas, omitidas)


async def _guardar_pagina(
    tenant_id: uuid.UUID, admin_id: uuid.UUID, connection_id: uuid.UUID, pagina: PaginaCandidata
) -> uuid.UUID:
    """Reclama la Página y escribe todo lo suyo en UNA transacción. El token de la Página se guarda antes (su
    fila no depende de las demás) y se revoca si el reclamo falla."""
    assert pagina.access_token is not None
    await meta_token_store.put_token(
        connection_id, PAGE_TOKEN_KIND, pagina.page_id, pagina.access_token, tenant_id=tenant_id
    )
    try:
        async with get_session() as session:
            await claim_messaging_assets(session, tenant_id, page_id=pagina.page_id)
            cuenta = await session.get(MetaConnection, connection_id)
            if cuenta is None or cuenta.deleted_at is not None:
                raise MetaAssetError(
                    "La conexión se eliminó mientras se completaba el alta: empieza de nuevo.",
                    code="connection_deleted",
                )
            fila = (
                await session.execute(
                    select(FbPage).where(
                        FbPage.tenant_id == tenant_id, FbPage.connection_id == connection_id,
                        FbPage.page_id == pagina.page_id,
                    )
                )
            ).scalar_one_or_none()
            if fila is None:
                session.add(
                    FbPage(
                        tenant_id=tenant_id, connection_id=connection_id, page_id=pagina.page_id,
                        name=pagina.name, category=pagina.category,
                    )
                )
            else:
                fila.name, fila.category = pagina.name, pagina.category
            # Sale de `draft` en la misma transacción que escribe el primer activo.
            if cuenta.status == "draft":
                cuenta.status = "pending"
                cuenta.connected_by = admin_id
            await session.flush()
            canonica_id, _ = await asegurar_conexion_de_pagina(
                session, tenant_id, pagina.page_id, connection_id, nombre=pagina.name
            )
            from core.agents.instancias import instancias_de_conexion_nueva

            await instancias_de_conexion_nueva(session, tenant_id, canonica_id)
    except Exception:
        await meta_token_store.revoke_token(connection_id, PAGE_TOKEN_KIND, pagina.page_id, tenant_id=tenant_id)
        raise
    return canonica_id


async def _registrar_campos(
    tenant_id: uuid.UUID, connection_id: uuid.UUID, page_id: str, campos: tuple[str, ...]
) -> None:
    async with get_session() as session:
        fila = (
            await session.execute(
                select(FbPage).where(
                    FbPage.tenant_id == tenant_id, FbPage.connection_id == connection_id, FbPage.page_id == page_id
                )
            )
        ).scalar_one_or_none()
        if fila is not None:
            fila.subscribed_fields = list(campos)


async def _cerrar(
    tenant_id: uuid.UUID, admin_id: uuid.UUID, connection_id: uuid.UUID, conectadas: list[PaginaConectada]
) -> None:
    """La cuenta queda `connected` si alguna Página quedó suscrita; el token del usuario ya no hace falta."""
    async with get_session() as session:
        cuenta = await session.get(MetaConnection, connection_id)
        if cuenta is not None and cuenta.tenant_id == tenant_id and any(p.suscrita for p in conectadas):
            cuenta.status = "connected"
            cuenta.connected_at = utcnow()
            cuenta.connected_by = admin_id
    await meta_token_store.revoke_token(connection_id, USER_TOKEN_KIND, USER_TOKEN_SUBJECT, tenant_id=tenant_id)
