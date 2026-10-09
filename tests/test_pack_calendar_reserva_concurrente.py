"""Dos reservas a la vez en modo interno (HU-SH-02): Postgres puede cortar con `deadlock
detected` en vez de con la restricción de exclusión. El espejo reintenta y, si igual no
queda la fila, la tool no le dice al cliente que la cita quedó agendada."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from pack_calendar.demo import RESTRICCION_SIN_SOLAPE, HorarioOcupado, es_bloqueo_mutuo
from pack_calendar.skills import CalendarSkill
from sqlalchemy.exc import DBAPIError, IntegrityError

from core.mcp.types import ToolContext
from core.shared.tenant import tenant_scope

INICIO = datetime(2027, 3, 15, 16, tzinfo=UTC)


class _ErrorPg(Exception):
    def __init__(self, sqlstate: str, texto: str = "") -> None:
        super().__init__(texto or sqlstate)
        self.sqlstate = sqlstate


def _bloqueo() -> DBAPIError:
    return DBAPIError("INSERT", {}, _ErrorPg("40P01", "deadlock detected"))


def _solape() -> IntegrityError:
    return IntegrityError("INSERT", {}, _ErrorPg("23P01", f'violates exclusion constraint "{RESTRICCION_SIN_SOLAPE}"'))


def _contexto() -> ToolContext:
    return ToolContext(conversation_id=uuid.uuid4(), customer_id=uuid.uuid4(), idempotency_key="k", channel="test")


def _skill_con_inserts(*resultados: Any) -> tuple[CalendarSkill, list[int]]:
    skill = CalendarSkill()
    llamadas: list[int] = []
    pendientes = list(resultados)

    async def _insertar(*_args: Any) -> uuid.UUID:
        llamadas.append(1)
        siguiente = pendientes.pop(0)
        if isinstance(siguiente, Exception):
            raise siguiente
        return siguiente

    skill._insertar_cita = _insertar  # type: ignore[method-assign]
    return skill, llamadas


async def _espejar(skill: CalendarSkill) -> uuid.UUID | None:
    async with tenant_scope(uuid.uuid4()):
        return await skill._mirror_appointment({}, _contexto(), INICIO, INICIO + timedelta(hours=1), title="Demo")


def test_reconoce_el_bloqueo_mutuo_de_postgres() -> None:
    assert es_bloqueo_mutuo(_bloqueo())
    assert not es_bloqueo_mutuo(_solape())
    assert not es_bloqueo_mutuo(RuntimeError("x"))


def test_tras_un_bloqueo_mutuo_el_reintento_ve_el_solape() -> None:
    skill, llamadas = _skill_con_inserts(_bloqueo(), _solape())
    with pytest.raises(HorarioOcupado):
        asyncio.run(_espejar(skill))
    assert len(llamadas) == 2


def test_tras_un_bloqueo_mutuo_el_reintento_puede_ganar() -> None:
    cita = uuid.uuid4()
    skill, llamadas = _skill_con_inserts(_bloqueo(), cita)
    assert asyncio.run(_espejar(skill)) == cita
    assert len(llamadas) == 2


def test_los_reintentos_tienen_tope() -> None:
    skill, llamadas = _skill_con_inserts(_bloqueo(), _bloqueo(), _bloqueo())
    assert asyncio.run(_espejar(skill)) is None
    assert len(llamadas) == 3


def test_en_modo_interno_sin_fila_no_dice_que_quedo_agendada() -> None:
    skill = CalendarSkill()
    config = SimpleNamespace(timezone="America/Bogota", event_duration_minutes=60, calendar_id="primary")

    async def _valor(valor: Any) -> Any:
        return valor

    skill._source = lambda: _valor("internal")  # type: ignore[method-assign]
    skill._tenant_config = lambda: _valor(config)  # type: ignore[method-assign]
    skill._appointment_type = lambda _id: _valor(None)  # type: ignore[method-assign]
    skill._email_provided_by_client = lambda _e, _c: _valor(True)  # type: ignore[method-assign]
    skill._validate_internal_slot = lambda _c, _s, _e: _valor(None)  # type: ignore[method-assign]
    skill._business_name = lambda: _valor("Negocio")  # type: ignore[method-assign]
    skill._mirror_appointment = lambda *_a, **_k: _valor(None)  # type: ignore[method-assign]

    resultado = asyncio.run(
        skill.create_event(
            {"title": "Demo", "start_datetime": "2027-03-15T11:00:00", "attendee_email": "a@ejemplo.com"}, _contexto()
        )
    )
    assert resultado["status"] == "failed"
    assert "error" in resultado and "_safe_append" not in resultado
