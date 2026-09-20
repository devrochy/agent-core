"""Contabilidad de costes de Meta: `message_costs` (ingesta por webhook,
factual pero SIN dólares -- `statuses[].pricing` nunca trae un monto) y
`pricing_daily` (ingesta diaria desde `pricing_analytics`, con `cost`
AUTORITATIVO -- regla innegociable del ticket: nunca se calcula un costo
localmente con una tabla de tarifas propia).

Revision ID: 0028
Revises: 0027
Create Date: 2026-09-20
"""
from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

from alembic import op

revision = "0028"
down_revision = "0027"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "message_costs",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
        sa.Column("message_id", UUID(as_uuid=True), sa.ForeignKey("messages.id", ondelete="SET NULL"), nullable=True),
        sa.Column("external_message_id", sa.Text(), nullable=False),
        sa.Column("waba_account_id", UUID(as_uuid=True), sa.ForeignKey("waba_accounts.id", ondelete="SET NULL"), nullable=True),
        sa.Column("phone_number_id", sa.Text(), nullable=True),
        sa.Column("billable", sa.Boolean(), nullable=True),
        sa.Column("pricing_model", sa.Text(), nullable=True),
        sa.Column("pricing_type", sa.Text(), nullable=True),
        sa.Column("pricing_category", sa.Text(), nullable=True),
        # Id de conversación de META (ventana de facturación) -- NO
        # confundir con `conversations.id` interno, por eso el nombre distinto.
        sa.Column("meta_conversation_id", sa.Text(), nullable=True),
        sa.Column("conversation_origin_type", sa.Text(), nullable=True),
        sa.Column("conversation_expiration_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.Text(), nullable=True),
        sa.Column("event_ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("raw", JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    # NO único -- Meta puede mandar el mismo wamid en más de un status con
    # pricing (ej. sent + delivered si ambos lo llevan); cada evento es un
    # hecho nuevo, no un estado a sobreescribir.
    op.create_index("ix_message_costs_tenant_external_id", "message_costs", ["tenant_id", "external_message_id"])
    op.execute("alter table message_costs enable row level security")
    op.execute(
        "create policy tenant_isolation on message_costs using "
        "(tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid) "
        "with check (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)"
    )
    op.execute("grant select, insert, update, delete on message_costs to app_runtime")

    op.create_table(
        "pricing_daily",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
        sa.Column("waba_account_id", UUID(as_uuid=True), sa.ForeignKey("waba_accounts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("phone_number", sa.Text(), nullable=True),
        sa.Column("country", sa.Text(), nullable=True),
        # "<LOWER>:<UPPER>" tal cual lo manda Meta -- no se parsea a número,
        # ninguna operación de este ticket lo necesita como tal.
        sa.Column("tier", sa.Text(), nullable=True),
        sa.Column("pricing_type", sa.Text(), nullable=True),
        sa.Column("pricing_category", sa.Text(), nullable=True),
        sa.Column("volume", sa.Integer(), nullable=False),
        # AUTORITATIVO -- nunca calculado localmente (ver docstring del módulo).
        sa.Column("cost", sa.Numeric(12, 6), nullable=False),
        sa.Column("synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("raw", JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    # Índice único de EXPRESIÓN con coalesce -- las dimensiones nullable
    # (phone_number/country/tier/pricing_type/pricing_category) participan
    # de la igualdad de todos modos, para que el upsert sea determinístico
    # incluso cuando Meta omite alguna dimensión en un data point.
    # `tenant_id` participa de la clave como defensa en profundidad además
    # de RLS (hallazgo auditor-multitenant, 2026-09-20, severidad baja):
    # hoy `waba_account_id` ya es 1:1 con un tenant, pero si esa garantía
    # de esquema se relajara algún día, esta clave evita una colisión
    # entre tenants a nivel de constraint de BD.
    op.execute(
        """
        create unique index uq_pricing_daily_dimensions on pricing_daily (
            tenant_id, waba_account_id, date,
            coalesce(phone_number, ''), coalesce(country, ''), coalesce(tier, ''),
            coalesce(pricing_type, ''), coalesce(pricing_category, '')
        )
        """
    )
    op.create_index("ix_pricing_daily_tenant_date", "pricing_daily", ["tenant_id", "date"])
    op.execute("alter table pricing_daily enable row level security")
    op.execute(
        "create policy tenant_isolation on pricing_daily using "
        "(tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid) "
        "with check (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)"
    )
    op.execute("grant select, insert, update, delete on pricing_daily to app_runtime")


def downgrade() -> None:
    op.execute("drop policy if exists tenant_isolation on pricing_daily")
    op.execute("alter table pricing_daily disable row level security")
    op.execute("drop index if exists ix_pricing_daily_tenant_date")
    op.execute("drop index if exists uq_pricing_daily_dimensions")
    op.drop_table("pricing_daily")

    op.execute("drop policy if exists tenant_isolation on message_costs")
    op.execute("alter table message_costs disable row level security")
    op.execute("drop index if exists ix_message_costs_tenant_external_id")
    op.drop_table("message_costs")
