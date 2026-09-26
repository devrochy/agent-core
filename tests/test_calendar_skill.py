"""Disponibilidad por horario de atención del pack-calendar."""

from __future__ import annotations

from pack_calendar.skills import _open_window


class _Cfg:
    business_hours = '{"mon": "09:00-18:00", "sun": ""}'


def test_open_window_por_dia():
    # 2026-09-28 es lunes; 2026-09-27 domingo.
    assert _open_window(_Cfg(), "2026-09-28") == "09:00-18:00"
    assert _open_window(_Cfg(), "2026-09-27") == ""


def test_open_window_fecha_invalida_o_json_roto():
    assert _open_window(_Cfg(), "no-es-fecha") == ""

    class _Bad:
        business_hours = "no-json"

    assert _open_window(_Bad(), "2026-09-28") == ""
