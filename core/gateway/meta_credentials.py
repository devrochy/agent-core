"""Resolver único de credenciales de Meta por conexión (HU-CONN-01, ADR-0094).

Toda llamada a Graph API que actúa sobre UNA conexión (números, plantillas, salud,
precios, créditos, descarga de media...) recibe un `MetaCredentials` en vez de
buscar "el" token del negocio o caer al App Secret de la plataforma. De dónde salen
las credenciales lo decide `MetaConnection.origin`, que se guarda al crear la
conexión (nunca se adivina mirando qué tokens existen):

| origin            | access token                                | App ID / App Secret            |
|-------------------|---------------------------------------------|--------------------------------|
| `byok`            | de la conexión (`cloud_api_access_token`)   | los del negocio (`app_access`) |
| `embedded_signup` | de la conexión (`business_integration_...`) | los de la plataforma (config)  |
| `manual`          | de la conexión (`business_integration_...`) | los de la plataforma (config)  |

El `appsecret_proof` de cada llamada se calcula con el secreto de ESA conexión. El
token y el secreto se leen de `meta_token_store` (única vía a `meta_private`), con
el `tenant_id` de la propia conexión: el AAD de AES-GCM ata cada fila a su tenant, así
que un token de otro negocio nunca se descifra, y RLS ya no lo devuelve.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Final, Literal

from sqlalchemy import select

from core.config import get_settings
from core.db.models import MetaConnection, WabaAccount
from core.db.session import get_session
from core.gateway import meta_token_store
from core.gateway.meta_byok import APP_SECRET_KIND, CLOUD_API_ACCESS_TOKEN_KIND
from core.gateway.meta_onboarding import TOKEN_KIND
from core.shared.logger import get_logger
from core.shared.tenant import get_current_tenant_id

log = get_logger(__name__)

Origin = Literal["byok", "embedded_signup", "manual"]
ORIGINS: Final[tuple[str, ...]] = ("byok", "embedded_signup", "manual")


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


async def _system_user_token(connection: MetaConnection) -> str | None:
    """Token del system user de la plataforma (Embedded Signup y carga manual).

    Se guarda con `subject_id = business_id` si ya se conocía, o con el id de la
    conexión si el `business_id` llegó después; se prueban los dos."""
    subjects = [s for s in (connection.business_id, str(connection.id)) if s]
    for subject in dict.fromkeys(subjects):
        token = await meta_token_store.get_token(
            connection.id, TOKEN_KIND, subject, tenant_id=connection.tenant_id
        )
        if token:
            return token
    return None


async def credentials_for(connection: MetaConnection, *, waba_id: str | None = None) -> MetaCredentials:
    """Credenciales de `connection` según su `origin`, o `CredentialsUnavailable`.

    `waba_id` es opcional: si el llamador ya lo tiene, se evita la consulta.
    """
    if connection.deleted_at is not None:
        # Invariante nº8: una conexión eliminada no presta tokens.
        raise _unavailable(connection, "connection_deleted", "La conexión fue eliminada")

    current = get_current_tenant_id()
    if current is not None and current != connection.tenant_id:
        # Defensa en profundidad además de RLS y del AAD del token store.
        raise _unavailable(connection, "tenant_mismatch", "La conexión no pertenece al negocio actual")

    origin = connection.origin
    if origin == "byok":
        app_id = connection.meta_app_id
        token = await meta_token_store.get_token(
            connection.id, CLOUD_API_ACCESS_TOKEN_KIND, connection.meta_app_id, tenant_id=connection.tenant_id
        )
        app_secret = await meta_token_store.get_token(
            connection.id, APP_SECRET_KIND, connection.meta_app_id, tenant_id=connection.tenant_id
        )
        if not token:
            raise _unavailable(connection, "missing_access_token", "No hay token de acceso guardado para esta conexión")
        if not app_secret:
            raise _unavailable(connection, "missing_app_secret", "No hay App Secret guardado para esta conexión")
    elif origin in ("embedded_signup", "manual"):
        settings = get_settings()
        if not settings.meta_app_id or not settings.meta_app_secret:
            raise _unavailable(
                connection, "platform_app_not_configured", "La app de Meta de la plataforma no está configurada"
            )
        app_id, app_secret = settings.meta_app_id, settings.meta_app_secret
        token = await _system_user_token(connection)
        if not token:
            raise _unavailable(connection, "missing_access_token", "No hay token de acceso guardado para esta conexión")
    else:
        raise _unavailable(connection, "origin_invalid", "El origen de la conexión no es válido")

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
