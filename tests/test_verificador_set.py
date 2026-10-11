"""Gate del verificador sobre el set etiquetado (HU-ARCH-29): falsos positivos
≤ 2 % y ningún falso negativo. Si un cambio en el verificador o en las
afirmaciones de un pack lo rompe, este test lo dice antes de llegar a un cliente.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location("eval_verificador", RAIZ / "evals" / "verificador.py")
assert _SPEC and _SPEC.loader
eval_verificador = importlib.util.module_from_spec(_SPEC)
sys.modules["eval_verificador"] = eval_verificador  # los dataclasses resuelven tipos por su módulo
_SPEC.loader.exec_module(eval_verificador)
cargar, medir = eval_verificador.cargar, eval_verificador.medir
UMBRAL_FALSOS_POSITIVOS = eval_verificador.UMBRAL_FALSOS_POSITIVOS


def test_el_set_tiene_volumen_para_medir_el_umbral() -> None:
    casos = cargar()
    # Con menos de ~150 correctas, un solo error ya pasa el 2 %: el umbral sería ruido.
    assert sum(c["etiqueta"] == "correcta" for c in casos) >= 150
    assert any(c["etiqueta"] == "falsa" for c in casos)
    assert len({c["id"] for c in casos}) == len(casos), "ids repetidos en el set"


def test_falsos_positivos_y_negativos_dentro_del_umbral() -> None:
    medicion = medir(cargar())
    assert medicion.tasa_falsos_positivos <= UMBRAL_FALSOS_POSITIVOS, medicion.falsos_positivos
    assert medicion.falsos_negativos == []


def test_los_incidentes_reales_no_se_repiten() -> None:
    """Un caso de incidente (`origen: incidente`) no puede fallar aunque el total quede bajo el
    umbral: con 200 correctas, un falso positivo es 0,5 % y el gate del 2 % no lo vería."""
    incidentes = [c for c in cargar() if c["origen"] == "incidente"]
    assert incidentes
    medicion = medir(incidentes)
    assert medicion.falsos_positivos == [] and medicion.falsos_negativos == []


def test_quitar_un_link_de_una_respuesta_correcta_es_un_falso_positivo() -> None:
    """El link sin respaldo no rechaza el borrador, se quita: el set lo cuenta igual."""
    caso = {
        "id": "x",
        "etiqueta": "correcta",
        "resultados": [],
        "texto": "Términos y condiciones: https://www.solvershub.site/terminos",
    }
    medicion = medir([caso])
    assert [fp["urls_quitadas"] for fp in medicion.falsos_positivos] == [1]
    falsa = {**caso, "etiqueta": "falsa", "urls_sin_respaldo": 1}
    assert medir([falsa]).falsos_negativos == []
    assert medir([{**falsa, "urls_sin_respaldo": 0}]).falsos_negativos != [], "sin links inventados, nada lo detecta"


def test_con_el_estado_de_las_tareas_no_suben_los_falsos_positivos() -> None:
    """HU-AOS-06: la regla de referencias del motor activo, medida sobre las respuestas reales
    con un estado derivado de sus propios resultados, no agrega falsos positivos; y los casos
    con estado explícito (`origen: sintetico-estado`) no fallan ninguno."""
    casos = cargar()
    sin, con = medir(casos), medir(casos, con_estado=True)
    assert con.falsos_negativos == [] and con.tasa_falsos_positivos <= UMBRAL_FALSOS_POSITIVOS
    assert [fp["id"] for fp in con.falsos_positivos] == [fp["id"] for fp in sin.falsos_positivos]
    con_estado = [c for c in casos if c["origen"] == "sintetico-estado"]
    assert sum(c["etiqueta"] == "falsa" for c in con_estado) >= 4
    medicion = medir(con_estado)
    assert medicion.falsos_positivos == [] and medicion.falsos_negativos == []
