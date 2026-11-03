"""Envoltorio común de los crons (ADR-0183): pausa, lock anti-solape e historial.

Todos los crons (los de core con `core_cron` y los de los packs con `register_cron`) pasan por
`run_cron`. Antes cada uno repetía `try/run_async/except → log.exception`, así que Celery veía
SUCCESS aunque el cron fallara y no quedaba rastro de qué corrió, cuándo ni cómo terminó.

Reglas:
- Sigue SIN relanzar la excepción de un cron (comportamiento de siempre: un fallo no mata al
  worker ni dispara reintentos); ahora además queda `failed` en `cron_runs`.
- El registro del historial es *best-effort*: si la base falla, el cron corre igual.
- Una ejecución programada de un cron pausado sale en silencio (sin fila: sería ruido cada
  minuto). Una ejecución manual ignora la pausa: es una orden explícita.
- Un lock en Redis (`cron:run:<nombre>`) impide que dos ejecuciones del mismo cron se solapen;
  el que llega segundo queda `skipped` (`en_ejecucion`).
- Los crons con `record_success=False` (latido, outbox) no guardan cada éxito; sí guardan sus
  fallos y sus ejecuciones manuales.
"""

from __future__ import annotations

import inspect
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from core.admin import crons_service as svc
from core.db.models import utcnow
from core.shared.async_bridge import run_async
from core.shared.logger import get_logger
from core.tasks.cron_catalog import get_meta

log = get_logger(__name__)

# Motivos de una ejecución omitida (columna `detail`).
OMITIDA_EN_EJECUCION = "en_ejecucion"
OMITIDA_PLUGIN_INACTIVO = "plugin_inactivo"

Job = Callable[[], Awaitable[None] | None]
SkipIf = Callable[[], str | None]


def _uuid_o_none(valor: str | uuid.UUID | None) -> uuid.UUID | None:
    if valor is None or isinstance(valor, uuid.UUID):
        return valor
    try:
        return uuid.UUID(str(valor))
    except ValueError:
        return None


def _seguro(coro: Awaitable[Any], *, que: str, task: str, default: Any = None) -> Any:
    """Corre una operación del historial; si falla, lo registra y sigue (nunca tumba al cron)."""
    try:
        return run_async(coro)  # type: ignore[arg-type]
    except Exception as exc:
        log.error("cron_registro_fallido", task=task, operacion=que, error=type(exc).__name__)
        return default


def run_cron(
    task_name: str,
    job: Job,
    *,
    trigger: str = "scheduled",
    triggered_by: str | uuid.UUID | None = None,
    skip_if: SkipIf | None = None,
) -> None:
    """Ejecuta `job` con pausa, lock e historial. `skip_if` devuelve el motivo de omitirla."""
    meta = get_meta(task_name)
    manual = trigger == "manual"
    guardar_exito = manual or meta is None or meta.record_success
    quien = _uuid_o_none(triggered_by)

    if not manual and _seguro(svc.esta_pausado(task_name), que="pausa", task=task_name, default=False):
        log.info("cron_pausado_omitido", task=task_name)
        return

    inicio_reloj = time.monotonic()
    inicio = utcnow()
    motivo = skip_if() if skip_if is not None else None
    # Sin Redis no hay lock, pero el cron corre: peor es no cobrar que cobrar sin lock.
    tomado = _seguro(svc.tomar_lock(task_name), que="lock", task=task_name, default=True)
    if tomado is not True and not motivo:
        motivo = OMITIDA_EN_EJECUCION

    if motivo:
        log.info("cron_omitido", task=task_name, motivo=motivo)
        if tomado:
            _seguro(svc.soltar_lock(task_name), que="soltar_lock", task=task_name)
        if guardar_exito:
            _seguro(
                svc.registrar_ejecucion(task_name, "skipped", trigger, quien, inicio, 0, detail=motivo),
                que="omitida", task=task_name,
            )
        return

    run_id = None
    if guardar_exito:
        run_id = _seguro(svc.iniciar_ejecucion(task_name, trigger, quien), que="inicio", task=task_name)

    # Falla por defecto: una BaseException (cancelación, SystemExit) que cruce el `finally` no debe quedar
    # anotada como exitosa.
    status, error_class = "failed", "Interrumpida"
    try:
        resultado = job()
        if inspect.isawaitable(resultado):
            run_async(resultado)  # type: ignore[arg-type]
        status, error_class = "success", None
    except Exception as exc:
        status, error_class = "failed", type(exc).__name__
        log.exception("cron_task_failed", task=task_name)
    finally:
        duracion = int((time.monotonic() - inicio_reloj) * 1000)
        if run_id is not None:
            _seguro(svc.cerrar_ejecucion(run_id, status, duracion, error_class), que="cierre", task=task_name)
        elif status == "failed":
            _seguro(
                svc.registrar_ejecucion(task_name, "failed", trigger, quien, inicio, duracion, error_class),
                que="fallo", task=task_name,
            )
        _seguro(svc.soltar_lock(task_name), que="soltar_lock", task=task_name)
