"""Retención del centro de notificaciones (ADR-0167).

Da de baja lógica (`deleted_at`, invariante 8: sin borrado físico) las notificaciones más
viejas que `RETENCION_DIAS`, leídas o no: una no leída nunca vence sola y la tabla no puede
crecer sin tope. Enumera los negocios vivos (tabla de sistema, sin RLS) y entra a cada uno con
su `tenant_scope`; actualiza en lotes para no tomar un lock largo.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select, update

from core.db.models import Notification, Tenant
from core.db.session import get_session
from core.notifications.feed import limite_de_retencion
from core.shared.logger import get_logger
from core.shared.tenant import tenant_scope

log = get_logger(__name__)

RETENCION_DIAS = 180
LOTE = 1000


async def retener_notificaciones(ahora: datetime | None = None) -> dict[str, int]:
    """Marca `deleted_at` de las notificaciones vencidas. Devuelve conteos."""
    corte = limite_de_retencion(ahora, RETENCION_DIAS)
    async with get_session() as session:
        tenant_ids = (
            await session.execute(select(Tenant.id).where(Tenant.deleted_at.is_(None)))
        ).scalars().all()

    dadas_de_baja = 0
    negocios = 0
    for tenant_id in tenant_ids:
        tocado = False
        async with tenant_scope(tenant_id):
            while True:
                async with get_session() as session:
                    ids = (
                        await session.execute(
                            select(Notification.id)
                            .where(
                                Notification.tenant_id == tenant_id,
                                Notification.deleted_at.is_(None),
                                Notification.created_at < corte,
                            )
                            .limit(LOTE)
                        )
                    ).scalars().all()
                    if not ids:
                        break
                    await session.execute(
                        update(Notification)
                        .where(Notification.tenant_id == tenant_id, Notification.id.in_(ids))
                        .values(deleted_at=datetime.now(UTC))
                    )
                dadas_de_baja += len(ids)
                tocado = True
        negocios += int(tocado)

    if dadas_de_baja:
        log.info("notifications_retention", dadas_de_baja=dadas_de_baja, negocios=negocios)
    return {"dadas_de_baja": dadas_de_baja, "negocios": negocios}
