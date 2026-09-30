"""SafeRenderer: el core adjunta los datos sensibles, el LLM nunca los ve."""

from __future__ import annotations

from core.orchestrator.safe_renderer import (
    apply_safe_extras,
    extract_safe_extras,
    strip_reserved_keys,
)


def test_sin_claves_reservadas_no_hay_extras() -> None:
    extras = extract_safe_extras([{"ok": True}])
    assert extras.safe_appends == [] and extras.image_url is None


def test_se_extrae_el_texto_a_adjuntar() -> None:
    extras = extract_safe_extras([{"_safe_append": "Cuenta: 123-456"}])
    assert extras.safe_appends == ["Cuenta: 123-456"]


def test_se_acumulan_varios_appends_en_orden() -> None:
    extras = extract_safe_extras([{"_safe_append": "A"}, {"_safe_append": "B"}])
    assert extras.safe_appends == ["A", "B"]


def test_la_ultima_imagen_gana() -> None:
    extras = extract_safe_extras([{"_image_url": "a.png"}, {"_image_url": "b.png"}])
    assert extras.image_url == "b.png"


def test_los_valores_vacios_o_no_texto_se_ignoran() -> None:
    extras = extract_safe_extras([{"_safe_append": "   "}, {"_image_url": 42}])
    assert extras.safe_appends == [] and extras.image_url is None


def test_apply_deja_el_texto_intacto_si_no_hay_extras() -> None:
    extras = extract_safe_extras([{}])
    assert apply_safe_extras("Respuesta", extras) == "Respuesta"


def test_apply_adjunta_verbatim_al_final() -> None:
    extras = extract_safe_extras([{"_safe_append": "Cuenta: 123-456"}])
    resultado = apply_safe_extras("Te envio los datos.", extras)
    assert resultado.endswith("Cuenta: 123-456")
    assert resultado.startswith("Te envio los datos.")


def test_strip_oculta_las_claves_reservadas_al_llm() -> None:
    visible = strip_reserved_keys({"total": 10, "_safe_append": "secreto", "_image_url": "x.png"})
    assert visible == {"total": 10}


def test_strip_no_altera_un_resultado_normal() -> None:
    original = {"total": 10, "moneda": "COP"}
    assert strip_reserved_keys(original) == original


def test_apply_no_duplica_si_el_modelo_ya_escribio_el_append() -> None:
    """Regresión: el LLM copió el link del resultado y el core lo volvía a
    agregar (doble 'Tu cita quedó agendada'). Con dedup no se repite."""
    link = "📅 Tu cita quedó agendada: https://g/abc"
    extras = extract_safe_extras([{"_safe_append": link}])
    resultado = apply_safe_extras("Listo, tu cita quedó agendada.\n\n" + link, extras)
    assert resultado.count(link) == 1


def test_apply_deduplica_appends_identicos_de_varias_tools() -> None:
    extras = extract_safe_extras(
        [{"_safe_append": "Link: https://g/1"}, {"_safe_append": "Link: https://g/1"}]
    )
    resultado = apply_safe_extras("Listo.", extras)
    assert resultado.count("Link: https://g/1") == 1


def test_apply_concatena_appends_distintos() -> None:
    extras = extract_safe_extras(
        [{"_safe_append": "A: 1"}, {"_safe_append": "B: 2"}]
    )
    resultado = apply_safe_extras("Listo.", extras)
    assert "A: 1" in resultado and "B: 2" in resultado
