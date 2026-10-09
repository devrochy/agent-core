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
