"""Enumeración cross-tenant de assets de Meta para los crons de core.

Las funciones SQL subyacentes son SECURITY DEFINER (migración 0033, mismo
patrón que `resolve_tenant_for_phone`/`admin_tenants`, ADR-0012): omiten RLS
SOLO para listar `id` + `tenant_id` -- nunca secretos ni contenido. Cada cron
hidrata/opera el asset bajo el `tenant_scope` de su tenant, así el resto de
los datos sigue protegido por RLS.

Sin esto, los crons enumeraban sin scope y RLS fail-closed devolvía 0 filas:
corrían como no-op silencioso.
"""

from __future__ import annotations

import uuid

from sqlalchemy import String, bindparam, text
from sqlalchemy.dialects.postgresql import ARRAY

from core.db.session import get_session


async def admin_meta_connection_ids(statuses: list[str]) -> list[tuple[uuid.UUID, uuid.UUID]]:
    """Pares `(connection_id, tenant_id)` de `MetaConnection` con esos status
    (excluye soft-deleted), de TODOS los tenants."""
    stmt = text("select id, tenant_id from admin_meta_connection_ids(:statuses)").bindparams(
        bindparam("statuses", type_=ARRAY(String))
    )
    async with get_session() as session:
        rows = (await session.execute(stmt, {"statuses": list(statuses)})).all()
    return [(row.id, row.tenant_id) for row in rows]


async def admin_connected_waba_ids() -> list[tuple[uuid.UUID, uuid.UUID]]:
    """Pares `(waba_id, tenant_id)` de WABAs cuya conexión está `connected`,
    de TODOS los tenants."""
    async with get_session() as session:
        rows = (await session.execute(text("select id, tenant_id from admin_connected_waba_ids()"))).all()
    return [(row.id, row.tenant_id) for row in rows]

