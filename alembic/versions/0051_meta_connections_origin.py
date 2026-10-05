"""`meta_connections.origin`: origen guardado de cada conexión (HU-CONN-01, ADR-0094).

Cada conexión de Meta se creó por una de tres vías y de ella depende de dónde salen
sus credenciales: `byok` (token y App Secret propios del negocio), `embedded_signup`
(token de la conexión, App Secret de la plataforma) o `manual`. Hasta ahora el código
lo adivinaba mirando qué tokens existían; ahora queda guardado.

Backfill (fila por fila, con la evidencia que dejó cada vía; no se filtra por
`revoked_at`, el origen es un hecho histórico):

1. tiene token `cloud_api_access_token`            -> `byok` (solo lo escribe el alta BYOK)
2. tiene token `business_integration_system_user`
   o un `connection_wizards` asociado               -> `embedded_signup`
3. tiene token `app_access` (secreto propio) o
   `webhook_verify_token` (solo lo escribe BYOK)    -> `byok`
4. sin evidencia                                    -> `manual`

RLS: `meta_connections` tiene RLS forzada, así que un UPDATE de un rol sin bypass no
falla: afecta 0 filas. Por eso se recorre tenant por tenant fijando `app.tenant_id`
(igual que el runtime) y se verifica que no quede ninguna fila sin origen; además
`SET NOT NULL` valida toda la tabla sin RLS y aborta la migración si algo escapó.

Revision ID: 0051
Revises: 0050
Create Date: 2026-10-05
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0051"
down_revision = "0050"
branch_labels = None
depends_on = None

_BACKFILL = """
update meta_connections c
set origin = case
    when exists (
        select 1 from meta_private.meta_tokens t
        where t.connection_id = c.id and t.kind = 'cloud_api_access_token'
    ) then 'byok'
    when exists (
        select 1 from meta_private.meta_tokens t
        where t.connection_id = c.id and t.kind = 'business_integration_system_user'
    ) or exists (
        select 1 from connection_wizards w where w.connection_id = c.id
    ) then 'embedded_signup'
    when exists (
        select 1 from meta_private.meta_tokens t
        where t.connection_id = c.id and t.kind in ('app_access', 'webhook_verify_token')
    ) then 'byok'
    else 'manual'
end
where c.origin is null
"""


def upgrade() -> None:
    op.add_column("meta_connections", sa.Column("origin", sa.Text(), nullable=True))

    conn = op.get_bind()
    tenant_ids = [row[0] for row in conn.execute(sa.text("select id from tenants"))]
    try:
        for tenant_id in tenant_ids:
            conn.execute(sa.text("select set_config('app.tenant_id', :tid, false)"), {"tid": str(tenant_id)})
            conn.execute(sa.text(_BACKFILL))
            pending = conn.execute(
                sa.text("select count(*) from meta_connections where origin is null")
            ).scalar_one()
            if pending:
                raise RuntimeError(
                    f"meta_connections.origin: {pending} fila(s) del tenant {tenant_id} sin origen tras el backfill"
                )
    finally:
        conn.execute(sa.text("select set_config('app.tenant_id', '', false)"))

    op.alter_column("meta_connections", "origin", nullable=False, server_default="manual")
    op.create_check_constraint(
        "ck_meta_connections_origin",
        "meta_connections",
        "origin in ('byok','embedded_signup','manual')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_meta_connections_origin", "meta_connections", type_="check")
    op.drop_column("meta_connections", "origin")
