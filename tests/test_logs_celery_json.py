"""Los procesos de Celery escriben sus logs en JSON y sin variables locales.

Visto en el worker de test (2026-10-11): el worker y el beat no llamaban a `configure_logging`,
así que structlog quedaba con su default y `cron_task_failed` salía con el traceback de rich y las
variables locales de cada frame (`│ nombre = valor`). Si una local tiene un token o datos de un
cliente, terminan en el log. Los tests corren en un proceso aparte porque structlog y `logging`
son estado global.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import structlog

ROOT = Path(__file__).resolve().parent.parent
SECRETO = "tok-SECRETO-EN-UNA-LOCAL"

_FALLA = f"""
def falla():
    token = {SECRETO!r}
    raise RuntimeError("se cayó la tarea")
"""


def _correr(codigo: str) -> str:
    salida = subprocess.run(
        [sys.executable, "-c", _FALLA + codigo],
        cwd=ROOT,
        env={**os.environ, "LOG_LEVEL": "INFO", "NO_COLOR": "1"},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert salida.returncode == 0, salida.stdout + salida.stderr
    return salida.stdout + salida.stderr


def _lineas_json(salida: str) -> list[dict[str, object]]:
    lineas = [linea for linea in salida.splitlines() if linea.strip()]
    assert lineas, salida
    return [json.loads(linea) for linea in lineas]  # falla si alguna línea no es JSON


def test_worker_y_beat_loguean_los_errores_en_json_sin_locals() -> None:
    # `celery.log.setup` es lo que corren el worker, el beat y cada proceso hijo del worker al
    # arrancar, con el stdout redirigido al logger `celery.redirected` como en producción.
    salida = _correr("""
import logging, sys
from celery.utils.log import LoggingProxy
from core.tasks.celery_app import celery
from core.shared.logger import get_logger

celery.log.setup(loglevel="INFO", redirect_stdouts=True, redirect_level="WARNING")
assert not isinstance(sys.stdout, LoggingProxy), "Celery redirigió stdout"
try:
    falla()
except RuntimeError:
    get_logger("pack_commerce.jobs").exception("cron_task_failed", task="pack_commerce.follow_up_quotes")
try:
    falla()
except RuntimeError:
    logging.getLogger("celery.app.trace").exception("Task raised unexpected")
""")
    assert SECRETO not in salida
    registros = _lineas_json(salida)
    por_evento = {r["event"]: r for r in registros}
    for evento in ("cron_task_failed", "Task raised unexpected"):
        excepcion = str(por_evento[evento]["exception"])
        assert "Traceback" in excepcion and "se cayó la tarea" in excepcion
    assert por_evento["cron_task_failed"]["task"] == "pack_commerce.follow_up_quotes"
    assert por_evento["Task raised unexpected"]["logger"] == "celery.app.trace"


def test_el_worker_configura_los_logs_antes_de_escanear_los_plugins(monkeypatch) -> None:
    # `worker_init` (escaneo de plugins) llega antes que `setup_logging`.
    import core.tasks.celery_app as celery_app_mod

    renderers: list[object] = []
    monkeypatch.setattr(
        celery_app_mod,
        "_load_plugins",
        lambda: renderers.append(structlog.get_config()["processors"][-1]),
    )
    previa = structlog.get_config()
    try:
        structlog.reset_defaults()
        celery_app_mod._load_plugins_on_worker_start()
    finally:
        structlog.configure(**previa)  # sin dejar el default (con locals) al resto de la suite
    assert len(renderers) == 1 and isinstance(renderers[0], structlog.processors.JSONRenderer)


def test_sin_configurar_los_tracebacks_no_muestran_locals() -> None:
    # Red de seguridad: un proceso que importa el core y olvida `configure_logging`.
    salida = _correr("""
from core.shared.logger import get_logger
try:
    falla()
except RuntimeError:
    get_logger("x").exception("algo_fallo")
""")
    assert "se cayó la tarea" in salida
    assert SECRETO not in salida
