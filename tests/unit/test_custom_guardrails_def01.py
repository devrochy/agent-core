"""DEF-01: las líneas mal formadas del DSL de guardrails se reportan, no se pierden."""

from core.admin.advanced_service import (
    parse_custom_guardrails,
    parse_custom_guardrails_report,
)


def test_reporta_lineas_mal_formadas_y_conserva_las_validas() -> None:
    rules, ignoradas = parse_custom_guardrails_report(
        "max_length | 400\n"
        "basura sin separador\n"
        "max_length | muchas\n"
        "required_tool | pedido, compra | crear_pedido\n"
        "tipo_raro | 1\n"
        "forbidden_phrase | groserías | escalate"
    )
    assert [r["kind"] for r in rules] == ["max_length", "required_tool", "forbidden_phrase"]
    assert len(ignoradas) == 3
    assert "línea 2" in ignoradas[0]
    assert "línea 3" in ignoradas[1] and "número" in ignoradas[1]
    assert "línea 5" in ignoradas[2] and "desconocido" in ignoradas[2]


def test_parser_clasico_sigue_devolviendo_solo_reglas() -> None:
    rules = parse_custom_guardrails("max_length | 200\ntexto suelto")
    assert rules == [{"kind": "max_length", "value": 200, "action": "retry"}]


def test_texto_valido_no_reporta_nada() -> None:
    rules, ignoradas = parse_custom_guardrails_report("max_length | 300 | escalate\n\n")
    assert rules and ignoradas == []
