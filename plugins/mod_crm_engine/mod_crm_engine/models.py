"""Tablas propias del plugin (prefijo crm_ para no chocar con el core).

Las migraciones viven en el árbol Alembic PROPIO del plugin
(plugins/mod_crm_engine/alembic/) con version_table separada.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, Integer, Numeric, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Lead(Base):
    __tablename__ = "crm_leads"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    customer_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))  # FK lógica a customers
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    name: Mapped[str | None] = mapped_column(Text)
    phone: Mapped[str | None] = mapped_column(Text)
    email: Mapped[str | None] = mapped_column(Text)
    interest: Mapped[str] = mapped_column(Text, default="")
    score: Mapped[int] = mapped_column(Integer, default=0)          # 0-10
    status: Mapped[str] = mapped_column(Text, default="nuevo")      # nuevo|calificado|contactado|convertido|perdido
    idempotency_key: Mapped[str | None] = mapped_column(Text, unique=True)
    last_activity_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Deal(Base):
    __tablename__ = "crm_deals"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    lead_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    title: Mapped[str] = mapped_column(Text)
    amount: Mapped[float] = mapped_column(Numeric(14, 2), default=0)
    stage: Mapped[str] = mapped_column(Text, default="propuesta")   # propuesta|negociacion|ganado|perdido
    notes: Mapped[str] = mapped_column(Text, default="")
    idempotency_key: Mapped[str | None] = mapped_column(Text, unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
