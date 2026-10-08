"""Las URLs de los clientes HTTP no llegan al log (pueden traer secretos).

Visto en el worker de test (2026-10-08): `httpx` registra cada petición a nivel INFO con
la URL completa, y el intercambio `client_credentials` de Graph manda el App Secret en
la query (`/oauth/access_token?client_id=…&client_secret=…`). El worker de Celery no
pasaba por `configure_logging`, así que la línea salía con el secreto en claro.
"""

from __future__ import annotations

import logging

import pytest

LOGGERS = ("httpx", "httpcore")


@pytest.fixture(autouse=True)
def _restaura_niveles():
    previos = {n: logging.getLogger(n).level for n in (*LOGGERS, "")}
    yield
    for n, nivel in previos.items():
        logging.getLogger(n).setLevel(nivel)


def _resetea():
    """Como en producción: el raíz en INFO y httpx heredándolo (sin el arreglo, sus
    URLs salen)."""
    logging.getLogger().setLevel(logging.INFO)
    for n in LOGGERS:
        logging.getLogger(n).setLevel(logging.NOTSET)


def test_configure_logging_silencia_las_urls_de_httpx(caplog) -> None:
    from core.shared.logger import configure_logging

    _resetea()
    configure_logging()

    with caplog.at_level(logging.INFO):
        logging.getLogger("httpx").info(
            'HTTP Request: GET https://graph.facebook.com/v26.0/oauth/access_token?client_secret=SECRETO "HTTP/1.1 200 OK"'
        )
    assert "SECRETO" not in caplog.text
    for n in LOGGERS:
        assert logging.getLogger(n).getEffectiveLevel() >= logging.WARNING


def test_el_worker_y_el_beat_de_celery_tambien_las_silencian() -> None:
    from celery.signals import after_setup_logger, after_setup_task_logger

    import core.tasks.celery_app as celery_app_mod

    _resetea()
    after_setup_logger.send(sender=None, logger=logging.getLogger(), loglevel=logging.INFO)
    after_setup_task_logger.send(sender=None, logger=logging.getLogger(), loglevel=logging.INFO)

    assert celery_app_mod is not None
    for n in LOGGERS:
        assert logging.getLogger(n).getEffectiveLevel() >= logging.WARNING


def test_los_errores_de_httpx_siguen_saliendo(caplog) -> None:
    from core.shared.logger import silenciar_urls_de_clientes_http

    _resetea()
    silenciar_urls_de_clientes_http()
    with caplog.at_level(logging.INFO):
        logging.getLogger("httpx").warning("algo falló")
    assert "algo falló" in caplog.text
