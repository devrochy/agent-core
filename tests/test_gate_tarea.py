"""Tarea del worker del gate (HU-AOS-21 parte 4): límites por encima del tiempo de medición, sin
`acks_late` y espera (sin escribir nada) cuando otra medición tiene el candado. Puro."""

from __future__ import annotations

import pytest
from celery.exceptions import Retry


def test_limites_y_entrega_de_la_tarea() -> None:
    from core.config import get_settings
    from core.tasks.celery_app import celery
    from core.tasks.gate_tasks import NOMBRE, evaluar_candidata

    assert NOMBRE in celery.tasks
    timeout = get_settings().eval_timeout_segundos
    assert evaluar_candidata.time_limit > evaluar_candidata.soft_time_limit > timeout
    assert evaluar_candidata.acks_late is False
    assert "core.tasks.gate_tasks" in celery.conf.include


def test_con_el_candado_ocupado_se_reprograma(monkeypatch: pytest.MonkeyPatch) -> None:
    import core.tasks.gate_tasks as tarea

    async def _ocupado(*_a: object, **_k: object) -> str:
        return "ocupado"

    monkeypatch.setattr(tarea, "evaluar", _ocupado)
    with pytest.raises(Retry):
        tarea.evaluar_candidata.apply(args=("t", "c", "r", "test"), throw=True)
