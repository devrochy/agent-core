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


# ---- línea huérfana (test, 2026-10-10): el verificador quitó la URL que el modelo copió del
# formato del append y la misma línea llegó completa por `_safe_append`.
_LINK = "💳 Podés pagar en línea aquí: https://checkout.wompi.co/l/abc123"


def test_se_quita_la_linea_huerfana_que_repite_el_append_sin_url() -> None:
    texto = "¡Listo! Tu pedido OC-0007 quedó creado.\n\n💳 Podés pagar en línea aquí:\n\n¿Algo más?"
    final = apply_safe_extras(texto, extract_safe_extras([{"_safe_append": _LINK}]))
    assert final == "¡Listo! Tu pedido OC-0007 quedó creado.\n\n¿Algo más?\n\n" + _LINK
    assert final.count("Podés pagar en línea aquí") == 1


def test_la_linea_huerfana_con_vineta_o_sin_dos_puntos_tambien_se_quita() -> None:
    for huerfana in ("- 💳 Podés pagar en línea aquí:", "💳 Podés pagar en línea aquí", "* 💳  Podés pagar en línea aquí: "):
        final = apply_safe_extras(f"Pedido creado.\n{huerfana}", extract_safe_extras([{"_safe_append": _LINK}]))
        assert final == "Pedido creado.\n\n" + _LINK, huerfana


def test_si_solo_quedaba_la_linea_huerfana_queda_el_append() -> None:
    final = apply_safe_extras("💳 Podés pagar en línea aquí:", extract_safe_extras([{"_safe_append": _LINK}]))
    assert final == _LINK


def test_no_se_tocan_las_lineas_con_otra_url_ni_las_que_no_repiten_el_append() -> None:
    texto = (
        "Términos: https://www.solvershub.site/terminos\n"
        "Podés pagar cuando quieras.\n"
        "💳 Podés pagar en línea aquí: https://otro.link/viejo"
    )
    final = apply_safe_extras(texto, extract_safe_extras([{"_safe_append": _LINK}]))
    assert final == texto + "\n\n" + _LINK


def test_las_lineas_sin_url_del_append_no_cuentan_como_huerfanas() -> None:
    """Un bloque de transferencia no trae URL: una línea del modelo igual a su encabezado no
    es un link que quitó el verificador."""
    append = "💳 Datos para tu transferencia:\nNequi 300 000 0000"
    texto = "Te paso los datos.\n💳 Datos para tu transferencia:"
    assert apply_safe_extras(texto, extract_safe_extras([{"_safe_append": append}])) == texto + "\n\n" + append
