"""Resolución de un tenant (slug o UUID) a `tenant_id`, para comandos del CLI.

Compartido por cualquier comando que necesite abrir un `tenant_scope` antes de
tocar tablas de negocio (RLS): sin esto, `select(...)` contra una tabla con
policy `tenant_isolation` no matchea ninguna fila y falla en silencio.
"""

from __future__ import annotations

import uuid

import typer


async def resolve_tenant(ref: str) -> uuid.UUID:
    """Slug o UUID (36 chars) -> `tenant_id`. Lanza `typer.BadParameter` si no existe."""
    from sqlalchemy import select

    from core.db.models import Tenant
    from core.db.session import get_session

    async with get_session() as session:
        if len(ref) == 36:
            row = await session.get(Tenant, uuid.UUID(ref))
        else:
            row = (
                await session.execute(select(Tenant).where(Tenant.slug == ref))
            ).scalar_one_or_none()
        if row is None:
            raise typer.BadParameter(f"Tenant no encontrado: {ref}")
        return row.id
