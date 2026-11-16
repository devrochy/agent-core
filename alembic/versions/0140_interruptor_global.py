"""Tabla de SISTEMA (sin RLS, sin `tenant_id`): el interruptor global de la plataforma (HU-ARCH-38, ADR-0200).

- `platform_kill_switch`: una sola fila (`id = 1`) con el estado del interruptor que detiene todos los agentes
  de todos los negocios: activo, motivo, quién y cuándo. La siembra esta migración apagada: sin la fila el
  lector falla cerrado y la plataforma quedaría detenida.
- `platform_kill_switch_events`: historial de cada cambio y de cada intento denegado (un no master). Solo
  admite inserciones (`prevent_mutation`).

Es de la plataforma, no de un negocio: lo lee el camino caliente antes y fuera de cualquier `tenant_scope`.
Mismo criterio que `cron_job_configs` (0120, ADR-0183). Solo lo escribe `core/admin/kill_switch_service.py`.

Tablas nuevas y casi vacías: sin reescritura ni bloqueo largo.

Revision ID: 0140
Revises: 0139
Create Date: 2026-11-16
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0140"
down_revision = "0139"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "platform_kill_switch",
        sa.Column("id", sa.SmallInteger(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("changed_by", sa.Uuid(), nullable=True),
        sa.Column("changed_by_label", sa.Text(), nullable=True),
        sa.Column("source", sa.Text(), nullable=True),
        sa.Column("changed_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint("id = 1", name="ck_platform_kill_switch_una_fila"),
        sa.CheckConstraint("source is null or source in ('api','cli')", name="ck_platform_kill_switch_source"),
    )
    op.execute("INSERT INTO platform_kill_switch (id, active) VALUES (1, false) ON CONFLICT DO NOTHING")

    op.create_table(
        "platform_kill_switch_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("actor_admin_id", sa.Uuid(), nullable=True),
        sa.Column("actor_label", sa.Text(), nullable=True),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "action in ('activated','deactivated','denied')", name="ck_platform_kill_switch_events_action"
        ),
        sa.CheckConstraint("source in ('api','cli')", name="ck_platform_kill_switch_events_source"),
    )
    op.execute(
        "CREATE INDEX ix_platform_kill_switch_events_created ON platform_kill_switch_events (created_at DESC)"
    )
    # El historial no se edita ni se borra (mismo trigger que los ledgers del Agent OS, 0067).
    op.execute(
        "CREATE TRIGGER platform_kill_switch_events_inmutable BEFORE UPDATE OR DELETE "
        "ON platform_kill_switch_events FOR EACH ROW EXECUTE FUNCTION prevent_mutation()"
    )

    # Explícito: los default privileges de 0003 solo valen si migra el mismo rol que la corrió.
    op.execute(
        """
        DO $do$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_runtime') THEN
                GRANT SELECT, UPDATE ON platform_kill_switch TO app_runtime;
                GRANT SELECT, INSERT ON platform_kill_switch_events TO app_runtime;
            END IF;
        END $do$;
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS platform_kill_switch_events_inmutable ON platform_kill_switch_events")
    op.execute("DROP INDEX IF EXISTS ix_platform_kill_switch_events_created")
    op.drop_table("platform_kill_switch_events")
    op.drop_table("platform_kill_switch")
