"""Datos de prueba por tester y clasificación del catálogo (SPEC-30, HU-QA-07 y HU-QA-08).

1. `qa_items`: `entorno` (dónde se ejecuta la prueba: panel, apk, whatsapp, web o api),
   `area` (la sección del panel o la pantalla de la APK) y `requiere` (los datos de la guía
   que necesita). La sección nueva `datos` es la guía de datos externos.
2. `qa_tester_datasets`: el negocio de prueba de cada tester y sus tres cuentas (dueño,
   gestor y colaborador). Una fila por tester. Las contraseñas no se guardan: se muestran
   una sola vez.

Tabla de SISTEMA (sin RLS): `qa_tester_datasets` es de la plataforma, como el resto de
`qa_*` (ADR-0133); se consulta sin contexto de tenant. `tenant_id` es una FK lógica: si el
master purga el negocio, el tester lo vuelve a preparar. Grants mínimos a app_runtime.

Revision ID: 0089
Revises: 0088
Create Date: 2026-10-13
"""
from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY, UUID

from alembic import op

revision = "0089"
down_revision = "0088"
branch_labels = None
depends_on = None

_TS = sa.DateTime(timezone=True)


def _admin_fk(nombre: str, *, nullable: bool = True) -> sa.Column:
    return sa.Column(nombre, UUID(as_uuid=True), sa.ForeignKey("admins.id", ondelete="SET NULL"), nullable=nullable)


def upgrade() -> None:
    op.add_column("qa_items", sa.Column("entorno", sa.Text()))
    op.add_column("qa_items", sa.Column("area", sa.Text()))
    op.add_column("qa_items", sa.Column("requiere", ARRAY(sa.Text()), nullable=False, server_default="{}"))
    op.drop_constraint("ck_qa_items_seccion", "qa_items", type_="check")
    op.create_check_constraint("ck_qa_items_seccion", "qa_items", "seccion in ('pruebas', 'test', 'prod', 'datos')")
    op.create_check_constraint(
        "ck_qa_items_entorno", "qa_items", "entorno is null or entorno in ('panel', 'apk', 'whatsapp', 'web', 'api')"
    )

    op.create_table(
        "qa_tester_datasets",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("admin_id", UUID(as_uuid=True), sa.ForeignKey("admins.id", ondelete="CASCADE"), nullable=False),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
        sa.Column("slug", sa.Text(), nullable=False),
        _admin_fk("dueno_admin_id"),
        _admin_fk("gestor_admin_id"),
        _admin_fk("colaborador_admin_id"),
        sa.Column("estado", sa.Text(), nullable=False, server_default="creando"),
        sa.Column("corrida", UUID(as_uuid=True)),
        sa.Column("error", sa.Text()),
        sa.Column("creado_en", _TS, nullable=False),
        sa.Column("actualizado_en", _TS, nullable=False),
        sa.UniqueConstraint("admin_id", name="uq_qa_tester_datasets_admin"),
        sa.UniqueConstraint("slug", name="uq_qa_tester_datasets_slug"),
        sa.CheckConstraint("estado in ('creando', 'listo', 'error')", name="ck_qa_tester_datasets_estado"),
    )
    # Los default privileges de la 0003 dan todo: se deja solo lo que usa la app.
    op.execute("revoke all on qa_tester_datasets from app_runtime")
    op.execute("grant select, insert, update on qa_tester_datasets to app_runtime")


def downgrade() -> None:
    op.drop_table("qa_tester_datasets")
    op.drop_constraint("ck_qa_items_entorno", "qa_items", type_="check")
    op.execute("delete from qa_items where seccion = 'datos'")
    op.drop_constraint("ck_qa_items_seccion", "qa_items", type_="check")
    op.create_check_constraint("ck_qa_items_seccion", "qa_items", "seccion in ('pruebas', 'test', 'prod')")
    op.drop_column("qa_items", "requiere")
    op.drop_column("qa_items", "area")
    op.drop_column("qa_items", "entorno")
