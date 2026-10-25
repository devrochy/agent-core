"""Registro de avisos de cobro por cliente y día (HU-SUB-18).

La Ley 2300 de 2023 no permite contactar por cobranza más de una vez el mismo día. Cada
aviso de cobro (cargo nuevo, recordatorio, pausa por mora o reenvío del panel) reserva
primero la fila `(tenant_id, customer_id, fecha)` en la fecha local del negocio; si ya
existe, el aviso espera al día hábil siguiente. Solo inserción: no se edita ni se borra
(la purga del negocio la alcanza por `tenant_id`).

Revision ID: pack_subscriptions_0009
Revises: pack_subscriptions_0008
Create Date: 2026-10-24
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision = "pack_subscriptions_0009"
down_revision = "pack_subscriptions_0008"
branch_labels = None
depends_on = None

RLS = "nullif(current_setting('app.tenant_id', true), '')::uuid"
TABLA = "pack_subscriptions_avisos_cobro"


def upgrade() -> None:
    op.create_table(
        TABLA,
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
        sa.Column("customer_id", UUID(as_uuid=True), nullable=False),
        sa.Column("fecha", sa.Date(), nullable=False),
        sa.Column("topic", sa.Text(), nullable=False),
        sa.Column("charge_id", UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.PrimaryKeyConstraint("tenant_id", "customer_id", "fecha", name="pk_pack_subscriptions_avisos_cobro"),
    )
    op.create_index(f"ix_{TABLA}_tenant_fecha", TABLA, ["tenant_id", "fecha"])
    op.execute(f"alter table {TABLA} enable row level security")
    op.execute(f"alter table {TABLA} force row level security")
    op.execute(
        f"create policy tenant_isolation on {TABLA} "
        f"using (tenant_id = {RLS}) with check (tenant_id = {RLS})"
    )
    # Solo inserción (invariante 8): sin UPDATE ni DELETE para el runtime, y el trigger
    # `prevent_mutation()` del core (0067) lo impide también al dueño. purge_tenant lo apaga.
    op.execute(
        f"""
        DO $do$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_runtime') THEN
                REVOKE ALL ON {TABLA} FROM app_runtime;
                GRANT SELECT, INSERT ON {TABLA} TO app_runtime;
            END IF;
        END $do$;
        """
    )
    op.execute(
        f"CREATE TRIGGER {TABLA}_immutable BEFORE UPDATE OR DELETE ON {TABLA} "
        "FOR EACH ROW EXECUTE FUNCTION prevent_mutation()"
    )
    op.execute(
        f"CREATE TRIGGER {TABLA}_no_truncate BEFORE TRUNCATE ON {TABLA} "
        "FOR EACH STATEMENT EXECUTE FUNCTION prevent_mutation()"
    )


def downgrade() -> None:
    op.execute(f"drop table if exists {TABLA}")
