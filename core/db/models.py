"""Modelos base del core (SQLAlchemy 2.0, tipado estricto).

Multi-tenant pooled (ADR-0001): toda tabla de NEGOCIO lleva `tenant_id uuid not null`
+ RLS + índice compuesto por tenant. Las tablas de SISTEMA (plugins, admins,
admin_sessions, tenant_user) no llevan RLS: se consultan antes de conocer el tenant
(ver ADR-0012).

Decisiones de diseño heredadas de agent-sale (probadas en producción):
- Identidad por canal: UNIQUE (tenant_id, channel, external_id) en customers.
- Kill-switch en 3 niveles: settings.bot_paused (por tenant) → customers.bot_paused
  → conversations.bot_paused. El nivel global pasa a scope de sistema (Redis/env).
- audit_log inmutable (trigger en la migración impide UPDATE/DELETE).
- usage_ledger append-only (ADR-0008).
- processed_events: claim de idempotencia revocable (ADR-0013).
- Secretos SIEMPRE cifrados (columnas *_encrypted via core.shared.crypto).

Los plugins definen sus PROPIAS tablas en su propio árbol de migraciones
(ver docs/PLUGIN-CONTRACT.md § Migraciones).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    Numeric,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from core.config import get_settings


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


# ---------------------------------------------------------------- tenant (registry)
class Tenant(Base):
    """Registro de tenants. Tabla de sistema: se consulta sin RLS en el bootstrap."""

    __tablename__ = "tenants"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(Text, default="")
    slug: Mapped[str] = mapped_column(Text, unique=True)
    status: Mapped[str] = mapped_column(Text, default="active")  # active | suspended
    settings: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class TenantUser(Base):
    """Acceso admin → tenant. Tabla de sistema (sin RLS): se consulta al resolver
    el tenant de una sesión (ADR-0012)."""

    __tablename__ = "tenant_user"
    __table_args__ = (UniqueConstraint("tenant_id", "admin_id", name="uq_tenant_user"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"))
    admin_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("admins.id"))
    role: Mapped[str] = mapped_column(Text, default="owner")  # owner | colaborador
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# ---------------------------------------------------------------- settings
class AppSettings(Base):
    """Configuración del asistente POR TENANT (una fila por tenant).

    En pooled, el antiguo singleton queda atrás (ADR-0001): cada tenant tiene su
    propia identidad, BYOK, tono y kill-switch. El kill-switch global es scope de
    sistema (Redis/env), no una fila aquí.
    """

    __tablename__ = "app_settings"
    __table_args__ = (UniqueConstraint("tenant_id", name="uq_app_settings_tenant"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    assistant_name: Mapped[str] = mapped_column(Text, default="Asistente")
    business_description: Mapped[str] = mapped_column(Text, default="")
    language: Mapped[str] = mapped_column(Text, default="es")
    # Moneda del negocio para mostrar montos (comercio, cobranza, plan). Los
    # costos de proveedor siguen en USD (HU-UI-01).
    currency: Mapped[str] = mapped_column(Text, default="COP")
    # Zona horaria del negocio (HU-CAL-04, ADR-0091): fecha y hora actual del
    # prompt y horario de atención sin zona propia.
    timezone: Mapped[str] = mapped_column(
        Text, nullable=False, default="America/Bogota", server_default="America/Bogota"
    )
    bot_paused: Mapped[bool] = mapped_column(Boolean, default=False)  # kill-switch por tenant

    # LLM configurable en runtime (BYOK). Si es NULL se usa el .env.
    llm_provider: Mapped[str | None] = mapped_column(Text)
    llm_model: Mapped[str | None] = mapped_column(Text)
    llm_api_key_encrypted: Mapped[str | None] = mapped_column(Text)

    # Comportamiento
    behavior_config: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    # {"tono": "calido", "debounce_mode": "natural", "split_style": "pocos_cortos"}
    brand_voice: Mapped[str] = mapped_column(Text, default="")
    escalation_config: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    # {"keywords": ["reclamo", "asesor"], "max_turnos_sin_resolver": 3}

    # Chat web embebible (HU-CHAN-02). JSONB con:
    # {"enabled": bool, "key": "<clave pública>", "allowed_origins": [...],
    #  "turnstile_secret": "<opcional>"}. La `key` es pública (va en el
    # fragmento del widget); el `turnstile_secret` NUNCA se expone al cliente.
    web_chat_config: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)

    # Plantillas por evento (HU-ARCH-13, ADR-0108, migración 0057):
    # {"<evento>": {"template": str, "language": str, "variables": [ruta, ...],
    #  "fallback_text": str}}. Las variables son rutas del contexto del evento
    # (p. ej. "cliente.nombre"), nunca texto del LLM.
    message_templates_map: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb")
    )

    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class MetaSetupChecklist(Base):
    """Checklist de pasos MANUALES de configuración de la app de Meta, por tenant.

    Cada paso (crear la app, copiar el App Secret, añadir productos, etc. --
    ver core/admin/templates/meta_requisitos.html) no es automatizable: un
    humano lo hace en developers.facebook.com y marca la casilla acá. `done_by`
    queda en NULL si el admin que lo marcó fue borrado (no hay ON DELETE
    CASCADE con `admins`, tabla de sistema sin RLS)."""

    __tablename__ = "meta_setup_checklist"
    __table_args__ = (UniqueConstraint("tenant_id", "step_key", name="uq_meta_checklist_tenant_step"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    step_key: Mapped[str] = mapped_column(Text, nullable=False)
    done: Mapped[bool] = mapped_column(Boolean, default=False)
    done_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("admins.id", ondelete="SET NULL"))
    done_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


# ---------------------------------------------------------------- clientes / conversaciones
class Customer(Base):
    __tablename__ = "customers"
    __table_args__ = (
        Index(
            "uq_customer_tenant_channel_external",
            "tenant_id",
            "channel",
            "external_id",
            unique=True,
            postgresql_where=text("deleted_at is null"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    channel: Mapped[str] = mapped_column(Text)          # "whatsapp" | "instagram" | ...
    external_id: Mapped[str] = mapped_column(Text)      # "whatsapp:+573001234567" | IGSID
    contact_phone: Mapped[str | None] = mapped_column(Text)  # cruce entre canales
    name: Mapped[str | None] = mapped_column(Text)
    bot_paused: Mapped[bool] = mapped_column(Boolean, default=False)  # kill-switch por cliente
    # Entity Memory (Capa 2): atributos estructurados que los plugins actualizan
    attributes: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ChannelConnection(Base):
    """Conexión canal × proveedor POR TENANT. Credenciales cifradas, editables sin reiniciar."""

    __tablename__ = "channel_connections"
    __table_args__ = (
        Index(
            "uq_connection_tenant_provider_external",
            "tenant_id",
            "provider",
            "external_id",
            unique=True,
            postgresql_where=text("deleted_at is null"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    channel: Mapped[str] = mapped_column(Text)     # "whatsapp"
    provider: Mapped[str] = mapped_column(Text)    # "meta"
    # Catálogo de tipos (ADR-0069): `type_id` referencia `connection_types.id`
    # (FK lógica, validada en código); `category` es desnormalizada para el
    # índice y para acotar la primacía. Nulas = filas legacy (se backfillean).
    type_id: Mapped[str | None] = mapped_column(Text)
    category: Mapped[str | None] = mapped_column(Text)
    label: Mapped[str] = mapped_column(Text, default="")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    is_primary: Mapped[bool] = mapped_column(Boolean, default=False)
    external_id: Mapped[str] = mapped_column(Text)  # phone_number_id de Meta (clave de ruteo)
    credentials_encrypted: Mapped[str | None] = mapped_column(Text)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class WaWebSession(Base):
    """Sesión multi-device del canal WhatsApp Web no oficial (ADR-0074).

    Una sesión viva por conexión `waweb` (índice único parcial por `deleted_at`).
    Las credenciales de sesión (auth state de Neonize) van CIFRADAS con
    `SecretBox` -- nunca en el sistema de archivos suelto -- para sobrevivir
    reinicios de contenedor sin volver a pedir QR.
    """

    __tablename__ = "waweb_sessions"
    __table_args__ = (
        Index(
            "uq_waweb_sessions_connection",
            "tenant_id",
            "connection_id",
            unique=True,
            postgresql_where=text("deleted_at is null"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    connection_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("channel_connections.id", ondelete="CASCADE")
    )
    jid: Mapped[str | None] = mapped_column(Text)  # identidad del dispositivo vinculado
    creds_encrypted: Mapped[str | None] = mapped_column(Text)
    # pending_qr | connecting | connected | disconnected | logged_out | error
    status: Mapped[str] = mapped_column(Text, default="pending_qr")
    last_qr_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_connected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    risk_ack_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    risk_ack_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    risk_ack_version: Mapped[str | None] = mapped_column(Text)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class Conversation(Base):
    __tablename__ = "conversations"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    customer_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("customers.id"))
    status: Mapped[str] = mapped_column(Text, default="open")  # open | escalated | closed
    state: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    # {"step": ..., "turnos_sin_resolver": 0, ...} — actualizado por el orchestrator, no por el LLM
    channel: Mapped[str] = mapped_column(Text, default="whatsapp")
    connection_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("channel_connections.id"))
    # Puente paralelo (migración 0024) para conexiones creadas por el wizard
    # de Embedded Signup (MetaConnection + meta_token_store), que no tienen
    # fila en channel_connections -- `connection_id` sigue intacto para las
    # conexiones manuales viejas. `sender.py::resolve_credentials` prueba
    # este primero, cae al de arriba si es None.
    meta_connection_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("meta_connections.id", ondelete="SET NULL"))
    bot_paused: Mapped[bool] = mapped_column(Boolean, default=False)  # kill-switch por conversación
    # Ventana de 24h de WhatsApp: se refresca en cada inbound
    # (`consumer.py::_persist_inbound`), se valida antes de encolar un envío
    # libre (`sender.py::validate_service_window`) -- fuera de ventana solo
    # se puede enviar una plantilla.
    service_window_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (
        Index(
            "uq_messages_tenant_idempotency",
            "tenant_id",
            "idempotency_key",
            unique=True,
            postgresql_where=text("deleted_at is null AND idempotency_key IS NOT NULL"),
        ),
        Index(
            "uq_messages_tenant_client_message_id",
            "tenant_id",
            "client_message_id",
            unique=True,
            postgresql_where=text("client_message_id is not null"),
        ),
        Index(
            "uq_messages_tenant_external_message_id",
            "tenant_id",
            "external_message_id",
            unique=True,
            postgresql_where=text("external_message_id is not null"),
        ),
        CheckConstraint(
            "status in ('queued','sent','delivered','read','failed')", name="ck_messages_status"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    conversation_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("conversations.id"))
    direction: Mapped[str] = mapped_column(Text)      # inbound | outbound
    sender_type: Mapped[str] = mapped_column(Text)    # customer | agent | human
    content: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(Text, default="sent")  # queued|sent|delivered|read|failed (outbound)
    tool_calls: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB)
    # Soporte de medios (mod-media-processor los transforma antes del orchestrator)
    media_type: Mapped[str | None] = mapped_column(Text)          # "audio/ogg", "image/jpeg"
    media_url: Mapped[str | None] = mapped_column(Text)           # URL original (auditoría)
    media_transcription: Mapped[str | None] = mapped_column(Text)  # resultado STT/vision
    idempotency_key: Mapped[str | None] = mapped_column(Text)  # message_id del canal (UNIQUE parcial por tenant)
    # Cola de salida (idempotencia): generado ANTES de encolar la tarea
    # Celery -- un reintento de la tarea con el mismo id nunca duplica el
    # envío. `external_message_id` es el wamid que devuelve Meta al enviar.
    client_message_id: Mapped[str | None] = mapped_column(Text)
    external_message_id: Mapped[str | None] = mapped_column(Text)
    error_code: Mapped[int | None] = mapped_column(Integer)
    error_subcode: Mapped[int | None] = mapped_column(Integer)
    error_fbtrace_id: Mapped[str | None] = mapped_column(Text)
    error_reason: Mapped[str | None] = mapped_column(Text)
    status_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Envío como plantilla de WhatsApp (HU-ARCH-13, ADR-0108): `content` guarda el
    # texto renderizado para el panel; `template_components` es el payload de Graph.
    template_name: Mapped[str | None] = mapped_column(Text)
    template_language: Mapped[str | None] = mapped_column(Text)
    template_components: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# ---------------------------------------------------------------- agente (AgentSpec desde BD)
class AgentSpecRecord(Base):
    """AgentSpec por tenant desde BD (Sprint 2, ADR-0014).

    `spec` es el JSONB del contrato congelado `AgentSpec`. Índice único PARCIAL
    (tenant_id, version) WHERE deleted_at IS NULL (0009, invariante #8) + un solo
    activo por tenant (ADR-0009: canary corre dos versiones en paralelo).
    """

    __tablename__ = "agent_specs"
    __table_args__ = (
        Index(
            "uq_agent_specs_tenant_version",
            "tenant_id",
            "version",
            unique=True,
            postgresql_where=text("deleted_at is null"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    version: Mapped[str] = mapped_column(Text, default="1.0.0")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    spec: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


# ---------------------------------------------------------------- plugins
class PluginRecord(Base):
    """Registro de plugins instalados en el binario (catálogo de SISTEMA, sin RLS).

    El estado de activación por tenant vive en `plugin_configs` (con tenant_id); el
    enablement por tenant formará parte de AgentSpec.packs (Sprint 2) y del registry
    (Sprint 4). Ver ADR-0012.
    """

    __tablename__ = "plugins"

    id: Mapped[str] = mapped_column(Text, primary_key=True)  # "mod_crm_engine"
    name: Mapped[str] = mapped_column(Text)
    version: Mapped[str] = mapped_column(Text, default="0.0.0")
    status: Mapped[str] = mapped_column(Text, default="detected")  # detected | active | disabled | error
    error_message: Mapped[str | None] = mapped_column(Text)
    installed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PluginConfig(Base):
    """Configuración de cada plugin POR TENANT: JSON cifrado con Fernet."""

    __tablename__ = "plugin_configs"

    plugin_id: Mapped[str] = mapped_column(ForeignKey("plugins.id"), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    config_encrypted: Mapped[str] = mapped_column(Text)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class PluginTenantState(Base):
    """Salud de un plugin POR TENANT (ADR-0024): sin fila activa = sano.

    Independiente de `PluginRecord.status` (¿el módulo cargó bien al
    activarse? -- sistema, sigue siendo global a propósito) y de
    `mcp_host` (qué código existe -- singleton de proceso, ver ADR-0019,
    no se toca). Esta tabla existe para que una excepción de runtime
    causada por el tráfico de UN tenant no apague el pack para los demás
    -- incidente real documentado en `docs/ROADMAP.md` (filas 143-149).

    `deleted_at` (invariante #8: sin borrado físico) marca "reactivado por
    un admin" -- `reactivate_for_tenant` no borra la fila, la marca. Si el
    mismo plugin vuelve a fallar para este tenant, la fila se reutiliza
    (mismo PK) y `deleted_at` se limpia.
    """

    __tablename__ = "plugin_tenant_state"

    plugin_id: Mapped[str] = mapped_column(ForeignKey("plugins.id"), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    status: Mapped[str] = mapped_column(Text, default="error")
    error_message: Mapped[str | None] = mapped_column(Text)
    disabled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


# ---------------------------------------------------------------- admin (sistema, sin RLS)
class Admin(Base):
    __tablename__ = "admins"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(Text, unique=True)
    username: Mapped[str | None] = mapped_column(Text, unique=True)
    password_hash: Mapped[str] = mapped_column(Text)
    role: Mapped[str] = mapped_column(Text, default="colaborador")  # master | colaborador
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AdminSession(Base):
    """Sesión con revocación inmediata: cada request valida admins.active."""

    __tablename__ = "admin_sessions"

    token: Mapped[str] = mapped_column(Text, primary_key=True)
    admin_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("admins.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class PasswordResetToken(Base):
    """Código de recuperación de contraseña de un solo uso (ADR-0098).

    Tabla de SISTEMA (sin RLS): infraestructura de identidad, se consulta sin
    contexto de tenant (el admin pide recuperar su contraseña antes de elegir
    negocio). Guarda solo el hash SHA-256 del código, nunca el código.
    """

    __tablename__ = "password_reset_tokens"
    __table_args__ = (
        Index("ix_password_reset_tokens_admin", "admin_id"),
        Index("ix_password_reset_tokens_token_hash", "token_hash"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    admin_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("admins.id"))
    token_hash: Mapped[str] = mapped_column(Text)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PushToken(Base):
    """Token de push (Expo) de un dispositivo de la app oficial (ADR-0099).

    Tabla de NEGOCIO: tenant_id + RLS forzada (invariante 1). La unicidad activa
    es parcial: (tenant_id, token) WHERE deleted_at IS NULL; un re-registro
    reactiva el mismo token en lugar de duplicarlo.
    """

    __tablename__ = "push_tokens"
    __table_args__ = (
        Index(
            "uq_push_tokens_tenant_token",
            "tenant_id",
            "token",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
        Index("ix_push_tokens_admin", "tenant_id", "admin_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    admin_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    provider: Mapped[str] = mapped_column(Text, default="expo")
    token: Mapped[str] = mapped_column(Text)
    platform: Mapped[str] = mapped_column(Text, default="android")
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class MobileAuditEvent(Base):
    """Auditoría de acciones críticas de la app oficial (ADR-0097, HU-APP-15).

    Tabla de NEGOCIO: tenant_id + RLS forzada. Idempotente por
    (tenant_id, event_id): la app reenvía eventos offline sin duplicar.
    """

    __tablename__ = "mobile_audit_events"
    __table_args__ = (
        Index(
            "uq_mobile_audit_events_tenant_event",
            "tenant_id",
            "event_id",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
        Index("ix_mobile_audit_events_admin", "tenant_id", "admin_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    admin_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    event_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    accion: Mapped[str] = mapped_column(Text, nullable=False)
    entidad: Mapped[str] = mapped_column(Text, nullable=False)
    entidad_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    device_id: Mapped[str | None] = mapped_column(Text)
    ocurrido_en: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class AdminPermission(Base):
    """Permiso de sección de un admin (RBAC configurable, ADR-0047).

    Tabla de SISTEMA (sin RLS): se consulta al autenticar, antes de conocer el
    tenant. `tenant_id` null = el permiso vale para TODOS los tenants (global);
    con valor = solo ese tenant. `access` es `read | write`.
    """

    __tablename__ = "admin_permissions"
    __table_args__ = (
        Index(
            "uq_admin_permission_scope",
            "admin_id",
            "section",
            text("coalesce(tenant_id, '00000000-0000-0000-0000-000000000000'::uuid)"),
            unique=True,
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    admin_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("admins.id"))
    section: Mapped[str] = mapped_column(Text)
    access: Mapped[str] = mapped_column(Text, default="read")  # read | write
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("tenants.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class TenantInvitation(Base):
    """Invitación a que un admin se una a un tenant (ADR-0081).

    Tabla de SISTEMA (sin RLS): el invitado debe poder verla ANTES de tener acceso
    al tenant, igual que `tenant_user` (ADR-0012). `estado` es
    `pendiente | aceptada | rechazada | revocada`.
    """

    __tablename__ = "tenant_invitations"
    __table_args__ = (
        Index(
            "uq_tenant_invitation_pending",
            "tenant_id",
            "admin_id",
            unique=True,
            postgresql_where=text("estado = 'pendiente'"),
        ),
        Index("ix_tenant_invitations_admin", "admin_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"))
    admin_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("admins.id"))
    invited_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("admins.id"))
    role: Mapped[str] = mapped_column(Text, default="colaborador")  # owner | colaborador
    pack_roles: Mapped[list[str]] = mapped_column(JSONB, default=list)
    estado: Mapped[str] = mapped_column(Text, default="pendiente")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    responded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    # Nonce del enlace de un solo uso (ADR-0120): el token se deriva de él y del id con
    # APP_SECRET_KEY; la base nunca guarda el token. NULL = invitación sin enlace (ADR-0081).
    token_nonce: Mapped[str | None] = mapped_column(Text, default=None)


class ProvisioningRequest(Base):
    """Alta automática de un negocio pedida por un negocio vendedor (HU-SH-03, ADR-0120).

    Tabla de NEGOCIO del vendedor (`tenant_id`, RLS forzada): una fila por alta, única por
    `(tenant_id, referencia)` y por `(tenant_id, subscription_id)`. `estado`:
    `en_curso | completado | revision | omitido`. Solo la escribe el consumidor de
    `subscription.activated` (identidad de servicio `provisioning`).
    """

    __tablename__ = "provisioning_requests"
    __table_args__ = (
        Index(
            "uq_provisioning_requests_tenant_referencia",
            "tenant_id",
            "referencia",
            unique=True,
            postgresql_where=text("deleted_at is null"),
        ),
        Index(
            "uq_provisioning_requests_tenant_subscription",
            "tenant_id",
            "subscription_id",
            unique=True,
            postgresql_where=text("subscription_id is not null and deleted_at is null"),
        ),
        Index("ix_provisioning_requests_tenant_estado", "tenant_id", "estado", "created_at"),
        CheckConstraint(
            "estado in ('en_curso', 'completado', 'revision', 'omitido')",
            name="ck_provisioning_requests_estado",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    referencia: Mapped[str] = mapped_column(Text, nullable=False)
    subscription_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), default=None)
    customer_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), default=None)
    estado: Mapped[str] = mapped_column(Text, nullable=False, default="en_curso")
    plan_id: Mapped[str | None] = mapped_column(Text, default=None)
    vertical: Mapped[str | None] = mapped_column(Text, default=None)
    negocio_tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("tenants.id", ondelete="SET NULL"), default=None
    )
    negocio_slug: Mapped[str | None] = mapped_column(Text, default=None)
    invitation_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), default=None)
    motivo: Mapped[str | None] = mapped_column(Text, default=None)
    detalle: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    intentos: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)


# ---------------------------------------------------------------- escalamiento
class Escalation(Base):
    """Handoff a humano — mecanismo NATIVO del core (los plugins lo disparan vía MCPHost).

    Al crear una escalación el core pausa el bot para esa conversación.
    """

    __tablename__ = "escalations"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    conversation_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("conversations.id"))
    reason: Mapped[str] = mapped_column(Text)   # keyword | guardrail | plugin | llm_refusal | manual
    summary: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(Text, default="pendiente")  # pendiente | asignada | resuelta
    assigned_admin_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("admins.id"))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


# ---------------------------------------------------------------- observabilidad
class LLMUsage(Base):
    """Legacy (Sprint 2): fuente de facturación real es UsageLedger (ADR-0008).

    Ya no se escribe (retirado 2026-09-11, ver docs/ROADMAP.md — deuda de
    Sprint 2). El modelo queda mapeado para no perder metadata/histórico
    del schema hasta que se decida la retención de los datos viejos y se
    dé de baja la tabla con una migración.
    """

    __tablename__ = "llm_usage"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("conversations.id"))
    provider: Mapped[str] = mapped_column(Text)
    model: Mapped[str] = mapped_column(Text)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Numeric(10, 6), default=0)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AuditLog(Base):
    """Fuente de verdad de lo que las tools HICIERON. Inmutable (solo INSERT).

    Al ser inmutable NO lleva deleted_at (contradice la invariante #8 de soft-delete;
    aquí el trigger impide cualquier borrado, físico o lógico).
    """
    __tablename__ = "audit_log"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("conversations.id"))
    actor: Mapped[str] = mapped_column(Text)    # "tool:crear_lead" | "core:guardrail" | "admin:email"
    action: Mapped[str] = mapped_column(Text)
    input: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    output: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class DomainEvent(Base):
    """Outbox de eventos de dominio (HU-SUB-03, ADR-0107).

    Se escribe en la MISMA transacción que el cambio de estado; el despachador
    de Celery lo entrega bajo el `tenant_scope` de su tenant con reintentos.
    No lleva deleted_at (append-only; la depuración es de HU futuras).
    """

    __tablename__ = "domain_events"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    topic: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="pendiente")  # pendiente|entregado|fallido
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    dedupe_key: Mapped[str] = mapped_column(Text, nullable=False)
    last_error: Mapped[str | None] = mapped_column(Text, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class UsageLedger(Base):
    """Ledger de uso append-only (ADR-0008): una fila por turno, inmutable.

    Fuente de la facturación y del análisis de margen. No lleva deleted_at.
    """

    __tablename__ = "usage_ledger"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("conversations.id"))
    agent_spec_version: Mapped[str | None] = mapped_column(Text)
    model: Mapped[str] = mapped_column(Text)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cached_tokens: Mapped[int] = mapped_column(Integer, default=0)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Numeric(10, 6), default=0)
    tool_calls: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# ------------------------------------------------- planes y facturación (ADR-0045 / ADR-0046)
class Plan(Base):
    """Catálogo de planes. Tabla de SISTEMA (sin RLS, sin `tenant_id`).

    Excepción deliberada a la invariante #1 (ADR-0045), mismo criterio que
    `webhook_events` (ADR-0028): el catálogo es global y compartido. La fuente
    de verdad es `core/billing/plans.py`; esta tabla la espeja para el panel y
    las FKs, y `sync_plans()` la mantiene al día.
    """

    __tablename__ = "plans"

    id: Mapped[str] = mapped_column(Text, primary_key=True)  # free|basic|premium|empresarial
    name: Mapped[str] = mapped_column(Text)
    price_cop: Mapped[float] = mapped_column(Numeric(12, 2), default=0)
    currency: Mapped[str] = mapped_column(Text, default="COP")
    interval_days: Mapped[int] = mapped_column(Integer, default=30)
    trial_days: Mapped[int] = mapped_column(Integer, default=0)
    limits: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    features: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    is_public: Mapped[bool] = mapped_column(Boolean, default=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class ConnectionTypeRecord(Base):
    """Catálogo de tipos de conexión. Tabla de SISTEMA (sin RLS, sin `tenant_id`).

    Excepción deliberada a la invariante #1 (ADR-0069), mismo criterio que
    `plans` (ADR-0045): el catálogo es global y compartido. La fuente de verdad es
    `core/gateway/connection_types.py`; esta tabla la espeja para el panel y los
    FKs, y `sync_connection_types()` la mantiene al día.
    """

    __tablename__ = "connection_types"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    category: Mapped[str] = mapped_column(Text)       # comunicacion | correo | calendario
    channel: Mapped[str] = mapped_column(Text, default="")
    label: Mapped[str] = mapped_column(Text)
    provider: Mapped[str] = mapped_column(Text, default="")
    icon: Mapped[str] = mapped_column(Text, default="")
    description: Mapped[str] = mapped_column(Text, default="")
    credentials_schema: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    external_id_key: Mapped[str] = mapped_column(Text, default="")
    produces_conversations: Mapped[bool] = mapped_column(Boolean, default=False)
    available: Mapped[bool] = mapped_column(Boolean, default=True)
    setup_kind: Mapped[str] = mapped_column(Text, default="manual")
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class TenantSubscription(Base):
    """Relación de un tenant con un plan (negocio: RLS).

    Una sola suscripción activa por tenant (índice único parcial sobre
    `deleted_at is null`). `status`: trialing | active | past_due | expired |
    canceled. El plan free es también el trial: `trial_ends_at` marca su fin.
    """

    __tablename__ = "tenant_subscriptions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    plan_id: Mapped[str] = mapped_column(ForeignKey("plans.id"))
    status: Mapped[str] = mapped_column(Text, default="trialing")
    period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    period_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    trial_ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    provider: Mapped[str | None] = mapped_column(Text)
    provider_reference: Mapped[str | None] = mapped_column(Text)
    canceled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class SubscriptionPayment(Base):
    """Un intento de cobro del plan de un tenant (negocio: RLS).

    Idempotente por `(tenant_id, provider_reference)` (índice único parcial):
    el webhook de Wompi puede reintentar el mismo evento sin duplicar el pago.
    `status`: pendiente | aprobado | rechazado | anulado.
    """

    __tablename__ = "subscription_payments"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    subscription_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenant_subscriptions.id"))
    plan_id: Mapped[str] = mapped_column(ForeignKey("plans.id"))
    amount_cop: Mapped[float] = mapped_column(Numeric(12, 2), default=0)
    currency: Mapped[str] = mapped_column(Text, default="COP")
    status: Mapped[str] = mapped_column(Text, default="pendiente")
    provider: Mapped[str] = mapped_column(Text)
    provider_reference: Mapped[str] = mapped_column(Text)
    checkout_url: Mapped[str | None] = mapped_column(Text)
    period_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    period_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class ProcessedEvent(Base):
    """Claim de idempotencia (ADR-0007) revocable (ADR-0013).

    Índice único parcial (tenant_id, external_message_id) WHERE deleted_at IS NULL:
    el reintento manual del DLQ hace soft-delete del claim y re-inserta uno fresco.

    `completed_at` (HU-ARCH-06, ADR-0090) distingue un evento TERMINADO de uno
    EN VUELO: NULL = el turno empezó pero no terminó (proceso vivo o abandonado).
    El reclamo de pendientes revoca los claims incompletos viejos para reprocesar.
    """

    __tablename__ = "processed_events"
    __table_args__ = (
        Index(
            "uq_processed_event_tenant_msg",
            "tenant_id",
            "external_message_id",
            unique=True,
            postgresql_where=text("deleted_at is null"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    provider: Mapped[str] = mapped_column(Text)
    external_message_id: Mapped[str] = mapped_column(Text)
    handled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# ---------------------------------------------------------------- memoria vectorial (Capa 3)
class MemoryVector(Base):
    __tablename__ = "memory_vectors"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    customer_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("customers.id"))
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("conversations.id"))
    content: Mapped[str] = mapped_column(Text)
    # Dimensión tomada de la config (EMBEDDING_DIMENSIONS). Cambiarla exige una
    # migración que re-ajuste la columna; ver `alembic/versions/0046_*`.
    embedding: Mapped[Any] = mapped_column(Vector(get_settings().embedding_dimensions))
    meta: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# ---------------------------------------------------------------- integración Meta (rico)
class MetaConnection(Base):
    """App de Meta conectada, por tenant (reemplaza `AppSettings.meta_app_config_
    encrypted` de la migración 0013 -- migración 0014). Guarda METADATA (status,
    scopes, salud): el App Secret y los tokens de la app viven cifrados en
    `meta_private.meta_tokens` (`kind='app_access'`), nunca acá."""

    __tablename__ = "meta_connections"
    __table_args__ = (
        Index(
            "uq_meta_connections_tenant_app",
            "tenant_id", "channel", "environment", "meta_app_id",
            unique=True,
            postgresql_where=text("deleted_at is null"),
        ),
        CheckConstraint(
            "channel in ('whatsapp','messenger','instagram','facebook_page')",
            name="ck_meta_connections_channel",
        ),
        CheckConstraint("environment in ('sandbox','production')", name="ck_meta_connections_environment"),
        CheckConstraint(
            "status in ('draft','pending','connected','degraded','revoked','error')",
            name="ck_meta_connections_status",
        ),
        CheckConstraint(
            "origin in ('byok','embedded_signup','manual')",
            name="ck_meta_connections_origin",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    channel: Mapped[str] = mapped_column(Text)
    environment: Mapped[str] = mapped_column(Text, default="production")
    # Modo de credenciales vigente (`byok` | `embedded_signup` | `manual`, HU-CONN-01,
    # ADR-0094): decide qué token y qué App Secret usa la conexión
    # (`core/gateway/meta_credentials.py`). Lo fija cada alta; SIN valor por defecto, para
    # que un alta que lo olvide falle en vez de usar en silencio la app de la plataforma.
    origin: Mapped[str] = mapped_column(Text)
    meta_app_id: Mapped[str] = mapped_column(Text)
    meta_config_id: Mapped[str | None] = mapped_column(Text)  # Embedded Signup Login Configuration
    business_id: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, default="draft")
    status_reason: Mapped[str | None] = mapped_column(Text)
    graph_api_version: Mapped[str] = mapped_column(Text, default="v26.0")
    scopes: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)
    connected_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("admins.id", ondelete="SET NULL"))
    connected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_health_check_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    health: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


WIZARD_STEPS: tuple[str, ...] = (
    "select_channel",
    "launch_oauth",
    "exchange_code",
    "discover_assets",
    "select_assets",
    "subscribe_webhook",
    "register_number",
    "sync_templates",
    "verify_health",
    "done",
)


class ConnectionWizard(Base):
    """Máquina de estados persistida del wizard de Embedded Signup, tabla de
    NEGOCIO normal (tenant_id not null + RLS, a diferencia de webhook_events/
    meta_app_subscriptions -- ver ADR-0030). Reanudable: si el admin cierra
    el navegador a mitad de camino, `current_step`/`completed_steps` dicen
    exactamente dónde retomar sin repetir el OAuth.

    REGLA DURA: `context`/`last_error` NUNCA contienen el `code` de un solo
    uso ni ningún token -- son columnas legibles por RLS del propio tenant,
    no custodia de secretos (eso vive en meta_private.meta_tokens vía
    core/gateway/meta_token_store.py). Verificado por
    tests/test_meta_onboarding_no_persiste_secretos.py."""

    __tablename__ = "connection_wizards"
    __table_args__ = (
        CheckConstraint(
            "channel in ('whatsapp','messenger','instagram','facebook_page')",
            name="ck_connection_wizards_channel",
        ),
        CheckConstraint("environment in ('sandbox','production')", name="ck_connection_wizards_environment"),
        CheckConstraint(
            "current_step in (" + ",".join(f"'{s}'" for s in WIZARD_STEPS) + ")",
            name="ck_connection_wizards_current_step",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    connection_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("meta_connections.id", ondelete="CASCADE"))
    channel: Mapped[str] = mapped_column(Text)
    environment: Mapped[str] = mapped_column(Text, default="production")
    current_step: Mapped[str] = mapped_column(Text, default="select_channel")
    completed_steps: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)
    context: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    last_error: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    started_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("admins.id", ondelete="SET NULL"))
    # default de Python (además del server_default de la migración): el ORM
    # manda NULL explícito si no hay valor, pisando el server_default.
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: utcnow() + timedelta(hours=24))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class WabaAccount(Base):
    """Cuenta de WhatsApp Business sincronizada desde Graph API para una `MetaConnection`."""

    __tablename__ = "waba_accounts"
    __table_args__ = (UniqueConstraint("waba_id", "connection_id", name="uq_waba_accounts_waba_connection"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    connection_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("meta_connections.id", ondelete="CASCADE"))
    waba_id: Mapped[str] = mapped_column(Text)
    name: Mapped[str | None] = mapped_column(Text)
    currency: Mapped[str | None] = mapped_column(Text)
    timezone_id: Mapped[str | None] = mapped_column(Text)
    message_template_namespace: Mapped[str | None] = mapped_column(Text)
    account_review_status: Mapped[str | None] = mapped_column(Text)
    subscribed: Mapped[bool] = mapped_column(Boolean, default=False)
    # Cacheados desde el `summary` de `GET /{waba_id}/message_templates`
    # (nunca hardcodear 250 ni 6000 -- el límite real depende del tier de la
    # cuenta) -- refrescados en cada sync, leídos por el panel para
    # habilitar/deshabilitar "Nueva plantilla".
    message_template_count: Mapped[int | None] = mapped_column(Integer)
    message_template_limit: Mapped[int | None] = mapped_column(Integer)
    are_translations_complete: Mapped[bool | None] = mapped_column(Boolean)
    template_limits_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    raw: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class PhoneNumber(Base):
    """Número de WhatsApp sincronizado desde Graph API."""

    __tablename__ = "phone_numbers"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    connection_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("meta_connections.id", ondelete="CASCADE"))
    waba_account_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("waba_accounts.id", ondelete="SET NULL")
    )
    phone_number_id: Mapped[str] = mapped_column(Text, unique=True)
    display_phone_number: Mapped[str | None] = mapped_column(Text)
    verified_name: Mapped[str | None] = mapped_column(Text)
    quality_rating: Mapped[str | None] = mapped_column(Text)
    messaging_limit_tier: Mapped[str | None] = mapped_column(Text)
    name_status: Mapped[str | None] = mapped_column(Text)
    code_verification_status: Mapped[str | None] = mapped_column(Text)
    platform_type: Mapped[str | None] = mapped_column(Text)
    registered: Mapped[bool] = mapped_column(Boolean, default=False)
    pin_set: Mapped[bool] = mapped_column(Boolean, default=False)
    raw: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class FbPage(Base):
    """Página de Facebook sincronizada desde Graph API."""

    __tablename__ = "fb_pages"
    __table_args__ = (UniqueConstraint("page_id", "connection_id", name="uq_fb_pages_page_connection"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    connection_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("meta_connections.id", ondelete="CASCADE"))
    page_id: Mapped[str] = mapped_column(Text)
    name: Mapped[str | None] = mapped_column(Text)
    category: Mapped[str | None] = mapped_column(Text)
    subscribed_fields: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)
    raw: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class IgAccount(Base):
    """Cuenta de Instagram sincronizada desde Graph API."""

    __tablename__ = "ig_accounts"
    __table_args__ = (
        UniqueConstraint("ig_user_id", "connection_id", name="uq_ig_accounts_iguser_connection"),
        CheckConstraint(
            "login_route is null or login_route in ('ig_facebook_login','ig_instagram_login')",
            name="ck_ig_accounts_login_route",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    connection_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("meta_connections.id", ondelete="CASCADE"))
    ig_user_id: Mapped[str] = mapped_column(Text)
    username: Mapped[str | None] = mapped_column(Text)
    linked_page_id: Mapped[str | None] = mapped_column(Text)
    login_route: Mapped[str | None] = mapped_column(Text)
    raw: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class MessageTemplate(Base):
    """Plantilla de WhatsApp sincronizada desde Graph API."""

    __tablename__ = "message_templates"
    __table_args__ = (
        UniqueConstraint(
            "waba_account_id", "name", "language", name="uq_message_templates_waba_name_lang"
        ),
        Index(
            "uq_message_templates_meta_id",
            "meta_template_id",
            unique=True,
            postgresql_where=text("meta_template_id is not null"),
        ),
        CheckConstraint(
            "status in ('DRAFT','PENDING','APPROVED','REJECTED','PAUSED','DISABLED','IN_APPEAL',"
            "'ARCHIVED','LOCKED','LIMIT_EXCEEDED','FLAGGED','REINSTATED','PENDING_DELETION','DELETED',"
            "'UNARCHIVED')",
            name="ck_message_templates_status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    waba_account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("waba_accounts.id", ondelete="CASCADE"))
    meta_template_id: Mapped[str | None] = mapped_column(Text)
    name: Mapped[str] = mapped_column(Text)
    language: Mapped[str] = mapped_column(Text)
    category: Mapped[str | None] = mapped_column(Text)
    parameter_format: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, default="DRAFT")
    rejected_reason: Mapped[str | None] = mapped_column(Text)
    rejection_recommendation: Mapped[str | None] = mapped_column(Text)
    quality_score: Mapped[str | None] = mapped_column(Text)
    # `disable_info`/`other_info` de `message_template_status_update`, y el
    # rastro de recategorización de `template_category_update` -- afecta al
    # costo desde el 1/oct/2026, tiene que ser visible en el panel.
    disable_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    other_info_title: Mapped[str | None] = mapped_column(Text)
    other_info_description: Mapped[str | None] = mapped_column(Text)
    category_changed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    previous_category: Mapped[str | None] = mapped_column(Text)
    previous_quality_score: Mapped[str | None] = mapped_column(Text)
    # Límite de Meta: 1 edición/día, 10/mes -- `edits_month_key` ("yyyy-MM")
    # resetea `edits_this_month` al cambiar de mes sin necesitar un cron aparte.
    last_edited_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    edits_this_month: Mapped[int] = mapped_column(Integer, default=0)
    edits_month_key: Mapped[str | None] = mapped_column(Text)
    components: Mapped[Any | None] = mapped_column(JSONB)
    synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sync_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class MessageCost(Base):
    """Ingesta de costos por webhook (`statuses[].pricing`) -- factual pero
    SIN dólares: este objeto nunca trae un monto, solo metadata de
    facturación (`billable`/`pricing_model`/categoría). El monto real vive
    en `PricingDaily.cost` (única fuente autoritativa, `pricing_analytics`).
    NO única sobre `external_message_id`: Meta puede mandar el mismo wamid
    en más de un status con pricing -- cada evento es un hecho nuevo."""

    __tablename__ = "message_costs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    message_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("messages.id", ondelete="SET NULL"))
    external_message_id: Mapped[str] = mapped_column(Text)
    waba_account_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("waba_accounts.id", ondelete="SET NULL"))
    phone_number_id: Mapped[str | None] = mapped_column(Text)
    billable: Mapped[bool | None] = mapped_column(Boolean)
    pricing_model: Mapped[str | None] = mapped_column(Text)
    pricing_type: Mapped[str | None] = mapped_column(Text)
    pricing_category: Mapped[str | None] = mapped_column(Text)
    # Id de conversación de META (ventana de facturación) -- DISTINTO de
    # `conversations.id` interno, nombre separado a propósito.
    meta_conversation_id: Mapped[str | None] = mapped_column(Text)
    conversation_origin_type: Mapped[str | None] = mapped_column(Text)
    conversation_expiration_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str | None] = mapped_column(Text)
    event_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    raw: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PricingDaily(Base):
    """Ingesta diaria desde `pricing_analytics` -- `cost` es el valor
    AUTORITATIVO (regla innegociable: nunca se calcula localmente con una
    tabla de tarifas propia; los volume tiers se agregan a nivel de
    business portfolio, no de WABA, así que un panel multi-tenant no puede
    predecir la tarifa unitaria de un tenant)."""

    __tablename__ = "pricing_daily"
    __table_args__ = (
        # Índice de EXPRESIÓN con coalesce -- las dimensiones nullable
        # participan de la igualdad de todos modos, para que el upsert sea
        # determinístico incluso cuando Meta omite alguna dimensión (creado
        # como SQL crudo en la migración 0028; declarado acá con `text()`
        # para que el metadata de SQLAlchemy coincida con la DB real).
        # `tenant_id` participa de la clave como defensa en profundidad
        # además de RLS (hallazgo auditor-multitenant, 2026-09-20).
        Index(
            "uq_pricing_daily_dimensions", "tenant_id", "waba_account_id", "date",
            text("coalesce(phone_number, '')"), text("coalesce(country, '')"), text("coalesce(tier, '')"),
            text("coalesce(pricing_type, '')"), text("coalesce(pricing_category, '')"),
            unique=True,
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    waba_account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("waba_accounts.id", ondelete="CASCADE"))
    date: Mapped[Any] = mapped_column(Date)
    phone_number: Mapped[str | None] = mapped_column(Text)
    country: Mapped[str | None] = mapped_column(Text)
    # "<LOWER>:<UPPER>" tal cual lo manda Meta -- no se parsea a número.
    tier: Mapped[str | None] = mapped_column(Text)
    pricing_type: Mapped[str | None] = mapped_column(Text)
    pricing_category: Mapped[str | None] = mapped_column(Text)
    volume: Mapped[int] = mapped_column(Integer)
    cost: Mapped[Any] = mapped_column(Numeric(12, 6))
    synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    raw: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class WebhookEvent(Base):
    """Log crudo de entregas de webhook de Meta.

    Tabla de SISTEMA (sin RLS, igual categoría que `admins`/`plugins` -- ver
    docstring del módulo): `tenant_id` nullable A PROPÓSITO, excepción
    documentada a la invariante nº1 de CLAUDE.md. Un webhook puede llegar
    ANTES de poder resolver a qué tenant pertenece (firma inválida, app
    todavía no vinculada a ningún tenant) -- por diseño este log existe
    precisamente para auditar esos casos, así que exigir `tenant_id not null`
    lo volvería inútil para su propio propósito.

    ADVERTENCIA (auditor-multitenant, 2026-09-19): sin RLS, nada impide un
    `select(WebhookEvent)` sin `WHERE tenant_id = ...` explícito que mezcle
    payloads crudos de varios tenants. El pipeline ya escribe acá
    (`core/gateway/webhooks.py::_log_webhook_event`, único punto de
    escritura de código de aplicación) -- verificado por
    `tests/test_webhook_events_solo_desde_webhooks.py`, mismo patrón que
    `tests/test_meta_token_store_aislado.py`. Todo acceso (lectura desde el
    panel admin incluida, si se agrega en el futuro) debe filtrar
    `tenant_id` a mano en cada query, nunca confiar en RLS para esto."""

    __tablename__ = "webhook_events"
    __table_args__ = (
        UniqueConstraint("meta_app_id", "external_id", name="uq_webhook_events_app_external"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    connection_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("meta_connections.id", ondelete="SET NULL")
    )
    meta_app_id: Mapped[str] = mapped_column(Text)
    object_type: Mapped[str] = mapped_column(Text)
    field: Mapped[str | None] = mapped_column(Text)
    external_id: Mapped[str] = mapped_column(Text)
    signature_ok: Mapped[bool] = mapped_column(Boolean, default=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    process_error: Mapped[str | None] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(Integer, default=0)


class MetaAppSubscription(Base):
    """Estado de la suscripción de webhook a nivel de APP de Meta (`POST
    /{app_id}/subscriptions`), no por entrega (`WebhookEvent`) ni por objeto
    individual (`WabaAccount.subscribed`/`FbPage.subscribed_fields`).

    Tabla de SISTEMA (sin RLS, sin `tenant_id`): una misma app puede estar en
    uso por varios tenants (BYOK), el recurso pertenece a la app, no a un
    tenant. Ver ADR-0029. Único punto de escritura de código de aplicación:
    `core/gateway/meta_subscriptions.py` y el cron de salud
    (`core/tasks/meta_subscriptions_health.py`) -- verificado por
    `tests/test_meta_app_subscriptions_solo_desde_su_modulo.py`, mismo patrón
    que `tests/test_webhook_events_solo_desde_webhooks.py`. No guarda
    secretos (ni `app_secret` ni `verify_token`), solo estado observable."""

    __tablename__ = "meta_app_subscriptions"
    __table_args__ = (
        UniqueConstraint("meta_app_id", "object", name="uq_meta_app_subscriptions_app_object"),
        CheckConstraint(
            "object in ('whatsapp_business_account','page','instagram')",
            name="ck_meta_app_subscriptions_object",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    meta_app_id: Mapped[str] = mapped_column(Text)
    object: Mapped[str] = mapped_column(Text)
    callback_url: Mapped[str | None] = mapped_column(Text)
    active: Mapped[bool | None] = mapped_column(Boolean)
    fields: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class GraphApiCall(Base):
    """Observabilidad: una fila por llamada saliente a Graph API."""

    __tablename__ = "graph_api_calls"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    connection_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("meta_connections.id", ondelete="SET NULL")
    )
    method: Mapped[str | None] = mapped_column(Text)
    endpoint: Mapped[str] = mapped_column(Text)  # path SIN query string -- nunca incluye el token/proof
    status_code: Mapped[int | None] = mapped_column(Integer)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    error_code: Mapped[int | None] = mapped_column(Integer)
    error_subcode: Mapped[int | None] = mapped_column(Integer)
    error_type: Mapped[str | None] = mapped_column(Text)
    error_message: Mapped[str | None] = mapped_column(Text)
    fbtrace_id: Mapped[str | None] = mapped_column(Text)
    request_id: Mapped[str | None] = mapped_column(Text)  # x-fb-request-id, si Meta lo manda
    called_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class GraphApiUsage(Base):
    """Observabilidad: agregado diario de llamadas a Graph API por conexión."""

    __tablename__ = "graph_api_usage"
    __table_args__ = (UniqueConstraint("connection_id", "usage_date", name="uq_graph_api_usage_connection_date"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    connection_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("meta_connections.id", ondelete="CASCADE"))
    usage_date: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    call_count: Mapped[int] = mapped_column(Integer, default=0)
    error_count: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


# ---------------------------------------------------------------- custodia de tokens (schema aparte)
class MetaToken(Base):
    """Custodia cifrada de tokens de Meta (AES-256-GCM, `core/shared/crypto.py`).

    Vive en el schema `meta_private` (no `public`), separado a propósito.
    SOLO `core/gateway/meta_token_store.py` debe importar/tocar este modelo
    -- verificado por `tests/test_meta_token_store_aislado.py` (grep estático).
    Nunca se registra en el sistema de admin/view-descriptor ni se serializa
    en ningún endpoint."""

    __tablename__ = "meta_tokens"
    __table_args__ = (
        UniqueConstraint(
            "connection_id", "kind", "subject_id", name="uq_meta_tokens_connection_kind_subject"
        ),
        {"schema": "meta_private"},
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    connection_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("meta_connections.id", ondelete="CASCADE")
    )
    kind: Mapped[str] = mapped_column(Text)
    subject_id: Mapped[str] = mapped_column(Text)
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary)
    iv: Mapped[bytes] = mapped_column(LargeBinary)
    auth_tag: Mapped[bytes] = mapped_column(LargeBinary)
    key_id: Mapped[str] = mapped_column(Text)
    token_hash: Mapped[str] = mapped_column(Text)
    scopes: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_validated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

# ---------------------------------------------------------------- evals (ADR-0051)
class EvalRun(Base):
    """Una corrida del golden set del tenant, atada a la versión del AgentSpec.

    Tabla de negocio (tenant_id + RLS): el runner corre con un tenant real. El
    detalle por escenario vive en `EvalResult`.
    """

    __tablename__ = "eval_runs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    spec_version: Mapped[str | None] = mapped_column(Text)
    git_ref: Mapped[str | None] = mapped_column(Text)
    passed: Mapped[int] = mapped_column(Integer, default=0)
    total: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Numeric(10, 6), default=0)
    # HU-ARCH-28 (ADR-0119): consistencia pass^k.
    k: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    pass_1: Mapped[float | None] = mapped_column(Numeric(5, 4))
    pass_k: Mapped[float | None] = mapped_column(Numeric(5, 4))
    simulator: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class EvalResult(Base):
    """Resultado de un escenario dentro de una corrida de evals."""

    __tablename__ = "eval_results"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("eval_runs.id", ondelete="CASCADE"))
    scenario: Mapped[str] = mapped_column(Text)
    ok: Mapped[bool] = mapped_column(Boolean, default=False)
    detail: Mapped[str] = mapped_column(Text, default="")
    # HU-ARCH-28 (ADR-0119): intentos, consistencia y evidencia por escenario.
    k: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    pass_1: Mapped[float | None] = mapped_column(Numeric(5, 4))
    pass_k: Mapped[float | None] = mapped_column(Numeric(5, 4))
    inestable: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    simulator: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True))
    state_assertions: Mapped[list | None] = mapped_column(JSONB(none_as_null=True))
    verifier: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True))  # HU-ARCH-29
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
