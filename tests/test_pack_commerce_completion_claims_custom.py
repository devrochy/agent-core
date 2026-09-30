"""`_parse_custom_completion_claims` (ADR-0025): el DSL de
`CRMConfig.completion_claims_custom` -- un claim por línea,
`etiqueta: frase uno | frase dos => tool_uno, tool_dos`.

El caso de seguridad clave: las frases NUNCA se interpretan como regex
-- se escapan (`re.escape`) antes de compilar, así que un tenant no
puede introducir un patrón costoso (ReDoS) ni uno que rompa la
compilación, aunque escriba metacaracteres de regex sin saberlo.
"""

from __future__ import annotations

from pack_commerce.module import _parse_custom_completion_claims


def test_una_linea_valida():
    claims = _parse_custom_completion_claims("agenda_cita: agendé tu cita => crear_cita")
    assert set(claims) == {"agenda_cita"}
    pattern, tools = claims["agenda_cita"]
    assert tools == {"crear_cita"}
    assert pattern.search("listo, agendé tu cita para el jueves")
    assert not pattern.search("no tengo nada que ver con esto")


def test_multiples_frases_y_tools():
    claims = _parse_custom_completion_claims(
        "agenda_cita: agendé tu cita | quedó reservada tu cita => crear_cita, notificar_cita"
    )
    pattern, tools = claims["agenda_cita"]
    assert tools == {"crear_cita", "notificar_cita"}
    assert pattern.search("quedó reservada tu cita")
    assert pattern.search("ya agendé tu cita")


def test_multiples_lineas_producen_multiples_claims():
    raw = (
        "agenda_cita: agendé tu cita => crear_cita\n"
        "cancelacion: cancelé tu cita => cancelar_cita\n"
    )
    claims = _parse_custom_completion_claims(raw)
    assert set(claims) == {"agenda_cita", "cancelacion"}


def test_lineas_vacias_y_comentarios_se_ignoran():
    raw = "\n# esto es un comentario\n\nagenda_cita: agendé tu cita => crear_cita\n\n"
    claims = _parse_custom_completion_claims(raw)
    assert set(claims) == {"agenda_cita"}


def test_linea_sin_separador_de_tools_se_ignora():
    """Sin `=>` no hay forma de saber qué tool exigir -- se descarta, no
    revienta (fail-open, mismo criterio que el resto de guardrails.py)."""
    claims = _parse_custom_completion_claims("agenda_cita: agendé tu cita")
    assert claims == {}


def test_linea_sin_etiqueta_se_ignora():
    claims = _parse_custom_completion_claims(": agendé tu cita => crear_cita")
    assert claims == {}


def test_linea_sin_frases_o_sin_tools_se_ignora():
    assert _parse_custom_completion_claims("agenda_cita:  => crear_cita") == {}
    assert _parse_custom_completion_claims("agenda_cita: agendé tu cita => ") == {}


def test_string_vacio_no_produce_claims():
    assert _parse_custom_completion_claims("") == {}


def test_metacaracteres_de_regex_se_tratan_literal():
    """El caso de seguridad clave: una frase con paréntesis/asterisco/signo
    de pregunta NO se interpreta como regex -- matchea el string exacto,
    literal, y nunca puede volverse un patrón costoso (ReDoS)."""
    claims = _parse_custom_completion_claims(
        "raro: cerré (tu) pedido* de verdad? => crear_pedido"
    )
    pattern, _ = claims["raro"]
    # matchea el texto EXACTO con los símbolos tal cual
    assert pattern.search("te confirmo: cerré (tu) pedido* de verdad? listo")
    # no los interpreta como grupo/cuantificador/interrogación regex
    assert not pattern.search("cerré tu pedido de verdad")


def test_mas_de_50_lineas_se_trunca():
    """Defensa en profundidad además del `max_length` del campo en
    CRMConfig -- este guardrail corre en CADA turno, un tenant con miles
    de líneas no debe degradar la latencia de su propia conversación sin
    límite."""
    raw = "\n".join(f"claim_{i}: frase {i} => tool_{i}" for i in range(60))
    claims = _parse_custom_completion_claims(raw)
    assert len(claims) == 50
    assert "claim_0" in claims
    assert "claim_49" in claims
    assert "claim_50" not in claims


def test_nombre_custom_puede_coincidir_con_uno_incorporado():
    """Si el tenant reutiliza el nombre de un claim incorporado (ej.
    "pedido"), `_resolved_completion_claims` lo reemplaza -- acá solo se
    prueba que el parser no le da ningún trato especial al nombre."""
    claims = _parse_custom_completion_claims("pedido: ya despaché tu pedido => marcar_despachado")
    assert set(claims) == {"pedido"}
    _, tools = claims["pedido"]
    assert tools == {"marcar_despachado"}
