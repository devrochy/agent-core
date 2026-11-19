"""`meta_connections.origin`: modo de credenciales de cada conexión (HU-CONN-01, ADR-0094).

Cada conexión de Meta usa las credenciales de una de tres vías: `byok` (token y App
Secret propios del negocio), `embedded_signup` (token de la conexión, App Secret de la
plataforma) o `manual` (igual que ES). Hasta ahora cada servicio lo adivinaba mirando
qué tokens existían; ahora queda guardado y lo leen todos (`core/gateway/meta_credentials.py`).

Backfill, conexión por conexión, con la evidencia que dejó cada alta (no se filtra por
`revoked_at`):

1. tiene token `cloud_api_access_token`            -> `byok` (solo lo escribe el alta BYOK)
2. tiene token `business_integration_system_user`
   o un `connection_wizards` asociado               -> `embedded_signup`
3. tiene token `app_access` (secreto propio) o
   `webhook_verify_token` (solo lo escribe BYOK)    -> `byok`
4. sin evidencia                                    -> `manual`

Con tokens de BYOK y de ES a la vez (el wizard de ES reutilizó una conexión BYOK) gana
`byok`: es el token con el que el sender enviaba hasta hoy (probaba BYOK primero,
ADR-0040 §3), así el despliegue no cambia con qué token sale cada mensaje. Esas
conexiones se listan en el log para revisarlas a mano (`debug_token`).

RLS: `meta_connections` tiene RLS forzada, así que un UPDATE de un rol sin bypass no
falla: afecta 0 filas. Por eso se recorre tenant por tenant fijando `app.tenant_id`
(igual que el runtime) y se verifica que no quede ninguna fila sin origen; además
`SET NOT NULL` valida toda la tabla sin RLS y aborta la migración si algo escapó.
`set_config` es transaccional: si algo falla, se revierte junto con la migración.

Sin valor por defecto: un alta que olvide fijar `origin` debe fallar, no quedar en
silencio con las credenciales de la plataforma.

Revision ID: 0051
Revises: 0050
Create Date: 2026-10-05
"""
from __future__ import annotations

import logging

import sqlalchemy as sa

from alembic import op

revision = "0051"
down_revision = "0050"
branch_labels = None
depends_on = None

log = logging.getLogger("alembic.runtime.migration")

_BACKFILL = """
update meta_connections c
set origin = case
    when exists (
        select 1 from meta_private.meta_tokens t
        where t.connection_id = c.id and t.tenant_id = c.tenant_id and t.kind = 'cloud_api_access_token'
    ) then 'byok'
    when exists (
        select 1 from meta_private.meta_tokens t
        where t.connection_id = c.id and t.tenant_id = c.tenant_id and t.kind = 'business_integration_system_user'
    ) or exists (
        select 1 from connection_wizards w where w.connection_id = c.id and w.tenant_id = c.tenant_id
    ) then 'embedded_signup'
    when exists (
        select 1 from meta_private.meta_tokens t
        where t.connection_id = c.id and t.tenant_id = c.tenant_id
          and t.kind in ('app_access', 'webhook_verify_token')
    ) then 'byok'
    else 'manual'
end
where c.origin is null
"""

# Conexiones con tokens de BYOK y de ES a la vez: quedan `byok` y se revisan a mano.
_BOTH_FAMILIES = """
select c.id from meta_connections c
where exists (
    select 1 from meta_private.meta_tokens t
    where t.connection_id = c.id and t.tenant_id = c.tenant_id and t.kind = 'cloud_api_access_token'
) and exists (
    select 1 from meta_private.meta_tokens t
    where t.connection_id = c.id and t.tenant_id = c.tenant_id and t.kind = 'business_integration_system_user'
)
"""


def upgrade() -> None:
    op.add_column("meta_connections", sa.Column("origin", sa.Text(), nullable=True))

    conn = op.get_bind()
    tenant_ids = [row[0] for row in conn.execute(sa.text("select id from tenants"))]
    both: list[str] = []
    for tenant_id in tenant_ids:
        conn.execute(sa.text("select set_config('app.tenant_id', :tid, false)"), {"tid": str(tenant_id)})
        both.extend(str(row[0]) for row in conn.execute(sa.text(_BOTH_FAMILIES)))
        conn.execute(sa.text(_BACKFILL))
        pending = conn.execute(sa.text("select count(*) from meta_connections where origin is null")).scalar_one()
        if pending:
            raise RuntimeError(
                f"meta_connections.origin: {pending} fila(s) del tenant {tenant_id} sin origen tras el backfill"
            )
    conn.execute(sa.text("select set_config('app.tenant_id', '', false)"))

    if both:
        log.warning(
            "meta_connections.origin: %d conexión(es) con tokens de BYOK y de Embedded Signup quedaron "
            "'byok' (el token con el que se enviaba); revisarlas con debug_token: %s",
            len(both), ", ".join(both),
        )

    op.alter_column("meta_connections", "origin", nullable=False)
    op.create_check_constraint(
        "ck_meta_connections_origin",
        "meta_connections",
        "origin in ('byok','embedded_signup','manual')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_meta_connections_origin", "meta_connections", type_="check")
    op.drop_column("meta_connections", "origin")
