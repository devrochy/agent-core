"""Pasar a producción una conexión BYOK de prueba (HU-CONN-06, ADR-0127).

La promoción crea (o reusa) la conexión de **producción** de la misma app de Meta con el alta
BYOK de siempre (`save_byok_connection(environment="production")`), y al terminar da de baja
la de prueba (`soft_delete_connection`, ADR-0122). Lo que el negocio ve pasa de prueba a
producción; las conversaciones de los testers quedan con la conexión de prueba, en pausa, y
nunca se responden desde el número real (ADR-0122 §3). El panel guía cuatro pasos:

1. **Número real** (`cargar_numero_real`): WABA, número y, opcional, el token permanente.
   El App ID y el App Secret son los guardados de la conexión de prueba: no salen del
   servidor. El alta rechaza el número de prueba de Meta y deja la conexión `pending` con
   `MOTIVO_NUMERO_SIN_REGISTRAR` si falta el PIN. Mientras tanto la de prueba sigue igual.
2. **Registro** con el PIN (`meta_numbers_service.register_number`, la ruta de "Números").
3. **Plantillas**: al menos una aprobada en la WABA del número real.
4. **Verificar y terminar** (`terminar_promocion`): `checks_de_produccion` sobre la conexión
   de producción: el checklist por conexión de HU-CONN-07 con la app del negocio (nunca la de la
   plataforma), más lo propio de la promoción. Si pasan, se da de baja la de prueba. No cambia
   el estado de la de producción (ADR-0102: mejorar lo decide el diagnóstico o el registro).

El estado sale de lo guardado: la "pareja" de producción es la conexión viva de WhatsApp del
negocio con la misma app y `environment = production` (el índice único
`uq_meta_connections_tenant_app` garantiza que hay a lo sumo una). `estado_promocion` y
`verificar_promocion` también aceptan una conexión BYOK de producción: es su propia pareja.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select

from core.db.models import MessageTemplate, MetaConnection, PhoneNumber, WabaAccount
from core.db.session import get_session
from core.gateway.meta_byok import TEST_NUMBER_IN_PRODUCTION_MESSAGE, ByokError, numero_registrado
from core.gateway.meta_connection_state import get_live_meta_connection
from core.gateway.meta_errors import GraphError
from core.gateway.meta_graph import graph_fetch
from core.gateway.meta_preflight import PreflightCheck
from core.shared.exceptions import AgentCoreError
from core.shared.logger import get_logger

log = get_logger(__name__)

ACCOUNT_MODE_LIVE = "LIVE"
# Tope de los chequeos de "Verificar"/"Terminar", que corren en el request (varias llamadas a
# Graph y el GET al override de webhook de cada número).
TOPE_CHEQUEOS_S = 60
MOTIVO_REEMPLAZADA = "Reemplazada por la conexión de producción de la misma app"


class PromocionError(AgentCoreError):
    """No se puede avanzar la promoción. `code` es estable para la API; `message`, legible
    para el panel (nunca lleva tokens ni secretos)."""

    def __init__(self, message: str, *, code: str, status: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.status = status


@dataclass(frozen=True, slots=True)
class PasoPromocion:
    key: str
    label: str
    done: bool
    detail: str = ""


def _paso_dict(paso: PasoPromocion) -> dict[str, Any]:
    return {"key": paso.key, "label": paso.label, "done": paso.done, "detail": paso.detail}


def _no_es_byok(connection: MetaConnection) -> PromocionError | None:
    from core.gateway.meta_credentials import uses_platform_app

    if connection.channel != "whatsapp" or connection.origin != "byok":
        return PromocionError(
            "Solo una conexión de WhatsApp con tu propia app de Meta (BYOK) se pasa a producción desde aquí",
            code="not_byok",
        )
    if uses_platform_app(connection):
        # Una fila vieja con el App ID de la plataforma (ADR-0126 §7): la app es de todos.
        return PromocionError(
            "Esta conexión usa la app de Meta de la plataforma: conecta tu propia app con el asistente BYOK",
            code="platform_app",
        )
    return None


def _no_promovible(connection: MetaConnection) -> PromocionError | None:
    if (error := _no_es_byok(connection)) is not None:
        return error
    if connection.environment == "production":
        return PromocionError("La conexión ya está en producción", code="already_production", status=409)
    if connection.status in ("revoked", "draft"):
        return PromocionError(
            "La conexión está desactivada o no terminó el alta: actívala o complétala primero",
            code="invalid_state",
        )
    return None


async def _conexion(session: Any, tenant_id: uuid.UUID, connection_id: uuid.UUID) -> MetaConnection:
    connection = await get_live_meta_connection(session, connection_id, tenant_id)
    if connection is None:
        raise PromocionError("Conexión no encontrada", code="not_found", status=404)
    return connection


async def _pareja_de_produccion(session: Any, connection: MetaConnection) -> MetaConnection | None:
    """La conexión viva de producción de la misma app (ella misma si ya es de producción)."""
    if connection.environment == "production":
        return connection
    return (
        await session.execute(
            select(MetaConnection).where(
                MetaConnection.tenant_id == connection.tenant_id,
                MetaConnection.channel == connection.channel,
                MetaConnection.environment == "production",
                MetaConnection.meta_app_id == connection.meta_app_id,
                MetaConnection.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()


async def _numeros_vivos(session: Any, connection: MetaConnection) -> list[PhoneNumber]:
    return list(
        (
            await session.execute(
                select(PhoneNumber)
                .where(
                    PhoneNumber.tenant_id == connection.tenant_id,
                    PhoneNumber.connection_id == connection.id,
                    PhoneNumber.deleted_at.is_(None),
                )
                .order_by(PhoneNumber.created_at)
            )
        ).scalars()
    )


async def _plantillas_aprobadas(session: Any, tenant_id: uuid.UUID, waba_account_id: uuid.UUID | None) -> int:
    if waba_account_id is None:
        return 0
    return (
        await session.execute(
            select(func.count()).select_from(MessageTemplate).where(
                MessageTemplate.tenant_id == tenant_id,
                MessageTemplate.waba_account_id == waba_account_id,
                MessageTemplate.status == "APPROVED",
            )
        )
    ).scalar_one()


async def estado_promocion(tenant_id: uuid.UUID, connection_id: uuid.UUID) -> dict[str, Any]:
    """Dónde va la promoción, con lo que hay guardado (sin llamar a Meta)."""
    async with get_session() as session:
        connection = await _conexion(session, tenant_id, connection_id)
        error = _no_promovible(connection)
        bloqueo = None if error is None or error.code == "already_production" else error.message
        pareja = await _pareja_de_produccion(session, connection)
        numeros = await _numeros_vivos(session, pareja) if pareja is not None else []
        numero = numeros[0] if numeros else None
        aprobadas = await _plantillas_aprobadas(session, tenant_id, numero.waba_account_id if numero else None)

    promovida = connection.environment == "production"
    pasos = [
        PasoPromocion(
            "real_number",
            "Número real",
            numero is not None,
            (numero.display_phone_number or numero.phone_number_id) if numero else "Carga el número de producción y su WABA",
        ),
        PasoPromocion(
            "number_registered",
            "Registro con PIN",
            bool(numero and numero.registered),
            "" if numero is None else ("Registrado en la Cloud API" if numero.registered else "Falta registrarlo con su PIN"),
        ),
        PasoPromocion(
            "templates",
            "Plantillas aprobadas",
            aprobadas > 0,
            f"{aprobadas} aprobada{'s' if aprobadas != 1 else ''}" if numero else "",
        ),
        PasoPromocion("verify", "Verificar y pasar a producción", promovida),
    ]
    return {
        "connection_id": connection.id,
        "environment": connection.environment,
        "status": connection.status,
        "status_reason": connection.status_reason,
        "meta_app_id": connection.meta_app_id,
        "production_connection_id": pareja.id if pareja is not None else None,
        "production_status": pareja.status if pareja is not None else None,
        "production_status_reason": pareja.status_reason if pareja is not None else None,
        "phone_number_id": numero.phone_number_id if numero else None,
        "blocked_reason": bloqueo,
        "steps": [_paso_dict(paso) for paso in pasos],
    }


async def cargar_numero_real(
    tenant_id: uuid.UUID,
    admin_id: uuid.UUID | None,
    connection_id: uuid.UUID,
    *,
    waba_id: str,
    phone_number_id: str,
    access_token: str | None = None,
) -> MetaConnection:
    """Paso 1: crea (o actualiza) la conexión de producción de la misma app con el alta BYOK.

    El App Secret y, si no viene uno nuevo, el token son los guardados de la conexión de
    prueba; el `verify_token` también, para que la suscripción de la app no cambie. La de
    prueba no se toca. Reintentable: con la misma app y entorno, el alta reusa la fila."""
    from core.gateway.meta_byok import save_byok_connection
    from core.gateway.meta_credentials import (
        access_token_for,
        stored_app_secret,
        stored_verify_token,
    )

    waba_id, phone_number_id = waba_id.strip(), phone_number_id.strip()
    access_token = (access_token or "").strip() or None
    if not waba_id or not phone_number_id:
        raise PromocionError("Faltan el WABA ID o el Phone Number ID del número real", code="missing_fields")

    async with get_session() as session:
        connection = await _conexion(session, tenant_id, connection_id)
        if (error := _no_promovible(connection)) is not None:
            raise error
        pareja = await _pareja_de_produccion(session, connection)
        otros = [n for n in await _numeros_vivos(session, pareja) if n.phone_number_id != phone_number_id] if pareja else []
    if otros:
        # Ya hay una conexión de producción de esta app atendiendo otro número: el alta la
        # reusaría y le sumaría este (dos números vivos: el sender responde por cualquiera).
        # Solo se reusa para reintentar con el mismo número.
        raise PromocionError(
            "Ya tienes una conexión de producción con esta app y otro número: usa esa conexión, "
            "o elimínala antes de pasar esta",
            code="production_exists",
            status=409,
        )
    app_secret = await stored_app_secret(connection)
    if not app_secret:
        raise PromocionError(
            "No hay App Secret guardado para esta conexión: vuelve a conectarla con el asistente BYOK",
            code="missing_app_secret",
        )
    token = access_token or await access_token_for(connection)
    if not token:
        raise PromocionError("Falta el access token del número real", code="missing_access_token")

    try:
        produccion = await save_byok_connection(
            tenant_id,
            admin_id,
            connection.meta_app_id,
            app_secret,
            phone_number_id,
            token,
            waba_id,
            environment="production",
            verify_token=await stored_verify_token(connection),
        )
    except ByokError as exc:
        code = "test_number" if str(exc) == TEST_NUMBER_IN_PRODUCTION_MESSAGE else "byok_error"
        raise PromocionError(str(exc)[:250], code=code) from exc
    log.info(
        "byok_promocion_numero_real",
        tenant_id=str(tenant_id), connection_id=str(connection.id), production_connection_id=str(produccion.id),
        status=produccion.status,
    )
    return produccion


def _check(key: str, ok: bool | None, detail: str, repair_action: str | None = None) -> PreflightCheck:
    return PreflightCheck(key=key, ok=ok, detail=detail, checked_at=datetime.now(UTC), repair_action=repair_action)


async def _checks_del_numero(phone: PhoneNumber, creds: Any) -> tuple[PreflightCheck, PreflightCheck]:
    """`(real_number, registro)` de un número, con una sola llamada a Graph.

    El registro es el de ADR-0127 (`numero_registrado`: `platform_type = CLOUD_API`) y se
    suma al ítem `number_registered` del checklist, que mira `status`."""
    try:
        data = await graph_fetch(
            f"/{phone.phone_number_id}", token=creds.access_token, app_secret=creds.app_secret,
            query={"fields": "account_mode,platform_type,status"}, version=creds.graph_api_version,
            connection_id=creds.connection_id, tenant_id=creds.tenant_id,
        )
    except GraphError as exc:
        return _check("real_number", False, exc.message), _check("number_registered", None, exc.message)
    data = data or {}
    modo = data.get("account_mode")
    real = _check(
        "real_number",
        modo == ACCOUNT_MODE_LIVE,
        "Número real" if modo == ACCOUNT_MODE_LIVE else f"account_mode={modo!r}: no es un número de producción",
    )
    registrado = numero_registrado(data)
    return real, _check(
        "number_registered",
        registrado,
        "Registrado en la Cloud API" if registrado else "Falta registrarlo en la Cloud API con su PIN",
        None if registrado else "add_number",
    )


async def _check_waba_en_su_app(waba_id: str, creds: Any) -> PreflightCheck:
    """La WABA está suscrita a la app DEL NEGOCIO (no basta "alguna app": ADR-0102)."""
    key = "waba_subscribed"
    try:
        data = await graph_fetch(
            f"/{waba_id}/subscribed_apps", token=creds.access_token, app_secret=creds.app_secret,
            version=creds.graph_api_version, connection_id=creds.connection_id, tenant_id=creds.tenant_id,
        )
    except GraphError as exc:
        return _check(key, False, exc.message, "subscribe_waba")
    apps = [item for item in (data or {}).get("data") or [] if isinstance(item, dict)]
    ids = {
        str((item.get("whatsapp_business_api_data") or {}).get("id") or item.get("id") or "") for item in apps
    } - {""}
    if creds.app_id in ids:
        return _check(key, True, f"La WABA está suscrita a tu app {creds.app_id}")
    if ids:
        return _check(key, False, "La WABA está suscrita a otra app, no a la tuya: suscríbela", "subscribe_waba")
    if apps:
        return _check(key, None, "Meta no dijo a qué app está suscrita la WABA")
    return _check(key, False, "La WABA no está suscrita a ninguna app: suscríbela", "subscribe_waba")


# Lo que la promoción suma al checklist por conexión (HU-CONN-07): que la app sea propia y que
# el número sea real (no el de prueba de Meta).
PROMOCION_ITEM_LABELS: dict[str, str] = {
    "own_app": "Tu propia app de Meta",
    "real_number": "Número real (no el de prueba de Meta)",
}


def promotion_item_labels() -> dict[str, str]:
    from core.gateway.meta_production_readiness import CONNECTION_ITEM_LABELS

    return {**CONNECTION_ITEM_LABELS, **PROMOCION_ITEM_LABELS}


async def _chequear_con_tope(connection: MetaConnection) -> list[PreflightCheck]:
    try:
        return await asyncio.wait_for(checks_de_produccion(connection), TOPE_CHEQUEOS_S)
    except TimeoutError as exc:
        log.warning("promocion_chequeos_tope", tenant_id=str(connection.tenant_id), connection_id=str(connection.id))
        raise PromocionError(
            "Meta tardó demasiado en responder los chequeos: vuelve a intentarlo en unos minutos",
            code="checks_timeout",
            status=504,
        ) from exc


async def checks_de_produccion(connection: MetaConnection) -> list[PreflightCheck]:
    """Chequeos para pasar a producción una conexión BYOK (HU-CONN-06 sobre HU-CONN-07).

    La base es el checklist por conexión de HU-CONN-07 (`run_connection_production_checks`:
    token, número registrado, WABA suscrita, webhook y plantillas mínimas), con las
    credenciales de ESA conexión (`credentials_for`, ADR-0094): nada usa `META_APP_ID`/
    `META_APP_SECRET`. La promoción le suma lo que ese checklist no mira (ADR-0127):

    - `own_app`: la conexión usa su propia app, no la de la plataforma;
    - `real_number`: cada número está en `account_mode = LIVE`;
    - al ítem `number_registered`, el registro de ADR-0127 (`platform_type = CLOUD_API`);
    - al ítem `waba_subscribed`, que la WABA esté suscrita a **su** app según Meta (el checklist
      solo pide "alguna app" más la marca local del alta).

    Para terminar, ninguno puede dar `False` y los de `CHECKS_OBLIGATORIOS` tienen que dar `True`
    (`puede_terminar`). Sirve también para una BYOK creada directo en producción."""
    from core.gateway.meta_credentials import (
        CredentialsUnavailable,
        credentials_for,
        uses_platform_app,
    )
    from core.gateway.meta_production_readiness import (
        combine_checks,
        run_connection_production_checks,
        safe_check,
    )

    if uses_platform_app(connection):
        return [_check("own_app", False, "La conexión usa la app de la plataforma, no una propia", "reauthorize")]

    checklist = await run_connection_production_checks(connection, connection.tenant_id)
    try:
        creds = await credentials_for(connection)
    except CredentialsUnavailable:
        # Sin credenciales el checklist ya deja `token` en rojo y lo que depende de Meta sin dato.
        creds = None

    async with get_session() as session:
        numeros = await _numeros_vivos(session, connection)
        wabas = list(
            (
                await session.execute(
                    select(WabaAccount)
                    .where(
                        WabaAccount.tenant_id == connection.tenant_id,
                        WabaAccount.connection_id == connection.id,
                        WabaAccount.deleted_at.is_(None),
                    )
                    .order_by(WabaAccount.created_at)
                )
            ).scalars()
        )

    reales: list[PreflightCheck] = []
    registros: list[PreflightCheck] = []
    en_su_app: list[PreflightCheck] = []
    if creds is not None:
        for numero in numeros:
            try:
                real, registro = await _checks_del_numero(numero, creds)
            except Exception as exc:
                # Un error de red que no llega como `GraphError`: sin dato (bloquea, es obligatorio),
                # nunca un 500 con el texto de la excepción.
                log.warning("promocion_numero_sin_dato", connection_id=str(connection.id), error=type(exc).__name__)
                sin_dato = f"No se pudo consultar a Meta ({type(exc).__name__})"
                real, registro = _check("real_number", None, sin_dato), _check("number_registered", None, sin_dato)
            reales.append(real)
            registros.append(registro)
        for waba in wabas:
            en_su_app.append(
                await safe_check("waba_subscribed", lambda waba=waba: _check_waba_en_su_app(waba.waba_id, creds))
            )

    sumados = {"number_registered": registros, "waba_subscribed": en_su_app}
    checks = [_check("own_app", True, f"Se evalúa tu app de Meta {connection.meta_app_id}")]
    for item in checklist:
        extra = sumados.get(item.key) or []
        checks.append(combine_checks(item.key, [item, *extra]) if extra else item)
        if item.key == "number_registered":
            if reales:
                checks.append(combine_checks("real_number", reales))
            elif not numeros:
                checks.append(_check("real_number", False, "La conexión no tiene número: carga el número real", "add_number"))
            else:
                checks.append(_check("real_number", None, "No se pudo revisar: la conexión no tiene credenciales utilizables."))
    return checks


# Sin estos en `True` no se pasa a producción: un `None` (Meta no respondió) no alcanza. El
# webhook puede quedar sin dato (el cron de 15 min todavía no leyó la suscripción de la app).
CHECKS_OBLIGATORIOS = ("own_app", "token", "number_registered", "real_number", "waba_subscribed", "templates_approved")


def puede_terminar(checks: list[PreflightCheck]) -> bool:
    """Ninguno en `False` y los obligatorios en `True` (cada uno que aparezca, y al menos uno
    de cada clave)."""
    if any(check.ok is False for check in checks):
        return False
    claves = {check.key for check in checks}
    if not set(CHECKS_OBLIGATORIOS) <= claves:
        return False
    return all(check.ok is True for check in checks if check.key in CHECKS_OBLIGATORIOS)


async def verificar_promocion(tenant_id: uuid.UUID, connection_id: uuid.UUID) -> list[PreflightCheck]:
    """Chequeos con la app del negocio de la conexión de producción (la pareja de una de prueba,
    o la propia si ya es de producción), sin cambiar nada."""
    async with get_session() as session:
        connection = await _conexion(session, tenant_id, connection_id)
        if (error := _no_es_byok(connection)) is not None:
            raise error
        pareja = await _pareja_de_produccion(session, connection)
    if pareja is None:
        raise PromocionError("Primero carga el número real", code="missing_real_number")
    return await _chequear_con_tope(pareja)


async def terminar_promocion(
    tenant_id: uuid.UUID, connection_id: uuid.UUID
) -> tuple[bool, list[PreflightCheck], uuid.UUID]:
    """Paso 4: si los chequeos de la conexión de producción con la app del negocio pasan
    (`puede_terminar`), se da de baja la de prueba. Devuelve `(promovida, checks, id de la
    conexión de producción)`."""
    from core.admin.meta_health_service import soft_delete_connection

    async with get_session() as session:
        connection = await _conexion(session, tenant_id, connection_id)
        if (error := _no_promovible(connection)) is not None:
            raise error
        pareja = await _pareja_de_produccion(session, connection)
        if pareja is None:
            raise PromocionError("Primero carga el número real", code="missing_real_number")
    checks = await _chequear_con_tope(pareja)
    if not puede_terminar(checks):
        return False, checks, pareja.id

    # Sin candado entre los chequeos y la baja: lo peor de una carrera es dar de baja la de
    # prueba, que se recupera reconectándola; cargar otro número en la de producción da 409.
    error = await soft_delete_connection(tenant_id, str(connection.id))
    if error is not None:
        raise PromocionError(error, code="retire_failed", status=409)
    async with get_session() as session:
        # Motivo legible en la historia de la de prueba (ya eliminada).
        retirada = await session.get(MetaConnection, connection.id)
        if retirada is not None and retirada.tenant_id == tenant_id:
            retirada.status_reason = MOTIVO_REEMPLAZADA
    log.info(
        "byok_promocion_terminada",
        tenant_id=str(tenant_id), connection_id=str(connection.id), production_connection_id=str(pareja.id),
    )
    return True, checks, pareja.id
