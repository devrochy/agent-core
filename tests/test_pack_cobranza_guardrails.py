"""`cob_promesa_check` (pack-cobranza): una afirmación de promesa o acuerdo exige la tool del
turno, pero una cita del pack de calendario no es una promesa (incidente del 2026-10-19: en un
negocio con cobranza y calendario, cada cita bien creada se escalaba)."""

from __future__ import annotations

from typing import Any

import pytest
from pack_cobranza.guardrails import promesa_guardrail


def _tool(nombre: str, *, actual: bool = True, ok: bool = True, **datos: Any) -> dict[str, Any]:
    return {**datos, "_tool_name": nombre, "_current_turn": actual, "_ok": ok}


_CITA = _tool(
    "create_calendar_event",
    status="created",
    email_sent=True,
    _safe_append="📅 Tu cita quedó agendada para el martes 21 de octubre a las 10:00.",
)


@pytest.mark.parametrize(
    ("texto", "resultados"),
    [
        # El incidente: confirmación de la cita con la tool del turno.
        ("¡Listo! Tu cita quedó agendada para el martes 21 a las 10:00.", [_CITA]),
        ("📅 Tu cita quedó agendada para el martes 21 de octubre a las 10:00.", [_CITA]),
        ("Te agendé la demo para el jueves a las 3:00 p. m.", [_CITA]),
        ("Tu cita quedó reagendada para el viernes a las 9:00.", [_tool("update_calendar_event", status="updated")]),
        # Una visita de cobro es una cita: la respalda la tool de calendario del turno.
        ("Te agendé la visita para recoger el pago el viernes.", [_CITA]),
        # Sin hablar de promesas, pagos ni acuerdos, ni siquiera hace falta la tool.
        ("Tu reunión quedó agendada para mañana.", []),
        ("Ya registré tu correo para enviarte la confirmación.", []),
    ],
)
async def test_una_cita_no_es_una_promesa(texto: str, resultados: list[dict[str, Any]]) -> None:
    assert (await promesa_guardrail(texto, resultados)).passed


@pytest.mark.parametrize(
    "texto",
    [
        "Dejé registrada tu promesa de pago para el viernes.",
        "Acordamos que pagas el 30.",
        "Registré tu abono de $50.000.",
        "Tu promesa quedó registrada para el 15 de noviembre.",
        "Te agendé el pago de la cuota para el viernes.",
        "Quedamos en que el lunes te ponés al día.",
    ],
)
async def test_una_promesa_sin_la_tool_falla(texto: str) -> None:
    resultado = await promesa_guardrail(texto, [])
    assert not resultado.passed
    assert "cob_registrar_promesa_pago" in (resultado.retry_instruction or "")


@pytest.mark.parametrize(
    "texto",
    [
        # La cita no respalda una promesa que viene en la misma respuesta.
        "Tu cita quedó agendada para el martes y acordamos que pagas el 30.",
        "Tu cita quedó agendada y dejé registrada tu promesa de pago.",
        "Te agendé tu compromiso de pago para el viernes.",
    ],
)
async def test_la_cita_no_respalda_la_promesa(texto: str) -> None:
    assert not (await promesa_guardrail(texto, [_CITA])).passed


@pytest.mark.parametrize(
    "cita",
    [
        _tool("create_calendar_event", actual=False, status="created"),
        _tool("create_calendar_event", ok=False, error="Horario ocupado."),
    ],
)
async def test_solo_la_cita_exitosa_del_turno_respalda_agendar_un_pago(cita: dict[str, Any]) -> None:
    assert not (await promesa_guardrail("Te agendé el pago de la cuota para el viernes.", [cita])).passed


@pytest.mark.parametrize("tool", ["cob_registrar_promesa_pago", "cob_registrar_promesa_saldo"])
@pytest.mark.parametrize(
    "texto",
    ["Dejé registrada tu promesa de pago para el viernes.", "Acordamos que pagas el 30."],
)
async def test_con_la_tool_de_promesa_pasa(tool: str, texto: str) -> None:
    assert (await promesa_guardrail(texto, [_tool(tool, status="registrada")])).passed
