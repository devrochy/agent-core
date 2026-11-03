"""Planificador del beat con horarios reprogramables desde la base (ADR-0183).

El `PersistentScheduler` por defecto lee el horario del código al arrancar y no cambia hasta el
siguiente despliegue. Este reemplazo, activado con `-S core.tasks.cron_scheduler:DbOverrideScheduler`,
vuelve a leer `cron_job_configs` cada `refresh_interval_s` y aplica el horario reprogramado de cada
cron, sin reiniciar el beat. Quitar la reprogramación vuelve al horario por defecto (el del código).

Trampas de Celery que este módulo respeta:

- `merge_inplace(b)` BORRA del planificador todo lo que no esté en `b`. Por eso siempre se le pasa
  una copia COMPLETA de `beat_schedule` con los horarios reprogramados, nunca solo los cambiados.
- `merge_inplace` actualiza las entradas existentes EN EL MISMO OBJETO que guarda `old_schedulers`
  (copia superficial), así que `tick()` no detecta el cambio y el montículo de próximas
  ejecuciones queda con los tiempos viejos. Cuando cambia un horario se fuerza `_heap = None`.
- `beat_init` vuelve a hacer `merge_inplace(beat_schedule)` con los crons de los packs
  (`celery_app._load_plugins_on_beat_start`), lo que deshace las reprogramaciones: ese hook llama
  a `refrescar_overrides(force=True)` justo después.

Si la base no responde, el beat sigue con lo que tiene (los horarios por defecto o los últimos
leídos) y lo deja en el log: nunca se queda sin crons por un fallo de lectura.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Mapping
from typing import Any

from celery.beat import PersistentScheduler

from core.shared.async_bridge import run_async
from core.shared.logger import get_logger
from core.tasks.cron_catalog import get_meta, parse_expression

log = get_logger(__name__)

LECTURA_TIMEOUT_S = 5.0


def horario_efectivo(
    beat_schedule: Mapping[str, Mapping[str, Any]], overrides: Mapping[str, str]
) -> dict[str, dict[str, Any]]:
    """Copia completa de `beat_schedule` con el horario reprogramado de cada cron que lo tenga.

    Ignora (y registra) un override de un cron que no existe, que no es reprogramable o cuya
    expresión no es válida: el cron conserva su horario por defecto."""
    efectivo = {nombre: dict(entrada) for nombre, entrada in beat_schedule.items()}
    for nombre, expresion in overrides.items():
        if nombre not in efectivo:
            continue
        meta = get_meta(nombre)
        if meta is not None and not meta.reprogramable:
            log.error("cron_override_ignorado", task=nombre, motivo="no_reprogramable")
            continue
        try:
            efectivo[nombre]["schedule"] = parse_expression(expresion)
        except ValueError:
            log.error("cron_override_ignorado", task=nombre, motivo="expresion_invalida")
    return efectivo


class OverridesMixin:
    """Lógica de overrides, separada de `PersistentScheduler` para poder probarla sin archivo."""

    refresh_interval_s: float = 30.0
    _ultimo_refresco: float | None = None

    # Los atributos de Celery que usa; declarados para el type checker.
    app: Any
    _heap: Any

    def _leer_overrides(self) -> dict[str, str]:
        from core.admin.crons_service import cargar_config

        async def _leer() -> dict[str, str]:
            config = await asyncio.wait_for(cargar_config(), timeout=LECTURA_TIMEOUT_S)
            return {n: c.schedule_override for n, c in config.items() if c.schedule_override}

        return run_async(_leer())

    def refrescar_overrides(self, force: bool = False) -> bool:
        """Aplica los horarios reprogramados. Devuelve True si cambió algún horario."""
        ahora = time.monotonic()
        if not force and self._ultimo_refresco is not None:
            if ahora - self._ultimo_refresco < self.refresh_interval_s:
                return False
        self._ultimo_refresco = ahora
        try:
            overrides = self._leer_overrides()
        except Exception as exc:
            log.error("cron_scheduler_lectura_fallida", error=type(exc).__name__)
            return False

        schedule = self.schedule  # type: ignore[attr-defined]
        antes = {nombre: entrada.schedule for nombre, entrada in schedule.items()}
        self.merge_inplace(horario_efectivo(self.app.conf.beat_schedule, overrides))  # type: ignore[attr-defined]
        cambio = any(
            nombre not in antes or antes[nombre] != entrada.schedule for nombre, entrada in schedule.items()
        ) or set(antes) != set(schedule)
        if cambio:
            # `merge_inplace` muta las entradas en sitio: `tick()` no lo notaría (ver docstring).
            self._heap = None
            log.info("cron_scheduler_horarios_aplicados", reprogramados=sorted(overrides))
        return cambio


class DbOverrideScheduler(OverridesMixin, PersistentScheduler):  # type: ignore[misc]
    def setup_schedule(self) -> None:
        super().setup_schedule()
        self.refrescar_overrides(force=True)

    def tick(self, *args: Any, **kwargs: Any) -> float:
        self.refrescar_overrides()
        return super().tick(*args, **kwargs)
