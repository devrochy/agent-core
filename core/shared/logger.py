"""Logging estructurado JSON (structlog).

Todos los logs llevan `conversation_id` cuando existe, para correlación
(mismo patrón que agent-sale: tracing por conversación, no tracing distribuido).
"""

from __future__ import annotations

import logging
import sys
from typing import Any

import structlog

from core.config import get_settings

# Clientes HTTP que registran cada petición a nivel INFO con la URL completa. Varias
# llamadas a Graph llevan secretos en la query (`/oauth/access_token?client_secret=…`,
# `fb_exchange_token`), así que sus logs de petición no deben salir: quedan solo sus
# warnings y errores.
_LOGGERS_CON_URLS = ("httpx", "httpcore")


def silenciar_urls_de_clientes_http() -> None:
    for nombre in _LOGGERS_CON_URLS:
        logging.getLogger(nombre).setLevel(logging.WARNING)


def _formateador_json_de_stdlib() -> logging.Formatter:
    """Los registros de `logging` (Celery, httpx, SQLAlchemy…) también en una línea JSON, con el
    traceback como texto plano en `exception` (sin variables locales)."""
    return structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=[
            structlog.stdlib.add_log_level,
            structlog.stdlib.add_logger_name,
            structlog.processors.TimeStamper(fmt="iso"),
        ],
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
    )


def configure_logging() -> None:
    """Logs en JSON para los procesos de servicio: API, worker y beat de Celery, connector.

    Idempotente: en el worker de Celery corre en `worker_init` y otra vez en `setup_logging`.
    """
    settings = get_settings()
    level = getattr(logging, settings.log_level.upper(), logging.INFO)

    # Mismo criterio que `logging.basicConfig`: si el raíz ya tiene handlers (pytest, una
    # llamada anterior) no se agrega otro. El nivel sí se fija siempre.
    raiz = logging.getLogger()
    if not raiz.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(_formateador_json_de_stdlib())
        raiz.addHandler(handler)
    raiz.setLevel(level)
    silenciar_urls_de_clientes_http()

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        cache_logger_on_first_use=True,
    )


def configure_console_logging() -> None:
    """Consola legible para procesos de línea de comandos (runners de evals, scripts).

    Igual que el default de structlog, pero los tracebacks van SIN variables locales. El default
    usa rich con `show_locals=True`, que imprimía el objeto `settings` entero (API key, claves de
    cifrado) y cualquier otro objeto del frame ante una excepción.
    """
    # Sin rich, `RichTracebackFormatter` se construye igual pero falla al formatear.
    formateador: Any = (
        structlog.dev.RichTracebackFormatter(show_locals=False)
        if getattr(structlog.dev, "rich", None) is not None
        else structlog.dev.plain_traceback  # traceback plano, sin locals
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.StackInfoRenderer(),
            structlog.dev.set_exc_info,
            structlog.processors.TimeStamper(fmt="%Y-%m-%d %H:%M:%S", utc=False),
            structlog.dev.ConsoleRenderer(exception_formatter=formateador),
        ],
    )


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)


# Red de seguridad: un proceso que importa el core sin llamar a `configure_logging` (así estaban el
# worker y el beat de Celery hasta 2026-10-11) quedaba con el default de structlog, cuyos
# tracebacks muestran las variables locales de cada frame. Con esto, a lo sumo sale en consola.
if not structlog.is_configured():
    configure_console_logging()
