"""HU-AOS-32 parte 2 · el job del canary está en el beat: señal dura cada 5 min y no-inferioridad
cada hora (dos entradas, sin estado para saber «una vez por hora»)."""

from __future__ import annotations


def test_las_dos_entradas_del_canary_estan_en_el_beat() -> None:
    from core.tasks import celery_app as app

    agenda = app.celery.conf.beat_schedule
    dura = agenda["core.canary_senal_dura"]
    horaria = agenda["core.canary_no_inferioridad"]
    assert dura["task"] == "core.canary_senal_dura" and horaria["task"] == "core.canary_no_inferioridad"
    assert dura["schedule"].minute == set(range(0, 60, 5))
    assert horaria["schedule"].minute == {0}
