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
