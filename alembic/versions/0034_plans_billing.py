"""Planes, cuotas y facturación de tenants (ADR-0045, ADR-0046).

Tres tablas:

- `plans`: catálogo de planes. Tabla de SISTEMA (sin `tenant_id`, sin RLS),
  excepción deliberada a la invariante #1 con el mismo criterio que
  `webhook_events` (ADR-0028) y `meta_app_subscriptions` (ADR-0029): el
  catálogo es global y compartido. La fuente de verdad es
  `core/billing/plans.py`; esta migración la siembra y `sync_plans()` la
  mantiene al día.
- `tenant_subscriptions`: relación de un tenant con un plan (negocio: RLS).
  Una activa por tenant (índice único parcial sobre `deleted_at is null`).
- `subscription_payments`: un registro por intento de cobro del plan,
  idempotente por `(tenant_id, provider_reference)` (negocio: RLS).

Backfill: todo tenant existente y vivo recibe una suscripción `free` con
30 días de trial DESDE HOY (no desde `created_at`) para no cortarle el
servicio de golpe a los clientes que ya están corriendo.

Revision ID: 0034
Revises: 0033
Create Date: 2026-09-22
"""
from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

from alembic import op

revision = "0034"
down_revision = "0033"
branch_labels = None
depends_on = None

# Límites `null` = ilimitado. Deben coincidir con `core/billing/plans.py`.
_PLANS: list[dict] = [
    {
        "id": "free",
        "name": "Free",
        "price_cop": 0,
        "trial_days": 30,
        "sort_order": 0,
        "limits": {"clientes": 100, "conversaciones": 500, "conversaciones_audio": 50,
                   "conversaciones_ocr": 20, "plugins": 1, "mensajes": 2000, "costo_llm_usd": 5},
        "features": {"soporte": "comunidad", "canales": ["whatsapp"]},
    },
    {
        "id": "basic",
        "name": "Basic",
        "price_cop": 200000,
        "trial_days": 0,
        "sort_order": 1,
        "limits": {"clientes": 500, "conversaciones": 3000, "conversaciones_audio": 300,
                   "conversaciones_ocr": 150, "plugins": 3, "mensajes": 15000, "costo_llm_usd": 50},
        "features": {"soporte": "email", "canales": ["whatsapp"]},
    },
    {
        "id": "premium",
        "name": "Premium",
        "price_cop": 600000,
        "trial_days": 0,
        "sort_order": 2,
        "limits": {"clientes": 2000, "conversaciones": 15000, "conversaciones_audio": 1500,
                   "conversaciones_ocr": 800, "plugins": 8, "mensajes": 80000, "costo_llm_usd": 200},
        "features": {"soporte": "prioritario", "canales": ["whatsapp", "instagram"]},
    },
    {
        "id": "empresarial",
        "name": "Empresarial",
        "price_cop": 1800000,
        "trial_days": 0,
        "sort_order": 3,
        "limits": {"clientes": None, "conversaciones": None, "conversaciones_audio": None,
                   "conversaciones_ocr": None, "plugins": None, "mensajes": None,
                   "costo_llm_usd": None},
        "features": {"soporte": "dedicado", "canales": ["whatsapp", "instagram", "web"]},
    },
]

_BUSINESS_TABLES = ("tenant_subscriptions", "subscription_payments")


def _seed_plans() -> None:
    for plan in _PLANS:
        op.execute(
            sa.text(
                """
                insert into plans
                    (id, name, price_cop, currency, interval_days, trial_days,
                     limits, features, sort_order, is_public, version)
                values
                    (:id, :name, :price_cop, 'COP', 30, :trial_days,
                     cast(:limits as jsonb), cast(:features as jsonb), :sort_order, true, 1)
                on conflict (id) do update set
                    name = excluded.name,
                    price_cop = excluded.price_cop,
                    interval_days = excluded.interval_days,
                    trial_days = excluded.trial_days,
                    limits = excluded.limits,
                    features = excluded.features,
                    sort_order = excluded.sort_order,
                    is_public = excluded.is_public,
                    updated_at = now()
                """
            ).bindparams(
                id=plan["id"],
                name=plan["name"],
                price_cop=plan["price_cop"],
                trial_days=plan["trial_days"],
                limits=_json(plan["limits"]),
                features=_json(plan["features"]),
                sort_order=plan["sort_order"],
            )
        )


def _json(value: dict) -> str:
    import json

    return json.dumps(value)


def upgrade() -> None:
    op.create_table(
        "plans",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("price_cop", sa.Numeric(12, 2), nullable=False, server_default="0"),
        sa.Column("currency", sa.Text(), nullable=False, server_default="COP"),
        sa.Column("interval_days", sa.Integer(), nullable=False, server_default="30"),
        sa.Column("trial_days", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("limits", JSONB(), nullable=False, server_default="{}"),
        sa.Column("features", JSONB(), nullable=False, server_default="{}"),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("is_public", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )

    op.create_table(
        "tenant_subscriptions",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
        sa.Column("plan_id", sa.Text(), sa.ForeignKey("plans.id"), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="trialing"),
        sa.Column("period_start", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("period_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column("trial_ends_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("provider", sa.Text(), nullable=True),
        sa.Column("provider_reference", sa.Text(), nullable=True),
        sa.Column("canceled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index(
        "ix_tenant_subscriptions_tenant",
        "tenant_subscriptions",
        ["tenant_id", "created_at"],
    )
    op.execute(
        "create unique index uq_tenant_subscription_active on tenant_subscriptions (tenant_id) "
        "where deleted_at is null"
    )

    op.create_table(
        "subscription_payments",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
        sa.Column("subscription_id", UUID(as_uuid=True), sa.ForeignKey("tenant_subscriptions.id"), nullable=False),
        sa.Column("plan_id", sa.Text(), sa.ForeignKey("plans.id"), nullable=False),
        sa.Column("amount_cop", sa.Numeric(12, 2), nullable=False),
        sa.Column("currency", sa.Text(), nullable=False, server_default="COP"),
        sa.Column("status", sa.Text(), nullable=False, server_default="pendiente"),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("provider_reference", sa.Text(), nullable=False),
        sa.Column("checkout_url", sa.Text(), nullable=True),
        sa.Column("period_start", sa.DateTime(timezone=True), nullable=True),
        sa.Column("period_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index(
        "ix_subscription_payments_tenant",
        "subscription_payments",
        ["tenant_id", "created_at"],
    )
    op.execute(
        "create unique index uq_subscription_payment_reference on subscription_payments "
        "(tenant_id, provider_reference) where deleted_at is null"
    )

    # RLS + policy (mismo patrón nullif de 0003/0004/0012) + FORCE (0029).
    for table in _BUSINESS_TABLES:
        op.execute(f"alter table {table} enable row level security")
        op.execute(f"alter table {table} force row level security")
        op.execute(
            f"create policy tenant_isolation on {table} using "
            "(tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid) "
            "with check (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)"
        )
        op.execute(f"grant select, insert, update, delete on {table} to app_runtime")

    # `plans` es de sistema: sin RLS. `app_runtime` (rol de tráfico de tenants)
    # SOLO puede LEER: el catálogo define precios y límites compartidos por todos
    # los tenants, así que no puede ser escribible desde una sesión de tenant.
    # `sync_plans()` escribe vía `upsert_plan()` SECURITY DEFINER (dueño), mismo
    # criterio que `purge_tenant`/`admin_tenants`.
    op.execute("revoke insert, update, delete on plans from app_runtime")
    op.execute("grant select on plans to app_runtime")

    op.execute(
        """
        create or replace function upsert_plan(
            p_id text, p_name text, p_price_cop numeric, p_currency text,
            p_interval_days int, p_trial_days int, p_limits jsonb, p_features jsonb,
            p_sort_order int, p_is_public boolean, p_version int
        ) returns void
        language sql
        security definer
        set search_path = public
        as $$
            insert into plans
                (id, name, price_cop, currency, interval_days, trial_days,
                 limits, features, sort_order, is_public, version)
            values
                (p_id, p_name, p_price_cop, p_currency, p_interval_days, p_trial_days,
                 p_limits, p_features, p_sort_order, p_is_public, p_version)
            on conflict (id) do update set
                name = excluded.name,
                price_cop = excluded.price_cop,
                currency = excluded.currency,
                interval_days = excluded.interval_days,
                trial_days = excluded.trial_days,
                limits = excluded.limits,
                features = excluded.features,
                sort_order = excluded.sort_order,
                is_public = excluded.is_public,
                version = excluded.version,
                updated_at = now();
        $$
        """
    )
    op.execute("grant execute on function upsert_plan(text, text, numeric, text, int, int, jsonb, jsonb, int, boolean, int) to app_runtime")

    # Resolución del tenant de un pago por `provider_reference` sin enumerar
    # tenants (el webhook de billing llega sin tenant). SECURITY DEFINER: omite
    # RLS y devuelve SOLO el tenant_id, nunca el contenido del pago.
    op.execute(
        """
        create or replace function resolve_subscription_payment_tenant(p_reference text)
        returns uuid
        language sql
        security definer
        set search_path = public
        as $$
            select tenant_id from subscription_payments
            where provider_reference = p_reference and deleted_at is null
            limit 1;
        $$
        """
    )
    op.execute("grant execute on function resolve_subscription_payment_tenant(text) to app_runtime")

    _seed_plans()

    # Backfill: tenant vivo sin suscripción → free con 30 días desde hoy.
    op.execute(
        """
        insert into tenant_subscriptions
            (id, tenant_id, plan_id, status, period_start, period_end, trial_ends_at)
        select gen_random_uuid(), t.id, 'free', 'trialing',
               now(), now() + interval '30 days', now() + interval '30 days'
        from tenants t
        where t.deleted_at is null
          and not exists (
              select 1 from tenant_subscriptions s
              where s.tenant_id = t.id and s.deleted_at is null
          )
        """
    )


def downgrade() -> None:
    op.execute("drop function if exists resolve_subscription_payment_tenant(text)")
    op.execute("drop function if exists upsert_plan(text, text, numeric, text, int, int, jsonb, jsonb, int, boolean, int)")
    for table in _BUSINESS_TABLES:
        op.execute(f"drop policy if exists tenant_isolation on {table}")
        op.execute(f"alter table {table} no force row level security")
        op.execute(f"alter table {table} disable row level security")
    op.execute("drop index if exists uq_subscription_payment_reference")
    op.execute("drop index if exists ix_subscription_payments_tenant")
    op.drop_table("subscription_payments")
    op.execute("drop index if exists uq_tenant_subscription_active")
    op.execute("drop index if exists ix_tenant_subscriptions_tenant")
    op.drop_table("tenant_subscriptions")
    op.drop_table("plans")
