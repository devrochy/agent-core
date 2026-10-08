"""El beat agenda los crons que registran los packs al arrancar.

Celery crea el planificador (copiando `beat_schedule`) ANTES de `beat_init`, que es
donde se cargan los packs: el handler tiene que volver a pasarle el `beat_schedule`.
"""

from __future__ import annotations

from types import SimpleNamespace

from celery.beat import Scheduler
from celery.schedules import crontab

import core.tasks.celery_app as celery_app


def test_beat_init_agenda_los_crons_de_los_packs(monkeypatch) -> None:
    app = celery_app.celery
    # Igual que en `Service.start()`: el planificador nace antes de `beat_init`.
    scheduler = Scheduler(app=app, lazy=False)
    tarea = "pack_prueba.cron_de_prueba"
    assert tarea not in scheduler.schedule

    def _cargar_packs() -> None:  # lo que hace `@register_cron` al escanear los packs
        app.conf.beat_schedule[tarea] = {"task": tarea, "schedule": crontab(minute="*/10")}

    monkeypatch.setattr(celery_app, "_load_plugins", _cargar_packs)
    try:
        celery_app._load_plugins_on_beat_start(sender=SimpleNamespace(scheduler=scheduler))
        assert tarea in scheduler.schedule
        # Los crons del core siguen agendados.
        assert celery_app._DOMAIN_EVENTS_DISPATCH_TASK in scheduler.schedule
    finally:
        app.conf.beat_schedule.pop(tarea, None)
