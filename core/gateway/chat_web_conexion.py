"""El chat web como conexión `webchat` (ADR-0115 §4, HU-AOS-11 parte 2).

El widget de un negocio es una fila de `channel_connections` de tipo `webchat`:

- `external_id` es la **clave pública** del widget: aleatoria (`nueva_clave`, 192 bits), generada
  por el servidor, nunca derivada del negocio ni elegida por él. El índice `uq_cc_webchat_key`
  (0071) es global e incluye las borradas: el endpoint público resuelve sin negocio
  (`webchat_resolve`, 0086).
- `active` es "habilitado"; los dominios permitidos van en `settings` (`{"allowed_origins": [...]}`)
  y el secreto de Turnstile, cifrado, en `credentials_encrypted` (`{"turnstile_secret": ...}`).
- Sin cuenta de Meta (`meta_connection_id` NULL), nunca primaria y fuera de las conexiones
  manuales (`connections.ES_MANUAL`): se administra desde Configuración → Chat web.

**Expand con doble escritura.** Hasta el *contract*, `app_settings.web_chat_config` sigue siendo la
fuente de verdad y se escribe igual que antes (con el secreto incluido), así una vuelta atrás del
código sigue sirviendo el widget. Cada guardado sincroniza la conexión desde ese JSON
(`sincronizar`), y el backfill (`conexion_canonica_backfill`) hace lo mismo con los negocios que
lo configuraron antes del despliegue. El endpoint público lee la conexión si existe y si no cae a
`app_settings` (`web_chat_resolve`).

Todo corre dentro del `tenant_scope` del negocio (RLS) y además filtra por `tenant_id`.
"""

from __future__ import annotations

import secrets
import uuid
from dataclasses import dataclass, field
from typing import Any, Literal

from sqlalchemy import select

from core.db.models import ChannelConnection, utcnow
from core.gateway.connection_types import CATEGORY_COMMUNICATION, WEBCHAT
from core.shared.crypto import get_secret_box
from core.shared.logger import get_logger

log = get_logger(__name__)

PROVEEDOR = "web"
CANAL = "web"
ETIQUETA = "Chat web"

Resultado = Literal["creada", "sincronizada", "ya_estaba", "sin_widget"]

def nueva_clave() -> str:
    """Clave pública del widget: 24 bytes aleatorios en base64url (192 bits)."""
    return secrets.token_urlsafe(24)


@dataclass(frozen=True)
class ConfigWidget:
    """La configuración del widget, con la misma forma que `app_settings.web_chat_config`."""

    enabled: bool
    key: str
    allowed_origins: list[str] = field(default_factory=list)
    turnstile_secret: str = ""
    connection_id: uuid.UUID | None = None

    def como_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "key": self.key,
            "allowed_origins": list(self.allowed_origins),
            "turnstile_secret": self.turnstile_secret,
        }


def _origenes(valor: Any) -> list[str]:
    return [str(o) for o in (valor or []) if o] if isinstance(valor, list) else []


def desde_app_settings(config: dict[str, Any] | None) -> ConfigWidget | None:
    """`app_settings.web_chat_config` → `ConfigWidget`, o None si el negocio nunca guardó el widget."""
    config = config if isinstance(config, dict) else {}
    key = str(config.get("key", "") or "")
    if not key:
        return None
    return ConfigWidget(
        enabled=bool(config.get("enabled")),
        key=key,
        allowed_origins=_origenes(config.get("allowed_origins")),
        turnstile_secret=str(config.get("turnstile_secret", "") or "").strip(),
    )


def _secreto(conexion: ChannelConnection) -> str:
    if not conexion.credentials_encrypted:
        return ""
    datos = get_secret_box().decrypt_json(conexion.credentials_encrypted)
    return str(datos.get("turnstile_secret", "") or "")


async def conexion_del_negocio(session: Any, tenant_id: uuid.UUID) -> ChannelConnection | None:
    """La conexión `webchat` viva del negocio (la más antigua: hoy hay a lo sumo una)."""
    return (
        await session.execute(
            select(ChannelConnection)
            .where(
                ChannelConnection.tenant_id == tenant_id,
                ChannelConnection.type_id == WEBCHAT,
                ChannelConnection.deleted_at.is_(None),
            )
            .order_by(ChannelConnection.created_at)
            .limit(1)
        )
    ).scalars().first()


def config_de_conexion(conexion: ChannelConnection) -> ConfigWidget:
    ajustes = conexion.settings if isinstance(conexion.settings, dict) else {}
    return ConfigWidget(
        enabled=bool(conexion.active),
        key=conexion.external_id,
        allowed_origins=_origenes(ajustes.get("allowed_origins")),
        turnstile_secret=_secreto(conexion),
        connection_id=conexion.id,
    )


async def leer(session: Any, tenant_id: uuid.UUID, connection_id: uuid.UUID) -> ConfigWidget | None:
    """La configuración de la conexión `webchat` `connection_id` del negocio, o None."""
    conexion = (
        await session.execute(
            select(ChannelConnection).where(
                ChannelConnection.tenant_id == tenant_id,
                ChannelConnection.id == connection_id,
                ChannelConnection.type_id == WEBCHAT,
                ChannelConnection.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    return None if conexion is None else config_de_conexion(conexion)


async def secreto_turnstile(session: Any, tenant_id: uuid.UUID, connection_id: uuid.UUID) -> str:
    """El secreto de Turnstile de la conexión `webchat` viva (vacío si no tiene). Solo lo pide el
    POST del visitante: el resolvedor público dice si existe, nunca su valor."""
    widget = await leer(session, tenant_id, connection_id)
    return widget.turnstile_secret if widget is not None else ""


async def tiene_alguna(session: Any, tenant_id: uuid.UUID) -> bool:
    """¿El negocio tuvo alguna vez una conexión `webchat` (viva o borrada)? Si la tuvo, el
    endpoint público no cae a `app_settings`: un widget borrado no revive por la configuración
    vieja."""
    return (
        await session.execute(
            select(ChannelConnection.id)
            .where(ChannelConnection.tenant_id == tenant_id, ChannelConnection.type_id == WEBCHAT)
            .limit(1)
        )
    ).first() is not None


def _cifrar(secreto: str) -> str | None:
    return get_secret_box().encrypt_json({"turnstile_secret": secreto}) if secreto else None


async def sincronizar(
    session: Any, tenant_id: uuid.UUID, config: ConfigWidget | None, *, aplicar: bool = True
) -> tuple[Resultado, uuid.UUID | None]:
    """Crea la conexión `webchat` del negocio desde la configuración del widget, o la pone al día
    (clave rotada, habilitado, dominios, secreto). Va en la sesión del llamador (el guardado de
    Configuración o el lote del backfill). Con `aplicar=False` solo dice qué haría.

    Devuelve `(resultado, connection_id)`; `connection_id` es None en dry-run si se crearía."""
    if config is None:
        return "sin_widget", None
    actual = await conexion_del_negocio(session, tenant_id)
    if actual is None:
        if not aplicar:
            return "creada", None
        conexion = ChannelConnection(
            tenant_id=tenant_id,
            channel=CANAL,
            provider=PROVEEDOR,
            type_id=WEBCHAT,
            category=CATEGORY_COMMUNICATION,
            label=ETIQUETA,
            active=config.enabled,
            is_primary=False,
            external_id=config.key,
            credentials_encrypted=_cifrar(config.turnstile_secret),
            settings={"allowed_origins": list(config.allowed_origins)},
        )
        session.add(conexion)
        await session.flush()
        log.info("conexion_webchat_creada", tenant_id=str(tenant_id), connection_id=str(conexion.id))
        return "creada", conexion.id

    try:
        previa = config_de_conexion(actual)
    except Exception:  # el secreto guardado no se descifra (rotación de la clave maestra)
        # `app_settings` manda: se vuelve a cifrar desde ahí, sin tumbar el guardado ni el backfill.
        log.warning("conexion_webchat_secreto_ilegible", tenant_id=str(tenant_id), connection_id=str(actual.id))
        previa = ConfigWidget(enabled=bool(actual.active), key=actual.external_id, turnstile_secret="\0ilegible")
    if (previa.enabled, previa.key, previa.allowed_origins, previa.turnstile_secret) == (
        config.enabled,
        config.key,
        list(config.allowed_origins),
        config.turnstile_secret,
    ):
        return "ya_estaba", actual.id
    if aplicar:
        actual.active = config.enabled
        actual.external_id = config.key
        if previa.turnstile_secret != config.turnstile_secret:
            actual.credentials_encrypted = _cifrar(config.turnstile_secret)
        # Se fusiona: `settings` es de la conexión y otras HU pueden guardar ahí sus claves.
        actual.settings = dict(actual.settings or {}) | {"allowed_origins": list(config.allowed_origins)}
        actual.updated_at = utcnow()
        await session.flush()
    return "sincronizada", actual.id


__all__ = [
    "CANAL",
    "PROVEEDOR",
    "ConfigWidget",
    "conexion_del_negocio",
    "config_de_conexion",
    "desde_app_settings",
    "leer",
    "nueva_clave",
    "secreto_turnstile",
    "sincronizar",
    "tiene_alguna",
]
