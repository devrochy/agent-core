"""SPEC-47, HU-AOS-82 · horario propio del agente (reglas puras).

- Se valida (zona horaria, días 0-6, HH:MM con inicio < fin) y se ordena.
- Sin horario propio, o con uno guardado inválido, vale el del negocio (fallback, nunca falla).
- El override solo reemplaza `business_hours`: modo y aviso de fuera de horario siguen del negocio.
"""

from __future__ import annotations

import pytest

from core.agents import horario_agente as ha
from core.kernel import horario_de_servicio
from core.orchestrator.escalation import within_business_hours

NEGOCIO = {"tz": "America/Bogota", "ranges": [{"day": d, "from": "09:00", "to": "19:00"} for d in range(5)]}
PROPIO = {"tz": "America/Bogota", "ranges": [{"day": 0, "from": "08:00", "to": "10:00"}]}


def test_normaliza_y_ordena() -> None:
    crudo = {"tz": "America/Bogota", "ranges": [{"day": 2, "from": "14:00", "to": "16:00"}, {"day": 0, "from": "08:00", "to": "10:00"}]}
    assert [r["day"] for r in ha.normalizar(crudo)["ranges"]] == [0, 2]
    assert ha.normalizar(None) is None


@pytest.mark.parametrize(
    "crudo",
    [
        {"tz": "Marte/Base", "ranges": [{"day": 0, "from": "08:00", "to": "10:00"}]},
        {"tz": "America/Bogota", "ranges": []},
        {"tz": "America/Bogota", "ranges": [{"day": 7, "from": "08:00", "to": "10:00"}]},
        {"tz": "America/Bogota", "ranges": [{"day": 0, "from": "10:00", "to": "08:00"}]},
        {"tz": "America/Bogota", "ranges": [{"day": True, "from": "08:00", "to": "10:00"}]},
        "no",
    ],
)
def test_rechaza_lo_invalido(crudo) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(ha.HorarioInvalido):
        ha.normalizar(crudo)


def test_fallback_al_negocio() -> None:
    assert ha.efectivo(None, NEGOCIO) == NEGOCIO
    assert ha.efectivo({"tz": "x", "ranges": []}, NEGOCIO) == NEGOCIO  # inválido guardado: no falla
    assert ha.efectivo(PROPIO, NEGOCIO) == ha.normalizar(PROPIO)


def test_el_override_solo_cambia_el_horario() -> None:
    escalation = {"business_hours": NEGOCIO, "business_hours_mode": "bloquea", "out_of_hours_message": "Volvemos mañana"}
    con = ha.con_horario_del_agente(escalation, PROPIO)
    assert con["business_hours"] == ha.normalizar(PROPIO)
    assert con["out_of_hours_message"] == "Volvemos mañana" and con["business_hours_mode"] == "bloquea"
    assert escalation["business_hours"] == NEGOCIO  # no muta el del negocio
    assert ha.con_horario_del_agente(escalation, None) == escalation
    assert ha.con_horario_del_agente(None, None) == {}


def test_fuera_de_horario_del_agente_dentro_del_negocio() -> None:
    from datetime import datetime
    from zoneinfo import ZoneInfo

    lunes_11 = datetime(2026, 11, 16, 11, 0, tzinfo=ZoneInfo("America/Bogota"))
    assert within_business_hours(NEGOCIO, lunes_11)
    assert not within_business_hours(ha.efectivo(PROPIO, NEGOCIO), lunes_11)


def test_el_prompt_informativo_usa_el_horario_del_agente() -> None:
    escalation = {"business_hours": NEGOCIO, "business_hours_mode": "informativo"}
    del_negocio = horario_de_servicio.para_prompt(escalation, "es")
    del_agente = horario_de_servicio.para_prompt(ha.con_horario_del_agente(escalation, PROPIO), "es")
    assert del_negocio != del_agente and "08:00" in del_agente
    # Sin horario propio el bloque es idéntico (las grabaciones de los journeys siguen valiendo).
    assert horario_de_servicio.para_prompt(ha.con_horario_del_agente(escalation, None), "es") == del_negocio


def test_huella_estable() -> None:
    assert ha.huella(None) is None
    assert ha.huella(ha.normalizar(PROPIO)) == ha.huella(ha.normalizar(dict(PROPIO)))
