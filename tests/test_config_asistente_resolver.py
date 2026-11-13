"""Asistente de configuración: respuestas → cambios sobre el registro (lógica pura)."""

from __future__ import annotations

import pytest

from core.config_registro import POR_CLAVE, catalogo_para
from core.config_registro.asistente import (
    GUIONES,
    MOTIVO_VOZ_V3,
    RespuestasInvalidas,
    guiones_visibles,
    resolver,
)

DE_FABRICA = {c: o.por_defecto for c, o in POR_CLAVE.items()}


def _cambios(p) -> dict:
    return {c.clave: c.propuesto for c in p.cambios}


def test_todas_las_opciones_del_guion_proponen_valores_validos() -> None:
    """Se valida al importar (`Pregunta._coherente`): aquí solo se comprueba que el guion no está vacío."""
    assert {"voz", "atencion"} <= set(GUIONES)
    assert all(g.preguntas for g in GUIONES.values())


def test_tono_directo_no_cambia_la_persona() -> None:
    # La voz sale de la versión publicada del agente (ADR-0191): el asistente no la toca.
    p = resolver("voz", {"tono": "directo"}, DE_FABRICA)
    assert p.cambios == []
    assert {o.clave for o in p.omitidos} == {"persona_tone", "persona_warmth"}


def test_pregunta_sin_respuesta_no_cambia_nada() -> None:
    p = resolver("voz", {}, DE_FABRICA)
    assert p.cambios == []


def test_responder_lo_que_ya_hay_no_propone_cambios() -> None:
    actual = {**DE_FABRICA, "persona_tone": "neutral", "persona_warmth": 2}
    assert resolver("voz", {"tono": "directo"}, actual).cambios == []


def test_el_largo_cambia_solo_el_estilo_de_envio() -> None:
    p = resolver("voz", {"largo": "detalladas"}, DE_FABRICA)
    assert _cambios(p) == {"split_style": "pocos_cortos"}
    assert [o.clave for o in p.omitidos] == ["persona_max_length"]


def test_marca_lo_que_el_dueno_ya_habia_cambiado_a_mano() -> None:
    actual = {**DE_FABRICA, "split_style": "varios_cortos"}
    p = resolver("voz", {"largo": "detalladas"}, actual)
    (cambio,) = p.cambios
    assert (cambio.actual, cambio.propuesto, cambio.editado_a_mano) == ("varios_cortos", "pocos_cortos", True)
    sin_tocar = resolver("voz", {"largo": "detalladas"}, DE_FABRICA)
    assert sin_tocar.cambios[0].editado_a_mano is False


def test_las_preguntas_de_varias_conservan_lo_que_escribio_el_dueno() -> None:
    actual = {**DE_FABRICA, "escalation_keywords": ["asesor", "mi frase", "Mi Frase"]}
    p = resolver("atencion", {"escalar_motivos": ["pago"]}, actual)
    palabras = _cambios(p)["escalation_keywords"]
    # «asesor» era de un motivo no elegido: sale. «mi frase» es del dueño: se queda, sin repetirse.
    assert palabras == ["mi frase", "me cobraron", "cobro doble", "pago no llegó", "reembolso"]


def test_varias_vacia_quita_lo_sugerido_y_deja_lo_propio() -> None:
    actual = {**DE_FABRICA, "escalation_keywords": ["asesor", "mi frase"]}
    p = resolver("atencion", {"escalar_motivos": []}, actual)
    assert _cambios(p) == {"escalation_keywords": ["mi frase"]}


def test_atender_siempre_omite_lo_que_depende_del_horario() -> None:
    actual = {**DE_FABRICA, "business_ranges": [{"day": 0, "from": "08:00", "to": "18:00"}]}
    p = resolver("atencion", {"horario": "siempre", "fuera_de_horario": "avisa"}, actual)
    assert _cambios(p) == {"business_ranges": []}
    assert {o.clave for o in p.omitidos} == {"business_hours_mode", "out_of_hours_message"}


def test_con_horario_el_modo_fuera_de_horario_si_aplica() -> None:
    actual = {**DE_FABRICA, "business_hours_mode": "informativo"}
    p = resolver("atencion", {"horario": "semana", "fuera_de_horario": "avisa"}, actual)
    cambios = _cambios(p)
    assert [f["day"] for f in cambios["business_ranges"]] == [0, 1, 2, 3, 4]
    assert cambios["business_hours_mode"] == "bloquea"
    assert p.omitidos == []


def test_avisar_fuera_de_horario_deja_vacio_el_mensaje_para_usar_el_del_sistema() -> None:
    actual = {
        **DE_FABRICA,
        "business_ranges": [{"day": 0, "from": "08:00", "to": "18:00"}],
        "out_of_hours_message": "Texto viejo",
    }
    p = resolver("atencion", {"fuera_de_horario": "avisa"}, actual)
    assert _cambios(p)["out_of_hours_message"] == ""


def test_la_voz_se_omite_y_se_dice_por_que() -> None:
    p = resolver("voz", {"tono": "directo", "largo": "detalladas"}, DE_FABRICA)
    assert _cambios(p) == {"split_style": "pocos_cortos"}
    assert {o.clave for o in p.omitidos} == {"persona_tone", "persona_warmth", "persona_max_length"}
    assert {o.motivo for o in p.omitidos} == {MOTIVO_VOZ_V3}


def test_el_guion_no_ofrece_las_preguntas_solo_de_voz() -> None:
    ids = {g.seccion: [q.id for q in g.preguntas] for g in guiones_visibles()}
    assert ids["voz"] == ["largo"]
    assert ids["atencion"] == [q.id for q in GUIONES["atencion"].preguntas]
    # El registro completo sigue intacto: el filtro es por copia.
    assert [q.id for q in GUIONES["voz"].preguntas] == ["tono", "emojis", "largo", "prohibidas"]


@pytest.mark.parametrize(
    "respuestas",
    [
        {"no_existe": "x"},
        {"tono": "inventado"},
        {"tono": ["cercano"]},
        {"prohibidas": "gratis"},
        {"prohibidas": ["no_existe"]},
    ],
)
def test_respuestas_fuera_del_guion_se_rechazan(respuestas) -> None:
    with pytest.raises(RespuestasInvalidas) as exc:
        resolver("voz", respuestas, DE_FABRICA)
    assert exc.value.por_pregunta


def test_seccion_desconocida() -> None:
    with pytest.raises(ValueError, match="Sección desconocida"):
        resolver("cobros", {}, DE_FABRICA)


def test_la_huella_depende_de_lo_propuesto() -> None:
    a = resolver("voz", {"largo": "detalladas"}, DE_FABRICA)
    b = resolver("voz", {"largo": "detalladas"}, DE_FABRICA)
    c = resolver("voz", {"largo": "cortas"}, DE_FABRICA)
    assert a.huella == b.huella != c.huella


def test_el_catalogo_incluye_el_guion() -> None:
    catalogo = catalogo_para(es_master=False, guardrails_disponibles=[])
    assert [g.seccion for g in catalogo.asistente] == ["voz", "atencion"]
    assert [q.id for q in catalogo.asistente[0].preguntas] == ["largo"]
