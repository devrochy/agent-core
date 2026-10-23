"""Wizard de Embedded Signup de WhatsApp: máquina de estados persistida y
reanudable (`ConnectionWizard`). Único módulo que debe escribir esa tabla
-- verificado por `tests/test_meta_onboarding_no_persiste_secretos.py`.

Es la app del ISV/framework (`settings.meta_app_id`/`meta_app_secret`/
`meta_config_id`) la que hace el Embedded Signup en nombre de cada tenant --
no es BYOK como `meta_preflight.resolve_app_credentials` (eso es para
tenants que YA tienen su propia app y conexión). Por eso acá se usan esas
3 settings directo, no `resolve_app_credentials`.

REGLA DURA: ninguna función de este módulo acepta ni persiste un `code` de
Embedded Signup ni un token de acceso en `ConnectionWizard.context`/
`last_error` -- los secretos van a `meta_token_store` (AAD tenant+conexión+
tipo), nunca a esta tabla (RLS del propio tenant, no custodia de secretos).
Ver ADR-0030.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from core.config import get_settings
from core.db.models import (
    WIZARD_STEPS,
    ConnectionWizard,
    MessageTemplate,
    MetaConnection,
    PhoneNumber,
    WabaAccount,
    utcnow,
)
from core.db.session import get_owned, get_session
from core.gateway import meta_token_store
from core.gateway.meta_connection_state import get_live_meta_connection
from core.gateway.meta_errors import GraphError, classify_and_build_error
from core.gateway.meta_graph import graph_fetch

# El PIN vive en `meta_phone_numbers` (lo comparten el wizard y "Números"); se reexporta.
from core.gateway.meta_phone_numbers import PIN_KIND as PIN_KIND
from core.gateway.meta_phone_numbers import PIN_MISMATCH_CODE as PIN_MISMATCH_CODE
from core.gateway.meta_phone_numbers import TOO_MANY_GUESSES_CODE as TOO_MANY_GUESSES_CODE
from core.shared.exceptions import AgentCoreError
from core.shared.logger import get_logger
from core.shared.tenant import get_current_tenant_id

log = get_logger(__name__)

GRAPH_BASE = "https://graph.facebook.com"
_TIMEOUT_S = 15.0
TOKEN_KIND = "business_integration_system_user"

# object del popup -> canal (mismo mapeo que webhook_flatten.py/meta_subscriptions.py)
_EVENT_COEXISTENCE = "FINISH_WHATSAPP_BUSINESS_APP_ONBOARDING"
_EVENT_ONLY_WABA = "FINISH_ONLY_WABA"
_EVENT_FINISH = "FINISH"


class WizardError(AgentCoreError):
    """Error de esta capa (wizard no encontrado, code expirado, paso
    inválido) -- distinto de `GraphError`, que es un error de Graph API.

    `code` es estable (lo usa la API para el panel); `status` es el HTTP que le corresponde
    (400 por defecto, 404 si el wizard no es de este negocio, 409 si el paso ya pasó)."""

    def __init__(self, message: str, *, code: str = "wizard_error", status: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.status = status


# Pasos en los que todavía se puede canjear un `code` del popup (antes del canje).
PRE_EXCHANGE_STEPS: tuple[str, ...] = ("select_channel", "launch_oauth", "exchange_code")
# Eventos de éxito del popup (Embedded Signup v4, verificado contra la documentación de Meta
# el 2026-10-09). `FINISH_OBO_MIGRATION` y `FINISH_GRANT_ONLY_API_ACCESS` existen, pero no son
# flujos que ofrezca este wizard: se rechazan en vez de dejar una conexión a medias.
FINISH_EVENTS: frozenset[str] = frozenset({"FINISH", "FINISH_ONLY_WABA", "FINISH_WHATSAPP_BUSINESS_APP_ONBOARDING"})


@dataclass
class ConnectionWizardView:
    """Vista seguro-de-exponer de un `ConnectionWizard` (sin nada sensible,
    apto para pasar a la plantilla o devolver en JSON)."""

    id: uuid.UUID
    connection_id: uuid.UUID | None
    channel: str
    environment: str
    current_step: str
    completed_steps: list[str]
    last_error: dict[str, Any] | None
    expires_at: datetime | None = None
    # Última cancelación del popup (`record_cancel_or_progress`): paso de Meta y cuándo.
    cancelled: dict[str, Any] | None = None


def _to_view(wizard: ConnectionWizard) -> ConnectionWizardView:
    return ConnectionWizardView(
        id=wizard.id,
        connection_id=wizard.connection_id,
        channel=wizard.channel,
        environment=wizard.environment,
        current_step=wizard.current_step,
        completed_steps=list(wizard.completed_steps or []),
        last_error=wizard.last_error,
        expires_at=getattr(wizard, "expires_at", None),
        cancelled=(getattr(wizard, "context", None) or {}).get("abandono"),
    )


def _mark_done(wizard: ConnectionWizard, step: str) -> None:
    if step not in wizard.completed_steps:
        wizard.completed_steps = [*wizard.completed_steps, step]


def _advance_to(wizard: ConnectionWizard, step: str) -> None:
    _mark_done(wizard, wizard.current_step)
    wizard.current_step = step


async def _get_wizard(wizard_id: uuid.UUID, tenant_id: uuid.UUID | None = None) -> ConnectionWizard:
    async with get_session() as session:
        wizard = await get_owned(session, ConnectionWizard, wizard_id, tenant_id)
        if wizard is None:
            raise WizardError("El wizard no existe o no pertenece a este tenant", code="not_found", status=404)
        return wizard


async def get_wizard(wizard_id: uuid.UUID) -> ConnectionWizardView:
    """El wizard del negocio actual, o `WizardError` 404 (también si es de otro negocio)."""
    return _to_view(await _get_wizard(wizard_id, get_current_tenant_id()))


def missing_platform_settings() -> list[str]:
    """Variables de entorno de la app de la plataforma que le faltan a Embedded Signup."""
    settings = get_settings()
    faltan = [
        nombre
        for nombre, valor in (
            ("META_APP_ID", settings.meta_app_id),
            ("META_APP_SECRET", settings.meta_app_secret),
            ("META_CONFIG_ID", settings.meta_config_id),
        )
        if not valor
    ]
    return faltan


async def start_or_resume_wizard(
    tenant_id: uuid.UUID,
    channel: str = "whatsapp",
    environment: str = "production",
    *,
    admin_id: uuid.UUID | None = None,
    restart: bool = False,
) -> ConnectionWizardView:
    """Reanuda el wizard "vivo" (`current_step != 'done'`) de este tenant+
    canal+entorno si existe y no expiró; si expiró (o `restart`), lo cierra y
    arranca uno nuevo sobre la misma conexión (el borrador se reutiliza). Si ya
    hay una `MetaConnection` `connected` para ese tenant+canal, entra en modo
    REAUTORIZACIÓN: reusa esa conexión en vez de crear una nueva, y arranca
    directo en `launch_oauth` (se salta `select_channel`).

    Dos pedidos simultáneos chocan en el índice único del wizard vivo: el que pierde
    reintenta una vez y reanuda el que creó el otro (antes era un 500)."""
    faltan = missing_platform_settings()
    if faltan:
        raise WizardError(
            f"Embedded Signup no está configurado en este entorno: faltan {', '.join(faltan)} "
            "(la app de Meta de la plataforma). Mientras tanto, conecta con tu propia app (BYOK).",
            code="platform_app_not_configured",
        )
    try:
        return await _start_or_resume_once(tenant_id, channel, environment, admin_id=admin_id, restart=restart)
    except IntegrityError:
        log.info("embedded_signup_wizard_carrera", tenant_id=str(tenant_id))
        return await _start_or_resume_once(tenant_id, channel, environment, admin_id=admin_id, restart=False)


def _puede_reutilizar(connection: MetaConnection) -> None:
    """El alta con Facebook reutiliza la conexión viva del entorno (reautorizar, o el borrador
    tras 24 h), pero no toma (HU-SH-05, ADR-0128):

    - una conexión con la app propia del negocio (BYOK): desde ADR-0127 las hay en producción y
      tomarla dejaba su app suscrita a la WABA, su secreto aceptado en el webhook y
      `meta_app_id` desalineado (ADR-0126 §8). El negocio la elimina y conecta de nuevo;
    - una conexión pausada a mano (`revoked`): el canje la volvía `pending` y la reactivaba sin
      el diagnóstico que exige "Activar" (ADR-0122)."""
    if connection.origin == "byok":
        raise WizardError(
            "Ya tienes una conexión de WhatsApp con tu propia app de Meta en este entorno. Para "
            "conectar con Facebook, elimínala primero en Conexiones; si no, sigue usándola.",
            code="byok_connection_exists",
            status=409,
        )
    if connection.status == "revoked":
        raise WizardError(
            "Tienes una conexión de WhatsApp pausada en este entorno: actívala o elimínala en "
            "Conexiones antes de conectar con Facebook.",
            code="connection_paused",
            status=409,
        )


async def _start_or_resume_once(
    tenant_id: uuid.UUID, channel: str, environment: str, *, admin_id: uuid.UUID | None, restart: bool
) -> ConnectionWizardView:
    settings = get_settings()
    async with get_session() as session:
        existing = (
            await session.execute(
                select(ConnectionWizard).where(
                    ConnectionWizard.tenant_id == tenant_id,
                    ConnectionWizard.channel == channel,
                    ConnectionWizard.environment == environment,
                    ConnectionWizard.current_step != "done",
                )
            )
        ).scalar_one_or_none()

        if existing is not None and existing.expires_at > utcnow() and not restart:
            return _to_view(existing)

        if existing is not None:
            # Expiró (o el admin pidió empezar de nuevo): se cierra (no se borra -- queda
            # como historial de abandono) para liberar el índice único parcial y poder crear
            # una fila nueva. La conexión (el borrador) se reutiliza abajo.
            existing.current_step = "done"
            existing.last_error = (
                {"code": "wizard_restarted", "message": "Se empezó el alta de nuevo"}
                if restart and existing.expires_at > utcnow()
                else {"code": "wizard_expired", "message": "El wizard expiró (>24h) y se reinició"}
            )
            await session.flush()

        connected = (
            await session.execute(
                select(MetaConnection)
                .where(
                    MetaConnection.tenant_id == tenant_id,
                    MetaConnection.channel == channel,
                    MetaConnection.environment == environment,
                    MetaConnection.status == "connected",
                    MetaConnection.deleted_at.is_(None),
                )
                .order_by(MetaConnection.created_at.desc())
            )
        ).scalars().first()

        if connected is not None:
            _puede_reutilizar(connected)
            connection_id = connected.id
            current_step = "launch_oauth"
        else:
            # Reutiliza cualquier MetaConnection viva (draft/pending/degraded)
            # del mismo tenant+canal+entorno: el índice único parcial
            # `uq_meta_connections_tenant_app` no admite una segunda fila no
            # borrada para la misma app. Sin esto, reiniciar un wizard vencido
            # revienta con IntegrityError (HU-CONN-02).
            reutilizable = (
                await session.execute(
                    select(MetaConnection)
                    .where(
                        MetaConnection.tenant_id == tenant_id,
                        MetaConnection.channel == channel,
                        MetaConnection.environment == environment,
                        MetaConnection.deleted_at.is_(None),
                    )
                    .order_by(MetaConnection.created_at.desc())
                )
            ).scalars().first()
            if reutilizable is not None:
                _puede_reutilizar(reutilizable)
                connection_id = reutilizable.id
            else:
                connection = MetaConnection(
                    tenant_id=tenant_id,
                    channel=channel,
                    environment=environment,
                    origin="embedded_signup",
                    meta_app_id=settings.meta_app_id,
                    meta_config_id=settings.meta_config_id,
                    graph_api_version=settings.graph_api_version,
                    status="draft",
                )
                session.add(connection)
                await session.flush()
                connection_id = connection.id
            current_step = "select_channel"

        wizard = ConnectionWizard(
            tenant_id=tenant_id,
            connection_id=connection_id,
            channel=channel,
            environment=environment,
            current_step=current_step,
            started_by=admin_id,
        )
        session.add(wizard)
        await session.flush()
        return _to_view(wizard)


async def latest_wizard(
    tenant_id: uuid.UUID, channel: str = "whatsapp", environment: str = "production"
) -> ConnectionWizardView | None:
    """El wizard más reciente del negocio para ese canal y entorno (vivo o terminado), sin
    tocar nada: para mostrar en qué paso quedó y reanudarlo. Si hay uno vivo, es ese."""
    async with get_session() as session:
        wizard = (
            await session.execute(
                select(ConnectionWizard)
                .where(
                    ConnectionWizard.tenant_id == tenant_id,
                    ConnectionWizard.channel == channel,
                    ConnectionWizard.environment == environment,
                )
                .order_by((ConnectionWizard.current_step == "done").asc(), ConnectionWizard.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        return _to_view(wizard) if wizard is not None else None


def _attempt_hash(attempt: str) -> str:
    return hashlib.sha256(attempt.encode()).hexdigest()


# Un intento vale lo que dura un popup razonable: después hay que abrir otro.
ATTEMPT_TTL_S = 15 * 60


async def issue_attempt(wizard_id: uuid.UUID, admin_id: uuid.UUID | None) -> str:
    """Abre un intento de popup: devuelve un valor aleatorio que el panel manda de vuelta con
    el `code`. Solo se guarda su hash, con el admin que lo pidió y cuándo. Ata el canje al
    wizard, al admin y a la sesión del panel que abrió el popup (una pestaña vieja, otro admin
    o un pedido armado en otro lado no canjean), vence a los 15 min y es de un solo uso: el
    canje lo consume con la fila bloqueada. Cada llamada invalida el anterior."""
    tenant_id = get_current_tenant_id()
    attempt = secrets.token_urlsafe(24)
    async with get_session() as session:
        wizard = await get_owned(session, ConnectionWizard, wizard_id, tenant_id)
        if wizard is None:
            raise WizardError("El wizard no existe o no pertenece a este tenant", code="not_found", status=404)
        contexto = dict(wizard.context or {})
        contexto.pop("attempt_sha256", None)
        contexto["intento"] = {
            "sha256": _attempt_hash(attempt),
            "admin_id": str(admin_id) if admin_id else None,
            "issued_at": time.time(),
        }
        wizard.context = contexto
    return attempt


def _check_exchange_allowed(wizard: ConnectionWizard, attempt: str, admin_id: uuid.UUID | None) -> None:
    if wizard.current_step not in PRE_EXCHANGE_STEPS:
        raise WizardError(
            "Este alta ya pasó el paso de Facebook. Retómala desde donde quedó o empieza de nuevo.",
            code="already_exchanged",
            status=409,
        )
    if wizard.expires_at is not None and wizard.expires_at <= utcnow():
        raise WizardError("El alta venció (24 h): empieza de nuevo.", code="wizard_expired", status=409)
    intento = (wizard.context or {}).get("intento") or {}
    esperado = str(intento.get("sha256") or "")
    vigente = time.time() - float(intento.get("issued_at") or 0) <= ATTEMPT_TTL_S
    mismo_admin = intento.get("admin_id") == (str(admin_id) if admin_id else None)
    if (
        not attempt
        or not esperado
        or not hmac.compare_digest(_attempt_hash(attempt), esperado)
        or not vigente
        or not mismo_admin
    ):
        raise WizardError(
            "El intento de conexión no es válido, venció o ya se usó: vuelve a abrir el popup de Facebook.",
            code="invalid_attempt",
            status=409,
        )


_CANCEL_TEXT_MAX = 300


def _texto_de_meta(value: str | None) -> str | None:
    """Texto que el navegador reenvía desde el popup de Meta: es dato, no instrucción. Se
    guarda recortado y sin caracteres de control."""
    if not value:
        return None
    limpio = "".join(ch for ch in str(value) if ch.isprintable())
    return limpio[:_CANCEL_TEXT_MAX] or None


async def record_cancel_or_progress(
    wizard_id: uuid.UUID,
    *,
    current_step: str | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
    error_id: str | None = None,
    session_id: str | None = None,
) -> ConnectionWizardView:
    """Escrito por el listener de `postMessage` en el evento `CANCEL` --
    analítica de abandono y "reintentar". NUNCA acepta `code` ni token (no hay
    parámetro para eso, a propósito): esta función no puede persistir un secreto
    aunque quisiera. El wizard sigue vivo en el mismo paso: el admin puede volver a
    abrir el popup. Si el wizard ya pasó el canje, la cancelación de un popup viejo
    no cambia nada."""
    tenant_id = get_current_tenant_id()
    async with get_session() as session:
        wizard = await get_owned(session, ConnectionWizard, wizard_id, tenant_id)
        if wizard is None:
            raise WizardError("El wizard no existe o no pertenece a este tenant", code="not_found", status=404)
        if wizard.current_step not in PRE_EXCHANGE_STEPS:
            return _to_view(wizard)
        mensaje = _texto_de_meta(error_message)
        if error_code or mensaje or error_id:
            wizard.last_error = {
                "code": "meta_signup_error",
                "message": mensaje or "Meta reportó un error en el popup.",
                "meta_error_code": _texto_de_meta(error_code),
                "error_id": _texto_de_meta(error_id),
                "session_id": _texto_de_meta(session_id),
                "at_step": "launch_oauth",
            }
        contexto = dict(wizard.context or {})
        contexto["abandono"] = {
            "current_step": _texto_de_meta(current_step),
            "session_id": _texto_de_meta(session_id),
            "with_error": bool(error_code or mensaje),
            "at": utcnow().isoformat(),
        }
        contexto["cancel_count"] = int(contexto.get("cancel_count") or 0) + 1
        # Un intento cancelado ya no canjea: reintentar abre uno nuevo.
        contexto.pop("intento", None)
        wizard.context = contexto
        await session.flush()
        return _to_view(wizard)


async def consume_attempt(
    wizard_id: uuid.UUID, event_type: str, *, attempt: str, admin_id: uuid.UUID | None
) -> ConnectionWizardView:
    """Primer paso del canje: con la fila del wizard bloqueada, verifica que espere el canje,
    que no haya vencido y el intento del popup (`issue_attempt`), y lo consume en la misma
    transacción. Dos canjes simultáneos con el mismo intento: el segundo recibe 409. No escribe
    nada de lo que manda el navegador (portafolio, coexistencia): eso se guarda en
    `exchange_code`, después del canje verificado."""
    if event_type not in FINISH_EVENTS:
        raise WizardError("Ese tipo de alta de Meta no está soportado por este asistente.", code="unsupported_event")
    tenant_id = get_current_tenant_id()
    async with get_session() as session:
        wizard = (
            await session.execute(
                select(ConnectionWizard)
                .where(ConnectionWizard.id == wizard_id, ConnectionWizard.tenant_id == tenant_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if wizard is None:
            raise WizardError("El wizard no existe o no pertenece a este tenant", code="not_found", status=404)
        _check_exchange_allowed(wizard, attempt, admin_id)
        contexto = dict(wizard.context or {})
        contexto.pop("intento", None)
        wizard.context = contexto
        await session.flush()
        return _to_view(wizard)


async def _activos_vivos(connection_id: uuid.UUID, tenant_id: uuid.UUID | None) -> tuple[set[str], set[str]]:
    """WABAs y números vivos de la conexión (por si se reutiliza una que ya los tiene)."""
    async with get_session() as session:
        wabas = set(
            (
                await session.execute(
                    select(WabaAccount.waba_id).where(
                        WabaAccount.connection_id == connection_id,
                        WabaAccount.tenant_id == tenant_id,
                        WabaAccount.deleted_at.is_(None),
                    )
                )
            ).scalars()
        )
        numeros = set(
            (
                await session.execute(
                    select(PhoneNumber.phone_number_id).where(
                        PhoneNumber.connection_id == connection_id,
                        PhoneNumber.tenant_id == tenant_id,
                        PhoneNumber.deleted_at.is_(None),
                    )
                )
            ).scalars()
        )
    return wabas, numeros


def _check_mismos_activos(
    wabas: set[str], numeros: set[str], *, waba_id: str | None, phone_number_id: str | None
) -> None:
    """Una conexión que ya tiene WABA o número solo se reautoriza con los mismos. Con otro, la
    conexión quedaría con dos números vivos y el sender respondería por cualquiera (ADR-0127,
    alternativas); para otro número se elimina la conexión y se conecta de nuevo."""
    if wabas and waba_id and waba_id not in wabas:
        raise WizardError(
            "Esta conexión ya es de otra cuenta de WhatsApp Business. Para conectar otra, elimina "
            "esta conexión en Conexiones y vuelve a empezar.",
            code="different_waba",
            status=409,
        )
    if numeros and phone_number_id and phone_number_id not in numeros:
        raise WizardError(
            "Esta conexión ya tiene otro número. Para conectar otro, elimina esta conexión en "
            "Conexiones y vuelve a empezar.",
            code="different_number",
            status=409,
        )


async def _attach_finish_assets(
    wizard_id: uuid.UUID,
    tenant_id: uuid.UUID | None,
    connection_id: uuid.UUID,
    token: str,
    *,
    waba_id: str | None,
    phone_number_id: str | None,
) -> None:
    """Guarda la WABA y el número que dio el popup, solo si el token recién canjeado los
    ve en Meta y ningún otro negocio los tiene (ADR-0095). Los IDs del popup los manda el
    navegador: sin esta verificación, cualquiera podría ocupar la WABA de otro."""
    from core.gateway.meta_asset_claims import (
        MetaAssetError,
        claim_assets,
        numero_para_conexion,
        verify_waba_and_phone,
        waba_para_conexion,
    )

    if not waba_id:
        if phone_number_id:
            raise WizardError("Meta no devolvió la cuenta de WhatsApp Business del número.", code="missing_waba")
        return

    settings = get_settings()
    async with get_session() as session:
        connection = await get_live_meta_connection(session, connection_id, tenant_id)
        version = (connection.graph_api_version if connection else None) or settings.graph_api_version

    try:
        await verify_waba_and_phone(
            waba_id=waba_id, phone_number_id=phone_number_id, token=token,
            app_secret=settings.meta_app_secret, version=version, connection_id=connection_id,
        )
        async with get_session() as session:
            await claim_assets(session, tenant_id, waba_id=waba_id, phone_number_id=phone_number_id)
            # Las filas de una conexión en `draft` no cuentan para el ruteo ni el reclamo
            # (migración 0052): sale de `draft` en la misma transacción que las inserta, o
            # un alta simultánea de la misma WABA no las vería.
            # La conexión pudo eliminarse mientras el asistente estaba abierto: sin esto, el
            # número y la WABA quedarían vivos colgando de una conexión borrada y bloquearían
            # el número para cualquier otro negocio (auditor-multitenant, ADR-0122).
            connection = await get_live_meta_connection(session, connection_id, tenant_id, for_update=True)
            if connection is None:
                raise MetaAssetError(
                    "La conexión se eliminó mientras se completaba el alta: empieza de nuevo.",
                    code="connection_deleted",
                )
            if connection.status == "draft":
                connection.status = "pending"
            # Revive la WABA y el número si el negocio los tenía en una conexión eliminada, y
            # le pasa el número a esta conexión si lo tenía otra (ADR-0122).
            waba = await waba_para_conexion(session, tenant_id, connection_id, waba_id)
            if phone_number_id:
                await numero_para_conexion(
                    session, tenant_id, connection_id, phone_number_id, waba_account_id=waba.id
                )
    except MetaAssetError as exc:
        async with get_session() as session:
            wizard = await get_owned(session, ConnectionWizard, wizard_id, tenant_id)
            if wizard is not None:
                wizard.last_error = {"code": exc.code, "message": exc.message, "at_step": "exchange_code"}
        raise WizardError(exc.message, code=exc.code) from exc


async def exchange_code(
    wizard_id: uuid.UUID,
    code: str,
    *,
    waba_id: str | None = None,
    phone_number_id: str | None = None,
    business_id: str | None = None,
    event_type: str = _EVENT_FINISH,
) -> ConnectionWizardView:
    """Time-critical: el `code` de Meta tiene TTL de 30s. Sin `redirect_uri` (así lo documenta
    Meta para el popup, verificado 2026-10-09); si Meta responde OAuthException mencionando
    `redirect_uri`, se reintenta UNA vez agregándolo.

    Orden (HU-SH-05, revisión de seguridad): antes de llamar a Meta se rechaza reautorizar con
    otra WABA u otro número; después del canje, primero se verifica con el token **en memoria**
    que vea la WABA y el número (ADR-0095) y recién entonces se guarda. Un canje que no verifica
    no reemplaza el token de una conexión que funciona. El portafolio (`business_id`) y la
    coexistencia del popup también se guardan solo después; la coexistencia la confirma Meta en
    el paso siguiente (`is_on_biz_app`)."""
    from core.gateway.meta_asset_claims import MetaAssetError, verify_waba_and_phone

    tenant_id = get_current_tenant_id()
    settings = get_settings()
    started_at = time.monotonic()

    wizard = await _get_wizard(wizard_id, tenant_id)
    if wizard.connection_id is None:
        raise WizardError("El wizard no tiene una conexión asociada todavía")
    if wizard.current_step not in PRE_EXCHANGE_STEPS:
        # Un segundo canje (doble clic, pestaña vieja) no pisa el token ya guardado.
        raise WizardError(
            "Este alta ya pasó el paso de Facebook. Retómala desde donde quedó o empieza de nuevo.",
            code="already_exchanged",
            status=409,
        )
    connection_id = wizard.connection_id
    wabas, numeros = await _activos_vivos(connection_id, tenant_id)
    _check_mismos_activos(wabas, numeros, waba_id=waba_id, phone_number_id=phone_number_id)
    if not waba_id and not wabas:
        raise WizardError(
            "Meta no devolvió la cuenta de WhatsApp Business: vuelve a conectar y elige una.", code="missing_waba"
        )

    version = settings.graph_api_version
    params = {"client_id": settings.meta_app_id, "client_secret": settings.meta_app_secret, "code": code}
    call_started_at = time.monotonic()

    token, used_redirect_uri = await _fetch_exchange_token(version, params, settings)
    del params

    latency_ms = int((time.monotonic() - started_at) * 1000)
    dispatch_ms = int((call_started_at - started_at) * 1000)
    log.info(
        "embedded_signup_exchange_latency",
        wizard_id=str(wizard_id),
        latency_ms=latency_ms,
        dispatch_ms=dispatch_ms,
        used_redirect_uri=used_redirect_uri,
    )

    if waba_id:
        await _attach_finish_assets(
            wizard_id, tenant_id, connection_id, token, waba_id=waba_id, phone_number_id=phone_number_id
        )
    else:
        # Reautorización sin WABA en el popup: el token nuevo tiene que ver la que ya tiene.
        try:
            await verify_waba_and_phone(
                waba_id=sorted(wabas)[0], phone_number_id=None, token=token,
                app_secret=settings.meta_app_secret, version=version, connection_id=connection_id,
            )
        except MetaAssetError as exc:
            raise WizardError(exc.message, code=exc.code) from exc

    async with get_session() as session:
        wizard = await get_owned(session, ConnectionWizard, wizard_id, tenant_id)
        connection = await get_live_meta_connection(session, connection_id, tenant_id, for_update=True)
        if connection is None:
            raise WizardError(
                "La conexión se eliminó mientras se completaba el alta: empieza de nuevo.", code="connection_deleted"
            )
        # El sujeto del token no cambia en una reautorización: si cambiara, `access_token_refs`
        # seguiría encontrando el token viejo primero.
        subject_id = connection.business_id or str(connection.id)
        if connection.status == "draft":
            connection.status = "pending"
        # Una conexión `manual` reautorizada por Facebook pasa a Embedded Signup (la misma app de
        # la plataforma). Las BYOK no llegan aquí (`_puede_reutilizar`).
        connection.origin = "embedded_signup"
        if business_id and not connection.business_id:
            connection.business_id = business_id
        if event_type == _EVENT_COEXISTENCE:
            connection.health = {**(connection.health or {}), "coexistence": True}
            _mark_done(wizard, "register_number")
        _advance_to(wizard, "discover_assets")
        wizard.last_error = None
        await session.flush()
        view = _to_view(wizard)

    # Verificado: recién ahora el token reemplaza al anterior (si lo había).
    await meta_token_store.put_token(connection_id, TOKEN_KIND, subject_id, token, tenant_id=tenant_id)
    return view


async def _fetch_exchange_token(
    version: str,
    params: dict[str, str],
    settings: Any,
    *,
    redirect_path: str = "/admin/conexiones/wizard/whatsapp",
) -> tuple[str, bool]:
    async def _call(extra_params: dict[str, str]) -> httpx.Response:
        async with httpx.AsyncClient(timeout=_TIMEOUT_S) as client:
            return await client.get(f"{GRAPH_BASE}/{version}/oauth/access_token", params={**params, **extra_params})

    try:
        response = await _call({})
    except httpx.HTTPError as exc:
        # Cualquier error de transporte (no solo timeout o conexión): sin esto un `ReadError`
        # subía como 500 con `params` (client_secret y code) en los frames.
        raise GraphError(f"No se pudo contactar a Meta para canjear el code ({type(exc).__name__})", category="TRANSIENT") from None

    if response.status_code >= 400:
        body = _safe_json(response)
        log.warning("embedded_signup_exchange_error_raw", body_redacted=_redact(body))
        error = classify_and_build_error(http_status=response.status_code, body=body, headers=response.headers, raw_text=response.text)
        message = (error.error_user_msg or error.message or "").lower()
        if "redirect_uri" in message:
            # Hallazgo documentado (no confirmado contra doc primaria, ver
            # docstring del módulo): en al menos algún caso real Meta exigió
            # redirect_uri incluso en el flujo de popup. Se reintenta UNA vez.
            redirect_uri = f"{settings.public_base_url.rstrip('/')}{redirect_path}"
            try:
                response2 = await _call({"redirect_uri": redirect_uri})
            except httpx.HTTPError as exc:
                raise GraphError(
                    f"No se pudo contactar a Meta (reintento con redirect_uri, {type(exc).__name__})", category="TRANSIENT"
                ) from None
            if response2.status_code >= 400:
                body2 = _safe_json(response2)
                raise classify_and_build_error(
                    http_status=response2.status_code, body=body2, headers=response2.headers, raw_text=response2.text
                )
            token = _extract_token(response2)
            return token, True
        raise error

    token = _extract_token(response)
    return token, False


def _extract_token(response: httpx.Response) -> str:
    data = _safe_json(response) or {}
    token = data.get("access_token")
    if not token:
        raise WizardError("Meta no devolvió access_token en el canje")
    return str(token)


def _safe_json(response: httpx.Response) -> dict[str, Any] | None:
    try:
        return response.json()
    except Exception:
        return None


def _redact(body: Any) -> Any:
    """Tapa recursivamente cualquier clave con "token" o "secret" (también anidada)."""
    if isinstance(body, dict):
        return {
            k: ("<redactado>" if any(x in str(k).lower() for x in ("token", "secret")) else _redact(v))
            for k, v in body.items()
        }
    if isinstance(body, list):
        return [_redact(v) for v in body]
    return body


async def run_automatic_steps(wizard_id: uuid.UUID, *, deadline: float | None = None) -> ConnectionWizardView:
    """Ejecuta en cadena los pasos que NO requieren input del admin, hasta
    llegar a uno que sí lo requiere (`register_number` sin PIN todavía) o a
    `done`. Cada paso es idempotente: si ya está en `completed_steps`, no
    vuelve a llamar a Meta.

    `deadline` (`time.monotonic()`): pasado ese momento no arranca otro paso y lanza
    `continue_later`; el panel lo retoma con `/wizard/resume` (el request no crece sin tope)."""
    tenant_id = get_current_tenant_id()
    wizard = await _get_wizard(wizard_id, tenant_id)

    step_order = list(WIZARD_STEPS)
    while wizard.current_step not in ("register_number", "done") and wizard.current_step in step_order:
        step = wizard.current_step
        if deadline is not None and step not in PRE_EXCHANGE_STEPS and time.monotonic() > deadline:
            raise WizardError(
                "El alta sigue en curso: pulsa «Retomar desde aquí» para terminarla.", code="continue_later"
            )
        if step == "discover_assets":
            wizard = await _discover_assets_step(wizard_id, tenant_id)
        elif step == "select_assets":
            wizard = await _select_assets_step(wizard_id, tenant_id)
        elif step == "subscribe_webhook":
            wizard = await _subscribe_webhook_step(wizard_id, tenant_id)
        elif step == "sync_templates":
            wizard = await _sync_templates_step(wizard_id, tenant_id)
        elif step == "verify_health":
            wizard = await _verify_health_step(wizard_id, tenant_id)
        else:
            break  # select_channel/launch_oauth/exchange_code: esperan al frontend

    if wizard.current_step == "register_number" and "register_number" in (wizard.completed_steps or []):
        wizard = await _advance_past_register(wizard_id, tenant_id)
        return await run_automatic_steps(wizard.id, deadline=deadline)

    return _to_view(wizard)


async def _token_de(connection: MetaConnection | None) -> str | None:
    """Token del system user de la conexión, con la regla única de `meta_credentials`
    (prueba el `business_id` y el id de la conexión como sujeto)."""
    from core.gateway.meta_credentials import access_token_for

    if connection is None:
        return None
    return await access_token_for(connection)


async def _waba_viva(session, wizard: ConnectionWizard) -> WabaAccount | None:
    """La WABA viva de la conexión del wizard (la más antigua, con orden fijo)."""
    return (
        await session.execute(
            select(WabaAccount)
            .where(
                WabaAccount.connection_id == wizard.connection_id,
                WabaAccount.tenant_id == wizard.tenant_id,
                WabaAccount.deleted_at.is_(None),
            )
            .order_by(WabaAccount.created_at)
            .limit(1)
        )
    ).scalar_one_or_none()


async def _numero_vivo(session, wizard: ConnectionWizard) -> PhoneNumber | None:
    """El número vivo de la conexión del wizard (el más antiguo, con orden fijo)."""
    return (
        await session.execute(
            select(PhoneNumber)
            .where(
                PhoneNumber.connection_id == wizard.connection_id,
                PhoneNumber.tenant_id == wizard.tenant_id,
                PhoneNumber.deleted_at.is_(None),
            )
            .order_by(PhoneNumber.created_at)
            .limit(1)
        )
    ).scalar_one_or_none()


async def _load_connection(wizard: ConnectionWizard, session) -> MetaConnection | None:
    if wizard.connection_id is None:
        return None
    return await get_live_meta_connection(session, wizard.connection_id, wizard.tenant_id)


async def _discover_assets_step(wizard_id: uuid.UUID, tenant_id: uuid.UUID | None) -> ConnectionWizardView:
    async with get_session() as session:
        wizard = await get_owned(session, ConnectionWizard, wizard_id, tenant_id)
        connection = await _load_connection(wizard, session)
        waba = await _waba_viva(session, wizard)
        phone = await _numero_vivo(session, wizard)
        settings = get_settings()
        version = connection.graph_api_version if connection else settings.graph_api_version

    token = await _token_de(connection)
    if token and waba is not None and not waba.raw:
        data = await graph_fetch(
            f"/{waba.waba_id}", token=token, app_secret=settings.meta_app_secret,
            query={"fields": "name,currency,timezone_id,message_template_namespace,account_review_status"},
            version=version, connection_id=wizard.connection_id,
        )
        async with get_session() as session:
            waba_row = await session.get(WabaAccount, waba.id)
            waba_row.name = data.get("name")
            waba_row.currency = data.get("currency")
            waba_row.timezone_id = data.get("timezone_id")
            waba_row.message_template_namespace = data.get("message_template_namespace")
            waba_row.account_review_status = data.get("account_review_status")
            waba_row.raw = data

    if token and phone is not None and not phone.raw:
        data = await graph_fetch(
            f"/{phone.phone_number_id}", token=token, app_secret=settings.meta_app_secret,
            query={"fields": "display_phone_number,verified_name,quality_rating,messaging_limit_tier,name_status,code_verification_status,platform_type,is_on_biz_app"},
            version=version, connection_id=wizard.connection_id,
        )
        async with get_session() as session:
            phone_row = await session.get(PhoneNumber, phone.id)
            phone_row.display_phone_number = data.get("display_phone_number")
            phone_row.verified_name = data.get("verified_name")
            phone_row.quality_rating = data.get("quality_rating")
            phone_row.messaging_limit_tier = data.get("messaging_limit_tier")
            phone_row.name_status = data.get("name_status")
            phone_row.code_verification_status = data.get("code_verification_status")
            phone_row.platform_type = data.get("platform_type")
            phone_row.raw = data
            # La coexistencia (número compartido con la app de WhatsApp Business) la dice Meta,
            # no el evento del popup: decide si el número se registra (HU-SH-05).
            en_la_app = data.get("is_on_biz_app")
            if isinstance(en_la_app, bool):
                conexion = await get_live_meta_connection(session, wizard.connection_id, tenant_id, for_update=True)
                wizard_row = await get_owned(session, ConnectionWizard, wizard_id, tenant_id)
                if conexion is not None:
                    conexion.health = {**(conexion.health or {}), "coexistence": en_la_app}
                if wizard_row is not None and not en_la_app and "register_number" in (wizard_row.completed_steps or []):
                    wizard_row.completed_steps = [p for p in wizard_row.completed_steps if p != "register_number"]
                if wizard_row is not None and en_la_app:
                    _mark_done(wizard_row, "register_number")

    if token:
        await _verify_token_scopes(token, version, settings, wizard.connection_id, waba.waba_id if waba else None)

    async with get_session() as session:
        wizard = await get_owned(session, ConnectionWizard, wizard_id, tenant_id)
        _advance_to(wizard, "select_assets")
        await session.flush()
        return _to_view(wizard)


# Nombres de scope vistos en la práctica para el mismo permiso -- evidencia
# de 2026 de que Meta devuelve `granular_scopes` con nombres distintos
# según la app/momento (`whatsapp_business_manage_events` en vez de
# `whatsapp_business_management`), mientras el array plano `scopes` sí trae
# el nombre esperado. Se aceptan todos, nunca se aborta si no matchea.
_WHATSAPP_MANAGEMENT_SCOPE_NAMES = {"whatsapp_business_management", "whatsapp_business_manage_events"}
_debug_token_logged_once = False


async def _verify_token_scopes(
    token: str, version: str, settings: Any, connection_id: uuid.UUID, waba_id: str | None
) -> None:
    """VERIFICACIÓN/FALLBACK, no gate: `waba_id` del popup (ya persistido
    por `persist_finish_payload`) es la fuente PRIMARIA -- esto solo es para
    detectar de antemano un token con permisos insuficientes y dejarlo
    registrado en `connection.health` para soporte. La forma exacta del
    JSON de `/debug_token` no está verificada contra doc primaria: parseo
    100% tolerante (`.get()` con default en cada campo), nunca levanta
    excepción."""
    global _debug_token_logged_once
    app_token = f"{settings.meta_app_id}|{settings.meta_app_secret}"
    try:
        data = await graph_fetch(
            "/debug_token", token=app_token, app_secret=settings.meta_app_secret,
            query={"input_token": token}, version=version, connection_id=connection_id,
        )
    except GraphError as exc:
        log.warning("embedded_signup_debug_token_fallo", detail=exc.message)
        return

    if not _debug_token_logged_once:
        # Solo la forma (claves), nunca valores: trae IDs de activos del negocio.
        payload_log = (data or {}).get("data") if isinstance((data or {}).get("data"), dict) else {}
        log.info("embedded_signup_debug_token_raw_shape", claves=sorted(payload_log.keys()))
        _debug_token_logged_once = True

    payload = (data or {}).get("data", {}) or {}
    flat_scopes = set(payload.get("scopes") or [])
    granular = payload.get("granular_scopes") or []
    granular_scope_names = {g.get("scope") for g in granular if isinstance(g, dict)}
    has_whatsapp_scope = bool((flat_scopes | granular_scope_names) & _WHATSAPP_MANAGEMENT_SCOPE_NAMES)

    target_ids: list[str] = []
    for g in granular:
        if isinstance(g, dict) and g.get("scope") in _WHATSAPP_MANAGEMENT_SCOPE_NAMES:
            target_ids.extend(g.get("target_ids") or [])

    if not has_whatsapp_scope:
        log.warning("embedded_signup_token_sin_scope_whatsapp", connection_id=str(connection_id))
    if waba_id and target_ids and waba_id not in target_ids:
        log.warning(
            "embedded_signup_token_target_ids_no_incluye_waba",
            connection_id=str(connection_id), waba_id=waba_id, target_ids_count=len(target_ids),
        )
    elif not target_ids:
        log.warning("embedded_signup_token_sin_target_ids", connection_id=str(connection_id))

    async with get_session() as session:
        connection = await session.get(MetaConnection, connection_id)
        if connection is not None:
            connection.health = {
                **(connection.health or {}),
                "token_scope_check": {
                    "has_whatsapp_scope": has_whatsapp_scope,
                    "target_ids_present": bool(target_ids),
                },
            }


async def _select_assets_step(wizard_id: uuid.UUID, tenant_id: uuid.UUID | None) -> ConnectionWizardView:
    """No-op idempotente: Meta ya hizo la selección dentro del popup. Este
    paso solo existe para el caso `FINISH_ONLY_WABA` (fase 6, sin número
    todavía) -- el admin carga el número más tarde y ese flujo re-dispara
    `discover_assets` para ese número."""
    async with get_session() as session:
        wizard = await get_owned(session, ConnectionWizard, wizard_id, tenant_id)
        _advance_to(wizard, "subscribe_webhook")
        await session.flush()
        return _to_view(wizard)


async def _record_graph_error(wizard_id: uuid.UUID, tenant_id: uuid.UUID | None, exc: GraphError, *, at_step: str) -> None:
    """Persiste code/error_subcode/fbtrace_id en `connection_wizards.
    last_error` -- criterio de aceptación explícito del ticket, para que
    soporte pueda diagnosticar sin pedirle logs al admin."""
    async with get_session() as session:
        wizard = await get_owned(session, ConnectionWizard, wizard_id, tenant_id)
        if wizard is None:
            return
        wizard.last_error = {
            "code": exc.code, "error_subcode": exc.error_subcode, "fbtrace_id": exc.fbtrace_id,
            "message": exc.message, "at_step": at_step,
        }


def _humanize_subscribed_apps_error(exc: GraphError) -> str:
    if exc.http_status == 403:
        return (
            "El token no tiene whatsapp_business_management sobre esta WABA, o no se completó "
            "el Embedded Signup -- hay que volver a conectar."
        )
    if exc.http_status == 404:
        return "WABA no encontrada."
    if exc.http_status == 422:
        from core.gateway.meta_subscriptions import humanize_subscription_error

        return humanize_subscription_error(exc)
    return f"Error de Meta: {exc.message}"


async def _subscribe_waba_to_app(
    token: str, waba_id: str, version: str, connection_id: uuid.UUID, app_secret: str, tenant_id: uuid.UUID | None = None,
    *, override_callback_uri: str | None = None, override_verify_token: str | None = None,
) -> None:
    """`POST /{waba_id}/subscribed_apps`: la suscripción de OBJETO -- distinta
    de `meta_subscriptions.upsert_app_subscription` (suscripción de APP,
    `POST /{app_id}/subscriptions`, PR #43). Sin esta, la WABA nunca manda
    eventos aunque la app esté bien configurada.

    REGLA DE DISEÑO INNEGOCIABLE (ticket): aunque se use `override_callback_
    uri` acá, el endpoint global de la app (`/webhooks/whatsapp`) NO se
    puede retirar -- los webhooks de plantillas (`message_template_*`) y de
    cuenta (`account_update`/`account_alerts`/`account_review_update`) están
    configurados a nivel de APP (`meta_subscriptions.OBJECT_FIELDS`) y
    SIEMPRE van al callback por defecto, nunca a un override -- un override
    de `subscribed_apps` es por-WABA y solo afecta el tráfico de MENSAJES de
    esa WABA puntual.

    Verificación OBLIGATORIA inmediata (GET) antes de dar el paso por
    completo -- sin ella, un `{"success": true}` del POST no garantiza nada."""
    if override_callback_uri and len(override_callback_uri) > 200:
        raise WizardError("override_callback_uri supera los 200 caracteres permitidos por Meta")

    body: dict[str, str] | None = None
    if override_callback_uri:
        body = {"override_callback_uri": override_callback_uri}
        if override_verify_token:
            body["verify_token"] = override_verify_token

    await graph_fetch(
        f"/{waba_id}/subscribed_apps", method="POST", token=token, app_secret=app_secret,
        body=body, version=version, connection_id=connection_id,
    )

    verification = await graph_fetch(
        f"/{waba_id}/subscribed_apps", token=token, app_secret=app_secret, version=version, connection_id=connection_id,
    )
    subscribed = bool((verification or {}).get("data"))

    # tenant_id explícito -- defensa en profundidad, mismo criterio ya
    # aplicado en persist_finish_payload/sync_phone_numbers (hallazgo
    # auditor-multitenant, 2026-09-22, severidad baja: connection_id acá
    # siempre viene de wizard.connection_id ya resuelto con tenant_id, pero
    # no cuesta nada repetir el filtro).
    async with get_session() as session:
        conditions = [WabaAccount.waba_id == waba_id, WabaAccount.connection_id == connection_id]
        if tenant_id is not None:
            conditions.append(WabaAccount.tenant_id == tenant_id)
        waba_row = (await session.execute(select(WabaAccount).where(*conditions))).scalar_one_or_none()
        if waba_row is not None:
            waba_row.subscribed = subscribed

    if not subscribed:
        raise WizardError(
            "Meta no confirmó la suscripción de esta WABA (GET /subscribed_apps devolvió vacío) "
            "-- sin esto no llega ningún webhook."
        )


async def _subscribe_webhook_step(wizard_id: uuid.UUID, tenant_id: uuid.UUID | None) -> ConnectionWizardView:
    """Suscribe la WABA del negocio a la app de la plataforma (`POST /{waba_id}/subscribed_apps`
    con el token del negocio). NO toca la suscripción de la APP de la plataforma (URL y
    `verify_token` del webhook): es compartida por todos los negocios y solo la registra el
    master ("Registrar webhook", ADR-0126 §5). Si falta o está caída, el diagnóstico del último
    paso lo dice (`app_subscription_active`) y la conexión no queda `connected`."""
    settings = get_settings()
    async with get_session() as session:
        wizard = await get_owned(session, ConnectionWizard, wizard_id, tenant_id)
        connection = await _load_connection(wizard, session)
        waba = await _waba_viva(session, wizard)

    version = connection.graph_api_version if connection else settings.graph_api_version
    if waba is not None:
        token = await _token_de(connection)
        if token and not waba.subscribed:
            try:
                await _subscribe_waba_to_app(token, waba.waba_id, version, wizard.connection_id, settings.meta_app_secret, tenant_id)
            except GraphError as exc:
                await _record_graph_error(wizard_id, tenant_id, exc, at_step="subscribe_webhook")
                raise WizardError(_humanize_subscribed_apps_error(exc), code="subscribe_failed") from exc

    async with get_session() as session:
        wizard = await get_owned(session, ConnectionWizard, wizard_id, tenant_id)
        _advance_to(wizard, "register_number")
        await session.flush()
        return _to_view(wizard)


async def _advance_past_register(wizard_id: uuid.UUID, tenant_id: uuid.UUID | None) -> ConnectionWizardView:
    async with get_session() as session:
        wizard = await get_owned(session, ConnectionWizard, wizard_id, tenant_id)
        _advance_to(wizard, "sync_templates")
        await session.flush()
        return _to_view(wizard)


async def _wizard_phone(
    wizard_id: uuid.UUID, tenant_id: uuid.UUID | None
) -> tuple[ConnectionWizard, MetaConnection | None, PhoneNumber | None]:
    async with get_session() as session:
        wizard = await get_owned(session, ConnectionWizard, wizard_id, tenant_id)
        if wizard is None:
            raise WizardError("El wizard no existe o no pertenece a este tenant", code="not_found", status=404)
        connection = await _load_connection(wizard, session)
        phone = await _numero_vivo(session, wizard)
    return wizard, connection, phone


async def _esperar_registro(wizard_id: uuid.UUID, tenant_id: uuid.UUID | None, message: str) -> None:
    """El registro del número no salió: el wizard queda en `register_number` con el motivo y
    la conexión `pending` con `MOTIVO_NUMERO_SIN_REGISTRAR`, como una BYOK con el número sin
    registrar (ADR-0127): registrarlo después, por aquí o por Números, la deja `connected`."""
    from core.gateway.meta_connection_state import MOTIVO_NUMERO_SIN_REGISTRAR

    async with get_session() as session:
        wizard = await get_owned(session, ConnectionWizard, wizard_id, tenant_id)
        if wizard is None:
            return
        wizard.last_error = {"code": "register_failed", "message": message[:300], "at_step": "register_number"}
        connection = await get_live_meta_connection(session, wizard.connection_id, tenant_id, for_update=True)
        if connection is not None and connection.status in ("pending", "draft"):
            connection.status = "pending"
            connection.status_reason = MOTIVO_NUMERO_SIN_REGISTRAR


def _valid_pin(pin: str | None) -> str | None:
    pin = (pin or "").strip()
    # `isascii`: `isdigit` acepta dígitos Unicode que Meta no.
    return pin if len(pin) == 6 and pin.isascii() and pin.isdigit() else None


async def register_number_step(wizard_id: uuid.UUID, pin: str | None = None) -> ConnectionWizardView:
    """Registra el número en la Cloud API con el PIN de verificación en dos pasos que eligió
    (o que ya tenía) el negocio. Usa el mismo servicio que "Números" (`register_number`):
    credenciales de la conexión, PIN cifrado antes de llamar a Meta y restaurado si Meta lo
    rechaza, un intento por minuto y la conexión `connected` cuando era lo único que faltaba.

    Antes el PIN lo generaba el sistema y nadie lo veía: el número real del negocio quedaba
    con una verificación en dos pasos que el negocio no conocía (mismo motivo que ADR-0127 para
    BYOK de producción). Ahora es obligatorio, salvo que ya haya uno guardado.

    Si `coexistence=true` (número compartido con la app de WhatsApp Business), el paso se
    saltea: Meta no permite registrarlo."""
    from core.admin.meta_numbers_service import register_number

    tenant_id = get_current_tenant_id()
    wizard, connection, phone = await _wizard_phone(wizard_id, tenant_id)
    if wizard.current_step != "register_number":
        raise WizardError("El alta no está esperando el registro del número.", code="invalid_step", status=409)

    if connection is not None and (connection.health or {}).get("coexistence"):
        async with get_session() as session:
            wizard = await get_owned(session, ConnectionWizard, wizard_id, tenant_id)
            _advance_to(wizard, "sync_templates")
            await session.flush()
            return _to_view(wizard)

    if phone is None:
        raise WizardError(
            "Meta no devolvió un número para esta conexión: agrégalo en Números y regístralo ahí.",
            code="no_phone_number",
        )

    if pin is not None and _valid_pin(pin) is None:
        raise WizardError("El PIN tiene que ser de 6 dígitos.", code="invalid_pin")
    guardado = await meta_token_store.get_token(
        wizard.connection_id, PIN_KIND, phone.phone_number_id, tenant_id=tenant_id
    )
    if pin is None and guardado is None:
        await _esperar_registro(wizard_id, tenant_id, "Falta el PIN de 6 dígitos del número.")
        raise WizardError(
            "Escribe el PIN de 6 dígitos del número: será su verificación en dos pasos (si ya "
            "tiene una, escribe ese PIN).",
            code="pin_required",
        )

    error = await register_number(tenant_id, str(wizard.connection_id), phone.phone_number_id, pin=pin)
    if error is not None:
        await _esperar_registro(wizard_id, tenant_id, error)
        raise WizardError(error, code="register_failed")

    async with get_session() as session:
        wizard = await get_owned(session, ConnectionWizard, wizard_id, tenant_id)
        wizard.last_error = None
        _advance_to(wizard, "sync_templates")
        await session.flush()
        return _to_view(wizard)


async def set_new_pin_step(wizard_id: uuid.UUID, pin: str | None = None) -> ConnectionWizardView:
    """Remediación de un `133005` (el número ya tiene verificación en dos pasos con otro
    PIN que nadie recuerda): Meta no tiene endpoint para desactivarla, pero con el token se
    puede fijar un PIN nuevo con `POST /{phone_number_id}` (no `/register`). Después se
    registra el número con ese PIN: fijarlo no lo registra.

    El PIN nuevo lo elige el negocio (6 dígitos)."""
    tenant_id = get_current_tenant_id()
    settings = get_settings()
    nuevo = _valid_pin(pin)
    if nuevo is None:
        raise WizardError("El PIN nuevo tiene que ser de 6 dígitos.", code="invalid_pin")

    wizard, connection, phone = await _wizard_phone(wizard_id, tenant_id)
    if wizard.current_step != "register_number":
        raise WizardError("El alta no está esperando el registro del número.", code="invalid_step", status=409)
    if phone is None:
        raise WizardError("Todavía no hay un número asociado a esta conexión", code="no_phone_number")

    token = await _token_de(connection)
    if not token:
        raise WizardError("No hay token guardado todavía para esta conexión", code="missing_token")

    from core.memory.working import get_redis

    # Uno por minuto y número: cambiar el PIN en Meta repetido puede terminar en un bloqueo.
    if not await get_redis().set(f"set_pin:{wizard.tenant_id}:{phone.phone_number_id}", "1", nx=True, ex=60):
        raise WizardError("Espera un minuto antes de volver a cambiar el PIN.", code="too_many_attempts")

    try:
        await graph_fetch(
            f"/{phone.phone_number_id}", method="POST", token=token, app_secret=settings.meta_app_secret,
            body={"pin": nuevo}, version=connection.graph_api_version if connection else settings.graph_api_version,
            connection_id=wizard.connection_id,
        )
    except GraphError as exc:
        await _record_graph_error(wizard_id, tenant_id, exc, at_step="register_number")
        raise WizardError(f"Meta no aceptó el PIN nuevo: {exc.message}"[:200], code="set_pin_failed") from exc
    from core.admin.meta_numbers_service import release_register_rate_limit

    # Meta ya tiene el PIN nuevo: se guarda, y registrar con él no cuenta como otro intento.
    await meta_token_store.put_token(wizard.connection_id, PIN_KIND, phone.phone_number_id, nuevo, tenant_id=tenant_id)
    await release_register_rate_limit(wizard.tenant_id, phone.phone_number_id)
    return await register_number_step(wizard_id, nuevo)


async def _sync_templates_step(wizard_id: uuid.UUID, tenant_id: uuid.UUID | None) -> ConnectionWizardView:
    settings = get_settings()
    async with get_session() as session:
        wizard = await get_owned(session, ConnectionWizard, wizard_id, tenant_id)
        connection = await _load_connection(wizard, session)
        waba = await _waba_viva(session, wizard)

    if waba is not None:
        token = await _token_de(connection)
        if token:
            try:
                data = await graph_fetch(
                    f"/{waba.waba_id}/message_templates", token=token, app_secret=settings.meta_app_secret,
                    query={"fields": "name,language,category,status,rejected_reason,quality_score,components"},
                    version=connection.graph_api_version if connection else settings.graph_api_version,
                    connection_id=wizard.connection_id,
                )
                for item in (data or {}).get("data", []):
                    await _upsert_template(waba.id, tenant_id, item)
            except GraphError as exc:
                log.warning("embedded_signup_sync_templates_fallo", detail=exc.message)

    async with get_session() as session:
        wizard = await get_owned(session, ConnectionWizard, wizard_id, tenant_id)
        _advance_to(wizard, "verify_health")
        await session.flush()
        return _to_view(wizard)


async def _upsert_template(waba_account_id: uuid.UUID, tenant_id: uuid.UUID | None, item: dict[str, Any]) -> None:
    async with get_session() as session:
        existing = (
            await session.execute(
                select(MessageTemplate).where(
                    MessageTemplate.waba_account_id == waba_account_id,
                    MessageTemplate.tenant_id == tenant_id,
                    MessageTemplate.name == item.get("name", ""),
                    MessageTemplate.language == item.get("language", ""),
                )
            )
        ).scalar_one_or_none()
        if existing is None:
            session.add(
                MessageTemplate(
                    tenant_id=tenant_id,
                    waba_account_id=waba_account_id,
                    meta_template_id=item.get("id"),
                    name=item.get("name", ""),
                    language=item.get("language", ""),
                    category=item.get("category"),
                    status=item.get("status", "DRAFT"),
                    rejected_reason=item.get("rejected_reason"),
                    quality_score=str(item.get("quality_score")) if item.get("quality_score") else None,
                    components=item.get("components"),
                    synced_at=utcnow(),
                )
            )
        else:
            existing.status = item.get("status", existing.status)
            existing.rejected_reason = item.get("rejected_reason")
            existing.components = item.get("components")
            existing.synced_at = utcnow()


async def _verify_health_step(wizard_id: uuid.UUID, tenant_id: uuid.UUID | None) -> ConnectionWizardView:
    """Último paso: el diagnóstico de siempre (`diagnose_connection`, con las credenciales de
    la conexión). Decide el estado como en "Diagnosticar" (ADR-0102): de `pending` solo pasa a
    `connected` con el token verificado contra Meta y, en Embedded Signup, con la suscripción de
    la app de la plataforma confirmada (`next_status`). Si no, el wizard termina igual y la
    conexión queda con su estado y motivo: el hub lo muestra y "Diagnosticar" lo retoma."""
    from core.admin.meta_health_service import diagnose_connection
    from core.tasks.meta_subscriptions_health import refrescar_app_de_la_plataforma

    async with get_session() as session:
        wizard = await get_owned(session, ConnectionWizard, wizard_id, tenant_id)
        connection_id = wizard.connection_id

    # La suscripción de la app de la plataforma se lee en el acto (solo lectura): el
    # diagnóstico la usa y, en Embedded Signup, sin ella confirmada la conexión no mejora.
    await refrescar_app_de_la_plataforma(tenant_id=tenant_id, connection_id=connection_id)

    error = await diagnose_connection(tenant_id, str(connection_id)) if tenant_id is not None else "Sin negocio"
    if error is not None:
        raise WizardError(error, code="health_failed")

    async with get_session() as session:
        wizard = await get_owned(session, ConnectionWizard, wizard_id, tenant_id)
        connection = await get_live_meta_connection(session, wizard.connection_id, tenant_id)
        if connection is not None and connection.status == "connected" and connection.connected_at is None:
            connection.connected_at = utcnow()
        wizard.last_error = None
        _advance_to(wizard, "done")
        await session.flush()
        return _to_view(wizard)


# Tope de la cadena dentro de un request (como "Activar", ADR-0122 §4). Cada llamada a Graph
# tiene su timeout; pasado este tope no se arranca otro paso y el panel lo retoma.
SIGNUP_BUDGET_S = 45.0


async def complete_signup(wizard_id: uuid.UUID, *, pin: str | None = None) -> ConnectionWizardView:
    """Encadena lo que sigue al canje (HU-SH-05): datos de la WABA y del número, suscribir la
    WABA a la app de la plataforma, registrar el número con el PIN, sincronizar plantillas y
    verificar salud. Retoma desde el paso guardado: cada paso es idempotente.

    No lanza por un paso que falla: lo deja en `last_error` (con `at_step`) y devuelve el
    estado, para que el panel muestre dónde quedó y ofrezca reintentar o pedir el PIN.
    Solo propaga que el wizard no exista."""
    tenant_id = get_current_tenant_id()
    deadline = time.monotonic() + SIGNUP_BUDGET_S
    try:
        view = await run_automatic_steps(wizard_id, deadline=deadline)
        if view.current_step == "register_number":
            await register_number_step(wizard_id, pin)
            view = await run_automatic_steps(wizard_id, deadline=deadline)
    except WizardError as exc:
        if exc.code == "not_found":
            raise
        log.info("embedded_signup_paso_pendiente", wizard_id=str(wizard_id), code=exc.code)
        await _record_step_error(wizard_id, tenant_id, exc.code, exc.message)
        view = _to_view(await _get_wizard(wizard_id, tenant_id))
    except GraphError as exc:
        log.warning("embedded_signup_paso_fallo_meta", wizard_id=str(wizard_id), code=exc.code)
        async with get_session() as session:
            wizard = await get_owned(session, ConnectionWizard, wizard_id, tenant_id)
            at_step = wizard.current_step if wizard is not None else None
        await _record_graph_error(wizard_id, tenant_id, exc, at_step=at_step or "unknown")
        view = _to_view(await _get_wizard(wizard_id, tenant_id))
    return view


async def _record_step_error(wizard_id: uuid.UUID, tenant_id: uuid.UUID | None, code: str, message: str) -> None:
    async with get_session() as session:
        wizard = await get_owned(session, ConnectionWizard, wizard_id, tenant_id)
        if wizard is None:
            return
        previo = wizard.last_error or {}
        # Un error de Meta ya guardado en este paso (code/fbtrace_id) se conserva.
        if previo.get("at_step") == wizard.current_step and previo.get("fbtrace_id"):
            wizard.last_error = {**previo, "message": message[:300], "reason": code}
        else:
            wizard.last_error = {"code": code, "message": message[:300], "at_step": wizard.current_step}
