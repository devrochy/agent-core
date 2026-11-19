"""Entrypoint del proceso `connector` (rol `AGENT_CORE_ROLE=connector`, ADR-0074).

Mantiene los sockets de WhatsApp Web de los tenants con sesión `waweb` activa.
Está **apagado por defecto** (`WAWEB_ENABLED=false`): es un canal no oficial con
riesgo de suspensión y no debe correr salvo habilitación explícita.

Uso:
    AGENT_CORE_ROLE=connector python -m core.waweb.connector
    docker compose --profile waweb up -d waweb-connector
"""

from __future__ import annotations

import asyncio
import signal
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from core.config import get_settings
from core.shared.logger import configure_logging, get_logger
from core.waweb.manager import WaWebConnector
from core.waweb.registry import DbSessionRegistry
from core.waweb.store import DbEncryptedSessionStore

log = get_logger(__name__)

# Hilos del executor por defecto (`asyncio.to_thread`, que usa Neonize para cada llamada al puente
# Go). Por defecto son `min(32, CPU + 4)` (12 en el servidor de test). neonize 0.5.2 deja un hilo
# bloqueado en Go por cada ciclo de QR de una sesión sin vincular y no hay forma de liberarlo:
# esto solo espacia los reinicios. La red de seguridad es `WaWebConnector._check_executor`.
EXECUTOR_WORKERS = 64


async def run_connector() -> None:
    settings = get_settings()
    if not settings.waweb_enabled:
        log.warning("waweb_connector_disabled", note="WAWEB_ENABLED=false: el connector no arranca")
        return

    from core.waweb.backend_neonize import NeonizeSocket

    asyncio.get_running_loop().set_default_executor(
        ThreadPoolExecutor(max_workers=EXECUTOR_WORKERS, thread_name_prefix="waweb_go")
    )

    store = DbEncryptedSessionStore(Path(settings.waweb_session_dir))
    connector = WaWebConnector(
        registry=DbSessionRegistry(),
        store=store,
        socket_factory=NeonizeSocket,
        sweep_interval_s=settings.waweb_sweep_interval_s,
        reconnect_max_s=settings.waweb_reconnect_max_s,
    )
    log.info("waweb_connector_starting", session_dir=settings.waweb_session_dir)
    await connector.run()


def _configurar_sentry() -> None:
    """Sentry como en la API y el worker (no-op sin SENTRY_ENABLED/SENTRY_DSN), con `role=connector`
    para separar sus errores en el proyecto."""
    from core.observability.sentry_setup import configure_sentry

    configure_sentry()
    settings = get_settings()
    if settings.sentry_enabled and settings.sentry_dsn:
        import sentry_sdk

        sentry_sdk.set_tag("role", "connector")


def main() -> None:
    configure_logging()
    _configurar_sentry()

    from core.observability.metricas import configure_metrics, shutdown_metrics

    configure_metrics()  # no-op sin OTEL_METRICS_ENABLED (ADR-0188)

    def _stop(*_: object) -> None:
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, _stop)
    try:
        asyncio.run(run_connector())
    except KeyboardInterrupt:
        log.info("waweb_connector_stopped")
    finally:
        shutdown_metrics()


if __name__ == "__main__":
    main()
