"""Plantillas de WhatsApp vía Twilio (ADR-0186 §8, PR-6).

`twilio_content_templates`, tabla de NEGOCIO (invariante 1): una plantilla de la Content API (`HX…`) de la cuenta de
una conexión de Twilio, con su aprobación de WhatsApp.

- FK compuesta `(tenant_id, channel_connection_id)` a `channel_connections` (no puede apuntar a otro negocio).
- `friendly_name` único entre las vivas de la conexión; `content_sid` único entre las vivas. Borrado lógico.
- `approval_status`: `creating` (el Content todavía no existe en Twilio), `unsubmitted` (existe, sin pedir
  aprobación), `received`/`pending` (en revisión), `approved`, `rejected`, `paused`, `disabled`.
- Polling de la aprobación (Twilio no avisa por webhook): `next_poll_at` con su índice parcial y el enumerador
  `admin_twilio_plantillas_por_revisar(limite)`, SECURITY DEFINER (el cron recorre todos los negocios; solo
  devuelve `(id, tenant_id)` y cada fila se lee después en su `tenant_scope`). Exige BYPASSRLS como 0121.

Reversible: el downgrade elimina la función y la tabla (con la bandera apagada no hay plantillas de Twilio).

Revision ID: 0123
Revises: 0122
Create Date: 2026-11-07
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

from alembic import op

revision = "0123"
down_revision = "0122"
branch_labels = None
depends_on = None

RLS = "nullif(current_setting('app.tenant_id', true), '')::uuid"
_UUID = UUID(as_uuid=True)
_TS = sa.DateTime(timezone=True)
_AHORA = sa.text("now()")
_TABLA = "twilio_content_templates"
_ESTADOS = "'creating','unsubmitted','received','pending','approved','rejected','paused','disabled'"
_PUEDE_SALTAR_RLS = "select rolsuper or rolbypassrls from pg_roles where rolname = current_user"


def _rls(table: str) -> None:
    op.execute(f"alter table {table} enable row level security")
    op.execute(f"alter table {table} force row level security")
    op.execute(
        f"create policy tenant_isolation on {table} "
        f"using (tenant_id = {RLS}) with check (tenant_id = {RLS})"
    )
    op.execute(f"revoke all on {table} from app_runtime")
    op.execute(f"grant select, insert, update on {table} to app_runtime")


def upgrade() -> None:
    if not op.get_bind().execute(sa.text(_PUEDE_SALTAR_RLS)).scalar():
        raise RuntimeError(
            "0123: el rol de migraciones no es superusuario ni tiene BYPASSRLS. El enumerador SECURITY DEFINER de "
            "plantillas de Twilio quedaría sujeto a la RLS forzada. Corre la migración con el dueño de la base."
        )
    op.create_table(
        _TABLA,
        sa.Column("id", _UUID, primary_key=True),
        sa.Column("tenant_id", _UUID, nullable=False),
        sa.Column("channel_connection_id", _UUID, nullable=False),
        sa.Column("content_sid", sa.Text(), nullable=True),
        sa.Column("friendly_name", sa.Text(), nullable=False),
        sa.Column("whatsapp_name", sa.Text(), nullable=False),
        sa.Column("language", sa.Text(), nullable=False),
        sa.Column("category", sa.Text(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False, server_default=""),
        sa.Column("types", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("variables", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        # `solvershub` (creada desde el panel) o `twilio` (importada de la consola): las importadas no se borran en
        # Twilio al borrarlas aquí; pueden usarse fuera de solvershub.
        sa.Column("origen", sa.Text(), nullable=False, server_default="solvershub"),
        sa.Column("approval_status", sa.Text(), nullable=False, server_default="creating"),
        sa.Column("rejection_reason", sa.Text(), nullable=False, server_default=""),
        sa.Column("submitted_at", _TS, nullable=True),
        sa.Column("approved_at", _TS, nullable=True),
        sa.Column("last_polled_at", _TS, nullable=True),
        sa.Column("next_poll_at", _TS, nullable=True),
        sa.Column("deleted_at", _TS, nullable=True),
        sa.Column("created_at", _TS, nullable=False, server_default=_AHORA),
        sa.Column("updated_at", _TS, nullable=False, server_default=_AHORA),
        sa.ForeignKeyConstraint(
            ["tenant_id", "channel_connection_id"],
            ["channel_connections.tenant_id", "channel_connections.id"],
            name="fk_twilio_content_templates_conexion",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            f"approval_status in ({_ESTADOS})", name="ck_twilio_content_templates_estado"
        ),
        sa.CheckConstraint(
            "category in ('UTILITY','MARKETING','AUTHENTICATION')",
            name="ck_twilio_content_templates_categoria",
        ),
        sa.CheckConstraint(
            "friendly_name ~ '^[a-z0-9_]{1,64}$'", name="ck_twilio_content_templates_nombre"
        ),
        sa.CheckConstraint(
            "content_sid is null or content_sid ~ '^HX[0-9a-f]{32}$'",
            name="ck_twilio_content_templates_sid",
        ),
        sa.CheckConstraint("char_length(body) <= 1024", name="ck_twilio_content_templates_body"),
    )
    op.create_index(
        "uq_twilio_content_templates_nombre_vivo",
        _TABLA,
        ["tenant_id", "channel_connection_id", "friendly_name"],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    op.create_index(
        "uq_twilio_content_templates_sid_vivo",
        _TABLA,
        ["tenant_id", "channel_connection_id", "content_sid"],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL AND content_sid IS NOT NULL"),
    )
    op.create_index(
        "ix_twilio_content_templates_por_revisar",
        _TABLA,
        ["next_poll_at"],
        postgresql_where=sa.text("deleted_at IS NULL AND next_poll_at IS NOT NULL"),
    )
    _rls(_TABLA)

    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION admin_twilio_plantillas_por_revisar(p_limite integer)
        RETURNS TABLE(id uuid, tenant_id uuid)
        LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
            SELECT t.id, t.tenant_id
            FROM {_TABLA} t
            JOIN channel_connections cc
              ON cc.id = t.channel_connection_id AND cc.tenant_id = t.tenant_id
            WHERE t.deleted_at IS NULL
              AND t.next_poll_at IS NOT NULL
              AND t.next_poll_at <= now()
              AND cc.deleted_at IS NULL
              AND cc.active
              AND cc.provider = 'twilio'
              AND EXISTS (
                SELECT 1 FROM twilio_connections tc
                WHERE tc.channel_connection_id = cc.id AND tc.tenant_id = cc.tenant_id AND tc.deleted_at IS NULL
              )
            ORDER BY t.next_poll_at
            LIMIT least(greatest(p_limite, 1), 500);
        $$;
        """
    )
    op.execute("REVOKE ALL ON FUNCTION admin_twilio_plantillas_por_revisar(integer) FROM PUBLIC")
    op.execute(
        """
        DO $do$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_runtime') THEN
                GRANT EXECUTE ON FUNCTION admin_twilio_plantillas_por_revisar(integer) TO app_runtime;
            END IF;
        END $do$;
        """
    )


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS admin_twilio_plantillas_por_revisar(integer)")
    op.execute(f"drop policy if exists tenant_isolation on {_TABLA}")
    op.execute(f"alter table {_TABLA} no force row level security")
    op.execute(f"alter table {_TABLA} disable row level security")
    op.drop_index("ix_twilio_content_templates_por_revisar", table_name=_TABLA)
    op.drop_index("uq_twilio_content_templates_sid_vivo", table_name=_TABLA)
    op.drop_index("uq_twilio_content_templates_nombre_vivo", table_name=_TABLA)
    op.drop_table(_TABLA)
