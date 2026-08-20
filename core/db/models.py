"""Modelos base del core (SQLAlchemy 2.0, tipado estricto).

Decisiones de diseño heredadas de agent-sale (probadas en producción):
- Identidad por canal: UNIQUE (channel, external_id) en customers (ADR-037 de agent-sale).
- Kill-switch en 3 niveles: settings.bot_paused, customers.bot_paused, conversations.bot_paused.
- audit_log inmutable (trigger en la migración impide UPDATE/DELETE).
- llm_usage: costo/tokens/latencia por llamada al LLM.
- Secretos SIEMPRE cifrados (columnas *_encrypted via core.shared.crypto).

Los plugins definen sus PROPIAS tablas en su propio árbol de migraciones
(ver docs/PLUGIN-CONTRACT.md § Migraciones).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


# ---------------------------------------------------------------- settings
class AppSettings(Base):
    """Singleton de configuración del asistente (una sola fila).

    Lección de agent-sale ADR-032: single-tenant por despliegue desde el día 1.
    """

    __tablename__ = "app_settings"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    assistant_name: Mapped[str] = mapped_column(Text, default="Asistente")
    business_description: Mapped[str] = mapped_column(Text, default="")
    language: Mapped[str] = mapped_column(Text, default="es")
    bot_paused: Mapped[bool] = mapped_column(Boolean, default=False)  # kill-switch global

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

    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


# ---------------------------------------------------------------- clientes / conversaciones
class Customer(Base):
    __tablename__ = "customers"
    __table_args__ = (UniqueConstraint("channel", "external_id", name="uq_customer_channel_external"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    channel: Mapped[str] = mapped_column(Text)          # "whatsapp" | "instagram" | ...
    external_id: Mapped[str] = mapped_column(Text)      # "whatsapp:+573001234567" | IGSID
    contact_phone: Mapped[str | None] = mapped_column(Text)  # cruce entre canales
    name: Mapped[str | None] = mapped_column(Text)
    bot_paused: Mapped[bool] = mapped_column(Boolean, default=False)  # kill-switch por cliente
    # Entity Memory (Capa 2): atributos estructurados que los plugins actualizan
    attributes: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ChannelConnection(Base):
    """Conexión canal × proveedor (ADR-029 de agent-sale). Credenciales cifradas, editables sin reiniciar."""

    __tablename__ = "channel_connections"
    __table_args__ = (UniqueConstraint("provider", "external_id", name="uq_connection_provider_external"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    channel: Mapped[str] = mapped_column(Text)     # "whatsapp"
    provider: Mapped[str] = mapped_column(Text)    # "meta"
    label: Mapped[str] = mapped_column(Text, default="")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    is_primary: Mapped[bool] = mapped_column(Boolean, default=False)
    external_id: Mapped[str] = mapped_column(Text)  # phone_number_id de Meta (clave de ruteo)
    credentials_encrypted: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Conversation(Base):
    __tablename__ = "conversations"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    customer_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("customers.id"))
    status: Mapped[str] = mapped_column(Text, default="open")  # open | escalated | closed
    state: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    # {"step": ..., "turnos_sin_resolver": 0, ...} — actualizado por el orchestrator, no por el LLM
    channel: Mapped[str] = mapped_column(Text, default="whatsapp")
    connection_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("channel_connections.id"))
    bot_paused: Mapped[bool] = mapped_column(Boolean, default=False)  # kill-switch por conversación
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    conversation_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("conversations.id"))
    direction: Mapped[str] = mapped_column(Text)      # inbound | outbound
    sender_type: Mapped[str] = mapped_column(Text)    # customer | agent | human
    content: Mapped[str] = mapped_column(Text, default="")
    tool_calls: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB)
    # Soporte de medios (mod-media-processor los transforma antes del orchestrator)
    media_type: Mapped[str | None] = mapped_column(Text)          # "audio/ogg", "image/jpeg"
    media_url: Mapped[str | None] = mapped_column(Text)           # URL original (auditoría)
    media_transcription: Mapped[str | None] = mapped_column(Text)  # resultado STT/vision
    idempotency_key: Mapped[str | None] = mapped_column(Text, unique=True)  # message_id del canal
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# ---------------------------------------------------------------- plugins
class PluginRecord(Base):
    """Registro de plugins detectados por entry_points y su ciclo de vida.

    Estados: detected -> configured -> active -> disabled | error
    """

    __tablename__ = "plugins"

    id: Mapped[str] = mapped_column(Text, primary_key=True)  # "mod_crm_engine"
    name: Mapped[str] = mapped_column(Text)
    version: Mapped[str] = mapped_column(Text, default="0.0.0")
    status: Mapped[str] = mapped_column(Text, default="detected")
    error_message: Mapped[str | None] = mapped_column(Text)
    installed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PluginConfig(Base):
    """Configuración de cada plugin: JSON cifrado con Fernet (puede contener SecretStr)."""

    __tablename__ = "plugin_configs"

    plugin_id: Mapped[str] = mapped_column(ForeignKey("plugins.id"), primary_key=True)
    config_encrypted: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


# ---------------------------------------------------------------- admin
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
    """Sesión con revocación inmediata: cada request valida admins.active (ADR-025 de agent-sale)."""

    __tablename__ = "admin_sessions"

    token: Mapped[str] = mapped_column(Text, primary_key=True)
    admin_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("admins.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


# ---------------------------------------------------------------- escalamiento
class Escalation(Base):
    """Handoff a humano — mecanismo NATIVO del core (los plugins lo disparan vía MCPHost).

    Al crear una escalación el core pausa el bot para esa conversación
    (lección de la Fase 18 de agent-sale: bug del bot que seguía respondiendo).
    """

    __tablename__ = "escalations"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    conversation_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("conversations.id"))
    reason: Mapped[str] = mapped_column(Text)   # keyword | guardrail | plugin | llm_refusal | manual
    summary: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(Text, default="pendiente")  # pendiente | asignada | resuelta
    assigned_admin_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("admins.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


# ---------------------------------------------------------------- observabilidad
class LLMUsage(Base):
    __tablename__ = "llm_usage"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("conversations.id"))
    provider: Mapped[str] = mapped_column(Text)
    model: Mapped[str] = mapped_column(Text)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Numeric(10, 6), default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AuditLog(Base):
    """Fuente de verdad de lo que las tools HICIERON (no lo que el LLM propuso). Inmutable."""

    __tablename__ = "audit_log"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("conversations.id"))
    actor: Mapped[str] = mapped_column(Text)    # "tool:crear_lead" | "core:guardrail" | "admin:email"
    action: Mapped[str] = mapped_column(Text)
    input: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    output: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# ---------------------------------------------------------------- memoria vectorial (Capa 3)
class MemoryVector(Base):
    __tablename__ = "memory_vectors"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    customer_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("customers.id"))
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("conversations.id"))
    content: Mapped[str] = mapped_column(Text)
    embedding: Mapped[Any] = mapped_column(Vector(384))  # dim = EMBEDDING_DIMENSIONS
    meta: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
