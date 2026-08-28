"""Esquema completo del CRM v2 en una sola migración.

Incluye: leads, catálogo (categorías, productos, variantes, inventario),
promociones y ventas (cotizaciones, pedidos). NO incluye crm_deals
(eliminado del dominio en v2.0.0).

Revision ID: crm_0001
Revises:
"""
from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID

from alembic import op

revision = "crm_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ---------------------------------------------------------------- leads
    op.create_table(
        "crm_leads",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("customer_id", UUID(as_uuid=True), nullable=True),
        sa.Column("conversation_id", UUID(as_uuid=True), nullable=True),
        sa.Column("name", sa.Text(), nullable=True),
        sa.Column("phone", sa.Text(), nullable=True),
        sa.Column("email", sa.Text(), nullable=True),
        sa.Column("interest", sa.Text(), nullable=False, server_default=""),
        sa.Column("score", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("status", sa.Text(), nullable=False, server_default="nuevo"),
        sa.Column("idempotency_key", sa.Text(), nullable=True, unique=True),
        sa.Column("last_activity_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_crm_leads_customer", "crm_leads", ["customer_id"])
    op.create_index("ix_crm_leads_status", "crm_leads", ["status"])

    # ---------------------------------------------------------------- catálogo
    op.create_table(
        "crm_categories",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("parent_id", UUID(as_uuid=True), nullable=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("active", sa.Boolean(), nullable=False, server_default="true"),
    )
    op.create_index("ix_crm_categories_parent", "crm_categories", ["parent_id"])

    op.create_table(
        "crm_category_complements",
        sa.Column("category_id", UUID(as_uuid=True), nullable=False),
        sa.Column("complementary_category_id", UUID(as_uuid=True), nullable=False),
        sa.PrimaryKeyConstraint("category_id", "complementary_category_id", name="pk_crm_cat_complement"),
    )

    op.create_table(
        "crm_products",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("image_url", sa.Text(), nullable=True),
        sa.Column("category_id", UUID(as_uuid=True), nullable=True),
        sa.Column("has_variants", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_crm_products_category", "crm_products", ["category_id"])

    op.create_table(
        "crm_product_variants",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("product_id", UUID(as_uuid=True), nullable=False),
        sa.Column("sku", sa.Text(), nullable=False, unique=True),
        sa.Column("attributes", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("price", sa.Numeric(12, 2), nullable=False, server_default="0"),
        sa.Column("active", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_crm_product_variants_product", "crm_product_variants", ["product_id"])

    op.create_table(
        "crm_inventory",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("variant_id", UUID(as_uuid=True), nullable=False),
        sa.Column("stock_quantity", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("source", sa.Text(), nullable=False, server_default="manual"),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_crm_inventory_variant", "crm_inventory", ["variant_id"])

    # ---------------------------------------------------------------- promociones
    op.create_table(
        "crm_promotions",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("type", sa.Text(), nullable=False),  # temporada|volumen|campaña
        sa.Column("rules", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=True),
        sa.Column("valid_to", sa.DateTime(timezone=True), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("category_id", UUID(as_uuid=True), nullable=True),
        sa.Column("include_child_categories", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("product_id", UUID(as_uuid=True), nullable=True),
        sa.Column("variant_id", UUID(as_uuid=True), nullable=True),
        sa.Column("eligible_segments", ARRAY(sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_crm_promotions_active", "crm_promotions", ["active"])

    op.create_table(
        "crm_promotion_redemptions",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("promotion_id", UUID(as_uuid=True), nullable=False),
        sa.Column("customer_id", UUID(as_uuid=True), nullable=False),
        sa.Column("order_id", UUID(as_uuid=True), nullable=False),
        sa.Column("redeemed_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_crm_promo_redemptions_promo_cust", "crm_promotion_redemptions", ["promotion_id", "customer_id"])

    # ---------------------------------------------------------------- ventas
    op.execute("CREATE SEQUENCE IF NOT EXISTS crm_orders_seq")
    op.create_table(
        "crm_quotes",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("conversation_id", UUID(as_uuid=True), nullable=False),
        sa.Column("customer_id", UUID(as_uuid=True), nullable=False),
        sa.Column("subtotal", sa.Numeric(12, 2), nullable=False, server_default="0"),
        sa.Column("discount", sa.Numeric(12, 2), nullable=False, server_default="0"),
        sa.Column("total", sa.Numeric(12, 2), nullable=False, server_default="0"),
        sa.Column("status", sa.Text(), nullable=False, server_default="draft"),
        sa.Column("applied_promotion_id", UUID(as_uuid=True), nullable=True),
        sa.Column("follow_up_sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_crm_quotes_customer", "crm_quotes", ["customer_id"])

    op.create_table(
        "crm_quote_items",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("quote_id", UUID(as_uuid=True), nullable=False),
        sa.Column("variant_id", UUID(as_uuid=True), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("unit_price", sa.Numeric(12, 2), nullable=False),
    )
    op.create_index("ix_crm_quote_items_quote", "crm_quote_items", ["quote_id"])

    op.create_table(
        "crm_orders",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("quote_id", UUID(as_uuid=True), nullable=True),
        sa.Column("conversation_id", UUID(as_uuid=True), nullable=False),
        sa.Column("customer_id", UUID(as_uuid=True), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="abierto"),
        sa.Column("payment_status", sa.Text(), nullable=False, server_default="pendiente"),
        sa.Column("payment_method", sa.Text(), nullable=False, server_default="efectivo_contraentrega"),
        sa.Column("delivery_method", sa.Text(), nullable=False, server_default="domicilio"),
        sa.Column("total", sa.Numeric(12, 2), nullable=False, server_default="0"),
        sa.Column("idempotency_key", sa.Text(), nullable=False, unique=True),
        sa.Column("order_seq", sa.Integer(), sa.Sequence("crm_orders_seq"), nullable=False),
        sa.Column(
            "public_order_number",
            sa.Text(),
            sa.Computed("'OC-' || lpad(order_seq::text, 4, '0')", persisted=True),
            nullable=False,
        ),
        sa.Column("delivery_full_name", sa.Text(), nullable=True),
        sa.Column("delivery_id_document", sa.Text(), nullable=True),
        sa.Column("delivery_address", sa.Text(), nullable=True),
        sa.Column("delivery_municipality", sa.Text(), nullable=True),
        sa.Column("delivery_city", sa.Text(), nullable=True),
        sa.Column("status_changed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status_reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_crm_orders_customer", "crm_orders", ["customer_id"])
    op.create_index("ix_crm_orders_status", "crm_orders", ["status"])
    op.create_index("ix_crm_orders_payment_status", "crm_orders", ["payment_status"])

    op.create_table(
        "crm_order_items",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("order_id", UUID(as_uuid=True), nullable=False),
        sa.Column("variant_id", UUID(as_uuid=True), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("unit_price", sa.Numeric(12, 2), nullable=False),
    )
    op.create_index("ix_crm_order_items_order", "crm_order_items", ["order_id"])

    op.create_table(
        "crm_order_item_batches",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("order_id", UUID(as_uuid=True), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=False, unique=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )


def downgrade() -> None:
    op.drop_table("crm_order_item_batches")
    op.drop_table("crm_order_items")
    op.drop_table("crm_orders")
    op.drop_table("crm_quote_items")
    op.drop_table("crm_quotes")
    op.drop_table("crm_promotion_redemptions")
    op.drop_table("crm_promotions")
    op.drop_table("crm_inventory")
    op.drop_table("crm_product_variants")
    op.drop_table("crm_products")
    op.drop_table("crm_category_complements")
    op.drop_table("crm_categories")
    op.drop_table("crm_leads")
    op.execute("DROP SEQUENCE IF EXISTS crm_orders_seq")