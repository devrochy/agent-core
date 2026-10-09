"""Antiabuso y cumplimiento del alta automática (HU-SH-07, ADR-0125).

- `documentos_legales`: versiones publicadas de términos, privacidad y DPA de un negocio
  vendedor. Una versión nueva es una fila nueva (no se editan).
- `pedidos_de_alta`: un registro por pedido de un plan de la plataforma, con la aceptación
  de los documentos (versiones, cuándo, en qué mensaje) y las huellas HMAC del teléfono y
  del documento para «un Free por persona» y «muchos intentos». Nunca el dato en claro.

Las dos guardan la prueba del contrato: un trigger impide cambiar una versión publicada (solo
`deleted_at`) o la aceptación de un pedido (solo pueden cambiar las huellas, que se vacían a los
12 meses, `updated_at` y `deleted_at`), y borrar filas. `purge_tenant` desactiva los
triggers al purgar, así que no lo frena.

Las dos son tablas de NEGOCIO del vendedor (`tenant_id`, RLS habilitada y forzada, policy de
aislamiento, índices que empiezan por `tenant_id`). Tablas nuevas y vacías: sin reescritura
ni bloqueo largo. `purge_tenant` las cubre solo (descubre las tablas con `tenant_id`).

Revision ID: 0065
Revises: 0063
Create Date: 2026-10-09
"""
from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY, JSONB

from alembic import op

revision = "0065"
# 0064 es del PR #354 (checklist de producción), todavía abierto y también sobre 0063. El que
# se mergee segundo encadena su migración sobre la del otro: un solo head.
down_revision = "0063"
branch_labels = None
depends_on = None

_RLS = "nullif(current_setting('app.tenant_id', true), '')::uuid"
_TS = sa.DateTime(timezone=True)


def _rls(tabla: str) -> None:
    op.execute(f"ALTER TABLE {tabla} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {tabla} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY {tabla}_tenant_isolation ON {tabla} "
        f"USING (tenant_id = {_RLS}) WITH CHECK (tenant_id = {_RLS})"
    )
    # Explícito: los default privileges de 0003 solo valen si migra el mismo rol que la corrió.
    op.execute(
        f"""
        DO $do$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_runtime') THEN
                GRANT SELECT, INSERT, UPDATE, DELETE ON {tabla} TO app_runtime;
            END IF;
        END $do$;
        """
    )


_TRIGGERS = """
CREATE OR REPLACE FUNCTION documentos_legales_inmutables() RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'documentos_legales: sin borrado físico (deleted_at)';
    END IF;
    IF (to_jsonb(NEW) - 'deleted_at') IS DISTINCT FROM (to_jsonb(OLD) - 'deleted_at') THEN
        RAISE EXCEPTION 'documentos_legales: una versión publicada no se edita; publicar otra';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER documentos_legales_inmutables
    BEFORE UPDATE OR DELETE ON documentos_legales
    FOR EACH ROW EXECUTE FUNCTION documentos_legales_inmutables();

CREATE OR REPLACE FUNCTION pedidos_de_alta_aceptacion_inmutable() RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'pedidos_de_alta: sin borrado físico (deleted_at)';
    END IF;
    IF (to_jsonb(NEW) - ARRAY['huellas', 'updated_at', 'deleted_at'])
        IS DISTINCT FROM (to_jsonb(OLD) - ARRAY['huellas', 'updated_at', 'deleted_at']) THEN
        RAISE EXCEPTION 'pedidos_de_alta: la aceptación y los datos del pedido no se editan';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER pedidos_de_alta_aceptacion_inmutable
    BEFORE UPDATE OR DELETE ON pedidos_de_alta
    FOR EACH ROW EXECUTE FUNCTION pedidos_de_alta_aceptacion_inmutable();
"""


def upgrade() -> None:
    op.create_table(
        "documentos_legales",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("tipo", sa.Text(), nullable=False),
        sa.Column("version", sa.Text(), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("vigente_desde", _TS, nullable=False, server_default=sa.text("now()")),
        sa.Column("publicado_por", sa.Text(), nullable=False),
        sa.Column("created_at", _TS, nullable=False, server_default=sa.text("now()")),
        sa.Column("deleted_at", _TS, nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint("tipo in ('terminos', 'privacidad', 'dpa')", name="ck_documentos_legales_tipo"),
        sa.CheckConstraint(
            "version ~ '^[A-Za-z0-9][A-Za-z0-9._-]{0,39}$'", name="ck_documentos_legales_version"
        ),
        sa.CheckConstraint("url ~ '^https://[^[:space:]]+$' and char_length(url) <= 500", name="ck_documentos_legales_url"),
    )
    op.create_index(
        "uq_documentos_legales_tenant_tipo_version",
        "documentos_legales",
        ["tenant_id", "tipo", "version"],
        unique=True,
        postgresql_where=sa.text("deleted_at is null"),
    )
    op.create_index(
        "ix_documentos_legales_tenant_tipo_vigente",
        "documentos_legales",
        ["tenant_id", "tipo", "vigente_desde"],
    )
    _rls("documentos_legales")

    op.create_table(
        "pedidos_de_alta",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("referencia", sa.Text(), nullable=False),
        sa.Column("customer_id", sa.Uuid(), nullable=True),
        sa.Column("conversation_id", sa.Uuid(), nullable=True),
        sa.Column("canal", sa.Text(), nullable=True),
        sa.Column("gratis", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("huellas", ARRAY(sa.Text()), nullable=False, server_default=sa.text("'{}'::text[]")),
        sa.Column("senales", ARRAY(sa.Text()), nullable=False, server_default=sa.text("'{}'::text[]")),
        sa.Column("aceptacion", JSONB(), nullable=True),
        sa.Column("aceptado_en", _TS, nullable=True),
        sa.Column("created_at", _TS, nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", _TS, nullable=False, server_default=sa.text("now()")),
        sa.Column("deleted_at", _TS, nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint("(aceptacion is null) = (aceptado_en is null)", name="ck_pedidos_de_alta_aceptacion"),
    )
    op.create_index(
        "uq_pedidos_de_alta_tenant_referencia",
        "pedidos_de_alta",
        ["tenant_id", "referencia"],
        unique=True,
        postgresql_where=sa.text("deleted_at is null"),
    )
    op.create_index("ix_pedidos_de_alta_tenant_creado", "pedidos_de_alta", ["tenant_id", "created_at"])
    op.create_index(
        "ix_pedidos_de_alta_tenant_cliente", "pedidos_de_alta", ["tenant_id", "customer_id", "created_at"]
    )
    _rls("pedidos_de_alta")
    op.execute(_TRIGGERS)


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS pedidos_de_alta_aceptacion_inmutable ON pedidos_de_alta")
    op.execute("DROP FUNCTION IF EXISTS pedidos_de_alta_aceptacion_inmutable()")
    op.execute("DROP TRIGGER IF EXISTS documentos_legales_inmutables ON documentos_legales")
    op.execute("DROP FUNCTION IF EXISTS documentos_legales_inmutables()")
    for tabla, indices in (
        (
            "pedidos_de_alta",
            (
                "ix_pedidos_de_alta_tenant_cliente",
                "ix_pedidos_de_alta_tenant_creado",
                "uq_pedidos_de_alta_tenant_referencia",
            ),
        ),
        (
            "documentos_legales",
            ("ix_documentos_legales_tenant_tipo_vigente", "uq_documentos_legales_tenant_tipo_version"),
        ),
    ):
        op.execute(f"drop policy if exists {tabla}_tenant_isolation on {tabla}")
        op.execute(f"alter table {tabla} no force row level security")
        op.execute(f"alter table {tabla} disable row level security")
        for indice in indices:
            op.drop_index(indice, table_name=tabla)
        op.drop_table(tabla)
