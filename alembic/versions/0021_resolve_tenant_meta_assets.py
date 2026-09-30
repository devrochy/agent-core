"""Funciones SECURITY DEFINER para resolver tenant desde WABA/página/IG.

Mismo patrón que `resolve_tenant_for_phone` (migración 0003, ADR-0012): el
webhook de Meta necesita saber a qué tenant pertenece un evento ANTES de
poder setear `app.tenant_id` -- RLS bloquearía la consulta a
`waba_accounts`/`fb_pages`/`ig_accounts` en ese momento (chicken-and-egg).

Revision ID: 0021
Revises: 0020
Create Date: 2026-09-20
"""
from __future__ import annotations

from alembic import op

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION resolve_tenant_for_waba(p_waba_id text)
        RETURNS uuid LANGUAGE sql SECURITY DEFINER SET search_path = public AS $$
            SELECT tenant_id FROM waba_accounts WHERE waba_id = p_waba_id LIMIT 1;
        $$;

        CREATE OR REPLACE FUNCTION resolve_tenant_for_page(p_page_id text)
        RETURNS uuid LANGUAGE sql SECURITY DEFINER SET search_path = public AS $$
            SELECT tenant_id FROM fb_pages WHERE page_id = p_page_id LIMIT 1;
        $$;

        CREATE OR REPLACE FUNCTION resolve_tenant_for_ig_user(p_ig_user_id text)
        RETURNS uuid LANGUAGE sql SECURITY DEFINER SET search_path = public AS $$
            SELECT tenant_id FROM ig_accounts WHERE ig_user_id = p_ig_user_id LIMIT 1;
        $$;
        """
    )


def downgrade() -> None:
    op.execute("drop function if exists resolve_tenant_for_waba(text)")
    op.execute("drop function if exists resolve_tenant_for_page(text)")
    op.execute("drop function if exists resolve_tenant_for_ig_user(text)")
