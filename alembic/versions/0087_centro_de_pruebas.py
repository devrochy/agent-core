"""Centro de pruebas (SPEC-30, HU-QA-01): catálogo, ciclos, ejecuciones por tester, pasos, capturas e historial.

Tabla de SISTEMA (sin RLS): las nueve tablas `qa_*` son de la plataforma, no de un
negocio (D-SPEC-30-3, ADR-0133). No llevan `tenant_id`: las usan master y el rol
tester desde la sección global `qa`, y ninguna query de negocio las toca. Grants
mínimos a app_runtime; `qa_events` solo admite insertar y leer.

Revision ID: 0087
Revises: 0086
Create Date: 2026-10-13
"""
from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID

from alembic import op

revision = "0087"
down_revision = "0086"
branch_labels = None
depends_on = None

_TS = sa.DateTime(timezone=True)


def _admin_fk(name: str, nullable: bool = True) -> sa.Column:
    return sa.Column(name, UUID(as_uuid=True), sa.ForeignKey("admins.id"), nullable=nullable)


def upgrade() -> None:
    op.create_table(
        "qa_catalog_versions",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("fecha", sa.Text(), nullable=False, server_default=""),
        sa.Column("commit_docs", sa.Text()),
        sa.Column("contenido_sha256", sa.Text(), nullable=False, unique=True),
        _admin_fk("importado_por"),
        sa.Column("importado_en", _TS, nullable=False),
    )
    op.create_table(
        "qa_items",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "catalog_version_id",
            UUID(as_uuid=True),
            sa.ForeignKey("qa_catalog_versions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("slug", sa.Text(), nullable=False),
        sa.Column("seccion", sa.Text(), nullable=False),
        sa.Column("orden", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("modulo", sa.Text()),
        sa.Column("grupo", sa.Text(), nullable=False, server_default=""),
        sa.Column("titulo", sa.Text(), nullable=False),
        sa.Column("resumen", sa.Text()),
        sa.Column("contexto", sa.Text()),
        sa.Column("prioridad", sa.Text()),
        sa.Column("plataforma", ARRAY(sa.Text()), nullable=False, server_default="{}"),
        sa.Column("precondiciones", ARRAY(sa.Text()), nullable=False, server_default="{}"),
        sa.Column("datos", ARRAY(sa.Text()), nullable=False, server_default="{}"),
        sa.Column("pasos", JSONB(), nullable=False, server_default="[]"),
        sa.Column("esperado", sa.Text()),
        sa.Column("porque", sa.Text()),
        sa.Column("como", sa.Text()),
        sa.Column("vars", ARRAY(sa.Text()), nullable=False, server_default="{}"),
        sa.Column("quien", sa.Text()),
        sa.Column("hu", ARRAY(sa.Text()), nullable=False, server_default="{}"),
        sa.Column("origen", ARRAY(sa.Text()), nullable=False, server_default="{}"),
        sa.UniqueConstraint("catalog_version_id", "slug", name="uq_qa_items_version_slug"),
        sa.CheckConstraint("seccion in ('pruebas', 'test', 'prod')", name="ck_qa_items_seccion"),
    )
    op.create_table(
        "qa_cycles",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("nombre", sa.Text(), nullable=False),
        sa.Column(
            "catalog_version_id",
            UUID(as_uuid=True),
            sa.ForeignKey("qa_catalog_versions.id"),
            nullable=False,
        ),
        _admin_fk("abierto_por"),
        sa.Column("abierto_en", _TS, nullable=False),
        _admin_fk("cerrado_por"),
        sa.Column("cerrado_en", _TS),
        sa.Column("resumen", JSONB()),
    )
    # Un solo ciclo abierto a la vez.
    op.execute("create unique index uq_qa_cycles_uno_abierto on qa_cycles ((true)) where cerrado_en is null")
    op.create_table(
        "qa_runs",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("cycle_id", UUID(as_uuid=True), sa.ForeignKey("qa_cycles.id"), nullable=False),
        _admin_fk("admin_id", nullable=False),
        sa.Column("estado", sa.Text(), nullable=False, server_default="en_curso"),
        sa.Column("comentario", sa.Text()),
        sa.Column("informe", JSONB()),
        sa.Column("iniciada_en", _TS, nullable=False),
        sa.Column("actualizada_en", _TS, nullable=False),
        sa.Column("enviada_en", _TS),
        sa.UniqueConstraint("cycle_id", "admin_id", name="uq_qa_runs_cycle_admin"),
        sa.CheckConstraint("estado in ('en_curso', 'enviada')", name="ck_qa_runs_estado"),
    )
    op.create_table(
        "qa_step_results",
        sa.Column(
            "run_id", UUID(as_uuid=True), sa.ForeignKey("qa_runs.id", ondelete="CASCADE"), primary_key=True
        ),
        sa.Column("item_slug", sa.Text(), primary_key=True),
        sa.Column("paso", sa.Integer(), primary_key=True),
        sa.Column("estado", sa.Text()),
        sa.Column("nota", sa.Text()),
        sa.Column("actualizado_en", _TS, nullable=False),
        sa.CheckConstraint(
            "estado in ('ok', 'falla', 'bloqueado', 'omitido')", name="ck_qa_step_results_estado"
        ),
    )
    op.create_table(
        "qa_item_results",
        sa.Column(
            "run_id", UUID(as_uuid=True), sa.ForeignKey("qa_runs.id", ondelete="CASCADE"), primary_key=True
        ),
        sa.Column("item_slug", sa.Text(), primary_key=True),
        sa.Column("estado", sa.Text(), nullable=False, server_default="sin_correr"),
        sa.Column("nota", sa.Text()),
        sa.Column("actualizado_en", _TS, nullable=False),
    )
    op.create_table(
        "qa_attachments",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "run_id", UUID(as_uuid=True), sa.ForeignKey("qa_runs.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("item_slug", sa.Text(), nullable=False),
        sa.Column("paso", sa.Integer(), nullable=False),
        sa.Column("ruta", sa.Text(), nullable=False),
        sa.Column("nombre_original", sa.Text()),
        sa.Column("mimetype", sa.Text(), nullable=False),
        sa.Column("tamano", sa.Integer(), nullable=False),
        _admin_fk("subido_por", nullable=False),
        sa.Column("subido_en", _TS, nullable=False),
    )
    op.create_index("ix_qa_attachments_run_item", "qa_attachments", ["run_id", "item_slug"])
    op.create_table(
        "qa_config_status",
        sa.Column("item_slug", sa.Text(), primary_key=True),
        sa.Column("estado", sa.Text(), nullable=False, server_default="pendiente"),
        sa.Column("nota", sa.Text()),
        _admin_fk("actualizado_por"),
        sa.Column("actualizado_en", _TS, nullable=False),
        sa.CheckConstraint(
            "estado in ('pendiente', 'en_curso', 'listo', 'no_aplica')",
            name="ck_qa_config_status_estado",
        ),
    )
    op.create_table(
        "qa_events",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("cycle_id", UUID(as_uuid=True), sa.ForeignKey("qa_cycles.id")),
        sa.Column("run_id", UUID(as_uuid=True), sa.ForeignKey("qa_runs.id", ondelete="SET NULL")),
        sa.Column("item_slug", sa.Text()),
        sa.Column("paso", sa.Integer()),
        sa.Column("campo", sa.Text(), nullable=False),
        sa.Column("antes", sa.Text()),
        sa.Column("despues", sa.Text()),
        _admin_fk("admin_id"),
        sa.Column("en", _TS, nullable=False),
    )
    op.create_index("ix_qa_events_cycle_item", "qa_events", ["cycle_id", "item_slug", "en"])

    for table in (
        "qa_catalog_versions",
        "qa_items",
        "qa_cycles",
        "qa_runs",
        "qa_step_results",
        "qa_item_results",
        "qa_attachments",
        "qa_config_status",
    ):
        op.execute(f"grant select, insert, update, delete on {table} to app_runtime")
    # El historial es de solo agregar (el `set null` del FK lo hace el dueño de la tabla).
    op.execute("grant select, insert on qa_events to app_runtime")


def downgrade() -> None:
    op.drop_index("ix_qa_events_cycle_item", table_name="qa_events")
    op.drop_table("qa_events")
    op.drop_table("qa_config_status")
    op.drop_index("ix_qa_attachments_run_item", table_name="qa_attachments")
    op.drop_table("qa_attachments")
    op.drop_table("qa_item_results")
    op.drop_table("qa_step_results")
    op.drop_table("qa_runs")
    op.execute("drop index if exists uq_qa_cycles_uno_abierto")
    op.drop_table("qa_cycles")
    op.drop_table("qa_items")
    op.drop_table("qa_catalog_versions")
