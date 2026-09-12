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
from datetime import UTC, datetime
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


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

    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
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
    label: Mapped[str] = mapped_column(Text, default="")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    is_primary: Mapped[bool] = mapped_column(Boolean, default=False)
    external_id: Mapped[str] = mapped_column(Text)  # phone_number_id de Meta (clave de ruteo)
    credentials_encrypted: Mapped[str | None] = mapped_column(Text)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


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
    bot_paused: Mapped[bool] = mapped_column(Boolean, default=False)  # kill-switch por conversación
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
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    conversation_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("conversations.id"))
    direction: Mapped[str] = mapped_column(Text)      # inbound | outbound
    sender_type: Mapped[str] = mapped_column(Text)    # customer | agent | human
    content: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(Text, default="sent")  # sent | failed (outbound)
    tool_calls: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB)
    # Soporte de medios (mod-media-processor los transforma antes del orchestrator)
    media_type: Mapped[str | None] = mapped_column(Text)          # "audio/ogg", "image/jpeg"
    media_url: Mapped[str | None] = mapped_column(Text)           # URL original (auditoría)
    media_transcription: Mapped[str | None] = mapped_column(Text)  # resultado STT/vision
    idempotency_key: Mapped[str | None] = mapped_column(Text)  # message_id del canal (UNIQUE parcial por tenant)
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
    role: Mapped[str] = mapped_column(Text, default="master")  # master | colaborador
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AdminSession(Base):
    """Sesión con revocación inmediata: cada request valida admins.active."""

    __tablename__ = "admin_sessions"

    token: Mapped[str] = mapped_column(Text, primary_key=True)
    admin_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("admins.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


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


class ProcessedEvent(Base):
    """Claim de idempotencia (ADR-0007) revocable (ADR-0013).

    Índice único parcial (tenant_id, external_message_id) WHERE deleted_at IS NULL:
    el reintento manual del DLQ hace soft-delete del claim y re-inserta uno fresco.
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
    embedding: Mapped[Any] = mapped_column(Vector(384))  # dim = EMBEDDING_DIMENSIONS
    meta: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)