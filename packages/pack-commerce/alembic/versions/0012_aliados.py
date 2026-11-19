"""Aliados (proveedores) opcionales por negocio (HU-CRM-03, SPEC-33).

`pack_commerce_allies` es nueva: los proveedores del negocio (nombre, contacto y estado),
como `allies` de agent-sale (`migrations/0039`). `ally_id` en productos y promociones es
nullable y sin aliado sembrado (D-SPEC-33-3): los negocios de hoy no cambian y no hay
reescritura de datos.

FK lógica, como el resto del pack (sin FOREIGN KEY física): los aliados se borran de forma
lógica (`deleted_at`) y la API limpia las referencias al borrar.

`add column` nullable sin default es solo metadato en Postgres (sin reescritura). Los
índices se crean sin `concurrently`: las tablas del pack son chicas (catálogo por negocio)
y Alembic corre en transacción.

Revision ID: crm_0012
Revises: crm_0011
Create Date: 2026-10-21
"""
from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

from alembic import op

revision = "crm_0012"
down_revision = "crm_0011"
branch_labels = None
depends_on = None

_TABLE = "pack_commerce_allies"


def _rls(table: str) -> None:
    op.execute(f"alter table {table} enable row level security")
    op.execute(f"alter table {table} force row level security")
    op.execute(f"drop policy if exists tenant_isolation on {table}")
    op.execute(
        f"create policy tenant_isolation on {table} using "
        "(tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid) "
        "with check (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)"
    )
    op.execute(f"grant select, insert, update, delete on {table} to app_runtime")


def upgrade() -> None:
    # Los ALTER piden un lock exclusivo breve en productos y promociones: si una transacción
    # larga los tiene tomados, fallar rápido en vez de encolar el tráfico detrás.
    op.execute("set local lock_timeout = '5s'")
    op.create_table(
        _TABLE,
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("contact_info", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint("length(btrim(name)) between 1 and 120", name="ck_pack_commerce_allies_name"),
    )
    # Un nombre por negocio (sin distinguir mayúsculas) entre los aliados vivos: el import
    # CSV resuelve el aliado por nombre.
    op.create_index(
        "uq_pack_commerce_allies_tenant_name",
        _TABLE,
        ["tenant_id", sa.text("lower(name)")],
        unique=True,
        postgresql_where=sa.text("deleted_at is null"),
    )
    _rls(_TABLE)

    op.add_column("pack_commerce_products", sa.Column("ally_id", UUID(as_uuid=True), nullable=True))
    op.add_column("pack_commerce_promotions", sa.Column("ally_id", UUID(as_uuid=True), nullable=True))
    # Filtro «Productos por aliado» y limpieza al borrar un aliado.
    op.create_index(
        "ix_pack_commerce_products_tenant_ally",
        "pack_commerce_products",
        ["tenant_id", "ally_id"],
        postgresql_where=sa.text("ally_id is not null"),
    )
    op.create_index(
        "ix_pack_commerce_promotions_tenant_ally",
        "pack_commerce_promotions",
        ["tenant_id", "ally_id"],
        postgresql_where=sa.text("ally_id is not null"),
    )


def downgrade() -> None:
    # Reversa solo si no hay datos de aliados: quitarla con datos los perdería en silencio.
    conn = op.get_bind()
    # Las tablas tienen RLS forzada: sin esto el conteo vería 0 filas. Si el rol no puede
    # saltarse RLS, Postgres falla en vez de filtrar (falla cerrada).
    conn.execute(sa.text("set local row_security = off"))
    con_datos = conn.execute(
        sa.text(
            "select (select count(*) from pack_commerce_allies)"
            " + (select count(*) from pack_commerce_products where ally_id is not null)"
            " + (select count(*) from pack_commerce_promotions where ally_id is not null)"
        )
    ).scalar()
    if con_datos:
        raise RuntimeError(
            "crm_0012: hay aliados o productos/promociones con aliado; la reversa los perdería."
        )
    op.drop_index("ix_pack_commerce_promotions_tenant_ally", table_name="pack_commerce_promotions")
    op.drop_index("ix_pack_commerce_products_tenant_ally", table_name="pack_commerce_products")
    op.drop_column("pack_commerce_promotions", "ally_id")
    op.drop_column("pack_commerce_products", "ally_id")
    op.execute(f"drop policy if exists tenant_isolation on {_TABLE}")
    op.drop_index("uq_pack_commerce_allies_tenant_name", table_name=_TABLE)
    op.drop_table(_TABLE)
