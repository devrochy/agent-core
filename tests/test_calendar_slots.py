"""Tests unitarios del cálculo de horarios libres del calendario interno (ADR-0084).

Deterministas, sin BD: `_free_slots` es una función pura; `_open_window` solo lee
la config del pack.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from pack_calendar.config import CalendarConfig
from pack_calendar.skills import _free_slots, _google_interval, _open_window, _slot_conflict

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


# --------------------------------------------------- validación dura (HU-CAL-02)

def _futuro(hhmm: str, date: str = "2027-03-15") -> datetime:
    return datetime.fromisoformat(f"{date}T{hhmm}:00").replace(tzinfo=TZ)


def test_slot_conflict_libre() -> None:
    assert _slot_conflict(
        "09:00-18:00", [], TZ, _futuro("10:00"), _futuro("11:00"), buffer_minutes=0
    ) is None


def test_slot_conflict_fuera_de_horario() -> None:
    assert _slot_conflict(
        "09:00-18:00", [], TZ, _futuro("08:00"), _futuro("09:00"), buffer_minutes=0
    ) == "outside_hours"
    assert _slot_conflict(
        "", [], TZ, _futuro("10:00"), _futuro("11:00"), buffer_minutes=0
    ) == "outside_hours"


def test_slot_conflict_pasado() -> None:
    pasado = datetime.fromisoformat("2020-01-01T10:00:00").replace(tzinfo=TZ)
    assert _slot_conflict(
        "09:00-18:00", [], TZ, pasado, pasado.replace(hour=11), buffer_minutes=0
    ) == "in_past"


def test_slot_conflict_ocupado() -> None:
    busy = [(_futuro("10:30"), _futuro("11:30"))]
    assert _slot_conflict(
        "09:00-18:00", busy, TZ, _futuro("10:00"), _futuro("11:00"), buffer_minutes=0
    ) == "occupied"


def test_slot_conflict_buffer_rechaza_pegado() -> None:
    busy = [(_futuro("10:00"), _futuro("11:00"))]
    # Con 15 min de margen, una cita a las 11:00 choca con el ocupado 10:00-11:00.
    assert _slot_conflict(
        "09:00-18:00", busy, TZ, _futuro("11:00"), _futuro("11:30"), buffer_minutes=15
    ) == "occupied"


# --- HU-CAL-05: nunca horas pasadas, link de la cita y plantillas seguras -----


def test_slots_libres_desde_ahora_omite_horas_pasadas() -> None:
    """`not_before`: hoy a las 10:20 ya no se ofrecen 09:00 ni 10:00."""
    out = _free_slots(
        "09:00-13:00", [], TZ, "2026-09-30",
        slot_minutes=60, duration_minutes=60, buffer_minutes=0, not_before=_dt("10:20"),
    )
    assert out == ["11:00", "12:00"]


def test_link_de_calendario_con_hora_local_y_zona() -> None:
    from urllib.parse import parse_qs, urlparse

    from pack_calendar.skills import _calendar_link

    start = datetime(2027, 3, 15, 15, 0, tzinfo=ZoneInfo("UTC"))  # 10:00 en Bogotá
    end = datetime(2027, 3, 15, 16, 0, tzinfo=ZoneInfo("UTC"))
    link = _calendar_link(start, end, "Demo con Laura & Co", "Cita con SolversHub", "America/Bogota")

    parsed = urlparse(link)
    assert f"{parsed.scheme}://{parsed.netloc}{parsed.path}" == "https://calendar.google.com/calendar/render"
    query = parse_qs(parsed.query)
    assert query["action"] == ["TEMPLATE"]
    assert query["text"] == ["Demo con Laura & Co"]
    assert query["dates"] == ["20270315T100000/20270315T110000"]
    assert query["ctz"] == ["America/Bogota"]
    assert query["details"] == ["Cita con SolversHub"]


def test_plantilla_con_placeholder_desconocido_no_rompe() -> None:
    from pack_calendar.skills import _render_template

    assert _render_template("Hola {cliente}: {link}", cliente="Laura", link="https://x") == "Hola Laura: https://x"
    # Un placeholder que el negocio escribió mal queda literal en vez de tumbar el aviso.
    assert _render_template("Tu cita {titulo} {fechaa}", titulo="Demo") == "Tu cita Demo {fechaa}"
