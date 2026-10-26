"""HU-AOS-20 parte 3 · el verificador de horarios y de preguntas (sin LLM).

- **Parser:** horas y fechas en español, sin confundir «24 horas», «2 horas» ni «1.500»; «a las 3» vale
  03:00 o 15:00; «p. m.» no corta la oración; un día de la semana que no coincide va a sombra.
- **Respaldo:** `free_slots` y cualquier instante ISO del servidor sí; `busy_slots` y el texto de un
  error no; el horario del sistema sí; lo que dijo el cliente solo como eco de un dato que la tarea
  fijó del cliente.
- **Preguntas:** «¿Y…?» y las listas de datos cuentan una vez.
- **Modos:** `shadow` cuenta en `sombra`; `active` rechaza; sin estado, nada cambia.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

import pytest

from core.orchestrator.verificador import EstadoTareas, Evidencia, Modos, verificar
from core.orchestrator.verificador.horarios import (
    contar_preguntas,
    fechas_en,
    horarios_sin_respaldo,
    horas_en,
    normalizar,
    respaldo_de,
)
from core.shared.polaridad import oraciones_afirmativas

HOY = date(2026, 10, 26)  # lunes
AHORA = datetime(2026, 10, 26, 10, 0)
AGENDA = {
    "_tool_name": "check_availability",
    "date": "2026-10-29",
    "free_slots": ["09:00", "15:00"],
    "busy_slots": [{"start": "2026-10-29T11:00:00-05:00", "end": "2026-10-29T12:00:00-05:00"}],
}


def _horas(texto: str) -> list[list[str]]:
    return [sorted(m.horas) for m in horas_en(normalizar(texto))]


@pytest.mark.parametrize(
    ("texto", "esperado"),
    [
        ("a las 3 p. m.", [["15:00"]]),
        ("a las 3pm", [["15:00"]]),
        ("15:00", [["15:00"]]),
        ("a las 3", [["03:00", "15:00"]]),
        ("a las 3 y media de la tarde", [["15:30"]]),
        ("8 de la noche", [["20:00"]]),
        ("a mediodía", [["12:00"]]),
        ("15h30", [["15:30"]]),
        ("atendemos las 24 horas", []),
        ("durante las 2 horas", []),
        ("me cobraron 1.500", []),
    ],
)
def test_horas(texto: str, esperado: list[list[str]]) -> None:
    assert _horas(texto) == esperado


def test_fechas() -> None:
    def f(texto: str) -> list[tuple[list[str], bool]]:
        return [(sorted(map(str, m.fechas)), m.sin_normalizar) for m in fechas_en(normalizar(texto), HOY)]

    assert f("el jueves 29") == [(["2026-10-29"], False)]
    assert f("el 30 de octubre") == [(["2026-10-30"], False)]
    assert f("el viernes 29") == [([], True)]  # el 29 es jueves: sombra, nunca bloquea
    assert f("mañana a las 9") == [(["2026-10-27"], False)]
    assert f("a las 9 de la mañana") == []  # «de la mañana» es parte del día, no una fecha


def test_preguntas() -> None:
    assert contar_preguntas("¿Cuál es tu nombre?\n¿Y tu correo?") == 1
    assert contar_preguntas("Necesito:\n1. ¿Tu nombre?\n2. ¿Tu correo?") == 1
    assert contar_preguntas("¿Mensual o anual? ¿Te agendo la demo?") == 2
    assert contar_preguntas("¿Algo más??") == 1
    assert contar_preguntas("Listo, quedó.") == 0


def _fallas(texto: str, resultados: list[dict[str, Any]], tareas: tuple[dict[str, Any], ...] = (), **kw: Any) -> list[str]:
    respaldo, hoy = respaldo_de(resultados, tareas, ahora=AHORA, **kw)
    return horarios_sin_respaldo(oraciones_afirmativas(normalizar(texto)), respaldo, hoy)[0]


def test_respaldo_libres_si_ocupados_no() -> None:
    assert _fallas("Para el jueves 29 tengo a las 3 de la tarde.", [AGENDA]) == []
    # 11:00 está ocupada (`busy_slots`): no respalda.
    assert _fallas("Te agendo el jueves 29 a las 11:00.", [AGENDA])
    assert _fallas("Te agendo el jueves 29 a las 7 de la noche.", [AGENDA])
    # La pregunta no afirma: «¿te sirve a las 7?» no es un horario ofrecido.
    assert _fallas("¿Te sirve el jueves 29 a las 7 de la noche?", [AGENDA]) == []


def test_respaldo_instante_del_servidor_y_error() -> None:
    sub = {"_tool_name": "sub_consultar", "proximo_cargo": "2026-12-09T16:51:00+00:00"}
    assert _fallas("Tu próximo cargo es el 9 de diciembre.", [sub]) == []
    error = {"_tool_name": "check_availability", "_ok": False, "message": "Probá el 30 de octubre a las 10"}
    assert _fallas("Te espero el 30 de octubre a las 10.", [error])


def test_respaldo_horario_del_sistema_y_eco_del_cliente() -> None:
    sistema = "Horario de atención: lunes a viernes de 8:00 a 18:00."
    assert _fallas("Abrimos a las 8 de la mañana.", [], contexto_sistema=sistema) == []
    tarea = {"slots": {"horario": {"value": "2026-10-29T19:00:00", "source": "llm"}}}
    # El cliente pidió las 7 y la tarea tiene esa hora fijada por el cliente: el eco es legítimo.
    assert _fallas("Listo, anoto a las 7 de la noche.", [], (tarea,), contexto_cliente="¿puede ser a las 7 pm?") == []
    # Que el cliente lo diga no basta sin el dato fijado (no puede «lavar» una hora inventada).
    assert _fallas("Listo, anoto a las 7 de la noche.", [], contexto_cliente="¿puede ser a las 7 pm?")


# ---------------------------------------------------------------- modos en verificar()
def _ev(resultados: list[dict[str, Any]], con_estado: bool = True) -> Evidencia:
    return Evidencia.desde(resultados, "", EstadoTareas() if con_estado else None, ahora=AHORA)


def test_shadow_cuenta_y_active_rechaza() -> None:
    texto = "Te agendo el jueves 29 a las 7 de la noche. ¿Te sirve? ¿Y quieres el plan?"
    sombra = verificar(texto, _ev([AGENDA]), [])
    assert sombra.passed and sombra.sombra["horarios_sin_evidencia"] == 1 and sombra.sombra["preguntas_de_mas"] == 0
    texto2 = "Te agendo el jueves 29 a las 9. ¿Te sirve? ¿Quieres el plan?"
    assert verificar(texto2, _ev([AGENDA]), []).sombra["preguntas_de_mas"] == 1
    horario = verificar(texto, _ev([AGENDA]), [], modos=Modos(horarios="active"))
    assert not horario.passed and horario.motivo == "horario_sin_evidencia"
    preguntas = verificar(texto2, _ev([AGENDA]), [], modos=Modos(preguntas="active"))
    assert not preguntas.passed and preguntas.motivo == "demasiadas_preguntas"
    assert verificar("¿Te sirve?", _ev([AGENDA]), [], modos=Modos(preguntas="active", max_preguntas=0)).motivo == (
        "demasiadas_preguntas"
    )


def test_sin_estado_no_cambia_nada() -> None:
    texto = "Te agendo el jueves 29 a las 7 de la noche. ¿Te sirve? ¿Quieres el plan?"
    v = verificar(texto, _ev([AGENDA], con_estado=False), [], modos=Modos(horarios="active", preguntas="active"))
    assert v.passed and "horarios_sin_evidencia" not in v.sombra and "preguntas_de_mas" not in v.sombra


def test_el_monto_va_primero() -> None:
    texto = "El plan cuesta $99.000 y te agendo a las 7 de la noche."
    v = verificar(texto, _ev([AGENDA]), [], modos=Modos(horarios="active"))
    assert v.motivo == "monto_sin_evidencia"


# ---------------------------------------------------------------- robustez (auditoría multitenant)
def test_un_anio_extremo_no_apaga_el_verificador() -> None:
    tarea = {"slots": {"horario": {"value": "9999-12-31T23:00:00-05:00", "source": "llm"}}}
    ev = Evidencia.desde([AGENDA], "", EstadoTareas(tareas=(tarea,)), ahora=AHORA)
    v = verificar("El plan cuesta $99.000.", ev, [], modos=Modos(horarios="active"))
    assert not v.error and v.motivo == "monto_sin_evidencia"  # montos se siguen verificando


def test_de_una_accion_fallida_solo_cuentan_los_libres() -> None:
    fallida = {"_tool_name": "create_calendar_event", "_ok": False, "start": "2026-10-29T19:00:00-05:00", "free_slots": ["09:00"]}
    assert _fallas("Te agendo el jueves 29 a las 7 de la noche.", [fallida])
    assert _fallas("Te puedo agendar a las 9 de la mañana.", [fallida]) == []


def test_un_mensaje_con_cientos_de_horas_tiene_tope() -> None:
    import time

    texto = "3pm " * 5000
    inicio = time.perf_counter()
    assert len(horas_en(normalizar(texto))) <= 40
    respaldo_de([], ({"slots": {"h": {"value": "15:00", "source": "llm"}}},), contexto_cliente=texto, ahora=AHORA)
    assert time.perf_counter() - inicio < 2
