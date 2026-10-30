"""Retención de los eventos de acceso (ADR-0174 §4, HU-ACC-06).

Da de baja lógica (`deleted_at`, invariante 8: sin borrado físico) los eventos más viejos que
`RETENCION_DIAS`. La tabla es append-only: el trigger solo deja pasar esa baja lógica. Enumera los
negocios vivos (tabla de sistema, sin RLS), entra a cada uno con su `tenant_scope` y actualiza en
lotes para no tomar un lock largo. Mismo patrón que `notifications_retention`.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update

from core.db.models import AccessEvent, Tenant
from core.db.session import get_session
from core.shared.logger import get_logger
from core.shared.tenant import tenant_scope

log = get_logger(__name__)

RETENCION_DIAS = 180
LOTE = 1000


async def retener_eventos_de_acceso(ahora: datetime | None = None) -> dict[str, int]:
    """Marca `deleted_at` de los eventos vencidos. Devuelve conteos."""
    corte = (ahora or datetime.now(UTC)) - timedelta(days=RETENCION_DIAS)
    async with get_session() as session:
        tenant_ids = (
            await session.execute(select(Tenant.id).where(Tenant.deleted_at.is_(None)))
        ).scalars().all()

    dadas_de_baja = 0
    negocios = 0
    for tenant_id in tenant_ids:
        tocado = False
        try:
            async with tenant_scope(tenant_id):
                while True:
                    async with get_session() as session:
                        ids = (
                            await session.execute(
                                select(AccessEvent.id)
                                .where(
                                    AccessEvent.tenant_id == tenant_id,
                                    AccessEvent.deleted_at.is_(None),
                                    AccessEvent.occurred_at < corte,
                                )
                                .limit(LOTE)
                            )
                        ).scalars().all()
                        if not ids:
                            break
                        await session.execute(
                            update(AccessEvent)
                            .where(AccessEvent.tenant_id == tenant_id, AccessEvent.id.in_(ids))
                            .values(deleted_at=datetime.now(UTC))
                        )
                    dadas_de_baja += len(ids)
                    tocado = True
        except Exception:
            # Un negocio que falla (un lock, por ejemplo) no frena la retención de los demás.
            log.warning("access_events_retention_negocio_fallo", tenant_id=str(tenant_id), exc_info=True)
        negocios += int(tocado)

    if dadas_de_baja:
        log.info("access_events_retention", dadas_de_baja=dadas_de_baja, negocios=negocios)
    return {"dadas_de_baja": dadas_de_baja, "negocios": negocios}
