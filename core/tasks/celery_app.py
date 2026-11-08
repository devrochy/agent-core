"""Aplicación Celery (broker y backend: Redis).

El worker y el beat importan este módulo. Al arrancar, el worker escanea los
plugins para que sus tareas @register_cron queden registradas.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from celery import Celery
from celery.schedules import crontab
from celery.signals import (
    beat_init,
    setup_logging,
    worker_init,
    worker_process_init,
    worker_process_shutdown,
)

from core.config import get_settings
from core.shared.logger import configure_logging, get_logger

log = get_logger(__name__)
settings = get_settings()

celery = Celery(
    "agent_core",
    broker=settings.redis_url,
    backend=settings.redis_url,
    # `include` (no un import suelto): garantiza que el worker registre las
    # tasks definidas en estos módulos al finalizar el arranque. Sin esto,
    # `core.outbound_send` quedaba como "unregistered task" y los mensajes de
    # salida nunca se enviaban.
    include=[
        "core.tasks.outbound_send",
        "core.tasks.push_tasks",
        "core.tasks.production_readiness",
        "core.tasks.gate_tasks",  # HU-AOS-21 parte 4: medición del gate de pass^k
    ],
)
celery.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    task_acks_late=True,          # el mensaje se re-entrega si el worker muere
    task_reject_on_worker_lost=True,
    task_time_limit=300,
    beat_schedule={},             # lo llenan los @register_cron de los plugins
)

# La traza del turno viaja a las tareas en `traceparent` (HU-AOS-10).
from core.observability.celery_trazas import conectar as _conectar_trazas  # noqa: E402

_conectar_trazas()


# ---------------------------------------------------------------- cron de core
def core_cron(task_name: str, schedule: Any) -> Callable[[Callable[[], Any]], Callable[[], Any]]:
    """Registra un cron de CORE (ADR-0183): tarea Celery + entrada del beat.

    Lo decorado es un `job()` sin argumentos que devuelve la corrutina a ejecutar (o hace el
    trabajo síncrono y devuelve `None`). `run_cron` lo envuelve: pausa, lock anti-solape y
    registro de la ejecución en `cron_runs`. La tarea acepta `trigger`/`triggered_by` para la
    ejecución manual (`POST /admin/platform/crons/{name}/run`); el beat no manda argumentos.
    Los crons de packs hacen lo mismo con `register_cron` (core/tasks/decorators.py).
    """

    def decorator(job: Callable[[], Any]) -> Callable[[], Any]:
        @celery.task(name=task_name)
        def _task(trigger: str = "scheduled", triggered_by: str | None = None) -> None:
            from core.tasks.cron_runtime import run_cron

            run_cron(task_name, job, trigger=trigger, triggered_by=triggered_by)

        celery.conf.beat_schedule[task_name] = {"task": task_name, "schedule": schedule}
        return job

    return decorator


# Salud de suscripciones de webhook de Meta (cada 15 min): capacidad de
# CORE, no de un pack -- no pasa por `register_cron` (core/tasks/decorators.py),
# que está atado a `plugin_manager.is_active(plugin_id)` y no tiene sentido
# para algo que no es un plugin. Se registra directo acá.
_META_SUBSCRIPTIONS_HEALTH_TASK = "core.meta_subscriptions_health_check"


@core_cron(_META_SUBSCRIPTIONS_HEALTH_TASK, crontab(minute="*/15"))
def _meta_subscriptions_health_check() -> Any:
    from core.tasks.meta_subscriptions_health import check_all_subscriptions_health

    return check_all_subscriptions_health()


# Refresco del token de 60 días de Instagram Login (diario, de madrugada): Meta solo deja refrescar un token de al
# menos 24 h y aún vigente, así que se corre una vez al día y solo toca los que están por vencer (HU-CHAN-04).
_INSTAGRAM_TOKEN_REFRESH_TASK = "core.instagram_token_refresh"


@core_cron(_INSTAGRAM_TOKEN_REFRESH_TASK, crontab(hour="3", minute="30"))
def _instagram_token_refresh() -> Any:
    from core.tasks.instagram_token_refresh import refresh_instagram_tokens

    return refresh_instagram_tokens()


# Sync de plantillas de WhatsApp (cada hora): el webhook `message_template_
# status_update` es la fuente de verdad real, esto es solo el fallback.
_MESSAGE_TEMPLATE_SYNC_TASK = "core.message_template_sync"


@core_cron(_MESSAGE_TEMPLATE_SYNC_TASK, crontab(minute="0"))
def _message_template_sync() -> Any:
    from core.tasks.message_template_sync import sync_all_waba_templates

    return sync_all_waba_templates()


# Sync de `pricing_analytics` (diario, de madrugada): el webhook `statuses`
# (con su objeto `pricing`) llega en tiempo real pero NUNCA trae un monto
# en dólares -- esto es lo único que sincroniza el `cost` autoritativo.
_META_PRICING_SYNC_TASK = "core.meta_pricing_sync"


@core_cron(_META_PRICING_SYNC_TASK, crontab(hour="2", minute="0"))
def _meta_pricing_sync() -> Any:
    from core.tasks.meta_pricing_sync import sync_all_waba_pricing

    return sync_all_waba_pricing()


# Salud completa de conexiones Meta (horaria) -- `app_subscription_active`
# (15 min) sigue siendo `core.meta_subscriptions_health_check`, este cron
# solo LEE ese resultado, nunca repite el GET.
_META_HEALTH_CHECK_TASK = "core.meta_health_check"


@core_cron(_META_HEALTH_CHECK_TASK, crontab(minute="30"))
def _meta_health_check() -> Any:
    from core.tasks.meta_health_check import check_all_connections_health

    return check_all_connections_health()


# Alerta de access tokens de Meta vencidos/por vencer (diaria, de madrugada).
_META_TOKEN_EXPIRY_TASK = "core.meta_token_expiry_check"


@core_cron(_META_TOKEN_EXPIRY_TASK, crontab(hour="3", minute="15"))
def _meta_token_expiry_check() -> Any:
    from core.tasks.meta_token_expiry_check import check_tokens_expiry

    return check_tokens_expiry()


# Retención de la memoria semántica (ADR-0053; no-op si MEMORY_RETENTION_DAYS=0).
_MEMORY_RETENTION_TASK = "core.memory_retention"


@core_cron(_MEMORY_RETENTION_TASK, crontab(hour="3", minute="45"))
def _memory_retention() -> Any:
    from core.tasks.memory_retention import purge_expired_memory

    return purge_expired_memory()


# Retención del centro de notificaciones (ADR-0167): baja lógica a los 180 días.
_NOTIFICATIONS_RETENTION_TASK = "core.notifications_retention"


@core_cron(_NOTIFICATIONS_RETENTION_TASK, crontab(hour="4", minute="0"))
def _notifications_retention() -> Any:
    from core.tasks.notifications_retention import retener_notificaciones

    return retener_notificaciones()


# Retención de los eventos de acceso (ADR-0174, HU-ACC-06): baja lógica a los 180 días.
_ACCESS_EVENTS_RETENTION_TASK = "core.access_events_retention"


@core_cron(_ACCESS_EVENTS_RETENTION_TASK, crontab(hour="4", minute="10"))
def _access_events_retention() -> Any:
    from core.tasks.access_events_retention import retener_eventos_de_acceso

    return retener_eventos_de_acceso()


# «Cliente esperando respuesta» (ADR-0167): cada 5 min avisa de los clientes con una escalación
# abierta que llevan más de 15 min sin respuesta humana (un aviso por episodio).
_CLIENTE_ESPERANDO_TASK = "core.cliente_esperando"


@core_cron(_CLIENTE_ESPERANDO_TASK, crontab(minute="*/5"))
def _cliente_esperando() -> Any:
    from core.tasks.cliente_esperando import avisar_clientes_esperando

    return avisar_clientes_esperando()


# Canary (HU-AOS-32, adenda de ADR-0117 §5): cada 5 min la señal dura y la habilidad retirada; cada
# hora, además, la no-inferioridad (dos entradas: no hace falta estado para saber «una vez por hora»).
_CANARY_SENAL_DURA_TASK = "core.canary_senal_dura"
_CANARY_NO_INFERIORIDAD_TASK = "core.canary_no_inferioridad"


@core_cron(_CANARY_SENAL_DURA_TASK, crontab(minute="*/5"))
def _canary_senal_dura() -> Any:
    from core.releases.canary_job import revisar

    return revisar(no_inferioridad=False)


@core_cron(_CANARY_NO_INFERIORIDAD_TASK, crontab(minute="0"))
def _canary_no_inferioridad() -> Any:
    from core.releases.canary_job import revisar

    return revisar(no_inferioridad=True)


# Consolidación nocturna de la memoria (ADR-0114, HU-AOS-36): datos de tareas cerradas → hechos.
_MEMORY_CONSOLIDATION_TASK = "core.memory_consolidation"


@core_cron(_MEMORY_CONSOLIDATION_TASK, crontab(hour="3", minute="30"))
def _memory_consolidation() -> Any:
    from core.tasks.memory_consolidation import consolidar_memoria

    return consolidar_memoria()


# Esperas vencidas del motor de procedimientos (HU-AOS-07): una tarea que espera el pago y no lo
# recibe en 72 h se cancela con `espera_vencida`. Sin negocios con `aos_procedures`, no hace nada.
_KERNEL_ESPERAS_TASK = "core.kernel_esperas_vencidas"


@core_cron(_KERNEL_ESPERAS_TASK, crontab(minute="25"))
def _kernel_esperas_vencidas() -> Any:
    from core.kernel.procedures.esperas import vencer_esperas
    from core.plugin_manager import plugin_manager

    async def _correr() -> None:
        # Los procedimientos los registran los packs al activarse (como el despachador).
        await plugin_manager.ensure_process_activation()
        await vencer_esperas()

    return _correr()


# Antiabuso del alta (HU-SH-07, ADR-0125): las huellas de teléfono y documento de los
# pedidos de alta se vacían a los 12 meses (ya no sirven para ninguna regla).
_ALTA_HUELLAS_RETENCION_TASK = "core.alta_huellas_retencion"


@core_cron(_ALTA_HUELLAS_RETENCION_TASK, crontab(hour="4", minute="10"))
def _alta_huellas_retencion() -> Any:
    from core.provisioning.antiabuso import olvidar_huellas_vencidas

    return olvidar_huellas_vencidas()


# Heartbeat del worker (cada 1 min) -- la única forma de que el checklist
# de la plataforma (core/gateway/meta_production_readiness.py::
# check_worker_heartbeat_and_dlq) sepa que el worker de Celery está vivo,
# no solo que existe la cola.
_WORKER_HEARTBEAT_TASK = "core.worker_heartbeat"
_WORKER_HEARTBEAT_KEY = "worker:heartbeat"
_WORKER_HEARTBEAT_TTL_S = 180


@core_cron(_WORKER_HEARTBEAT_TASK, crontab(minute="*/1"))
def _worker_heartbeat() -> Any:
    async def _set_heartbeat() -> None:
        from datetime import UTC, datetime

        from core.memory.working import get_redis

        redis = get_redis()
        await redis.set(_WORKER_HEARTBEAT_KEY, datetime.now(UTC).isoformat(), ex=_WORKER_HEARTBEAT_TTL_S)

    return _set_heartbeat()


# Normalización de suscripciones vencidas (diaria): pasa `trialing`->`expired` y
# `active`->`past_due` para que el panel refleje el estado real (ADR-0045). El
# bloqueo efectivo NO depende de este cron (se evalúa en vivo en webhook/consumer).
_BILLING_LIFECYCLE_TASK = "core.billing_lifecycle"


@core_cron(_BILLING_LIFECYCLE_TASK, crontab(hour="4", minute="0"))
def _billing_lifecycle() -> Any:
    from core.tasks.billing_lifecycle import normalize_expired_subscriptions

    return normalize_expired_subscriptions()


# Renovación del plan de la plataforma (HU-SH-04, ADR-0123): el cobro del período siguiente
# BILLING_DIAS_RENOVACION días antes, con aviso por el canal preferido. A las 13:00 UTC
# (08:00 en Bogotá), para que el aviso llegue de mañana.
_BILLING_RENOVACION_TASK = "core.billing_renovacion"


@core_cron(_BILLING_RENOVACION_TASK, crontab(hour="13", minute="0"))
def _billing_renovacion() -> Any:
    from core.billing.renovacion import renovar_suscripciones

    return renovar_suscripciones()


# Conciliación diaria de los dos canales del cobro de la plataforma (HU-SH-04): compara el
# asistente con el negocio y alerta cada diferencia con su detalle.
_BILLING_CONCILIACION_TASK = "core.billing_conciliacion"


@core_cron(_BILLING_CONCILIACION_TASK, crontab(hour="5", minute="30"))
def _billing_conciliacion() -> Any:
    from core.billing.conciliacion import conciliar

    return conciliar()


# Despachador del outbox de dominio (HU-SUB-03, ADR-0107): cada 30 s entrega
# los `domain_events` pendientes vencidos con backoff y DLQ.
_DOMAIN_EVENTS_DISPATCH_TASK = "core.domain_events_dispatch"


@core_cron(_DOMAIN_EVENTS_DISPATCH_TASK, 30.0)
def _domain_events_dispatch() -> Any:
    from core.tasks.domain_events_dispatch import dispatch_domain_events_task

    # Síncrono (usa su propio `run_async`): `run_cron` lo llama sin envolverlo.
    dispatch_domain_events_task()

# Mantenimiento del historial de crons (ADR-0183): borra a los 30 días y cierra las ejecuciones
# que quedaron colgadas (worker caído a mitad de una tarea).
_CRON_RUNS_RETENTION_TASK = "core.cron_runs_retention"


@core_cron(_CRON_RUNS_RETENTION_TASK, crontab(hour="4", minute="20"))
def _cron_runs_retention() -> Any:
    from core.admin.crons_service import mantenimiento

    return mantenimiento()



def _load_plugins() -> None:
    """Carga los plugins (entry_points) e importa los jobs para que corran los
    `@register_cron` de los packs. Necesario tanto en el worker como en el beat."""
    from core.observability.sentry_setup import configure_sentry
    from core.plugin_manager import plugin_manager

    configure_sentry()  # no-op sin SENTRY_ENABLED/SENTRY_DSN -- el worker también reporta
    plugin_manager.scan()
    # Importa los módulos de jobs para que los decoradores corran
    import core.tasks.decorators  # noqa: F401


@setup_logging.connect
def _configurar_logs(**_kwargs: object) -> None:
    """El worker y el beat usan `configure_logging` (JSON); los procesos hijos del worker
    (prefork) heredan la configuración por fork.

    Con un receptor de esta señal Celery no toca el logger raíz ni redirige stdout al logger
    `celery.redirected`. Antes, el worker y el beat quedaban con el default de structlog: los
    tracebacks salían con rich y las variables locales de cada frame (visto en el worker de test,
    2026-10-11), y `httpx` registraba la URL de `/oauth/access_token` con el App Secret (2026-10-08).
    """
    configure_logging()


@worker_init.connect
def _load_plugins_on_worker_start(**_kwargs: object) -> None:
    """El worker necesita los plugins cargados para resolver sus tareas.

    `worker_init` llega ANTES que `setup_logging`: sin configurar acá, los logs del escaneo de
    plugins salían con el default de structlog."""
    configure_logging()
    _load_plugins()


@beat_init.connect
def _load_plugins_on_beat_start(sender: object = None, **_kwargs: object) -> None:
    """El beat NO ejecuta `worker_init`: sin este escaneo, los `@register_cron`
    de los packs nunca entran al `beat_schedule` y sus crons (p. ej. los
    follow-ups de pack-commerce) no se programan.

    Celery crea el planificador (y le copia `beat_schedule`) ANTES de emitir
    `beat_init`, así que lo que registran los packs aquí hay que volver a
    pasárselo al planificador ya creado. Sin eso, ningún cron de pack se
    despachaba (visto al verificar HU-SUB-09 en test: el beat arrancaba a las
    16:09:23 y los crons se registraban 16:09:24, sin un solo envío después).
    """
    _load_plugins()
    scheduler = getattr(sender, "scheduler", None)
    if scheduler is not None:
        scheduler.merge_inplace(celery.conf.beat_schedule)
        # Esa fusión vuelve los horarios al código: reaplicar los reprogramados desde la base
        # (ADR-0183; solo existe con `-S core.tasks.cron_scheduler:DbOverrideScheduler`).
        refrescar = getattr(scheduler, "refrescar_overrides", None)
        if refrescar is not None:
            refrescar(force=True)
        log.info("beat_schedule_cargado", tareas=sorted(scheduler.schedule))
    # El beat es un solo proceso: el proveedor de métricas va aquí (ADR-0188); al salir lo
    # vacía el `atexit` del SDK.
    from core.observability.metricas import configure_metrics

    configure_metrics()


@worker_process_init.connect
def _metricas_en_el_hijo(**_kwargs: object) -> None:
    """Métricas en cada hijo del prefork (ADR-0188), no en el padre: el hilo del lector
    periódico no sobrevive al fork. Con `--pool=solo` Celery emite la señal en el propio
    proceso, así que también aplica."""
    from core.observability.metricas import configure_metrics

    configure_metrics()


@worker_process_shutdown.connect
def _metricas_al_salir_el_hijo(**_kwargs: object) -> None:
    """El hijo sale sin pasar por `atexit`: se exporta lo pendiente aquí."""
    from core.observability.metricas import shutdown_metrics

    shutdown_metrics()
