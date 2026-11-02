"""Próxima acción y fecha de seguimiento del lead, con su historia (HU-CRM-21, SPEC-33, ADR-0181).

`pack_commerce_leads` gana dos columnas nullable sin default (`next_action_at`, la fecha en UTC, y
`next_action_note`, tope 200): son solo metadato en Postgres. El índice parcial cubre los filtros
«Hoy», «Vencidos» y «Próximos»: solo indexa los leads que tienen seguimiento.

`pack_commerce_lead_followups` es nueva: un registro por cada seguimiento que se cierra (hecho,
reprogramado o cancelado), con su nota y la fecha que tenía, para la línea de tiempo del cliente.
`tenant_id`, RLS forzada y política de aislamiento. No se crea ninguna fila: los leads anteriores no
tenían seguimiento.

Aditiva: no toca datos.

Revision ID: crm_0018
Revises: crm_0017
Create Date: 2026-11-02
"""
from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision = "crm_0018"
down_revision = "crm_0017"
branch_labels = None
depends_on = None

_TABLE = "pack_commerce_lead_followups"


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
    # El ALTER pide un lock exclusivo breve en los leads: si una transacción larga lo tiene
    # tomado, fallar rápido en vez de encolar el tráfico detrás.
    op.execute("set local lock_timeout = '5s'")
    op.add_column("pack_commerce_leads", sa.Column("next_action_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("pack_commerce_leads", sa.Column("next_action_note", sa.Text(), nullable=True))
    op.create_check_constraint(
        "ck_pack_commerce_leads_next_action_note_len",
        "pack_commerce_leads",
        "next_action_note is null or char_length(next_action_note) <= 200",
    )
    op.create_index(
        "ix_pack_commerce_leads_seguimiento",
        "pack_commerce_leads",
        ["tenant_id", "next_action_at"],
        postgresql_where=sa.text("next_action_at is not null"),
    )
    op.create_table(
        _TABLE,
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
        sa.Column("lead_id", UUID(as_uuid=True), nullable=False),
        sa.Column("customer_id", UUID(as_uuid=True), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("outcome", sa.Text(), nullable=False),
        sa.Column("actor", sa.Text(), nullable=False, server_default="persona"),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint(
            "outcome in ('hecho','reprogramado','cancelado')", name="ck_pack_commerce_lead_followups_outcome"
        ),
        sa.CheckConstraint("actor in ('persona','sistema')", name="ck_pack_commerce_lead_followups_actor"),
    )
    # Línea de tiempo de la ficha: los seguimientos del cliente, del más nuevo al más viejo.
    op.create_index("ix_pack_commerce_lead_followups_cliente", _TABLE, ["tenant_id", "customer_id", "closed_at", "id"])
    _rls(_TABLE)


def downgrade() -> None:
    # Borra la historia de seguimientos y las columnas del lead. Reversa solo si no hay historia
    # ni seguimientos programados: quitarlos con datos los perdería en silencio.
    conn = op.get_bind()
    # Las tablas tienen RLS forzada: sin esto los conteos verían 0 filas.
    conn.execute(sa.text("set local row_security = off"))
    historia = conn.execute(sa.text(f"select count(*) from {_TABLE}")).scalar()
    programados = conn.execute(
        sa.text("select count(*) from pack_commerce_leads where next_action_at is not null")
    ).scalar()
    if historia or programados:
        raise RuntimeError("crm_0018: hay seguimientos guardados; la reversa los borraría.")
    op.execute("set local lock_timeout = '5s'")
    op.execute(f"drop policy if exists tenant_isolation on {_TABLE}")
    op.drop_index("ix_pack_commerce_lead_followups_cliente", table_name=_TABLE)
    op.drop_table(_TABLE)
    op.drop_index("ix_pack_commerce_leads_seguimiento", table_name="pack_commerce_leads")
    op.drop_constraint("ck_pack_commerce_leads_next_action_note_len", "pack_commerce_leads", type_="check")
    op.drop_column("pack_commerce_leads", "next_action_note")
    op.drop_column("pack_commerce_leads", "next_action_at")
