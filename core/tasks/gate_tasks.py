"""Tarea del worker que mide una release candidata (HU-AOS-21 parte 4, adenda de ADR-0117).

`core.releases.evaluar_candidata` corre `corredor.medir` dentro del `tenant_scope` del negocio. Va en
la cola y el worker de siempre; la «concurrencia 1» de las mediciones la da un candado de Redis
(`gate:corredor`), tomado antes de medir: si está ocupado, la tarea se reprograma sin escribir nada.

- `time_limit`/`soft_time_limit` por encima de `EVAL_TIMEOUT_SEGUNDOS`: el límite global de Celery
  (300 s) mataría la medición y dejaría huérfano el subproceso, que seguiría gastando LLM.
- `acks_late=False`: con la reentrega de Redis (una hora) el mensaje volvería justo cuando vence el
  reclamo `en_curso` y se mediría dos veces.
- Sin la base de eval configurada en el worker, no reclama ni exporta la foto: deja un `evaluated` con
  `estado = fallida` y `codigo = sin_base_de_eval` (nunca rechaza).
"""

from __future__ import annotations

import uuid

from core.config import get_settings
from core.shared.async_bridge import run_async
from core.shared.logger import get_logger
from core.shared.tenant import tenant_scope
from core.tasks.celery_app import celery

log = get_logger(__name__)

NOMBRE = "core.releases.evaluar_candidata"
CANDADO = "gate:corredor"
REINTENTO_S = 120
MAX_ESPERAS = 60  # dos horas esperando el candado


def _limites() -> tuple[int, int]:
    timeout = get_settings().eval_timeout_segundos
    return timeout + 300, timeout + 180


async def _registrar_sin_base(tenant_id: uuid.UUID, connection_id: uuid.UUID, release_id: uuid.UUID, actor: str) -> None:
    from sqlalchemy import text

    from core.db.session import get_session
    from core.releases.build import registrar_evento

    async with get_session() as session:
        activa = (
            await session.execute(
                text("SELECT active_release_id FROM channel_connections WHERE tenant_id = :t AND id = :c"),
                {"t": tenant_id, "c": connection_id},
            )
        ).scalar_one_or_none()
        await registrar_evento(
            session,
            tenant_id,
            connection_id,
            release_id,
            "evaluated",
            actor,
            {
                "estado": "fallida",
                "codigo": "sin_base_de_eval",
                "base_release_id": str(activa) if activa else None,
            },
        )


async def _negocio_activo(tenant_id: uuid.UUID) -> bool:
    from sqlalchemy import text

    from core.db.session import get_session

    async with get_session() as session:
        estado = (
            await session.execute(
                text("SELECT status FROM tenants WHERE id = :t AND deleted_at IS NULL"), {"t": tenant_id}
            )
        ).scalar_one_or_none()
    return estado is not None and estado != "suspended"


async def evaluar(tenant_id: str, connection_id: str, release_id: str, actor: str) -> str:
    """El trabajo de la tarea. Devuelve el estado (`ocupado` = otro proceso tiene el candado)."""
    from core.memory.working import get_redis
    from core.releases import corredor
    from core.releases.gate import base_de_eval_configurada

    tid, cid, rid = uuid.UUID(tenant_id), uuid.UUID(connection_id), uuid.UUID(release_id)
    async with tenant_scope(tid):
        if not await _negocio_activo(tid):
            log.info("release_gate_negocio_inactivo", tenant_id=tenant_id)
            return "negocio_inactivo"
        if not base_de_eval_configurada():
            await _registrar_sin_base(tid, cid, rid, actor)
            log.warning("release_gate_sin_base_de_eval", release_id=release_id)
            return "sin_base_de_eval"
        redis = get_redis()
        dueno = uuid.uuid4().hex
        if not await redis.set(CANDADO, dueno, nx=True, ex=get_settings().eval_timeout_segundos + 60):
            return "ocupado"
        try:
            resultado = await corredor.medir(tid, cid, rid, actor=actor)
        finally:
            # Solo lo suelta quien lo tomó (si venció, otro proceso pudo tomarlo).
            if await redis.get(CANDADO) == dueno:
                await redis.delete(CANDADO)
    log.info("release_gate_medida", release_id=release_id, estado=resultado.estado, costo=resultado.costo_usd)
    return resultado.estado


_LIMITE, _LIMITE_SUAVE = _limites()


@celery.task(
    bind=True,
    name=NOMBRE,
    acks_late=False,
    time_limit=_LIMITE,
    soft_time_limit=_LIMITE_SUAVE,
    max_retries=MAX_ESPERAS,
)
def evaluar_candidata(self, tenant_id: str, connection_id: str, release_id: str, actor: str = "system:gate") -> str:  # type: ignore[no-untyped-def]
    estado = run_async(evaluar(tenant_id, connection_id, release_id, actor))
    if estado == "ocupado":
        # Otra medición en curso: se espera sin escribir nada (el corredor sigue siendo idempotente).
        raise self.retry(countdown=REINTENTO_S)
    return estado
