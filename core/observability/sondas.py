"""Sondas de la plataforma como gauges (ADR-0188), solo en el proceso de la API.

Una tarea asyncio mide cada `INTERVALO_S` y deja los valores en `_valores`; los
`ObservableGauge` solo leen ese dict cuando el lector exporta (cada 60 s). Así el callback del SDK,
que corre en el hilo del lector, nunca toca la base ni Redis, y Grafana no se conecta a Postgres.

| Gauge | Etiquetas | Fuente |
|---|---|---|
| `agentcore_dlq_length` | — | DLQ del inbound sin las entradas sembradas por los escenarios de prueba (`dlq_pendientes`) |
| `agentcore_inbound_pending` | — | entregados sin ack + sin entregar (`lag`) del grupo del orquestador en `agent_core:inbound` |
| `agentcore_worker_heartbeat_age_seconds` | — | edad de `worker:heartbeat`; ausente = `9999` |
| `agentcore_dependency_up` | `component` (db, redis) | `SELECT 1` y `PING` (las sondas de `/healthz/deep`) |
| `agentcore_cron_failures_24h` | `job` (nombre de la tarea) | ejecuciones fallidas de las últimas 24 h, del servicio de crons (`crons_service.fallos_24h`) |

Solo con `AGENT_CORE_ROLE=api` y `OTEL_METRICS_ENABLED=true`: un solo proceso mide (worker, beat y
conector no repiten las series) y, apagadas las métricas, no hay consultas extra. Si una medición
falla, ese valor queda sin dato (el gauge no emite) salvo `dependency_up`, que pasa a 0.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from typing import Any

from opentelemetry import metrics
from opentelemetry.metrics import CallbackOptions, Observation

from core.config import get_settings
from core.observability import salud
from core.observability.metricas_negocio import _etiqueta
from core.shared.logger import get_logger

log = get_logger(__name__)

INTERVALO_S = 30.0
TOPE_S = 5.0
LATIDO_AUSENTE_S = 9999.0
GRUPO_INBOUND = "orchestrator-group"

_valores: dict[str, Any] = {"dlq": None, "inbound": None, "latido": None, "deps": {}, "crons": {}}


# ---------------------------------------------------------------------------- mediciones
async def _dlq() -> int:
    from core.orchestrator.consumer import dlq_pendientes

    return await dlq_pendientes(salud._redis())


async def _inbound_pendiente() -> int:
    from core.gateway.webhooks import INBOUND_STREAM

    for grupo in await salud._redis().xinfo_groups(INBOUND_STREAM):
        if grupo.get("name") == GRUPO_INBOUND:
            return int(grupo.get("pending") or 0) + int(grupo.get("lag") or 0)
    return 0


async def _latido() -> float:
    edad = await salud.edad_del_latido()
    return LATIDO_AUSENTE_S if edad is None else edad


async def _crons_fallidos() -> dict[str, int]:
    from core.admin.crons_service import fallos_24h

    return await fallos_24h()


async def _con_tope(nombre: str, medicion: Any) -> Any:
    try:
        return await asyncio.wait_for(medicion(), timeout=TOPE_S)
    except Exception as exc:  # el detalle no sale de aquí: solo el tipo
        log.warning("sonda_fallo", sonda=nombre, error_type=type(exc).__name__)
        return None


async def medir() -> None:
    dlq, inbound, latido, crons, db, redis = await asyncio.gather(
        _con_tope("dlq", _dlq),
        _con_tope("inbound", _inbound_pendiente),
        _con_tope("latido", _latido),
        _con_tope("crons", _crons_fallidos),
        salud._medir("db", salud.sonda_db),
        salud._medir("redis", salud.sonda_redis),
    )
    _valores["dlq"] = dlq
    _valores["inbound"] = inbound
    _valores["latido"] = latido
    if crons is not None:
        # Un cron que ya no corrió en 24 h queda en 0 (no desaparece) para que la alerta se resuelva.
        _valores["crons"] = {**dict.fromkeys(_valores["crons"], 0), **crons}
    _valores["deps"] = {"db": 1 if db == salud.OK else 0, "redis": 1 if redis == salud.OK else 0}


# ------------------------------------------------------------------------------- gauges
def _uno(clave: str) -> Iterable[Observation]:
    valor = _valores.get(clave)
    return [] if valor is None else [Observation(valor)]


def _observar_dlq(_opciones: CallbackOptions) -> Iterable[Observation]:
    return _uno("dlq")


def _observar_inbound(_opciones: CallbackOptions) -> Iterable[Observation]:
    return _uno("inbound")


def _observar_latido(_opciones: CallbackOptions) -> Iterable[Observation]:
    return _uno("latido")


def _observar_deps(_opciones: CallbackOptions) -> Iterable[Observation]:
    return [Observation(v, {"component": k}) for k, v in dict(_valores["deps"]).items()]


def _observar_crons(_opciones: CallbackOptions) -> Iterable[Observation]:
    return [Observation(v, {"job": _etiqueta(k)}) for k, v in dict(_valores["crons"]).items()]


_meter = metrics.get_meter("agent-core")
_meter.create_observable_gauge("agentcore_dlq_length", [_observar_dlq], description="Entradas en la DLQ del inbound")
_meter.create_observable_gauge(
    "agentcore_inbound_pending", [_observar_inbound], description="Mensajes entrantes sin procesar (pendientes + sin entregar)"
)
_meter.create_observable_gauge(
    "agentcore_worker_heartbeat_age_seconds",
    [_observar_latido],
    unit="s",
    description="Segundos desde el último latido del worker (9999 = sin latido)",
)
_meter.create_observable_gauge(
    "agentcore_dependency_up", [_observar_deps], description="1 si la dependencia responde, 0 si no"
)
_meter.create_observable_gauge(
    "agentcore_cron_failures_24h", [_observar_crons], description="Ejecuciones fallidas de cada cron en 24 h"
)


# --------------------------------------------------------------------------------- bucle
async def bucle() -> None:
    while True:
        try:
            await medir()
        except Exception as exc:  # una vuelta rota no apaga las sondas
            log.warning("sondas_vuelta_fallo", error_type=type(exc).__name__)
        await asyncio.sleep(INTERVALO_S)


def arrancar() -> asyncio.Task[None] | None:
    """Lanza el bucle si este proceso es la API y las métricas están encendidas; si no, `None`."""
    settings = get_settings()
    if settings.agent_core_role != "api" or not settings.otel_metrics_enabled:
        return None
    log.info("sondas_arrancadas", intervalo_s=INTERVALO_S)
    return asyncio.create_task(bucle())
