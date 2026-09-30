"""Ciclo de vida completo de plantillas de WhatsApp: límites cacheados en
waba_accounts, detalle de deshabilitación/recategorización/calidad y
contador de ediciones en message_templates.

`ck_message_templates_status` gana 'UNARCHIVED' -- es uno de los 14 valores
de `event` que manda el webhook `message_template_status_update`, y el
status de la plantilla se persiste literal (nunca reinterpretado, CLAUDE.md
invariante nº7) aunque hoy no sea un status "real" documentado de Meta.

Revision ID: 0026
Revises: 0025
Create Date: 2026-09-20
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0026"
down_revision = "0025"
branch_labels = None
depends_on = None

_OLD_STATUSES = (
    "'DRAFT','PENDING','APPROVED','REJECTED','PAUSED','DISABLED','IN_APPEAL',"
    "'ARCHIVED','LOCKED','LIMIT_EXCEEDED','FLAGGED','REINSTATED','PENDING_DELETION','DELETED'"
)
_NEW_STATUSES = _OLD_STATUSES + ",'UNARCHIVED'"


def upgrade() -> None:
    op.add_column("waba_accounts", sa.Column("message_template_count", sa.Integer(), nullable=True))
    op.add_column("waba_accounts", sa.Column("message_template_limit", sa.Integer(), nullable=True))
    op.add_column("waba_accounts", sa.Column("are_translations_complete", sa.Boolean(), nullable=True))
    op.add_column(
        "waba_accounts", sa.Column("template_limits_synced_at", sa.DateTime(timezone=True), nullable=True)
    )

    op.add_column("message_templates", sa.Column("disable_date", sa.DateTime(timezone=True), nullable=True))
    op.add_column("message_templates", sa.Column("other_info_title", sa.Text(), nullable=True))
    op.add_column("message_templates", sa.Column("other_info_description", sa.Text(), nullable=True))
    op.add_column("message_templates", sa.Column("category_changed_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("message_templates", sa.Column("previous_category", sa.Text(), nullable=True))
    op.add_column("message_templates", sa.Column("previous_quality_score", sa.Text(), nullable=True))
    op.add_column("message_templates", sa.Column("last_edited_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "message_templates",
        sa.Column("edits_this_month", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column("message_templates", sa.Column("edits_month_key", sa.Text(), nullable=True))

    op.execute("alter table message_templates drop constraint ck_message_templates_status")
    op.execute(f"alter table message_templates add constraint ck_message_templates_status check (status in ({_NEW_STATUSES}))")

    # Mismo patrón que `resolve_tenant_for_waba`/`_page`/`_ig_user` (migración
    # 0021): los webhooks de plantillas (`message_template_status_update`,
    # `template_category_update`, `message_template_quality_update`) solo
    # traen `message_template_id` -- RLS bloquearía el lookup en
    # `message_templates` sin tenant seteado todavía (chicken-and-egg).
    op.execute(
        """
        CREATE OR REPLACE FUNCTION resolve_tenant_for_message_template(p_meta_template_id text)
        RETURNS uuid LANGUAGE sql SECURITY DEFINER SET search_path = public AS $$
            SELECT tenant_id FROM message_templates WHERE meta_template_id = p_meta_template_id LIMIT 1;
        $$;
        """
    )


def downgrade() -> None:
    op.execute("drop function if exists resolve_tenant_for_message_template(text)")

    op.execute("alter table message_templates drop constraint ck_message_templates_status")
    op.execute(f"alter table message_templates add constraint ck_message_templates_status check (status in ({_OLD_STATUSES}))")

    op.drop_column("message_templates", "edits_month_key")
    op.drop_column("message_templates", "edits_this_month")
    op.drop_column("message_templates", "last_edited_at")
    op.drop_column("message_templates", "previous_quality_score")
    op.drop_column("message_templates", "previous_category")
    op.drop_column("message_templates", "category_changed_at")
    op.drop_column("message_templates", "other_info_description")
    op.drop_column("message_templates", "other_info_title")
    op.drop_column("message_templates", "disable_date")

    op.drop_column("waba_accounts", "template_limits_synced_at")
    op.drop_column("waba_accounts", "are_translations_complete")
    op.drop_column("waba_accounts", "message_template_limit")
    op.drop_column("waba_accounts", "message_template_count")
