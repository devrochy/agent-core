"""Tablas propias del plugin (prefijo crm_ para no chocar con el core).

Esquema adaptado de agent-sale (migraciones 0005-0056) a las convenciones de
agent-core: Base propia, FK lógica (UUID sin ForeignKey física), árbol Alembic
propio con version_table separada.

Dominios:
- leads: captura y calificación de prospectos.
- catálogo: productos, variantes (precio real por variante), categorías en árbol,
  complementos (venta cruzada) e inventario por variante.
- promociones: temporada/volumen/campaña con elegibilidad multidimensional.
- ventas: cotizaciones inmutables -> pedidos con estado y método de pago/entrega.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import Boolean, Computed, DateTime, Integer, Numeric, Sequence, Text
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class Lead(Base):
    """Prospecto capturado en conversación (lead de venta)."""

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


# ------------------------------------------------------------------ catálogo
class Category(Base):
    """Categoría de productos en árbol auto-referenciado."""

    __tablename__ = "crm_categories"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    parent_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), default=None)
    name: Mapped[str] = mapped_column(Text)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class CategoryComplement(Base):
    """Pares de categorías complementarias para venta cruzada (recomendar_producto)."""

    __tablename__ = "crm_category_complements"
    __table_args__ = (
        __import__("sqlalchemy").PrimaryKeyConstraint(
            "category_id", "complementary_category_id", name="pk_crm_cat_complement"
        ),
    )

    category_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    complementary_category_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))


class Product(Base):
    __tablename__ = "crm_products"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(Text)
    description: Mapped[str] = mapped_column(Text, default="")
    image_url: Mapped[str | None] = mapped_column(Text)
    category_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))  # FK lógica a crm_categories
    has_variants: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class ProductVariant(Base):
    __tablename__ = "crm_product_variants"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    product_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))  # FK lógica a crm_products
    sku: Mapped[str] = mapped_column(Text, unique=True)
    attributes: Mapped[dict] = mapped_column(JSONB, default=dict)  # {"talla": "M"} | {} si sin atributos
    price: Mapped[float] = mapped_column(Numeric(12, 2), default=0)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Inventory(Base):
    __tablename__ = "crm_inventory"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    variant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))  # FK lógica a crm_product_variants
    stock_quantity: Mapped[int] = mapped_column(Integer, default=0)
    source: Mapped[str] = mapped_column(Text, default="manual")  # manual | import
    last_synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# ------------------------------------------------------------------ promociones
class Promotion(Base):
    __tablename__ = "crm_promotions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    type: Mapped[str] = mapped_column(Text)  # temporada|volumen|campaña
    rules: Mapped[dict] = mapped_column(JSONB, default=dict)
    valid_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    valid_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    # elegibilidad multidimensional (todas opcionales; si una línea falla, la promo no aplica)
    category_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), default=None)
    include_child_categories: Mapped[bool] = mapped_column(Boolean, default=True)
    product_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), default=None)
    variant_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), default=None)
    eligible_segments: Mapped[list[str] | None] = mapped_column(ARRAY(Text), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PromotionRedemption(Base):
    __tablename__ = "crm_promotion_redemptions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    promotion_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    customer_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    order_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    redeemed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# ------------------------------------------------------------------ ventas
class Quote(Base):
    """Cotización inmutable: un cambio genera una nueva. Se convierte en pedido al aceptarla."""

    __tablename__ = "crm_quotes"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    conversation_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))  # FK lógica a conversations
    customer_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))      # FK lógica a customers
    subtotal: Mapped[float] = mapped_column(Numeric(12, 2), default=0)
    discount: Mapped[float] = mapped_column(Numeric(12, 2), default=0)
    total: Mapped[float] = mapped_column(Numeric(12, 2), default=0)
    status: Mapped[str] = mapped_column(Text, default="draft")  # draft|aceptada|convertida|expirada
    applied_promotion_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), default=None)
    follow_up_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class QuoteItem(Base):
    __tablename__ = "crm_quote_items"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    quote_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    variant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    quantity: Mapped[int] = mapped_column(Integer)
    unit_price: Mapped[float] = mapped_column(Numeric(12, 2))


class Order(Base):
    """Pedido confirmado. Estado y pago son dos ejes independientes (ag-sale 0056)."""

    __tablename__ = "crm_orders"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    quote_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    conversation_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    customer_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    status: Mapped[str] = mapped_column(Text, default="abierto")  # abierto|despachado|entregado|cancelado|expirado
    payment_status: Mapped[str] = mapped_column(Text, default="pendiente")  # pendiente|pagado|rechazado
    payment_method: Mapped[str] = mapped_column(Text, default="efectivo_contraentrega")  # efectivo_contraentrega|transferencia
    delivery_method: Mapped[str] = mapped_column(Text, default="domicilio")  # domicilio|recoger_en_tienda
    total: Mapped[float] = mapped_column(Numeric(12, 2), default=0)
    idempotency_key: Mapped[str] = mapped_column(Text, unique=True)
    order_seq: Mapped[int] = mapped_column(Sequence("crm_orders_seq"), unique=True, nullable=False)
    public_order_number: Mapped[str] = mapped_column(
        Text, Computed("'OC-' || lpad(order_seq::text, 4, '0')", persisted=True), nullable=False
    )
    # snapshot de entrega por pedido (el perfil del cliente vive en el core)
    delivery_full_name: Mapped[str | None] = mapped_column(Text, default=None)
    delivery_id_document: Mapped[str | None] = mapped_column(Text, default=None)
    delivery_address: Mapped[str | None] = mapped_column(Text, default=None)
    delivery_municipality: Mapped[str | None] = mapped_column(Text, default=None)
    delivery_city: Mapped[str | None] = mapped_column(Text, default=None)
    status_changed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    status_reason: Mapped[str | None] = mapped_column(Text, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class OrderItem(Base):
    __tablename__ = "crm_order_items"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    order_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    variant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    quantity: Mapped[int] = mapped_column(Integer)
    unit_price: Mapped[float] = mapped_column(Numeric(12, 2))


class OrderItemBatch(Base):
    """Idempotencia de agregar_item_pedido: un intento de agregar = una fila."""

    __tablename__ = "crm_order_item_batches"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    order_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    idempotency_key: Mapped[str] = mapped_column(Text, unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)