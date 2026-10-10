"""Modelos de pack-payments — SQLAlchemy sobre su PROPIA Base (invariante nº5 de CLAUDE.md).

`PaymentTransaction` no sabe qué es un "Order" ni un "Quote": `reference`
es una FK lógica hacia lo que sea que el llamador haya pasado (ej. el
`public_order_number` de pack-commerce). La correlación con el pedido real
la hace pack-commerce del lado suyo, suscrito al evento que este pack
emite (ADR-0023) — nunca importando este modelo.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Index, Numeric, Text, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from core.shared.tenant import get_current_tenant_id


def utcnow() -> datetime:
    from datetime import UTC

    return datetime.now(UTC)


def _tenant_or_raise() -> uuid.UUID:
    tenant_id = get_current_tenant_id()
    if tenant_id is None:
        raise RuntimeError("tenant_id requerido: usar dentro de tenant_scope()")
    return tenant_id


class TenantMixin:
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, default=_tenant_or_raise)


class Base(DeclarativeBase):
    pass


class PaymentTransaction(TenantMixin, Base):
    """Un intento de cobro en línea: link generado -> pendiente -> aprobado|rechazado|expirado."""

    __tablename__ = "pack_payments_transactions"
    __table_args__ = (
        # Una sola transacción VIVA por referencia: al regenerar un link vencido, la
        # vieja se marca `deleted_at` y queda para auditoría (migración 0003).
        Index(
            "uq_pack_payments_tenant_reference_viva",
            "tenant_id",
            "reference",
            unique=True,
            postgresql_where=text("deleted_at is null"),
        ),
        Index("uq_pack_payments_tenant_idem", "tenant_id", "idempotency_key", unique=True),
        Index("ix_pack_payments_tenant_provider_ref", "tenant_id", "provider", "provider_reference"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # Referencia de negocio que trajo el llamador (ej. Order.public_order_number
    # de pack-commerce) — este pack la trata como un string opaco, nunca la resuelve.
    reference: Mapped[str] = mapped_column(Text, nullable=False)
    provider: Mapped[str] = mapped_column(Text, default="wompi")
    provider_reference: Mapped[str | None] = mapped_column(Text, default=None)
    status: Mapped[str] = mapped_column(Text, default="pendiente")  # pendiente|aprobado|rechazado|expirado
    amount: Mapped[float] = mapped_column(Numeric(12, 2), default=0)
    currency: Mapped[str] = mapped_column(Text, default="COP")
    payment_link_url: Mapped[str | None] = mapped_column(Text, default=None)
    idempotency_key: Mapped[str] = mapped_column(Text, nullable=False)
    # Último payload de webhook recibido (auditoría/debug, nunca se muestra al LLM).
    last_webhook_payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB, default=None)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
