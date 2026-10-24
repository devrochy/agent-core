"""Clasificador de confirmación escrita (HU-AOS-09): determinista, conservador y medido.

El set etiquetado (`tests/fixtures/confirmacion/etiquetado.jsonl`) se mide sin LLM. La etiqueta es
"el cliente confirma de forma explícita e inequívoca"; lo ambiguo ("sí", "dale", "ok") va como
`false` por decisión del dueño. La exigencia es precisión 1.0: un falso "sí" ejecutaría algo que
compromete. El recall se reporta (un falso "no" solo hace que se vuelva a pedir).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.kernel.procedures import clasificador, textos
from core.kernel.tarjeta import RotulosInvalidos

_SET = Path(__file__).parent / "fixtures" / "confirmacion" / "etiquetado.jsonl"


def _etiquetado() -> list[tuple[str, bool]]:
    filas = [json.loads(linea) for linea in _SET.read_text(encoding="utf-8").splitlines() if linea.strip()]
    return [(f["texto"], bool(f["confirma"])) for f in filas]


def test_set_etiquetado_precision_total() -> None:
    filas = _etiquetado()
    assert len(filas) >= 150
    falsos_si = [t for t, etiqueta in filas if clasificador.es_confirmacion(t) and not etiqueta]
    assert falsos_si == [], f"falsos positivos: {falsos_si}"


def test_set_etiquetado_recall_reportado() -> None:
    positivos = [t for t, etiqueta in _etiquetado() if etiqueta]
    aciertos = sum(clasificador.es_confirmacion(t) for t in positivos)
    recall = aciertos / len(positivos)
    print(f"recall del clasificador de confirmación: {recall:.2%} ({aciertos}/{len(positivos)})")
    assert recall >= 0.85  # piso de regresión: el léxico solo crece con el set


def test_todo_lo_que_aceptaba_el_lexico_exacto_sigue_aceptado() -> None:
    for frase in textos.lexico_si():
        assert clasificador.es_confirmacion(frase), frase


@pytest.mark.parametrize("texto", ["sí", "Sí.", "si", "SI!!", "dale", "ok", "listo", "claro que sí", "👍", "", "   "])
def test_si_a_secas_y_lo_ambiguo_no_confirman(texto: str) -> None:
    """Decisión del dueño: el "sí" a secas no confirma por escrito."""
    assert not clasificador.es_confirmacion(texto)
    assert not textos.confirmacion_alta(texto)


@pytest.mark.parametrize(
    "texto",
    ["¿confirmo?", "confirmo pero cambia el correo", "no confirmo", "confirmo mañana", "confirmo que no",
     "sí confirmo " + "todo " * 8],
)
def test_preguntas_negaciones_y_largos_no_confirman(texto: str) -> None:
    assert not clasificador.es_confirmacion(texto)


@pytest.mark.parametrize("texto", ["Sí, confirmo", "confirmo 👍", "ok, confirmo el pedido", "Acepto, gracias", "así es"])
def test_confirmaciones_explicitas(texto: str) -> None:
    assert clasificador.es_confirmacion(texto)
    assert textos.confirmacion_alta(texto)


def test_lexico_roto_falla_cerrado() -> None:
    completo = "\n".join(f"## {s}\nconfirmo" if s in ("si", "nucleo") else f"## {s}\nx{s}" for s in clasificador._SECCIONES)
    clasificador.parsear_lexico(completo)
    with pytest.raises(RotulosInvalidos):
        clasificador.parsear_lexico("## si\nconfirmo\n## nucleo\nconfirmo\n")  # faltan secciones
    with pytest.raises(RotulosInvalidos):
        clasificador.parsear_lexico(completo.replace("## negaciones\nxnegaciones", "## negaciones\nconfirmo"))


def test_el_playbook_del_kernel_carga() -> None:
    lx = clasificador.lexico()
    assert "confirmo" in lx.nucleo and "si" in lx.prefijos and "no" in lx.negaciones
    assert "si" not in lx.nucleo  # un "sí" solo nunca es núcleo


# pass^4 de los procedimientos (2026-10-18): "Sí, confirmo. Pasame el link de pago" no confirmaba y la
# compra terminaba en relevo. Pedir el paso siguiente es cortesía: el núcleo sigue siendo obligatorio.
@pytest.mark.parametrize(
    "texto",
    ["Sí, confirmo. Pasame el link de pago", "Sí, confirmo. Espero el link.", "Confirmo, envíame el link de pago"],
)
def test_confirmo_y_pedir_el_link_confirma(texto: str) -> None:
    assert clasificador.es_confirmacion(texto), texto


@pytest.mark.parametrize(
    "texto",
    ["Pasame el link", "Pasame el link de pago", "no, pasame el link", "¿me pasás el link?", "Sí, pasame el link",
     "Espero el link", "no confirmo, mandame el link"],
)
def test_pedir_el_link_sin_confirmar_no_confirma(texto: str) -> None:
    assert not clasificador.es_confirmacion(texto), texto


@pytest.mark.parametrize(
    "texto",
    ["Sí, confirmo. Todo bien.", "confirmo, todo bien", "sí, todo correcto, bien", "acepto, todo bien, gracias"],
)
def test_confirmar_y_agregar_que_todo_esta_bien_sigue_confirmando(texto: str) -> None:
    """Medición pass^4 del 2026-10-23: el cliente simulado escribió «Sí, confirmo. Todo bien.», el motor no lo tomó
    por confirmación, el modelo dijo que quedó registrada y nunca se creó el pedido. «bien» acompaña a una marca de
    confirmación; solo, sigue sin confirmar."""
    assert clasificador.es_confirmacion(texto) is True


@pytest.mark.parametrize(
    "texto",
    ["todo bien", "sí, todo bien", "bien", "bien, gracias", "confirmo pero no todo bien", "¿todo bien? confirmo"],
)
def test_bien_solo_no_confirma_y_una_reserva_o_pregunta_tampoco(texto: str) -> None:
    assert clasificador.es_confirmacion(texto) is False


def test_el_mensaje_al_modelo_sin_confirmacion_le_prohibe_darla_por_hecha() -> None:
    """El modelo recibió «falta confirmación» tres veces y aun así dijo «ya quedó registrada tu confirmación»."""
    from core.kernel.procedures.textos import mensaje

    texto = mensaje("falta_confirmacion")
    assert "NO hay nada confirmado" in texto and "No digas que quedó confirmado" in texto
    assert "«Sí, confirmo»" in texto


_RESUMEN = (
    "Antes de seguir, revisa que todo esté bien:\n• Plan: Premium anual\n• Total: $6.000.000\n"
    "• Nombre completo: Ana Gómez\n• Documento: 6002271728\n• Forma de pago: Transferencia\n\n"
    "Si todo está bien, responde *Sí, confirmo*; si algo no, escríbeme qué cambiar."
)


@pytest.mark.parametrize(
    "texto",
    ["Sí, confirmo. Todo bien con el anual.", "Sí, confirmo el anual", "confirmo con el plan premium",
     "Confirmo, todo bien con la transferencia"],
)
def test_confirmar_nombrando_lo_que_dice_el_resumen_confirma(texto: str) -> None:
    """Medición pass^4 del 2026-10-24: «Sí, confirmo. Todo bien con el anual.» no confirmaba y el modelo
    dijo que quedó registrada. Las palabras del resumen que el cliente VIO acompañan a la marca."""
    assert clasificador.es_confirmacion(texto, eco=_RESUMEN) is True
    assert clasificador.es_confirmacion(texto) is False  # sin el resumen, el léxico no alcanza


@pytest.mark.parametrize(
    "texto",
    [
        "Sí, confirmo. Todo bien con el mensual.",  # otro plan: no es lo que se le mostró
        "confirmo el básico",
        "todo bien con el anual",  # sin marca de confirmación
        "sí, el anual",
        "el anual",
        "confirmo pero cambia el anual",  # una reserva
        "confirmo, no es el anual",  # una negación
        "¿confirmo el anual?",  # una pregunta
        "confirmo el anual mañana",
    ],
)
def test_el_resumen_no_confirma_solo_ni_valida_lo_que_no_dice(texto: str) -> None:
    assert clasificador.es_confirmacion(texto, eco=_RESUMEN) is False


def test_las_negaciones_del_resumen_nunca_se_vuelven_eco() -> None:
    """Si el resumen trae «cambiar» o «no» (la instrucción final), el cliente no los gana como palabras."""
    assert clasificador.es_confirmacion("confirmo cambiar", eco=_RESUMEN) is False
    assert clasificador.es_confirmacion("confirmo no", eco=_RESUMEN) is False
    assert clasificador.es_confirmacion("confirmo algo", eco="no hay nada") is False


def test_el_eco_no_reemplaza_a_la_marca_de_confirmacion() -> None:
    for texto in ("anual", "sí, anual", "ok, premium anual", "bien con el anual", "dale, con el plan"):
        assert clasificador.es_confirmacion(texto, eco=_RESUMEN) is False, texto


def test_el_eco_no_cambia_lo_que_ya_confirmaba_ni_lo_que_no() -> None:
    for texto in ("Sí, confirmo", "confirmo, todo bien", "acepto"):
        assert clasificador.es_confirmacion(texto, eco=_RESUMEN) is True
    for texto in ("sí", "ok", "listo", "dale", "no confirmo", "¿confirmo?"):
        assert clasificador.es_confirmacion(texto, eco=_RESUMEN) is False, texto


def test_confirmacion_suficiente_pasa_el_eco_solo_a_la_via_estricta() -> None:
    from core.kernel.procedures.textos import confirmacion_suficiente

    texto = "Sí, confirmo. Todo bien con el anual."
    assert confirmacion_suficiente(texto, kind="action", data_class="payment", eco=_RESUMEN) is True
    assert confirmacion_suficiente(texto, kind="action", data_class="payment") is False
    # Un dato personal usa el asentimiento: el eco no lo ayuda.
    assert confirmacion_suficiente(texto, kind="slot", data_class="pii", eco=_RESUMEN) is False


def test_las_palabras_que_invierten_el_sentido_no_se_vuelven_eco() -> None:
    """Revisión del auditor: «sin», «salvo», «solo», «anular»… son negaciones aunque el resumen las traiga."""
    resumen = "Plan: Premium anual, sin días de prueba. Solo se anula con aviso."
    for texto in ("confirmo sin días", "confirmo salvo el anual", "confirmo solo el anual", "confirmo anular"):
        assert clasificador.es_confirmacion(texto, eco=resumen) is False, texto
    assert clasificador.es_confirmacion("Sí, confirmo el anual", eco=resumen) is True
