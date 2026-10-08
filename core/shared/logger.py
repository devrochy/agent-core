"""Logging estructurado JSON (structlog).

Todos los logs llevan `conversation_id` cuando existe, para correlación
(mismo patrón que agent-sale: tracing por conversación, no tracing distribuido).
"""

from __future__ import annotations

import logging
import sys

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


def configure_logging() -> None:
    settings = get_settings()
    level = getattr(logging, settings.log_level.upper(), logging.INFO)

    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=level)
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


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)
