"""Modelos de pack-subscriptions (ADR-0083, planes como productos).

Suscripción de cliente final que se habilita cuando un pedido de un plan
(producto de commerce con `billing_period`) se paga. El snapshot del plan
(nombre, precio, moneda, periodicidad, trial) lo aporta commerce vía el
registry del core; este pack NO guarda catálogo ni lee tablas de commerce
(invariante nº5). Toda tabla lleva `tenant_id` (invariante nº1), RLS y sin
borrado físico (`deleted_at`). `customer_id`/`variante_id` son referencias
lógicas (UUID sin constraint) al `Customer` del core y a la variante de commerce.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import DateTime, Index, Numeric, SmallInteger, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from core.shared.tenant import get_current_tenant_id


def utcnow() -> datetime:
    return datetime.now(UTC)


def _tenant_or_raise() -> uuid.UUID:
    tenant_id = get_current_tenant_id()
    if tenant_id is None:
        raise RuntimeError("INSERT pack_subscriptions_* sin tenant_scope")
    return tenant_id


class Base(DeclarativeBase):
    pass


# HU-SUB-26: canal de `pago_externo` cuando el dueño registra una transferencia del cliente. A diferencia
# de otro canal de cobro (el panel de la plataforma), el pack SIGUE cobrando el período siguiente.
CANAL_TRANSFERENCIA = "transferencia"


class Subscription(Base):
    """Suscripción de un `Customer` a un plan (producto de commerce con recurrencia)."""

    __tablename__ = "pack_subscriptions_subscriptions"
    __table_args__ = (
        Index("ix_pack_subscriptions_subs_tenant_customer", "tenant_id", "customer_id"),
        Index("ix_pack_subscriptions_subs_tenant_proximo", "tenant_id", "proximo_cargo"),
        # SPEC-35 (migración 0008): la lista del panel filtra por estado.
        Index("ix_pack_subscriptions_subs_tenant_estado", "tenant_id", "estado"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, default=_tenant_or_raise)
    customer_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)  # FK lógica core
    variante_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), default=None)  # FK lógica commerce
    concepto: Mapped[str] = mapped_column(Text, nullable=False, default="")
    estado: Mapped[str] = mapped_column(Text, nullable=False, default="trialing")  # trialing|activa|pausada|cancelada|vencida
    valor_periodo: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False, default=0)
    moneda: Mapped[str] = mapped_column(Text, nullable=False, default="COP")
    # HU-SUB-04 (migración 0004): período por calendario, no +30 días.
    periodicidad: Mapped[str] = mapped_column(Text, nullable=False, default="mensual")  # mensual|trimestral|anual
    dia_ancla: Mapped[int | None] = mapped_column(SmallInteger)  # día del mes en que empezó
    motivo_pausa: Mapped[str | None] = mapped_column(Text)  # HU-SUB-05: "mora" si la pausó el cron
    # HU-SUB-07 (ADR-0111): pedido que la creó (único por negocio) y downgrade programado.
    referencia_origen: Mapped[str | None] = mapped_column(Text)
    cambio_programado: Mapped[dict | None] = mapped_column(JSONB)
    periodo_actual_inicio: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    periodo_actual_fin: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    proximo_cargo: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    trial_termina: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    fecha_alta: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    fecha_cancelacion: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class SubscriptionCharge(Base):
    """Cargo por período de una suscripción (pendiente → pagado)."""

    __tablename__ = "pack_subscriptions_charges"
    __table_args__ = (
        Index("ix_pack_subscriptions_charges_tenant_sub", "tenant_id", "subscription_id"),
        # SPEC-35 (migración 0008): pagos y mora filtran por estado y vencimiento.
        Index("ix_pack_subscriptions_charges_tenant_estado_vence", "tenant_id", "estado", "fecha_vencimiento"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, default=_tenant_or_raise)
    subscription_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)  # FK lógica
    periodo: Mapped[str] = mapped_column(Text, nullable=False)  # YYYY-MM
    monto: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False, default=0)
    estado: Mapped[str] = mapped_column(Text, nullable=False, default="pendiente")  # pendiente|pagado|anulado (`rechazado` no se escribe)
    referencia: Mapped[str | None] = mapped_column(Text, default=None)
    link_url: Mapped[str | None] = mapped_column(Text, default=None)  # HU-SUB-05
    # HU-SUB-07 (ADR-0111): `periodo` | `cambio_plan` (diferencia prorrateada de un upgrade).
    tipo: Mapped[str] = mapped_column(Text, nullable=False, default="periodo", server_default="periodo")
    cambio_plan: Mapped[dict | None] = mapped_column(JSONB, default=None)  # plan al que lleva el cargo
    # HU-SH-04 (ADR-0123): lo pagó otro canal ({"canal", "referencia", "registrado_en"}).
    pago_externo: Mapped[dict | None] = mapped_column(JSONB, default=None)
    fecha_vencimiento: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)