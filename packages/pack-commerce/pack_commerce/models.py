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

from sqlalchemy import Boolean, DateTime, Index, Integer, Numeric, Text, text
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from core.shared.exceptions import PluginError
from core.shared.tenant import get_current_tenant_id


def utcnow() -> datetime:
    return datetime.now(UTC)


def _tenant_or_raise() -> uuid.UUID:
    """tenant_id de cada INSERT. Fail-fast: sin tenant_scope, error claro (no silencioso)."""
    tenant_id = get_current_tenant_id()
    if tenant_id is None:
        raise PluginError("pack_commerce", "INSERT pack_commerce_* sin tenant_scope")
    return tenant_id


class TenantMixin:
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, default=_tenant_or_raise
    )


class Base(DeclarativeBase):
    pass


class Lead(TenantMixin, Base):
    """Prospecto capturado en conversación (lead de venta)."""

    __tablename__ = "pack_commerce_leads"
    __table_args__ = (
        Index(
            "uq_crm_leads_tenant_idem",
            "tenant_id",
            "idempotency_key",
            unique=True,
            postgresql_where=text("idempotency_key is not null"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    customer_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))  # FK lógica a customers
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    name: Mapped[str | None] = mapped_column(Text)
    phone: Mapped[str | None] = mapped_column(Text)
    email: Mapped[str | None] = mapped_column(Text)
    interest: Mapped[str] = mapped_column(Text, default="")
    score: Mapped[int] = mapped_column(Integer, default=0)          # 0-10
    status: Mapped[str] = mapped_column(Text, default="nuevo")      # nuevo|calificado|contactado|convertido|perdido
    idempotency_key: Mapped[str | None] = mapped_column(Text)
    last_activity_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# ------------------------------------------------------------------ catálogo
class Category(TenantMixin, Base):
    """Categoría de productos en árbol auto-referenciado."""

    __tablename__ = "pack_commerce_categories"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    parent_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), default=None)
    name: Mapped[str] = mapped_column(Text)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CategoryComplement(TenantMixin, Base):
    """Pares de categorías complementarias para venta cruzada (recomendar_producto)."""

    __tablename__ = "pack_commerce_category_complements"
    __table_args__ = (
        __import__("sqlalchemy").PrimaryKeyConstraint(
            "category_id", "complementary_category_id", name="pk_crm_cat_complement"
        ),
    )

    category_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    complementary_category_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))


class Product(TenantMixin, Base):
    __tablename__ = "pack_commerce_products"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(Text)
    description: Mapped[str] = mapped_column(Text, default="")
    image_url: Mapped[str | None] = mapped_column(Text)
    category_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))  # FK lógica a crm_categories
    ally_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), default=None)  # FK lógica (HU-CRM-03)
    has_variants: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class ProductVariant(TenantMixin, Base):
    __tablename__ = "pack_commerce_product_variants"
    __table_args__ = (
        Index("uq_crm_variants_tenant_sku", "tenant_id", "sku", unique=True),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    product_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))  # FK lógica a crm_products
    sku: Mapped[str] = mapped_column(Text)
    attributes: Mapped[dict] = mapped_column(JSONB, default=dict)  # {"talla": "M"} | {} si sin atributos
    price: Mapped[float] = mapped_column(Numeric(12, 2), default=0)
    # Recurrencia del plan (ADR-0083, #153): None = producto one-shot (sin suscripción).
    billing_period: Mapped[str | None] = mapped_column(Text, default=None)  # "mensual"|"anual"
    trial_days: Mapped[int | None] = mapped_column(Integer, default=None)
    entitlements: Mapped[dict | None] = mapped_column(JSONB, default=None)  # límites del plan
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Inventory(TenantMixin, Base):
    __tablename__ = "pack_commerce_inventory"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    variant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))  # FK lógica a crm_product_variants
    stock_quantity: Mapped[int] = mapped_column(Integer, default=0)
    source: Mapped[str] = mapped_column(Text, default="manual")  # manual | import
    last_synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Ally(TenantMixin, Base):
    """Aliado = proveedor del negocio (HU-CRM-03). Opcional: el módulo se enciende por negocio."""

    __tablename__ = "pack_commerce_allies"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(Text)
    contact_info: Mapped[dict] = mapped_column(JSONB, default=dict)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# ------------------------------------------------------------------ promociones
class Promotion(TenantMixin, Base):
    __tablename__ = "pack_commerce_promotions"

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
    # HU-CRM-03: con aliado, la promoción aplica solo si todos los ítems son de ese aliado.
    ally_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), default=None)
    eligible_segments: Mapped[list[str] | None] = mapped_column(ARRAY(Text), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class PromotionRedemption(TenantMixin, Base):
    __tablename__ = "pack_commerce_promotion_redemptions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    promotion_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    customer_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    order_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    redeemed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# ------------------------------------------------------------------ ventas
class Quote(TenantMixin, Base):
    """Cotización inmutable: un cambio genera una nueva. Se convierte en pedido al aceptarla."""

    __tablename__ = "pack_commerce_quotes"

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


class QuoteItem(TenantMixin, Base):
    __tablename__ = "pack_commerce_quote_items"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    quote_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    variant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    quantity: Mapped[int] = mapped_column(Integer)
    unit_price: Mapped[float] = mapped_column(Numeric(12, 2))


class Order(TenantMixin, Base):
    """Pedido confirmado. Estado y pago son dos ejes independientes (ag-sale 0056)."""

    __tablename__ = "pack_commerce_orders"
    __table_args__ = (
        Index("uq_crm_orders_tenant_idem", "tenant_id", "idempotency_key", unique=True),
        Index("uq_crm_orders_tenant_seq", "tenant_id", "order_seq", unique=True),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    quote_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    conversation_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    customer_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    status: Mapped[str] = mapped_column(Text, default="abierto")  # abierto|despachado|entregado|cancelado|expirado
    payment_status: Mapped[str] = mapped_column(Text, default="pendiente")  # pendiente|pagado|rechazado
    payment_method: Mapped[str] = mapped_column(Text, default="efectivo_contraentrega")  # efectivo_contraentrega|transferencia
    delivery_method: Mapped[str] = mapped_column(Text, default="domicilio")  # domicilio|recoger_en_tienda
    total: Mapped[float] = mapped_column(Numeric(12, 2), default=0)
    idempotency_key: Mapped[str] = mapped_column(Text)
    # Link de pago versionado (HU-COM-01): el total manda y, al cambiar, la
    # referencia sube de versión (`OC-0001` -> `OC-0001-r2`) para invalidar el link.
    payment_reference: Mapped[str | None] = mapped_column(Text, default=None)
    payment_link_url: Mapped[str | None] = mapped_column(Text, default=None)
    payment_link_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Numeración por tenant (ADR-0018 + HU-COM-03): la asigna crear_pedido vía
    # crm_next_order_seq(tenant) con el prefijo del negocio; índice único
    # (tenant_id, public_order_number) en la migración crm_0009.
    order_seq: Mapped[int] = mapped_column(Integer, nullable=False)
    public_order_number: Mapped[str] = mapped_column(Text, nullable=False)
    # snapshot de entrega por pedido (el perfil del cliente vive en el core)
    delivery_full_name: Mapped[str | None] = mapped_column(Text, default=None)
    delivery_id_document: Mapped[str | None] = mapped_column(Text, default=None)
    delivery_address: Mapped[str | None] = mapped_column(Text, default=None)
    delivery_municipality: Mapped[str | None] = mapped_column(Text, default=None)
    delivery_city: Mapped[str | None] = mapped_column(Text, default=None)
    status_changed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    status_reason: Mapped[str | None] = mapped_column(Text, default=None)
    # HU-SUB-09: activación del plan del pedido (NULL si no lleva planes).
    plan_activation_status: Mapped[str | None] = mapped_column(Text, default=None)  # pendiente|activada
    plan_activation_error: Mapped[str | None] = mapped_column(Text, default=None)
    # HU-SH-03 (ADR-0121): datos del checkout de un plan para dar de alta el servicio
    # ({"email", "telefono", "negocio", "vertical"}); NULL si el pedido no lleva planes.
    datos_alta: Mapped[dict | None] = mapped_column(JSONB, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class OrderItem(TenantMixin, Base):
    __tablename__ = "pack_commerce_order_items"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    order_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    variant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    quantity: Mapped[int] = mapped_column(Integer)
    unit_price: Mapped[float] = mapped_column(Numeric(12, 2))


class OrderItemBatch(TenantMixin, Base):
    """Idempotencia de agregar_item_pedido: un intento de agregar = una fila."""

    __tablename__ = "pack_commerce_order_item_batches"
    __table_args__ = (
        Index("uq_crm_batches_tenant_idem", "tenant_id", "idempotency_key", unique=True),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    order_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    idempotency_key: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)