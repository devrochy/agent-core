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


# Los leads (`pack_commerce_leads`, `pack_commerce_lead_stage_changes`, `pack_commerce_lead_followups`) son del core
# desde ADR-0193 (`core.db.models.Lead*`, migración 0146). Las tablas siguen en la base, sin escrituras, hasta una HU
# que las retire; `agent-core db copiar-leads` las lee con SQL. Ningún código del pack las usa.


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
    # HU-CRM-05: foto subida al controlador único (`<slug>/productos/...`); gana sobre image_url.
    image_key: Mapped[str | None] = mapped_column(Text)
    category_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))  # FK lógica a crm_categories
    ally_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), default=None)  # FK lógica (HU-CRM-03)
    has_variants: Mapped[bool] = mapped_column(Boolean, default=False)
    # Datos de prueba (HU-QA-10): un producto sembrado se retira con borrado lógico.
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
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
        Index("ix_pack_commerce_orders_tenant_customer", "tenant_id", "customer_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    quote_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    conversation_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    customer_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    status: Mapped[str] = mapped_column(Text, default="abierto")  # abierto|despachado|entregado|cancelado|expirado
    payment_status: Mapped[str] = mapped_column(Text, default="pendiente")  # pendiente|en_revision|pagado|rechazado
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
    # Revisión manual del pago (SPEC-48, HU-COM-22, crm_0020): quién lo cambió, cuándo y por qué.
    payment_status_changed_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), default=None)
    payment_status_changed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    payment_status_reason: Mapped[str | None] = mapped_column(Text, default=None)
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


class PaymentProof(TenantMixin, Base):
    """Comprobante de una transferencia (SPEC-48, HU-COM-22, crm_0020). Registro del negocio: no se borra.

    `extracted` es lo que leyó la visión (dato no confiable hasta compararlo); `match` es el resultado
    determinista de `evaluar_comprobante` con sus motivos. Idempotente por `(order_id, image_sha256)`."""

    __tablename__ = "pack_commerce_payment_proofs"
    __table_args__ = (
        Index("ix_pack_commerce_payment_proofs_tenant_order", "tenant_id", "order_id", "created_at"),
        Index("ix_pack_commerce_payment_proofs_tenant_sha256", "tenant_id", "image_sha256"),
        Index(
            "uq_pack_commerce_payment_proofs_tenant_order_sha256",
            "tenant_id", "order_id", "image_sha256",
            unique=True,
            postgresql_where=text("deleted_at is null"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    order_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), default=None)
    message_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), default=None)
    image_key: Mapped[str] = mapped_column(Text, nullable=False)
    image_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    extracted: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    match: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(Text, nullable=False)  # pagado|en_revision|rechazado
    reviewed_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), default=None)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    review_note: Mapped[str | None] = mapped_column(Text, default=None)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
