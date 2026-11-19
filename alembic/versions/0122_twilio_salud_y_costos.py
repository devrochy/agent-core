"""WhatsApp vía Twilio: salud y costos por mensaje (ADR-0186 §11, PR-5).

1. `admin_twilio_connection_ids()`, SECURITY DEFINER: el cron de salud recorre las conexiones de Twilio de TODOS
   los negocios (un `select` sin `tenant_scope` devolvería 0 filas por la RLS forzada), mismo patrón que
   `admin_meta_connection_ids` (0033). Devuelve solo `(channel_connection_id, tenant_id)` de las vivas y activas;
   cada una se lee después dentro de su `tenant_scope`.
2. `message_costs` aprende el proveedor y el monto, porque el recurso Message de Twilio sí trae precio:
   - `provider` (NOT NULL, default `'meta'`: las filas de Meta no cambian; agregar una columna con default
     constante no reescribe la tabla).
   - `price` (numeric 12,6, positivo: Twilio lo informa en negativo) y `price_unit` (ISO 4217, `usd`).
   - Un costo por mensaje de Twilio: índice único parcial `(tenant_id, external_message_id) WHERE provider =
     'twilio'` (empieza por `tenant_id`, invariante 1). Meta sigue sin único: cada evento con pricing es un hecho.

Exige BYPASSRLS como 0121. Reversible: el downgrade quita la función, el índice y las columnas (se pierden los
costos de Twilio, que con la bandera apagada no existen).

Revision ID: 0122
Revises: 0121
Create Date: 2026-11-07
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0122"
down_revision = "0121"
branch_labels = None
depends_on = None

_PUEDE_SALTAR_RLS = "select rolsuper or rolbypassrls from pg_roles where rolname = current_user"


def upgrade() -> None:
    if not op.get_bind().execute(sa.text(_PUEDE_SALTAR_RLS)).scalar():
        raise RuntimeError(
            "0122: el rol de migraciones no es superusuario ni tiene BYPASSRLS. El enumerador SECURITY DEFINER de "
            "Twilio quedaría sujeto a la RLS forzada. Corre la migración con el dueño de la base."
        )
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column(
        "message_costs", sa.Column("provider", sa.Text(), nullable=False, server_default="meta")
    )
    op.add_column("message_costs", sa.Column("price", sa.Numeric(12, 6), nullable=True))
    op.add_column("message_costs", sa.Column("price_unit", sa.Text(), nullable=True))
    op.create_check_constraint(
        "ck_message_costs_price", "message_costs", "price is null or price >= 0"
    )
    op.create_index(
        "uq_message_costs_twilio_mensaje",
        "message_costs",
        ["tenant_id", "external_message_id"],
        unique=True,
        postgresql_where=sa.text("provider = 'twilio'"),
    )

    op.execute(
        """
        CREATE OR REPLACE FUNCTION admin_twilio_connection_ids()
        RETURNS TABLE(id uuid, tenant_id uuid)
        LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
            SELECT cc.id, cc.tenant_id
            FROM channel_connections cc
            JOIN twilio_connections tc
              ON tc.channel_connection_id = cc.id AND tc.tenant_id = cc.tenant_id
            WHERE cc.provider = 'twilio'
              AND cc.active
              AND cc.deleted_at IS NULL
              AND tc.deleted_at IS NULL;
        $$;
        """
    )
    op.execute("REVOKE ALL ON FUNCTION admin_twilio_connection_ids() FROM PUBLIC")
    op.execute(
        """
        DO $do$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_runtime') THEN
                GRANT EXECUTE ON FUNCTION admin_twilio_connection_ids() TO app_runtime;
            END IF;
        END $do$;
        """
    )


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS admin_twilio_connection_ids()")
    op.drop_index("uq_message_costs_twilio_mensaje", table_name="message_costs")
    op.drop_constraint("ck_message_costs_price", "message_costs", type_="check")
    op.drop_column("message_costs", "price_unit")
    op.drop_column("message_costs", "price")
    op.drop_column("message_costs", "provider")
