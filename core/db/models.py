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
from decimal import Decimal
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    LargeBinary,
    Numeric,
    SmallInteger,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from core.config import get_settings
from core.shared.turno import trace_id_de_fila, turn_id_de_fila


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
# Nombre del asistente cuando el negocio no puso uno (lo usa también el aviso fuera de horario).
DEFAULT_ASSISTANT_NAME = "Asistente"


class AppSettings(Base):
    """Configuración del asistente POR TENANT (una fila por tenant).

    En pooled, el antiguo singleton queda atrás (ADR-0001): cada tenant tiene su
    propia identidad, BYOK, tono y kill-switch. El kill-switch global es scope de
    sistema (Redis/env), no una fila aquí.
    """

    __tablename__ = "app_settings"
    __table_args__ = (
        UniqueConstraint("tenant_id", name="uq_app_settings_tenant"),
        CheckConstraint(
            "vision_mode in ('describir','sin_descripcion')",
            name="ck_app_settings_vision_mode",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    assistant_name: Mapped[str] = mapped_column(Text, default=DEFAULT_ASSISTANT_NAME)
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

    # Multimedia entrante (SPEC-04, ADR-0151): proveedor/modelo/llave por negocio
    # para transcribir notas de voz y describir imágenes. NULL = fallback del .env.
    # La llave propia se cifra con SecretBox y nunca sale por la API.
    audio_enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    audio_provider: Mapped[str | None] = mapped_column(Text)
    audio_model: Mapped[str | None] = mapped_column(Text)
    audio_api_key_encrypted: Mapped[str | None] = mapped_column(Text)
    vision_enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    # describir = se llama al modelo de visión; sin_descripcion = el agente solo acusa recibo.
    vision_mode: Mapped[str] = mapped_column(
        Text, default="describir", server_default="describir"
    )
    vision_provider: Mapped[str | None] = mapped_column(Text)
    vision_model: Mapped[str | None] = mapped_column(Text)
    vision_api_key_encrypted: Mapped[str | None] = mapped_column(Text)

    # Comportamiento
    behavior_config: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    # {"debounce_mode": "natural", "split_style": "pocos_cortos", "guardrail_max_retries": 1, ...}
    # Inventario de claves: SPEC-37 de solvershub-docs. `tono` ya no se escribe ni se lee
    # (HU-CFG-01): la voz es `persona.tone` del AgentSpec.
    brand_voice: Mapped[str] = mapped_column(Text, default="")
    escalation_config: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    # {"keywords": ["reclamo", "asesor"], "business_hours": {"tz": ..., "ranges": [...]},
    #  "out_of_hours_message": "...", "auto_escalate_after_turns": 3}

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
    # Conexión canónica (ADR-0115, migración 0070): la fila de un número de Meta apunta a su
    # cuenta (`meta_connections`) y no guarda credenciales (CHECK `ck_cc_meta_sin_credenciales`).
    # NULL = conexión sin cuenta de Meta (manual, `waweb`, correo, calendario).
    meta_connection_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    # Configuración no secreta de la conexión (0070). Hoy la usa el chat web (`webchat`):
    # `{"allowed_origins": [...]}` (ADR-0115 §4, `chat_web_conexion`).
    settings: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default=text("'{}'::jsonb"))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


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
        # Migración 0082 (HU-AOS-09): forma mínima del mensaje interactivo y solo en un outbound
        # que no es plantilla.
        CheckConstraint(
            "interactive IS NULL OR (jsonb_typeof(interactive) = 'object' "
            "AND interactive->>'type' IN ('button','list') AND jsonb_typeof(interactive->'body') = 'string' "
            "AND direction = 'outbound' AND template_name IS NULL)",
            name="ck_messages_interactive_objeto",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    conversation_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("conversations.id"))
    direction: Mapped[str] = mapped_column(Text)      # inbound | outbound
    sender_type: Mapped[str] = mapped_column(Text)    # customer | agent | human
    content: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(Text, default="sent")  # queued|sent|delivered|read|failed (outbound)
    # Turno que causó la fila (HU-AOS-10): default del turno vigente, NULL fuera de un turno.
    turn_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), default=turn_id_de_fila)
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
    # Mensaje interactivo de WhatsApp (HU-AOS-09, migración 0082): el objeto `interactive` de
    # Graph sin el destinatario (`{"type": "button", "body", "buttons": [{"id", "title"}]}`).
    # `content` guarda el cuerpo para el panel.
    interactive: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# ---------------------------------------------------------------- estado de la conversación (ADR-0114)
class ConversationTask(Base):
    """Tarea: instancia de un procedimiento en una conversación (HU-AOS-04).

    Es la PROYECCIÓN de `task_events`: solo la escribe `core/kernel/state.py`, en la misma
    transacción que el evento, y se puede reconstruir desde los eventos. Las FKs compuestas
    `(tenant_id, …)` viven en la migración 0069. Sin `deleted_at`: es registro del negocio,
    como `messages` (ADR-0053); solo `purge_tenant` la borra.

    `slots`: `{nombre: {value, source, actor, seq, turn_id, confirmed, data_class}}`.
    `status`: active | suspended | completed | cancelled | failed | escalated.
    """

    __tablename__ = "conversation_tasks"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    conversation_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    procedure_id: Mapped[str] = mapped_column(Text, nullable=False)
    procedure_version: Mapped[str] = mapped_column(Text, nullable=False)
    owner_instance_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    parent_task_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    status: Mapped[str] = mapped_column(Text, nullable=False, default="active")
    stage: Mapped[str] = mapped_column(Text, nullable=False)
    slots: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    stack_position: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0)
    last_seq: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    started_turn_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    pending_confirmation: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True))  # 0074
    status_reason: Mapped[str | None] = mapped_column(Text)  # 0074
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class TaskEvent(Base):
    """Evento de una tarea: append-only (`prevent_mutation()`), fuente de verdad (ADR-0114)."""

    __tablename__ = "task_events"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    task_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    type: Mapped[str] = mapped_column(Text, nullable=False)
    data: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    actor: Mapped[str] = mapped_column(Text, nullable=False)  # llm | kernel | tool | human | backoffice
    turn_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
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
    # Perfil de la cuenta (sección Equipo, D-SPEC-34): nombre completo, teléfono con indicativo
    # y foto de perfil (editable desde el perfil del propio usuario).
    nombre: Mapped[str | None] = mapped_column(Text)
    telefono: Mapped[str | None] = mapped_column(Text)
    avatar_url: Mapped[str | None] = mapped_column(Text)
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


class AccountDevice(Base):
    """Equipo de una cuenta en un negocio (ADR-0174, SPEC-42, migración 0113).

    Tabla de NEGOCIO: tenant_id + RLS forzada (invariante 1). La clave es la cuenta (`admin_id`),
    no el perfil de gestor del pack: dos cuentas pueden registrar el mismo `device_id` sin chocar
    (el único parcial es por cuenta). La baja es lógica (`deleted_at`, invariante 8).
    """

    __tablename__ = "account_devices"
    __table_args__ = (
        Index(
            "uq_account_devices_cuenta_equipo",
            "tenant_id",
            "admin_id",
            "device_id",
            unique=True,
            postgresql_where=text("deleted_at is null and device_id is not null"),
        ),
        Index(
            "uq_account_devices_legacy_pack",
            "tenant_id",
            "legacy_pack_device_id",
            unique=True,
            postgresql_where=text("legacy_pack_device_id is not null"),
        ),
        Index("ix_account_devices_estado", "tenant_id", "estado"),
        Index("ix_account_devices_equipo", "tenant_id", "device_id"),
        Index("ix_account_devices_cuenta", "tenant_id", "admin_id"),
        CheckConstraint("plataforma in ('android', 'ios')", name="ck_account_devices_plataforma"),
        CheckConstraint(
            "estado in ('pendiente', 'activo', 'bloqueado')", name="ck_account_devices_estado"
        ),
        CheckConstraint(
            "origen in ('autoregistro', 'politica_abierta', 'alta_manual', 'migrado_pack')",
            name="ck_account_devices_origen",
        ),
        CheckConstraint("device_id is not null or legacy", name="ck_account_devices_equipo"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    admin_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("admins.id"), nullable=False)
    device_id: Mapped[str | None] = mapped_column(Text)
    device_label: Mapped[str | None] = mapped_column(Text)
    plataforma: Mapped[str] = mapped_column(Text, default="android")
    estado: Mapped[str] = mapped_column(Text, default="pendiente")  # pendiente|activo|bloqueado
    origen: Mapped[str] = mapped_column(Text, default="autoregistro")
    rol_al_registrar: Mapped[str | None] = mapped_column(Text)
    bloqueo_equipo: Mapped[bool] = mapped_column(Boolean, default=False)
    legacy: Mapped[bool] = mapped_column(Boolean, default=False)
    legacy_pack_device_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    ultimo_uso_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ultima_ip: Mapped[str | None] = mapped_column(Text)
    aprobado_por: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    aprobado_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    bloqueado_por: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    bloqueado_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    motivo: Mapped[str | None] = mapped_column(Text)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class AccessEvent(Base):
    """Evento de acceso de una cuenta o equipo (ADR-0174, migración 0113).

    Tabla de NEGOCIO, append-only: un trigger (`prevent_mutation('deleted_at')`) rechaza todo
    UPDATE salvo la baja lógica por retención. `detalle` no lleva contraseñas ni tokens.
    """

    __tablename__ = "access_events"
    __table_args__ = (
        Index("ix_access_events_feed", "tenant_id", text("occurred_at desc"), text("id desc")),
        Index("ix_access_events_severidad", "tenant_id", "severidad", text("occurred_at desc")),
        Index("ix_access_events_cuenta", "tenant_id", "admin_id", text("occurred_at desc")),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    admin_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    device_row_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    device_id: Mapped[str | None] = mapped_column(Text)
    rol: Mapped[str | None] = mapped_column(Text)
    canal: Mapped[str] = mapped_column(Text, default="campo")
    tipo: Mapped[str] = mapped_column(Text)
    severidad: Mapped[str] = mapped_column(Text, default="info")
    ip: Mapped[str | None] = mapped_column(Text)
    detalle: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    request_id: Mapped[str | None] = mapped_column(Text)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Notification(Base):
    """Notificación de un evento del negocio (feed in-app + push, ADR-0167).

    Una fila POR EVENTO (no por destinatario): quién la ve se resuelve al leer, por
    permiso de la `section` (dueño y master ven todo; un colaborador, solo si tiene acceso
    explícito a la sección). El estado de lectura de cada admin vive en `notification_reads`
    y `notification_state`. Tabla de NEGOCIO: tenant_id + RLS forzada (invariante 1).
    """

    __tablename__ = "notifications"
    __table_args__ = (
        Index(
            "uq_notifications_tenant_dedupe",
            "tenant_id",
            "dedupe_key",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
        Index("ix_notifications_tenant_created", "tenant_id", text("created_at DESC")),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    event_key: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str] = mapped_column(Text, nullable=False)
    severity: Mapped[str] = mapped_column(Text, nullable=False)
    section: Mapped[str | None] = mapped_column(Text)
    # Dirigida a una persona (p. ej. la cita asignada): además de dueños y masters, la ve ella
    # aunque no tenga acceso a la sección.
    audience_admin_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    title: Mapped[str] = mapped_column(Text, nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False, default="")
    data: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    target: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    dedupe_key: Mapped[str] = mapped_column(Text, nullable=False)
    push_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class NotificationState(Base):
    """Preferencias y marca de «leído hasta» de un admin en un negocio (ADR-0167).

    Sin fila = todo activo. `read_all_before` es la marca de «marcar todas como leídas»;
    `push_off_categories` y `muted_until` solo gobiernan el push (el feed siempre muestra).
    """

    __tablename__ = "notification_state"
    __table_args__ = (
        Index("uq_notification_state_admin", "tenant_id", "admin_id", unique=True),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    admin_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    read_all_before: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    muted_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    push_off_categories: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, default=list
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class NotificationRead(Base):
    """Notificación leída o archivada por un admin (solo existen filas de lo tocado)."""

    __tablename__ = "notification_reads"
    __table_args__ = (
        Index(
            "uq_notification_reads_admin_notif",
            "tenant_id",
            "admin_id",
            "notification_id",
            unique=True,
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    admin_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    notification_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


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
    con valor = solo ese tenant. `access` es `read | write | none`; `none` (denegado,
    ADR-0136) solo existe con `tenant_id` (CHECK de la migración 0090).
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
        CheckConstraint(
            "access in ('read', 'write') or (access = 'none' and tenant_id is not null)",
            name="ck_admin_permissions_access",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    admin_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("admins.id"))
    section: Mapped[str] = mapped_column(Text)
    access: Mapped[str] = mapped_column(Text, default="read")  # read | write | none
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
    # Nonce del enlace de un solo uso (ADR-0121): el token se deriva de él y del id con
    # APP_SECRET_KEY; la base nunca guarda el token. NULL = invitación sin enlace (ADR-0081).
    token_nonce: Mapped[str | None] = mapped_column(Text, default=None)


class ProvisioningRequest(Base):
    """Alta automática de un negocio pedida por un negocio vendedor (HU-SH-03, ADR-0121).

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
        # HU-SH-04: el negocio creado → su suscripción en el vendedor (canal asistente).
        Index("ix_provisioning_requests_tenant_negocio", "tenant_id", "negocio_tenant_id"),
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


class DocumentoLegal(Base):
    """Versión publicada de un documento legal de un negocio vendedor (HU-SH-07, ADR-0125).

    Tabla de NEGOCIO del vendedor (`tenant_id`, RLS forzada). `tipo`: `terminos | privacidad |
    dpa`. Las filas no se editan: una versión nueva es una fila nueva, y la vigente de cada
    tipo es la de `vigente_desde` más reciente que ya empezó. Así, cuando llega el texto del
    abogado, se publica otra versión (desde Plataforma) sin tocar código, y cada aceptación
    apunta a la versión que se mostró.
    """

    __tablename__ = "documentos_legales"
    __table_args__ = (
        Index(
            "uq_documentos_legales_tenant_tipo_version",
            "tenant_id",
            "tipo",
            "version",
            unique=True,
            postgresql_where=text("deleted_at is null"),
        ),
        Index("ix_documentos_legales_tenant_tipo_vigente", "tenant_id", "tipo", "vigente_desde"),
        CheckConstraint("tipo in ('terminos', 'privacidad', 'dpa')", name="ck_documentos_legales_tipo"),
        CheckConstraint("version ~ '^[A-Za-z0-9][A-Za-z0-9._-]{0,39}$'", name="ck_documentos_legales_version"),
        CheckConstraint("url ~ '^https://[^[:space:]]+$' and char_length(url) <= 500", name="ck_documentos_legales_url"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    tipo: Mapped[str] = mapped_column(Text, nullable=False)
    version: Mapped[str] = mapped_column(Text, nullable=False)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    vigente_desde: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    publicado_por: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)


class PedidoDeAlta(Base):
    """Pedido de un plan de la plataforma hecho a un negocio vendedor (HU-SH-07, ADR-0125).

    Tabla de NEGOCIO del vendedor (`tenant_id`, RLS forzada): una fila por pedido
    (`referencia` = número público del pedido), escrita en la misma transacción que el pedido.
    Guarda lo mínimo para el antiabuso y el cumplimiento:

    - `huellas`: HMAC del teléfono y del documento (`tel:<hex>`, `doc:<hex>`), nunca el dato.
      Sirven para «un Free por persona» y «muchos intentos»; se vacían a los 12 meses.
    - `senales`: códigos de datos inconsistentes vistos en el checkout (p. ej.
      `documento_invalido`), sin el dato.
    - `aceptacion` + `aceptado_en`: qué versiones de términos, privacidad y DPA aceptó, cuándo
      y en qué mensaje. Sin aceptación (no había documentos publicados), el alta va a revisión.
    """

    __tablename__ = "pedidos_de_alta"
    __table_args__ = (
        Index(
            "uq_pedidos_de_alta_tenant_referencia",
            "tenant_id",
            "referencia",
            unique=True,
            postgresql_where=text("deleted_at is null"),
        ),
        # Las reglas miran 24 h o 12 meses de un vendedor: decenas de filas por día.
        Index("ix_pedidos_de_alta_tenant_creado", "tenant_id", "created_at"),
        Index("ix_pedidos_de_alta_tenant_cliente", "tenant_id", "customer_id", "created_at"),
        CheckConstraint(
            "(aceptacion is null) = (aceptado_en is null)", name="ck_pedidos_de_alta_aceptacion"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    referencia: Mapped[str] = mapped_column(Text, nullable=False)
    customer_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), default=None)
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), default=None)
    canal: Mapped[str | None] = mapped_column(Text, default=None)
    gratis: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    huellas: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, default=list)
    senales: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, default=list)
    # none_as_null: sin aceptación es NULL de SQL (no el JSON `null`), por el CHECK.
    aceptacion: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True), default=None)
    aceptado_en: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)


class OnboardingHito(Base):
    """Primera vez que un negocio completó un paso del onboarding (HU-SH-06, ADR-0124).

    Tabla de NEGOCIO (`tenant_id`, RLS forzada): una fila por `(tenant_id, paso)` mientras no
    esté borrada. El estado de los primeros pasos no se guarda (se calcula del estado real,
    `core/primeros_pasos`); esto solo fija cuándo se cumplió cada uno, para el embudo de
    activación. `fuente`: `dato` si la hora sale del propio dato (la conexión, el primer
    producto) o `deteccion` si es la hora en que el core lo vio por primera vez.
    """

    __tablename__ = "onboarding_hitos"
    __table_args__ = (
        Index(
            "uq_onboarding_hitos_tenant_paso",
            "tenant_id",
            "paso",
            unique=True,
            postgresql_where=text("deleted_at is null"),
        ),
        CheckConstraint("paso ~ '^[a-z][a-z0-9_]{0,63}$'", name="ck_onboarding_hitos_paso"),
        CheckConstraint("segundos_desde_alta >= 0", name="ck_onboarding_hitos_segundos"),
        CheckConstraint("fuente in ('dato', 'deteccion')", name="ck_onboarding_hitos_fuente"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    paso: Mapped[str] = mapped_column(Text, nullable=False)
    completado_en: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    segundos_desde_alta: Mapped[int] = mapped_column(BigInteger, nullable=False)
    fuente: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
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
    reason: Mapped[str] = mapped_column(Text)   # crudo; código legible en core/orchestrator/motivos_escalacion.py
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
    turn_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), default=turn_id_de_fila)
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
    # Desde la migración 0114 (HU-AOS-35): solo lo LEÍDO de la caché del proveedor, y
    # `input_tokens` es el total de entrada con lo cacheado incluido. Antes, lectura + escritura.
    cached_tokens: Mapped[int] = mapped_column(Integer, default=0)
    # Lo ESCRITO en la caché (Anthropic). NULL en filas anteriores a la 0114: marca el corte.
    cache_write_tokens: Mapped[int | None] = mapped_column(Integer)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Numeric(10, 6), default=0)
    tool_calls: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    # Turno y traza (HU-AOS-10): default del turno vigente, NULL fuera de un turno.
    turn_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), default=turn_id_de_fila)
    trace_id: Mapped[str | None] = mapped_column(Text, default=trace_id_de_fila)
    # Release y agente del turno (migración 0070, HU-AOS-15): NULL con `aos_runtime = legacy`.
    connection_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    agent_instance_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    agent_version_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    release_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    # Tipo de fila (migración 0070, ADR-0116/0117): `turn` | `agent_run` | … | `eval`.
    kind: Mapped[str] = mapped_column(Text, default="turn")
    # Llamada A2A que originó el gasto (`kind = 'agent_run'`, HU-AOS-18): el `call_id` de `a2a_events`.
    a2a_call_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    # Crédito mensual (migración 0096, ADR-0144): créditos del turno calculados AL ESCRIBIR con
    # el tarifario y la TRM vigentes, que se guardan con la fila. NULL en filas anteriores.
    credits: Mapped[Decimal | None] = mapped_column(Numeric(12, 1))
    key_mode: Mapped[str | None] = mapped_column(Text)  # platform | byok
    tarifario_version: Mapped[str | None] = mapped_column(Text)
    trm_cop: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
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

    Es la FUENTE ÚNICA del cobro de la plataforma (D-13, ADR-0123): el pago por el
    asistente y por el panel actualizan esta fila. `dia_ancla`: día del mes en que
    empiezan los períodos pagos (calendario, como `pack_subscriptions`).
    """

    __tablename__ = "tenant_subscriptions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    plan_id: Mapped[str] = mapped_column(ForeignKey("plans.id"))
    status: Mapped[str] = mapped_column(Text, default="trialing")
    period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    period_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    trial_ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    dia_ancla: Mapped[int | None] = mapped_column(SmallInteger, default=None)
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
    `status`: pendiente | aprobado | rechazado | anulado | duplicado.

    `periodo` (ADR-0123): referencia del período que cubre (`plat-<slug>-<AAAAMM>`), la
    misma en los dos canales. Un período tiene a lo sumo UN pago `aprobado` (índice único
    parcial); un segundo pago del mismo período queda `duplicado` y se alerta. `anulado` =
    intento pendiente que dejó de servir porque el período lo pagó otro intento o canal.
    """

    __tablename__ = "subscription_payments"
    __table_args__ = (
        Index("ix_subscription_payments_tenant_periodo", "tenant_id", "periodo"),
        Index(
            "uq_subscription_payments_periodo_aprobado",
            "tenant_id",
            "periodo",
            unique=True,
            postgresql_where=text("status = 'aprobado' and periodo is not null and deleted_at is null"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    subscription_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenant_subscriptions.id"))
    plan_id: Mapped[str] = mapped_column(ForeignKey("plans.id"))
    amount_cop: Mapped[float] = mapped_column(Numeric(12, 2), default=0)
    currency: Mapped[str] = mapped_column(Text, default="COP")
    status: Mapped[str] = mapped_column(Text, default="pendiente")
    provider: Mapped[str] = mapped_column(Text)
    provider_reference: Mapped[str] = mapped_column(Text)
    periodo: Mapped[str | None] = mapped_column(Text, default=None)
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
    # Memoria por scope (0080, ADR-0114 §3): customer | connection | tenant (`ck_mv_keys`).
    scope: Mapped[str] = mapped_column(Text, nullable=False, default="customer")
    connection_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    agent_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))  # None = compartida
    kind: Mapped[str] = mapped_column(Text, nullable=False, default="exchange")  # exchange|episode|fact|document
    importance: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=1)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    embedding_model: Mapped[str | None] = mapped_column(Text)
    source_turn_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class MemoryFact(Base):
    """Hecho del cliente con procedencia (0069 + 0080, ADR-0114 §3).

    Es la FUENTE de la memoria del cliente; `customers.attributes` es su proyección (solo el
    namespace `perfil`). Se escribe SOLO en `core/memory/facts.py`, en la misma transacción que
    la proyección. El hecho nuevo cierra al anterior (`superseded_at`); el olvido es lógico.
    """

    __tablename__ = "memory_facts"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    customer_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    namespace: Mapped[str] = mapped_column(Text, nullable=False)
    key: Mapped[str] = mapped_column(Text, nullable=False)
    value: Mapped[Any] = mapped_column(JSONB(none_as_null=False), nullable=False)
    data_class: Mapped[str] = mapped_column(Text, nullable=False, default="internal")
    confidence: Mapped[Decimal] = mapped_column(Numeric(3, 2), nullable=False, default=Decimal("1.00"))
    source: Mapped[str] = mapped_column(Text, nullable=False)  # tool | agent | human | import
    written_by_instance_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    source_turn_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    source_task_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))  # 0080
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CustomerNote(Base):
    """Nota del equipo sobre un cliente (0099, ADR-0147). La ven la ficha, la línea de tiempo y el
    agente (bloque dinámico, como dato). Borrado lógico; la purga de memoria del cliente la borra."""

    __tablename__ = "customer_notes"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    customer_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    author_admin_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("admins.id", ondelete="SET NULL"))
    author_label: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class CustomerConsent(Base):
    """Consentimiento del cliente para recibir promociones (0100, ADR-0150). Solo inserción: la
    última fila por cliente y propósito manda. Guarda quién y cómo, para poder probarlo."""

    __tablename__ = "customer_consents"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    customer_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    proposito: Mapped[str] = mapped_column(Text, nullable=False, default="promociones")
    estado: Mapped[str] = mapped_column(Text, nullable=False)  # otorgado | revocado | informado (0117)
    # ficha | palabra_de_baja | meta_131050 | aviso_en_chat (0117)
    origen: Mapped[str] = mapped_column(Text, nullable=False)
    evidencia: Mapped[str] = mapped_column(Text, nullable=False, default="")
    actor_admin_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("admins.id", ondelete="SET NULL"))
    actor_label: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # Huella del texto del aviso que se mostró (obligatoria en `tratamiento_datos`, 0117).
    version_aviso: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class SegmentSend(Base):
    """Envío de una plantilla a un segmento de Clientes (0100, ADR-0150). La vista previa lo crea
    en `borrador` con su lista de destinatarios; confirmar no cambia la lista."""

    __tablename__ = "segment_sends"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    admin_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("admins.id", ondelete="SET NULL"))
    admin_label: Mapped[str] = mapped_column(Text, nullable=False, default="")
    connection_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    template_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    template_name: Mapped[str] = mapped_column(Text, nullable=False)
    template_language: Mapped[str] = mapped_column(Text, nullable=False)
    variables: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    filtro: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    excluidos: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    total: Mapped[int] = mapped_column(Integer, default=0)
    estado: Mapped[str] = mapped_column(Text, default="borrador")  # borrador|confirmado|enviando|terminado
    idempotency_key: Mapped[str | None] = mapped_column(Text)
    request_hash: Mapped[str | None] = mapped_column(Text)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class SegmentSendRecipient(Base):
    """Un destinatario de un envío a un segmento: la instantánea y la cola durable del envío."""

    __tablename__ = "segment_send_recipients"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    send_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    customer_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    conversation_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    estado: Mapped[str] = mapped_column(Text, default="pendiente")  # pendiente | encolado | omitido
    motivo: Mapped[str | None] = mapped_column(Text)
    message_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


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


class PlatformMetaAppRequirement(Base):
    """Requisito de Meta de la app de la PLATAFORMA para Embedded Signup que el master confirma
    a mano (verificación del negocio, App Review, Tech Provider, configuración, modo Live), con
    quién y cuándo (HU-SH-05, migración 0066).

    Tabla de SISTEMA (sin RLS, sin `tenant_id`): es de la app compartida por todos los negocios,
    no de un negocio. Solo la toca `core/admin/platform_meta_es.py`, que exige master."""

    __tablename__ = "platform_meta_app_requirements"

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    done: Mapped[bool] = mapped_column(Boolean, default=False)
    notes: Mapped[str | None] = mapped_column(Text)
    updated_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("admins.id", ondelete="SET NULL"))
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
    # Borrado lógico con su conexión (ADR-0122): plantillas y precios cuelgan de esta fila.
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class PhoneNumber(Base):
    """Número de WhatsApp sincronizado desde Graph API.

    Un número vive en un solo negocio a la vez (único parcial global) y cada negocio tiene
    una sola fila por número (único completo): reconectarlo revive la fila (ADR-0122)."""

    __tablename__ = "phone_numbers"
    __table_args__ = (
        Index(
            "uq_phone_numbers_phone_number_id_vivo",
            "phone_number_id",
            unique=True,
            postgresql_where=text("deleted_at is null"),
        ),
        UniqueConstraint("tenant_id", "phone_number_id", name="uq_phone_numbers_tenant_phone"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    connection_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("meta_connections.id", ondelete="CASCADE"))
    waba_account_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("waba_accounts.id", ondelete="SET NULL")
    )
    phone_number_id: Mapped[str] = mapped_column(Text)
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
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
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
        # Por negocio (ADR-0122): las plantillas de una WABA borrada se conservan, y si otro
        # negocio la reclama, su sync trae los mismos `meta_template_id`.
        Index(
            "uq_message_templates_tenant_meta_id",
            "tenant_id",
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


class MetaProductionCheck(Base):
    """Corrida del checklist de producción de UNA conexión de Meta (HU-CONN-07).

    La pide el panel ("Producción" de la conexión) y la ejecuta el worker de Celery con las
    credenciales de esa conexión; aquí queda el resultado con fecha. Tabla de NEGOCIO:
    `tenant_id`, RLS habilitada y forzada, policy de aislamiento. Una sola corrida activa
    (`queued`/`running`) por conexión. Solo se escribe desde
    `core/admin/production_readiness_service.py`."""

    __tablename__ = "meta_production_checks"
    __table_args__ = (
        CheckConstraint(
            "status in ('queued','running','done','failed')", name="ck_meta_production_checks_status"
        ),
        Index(
            "ix_meta_production_checks_tenant_connection",
            "tenant_id",
            "connection_id",
            "requested_at",
        ),
        Index(
            "uq_meta_production_checks_activa",
            "tenant_id",
            "connection_id",
            unique=True,
            postgresql_where=text("status in ('queued','running') and deleted_at is null"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    connection_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("meta_connections.id"), nullable=False)
    status: Mapped[str] = mapped_column(Text, default="queued")
    ready: Mapped[bool | None] = mapped_column(Boolean)
    checks: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)
    error: Mapped[str | None] = mapped_column(Text)
    requested_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PlatformReadinessRun(Base):
    """Corrida del checklist de la app de Meta de la plataforma y la infraestructura
    (HU-CONN-07, "Plataforma → App de Meta", solo master).

    Tabla de SISTEMA (sin RLS, sin `tenant_id`): el resultado es de la plataforma, no de un
    negocio. Solo la leen y escriben `core/admin/production_readiness_service.py` (detrás de
    rutas master-only) y el job de Celery; no guarda secretos, solo el estado de cada check.
    Una sola corrida activa (`queued`/`running`) a la vez."""

    __tablename__ = "platform_readiness_runs"
    __table_args__ = (
        CheckConstraint(
            "status in ('queued','running','done','failed')", name="ck_platform_readiness_runs_status"
        ),
        Index("ix_platform_readiness_runs_requested_at", "requested_at"),
        # Una sola corrida activa: índice único sobre una constante, parcial a las activas.
        Index(
            "uq_platform_readiness_runs_activa",
            text("(true)"),
            unique=True,
            postgresql_where=text("status in ('queued','running')"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    status: Mapped[str] = mapped_column(Text, default="queued")
    ready: Mapped[bool | None] = mapped_column(Boolean)
    checks: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)
    error: Mapped[str | None] = mapped_column(Text)
    requested_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


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
    # HU-AOS-21 (migración 0105): la release candidata medida por el gate y el origen de la corrida.
    release_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    origen: Mapped[str | None] = mapped_column(Text)
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
    procedure_id: Mapped[str | None] = mapped_column(Text)  # HU-AOS-21 (migración 0105)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# ---------------------------------------------------------------- Agent OS: agentes (HU-AOS-12)
# Tablas creadas en 0069 (HU-AOS-02); aquí solo el mapeo de lo que usa la API de agentes.
# Los defaults de BD de estado y textos se declaran con `server_default`; las fechas usan
# `utcnow` como el resto de los modelos, y los ids se fijan en Python (el AAD de los secretos
# necesita el id de la instalación antes del INSERT).
class AgentRecord(Base):
    """Agente del negocio (ADR-0113): `kind` y la identidad viven aquí, no en el contrato."""

    __tablename__ = "agents"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    key: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)  # core | specialist | backoffice
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", server_default="")
    source_template: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, default="active", server_default="active")
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AgentDraftRecord(Base):
    """Borrador editable de un agente, con concurrencia optimista por `lock_version`."""

    __tablename__ = "agent_drafts"

    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    agent_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    content: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    base_version_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    lock_version: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    updated_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AgentVersionRecord(Base):
    """Versión inmutable de un agente (append-only: trigger `prevent_mutation`, 0069)."""

    __tablename__ = "agent_versions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    agent_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    label: Mapped[str] = mapped_column(Text, nullable=False)
    contract: Mapped[str] = mapped_column(Text, nullable=False)  # agentspec/3 | kernelspec/1
    content: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    content_hash: Mapped[str] = mapped_column(Text, nullable=False)
    legacy_agent_spec_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    notes: Mapped[str] = mapped_column(Text, default="", server_default="")
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AgentInstanceRecord(Base):
    """Instancia de un agente en una conexión conversacional (ADR-0112 §1, HU-AOS-13).

    Una `core` (el Coordinador, índice único parcial) y N especialistas, uno por defecto.
    `agent_instance_checks()` (0069) rechaza conexiones que no son conversacionales y que
    cambien negocio, conexión, agente o rol; el rol coincide con el `kind` del agente por FK."""

    __tablename__ = "agent_instances"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    connection_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    agent_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    role: Mapped[str] = mapped_column(Text, nullable=False)  # core | specialist | backoffice
    is_default: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    priority: Mapped[int] = mapped_column(SmallInteger, default=100, server_default="100")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    overrides: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default=text("'{}'::jsonb"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AgentRecordOrigin(Base):
    """Procedencia de un registro que creó una acción del agente (HU-AOS-47, ADR-0140, 0095).

    Append-only (`prevent_mutation()`). El registro vive en la tabla de su pack; aquí solo su
    referencia, la referencia pública opcional (`label`) y quién lo creó."""

    __tablename__ = "agent_record_origins"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    pack_id: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    record_ref: Mapped[str] = mapped_column(Text, nullable=False)
    label: Mapped[str | None] = mapped_column(Text)
    tool: Mapped[str] = mapped_column(Text, nullable=False)
    agent_instance_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    agent_version_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    connection_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    turn_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), default=turn_id_de_fila)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class TestDataRecord(Base):
    """Registro sembrado por «Generar datos de prueba» (HU-QA-10, ADR-0146, 0098).

    El registro vive en la tabla de su pack; aquí su referencia lógica, el módulo, el agente
    (si lo hay) y el lote. Retirar marca `deleted_at` (invariante 8). `origen`: `llm` | `plantilla` | `importacion` (HU-QA-12: lo que el dueño importó de un CSV).
    `agent_id` es nulo cuando la generación se hizo desde un módulo (incluido el onboarding)."""

    __tablename__ = "test_data_records"
    __test__ = False  # pytest no la confunde con una clase de tests

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    agent_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    modulo: Mapped[str | None] = mapped_column(Text)
    lote_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    pack_id: Mapped[str] = mapped_column(Text, nullable=False)
    modo: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    record_ref: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    origen: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PackInstallationRecord(Base):
    """Habilidad instalada con scope (ADR-0118 §2). En esta HU solo se usa la instalación
    `core` de scope negocio, que guarda los secretos de las `declarativeTools` (ADR-0118 §5):
    no habilita nada y no cuenta para el tope del plan. `config_encrypted` lleva AAD
    `(tenant_id, installation_id)` (`core.agents.secretos.aad_instalacion`)."""

    __tablename__ = "pack_installations"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    pack_id: Mapped[str] = mapped_column(Text, nullable=False)
    scope: Mapped[str] = mapped_column(Text, nullable=False)  # tenant | connection
    connection_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    status: Mapped[str] = mapped_column(Text, default="pending_config", server_default="pending_config")
    config_encrypted: Mapped[str | None] = mapped_column(Text)
    installed_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    installed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


# ---------------------------------------------------------------- centro de pruebas (SPEC-30, ADR-0133)
# Tablas de SISTEMA (sin RLS): son de la plataforma, no de un negocio. Solo las usan
# master y el rol tester (sección global `qa`); ninguna query de negocio las toca.
# Los resultados se guardan por `item_slug` (estable entre versiones del catálogo).
class QaCatalogVersion(Base):
    """Una importación del catálogo `11-pruebas/catalogo.yaml` de solvershub-docs."""

    __tablename__ = "qa_catalog_versions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    version: Mapped[int] = mapped_column(Integer)
    fecha: Mapped[str] = mapped_column(Text, default="")
    commit_docs: Mapped[str | None] = mapped_column(Text)
    contenido_sha256: Mapped[str] = mapped_column(Text, unique=True)
    importado_por: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("admins.id"))
    importado_en: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class QaItem(Base):
    """Prueba, configuración de test o punto de producción de una versión del catálogo."""

    __tablename__ = "qa_items"
    __table_args__ = (
        UniqueConstraint("catalog_version_id", "slug", name="uq_qa_items_version_slug"),
        CheckConstraint("seccion in ('pruebas', 'test', 'prod', 'datos')", name="ck_qa_items_seccion"),
        CheckConstraint(
            "entorno is null or entorno in ('panel', 'apk', 'whatsapp', 'web', 'api')",
            name="ck_qa_items_entorno",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    catalog_version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("qa_catalog_versions.id", ondelete="CASCADE")
    )
    slug: Mapped[str] = mapped_column(Text)
    seccion: Mapped[str] = mapped_column(Text)
    orden: Mapped[int] = mapped_column(Integer, default=0)
    modulo: Mapped[str | None] = mapped_column(Text)
    grupo: Mapped[str] = mapped_column(Text, default="")
    titulo: Mapped[str] = mapped_column(Text)
    resumen: Mapped[str | None] = mapped_column(Text)
    contexto: Mapped[str | None] = mapped_column(Text)
    prioridad: Mapped[str | None] = mapped_column(Text)
    plataforma: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)
    precondiciones: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)
    datos: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)
    # [{"accion": str, "esperado": str | None}]
    pasos: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)
    esperado: Mapped[str | None] = mapped_column(Text)
    porque: Mapped[str | None] = mapped_column(Text)
    como: Mapped[str | None] = mapped_column(Text)
    vars: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)
    quien: Mapped[str | None] = mapped_column(Text)
    hu: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)
    origen: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)
    # Dónde se ejecuta (panel, apk, whatsapp, web, api) y en qué sección o pantalla (HU-QA-08).
    entorno: Mapped[str | None] = mapped_column(Text)
    area: Mapped[str | None] = mapped_column(Text)
    # Tercer nivel dentro del área (catálogo v7, HU-QA-08): reemplaza a `grupo` en las pruebas.
    subseccion: Mapped[str | None] = mapped_column(Text)
    # Slugs de la guía de datos (sección `datos`) que la prueba necesita (HU-QA-07).
    requiere: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)


class QaCycle(Base):
    """Ronda de pruebas. Un solo ciclo abierto a la vez (índice parcial en la migración)."""

    __tablename__ = "qa_cycles"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    nombre: Mapped[str] = mapped_column(Text)
    catalog_version_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("qa_catalog_versions.id"))
    abierto_por: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("admins.id"))
    abierto_en: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    cerrado_por: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("admins.id"))
    cerrado_en: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resumen: Mapped[dict[str, Any] | None] = mapped_column(JSONB)


class QaRun(Base):
    """Ejecución de un tester en un ciclo: sus pasos, notas, capturas e informe."""

    __tablename__ = "qa_runs"
    __table_args__ = (
        UniqueConstraint("cycle_id", "admin_id", name="uq_qa_runs_cycle_admin"),
        CheckConstraint("estado in ('en_curso', 'enviada')", name="ck_qa_runs_estado"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    cycle_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("qa_cycles.id"))
    admin_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("admins.id"))
    estado: Mapped[str] = mapped_column(Text, default="en_curso")
    comentario: Mapped[str | None] = mapped_column(Text)
    informe: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    iniciada_en: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    actualizada_en: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    enviada_en: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class QaStepResult(Base):
    """Resultado de un paso de una prueba en una ejecución."""

    __tablename__ = "qa_step_results"
    __table_args__ = (
        CheckConstraint(
            "estado in ('ok', 'falla', 'bloqueado', 'omitido')", name="ck_qa_step_results_estado"
        ),
    )

    run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("qa_runs.id", ondelete="CASCADE"), primary_key=True
    )
    item_slug: Mapped[str] = mapped_column(Text, primary_key=True)
    paso: Mapped[int] = mapped_column(Integer, primary_key=True)
    estado: Mapped[str | None] = mapped_column(Text)
    nota: Mapped[str | None] = mapped_column(Text)
    actualizado_en: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class QaItemResult(Base):
    """Estado de una prueba en una ejecución (derivado de sus pasos) y su nota general."""

    __tablename__ = "qa_item_results"

    run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("qa_runs.id", ondelete="CASCADE"), primary_key=True
    )
    item_slug: Mapped[str] = mapped_column(Text, primary_key=True)
    estado: Mapped[str] = mapped_column(Text, default="sin_correr")
    nota: Mapped[str | None] = mapped_column(Text)
    actualizado_en: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class QaAttachment(Base):
    """Captura de un paso. El archivo vive en `TENANTS_ROOT/.plataforma/pruebas/<run_id>/`."""

    __tablename__ = "qa_attachments"
    __table_args__ = (Index("ix_qa_attachments_run_item", "run_id", "item_slug"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("qa_runs.id", ondelete="CASCADE"))
    item_slug: Mapped[str] = mapped_column(Text)
    paso: Mapped[int] = mapped_column(Integer)
    ruta: Mapped[str] = mapped_column(Text)
    nombre_original: Mapped[str | None] = mapped_column(Text)
    mimetype: Mapped[str] = mapped_column(Text)
    tamano: Mapped[int] = mapped_column(Integer)
    subido_por: Mapped[uuid.UUID] = mapped_column(ForeignKey("admins.id"))
    subido_en: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class QaConfigStatus(Base):
    """Estado de un ítem de configuración de test o de producción (no se reinicia por ciclo)."""

    __tablename__ = "qa_config_status"
    __table_args__ = (
        CheckConstraint(
            "estado in ('pendiente', 'en_curso', 'listo', 'no_aplica')",
            name="ck_qa_config_status_estado",
        ),
    )

    item_slug: Mapped[str] = mapped_column(Text, primary_key=True)
    estado: Mapped[str] = mapped_column(Text, default="pendiente")
    nota: Mapped[str | None] = mapped_column(Text)
    actualizado_por: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("admins.id"))
    actualizado_en: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class QaEvent(Base):
    """Historial de cambios del centro de pruebas. Solo se inserta; nunca se borra."""

    __tablename__ = "qa_events"
    __table_args__ = (Index("ix_qa_events_cycle_item", "cycle_id", "item_slug", "en"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    cycle_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("qa_cycles.id"))
    run_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("qa_runs.id", ondelete="SET NULL"))
    item_slug: Mapped[str | None] = mapped_column(Text)
    paso: Mapped[int | None] = mapped_column(Integer)
    campo: Mapped[str] = mapped_column(Text)
    antes: Mapped[str | None] = mapped_column(Text)
    despues: Mapped[str | None] = mapped_column(Text)
    admin_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("admins.id"))
    en: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class QaTesterDataset(Base):
    """Negocio de prueba de un tester y sus cuentas de dueño, gestor y colaborador (HU-QA-07).

    Tabla de SISTEMA (sin RLS): de la plataforma, como el resto de `qa_*`; se consulta sin
    tenant. `tenant_id` es FK lógica (si el negocio se purga, se vuelve a preparar). Las
    contraseñas no se guardan: se muestran una sola vez.
    """

    __tablename__ = "qa_tester_datasets"
    __table_args__ = (
        UniqueConstraint("admin_id", name="uq_qa_tester_datasets_admin"),
        UniqueConstraint("slug", name="uq_qa_tester_datasets_slug"),
        CheckConstraint("estado in ('creando', 'listo', 'error')", name="ck_qa_tester_datasets_estado"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    admin_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("admins.id", ondelete="CASCADE"))
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    slug: Mapped[str] = mapped_column(Text)
    dueno_admin_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("admins.id", ondelete="SET NULL"))
    gestor_admin_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("admins.id", ondelete="SET NULL"))
    colaborador_admin_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("admins.id", ondelete="SET NULL"))
    estado: Mapped[str] = mapped_column(Text, default="creando")
    # Token de la corrida en curso: solo ella puede cerrar la fila como lista o con error.
    corrida: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    error: Mapped[str | None] = mapped_column(Text)
    creado_en: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    actualizado_en: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class QaTesterInvitation(Base):
    """Enlace de un solo uso para que un tester defina su contraseña (ADR-0134).

    Tabla de SISTEMA (sin RLS): identidad de plataforma; se consulta sin tenant.
    El token no se guarda: es `<id>.<HMAC(APP_SECRET_KEY, id + nonce)>`.
    """

    __tablename__ = "qa_tester_invitations"
    __table_args__ = (
        Index("ix_qa_tester_invitations_admin", "admin_id"),
        CheckConstraint(
            "estado in ('pendiente', 'aceptada', 'revocada', 'reemplazada')",
            name="ck_qa_tester_invitations_estado",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    admin_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("admins.id"))
    email: Mapped[str] = mapped_column(Text)
    token_nonce: Mapped[str | None] = mapped_column(Text)
    estado: Mapped[str] = mapped_column(Text, default="pendiente")
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    invitado_por: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("admins.id"))
    creada_en: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    respondida_en: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


# ---------------------------------------------------------------- opciones directas (SPEC-40, ADR-0172, migración 0112)
class OptionTemplate(Base):
    """Plantilla con opciones del negocio. El borrador vive en `draft_definition`; cada publicación crea una
    fila inmutable en `option_template_versions`. Estados: `draft`, `published`, `retired`. Única tabla del
    módulo con borrado lógico (las otras tres son historia de solo avance, la borra `purge_tenant`).
    Tabla de NEGOCIO: tenant_id + RLS forzada (invariante 1)."""

    __tablename__ = "option_templates"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_option_templates_tenant_id_id"),
        Index("uq_option_templates_tenant_key", "tenant_id", "key", unique=True, postgresql_where=text("deleted_at IS NULL")),
        CheckConstraint("status in ('draft','published','retired')", name="ck_option_templates_status"),
        CheckConstraint("key ~ '^[a-z0-9_]{1,40}$'", name="ck_option_templates_key"),
        CheckConstraint("current_version_n is null or current_version_n >= 1", name="ck_option_templates_version"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    key: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False, default="")
    status: Mapped[str] = mapped_column(Text, nullable=False, default="draft")
    draft_definition: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    current_version_n: Mapped[int | None] = mapped_column(Integer)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class OptionTemplateVersion(Base):
    """Versión PUBLICADA de una plantilla con opciones: inmutable (trigger `prevent_mutation`), salvo
    `message_template_id` (se fija al publicar en Meta como `<clave>_v<n>`) y `retired_at`."""

    __tablename__ = "option_template_versions"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_option_template_versions_tenant_id_id"),
        UniqueConstraint("tenant_id", "option_template_id", "version_n", name="uq_option_template_versions_n"),
        ForeignKeyConstraint(
            ["tenant_id", "option_template_id"],
            ["option_templates.tenant_id", "option_templates.id"],
            name="fk_option_versions_template",
        ),
        Index("ix_option_versions_tenant_message_template", "tenant_id", "message_template_id"),
        CheckConstraint("version_n >= 1", name="ck_option_versions_n"),
        CheckConstraint("definition_hash ~ '^[0-9a-f]{64}$'", name="ck_option_versions_hash"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    option_template_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    version_n: Mapped[int] = mapped_column(Integer, nullable=False)
    definition: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    definition_hash: Mapped[str] = mapped_column(Text, nullable=False)
    # FK compuesta `(tenant_id, message_template_id)` con `ON DELETE SET NULL (message_template_id)`: solo en la
    # migración 0112 (SQLAlchemy no expresa la lista de columnas del SET NULL).
    message_template_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class OptionSend(Base):
    """Un envío concreto de una versión a una conversación. `via`: `interactive` | `template` | `text`.
    `context` guarda lo que el servidor resolvió para renderizar el cuerpo (nunca texto del cliente)."""

    __tablename__ = "option_sends"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_option_sends_tenant_id_id"),
        ForeignKeyConstraint(
            ["tenant_id", "option_template_id"],
            ["option_templates.tenant_id", "option_templates.id"],
            name="fk_option_sends_template",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "version_id"],
            ["option_template_versions.tenant_id", "option_template_versions.id"],
            name="fk_option_sends_version",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "conversation_id"], ["conversations.tenant_id", "conversations.id"], name="fk_option_sends_conversation"
        ),
        Index("ix_option_sends_tenant_conversation", "tenant_id", "conversation_id", text("created_at DESC")),
        Index("ix_option_sends_tenant_version", "tenant_id", "version_id"),
        CheckConstraint("via in ('interactive','template','text')", name="ck_option_sends_via"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    option_template_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    version_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    conversation_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    via: Mapped[str] = mapped_column(Text, nullable=False)
    context: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class OptionActivation(Base):
    """Un toque del cliente. Estados: `pending_confirm`, `running`, `done`, `failed`, `cancelled`, `expired`.
    `UNIQUE (tenant_id, dedupe_key)`, a lo más una pendiente por (envío, opción) y `inbound_wamid` único.
    No guarda argumentos ni resultados: solo `args_hash` y un `error_code`."""

    __tablename__ = "option_activations"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_option_activations_tenant_id_id"),
        UniqueConstraint("tenant_id", "dedupe_key", name="uq_option_activations_tenant_dedupe"),
        ForeignKeyConstraint(["tenant_id", "send_id"], ["option_sends.tenant_id", "option_sends.id"], name="fk_option_activations_send"),
        Index(
            "uq_option_activations_pendiente",
            "tenant_id",
            "send_id",
            "option_id",
            unique=True,
            postgresql_where=text("state in ('pending_confirm','running')"),
        ),
        Index(
            "uq_option_activations_wamid",
            "tenant_id",
            "inbound_wamid",
            unique=True,
            postgresql_where=text("inbound_wamid IS NOT NULL"),
        ),
        Index("ix_option_activations_tenant_send", "tenant_id", "send_id"),
        CheckConstraint(
            "state in ('pending_confirm','running','done','failed','cancelled','expired')", name="ck_option_activations_state"
        ),
        CheckConstraint("option_id ~ '^[a-z0-9_]{1,12}$'", name="ck_option_activations_option_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    send_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    option_id: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[str] = mapped_column(Text, nullable=False, default="pending_confirm")
    dedupe_key: Mapped[str] = mapped_column(Text, nullable=False)
    inbound_wamid: Mapped[str | None] = mapped_column(Text)
    args_hash: Mapped[str | None] = mapped_column(Text)
    error_code: Mapped[str | None] = mapped_column(Text)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


# ---------------------------------------------------------------- mensajes guardados (ADR-0178 D4, migración 0116)
class MensajeGuardado(Base):
    """Respuesta rápida del operador, reutilizable entre canales (ADR-0178, pista D4). Texto libre con variables del
    dialecto común (`{cliente.nombre}`). `canales` son `ConnectionType.id`; vacío = todos. Estados: `borrador`,
    `activo`, `archivado`. Borrado lógico (`deleted_at`) que libera la clave. Tabla de NEGOCIO: tenant_id + RLS
    forzada (invariante 1)."""

    __tablename__ = "mensajes_guardados"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_mensajes_guardados_tenant_id_id"),
        Index("uq_mensajes_guardados_tenant_clave", "tenant_id", "clave", unique=True, postgresql_where=text("deleted_at IS NULL")),
        CheckConstraint("estado in ('borrador','activo','archivado')", name="ck_mensajes_guardados_estado"),
        CheckConstraint("clave ~ '^[a-z0-9_]{1,40}$'", name="ck_mensajes_guardados_clave"),
        CheckConstraint("char_length(titulo) between 1 and 120 and titulo !~ '^\\s*$'", name="ck_mensajes_guardados_titulo"),
        CheckConstraint("char_length(texto) between 1 and 4096 and texto !~ '^\\s*$'", name="ck_mensajes_guardados_texto"),
        CheckConstraint("cardinality(canales) <= 20 and (array_ndims(canales) is null or array_ndims(canales) = 1) "
            "and array_position(canales, null) is null and array_position(canales, '') is null", name="ck_mensajes_guardados_canales"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    clave: Mapped[str] = mapped_column(Text, nullable=False)
    titulo: Mapped[str] = mapped_column(Text, nullable=False)
    texto: Mapped[str] = mapped_column(Text, nullable=False)
    canales: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, default=list)
    estado: Mapped[str] = mapped_column(Text, nullable=False, default="borrador")
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    updated_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
