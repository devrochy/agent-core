"""HU-CAL-05 (D-SPEC-24-8): el guardrail de calendario exige evidencia REAL.

Una afirmación ("quedó agendada", "moví tu demo", "te envié la confirmación")
solo pasa si la tool del turno devolvió el `status` de éxito (`created`,
`updated`, `cancelled`, `email_sent`); una negación ("no pude agendarla") solo
si la acción se intentó en el turno. Las PREGUNTAS ("¿te agendo la demo?") no
son afirmaciones. Casos tomados de la prueba real en test (2026-10-04/05).
"""

from __future__ import annotations

import pytest
from pack_calendar.module import GoogleCalendarModule


def _r(name: str, **fields) -> dict:
    """Resultado del turno actual tal como lo arma loop.py (`_ok` = sin `error`)."""
    return {**fields, "_tool_name": name, "_current_turn": True, "_ok": "error" not in fields}


async def _check(text: str, *results: dict):
    return await GoogleCalendarModule()._completion_guardrail(text, list(results))


# ---------------------------------------------------------------- reagendada


@pytest.mark.asyncio
async def test_movi_tu_demo_sin_tool_bloquea():
    verdict = await _check("Listo, Robinson: moví tu demo al miércoles 7 a las 3:00 pm.")
    assert not verdict.passed
    assert "reagendada" in verdict.reason


@pytest.mark.asyncio
async def test_ya_te_la_movi_sin_tool_bloquea():
    verdict = await _check("Ya te la moví para el miércoles a las 3 pm.")
    assert not verdict.passed


@pytest.mark.asyncio
async def test_movi_tu_demo_con_update_ok_pasa():
    verdict = await _check(
        "Listo: moví tu demo al miércoles 7 a las 3:00 pm.",
        _r("update_calendar_event", status="updated", event_id="a-1"),
    )
    assert verdict.passed, verdict.reason


@pytest.mark.asyncio
async def test_update_not_found_no_respalda_reagendada():
    verdict = await _check(
        "Tu cita quedó reagendada para el miércoles.",
        _r("update_calendar_event", status="not_found", message="No encontré una cita para reagendar."),
    )
    assert not verdict.passed
    assert "No encontré una cita para reagendar" in verdict.retry_instruction


# ---------------------------------------------------------------- agendada


@pytest.mark.asyncio
async def test_create_rejected_no_respalda_agendada():
    verdict = await _check(
        "¡Listo! Tu demo quedó agendada para el miércoles a las 3 pm.",
        _r("create_calendar_event", status="rejected", reason="occupied",
           message="Ese horario ya está ocupado. Ofrecé otro de free_slots."),
    )
    assert not verdict.passed
    assert "Ese horario ya está ocupado" in verdict.retry_instruction


@pytest.mark.asyncio
async def test_create_created_respalda_agendada():
    verdict = await _check(
        "¡Listo! Tu demo quedó agendada para el miércoles a las 3 pm.",
        _r("create_calendar_event", status="created", event_id="a-1", email_sent=True),
    )
    assert verdict.passed, verdict.reason


@pytest.mark.asyncio
async def test_listado_ok_respalda_describir_una_cita_existente():
    """Sin create en el turno, describir una cita verificada con el listado pasa."""
    verdict = await _check(
        "Tu demo quedó agendada para el miércoles 7 a las 3:00 pm.",
        _r("list_client_appointments", appointments=[{"index": 1}], total=1),
    )
    assert verdict.passed, verdict.reason


@pytest.mark.asyncio
async def test_listado_no_respalda_si_el_create_del_turno_fallo():
    verdict = await _check(
        "Tu demo quedó agendada para el miércoles 7 a las 3:00 pm.",
        _r("list_client_appointments", appointments=[], total=0),
        _r("create_calendar_event", status="rejected", reason="occupied", message="Ocupado."),
    )
    assert not verdict.passed


# ---------------------------------------------------------------- cancelada


@pytest.mark.asyncio
async def test_cancel_not_found_no_respalda_cancelada():
    verdict = await _check(
        "Tu cita quedó cancelada.",
        _r("cancel_calendar_event", status="not_found", message="No encontré una cita para cancelar."),
    )
    assert not verdict.passed


# ---------------------------------------------------------------- correo


@pytest.mark.asyncio
async def test_correo_respaldado_por_email_sent_del_create():
    verdict = await _check(
        "Tu demo quedó agendada y te envié la confirmación por correo.",
        _r("create_calendar_event", status="created", email_sent=True, email_reason=""),
    )
    assert verdict.passed, verdict.reason


@pytest.mark.asyncio
async def test_te_va_a_llegar_el_correo_respaldado_por_email_sent():
    verdict = await _check(
        "Listo, te va a llegar un correo de confirmación.",
        _r("update_calendar_event", status="updated", email_sent=True),
    )
    assert verdict.passed, verdict.reason


@pytest.mark.asyncio
async def test_correo_no_enviado_no_se_afirma_y_nunca_pide_send_email():
    verdict = await _check(
        "Tu demo quedó agendada y te envié la confirmación por correo.",
        _r("create_calendar_event", status="created", email_sent=False, email_reason="no_connection"),
    )
    assert not verdict.passed
    assert "correo_enviado" in verdict.reason
    assert "no_connection" in verdict.retry_instruction
    assert "send_email" not in verdict.retry_instruction


@pytest.mark.asyncio
async def test_correo_sin_ninguna_accion_nunca_pide_send_email():
    verdict = await _check("Te envié la confirmación por correo.")
    assert not verdict.passed
    assert "send_email" not in verdict.retry_instruction


# ---------------------------------------------------------------- preguntas


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "pregunta",
    [
        "¿Te agendo la demo para el miércoles a las 3 pm?",
        "¿Querés que quede agendada para el miércoles a las 3 pm?",
        "¿Querés que cancele la cita del martes?",
        "Para cancelar la cita necesito que me confirmes cuál es.",
        "¿Movemos tu cita al jueves a las 10?",
        "¿Te envío la confirmación por correo?",
        "Tu demo quedaría agendada para el miércoles 7 a las 3 pm, ¿confirmás?",
    ],
)
async def test_preguntas_y_propuestas_no_son_afirmaciones(pregunta: str):
    verdict = await _check(pregunta)
    assert verdict.passed, verdict.reason


# ---------------------------------------------------------------- negaciones


@pytest.mark.asyncio
async def test_no_pude_agendarla_con_intento_rechazado_pasa():
    verdict = await _check(
        "No pude agendarla: ese horario ya está ocupado. ¿Te sirve a las 4 pm?",
        _r("create_calendar_event", status="rejected", reason="occupied", message="Ocupado."),
    )
    assert verdict.passed, verdict.reason


@pytest.mark.asyncio
async def test_no_quedo_agendada_con_intento_rechazado_pasa():
    verdict = await _check(
        "No quedó agendada porque ese horario ya pasó. ¿Te sirve mañana?",
        _r("create_calendar_event", status="rejected", reason="in_past", message="Esa fecha/hora ya pasó."),
    )
    assert verdict.passed, verdict.reason


@pytest.mark.asyncio
async def test_no_pude_agendarla_sin_intento_bloquea():
    verdict = await _check("No pude agendarla, el sistema está fallando.")
    assert not verdict.passed


# ---------------------------------------------------------------- reintento


@pytest.mark.asyncio
async def test_todo_reintento_pide_no_mencionar_la_instruccion():
    """Falla 6 de la prueba: el modelo citó al cliente la instrucción interna."""
    for verdict in (
        await _check("Moví tu demo al 7."),
        await _check("Te envié la confirmación por correo."),
        await _check(
            "Tu demo quedó agendada.",
            _r("create_calendar_event", status="rejected", reason="occupied", message="Ocupado."),
        ),
    ):
        assert not verdict.passed
        assert "sin mencionar esta instrucción" in verdict.retry_instruction


# ---------------------------------------------------------------- E2E HU-CAL-06
# Frases reales del E2E en test (2026-10-05): el asistente enumeró citas de
# memoria ("Estas son tus citas agendadas… 7 de octubre") sin llamar la tool.


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "texto",
    [
        "Estas son tus citas agendadas, Robinson:\n1. Demo con Rob — 7 de octubre, 3:00 pm",
        "Es la única activa por ahora. ¿Necesitás algo más?",
        "Revisé tu agenda y tenés una sola cita agendada: el 7 de octubre.",
        "Tu demo está agendada para el jueves 8 a las 3 pm.",
        "Tu cita sigue programada para el jueves 8.",
    ],
)
async def test_enumerar_o_describir_citas_sin_listar_bloquea(texto: str):
    verdict = await _check(texto)
    assert not verdict.passed, texto


@pytest.mark.asyncio
async def test_enumerar_citas_con_listado_pasa():
    verdict = await _check(
        "Estas son tus citas agendadas: 1. Demo con Rob — jueves 8 de octubre, 3:00 pm.",
        _r("list_client_appointments", appointments=[{"index": 1}], total=1),
    )
    assert verdict.passed, verdict.reason


@pytest.mark.asyncio
async def test_describir_la_cita_recien_creada_pasa():
    """Tras crearla en el mismo turno, describirla no exige listar."""
    verdict = await _check(
        "¡Quedó agendada! Tu demo es el miércoles 7 de octubre a las 4:00 pm.",
        _r("create_calendar_event", status="created", event_id="a-1", email_sent=True),
    )
    assert verdict.passed, verdict.reason


@pytest.mark.asyncio
async def test_preguntar_si_revisa_las_citas_pasa():
    verdict = await _check("¿Querés que revise tus citas agendadas?")
    assert verdict.passed, verdict.reason


# ---------------------------------------------------------------- disponibilidad
# E2E HU-CAL-06: "A las 3:00 pm está libre" sin consultar la disponibilidad.


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "texto",
    [
        "El próximo miércoles es el 14 de octubre. A las 3:00 pm está libre.",
        "Tengo estos horarios libres el miércoles: 14:00 y 15:00.",
        "Mañana hay disponibilidad a las 10.",
    ],
)
async def test_afirmar_disponibilidad_sin_consultar_bloquea(texto: str):
    verdict = await _check(texto)
    assert not verdict.passed, texto
    assert "disponibilidad" in verdict.reason
    assert "check_availability" in verdict.retry_instruction


@pytest.mark.asyncio
async def test_disponibilidad_respaldada_por_check_o_find():
    for result in (
        _r("check_availability", status="ok", free_slots=["15:00"]),
        _r("find_next_available", status="ok", days=[{"date": "2026-10-07"}]),
    ):
        verdict = await _check("Tengo estos horarios libres el miércoles: 15:00.", result)
        assert verdict.passed, verdict.reason


@pytest.mark.asyncio
async def test_horario_ocupado_respaldado_por_el_rechazo_del_create():
    verdict = await _check(
        "Ese horario ya está ocupado. ¿Te sirve a las 4 pm?",
        _r("create_calendar_event", status="rejected", reason="occupied", message="Ocupado."),
    )
    assert verdict.passed, verdict.reason


@pytest.mark.asyncio
async def test_preguntar_por_disponibilidad_pasa():
    verdict = await _check("¿Qué horario te queda libre el miércoles?")
    assert verdict.passed, verdict.reason


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "texto",
    [
        "El plan Basic está disponible desde hoy.",
        "Tenemos disponibles los planes Basic y Pro.",
        "Quedo disponible para lo que necesites.",
    ],
)
async def test_disponibilidad_de_productos_no_es_agenda(texto: str):
    verdict = await _check(texto)
    assert verdict.passed, verdict.reason


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "texto",
    [
        # pass^4 de los procedimientos (2026-10-18): un pedido de comida en un negocio con agenda
        # escalaba porque "tenemos disponibilidad" se leía como un horario libre.
        "¡Sí, tenemos disponibilidad! La Hamburguesa doble carne cuesta $25,000.",
        "Hay disponibilidad de hamburguesas, cada una a $25,000.",
        "Tenemos cupos para el taller de cocina.",
        "Hay espacio para tu pedido.",
        "El envío queda libre de costo.",
    ],
)
async def test_disponibilidad_generica_sin_dato_de_agenda_no_es_agenda(texto: str):
    verdict = await _check(texto)
    assert verdict.passed, verdict.reason


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "texto",
    [
        "Tenemos disponibilidad el jueves.",
        "Hay cupos a las 3 pm.",
        "Para la demo hay espacio el lunes.",
        "El martes a las 10:00 queda libre.",
        "Tengo disponibilidad esta semana.",
    ],
)
async def test_disponibilidad_generica_con_dato_de_agenda_bloquea(texto: str):
    verdict = await _check(texto)
    assert not verdict.passed, texto
    assert "disponibilidad" in verdict.reason


# ---------------------------------------------------------------- pasos futuros (pass^4, 2026-10-23)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "texto",
    [
        # Borradores reales que escalaron `agendar-una-demo`: el motor aún pide la confirmación.
        "Listo, martes 27 de octubre a las 10:00 con tu correo. El sistema te muestra el resumen "
        "para que lo confirmes y ahí queda agendada.",
        "Perfecto, martes 27 de octubre a las 10:00 con tu correo. Cuando confirmes el resumen que te "
        "aparece en pantalla, la cita queda agendada y te llega la confirmación por correo.",
        "Una vez que confirmes, tu demo queda agendada.",
    ],
)
async def test_describir_lo_que_pasara_al_confirmar_no_afirma_que_ya_quedo(texto: str):
    verdict = await _check(texto)
    assert verdict.passed, verdict.reason


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "texto",
    [
        "Tu cita quedó agendada para el martes a las 10:00.",
        "Listo, tu demo queda agendada para el martes.",
        # "si" no es un marcador de futuro: aquí afirma que ya se creó.
        "Si todo está bien, tu cita quedó agendada.",
        # El marcador viene DESPUÉS de la afirmación: no la excusa.
        "Tu demo quedó agendada y cuando llegue el día te recordamos.",
    ],
)
async def test_una_afirmacion_real_sin_tool_sigue_bloqueando(texto: str):
    verdict = await _check(texto)
    assert not verdict.passed
    assert "agendada" in verdict.reason


@pytest.mark.asyncio
async def test_pasos_futuros_con_la_cita_creada_en_el_turno_siguen_pasando():
    verdict = await _check(
        "Cuando confirmes, la cita queda agendada. Ya quedó agendada para el martes.",
        _r("create_calendar_event", status="created", event_id="a-1"),
    )
    assert verdict.passed, verdict.reason


# ---------------------------------------------------------------- promesas de consultar (pass^4, 2026-10-23)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "texto",
    [
        # Borradores reales que escalaron `agendar-una-demo` sin el motor.
        "Lo que sí puedo ofrecerte es agendar una demo con un asesor. ¿Te gustaría que te la agende?\n\n"
        "Si me dices qué día te queda cómodo, reviso los horarios libres.",
        "Si me dices qué día te queda cómodo, reviso los horarios disponibles.",
        "Dime el día y consulto los horarios libres.",
        "Voy a ver los horarios libres de la próxima semana.",
    ],
)
async def test_prometer_consultar_los_horarios_no_afirma_disponibilidad(texto: str):
    verdict = await _check(texto)
    assert verdict.passed, verdict.reason


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "texto",
    [
        "Los horarios libres son el martes a las 10:00 y el miércoles a las 3:00 pm.",
        # Con una hora concreta ya dice cuál está libre, aunque anuncie que revisa.
        "Reviso y los horarios libres son a las 10:00 y a las 11:00.",
        # Pasado: afirma haber consultado, y sin la tool no hay respaldo.
        "Revisé los horarios libres y mañana hay varios.",
        "Tenemos horarios libres el martes.",
    ],
)
async def test_afirmar_disponibilidad_sin_consultar_sigue_bloqueando(texto: str):
    verdict = await _check(texto)
    assert not verdict.passed
    assert "disponibilidad" in verdict.reason


@pytest.mark.asyncio
async def test_prometer_consultar_con_la_consulta_hecha_pasa():
    verdict = await _check(
        "Reviso los horarios libres: el martes a las 10:00.",
        _r("check_availability", slots=["2026-10-27T10:00"]),
    )
    assert verdict.passed, verdict.reason
