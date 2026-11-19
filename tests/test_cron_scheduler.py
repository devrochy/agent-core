"""Planificador con horarios reprogramables (ADR-0183).

Cubre las tres trampas de Celery documentadas en `core/tasks/cron_scheduler.py`:
`merge_inplace` borra lo que no recibe, muta las entradas en sitio (el montículo no se entera) y
`beat_init` deshace las reprogramaciones.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from celery import Celery
from celery.beat import Scheduler
from celery.schedules import crontab

from core.tasks import cron_scheduler
from core.tasks.cron_catalog import parse_expression
from core.tasks.cron_scheduler import OverridesMixin, horario_efectivo

BASE = {
    "core.billing_renovacion": {"task": "core.billing_renovacion", "schedule": crontab(hour="13", minute="0")},
    "core.cliente_esperando": {"task": "core.cliente_esperando", "schedule": crontab(minute="*/5")},
    "core.domain_events_dispatch": {"task": "core.domain_events_dispatch", "schedule": 30.0},
    "core.memory_retention": {"task": "core.memory_retention", "schedule": crontab(hour="3", minute="45")},
}


class SchedulerDePrueba(OverridesMixin, Scheduler):
    """Scheduler real de Celery (en memoria, sin archivo) con la lógica de overrides."""

    overrides: dict[str, str]
    lecturas = 0
    falla = False

    def _leer_overrides(self) -> dict[str, str]:
        self.lecturas += 1
        if self.falla:
            raise ConnectionError("base caída")
        return dict(self.overrides)


@pytest.fixture
def scheduler() -> SchedulerDePrueba:
    app = Celery("prueba")
    app.conf.beat_schedule = {n: dict(e) for n, e in BASE.items()}
    s = SchedulerDePrueba(app=app, lazy=False)
    s.overrides = {}
    return s


def _cron(s: Scheduler, nombre: str):
    return s.schedule[nombre].schedule


# ------------------------------------------------------------------ horario_efectivo
def test_horario_efectivo_devuelve_copia_completa_y_no_toca_el_original():
    efectivo = horario_efectivo(BASE, {"core.cliente_esperando": "*/10 * * * *"})
    assert set(efectivo) == set(BASE)  # trampa 1: nunca solo los cambiados
    assert efectivo["core.cliente_esperando"]["schedule"] == parse_expression("*/10 * * * *")
    assert BASE["core.cliente_esperando"]["schedule"] == crontab(minute="*/5")  # original intacto
    assert efectivo["core.billing_renovacion"]["schedule"] == BASE["core.billing_renovacion"]["schedule"]


def test_horario_efectivo_ignora_lo_que_no_se_puede_aplicar():
    efectivo = horario_efectivo(
        BASE,
        {
            "core.domain_events_dispatch": "* * * * *",  # intervalo fijo: no reprogramable
            "core.memory_retention": "esto no es un cron",  # inválido
            "core.no_existe": "* * * * *",  # no está en el beat
        },
    )
    assert efectivo["core.domain_events_dispatch"]["schedule"] == 30.0
    assert efectivo["core.memory_retention"]["schedule"] == BASE["core.memory_retention"]["schedule"]
    assert "core.no_existe" not in efectivo


# ------------------------------------------------------------------ refrescar_overrides
def test_aplica_el_override_sin_perder_los_demas_crons(scheduler):
    scheduler.overrides = {"core.cliente_esperando": "*/10 * * * *"}
    assert scheduler.refrescar_overrides(force=True) is True
    assert set(scheduler.schedule) == set(BASE)  # los otros crons siguen
    assert _cron(scheduler, "core.cliente_esperando") == parse_expression("*/10 * * * *")
    assert _cron(scheduler, "core.billing_renovacion") == crontab(hour="13", minute="0")


def test_el_monticulo_se_repuebla_al_cambiar_un_horario(scheduler):
    """Trampa 2: `merge_inplace` muta en sitio y `schedules_equal` no lo ve; sin forzar el
    montículo, `tick()` seguiría con los tiempos viejos."""
    scheduler.tick()  # puebla el montículo con los horarios por defecto
    assert scheduler._heap is not None
    scheduler.overrides = {"core.cliente_esperando": "*/10 * * * *"}
    scheduler.refrescar_overrides(force=True)
    assert scheduler._heap is None  # forzado a repoblar
    scheduler.tick()
    assert scheduler._heap is not None
    en_monticulo = {evento[2].name: evento[2].schedule for evento in scheduler._heap}
    assert en_monticulo["core.cliente_esperando"] == parse_expression("*/10 * * * *")


def test_sin_cambios_no_se_toca_el_monticulo(scheduler):
    scheduler.tick()
    monticulo = scheduler._heap
    assert scheduler.refrescar_overrides(force=True) is False
    assert scheduler._heap is monticulo


def test_quitar_el_override_vuelve_al_horario_por_defecto(scheduler):
    scheduler.overrides = {"core.cliente_esperando": "*/10 * * * *"}
    scheduler.refrescar_overrides(force=True)
    scheduler.overrides = {}
    assert scheduler.refrescar_overrides(force=True) is True
    assert _cron(scheduler, "core.cliente_esperando") == crontab(minute="*/5")


def test_si_la_base_falla_se_conserva_lo_que_hay(scheduler):
    scheduler.overrides = {"core.cliente_esperando": "*/10 * * * *"}
    scheduler.refrescar_overrides(force=True)
    scheduler.falla = True
    assert scheduler.refrescar_overrides(force=True) is False
    assert _cron(scheduler, "core.cliente_esperando") == parse_expression("*/10 * * * *")
    assert set(scheduler.schedule) == set(BASE)


def test_respeta_el_intervalo_de_refresco(scheduler, monkeypatch):
    reloj = {"t": 1000.0}
    monkeypatch.setattr(cron_scheduler.time, "monotonic", lambda: reloj["t"])
    scheduler.refrescar_overrides()  # primera lectura
    scheduler.refrescar_overrides()  # mismo instante: no relee
    assert scheduler.lecturas == 1
    reloj["t"] += scheduler.refresh_interval_s + 1
    scheduler.refrescar_overrides()
    assert scheduler.lecturas == 2
    scheduler.refrescar_overrides(force=True)  # forzado ignora el intervalo
    assert scheduler.lecturas == 3


def test_beat_init_vuelve_a_aplicar_los_overrides(scheduler, monkeypatch):
    """Trampa 3: el hook de `beat_init` hace `merge_inplace(beat_schedule)` con los crons de los
    packs, lo que devuelve todo al código; después debe reaplicar lo reprogramado."""
    from core.tasks import celery_app

    scheduler.overrides = {"core.cliente_esperando": "*/10 * * * *"}
    scheduler.refrescar_overrides(force=True)
    monkeypatch.setattr(celery_app, "_load_plugins", lambda: None)
    # El hook fusiona `celery.conf.beat_schedule` del módulo. Se sustituye el objeto `celery` entero:
    # parchear un atributo de `Settings` hace que el teardown de monkeypatch falle con `delattr`
    # bajo xdist (visto en CI).
    monkeypatch.setattr(
        celery_app, "celery", SimpleNamespace(conf=SimpleNamespace(beat_schedule={n: dict(e) for n, e in BASE.items()}))
    )
    celery_app._load_plugins_on_beat_start(sender=SimpleNamespace(scheduler=scheduler))
    assert _cron(scheduler, "core.cliente_esperando") == parse_expression("*/10 * * * *")
