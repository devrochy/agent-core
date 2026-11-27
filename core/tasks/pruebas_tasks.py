"""Tarea del worker que corre una corrida de pruebas del agente (HU-AOS-103, ADR-0212 §5).

`core.pruebas.correr` va en el worker y la cola de siempre (ADR-0212 §5: una cola propia solo si se mide que las
corridas frenan los turnos reales; hoy no hay `-Q` ni `task_routes` en el repo y una cola con nombre que ningún
worker consuma dejaría las corridas en cola para siempre).

- «Un negocio a la vez» lo da el reclamo `en_cola → corriendo` de `core.pruebas.corredor` (con el índice único parcial
  `uq_agent_test_runs_activa`), no un candado de Redis: es atómico, sobrevive a un reinicio y hace idempotente un reenvío.
- `acks_late=False` (como el gate): con la reentrega de Redis el mensaje volvería a mitad de la corrida.
- `time_limit`/`soft_time_limit` explícitos: el global de Celery (300 s) mataría una corrida de decenas de intentos. Van
  por debajo de las 2 h del barrido, así la tarea cierra su corrida antes de que el barrido la dé por colgada. El límite
  suave cierra la corrida en `fallo`; el corredor lo ve entre turnos y consolida lo que quedaba abierto.
"""

from __future__ import annotations

import uuid

from celery.exceptions import SoftTimeLimitExceeded

from core.shared.async_bridge import run_async
from core.shared.logger import get_logger
from core.tasks.celery_app import celery

log = get_logger(__name__)

NOMBRE = "core.pruebas.correr"
LIMITE_SUAVE_S = 100 * 60
LIMITE_DURO_S = 105 * 60


async def _fallo_por_tiempo(tenant_id: uuid.UUID, run_id: uuid.UUID) -> None:
    from core.pruebas import corredor
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tenant_id):
        await corredor.cerrar(tenant_id, run_id, corredor.FALLO, codigo="tiempo_agotado")


@celery.task(bind=True, name=NOMBRE, acks_late=False, time_limit=LIMITE_DURO_S, soft_time_limit=LIMITE_SUAVE_S)
def correr_pruebas(self, tenant_id: str, run_id: str) -> str:  # type: ignore[no-untyped-def]
    from core.pruebas.corredor import correr

    tid, rid = uuid.UUID(tenant_id), uuid.UUID(run_id)
    try:
        return run_async(correr(tid, rid))
    except SoftTimeLimitExceeded:
        run_async(_fallo_por_tiempo(tid, rid))
        return "fallo"


def encolar(tenant_id: uuid.UUID, run_id: uuid.UUID) -> None:
    """Manda la corrida al worker. Lo llama la ruta DESPUÉS de crear la corrida (el worker no vería la fila antes del
    commit). Si el broker falla, la excepción sube y quien llama cierra la corrida."""
    celery.send_task(NOMBRE, kwargs={"tenant_id": str(tenant_id), "run_id": str(run_id)})


__all__ = ["NOMBRE", "correr_pruebas", "encolar"]
