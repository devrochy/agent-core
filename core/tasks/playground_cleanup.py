"""Purga de pruebas huérfanas del panel (HU-ARCH-14).

Cerrar la prueba en el panel la purga al instante. Esta tarea es la red de seguridad para
cuando el navegador se cierra sin avisar: borra las conversaciones `test` que llevan más de
`PLAYGROUND_RETENTION_HOURS` sin iniciarse. `0` desactiva la tarea.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from core.config import get_settings
from core.db.models import Conversation, Tenant
from core.db.session import get_session
from core.shared.logger import get_logger
from core.shared.tenant import tenant_scope
from core.shared.test_mode import TEST_CHANNEL

log = get_logger(__name__)


async def purgar_pruebas_vencidas() -> dict[str, int]:
    from core.admin.playground import _purga_test

    horas = get_settings().playground_retention_hours
    if horas <= 0:
        return {"conversaciones": 0, "tenants": 0}
    corte = datetime.now(UTC) - timedelta(hours=horas)
    tenants = 0
    conversaciones = 0

    async with get_session() as session:
        tenant_ids = (
            await session.execute(select(Tenant.id).where(Tenant.deleted_at.is_(None)))
        ).scalars().all()

    for tenant_id in tenant_ids:
        async with tenant_scope(tenant_id):
            async with get_session() as session:
                ids = list(
                    (
                        await session.execute(
                            select(Conversation.id).where(
                                Conversation.tenant_id == tenant_id,
                                Conversation.channel == TEST_CHANNEL,
                                Conversation.state["panel"].astext == "true",
                                Conversation.started_at < corte,
                            )
                        )
                    ).scalars().all()
                )
            if not ids:
                continue
            await _purga_test(tenant_id, ids)
            tenants += 1
            conversaciones += len(ids)

    if conversaciones:
        log.info("playground_cleanup", conversaciones=conversaciones, tenants=tenants)
    return {"conversaciones": conversaciones, "tenants": tenants}


__all__ = ["purgar_pruebas_vencidas"]
