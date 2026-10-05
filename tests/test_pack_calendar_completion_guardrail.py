"""`calendar_completion_check` de pack-calendar: el LLM no puede afirmar que
agendó/reagendó/canceló una cita, envió el correo de confirmación, o enumeró
las citas del cliente, sin haber llamado la tool correspondiente en ese turno.

Reproduce el incidente real (tenant solvershub, 2026-09-28): el modelo dijo
"quedó agendada", "te envié la confirmación por correo" y "no tenés citas
agendadas" sin llamar create_calendar_event / send_email / list_client_appointments.
"""

from __future__ import annotations

import pytest
from pack_calendar.module import GoogleCalendarModule


def _tool_result(name: str, **fields) -> dict:
    """Resultado de una tool del turno ACTUAL y exitoso (como lo arma loop.py)."""
    return {**fields, "_tool_name": name, "_current_turn": True, "_ok": True}


def _tool_result_historial(name: str, **fields) -> dict:
    """Resultado de un turno ANTERIOR: no debe respaldar una afirmación."""
    return {**fields, "_tool_name": name, "_current_turn": False, "_ok": True}


def _tool_result_fallido(name: str, **fields) -> dict:
    """Resultado fallido del turno actual (p. ej. needs_data): no respalda."""
    return {**fields, "_tool_name": name, "_current_turn": True, "_ok": False}


@pytest.mark.asyncio
async def test_agendada_sin_create_calendar_event_bloquea():
    module = GoogleCalendarModule()
    verdict = await module._completion_guardrail(
        "¡Listo! Tu demo quedó agendada para el 28 de septiembre a las 4:00 pm.", []
    )
    assert not verdict.passed
    assert "agendada" in verdict.reason


@pytest.mark.asyncio
async def test_agendada_con_create_calendar_event_pasa():
    module = GoogleCalendarModule()
    verdict = await module._completion_guardrail(
        "¡Listo! Tu demo quedó agendada para el 28 de septiembre a las 4:00 pm.",
        [_tool_result("create_calendar_event", event_id="g-1")],
    )
    assert verdict.passed, verdict.reason


@pytest.mark.asyncio
async def test_reagendada_sin_update_calendar_event_bloquea():
    module = GoogleCalendarModule()
    verdict = await module._completion_guardrail(
        "Tu cita quedó reagendada para el 29 de septiembre a las 10:00 am.", []
    )
    assert not verdict.passed
    assert "reagendada" in verdict.reason


@pytest.mark.asyncio
async def test_reagendada_con_update_calendar_event_pasa():
    module = GoogleCalendarModule()
    verdict = await module._completion_guardrail(
        "Tu cita quedó reagendada para el 29 de septiembre a las 10:00 am.",
        [_tool_result("update_calendar_event", event_id="g-1")],
    )
    assert verdict.passed, verdict.reason


@pytest.mark.asyncio
async def test_cancelada_sin_cancel_calendar_event_bloquea():
    module = GoogleCalendarModule()
    verdict = await module._completion_guardrail("Tu cita quedó cancelada. ✅", [])
    assert not verdict.passed
    assert "cancelada" in verdict.reason


@pytest.mark.asyncio
async def test_cancelada_con_cancel_calendar_event_pasa():
    module = GoogleCalendarModule()
    verdict = await module._completion_guardrail(
        "Tu cita quedó cancelada. ✅",
        [_tool_result("cancel_calendar_event", cancelled=["g-1"])],
    )
    assert verdict.passed, verdict.reason


@pytest.mark.asyncio
async def test_correo_sin_send_email_bloquea():
    module = GoogleCalendarModule()
    verdict = await module._completion_guardrail(
        "Te envié la confirmación por correo a juanita@gmail.com. 📩", []
    )
    assert not verdict.passed
    assert "correo_enviado" in verdict.reason


@pytest.mark.asyncio
async def test_correo_con_send_email_pasa():
    module = GoogleCalendarModule()
    verdict = await module._completion_guardrail(
        "Te envié la confirmación por correo a juanita@gmail.com. 📩",
        [_tool_result("send_email", message_id="m-1")],
    )
    assert verdict.passed, verdict.reason


@pytest.mark.asyncio
async def test_listado_sin_list_client_appointments_bloquea():
    module = GoogleCalendarModule()
    verdict = await module._completion_guardrail(
        "No tenés citas agendadas en este momento.", []
    )
    assert not verdict.passed
    assert "listado" in verdict.reason


@pytest.mark.asyncio
async def test_listado_con_list_client_appointments_pasa():
    module = GoogleCalendarModule()
    verdict = await module._completion_guardrail(
        "No tenés citas agendadas en este momento.",
        [_tool_result("list_client_appointments", total=0)],
    )
    assert verdict.passed, verdict.reason


@pytest.mark.asyncio
async def test_respuesta_sin_claim_siempre_pasa():
    module = GoogleCalendarModule()
    verdict = await module._completion_guardrail(
        "¿Para qué fecha y hora te gustaría la demo?", []
    )
    assert verdict.passed, verdict.reason

@pytest.mark.asyncio
async def test_agendada_con_create_de_turno_anterior_bloquea():
    """Un create de un turno ANTERIOR no respalda "quedó agendada" (post-prueba)."""
    module = GoogleCalendarModule()
    verdict = await module._completion_guardrail(
        "¡Listo! Tu demo quedó agendada para el 28 de septiembre a las 4:00 pm.",
        [_tool_result_historial("create_calendar_event", event_id="g-1")],
    )
    assert not verdict.passed


@pytest.mark.asyncio
async def test_agendada_con_create_fallido_needs_data_bloquea():
    """Un create que devolvió needs_data/error no respalda "quedó agendada"."""
    module = GoogleCalendarModule()
    verdict = await module._completion_guardrail(
        "¡Listo! Tu demo quedó agendada para el 28 de septiembre a las 4:00 pm.",
        [_tool_result_fallido("create_calendar_event", status="needs_data", error="falta email")],
    )
    assert not verdict.passed


@pytest.mark.asyncio
async def test_cita_existente_sin_listar_bloquea():
    """Afirmar que el cliente ya tiene una cita exige list_client_appointments."""
    module = GoogleCalendarModule()
    verdict = await module._completion_guardrail(
        "Tenés tu demo agendada para el viernes 9 de octubre a las 3:00 pm.", []
    )
    assert not verdict.passed
    assert "cita_existente" in verdict.reason


@pytest.mark.asyncio
async def test_cita_existente_con_listar_pasa():
    module = GoogleCalendarModule()
    verdict = await module._completion_guardrail(
        "Tenés tu demo agendada para el viernes 9 de octubre a las 3:00 pm.",
        [_tool_result("list_client_appointments", total=1)],
    )
    assert verdict.passed, verdict.reason


@pytest.mark.asyncio
async def test_reintento_guia_a_pedir_el_dato_faltante():
    """Si la tool se intentó y devolvió needs_data, el reintento debe guiar a
    pedir el dato (no a repetir la afirmación ni escalar)."""
    module = GoogleCalendarModule()
    verdict = await module._completion_guardrail(
        "¡Listo! Tu demo quedó agendada.",
        [
            _tool_result_fallido(
                "create_calendar_event",
                status="needs_data",
                error="Falta el email del cliente: pedíselo antes de agendar.",
            )
        ],
    )
    assert not verdict.passed
    assert "Falta el email del cliente" in verdict.retry_instruction
    assert "NO afirmes que se completó" in verdict.retry_instruction


@pytest.mark.asyncio
async def test_fallo_de_cancelacion_sin_tool_bloquea():
    """Decir "no pude cancelarla" sin llamar la tool también es una afirmación
    de resultado (no se puede saber el fallo sin intentarlo)."""
    module = GoogleCalendarModule()
    verdict = await module._completion_guardrail(
        "No pude cancelarla, Robinson: el sistema no encontró la cita del martes "
        "13 de octubre para cancelar.",
        [],
    )
    assert not verdict.passed
    assert "cancelada" in verdict.reason


@pytest.mark.asyncio
async def test_fallo_de_reagenda_sin_tool_bloquea():
    module = GoogleCalendarModule()
    verdict = await module._completion_guardrail(
        "No pude reagendarla: no encontré la cita para mover.", []
    )
    assert not verdict.passed


@pytest.mark.asyncio
async def test_no_aparece_cita_sin_listar_bloquea():
    module = GoogleCalendarModule()
    verdict = await module._completion_guardrail(
        "Mmm, revisé tu agenda y no me aparece ninguna cita registrada.", []
    )
    assert not verdict.passed
