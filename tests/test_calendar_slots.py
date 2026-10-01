"""Tests unitarios del cálculo de horarios libres del calendario interno (ADR-0084).

Deterministas, sin BD: `_free_slots` es una función pura; `_open_window` solo lee
la config del pack.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from pack_calendar.config import CalendarConfig
from pack_calendar.skills import _free_slots, _google_interval, _open_window

TZ = ZoneInfo("America/Bogota")


def _dt(hhmm: str) -> datetime:
    return datetime.fromisoformat(f"2026-09-30T{hhmm}:00").replace(tzinfo=TZ)


def test_slots_libres_sin_ocupados() -> None:
    out = _free_slots(
        "09:00-12:00", [], TZ, "2026-09-30", slot_minutes=60, duration_minutes=60, buffer_minutes=0
    )
    assert out == ["09:00", "10:00", "11:00"]


def test_slots_libres_excluye_ocupado() -> None:
    busy = [(_dt("10:00"), _dt("11:00"))]
    out = _free_slots(
        "09:00-12:00", busy, TZ, "2026-09-30", slot_minutes=60, duration_minutes=60, buffer_minutes=0
    )
    assert out == ["09:00", "11:00"]


def test_buffer_expande_el_bloque_ocupado() -> None:
    # Con 15 min de margen a cada lado, el ocupado 10:00-11:00 bloquea los slots
    # de 09:00, 10:00 y 11:00; el primero libre es 12:00.
    busy = [(_dt("10:00"), _dt("11:00"))]
    out = _free_slots(
        "09:00-14:00", busy, TZ, "2026-09-30", slot_minutes=60, duration_minutes=60, buffer_minutes=15
    )
    assert out == ["12:00", "13:00"]


def test_ventana_cerrada_no_ofrece_slots() -> None:
    assert _free_slots("", [], TZ, "2026-09-30", slot_minutes=60, duration_minutes=60, buffer_minutes=0) == []


def test_duracion_larga_recorta_slots() -> None:
    # Una cita de 120 min dentro de 09:00-12:00 entra a las 09:00 y a las 10:00.
    out = _free_slots(
        "09:00-12:00", [], TZ, "2026-09-30", slot_minutes=60, duration_minutes=120, buffer_minutes=0
    )
    assert out == ["09:00", "10:00"]


def test_open_window_por_dia() -> None:
    config = CalendarConfig(
        source="interno",
        business_hours='{"wed": "09:00-13:00", "sun": ""}',
        timezone="America/Bogota",
    )
    # 2026-09-30 es miércoles.
    assert _open_window(config, "2026-09-30") == "09:00-13:00"
    # 2026-10-04 es domingo (cerrado).
    assert _open_window(config, "2026-10-04") == ""


def test_config_interno_no_requiere_google() -> None:
    config = CalendarConfig(source="interno")
    assert config.client_id == ""
    assert config.source == "interno"


def test_config_google_exige_credenciales() -> None:
    import pytest

    with pytest.raises(ValueError):
        CalendarConfig(source="google")


def test_intervalo_evento_google_allday() -> None:
    iv = _google_interval({"start": {"date": "2026-09-30"}, "end": {"date": "2026-10-01"}}, TZ)
    assert iv is not None
    start, end = iv
    assert start.hour == 0 and (end - start).days == 1
