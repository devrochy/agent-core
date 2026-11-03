"""Catálogo de crons (ADR-0183): guardia en los dos sentidos contra el beat real.

- Cada entrada del `beat_schedule` (core y packs) tiene su metadata: un cron nuevo sin clasificar
  rompe el CI.
- Cada entrada del catálogo existe en el beat y su horario por defecto es el del código.
"""

from __future__ import annotations

import importlib.util
from datetime import UTC, datetime

import pytest

from core.tasks.cron_catalog import (
    CATALOGO,
    MODULOS,
    CronMeta,
    parse_expression,
    validate_schedule,
)


@pytest.fixture(scope="module")
def beat() -> dict:
    from core.tasks.celery_app import _load_plugins, celery

    _load_plugins()
    # Otros tests registran crons de prueba (`pack_cron_prueba*`) en el beat global: no son reales.
    return {n: e for n, e in celery.conf.beat_schedule.items() if not n.startswith("pack_cron_prueba")}


def _pack_instalado(nombre: str) -> bool:
    prefijo = nombre.split(".")[0]
    return prefijo.startswith("core") or importlib.util.find_spec(prefijo) is not None


def test_todo_cron_del_beat_esta_clasificado(beat):
    sin_meta = sorted(set(beat) - set(CATALOGO))
    assert sin_meta == [], (
        f"Crons sin metadata en core/tasks/cron_catalog.py: {sin_meta}. "
        "Agrégalos al catálogo con su módulo y su horario."
    )


def test_todo_cron_del_catalogo_existe_en_el_beat(beat):
    # Los de un pack no instalado en este entorno no pueden estar en el beat.
    faltan = sorted(n for n in CATALOGO if n not in beat and _pack_instalado(n))
    assert faltan == [], f"Entradas del catálogo que ya no existen en el beat: {faltan}"


def test_el_horario_por_defecto_coincide_con_el_codigo(beat):
    distintos = []
    for nombre, meta in CATALOGO.items():
        if nombre not in beat:
            continue
        real = beat[nombre]["schedule"]
        if meta.interval_s is not None:
            ok = float(real.run_every.total_seconds()) == meta.interval_s if hasattr(real, "run_every") else real == meta.interval_s
        else:
            ok = parse_expression(meta.default) == real
        if not ok:
            distintos.append((nombre, meta.default or meta.interval_s, real))
    assert distintos == [], f"El catálogo y el código tienen horarios distintos: {distintos}"


def test_los_modulos_del_catalogo_estan_declarados():
    desconocidos = sorted({m.module for m in CATALOGO.values()} - set(MODULOS))
    assert desconocidos == []


def test_los_crons_de_intervalo_fijo_no_son_reprogramables():
    with pytest.raises(ValueError):
        CronMeta("x", "Plataforma", "x", "x", None, interval_s=30.0)  # reprogramable por defecto
    with pytest.raises(ValueError):
        CronMeta("x", "Plataforma", "x", "x", None)  # ni default ni intervalo
    with pytest.raises(ValueError):
        CronMeta("x", "Plataforma", "x", "x", "* * * * *", interval_s=30.0, reprogramable=False)


@pytest.mark.parametrize("expr", ["* * * * *", "*/15 * * * *", "30 9,15,20 * * *", "0 4 * * 1-5", "5 3 1 */2 *"])
def test_expresiones_validas(expr):
    assert validate_schedule(expr) == expr


def test_normaliza_espacios():
    assert validate_schedule("  0   4  * *   * ") == "0 4 * * *"


@pytest.mark.parametrize(
    "expr",
    ["", "* * * *", "* * * * * *", "61 * * * *", "* 25 * * *", "a b c d e", "@daily", "*/0 * * * *", "0 4 * * MON"],
)
def test_expresiones_invalidas(expr):
    with pytest.raises(ValueError):
        validate_schedule(expr)


def test_proxima_ejecucion_no_depende_de_la_fecha_real():
    """Con `ahora` en otro día que el real, el resultado es el mismo (Celery comparaba con su reloj)."""
    from core.admin.crons_service import proxima_ejecucion

    for dia in (datetime(2020, 3, 9, 12, 34, 56, tzinfo=UTC), datetime(2041, 12, 31, 12, 34, 56, tzinfo=UTC)):
        esperado = dia.replace(minute=45, second=0)
        assert proxima_ejecucion("*/15 * * * *", dia) == esperado


def test_proxima_ejecucion_es_estrictamente_posterior():
    from core.admin.crons_service import proxima_ejecucion

    ahora = datetime(2026, 11, 2, 12, 34, 56, tzinfo=UTC)
    assert proxima_ejecucion("*/15 * * * *", ahora) == datetime(2026, 11, 2, 12, 45, tzinfo=UTC)
    assert proxima_ejecucion("0 13 * * *", ahora) == datetime(2026, 11, 2, 13, 0, tzinfo=UTC)
    assert proxima_ejecucion("0 13 * * *", datetime(2026, 11, 2, 13, 30, tzinfo=UTC)) == datetime(
        2026, 11, 3, 13, 0, tzinfo=UTC
    )
    assert proxima_ejecucion("0 13 * * *", datetime(2026, 11, 2, 12, 0, tzinfo=UTC)) == datetime(
        2026, 11, 2, 13, 0, tzinfo=UTC
    )
    # Justo en el minuto de disparo: el siguiente, no el mismo.
    assert proxima_ejecucion("*/15 * * * *", datetime(2026, 11, 2, 12, 45, tzinfo=UTC)) == datetime(
        2026, 11, 2, 13, 0, tzinfo=UTC
    )
