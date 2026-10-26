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


# Suelta el candado solo si sigue siendo de quien lo tomó (comparar y borrar en un paso).
_SOLTAR = "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) else return 0 end"


def _limites() -> tuple[int, int]:
    timeout = get_settings().eval_timeout_segundos
    return timeout + 300, timeout + 180


async def _registrar_fallida(
    tenant_id: uuid.UUID, connection_id: uuid.UUID, release_id: uuid.UUID, actor: str, codigo: str
) -> uuid.UUID | None:
    """Un `evaluated` con `estado = fallida` (nunca un rechazo). Devuelve la base, o None si la release
    no es de esa conexión o no hay activa."""
    from sqlalchemy import text

    from core.db.session import get_session
    from core.releases.build import registrar_evento

    async with get_session() as session:
        fila = (
            await session.execute(
                text(
                    "SELECT c.active_release_id FROM channel_connections c JOIN connection_releases r "
                    "ON r.tenant_id = c.tenant_id AND r.connection_id = c.id "
                    "WHERE c.tenant_id = :t AND c.id = :c AND r.id = :r"
                ),
                {"t": tenant_id, "c": connection_id, "r": release_id},
            )
        ).first()
        if fila is None:
            return None
        await registrar_evento(
            session,
            tenant_id,
            connection_id,
            release_id,
            "evaluated",
            actor,
            {"estado": "fallida", "codigo": codigo, "base_release_id": str(fila[0]) if fila[0] else None},
        )
        return fila[0]


async def _soltar_pendiente(tenant_id: uuid.UUID, connection_id: uuid.UUID, release_id: uuid.UUID) -> None:
    """Libera la marca de «tarea pendiente» de la candidata contra la activa de ahora."""
    from sqlalchemy import text

    from core.db.session import get_session
    from core.memory.working import get_redis
    from core.releases.gate import clave_pendiente

    async with get_session() as session:
        activa = (
            await session.execute(
                text("SELECT active_release_id FROM channel_connections WHERE tenant_id = :t AND id = :c"),
                {"t": tenant_id, "c": connection_id},
            )
        ).scalar_one_or_none()
    if activa is not None:
        try:
            await get_redis().delete(clave_pendiente(tenant_id, release_id, activa))
        except Exception as exc:
            log.warning("release_gate_soltar_pendiente_fallo", error=type(exc).__name__)


async def agotada(tenant_id: str, connection_id: str, release_id: str, actor: str) -> None:
    """Se agotó la espera del candado: queda `fallida/corredor_ocupado` y se puede volver a pedir."""
    tid, cid, rid = uuid.UUID(tenant_id), uuid.UUID(connection_id), uuid.UUID(release_id)
    async with tenant_scope(tid):
        await _registrar_fallida(tid, cid, rid, actor, "corredor_ocupado")
        await _soltar_pendiente(tid, cid, rid)


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
            await _soltar_pendiente(tid, cid, rid)
            return "negocio_inactivo"
        if not base_de_eval_configurada():
            await _registrar_fallida(tid, cid, rid, actor, "sin_base_de_eval")
            await _soltar_pendiente(tid, cid, rid)
            log.warning("release_gate_sin_base_de_eval", release_id=release_id)
            return "sin_base_de_eval"
        redis = get_redis()
        dueno = uuid.uuid4().hex
        # Vence con el límite duro de la tarea: no puede arrancar otra medición mientras esta viva.
        if not await redis.set(CANDADO, dueno, nx=True, ex=_LIMITE):
            return "ocupado"
        try:
            resultado = await corredor.medir(tid, cid, rid, actor=actor)
        finally:
            await redis.eval(_SOLTAR, 1, CANDADO, dueno)
            await _soltar_pendiente(tid, cid, rid)
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
        if self.request.retries >= MAX_ESPERAS:
            run_async(agotada(tenant_id, connection_id, release_id, actor))
            return "corredor_ocupado"
        # Otra medición en curso: se espera sin escribir nada (el corredor sigue siendo idempotente).
        raise self.retry(countdown=REINTENTO_S)
    return estado
