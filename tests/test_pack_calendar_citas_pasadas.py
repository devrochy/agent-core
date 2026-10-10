"""`list_client_appointments` muestra las citas de las últimas 24 h que ya pasaron.

Test 2026-10-10 (tenant solvershub): a las 23:49 de Bogotá la clienta preguntó por
su cita de las 10:00 de ese mismo día, todavía en `scheduled`. La tool solo
miraba las futuras, devolvió `total: 0` tres veces y el asistente negó que la
cita existiera. Ahora la cita aparece con `ya_paso: true` y sin índice (la
numeración sigue siendo la que resuelve `cancel_calendar_event`).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from pack_calendar.skills import _RECENT_PAST, CalendarSkill


def _cita(title: str, start_at: datetime) -> SimpleNamespace:
    return SimpleNamespace(id=uuid.uuid4(), google_event_id=None, title=title, start_at=start_at)


def _skill(rows: list[SimpleNamespace], visto: dict) -> CalendarSkill:
    skill = CalendarSkill()

    async def _client_appointments(arguments, context, *, since=None):
        visto["since"] = since
        return [r for r in rows if since is None or r.start_at >= since]

    async def _timezone_name() -> str:
        return "America/Bogota"

    skill._client_appointments = _client_appointments  # type: ignore[method-assign]
    skill._timezone_name = _timezone_name  # type: ignore[method-assign]
    return skill


@pytest.mark.asyncio
async def test_cita_de_hoy_ya_pasada_aparece_marcada():
    ahora = datetime.now(UTC)
    pasada = _cita("Demo", ahora - timedelta(hours=13, minutes=49))
    visto: dict = {}

    listed = await _skill([pasada], visto).list_appointments({}, object())

    assert listed["total"] == 1
    assert listed["proximas"] == 0
    (cita,) = listed["appointments"]
    assert cita["ya_paso"] is True
    assert cita["event_id"] == str(pasada.id)
    assert "index" not in cita
    assert cita["hora"] == pasada.start_at.astimezone(_tz()).strftime("%H:%M")
    # La ventana hacia atrás es de 24 h.
    assert abs((ahora - visto["since"]) - _RECENT_PAST) < timedelta(seconds=5)


@pytest.mark.asyncio
async def test_pasadas_primero_sin_indice_y_futuras_numeradas_desde_1():
    ahora = datetime.now(UTC)
    vieja = _cita("Demo vieja", ahora - timedelta(hours=30))
    pasada = _cita("Demo hoy", ahora - timedelta(hours=2))
    futura1 = _cita("Demo mañana", ahora + timedelta(days=1))
    futura2 = _cita("Demo pasado", ahora + timedelta(days=2))

    listed = await _skill([vieja, pasada, futura1, futura2], {}).list_appointments({}, object())

    assert [c["title"] for c in listed["appointments"]] == ["Demo hoy", "Demo mañana", "Demo pasado"]
    assert [c["ya_paso"] for c in listed["appointments"]] == [True, False, False]
    assert [c.get("index") for c in listed["appointments"]] == [None, 1, 2]
    assert listed["total"] == 3
    assert listed["proximas"] == 2


@pytest.mark.asyncio
async def test_sin_citas_total_cero():
    listed = await _skill([], {}).list_appointments({}, object())
    assert listed == {"appointments": [], "total": 0, "proximas": 0}


def test_descripcion_prohibe_negar_una_cita_pasada():
    (tool,) = [t for t in CalendarSkill().get_tools() if t.name == "list_client_appointments"]
    assert "ya_paso=true" in tool.description
    assert "NUNCA digas que no tiene cita" in tool.description


def _tz():
    from zoneinfo import ZoneInfo

    return ZoneInfo("America/Bogota")
