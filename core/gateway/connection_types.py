"""Catálogo de tipos de conexión (ADR-0069).

La fuente de verdad de los tipos de conexión es este módulo (tipado, versionado en
git). `sync_connection_types()` lo espeja a la tabla de sistema `connection_types`
(sin RLS, excepción deliberada con el mismo criterio que `plans`/ADR-0045) para que
el panel y las FKs lo consulten. Mismo patrón que `core/billing/plans.py`.

`credentials_schema` es un JSON Schema: lo renderiza el FormEngine del panel
(`core/admin/form_generator.py`) y lo usa `save_connection` para validar/normalizar.

Este catálogo describe **presentación y credenciales** de cada tipo. Los adaptadores
de canal siguen viviendo en `agent_core.channels` (ADR-0052); acá no hay routing.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from core.shared.logger import get_logger

log = get_logger(__name__)

# Categorías (agrupan el selector del panel y acotan la primacía de mensajería).
CATEGORY_COMMUNICATION = "comunicacion"
CATEGORY_EMAIL = "correo"
CATEGORY_CALENDAR = "calendario"

CATEGORY_ORDER: tuple[str, ...] = (CATEGORY_COMMUNICATION, CATEGORY_EMAIL, CATEGORY_CALENDAR)

CATEGORY_LABELS: dict[str, str] = {
    CATEGORY_COMMUNICATION: "Comunicación",
    CATEGORY_EMAIL: "Correo",
    CATEGORY_CALENDAR: "Calendario",
}

# Forma en que el panel configura el tipo.
SETUP_MANUAL = "manual"  # formulario de credenciales generado del schema
SETUP_WIZARD = "wizard"  # flujo propio (p. ej. wizard OAuth/Embedded Signup)


@dataclass(frozen=True)
class ConnectionType:
    id: str
    category: str
    label: str
    provider: str
    channel: str = ""
    icon: str = ""
    description: str = ""
    credentials_schema: dict[str, Any] = field(default_factory=dict)
    # Clave de credencial que se usa como `external_id` de la conexión.
    external_id_key: str = ""
    # URL del flujo de alta para `setup_kind=wizard` (los `manual` van al form).
    setup_path: str = ""
    produces_conversations: bool = False
    available: bool = True
    setup_kind: str = SETUP_MANUAL
    sort_order: int = 0
    # Aparece en el selector de alta del panel. `webchat` no (ADR-0115, adenda de la segunda
    # parte de HU-AOS-11): el widget se configura en Configuración → Chat web, que crea su
    # conexión; la consola de conexiones es HU-AOS-23.
    en_selector: bool = True


_META_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "phone_number_id": {"type": "string", "title": "Phone Number ID"},
        "app_secret": {
            "type": "string",
            "title": "App Secret",
            "format": "password",
            "writeOnly": True,
        },
        "access_token": {
            "type": "string",
            "title": "Access Token",
            "format": "password",
            "writeOnly": True,
        },
        "verify_token": {
            "type": "string",
            "title": "Verify Token",
            "format": "password",
            "writeOnly": True,
        },
    },
    # HU-ARCH-05: sin `app_secret` el webhook no puede verificar la firma y el
    # alta se rechaza (fail-closed). Vienen del wizard de Embedded Signup.
    "required": ["phone_number_id", "app_secret"],
}

_EMAIL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "host": {"type": "string", "title": "Servidor SMTP", "description": "ej. smtp.gmail.com"},
        "port": {"type": "integer", "title": "Puerto", "default": 587, "minimum": 1, "maximum": 65535},
        "username": {"type": "string", "title": "Usuario SMTP"},
        "password": {
            "type": "string",
            "title": "Contraseña SMTP",
            "format": "password",
            "writeOnly": True,
        },
        "from_address": {"type": "string", "title": "Remitente (email)"},
        "use_tls": {"type": "boolean", "title": "Usar TLS", "default": True},
    },
    "required": ["host", "port", "username", "password", "from_address"],
}

_CALENDAR_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "access_token": {"type": "string", "format": "password", "writeOnly": True},
        "refresh_token": {"type": "string", "format": "password", "writeOnly": True},
        "expires_at": {"type": "integer", "title": "Expiración (epoch)"},
        "account_email": {"type": "string", "title": "Cuenta de Google"},
    },
}

# Debe coincidir con el seed de la migración `0038`.
CONNECTION_TYPES: tuple[ConnectionType, ...] = (
    ConnectionType(
        id="whatsapp_meta",
        category=CATEGORY_COMMUNICATION,
        channel="whatsapp",
        label="WhatsApp",
        provider="meta",
        icon="bi-whatsapp",
        description="Meta Cloud API",
        credentials_schema=_META_SCHEMA,
        external_id_key="phone_number_id",
        setup_path="/admin/conexiones/wizard/whatsapp/elegir-entorno",
        produces_conversations=True,
        available=True,
        setup_kind=SETUP_WIZARD,
        sort_order=10,
    ),
    ConnectionType(
        id="whatsapp_waweb",
        category=CATEGORY_COMMUNICATION,
        channel="whatsapp",
        label="WhatsApp (vincular por QR)",
        provider="waweb",
        icon="bi-phone",
        description="Número propio por QR — canal no oficial, con riesgo de suspensión",
        produces_conversations=True,
        available=True,
        setup_kind=SETUP_WIZARD,
        setup_path="/admin/conexiones/waweb",
        sort_order=15,
    ),
    ConnectionType(
        id="instagram_meta",
        category=CATEGORY_COMMUNICATION,
        channel="instagram",
        label="Instagram",
        provider="meta",
        icon="bi-instagram",
        description="Mensajes directos vía Meta",
        external_id_key="account_id",
        produces_conversations=True,
        available=True,
        setup_kind=SETUP_WIZARD,
        sort_order=20,
    ),
    ConnectionType(
        id="messenger_meta",
        category=CATEGORY_COMMUNICATION,
        channel="messenger",
        label="Messenger",
        provider="meta",
        icon="bi-messenger",
        description="Página de Facebook vía Meta",
        external_id_key="page_id",
        produces_conversations=True,
        available=True,
        setup_kind=SETUP_WIZARD,
        sort_order=30,
    ),
    ConnectionType(
        id="whatsapp_twilio",
        category=CATEGORY_COMMUNICATION,
        channel="whatsapp",
        label="WhatsApp",
        provider="twilio",
        icon="bi-whatsapp",
        description="vía Twilio",
        produces_conversations=True,
        available=False,
        setup_kind=SETUP_MANUAL,
        sort_order=31,
    ),
    ConnectionType(
        id="whatsapp_plivo",
        category=CATEGORY_COMMUNICATION,
        channel="whatsapp",
        label="WhatsApp",
        provider="plivo",
        icon="bi-whatsapp",
        description="vía Plivo",
        produces_conversations=True,
        available=False,
        setup_kind=SETUP_MANUAL,
        sort_order=32,
    ),
    ConnectionType(
        # Chat web embebible como conexión (ADR-0115 §4). `external_id` es la clave pública del
        # widget, aleatoria y generada por el servidor (`chat_web_conexion.nueva_clave`): no la
        # da el negocio, por eso no hay `external_id_key` ni credenciales en el formulario. El
        # secreto de Turnstile va en `credentials_encrypted` y los dominios en `settings`.
        id="webchat",
        category=CATEGORY_COMMUNICATION,
        channel="web",
        label="Chat web",
        provider="web",
        icon="bi-chat-dots",
        description="Widget de chat para tu sitio web",
        produces_conversations=True,
        available=True,
        setup_kind=SETUP_WIZARD,
        setup_path="/admin/configuracion",
        sort_order=35,
        en_selector=False,
    ),
    ConnectionType(
        # Conexión interna del Centro de pruebas (ADR-0115, adenda 2026-10-24): cada negocio tiene
        # una, creada al primer uso, para que la conversación de prueba también tenga
        # `connection_id` (el contract lo vuelve obligatorio). No es un canal: no produce
        # conversaciones de clientes (no entra en backfills, releases ni migración a v3), no tiene
        # credenciales ni se ve en el panel de conexiones.
        id="playground",
        category=CATEGORY_COMMUNICATION,
        channel="test",
        label="Centro de pruebas",
        provider="internal",
        icon="bi-bug",
        description="Conexión interna del Centro de pruebas",
        produces_conversations=False,
        available=True,
        setup_kind=SETUP_WIZARD,
        sort_order=36,
        en_selector=False,
    ),
    ConnectionType(
        id="email_smtp",
        category=CATEGORY_EMAIL,
        channel="email",
        label="Correo (SMTP)",
        provider="smtp",
        icon="bi-envelope",
        description="Envío de correos desde tu propio servidor SMTP",
        credentials_schema=_EMAIL_SCHEMA,
        external_id_key="from_address",
        produces_conversations=True,
        available=True,
        setup_kind=SETUP_MANUAL,
        sort_order=40,
    ),
    ConnectionType(
        id="google_calendar",
        category=CATEGORY_CALENDAR,
        channel="",
        label="Google Calendar",
        provider="google",
        icon="bi-calendar-check",
        description="Agendamiento de citas con OAuth2 de Google",
        credentials_schema=_CALENDAR_SCHEMA,
        external_id_key="account_email",
        setup_path="/admin/plugins/pack_calendar/oauth",
        produces_conversations=False,
        available=True,
        setup_kind=SETUP_WIZARD,
        sort_order=50,
    ),
)

BY_ID: dict[str, ConnectionType] = {t.id: t for t in CONNECTION_TYPES}

WHATSAPP_META = "whatsapp_meta"
MESSENGER_META = "messenger_meta"
INSTAGRAM_META = "instagram_meta"
WEBCHAT = "webchat"
PLAYGROUND = "playground"


def get_connection_type(type_id: str) -> ConnectionType | None:
    return BY_ID.get(type_id)


def connection_types() -> list[ConnectionType]:
    """Tipos ordenados para el panel (incluye no disponibles)."""
    return sorted(CONNECTION_TYPES, key=lambda t: t.sort_order)


def available_types() -> list[ConnectionType]:
    return [t for t in connection_types() if t.available]


def available_types_by_category() -> dict[str, list[ConnectionType]]:
    """Tipos disponibles agrupados por categoría, preservando el orden declarado."""
    grouped: dict[str, list[ConnectionType]] = {c: [] for c in CATEGORY_ORDER}
    for t in available_types():
        grouped.setdefault(t.category, []).append(t)
    return grouped


def types_by_category(include_unavailable: bool = True) -> list[dict[str, Any]]:
    """Tipos agrupados por categoría para el selector del panel.

    Devuelve `[{category, label, items: [ConnectionType, ...]}]`, omitiendo
    categorías vacías y preservando el orden declarado.
    """
    groups: list[dict[str, Any]] = []
    for cat in CATEGORY_ORDER:
        items = [
            t
            for t in connection_types()
            if t.category == cat and t.en_selector and (include_unavailable or t.available)
        ]
        if items:
            groups.append({"category": cat, "label": CATEGORY_LABELS[cat], "items": items})
    return groups


def setup_url(ctype: ConnectionType) -> str:
    """URL del flujo de alta del tipo (vacío si el tipo no está disponible)."""
    if not ctype.available:
        return ""
    if ctype.setup_kind == SETUP_WIZARD:
        return ctype.setup_path
    return f"/admin/conexiones/nuevo?type={ctype.id}"


def messaging_type_ids() -> set[str]:
    """Tipos de la categoría de mensajería: solo ellos pueden ser "primarios"."""
    return {t.id for t in CONNECTION_TYPES if t.category == CATEGORY_COMMUNICATION}


def normalize_credentials(
    ctype: ConnectionType,
    values: dict[str, Any],
    previous: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Filtra y tipa las credenciales según el `credentials_schema` del tipo.

    - Descarta claves que no declara el schema (nada de "guardar cualquier cosa").
    - Coacciona integer/boolean/string.
    - Un campo secreto o vacío conserva el valor previo (editar sin pisar el secreto).
    """
    previous = previous or {}
    props: dict[str, Any] = ctype.credentials_schema.get("properties", {}) or {}
    out: dict[str, Any] = {}
    for key, prop in props.items():
        if not isinstance(prop, dict):
            continue
        ptype = prop.get("type")
        val = values.get(key, None)
        if val is None or val == "":
            if key in previous and previous[key] not in (None, ""):
                out[key] = previous[key]
                continue
            if val is None and "default" in prop:
                out[key] = prop["default"]
            continue
        if ptype == "integer":
            try:
                out[key] = int(val)
            except (TypeError, ValueError):
                out[key] = prop.get("default")
        elif ptype == "boolean":
            out[key] = val in (True, "true", "on", "1", 1)
        else:
            out[key] = str(val)
    return out


def external_id_for(ctype: ConnectionType, credentials: dict[str, Any]) -> str:
    """Valor de `external_id` de la conexión según el tipo (vacío si no aplica)."""
    if not ctype.external_id_key:
        return ""
    return str(credentials.get(ctype.external_id_key, "") or "")


async def sync_connection_types() -> int:
    """Espeja el catálogo de código a la tabla `connection_types` (upsert idempotente).

    Escribe vía `upsert_connection_type()` SECURITY DEFINER: `app_runtime` solo
    tiene SELECT sobre el catálogo, que es global y no debe ser alterable desde
    una sesión de tenant. Mismo patrón que `sync_plans()` (ADR-0045).

    No borra tipos ausentes del código (un tipo retirado se marca a mano para no
    romper conexiones históricas). Devuelve cuántos tipos procesó (para logs).
    """
    from sqlalchemy import text

    from core.db.session import get_session

    try:
        async with get_session() as session:
            for ctype in CONNECTION_TYPES:
                await session.execute(
                    text(
                        "select upsert_connection_type("
                        ":id, :category, :channel, :label, :provider, :icon, :description, "
                        "cast(:credentials_schema as jsonb), :external_id_key, "
                        ":produces_conversations, :available, :setup_kind, :sort_order)"
                    ),
                    {
                        "id": ctype.id,
                        "category": ctype.category,
                        "channel": ctype.channel,
                        "label": ctype.label,
                        "provider": ctype.provider,
                        "icon": ctype.icon,
                        "description": ctype.description,
                        "credentials_schema": json.dumps(ctype.credentials_schema),
                        "external_id_key": ctype.external_id_key,
                        "produces_conversations": ctype.produces_conversations,
                        "available": ctype.available,
                        "setup_kind": ctype.setup_kind,
                        "sort_order": ctype.sort_order,
                    },
                )
            await session.commit()
        return len(CONNECTION_TYPES)
    except Exception as exc:  # tabla/función ausente antes de migrar: no tumbar el arranque
        log.warning("connection_types_sync_failed", error=type(exc).__name__)
        return 0


__all__ = [
    "BY_ID",
    "CATEGORY_CALENDAR",
    "CATEGORY_COMMUNICATION",
    "CATEGORY_EMAIL",
    "CATEGORY_LABELS",
    "CATEGORY_ORDER",
    "CONNECTION_TYPES",
    "SETUP_MANUAL",
    "SETUP_WIZARD",
    "WEBCHAT",
    "WHATSAPP_META",
    "ConnectionType",
    "available_types",
    "available_types_by_category",
    "connection_types",
    "external_id_for",
    "get_connection_type",
    "messaging_type_ids",
    "normalize_credentials",
    "setup_url",
    "sync_connection_types",
    "types_by_category",
]
