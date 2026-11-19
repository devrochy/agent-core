"""Horario informativo (HU-AOS-58): el horario es del equipo humano y el asistente atiende 24/7."""

from __future__ import annotations

import pytest

from core.kernel import horario_de_servicio as h
from core.llm.prompt_builder import PromptContext, bloque_dinamico

LUN_VIE = [{"day": d, "from": "08:00", "to": "18:00"} for d in range(5)]
SEMANA = {"tz": "America/Bogota", "ranges": [*LUN_VIE, {"day": 5, "from": "9:00", "to": "13:00"}]}


def test_el_playbook_del_kernel_es_valido() -> None:
    assert set(h.textos()) == {"encabezado", "es", "en", "pt"}


def test_modo_solo_es_informativo_si_esta_guardado_asi() -> None:
    assert h.modo_del_horario({"business_hours_mode": "informativo"}) == "informativo"
    for crudo in (None, {}, {"business_hours_mode": "bloquea"}, {"business_hours_mode": "otro"}):
        assert h.modo_del_horario(crudo) == "bloquea"


def test_resumen_agrupa_dias_iguales() -> None:
    assert h.resumen(SEMANA) == "lunes a viernes de 08:00 a 18:00; sábado de 09:00 a 13:00"


def test_resumen_con_dos_franjas_dias_sueltos_e_idiomas() -> None:
    partido = {
        "ranges": [
            {"day": 0, "from": "08:00", "to": "12:00"},
            {"day": 0, "from": "14:00", "to": "18:00"},
        ]
    }
    assert h.resumen(partido) == "lunes de 08:00 a 12:00 y de 14:00 a 18:00"
    assert h.resumen(
        {
            "ranges": [
                {"day": 1, "from": "08:00", "to": "12:00"},
                {"day": 3, "from": "08:00", "to": "12:00"},
            ]
        }
    ) == ("martes y jueves de 08:00 a 12:00")
    assert h.resumen(SEMANA, "en").startswith("Monday to Friday from 08:00 to 18:00")


@pytest.mark.parametrize(
    "crudo", [None, {}, {"ranges": []}, {"ranges": "x"}, {"ranges": [None, {"day": 9}, {"day": 1}]}]
)
def test_sin_franjas_validas_no_hay_resumen(crudo: object) -> None:
    assert h.resumen(crudo) == ""  # type: ignore[arg-type]


def test_el_bloque_solo_existe_con_horario_informativo() -> None:
    informativo = {"business_hours_mode": "informativo", "business_hours": SEMANA}
    bloque = h.para_prompt(informativo, "es")
    assert bloque.startswith("## Horario de atención del equipo\n")
    assert "lunes a viernes de 08:00 a 18:00" in bloque and "toda hora" in bloque
    assert h.para_prompt({"business_hours": SEMANA}, "es") == ""
    assert h.para_prompt({"business_hours_mode": "informativo"}, "es") == ""
    assert h.para_prompt(None, "es") == ""


def test_el_prompt_no_cambia_si_no_hay_horario_de_servicio() -> None:
    base = dict(assistant_name="Ana", business_description="Tienda")
    sin = bloque_dinamico(PromptContext(**base))
    con = bloque_dinamico(
        PromptContext(
            **base,
            service_hours=h.para_prompt(
                {"business_hours_mode": "informativo", "business_hours": SEMANA}, "es"
            ),
        )
    )
    assert "Horario de atención del equipo" not in sin
    assert "Horario de atención del equipo" in con
    # Va antes de la fecha, que sigue siendo lo último (no toca el prefijo cacheado).
    assert con.rindex("## Fecha y hora actual") > con.index("Horario de atención del equipo")
