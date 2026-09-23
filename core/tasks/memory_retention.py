"""Retención de la memoria semántica (ADR-0053).

Purga lógicamente (`deleted_at`) los `memory_vectors` más viejos que
`MEMORY_RETENTION_DAYS`. `0` (default) = desactivado: no borra nada.

Enumera los tenants vivos (tabla de sistema, sin RLS) y entra a cada uno con su
propio `tenant_scope`, porque `memory_vectors` sí tiene RLS.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from core.config import get_settings
from core.db.models import Tenant
from core.db.session import get_session
from core.shared.logger import get_logger
from core.shared.tenant import tenant_scope

log = get_logger(__name__)


async def purge_expired_memory() -> dict[str, int]:
    """Marca `deleted_at` de la memoria más vieja que el TTL. Devuelve conteos."""
    from sqlalchemy import select, update

    from core.db.models import MemoryVector

    days = get_settings().memory_retention_days
    if days <= 0:
        return {"purged": 0, "tenants": 0}

    cutoff = datetime.now(UTC) - timedelta(days=days)
    purged = 0
    tenants_touched = 0

    async with get_session() as session:
        tenant_ids = (
            await session.execute(select(Tenant.id).where(Tenant.deleted_at.is_(None)))
        ).scalars().all()

    for tenant_id in tenant_ids:
        async with tenant_scope(tenant_id):
            async with get_session() as session:
                result = await session.execute(
                    update(MemoryVector)
                    .where(
                        MemoryVector.tenant_id == tenant_id,
                        MemoryVector.created_at < cutoff,
                        MemoryVector.deleted_at.is_(None),
                    )
                    .values(deleted_at=datetime.now(UTC))
                )
                if result.rowcount:
                    purged += result.rowcount
                    tenants_touched += 1

    if purged:
        log.info("memory_retention_purged", purged=purged, tenants=tenants_touched)
    return {"purged": purged, "tenants": tenants_touched}
