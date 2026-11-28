"""HU-FB-04 · aviso al equipo de un reporte nuevo: sin destino no envía y el broker caído no rompe el envío."""

from __future__ import annotations

import asyncio
import uuid

from core.tasks import feedback as tareas


def test_sin_destino_no_envia(monkeypatch) -> None:
    from core.config import get_settings

    monkeypatch.setattr(get_settings(), "feedback_aviso_email", "")
    assert asyncio.run(tareas.enviar_aviso(uuid.uuid4(), uuid.uuid4())) == "sin_destino"


def test_broker_caido_no_lanza(monkeypatch) -> None:
    def _cae(*_a, **_k):
        raise ConnectionError("redis caído")

    monkeypatch.setattr(tareas.aviso_reporte_nuevo, "delay", _cae)
    tareas.encolar_aviso(uuid.uuid4(), uuid.uuid4())  # no lanza


def test_la_tarea_esta_registrada_en_el_worker() -> None:
    from core.tasks.celery_app import celery

    assert "core.tasks.feedback" in celery.conf.include
    assert tareas.TAREA == "core.feedback.aviso_reporte_nuevo"
