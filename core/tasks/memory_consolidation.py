"""Consolidación nocturna de la memoria (ADR-0114 §1 y §3, diseño §7.5, HU-AOS-36).

Pasa a `memory_facts` los datos confirmados de las tareas cerradas (`completed`, o `cancelled`
por `episode_expired`) de los últimos `VENTANA_DIAS`, con su procedencia. Corre fuera del turno:
no suma latencia ni locks a la conversación, y un fallo no tumba ningún turno. Es idempotente
(una tarea con hechos no se vuelve a mirar, aunque se hayan borrado) y no resucita lo olvidado
(`customers.memory_purged_at`).

Una tarea que no deja hechos (sin datos confirmados, o solo de pago) se vuelve a mirar cada
noche hasta salir de la ventana: es una lectura por el índice `ix_conversation_tasks_closed`.

Hoy no hay tareas sin `tenants.aos_procedures` encendida, así que con las banderas apagadas
esto no escribe nada.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from core.db.models import Tenant
from core.db.session import get_session
from core.shared.logger import get_logger
from core.shared.tenant import tenant_scope

log = get_logger(__name__)

VENTANA_DIAS = 7


async def consolidar_negocio(tenant_id: uuid.UUID, *, now: datetime | None = None) -> dict[str, int]:
    """Consolida las tareas cerradas de un negocio. Cada tarea en su propia transacción."""
    from core.memory import facts

    ahora = now or datetime.now(UTC)
    desde = ahora - timedelta(days=VENTANA_DIAS)
    tareas = 0
    hechos = 0
    async with tenant_scope(tenant_id):
        async with get_session() as session:
            pendientes = await facts.tareas_por_consolidar(session, tenant_id=tenant_id, desde=desde)
        for tarea in pendientes:
            try:
                async with get_session() as session:
                    hechos += await facts.consolidar_tarea(session, tenant_id=tenant_id, tarea=tarea, now=ahora)
                tareas += 1
            except Exception as exc:
                # Sin traza: el error de SQLAlchemy repite los parámetros (datos del cliente).
                log.error("memory_consolidation_task_failed", task_id=str(tarea.id), error_type=type(exc).__name__)
    return {"tareas": tareas, "hechos": hechos}


async def consolidar_memoria() -> dict[str, int]:
    """Cron: recorre los negocios vivos (tabla de sistema, sin RLS) y consolida cada uno."""
    async with get_session() as session:
        tenant_ids = (
            await session.execute(select(Tenant.id).where(Tenant.deleted_at.is_(None)))
        ).scalars().all()
    total = {"tareas": 0, "hechos": 0}
    for tenant_id in tenant_ids:
        try:
            parcial = await consolidar_negocio(tenant_id)
        except Exception as exc:
            log.error("memory_consolidation_tenant_failed", tenant_id=str(tenant_id), error_type=type(exc).__name__)
            continue
        total["tareas"] += parcial["tareas"]
        total["hechos"] += parcial["hechos"]
    if total["hechos"]:
        log.info("memory_consolidated", **total)
    return total
