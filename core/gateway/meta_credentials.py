"""Resolver único de credenciales de Meta por conexión (HU-CONN-01, ADR-0094).

Toda llamada a Graph API que actúa sobre UNA conexión (números, plantillas, salud,
precios, créditos, envío, descarga de media...) usa las credenciales que decide este
módulo en vez de buscar "el" token del negocio o caer al App Secret de la plataforma.
Las decide `MetaConnection.origin`: el **modo de credenciales vigente** de la conexión.
Lo fija cada alta y cambia solo cuando otra alta guarda sus credenciales sobre la misma
conexión (el wizard de Embedded Signup que reutiliza una conexión BYOK, o BYOK que
reconecta la misma app). Nunca se adivina mirando qué tokens existen:

| origin            | access token                                | App ID / App Secret            |
|-------------------|---------------------------------------------|--------------------------------|
| `byok`            | de la conexión (`cloud_api_access_token`)   | los del negocio (`app_access`) |
| `embedded_signup` | de la conexión (`business_integration_...`) | los de la plataforma (config)  |
| `manual`          | de la conexión (`business_integration_...`) | los de la plataforma (config)  |

El `appsecret_proof` de cada llamada se calcula con el secreto de ESA conexión. El
token y el secreto se leen de `meta_token_store` (única vía a `meta_private`), con el
`tenant_id` de la propia conexión (el AAD de AES-GCM, ADR-0027, ata cada fila a
`tenant:conexión:kind`). `credentials_for` además exige un `tenant_scope` activo y que sea
el de la conexión.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Final, Literal

from sqlalchemy import select

from core.config import get_settings
from core.db.models import MetaConnection, WabaAccount
from core.db.session import get_session
from core.gateway import meta_token_store
from core.gateway.meta_byok import (
    APP_SECRET_KIND,
    CLOUD_API_ACCESS_TOKEN_KIND,
    WEBHOOK_VERIFY_TOKEN_KIND,
)
from core.gateway.meta_onboarding import TOKEN_KIND
from core.shared.logger import get_logger
from core.shared.tenant import get_current_tenant_id

log = get_logger(__name__)

Origin = Literal["byok", "embedded_signup", "manual"]
ORIGINS: Final[tuple[str, ...]] = ("byok", "embedded_signup", "manual")
ORIGIN_LABELS: Final[dict[str, str]] = {
    "byok": "BYOK (app propia)",
    "embedded_signup": "Embedded Signup",
    "manual": "Carga manual",
}


class CredentialsUnavailable(Exception):
    """La conexión no tiene (o no puede usar) credenciales válidas.

    `reason` es un slug estable para métricas y tests; `message` es legible para el
    panel y NUNCA incluye tokens ni secretos.
    """

    def __init__(self, message: str, *, reason: str, connection_id: uuid.UUID | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.reason = reason
        self.connection_id = connection_id


@dataclass(frozen=True, slots=True)
class MetaCredentials:
    """Credenciales efectivas de una conexión. `repr` oculta token y secreto."""

    connection_id: uuid.UUID
    tenant_id: uuid.UUID
    origin: Origin
    app_id: str
    graph_api_version: str
    access_token: str = field(repr=False)
    app_secret: str = field(repr=False)
    waba_id: str | None = None
    business_id: str | None = None


def _unavailable(connection: MetaConnection, reason: str, message: str) -> CredentialsUnavailable:
    log.warning(
        "meta_credentials_unavailable",
        connection_id=str(connection.id),
        tenant_id=str(connection.tenant_id),
        origin=connection.origin,
        reason=reason,
    )
    return CredentialsUnavailable(message, reason=reason, connection_id=connection.id)


async def _waba_id_of(connection: MetaConnection) -> str | None:
    async with get_session() as session:
        return (
            await session.execute(
                select(WabaAccount.waba_id)
                .where(WabaAccount.connection_id == connection.id, WabaAccount.tenant_id == connection.tenant_id)
                .order_by(WabaAccount.created_at)
                .limit(1)
            )
        ).scalar_one_or_none()


def uses_platform_app(connection: MetaConnection) -> bool:
    """¿La conexión usa la app de Meta de la plataforma? Embedded Signup y la carga manual,
    sí (ADR-0094). Decide quién puede tocar la suscripción de webhook de su app: la de la
    plataforma la comparten todos los negocios y solo la toca el master (HU-CONN-05).

    Excepción deliberada a ADR-0094 §1 (ADR-0126): también una "BYOK" cuyo `meta_app_id` es
    el de la plataforma. El wizard BYOK no lo impide, y sin esta guarda su dueño movería el
    callback de la app compartida a su slug y cortaría el webhook genérico a todos."""
    if connection.origin != "byok":
        return True
    platform_app_id = get_settings().meta_app_id
    return bool(platform_app_id) and connection.meta_app_id == platform_app_id


async def stored_app_secret(connection: MetaConnection) -> str | None:
    """App Secret guardado de la app propia de la conexión (`app_access`, `subject_id =
    meta_app_id`), **sea cual sea su origen**, o `None`.

    Es la única regla de dónde vive ese secreto. No es `credentials_for`: ese decide con qué
    app se LLAMA a Graph según el origen vigente, y para una BYOK tomada por Embedded Signup
    da el de la plataforma. El webhook necesita saber con qué secretos puede VENIR firmada
    una conexión, y esa conserva el de su app vieja (ADR-0101, ADR-0126)."""
    return await meta_token_store.get_token(
        connection.id, APP_SECRET_KIND, connection.meta_app_id, tenant_id=connection.tenant_id
    )


async def stored_verify_token(connection: MetaConnection) -> str | None:
    """`verify_token` propio guardado por el wizard BYOK para la conexión, o `None`."""
    return await meta_token_store.get_token(
        connection.id, WEBHOOK_VERIFY_TOKEN_KIND, connection.meta_app_id, tenant_id=connection.tenant_id
    )


@dataclass(frozen=True, slots=True)
class AppCredentialsOf:
    """App de Meta que usa una conexión (sin token de acceso). `repr` oculta el secreto."""

    app_id: str
    app_secret: str = field(repr=False)
    uses_platform_app: bool = False


async def app_credentials_for(connection: MetaConnection) -> AppCredentialsOf:
    """App ID y App Secret de la app que usa la conexión según su origen (ADR-0094), sin
    exigir token de acceso: lo que hace falta para la suscripción de la app. La de la
    plataforma, de la configuración; la BYOK, su secreto guardado (nunca cae a la de la
    plataforma). `CredentialsUnavailable` si falta."""
    if uses_platform_app(connection):
        settings = get_settings()
        if not settings.meta_app_id or not settings.meta_app_secret:
            raise _unavailable(
                connection, "platform_app_not_configured", "La app de Meta de la plataforma no está configurada"
            )
        return AppCredentialsOf(settings.meta_app_id, settings.meta_app_secret, uses_platform_app=True)
    app_secret = await stored_app_secret(connection)
    if not app_secret:
        raise _unavailable(connection, "missing_app_secret", "No hay App Secret guardado para esta conexión")
    return AppCredentialsOf(connection.meta_app_id, app_secret)


def origin_label(origin: str) -> str:
    """Texto del origen para el panel (columna "Origen" de la lista de conexiones)."""
    return ORIGIN_LABELS.get(origin, origin)


def access_token_refs(connection: MetaConnection) -> list[tuple[str, str]]:
    """`(kind, subject_id)` del token de acceso que usa la conexión, según su `origin`.

    Es la ÚNICA regla de qué token usa una conexión: la usan el resolver, el sender y
    los avisos de vencimiento, y ninguno debe deducir el `kind` por su cuenta (una
    conexión que pasó de BYOK a Embedded Signup conserva los dos tokens).

    - `byok`: `cloud_api_access_token` con `subject_id = meta_app_id`.
    - `embedded_signup` y `manual`: el token del system user, con `subject_id =
      business_id` si ya se conocía, o con el id de la conexión si el `business_id`
      llegó después del canje; se prueban los dos, en ese orden.
    """
    if connection.origin == "byok":
        return [(CLOUD_API_ACCESS_TOKEN_KIND, connection.meta_app_id)]
    if connection.origin in ("embedded_signup", "manual"):
        subjects = [s for s in (connection.business_id, str(connection.id)) if s]
        return [(TOKEN_KIND, subject) for subject in dict.fromkeys(subjects)]
    return []


async def access_token_for(connection: MetaConnection) -> str | None:
    """Solo el token de acceso, sin App Secret ni app de la plataforma.

    Para el sender, que no firma con `appsecret_proof`: exigirle el secreto rompería
    envíos que no lo necesitan."""
    for kind, subject in access_token_refs(connection):
        token = await meta_token_store.get_token(connection.id, kind, subject, tenant_id=connection.tenant_id)
        if token:
            return token
    return None


async def access_token_expires_at(connection: MetaConnection) -> datetime | None:
    """Vencimiento del token que la conexión usa de verdad (sin descifrarlo)."""
    for kind, subject in access_token_refs(connection):
        expires_at = await meta_token_store.get_token_expires_at(
            connection.id, kind, subject, tenant_id=connection.tenant_id
        )
        if expires_at is not None:
            return expires_at
    return None


async def credentials_for(connection: MetaConnection, *, waba_id: str | None = None) -> MetaCredentials:
    """Credenciales de `connection` según su `origin`, o `CredentialsUnavailable`.

    Exige un `tenant_scope` activo y que sea el de la conexión: sin él falla cerrado,
    en vez de confiar solo en RLS y en el AAD del token store.
    `waba_id` es opcional: si el llamador ya lo tiene, se evita la consulta.
    """
    if connection.deleted_at is not None:
        # Invariante nº8: una conexión eliminada no presta tokens.
        raise _unavailable(connection, "connection_deleted", "La conexión fue eliminada")

    current = get_current_tenant_id()
    if current is None:
        raise _unavailable(connection, "no_tenant_scope", "No hay un negocio activo para resolver las credenciales")
    if current != connection.tenant_id:
        raise _unavailable(connection, "tenant_mismatch", "La conexión no pertenece al negocio actual")

    origin = connection.origin
    if origin == "byok":
        app_id = connection.meta_app_id
        app_secret = await stored_app_secret(connection)
    elif origin in ("embedded_signup", "manual"):
        settings = get_settings()
        if not settings.meta_app_id or not settings.meta_app_secret:
            raise _unavailable(
                connection, "platform_app_not_configured", "La app de Meta de la plataforma no está configurada"
            )
        app_id, app_secret = settings.meta_app_id, settings.meta_app_secret
    else:
        raise _unavailable(connection, "origin_invalid", "El origen de la conexión no es válido")

    token = await access_token_for(connection)
    if not token:
        raise _unavailable(connection, "missing_access_token", "No hay token de acceso guardado para esta conexión")
    if not app_secret:
        raise _unavailable(connection, "missing_app_secret", "No hay App Secret guardado para esta conexión")

    return MetaCredentials(
        connection_id=connection.id,
        tenant_id=connection.tenant_id,
        origin=origin,
        app_id=app_id,
        graph_api_version=connection.graph_api_version,
        access_token=token,
        app_secret=app_secret,
        waba_id=waba_id if waba_id is not None else await _waba_id_of(connection),
        business_id=connection.business_id,
    )
