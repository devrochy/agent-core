"""Resolver el chat web por su clave pública (HU-CHAN-02).

`app_settings` tiene RLS forzada: la superficie pública `/public/web-chat/{key}`
no puede leer la config sin un tenant fijado. Se añade una función
`SECURITY DEFINER` acotada que devuelve SOLO `(tenant_id, config)` para una clave
dada, con `search_path` fijo. No expone otras columnas ni permite enumerar.

Revision ID: 0048
Revises: 0047
Create Date: 2026-10-04
"""
from __future__ import annotations

from alembic import op

revision = "0048"
down_revision = "0047"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("drop function if exists web_chat_resolve(text)")
    op.execute(
        """
        create function web_chat_resolve(p_key text)
        returns table(tenant_id uuid, config jsonb)
        language sql security definer set search_path = public as $$
            select a.tenant_id, a.web_chat_config
            from app_settings a
            where a.deleted_at is null
              and a.web_chat_config->>'key' = p_key
            limit 1;
        $$;
        """
    )
    # La superficie pública corre como `app_runtime`: dale EXECUTE.
    op.execute("grant execute on function web_chat_resolve(text) to app_runtime")


def downgrade() -> None:
    op.execute("drop function if exists web_chat_resolve(text)")
